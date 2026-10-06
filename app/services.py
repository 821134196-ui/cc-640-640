"""业务服务层：一个业务动作 = 一个 BEGIN IMMEDIATE 事务，一次提交。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.mock_services import gateway
from app.models import (
    Arrival,
    Cancellation,
    IdempotencyRecord,
    LedgerEntry,
    Order,
    OrderEvent,
    PaymentTxn,
    PricingRule,
    Review,
)
from app.pricing import compute_fee, stage_from_events


class BizError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


# ---------- 规则快照 ----------

def snapshot_rule(rule: PricingRule) -> dict:
    import json

    snap = {
        "name": rule.name,
        "rule_id": rule.id,
        "version": f"r{rule.id}@{rule.created_at_ms}",
        "free_cancel_before_minutes": rule.free_cancel_before_minutes,
        "departed_fee_percent": rule.departed_fee_percent,
        "arrived_fee_percent": rule.arrived_fee_percent,
        "min_fee_cents": rule.min_fee_cents,
        "max_fee_cents": rule.max_fee_cents,
    }
    # 快照以 JSON 文本形式落库，事后无法被规则修改影响
    return json.loads(json.dumps(snap, ensure_ascii=False))


# ---------- 事件 ----------

def _append_event(db: Session, order: Order, type_: str, now_ms: int,
                  client_at_ms: int | None = None, payload: dict | None = None,
                  note: str = "") -> OrderEvent:
    max_seq = db.scalar(
        select(func.coalesce(func.max(OrderEvent.seq), 0)).where(OrderEvent.order_id == order.id)
    )
    ev = OrderEvent(
        order_id=order.id, seq=max_seq + 1, type=type_,
        server_at_ms=now_ms, client_at_ms=client_at_ms,
        payload=payload or {}, note=note,
    )
    db.add(ev)
    db.flush()
    return ev


def _require_active(order: Order):
    if order.status == "cancelled":
        raise BizError(409, "订单已取消，不能重复操作")


# ---------- 订单生命周期 ----------

def create_order(db: Session, *, customer_name: str, service_start_at_ms: int,
                 total_cents: int, paid: bool, rule: PricingRule,
                 now_ms: int, code: str | None = None) -> Order:
    order = Order(
        code=code or f"OD{now_ms}{db.scalar(select(func.count()).select_from(Order)) + 1:03d}",
        customer_name=customer_name,
        service_start_at_ms=service_start_at_ms,
        total_cents=total_cents,
        paid=paid,
        rule_snapshot=snapshot_rule(rule),
        status="booked",
        created_at_ms=now_ms,
    )
    db.add(order)
    db.flush()
    _append_event(db, order, "created", now_ms, note="订单创建，收费规则已快照锁定")
    if paid:
        key = f"pay-{order.id}"
        txn = gateway.pay(db, order.id, total_cents, key, now_ms)
        db.add(LedgerEntry(
            order_id=order.id, kind="payment", amount_signed_cents=total_cents,
            reason="客户下单付款（模拟支付）", ref_type="payment_txn", ref_id=txn.id,
            idempotency_key=key, created_at_ms=now_ms,
        ))
    db.commit()
    db.refresh(order)
    return order


def dispatch(db: Session, order: Order, *, now_ms: int,
             client_at_ms: int | None = None, note: str = "") -> OrderEvent:
    _require_active(order)
    if order.status in ("dispatched", "arrived"):
        raise BizError(409, "服务员已出发，请勿重复出发")
    ev = _append_event(db, order, "dispatched", now_ms, client_at_ms, note=note)
    order.status = "dispatched"
    db.commit()
    return ev


def arrive(db: Session, order: Order, *, lat: float, lng: float, now_ms: int,
           client_at_ms: int | None = None, note: str = "") -> dict:
    """服务员签到：定位围栏内才算到场；围栏外只留签到证据，不推进阶段。"""
    _require_active(order)
    if order.status == "booked":
        raise BizError(409, "服务员尚未出发，不能签到")
    from app.mock_services import check_in_location

    loc = check_in_location(lat, lng)
    attempt = _append_event(
        db, order, "arrive_attempt", now_ms, client_at_ms,
        payload=loc, note=note or "服务员签到定位",
    )
    arrival = Arrival(
        order_id=order.id, event_seq=attempt.seq, server_at_ms=now_ms,
        lat=lat, lng=lng, distance_m=loc["distance_m"],
        within_geofence=loc["within_geofence"], detail=loc,
    )
    db.add(arrival)
    accepted = False
    if loc["within_geofence"]:
        _append_event(
            db, order, "arrived", now_ms, client_at_ms,
            payload={"arrival_event_seq": attempt.seq}, note="定位在围栏内，确认到场",
        )
        order.status = "arrived"
        accepted = True
    db.commit()
    return {"accepted_as_arrived": accepted, "checkin_event_seq": attempt.seq, **loc}


# ---------- 取消：预览 → 确认 ----------

def preview_cancel(db: Session, order: Order, *, now_ms: int) -> dict:
    """不落库、不收费，只返回可能产生的空跑费与完整计算依据。"""
    if order.status == "cancelled":
        c = db.scalar(select(Cancellation).where(Cancellation.order_id == order.id))
        return {
            "already_cancelled": True,
            "basis": c.basis,
            "fee_cents": c.current_fee_cents,
            "message": "订单已取消，重复取消不会再次收费或退款。",
        }
    basis = compute_fee(
        order.rule_snapshot, stage_from_events(order.events),
        now_ms, order.service_start_at_ms, order.total_cents,
    )
    refund_cents = max(order.total_cents - basis["fee_cents"], 0) if order.paid else 0
    return {
        "already_cancelled": False,
        "order_code": order.code,
        "total_cents": order.total_cents,
        "paid": order.paid,
        "basis": basis,
        "fee_cents": basis["fee_cents"],
        "refund_cents": refund_cents,
        "customer_net_effect_cents": basis["fee_cents"] if order.paid else basis["fee_cents"],
        "settle_note": (
            f"已付款：确认后扣空跑费 {basis['fee_cents']} 分、退 {refund_cents} 分，两笔分别入账。"
            if order.paid
            else f"未付款：确认后产生空跑费 {basis['fee_cents']} 分待支付。"
        ),
    }


def _post_ledger(db: Session, order: Order, kind: str, signed: int, reason: str,
                 key: str, now_ms: int, ref_type: str = "", ref_id: int | None = None):
    db.add(LedgerEntry(
        order_id=order.id, kind=kind, amount_signed_cents=signed, reason=reason,
        ref_type=ref_type, ref_id=ref_id, idempotency_key=key, created_at_ms=now_ms,
    ))


def confirm_cancel(db: Session, order: Order, *, reason: str, now_ms: int,
                   client_at_ms: int | None = None,
                   idempotency_key: str | None = None) -> dict:
    idem_key = idempotency_key or f"cancel-{order.id}"

    # 幂等：同一键 / 已取消的重复请求，直接返回首次结果，不再收费或退款
    prior = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.key == idem_key))
    existing = db.scalar(select(Cancellation).where(Cancellation.order_id == order.id))
    if existing:
        result = _cancel_result(db, existing, deduplicated=True)
        if prior is None:
            db.add(IdempotencyRecord(key=idem_key, response=result, created_at_ms=now_ms))
            db.commit()
        return result
    if prior is not None:
        return {**prior.response, "deduplicated": True}

    # 用“服务端当前时间 + 服务端事件顺序”计算；client_at_ms 仅留档
    basis = compute_fee(
        order.rule_snapshot, stage_from_events(order.events),
        now_ms, order.service_start_at_ms, order.total_cents,
    )
    fee = basis["fee_cents"]
    ev = _append_event(db, order, "cancelled", now_ms, client_at_ms,
                       payload={"fee_cents": fee}, note=reason or "客户临时取消")

    cancel = Cancellation(
        order_id=order.id, event_seq=ev.seq, created_at_ms=now_ms,
        reason=reason or "客户临时取消",
        original_stage=basis["stage"], original_fee_cents=fee, basis=basis,
        current_stage=basis["stage"], current_fee_cents=fee,
    )
    db.add(cancel)
    db.flush()
    order.status = "cancelled"

    txns = []
    if fee > 0:
        if order.paid:
            # 已付款：扣费是从预付款中核减（单独入账，不重复向网关扣款）；
            # 真正退回的余额走支付网关。扣费、退款是两条独立流水。
            charge_key = f"cancel-fee-{order.id}-{cancel.id}"
            _post_ledger(db, order, "cancellation_fee", fee,
                         f"取消空跑费从预付款核减（{basis['stage_label']}，{basis['percent']}%）",
                         charge_key, now_ms)
            refund = order.total_cents - fee
            if refund > 0:
                rkey = f"refund-{order.id}-{cancel.id}"
                rtxn = gateway.refund(db, order.id, refund, rkey, now_ms)
                _post_ledger(db, order, "refund", -refund,
                             f"取消后退还余款（{order.total_cents}-{fee}）",
                             rkey, now_ms, "payment_txn", rtxn.id)
                txns.append(rtxn)
        else:
            # 未付款：空跑费为应收，经模拟支付收取
            charge_key = f"cancel-fee-{order.id}-{cancel.id}"
            txn = gateway.charge(db, order.id, fee, charge_key, now_ms)
            _post_ledger(db, order, "cancellation_fee", fee,
                         f"取消空跑费待支付（{basis['stage_label']}）",
                         charge_key, now_ms, "payment_txn", txn.id)
            txns.append(txn)
    elif order.paid:
        # 无空跑费：全额退款
        rkey = f"refund-{order.id}-{cancel.id}"
        rtxn = gateway.refund(db, order.id, order.total_cents, rkey, now_ms)
        _post_ledger(db, order, "refund", -order.total_cents,
                     "无空跑费，全额退款", rkey, now_ms, "payment_txn", rtxn.id)
        txns.append(rtxn)

    order.ledger_locked = True  # 结算完成即锁账
    db.flush()
    result = _cancel_result(db, cancel, deduplicated=False, txns=txns)
    db.add(IdempotencyRecord(key=idem_key, response=result, created_at_ms=now_ms))
    db.commit()
    return result


def _cancel_result(db: Session, cancel: Cancellation, *, deduplicated: bool,
                   txns: list | None = None) -> dict:
    return {
        "deduplicated": deduplicated,
        "cancellation_id": cancel.id,
        "order_id": cancel.order_id,
        "original_stage": cancel.original_stage,
        "original_fee_cents": cancel.original_fee_cents,
        "current_stage": cancel.current_stage,
        "current_fee_cents": cancel.current_fee_cents,
        "basis": cancel.basis,
        "gateway_txns": [
            {"kind": t.kind, "amount_cents": t.amount_cents, "ref": t.external_ref}
            for t in (txns or [])
        ],
    }


# ---------- 客服改判（锁账后只追加） ----------

def agent_review(db: Session, order: Order, *, new_stage: str, reason: str,
                 agent: str, now_ms: int, new_fee_cents: int | None = None) -> Review:
    if new_stage not in ("booked", "departed", "arrived"):
        raise BizError(400, "new_stage 必须是 booked/departed/arrived")
    if not reason.strip():
        raise BizError(400, "改判必须填写理由")
    cancel = db.scalar(select(Cancellation).where(Cancellation.order_id == order.id))
    if cancel is None:
        raise BizError(409, "订单尚未取消，无需改判")

    if new_fee_cents is None:
        # 仍按取消时刻的服务端时间 + 下单锁定规则重算，保证口径一致
        new_basis = compute_fee(
            order.rule_snapshot, new_stage,
            cancel.basis["cancel_server_at_ms"], order.service_start_at_ms, order.total_cents,
        )
        new_fee_cents = new_basis["fee_cents"]
    else:
        new_basis = None
    new_fee_cents = max(0, int(new_fee_cents))

    evidence_rows = db.scalars(
        select(Arrival).where(Arrival.order_id == order.id).order_by(Arrival.event_seq)
    ).all()
    evidence = {
        "arrivals_checked": [
            {
                "event_seq": a.event_seq, "server_at_ms": a.server_at_ms,
                "distance_m": a.distance_m, "within_geofence": a.within_geofence,
                "lat": a.lat, "lng": a.lng,
            } for a in evidence_rows
        ],
        "recomputed_basis": new_basis,
    }

    prev_stage, prev_fee = cancel.current_stage, cancel.current_fee_cents
    review = Review(
        cancellation_id=cancel.id, created_at_ms=now_ms, agent=agent,
        previous_stage=prev_stage, new_stage=new_stage,
        previous_fee_cents=prev_fee, new_fee_cents=new_fee_cents,
        reason=reason, evidence=evidence,
    )
    db.add(review)

    delta = new_fee_cents - prev_fee
    if delta != 0:
        if not order.ledger_locked:  # 理论上不会发生：取消结算后必锁账
            raise BizError(500, "账本未锁定，状态异常")
        db.flush()  # 取 review.id
        if delta > 0:
            key = f"review-addcharge-{order.id}-{review.id}"
            txn = gateway.charge(db, order.id, delta, key, now_ms)
            _post_ledger(db, order, "additional_charge", delta,
                         f"客服改判补收（{prev_stage}→{new_stage}：{prev_fee}→{new_fee_cents}）",
                         key, now_ms, "payment_txn", txn.id)
        else:
            # 差额退回：预付订单退预收款；未付订单的空跑费在取消时已由模拟网关收取，
            # 同样经网关退回 —— 不区分是否预付，保证网关收支净额恒等于当前有效空跑费。
            amount = -delta
            key = f"review-refund-{order.id}-{review.id}"
            txn = gateway.refund(db, order.id, amount, key, now_ms)
            _post_ledger(db, order, "additional_refund", -amount,
                         f"客服改判追加退款（{prev_stage}→{new_stage}：{prev_fee}→{new_fee_cents}）",
                         key, now_ms, "payment_txn", txn.id)

    cancel.current_stage = new_stage
    cancel.current_fee_cents = new_fee_cents
    db.commit()
    db.refresh(review)
    return review


# ---------- 退款重试（幂等） ----------

def retry_refund(db: Session, order: Order, *, now_ms: int,
                 idempotency_key: str | None = None) -> dict:
    cancel = db.scalar(select(Cancellation).where(Cancellation.order_id == order.id))
    if cancel is None:
        raise BizError(409, "订单尚未取消，没有可退的款")
    key = idempotency_key or f"refund-{order.id}-{cancel.id}"

    existing_ledger = db.scalar(
        select(LedgerEntry).where(LedgerEntry.idempotency_key == key)
    )
    existing_txn = db.scalar(select(PaymentTxn).where(PaymentTxn.idempotency_key == key))
    if existing_ledger or existing_txn:
        # 首笔退款已成功入账：重试直接返回原单，绝不再退一笔
        return {
            "retried": False, "message": "该退款已成功处理，重试不再重复退款。",
            "refund_txn": {
                "kind": existing_txn.kind, "amount_cents": existing_txn.amount_cents,
                "ref": existing_txn.external_ref,
            } if existing_txn else None,
            "ledger_entry_id": existing_ledger.id if existing_ledger else None,
        }

    # 未找到首笔退款流水（异常补偿场景）：
    # 应补退 = 网关已收 − 网关已退 − 当前有效空跑费；预付与未付订单同一口径。
    gateway_charges = db.scalar(select(func.coalesce(func.sum(PaymentTxn.amount_cents), 0)).where(
        PaymentTxn.order_id == order.id, PaymentTxn.kind.in_(["pay", "charge"])))
    gateway_refunds = db.scalar(select(func.coalesce(func.sum(PaymentTxn.amount_cents), 0)).where(
        PaymentTxn.order_id == order.id, PaymentTxn.kind == "refund"))
    should_refund = gateway_charges - gateway_refunds - cancel.current_fee_cents
    if should_refund <= 0:
        return {"retried": False, "message": "当前无应退余额。", "refund_txn": None}
    txn = gateway.refund(db, order.id, should_refund, key, now_ms)
    _post_ledger(db, order, "refund", -should_refund,
                 "退款重试：补发退款（幂等键首次出现）", key, now_ms,
                 "payment_txn", txn.id)
    db.commit()
    return {
        "retried": True, "message": "补发退款成功。",
        "refund_txn": {"kind": txn.kind, "amount_cents": txn.amount_cents, "ref": txn.external_ref},
    }


# ---------- 对账 ----------

def reconciliation(db: Session, order: Order) -> dict:
    """金额对得上吗？

    平台最终留存（客户实际承担）必须等于当前有效空跑费：
      预付充值 + 未付订单空跑费直扣 + 改判补收 − 取消退款 − 改判退款 == current_fee_cents
    其中预付订单的 cancellation_fee 是“从预付款中核减的扣费留痕”，不产生新的网关
    资金流入（ref_type 为空），而取消退款与其分别入账；未付订单的空跑费是真实网关
    扣款（关联 payment_txn）。两条口径分别展示，防止把“扣费留痕”重复算成实收。
    另外账本已锁，所有条目只追加：原 payment/扣费/退款条目永不改变。
    """
    cancel = db.scalar(select(Cancellation).where(Cancellation.order_id == order.id))
    entries = db.scalars(
        select(LedgerEntry).where(LedgerEntry.order_id == order.id).order_by(LedgerEntry.id)
    ).all()
    current_fee = cancel.current_fee_cents if cancel else 0
    original_fee = cancel.original_fee_cents if cancel else 0

    prepaid_in = sum(e.amount_signed_cents for e in entries if e.kind == "payment")
    fee_withheld = sum(e.amount_signed_cents for e in entries
                       if e.kind == "cancellation_fee" and e.ref_type != "payment_txn")
    fee_charged_direct = sum(e.amount_signed_cents for e in entries
                             if e.kind == "cancellation_fee" and e.ref_type == "payment_txn")
    additional_charges = sum(e.amount_signed_cents for e in entries
                             if e.kind == "additional_charge")
    refunds_out = sum(-e.amount_signed_cents for e in entries
                      if e.kind == "refund")
    additional_refunds = sum(-e.amount_signed_cents for e in entries
                             if e.kind == "additional_refund")

    retained = (prepaid_in + fee_charged_direct + additional_charges
                - refunds_out - additional_refunds)

    gateway_charges = db.scalar(select(func.coalesce(func.sum(PaymentTxn.amount_cents), 0)).where(
        PaymentTxn.order_id == order.id, PaymentTxn.kind.in_(["pay", "charge"])))
    gateway_refunds = db.scalar(select(func.coalesce(func.sum(PaymentTxn.amount_cents), 0)).where(
        PaymentTxn.order_id == order.id, PaymentTxn.kind == "refund"))
    gateway_net = gateway_charges - gateway_refunds

    balanced = retained == current_fee
    gateway_ok = gateway_net == current_fee and gateway_refunds <= gateway_charges
    # 预付订单：扣费留痕金额必须等于取消当时初判金额（改判差额只走追加单）
    withholding_ok = fee_withheld == (original_fee if order.paid and cancel else 0)
    return {
        "order_id": order.id,
        "paid": order.paid,
        "ledger_locked": order.ledger_locked,
        "original_fee_cents": original_fee,
        "current_fee_cents": current_fee,
        "prepaid_in_cents": prepaid_in,
        "fee_withheld_from_prepayment_cents": fee_withheld,
        "fee_charged_direct_cents": fee_charged_direct,
        "additional_charges_cents": additional_charges,
        "refunds_out_cents": refunds_out,
        "additional_refunds_cents": additional_refunds,
        "platform_retained_cents": retained,
        "balanced": balanced and withholding_ok,
        "withholding_matches_original": withholding_ok,
        "gateway_charges_cents": gateway_charges,
        "gateway_refunds_cents": gateway_refunds,
        "gateway_net_cents": gateway_net,
        "gateway_balanced": gateway_ok,
        "entries": [
            {"id": e.id, "kind": e.kind, "amount_signed_cents": e.amount_signed_cents,
             "reason": e.reason, "idempotency_key": e.idempotency_key,
             "created_at_ms": e.created_at_ms}
            for e in entries
        ],
    }
