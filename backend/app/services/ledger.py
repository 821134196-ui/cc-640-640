"""账本服务（只追加、锁账后只能追加调整单）。

流水类型：
- CHARGE    实际向客户收取的现金（预付款 / 未付款单的空跑费收款）
- FEE       扣费归属行：从已收预收款中划转为空跑费（不新增现金流入）
- REFUND    实际退给客户的现金
- SURCHARGE 改判后补收的现金

现金净支出恒等式（对账依据）：
    net_paid = CHARGE + SURCHARGE - REFUND = 最终空跑费
已付款单：CHARGE=预付款，REFUND=预付-空跑费，差额即扣费（另以 FEE 行显式入账）。
未付款单：CHARGE=空跑费。两者净额都精确等于最终费用。

不变量：
- 流水一旦写入即不可变，不做 UPDATE/DELETE。
- 改判只通过新增 REFUND/SURCHARGE 找平，绝不修改原结算行。
- 每行有唯一幂等键，重复取消/退款重试只命中同一行，不产生第二笔。
"""
from __future__ import annotations

from sqlalchemy import select

from .. import models
from . import payment
from .clock import now

CHARGE = "CHARGE"
FEE = "FEE"
REFUND = "REFUND"
SURCHARGE = "SURCHARGE"


def _add_entry(
    db,
    *,
    order_id,
    entry_type,
    amount_cents,
    stage,
    idempotency_key,
    currency="CNY",
    cancellation_id=None,
    adjudication_id=None,
    payment_ref=None,
    reason=None,
    operator=None,
):
    """幂等地写入一条 settled 流水；同键已存在则原样返回（不重复入账）。"""
    existing = db.scalar(
        select(models.LedgerEntry).where(models.LedgerEntry.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing, False

    entry = models.LedgerEntry(
        order_id=order_id,
        cancellation_id=cancellation_id,
        adjudication_id=adjudication_id,
        entry_type=entry_type,
        stage=stage,
        amount_cents=amount_cents,
        currency=currency,
        status="settled",
        payment_ref=payment_ref,
        idempotency_key=idempotency_key,
        reason=reason,
        operator=operator,
        created_at=now(),
    )
    db.add(entry)
    db.flush()
    return entry, True


def list_entries(db, order_id):
    """按插入顺序返回（rowid 单调），避免同一时刻 created_at 相同时被 UUID 主键打乱。"""
    from sqlalchemy import text

    rows = db.execute(
        select(models.LedgerEntry)
        .where(models.LedgerEntry.order_id == order_id)
        .order_by(text("rowid"))
    ).scalars()
    return list(rows)


def totals(db, order_id) -> dict:
    entries = list_entries(db, order_id)
    charge = sum(e.amount_cents for e in entries if e.entry_type == CHARGE)
    fee = sum(e.amount_cents for e in entries if e.entry_type == FEE)
    refund = sum(e.amount_cents for e in entries if e.entry_type == REFUND)
    surcharge = sum(e.amount_cents for e in entries if e.entry_type == SURCHARGE)
    net = charge + surcharge - refund
    return {
        "charge_cents": charge,
        "fee_alloc_cents": fee,
        "refund_cents": refund,
        "surcharge_cents": surcharge,
        # 客户最终实际承担的现金
        "net_paid_cents": net,
        "count": len(entries),
    }


def settle_prepayment(db, order) -> dict | None:
    """已付款订单：把预收款入账（CHARGE/prepay）。幂等。"""
    if not order.paid or order.prepaid_cents <= 0:
        return None
    key = f"order:{order.id}:prepay"
    gw = payment.charge(db, key, order.prepaid_cents)
    if not gw.ok:
        return {"ok": False, "error": gw.error, "stage": "prepay"}
    entry, created = _add_entry(
        db,
        order_id=order.id,
        entry_type=CHARGE,
        amount_cents=order.prepaid_cents,
        stage="prepay",
        idempotency_key=key,
        payment_ref=gw.payment_ref,
        reason="下单预付款入账",
    )
    return {"ok": True, "entry_id": entry.id, "created": created, "payment_ref": gw.payment_ref}


def settle_cancellation(db, order, cancellation) -> dict:
    """取消结算。扣费与退款分别成行，全部幂等（重试/重复取消只处理一次）。"""
    fee = cancellation.final_amount_cents
    result = {"fee_cents": fee, "lines": []}

    if order.paid:
        # 1) 扣费：从预收款中划转出空跑费（显式 FEE 行，不新增现金流入）。
        fee_key = f"order:{order.id}:cancel:{cancellation.id}:fee"
        if fee > 0:
            fee_entry, fee_created = _add_entry(
                db,
                order_id=order.id,
                entry_type=FEE,
                amount_cents=fee,
                stage="cancel_fee_from_prepay",
                idempotency_key=fee_key,
                cancellation_id=cancellation.id,
                payment_ref="PREPAY-ALLOC",
                reason=f"取消空跑费扣费（{cancellation.final_result}），从预付款划扣",
            )
            result["lines"].append(
                {"kind": "fee", "created": fee_created, "amount_cents": fee}
            )

        # 2) 退款：预付 - 空跑费，经网关实际退回（会经历成功/失败/重试，幂等）。
        refund_cents = max(0, order.prepaid_cents - fee)
        refund_key = f"order:{order.id}:cancel:{cancellation.id}:refund"
        if refund_cents > 0:
            gw = payment.refund(db, refund_key, refund_cents)
            if not gw.ok:
                return {
                    "ok": False, "error": gw.error, "stage": "refund",
                    "refund_cents": refund_cents, "retryable": True,
                }
            r_entry, r_created = _add_entry(
                db,
                order_id=order.id,
                entry_type=REFUND,
                amount_cents=refund_cents,
                stage="cancel_refund",
                idempotency_key=refund_key,
                cancellation_id=cancellation.id,
                payment_ref=gw.payment_ref,
                reason="取消后退还预付余额",
            )
            result["lines"].append(
                {"kind": "refund", "created": r_created, "amount_cents": refund_cents,
                 "payment_ref": gw.payment_ref}
            )
        result["refund_cents"] = refund_cents
    else:
        # 未付款：空跑费作为应收向客户收取（CHARGE，可失败重试，幂等）。
        charge_key = f"order:{order.id}:cancel:{cancellation.id}:fee"
        if fee > 0:
            gw = payment.charge(db, charge_key, fee)
            if not gw.ok:
                return {"ok": False, "error": gw.error, "stage": "charge",
                        "fee_cents": fee, "retryable": True}
            c_entry, c_created = _add_entry(
                db,
                order_id=order.id,
                entry_type=CHARGE,
                amount_cents=fee,
                stage="cancel_settlement",
                idempotency_key=charge_key,
                cancellation_id=cancellation.id,
                payment_ref=gw.payment_ref,
                reason=f"取消空跑费收款（{cancellation.final_result}）",
            )
            result["lines"].append(
                {"kind": "charge", "created": c_created, "amount_cents": fee,
                 "payment_ref": gw.payment_ref}
            )
        result["refund_cents"] = 0

    result["ok"] = True
    return result


def apply_adjudication_delta(db, order, cancellation, adjudication) -> dict:
    """改判找平：新费 vs 原费差额，只新增 REFUND/SURCHARGE，不改原账。幂等。"""
    delta = adjudication.to_amount_cents - adjudication.from_amount_cents
    result = {"delta_cents": delta, "lines": []}
    if delta == 0:
        result["ok"] = True
        return result

    key = f"order:{order.id}:adjudication:{adjudication.id}:delta"
    if delta < 0:
        amount = -delta
        # 费下调：此前费已收（预付款划扣或取消时收款），差额经网关退回。
        gw = payment.refund(db, key, amount)
        if not gw.ok:
            return {"ok": False, "error": gw.error, "stage": "adjudication_refund",
                    "amount_cents": amount, "retryable": True}
        _add_entry(
            db,
            order_id=order.id,
            entry_type=REFUND,
            amount_cents=amount,
            stage="adjudication_adjust",
            idempotency_key=key,
            cancellation_id=cancellation.id,
            adjudication_id=adjudication.id,
            payment_ref=gw.payment_ref,
            reason=f"客服改判退还差额：{adjudication.from_result}→{adjudication.to_result}；{adjudication.reason}",
            operator=adjudication.operator,
        )
        result["lines"].append({"kind": "refund", "amount_cents": amount, "payment_ref": gw.payment_ref})
    else:
        amount = delta
        gw = payment.charge(db, key, amount)
        if not gw.ok:
            return {"ok": False, "error": gw.error, "stage": "adjudication_surcharge",
                    "amount_cents": amount, "retryable": True}
        _add_entry(
            db,
            order_id=order.id,
            entry_type=SURCHARGE,
            amount_cents=amount,
            stage="adjudication_adjust",
            idempotency_key=key,
            cancellation_id=cancellation.id,
            adjudication_id=adjudication.id,
            payment_ref=gw.payment_ref,
            reason=f"客服改判补收差额：{adjudication.from_result}→{adjudication.to_result}；{adjudication.reason}",
            operator=adjudication.operator,
        )
        result["lines"].append({"kind": "surcharge", "amount_cents": amount, "payment_ref": gw.payment_ref})

    result["ok"] = True
    return result
