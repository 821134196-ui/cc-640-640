"""取消与空跑费核心编排。

职责：
- 服务端事件流落库（seq 单调，事务已 BEGIN IMMEDIATE 串行化）。
- 取消预览：只读，返回金额与完整计算依据，不落任何数据。
- 确认取消：幂等；按落库瞬间的事件状态计费并结算（扣费/退款分别入账）。
- 到场争议：收集签到与定位证据，交客服核对改判，全程留痕。
- 锁账后：不改原账，只允许追加补收/退款单。
"""
from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .. import models
from . import fee_engine, geo, ledger, payment, pricing
from .clock import now

CUSTOMER = "customer"
ATTENDANT = "attendant"
CS = "cs"
SYSTEM = "system"


# ---------------- 事件 ----------------

def _next_seq(db, order_id: str) -> int:
    m = db.scalar(
        select(func.max(models.OrderEvent.seq)).where(models.OrderEvent.order_id == order_id)
    )
    return (m or 0) + 1


def _settle_atomic(db, fn):
    """在保存点内执行结算，保证账本原子性。

    网关失败时回滚本段所有账本写入（例如退款失败时不留半条扣费行），
    外层仍可保留取消单/改判记录（标记 failed），稍后幂等重试。
    """
    db.flush()  # 先把外层的状态/改判记录落出，随后账本写入才只在保存点内
    sp = db.begin_nested()
    try:
        res = fn()
    except Exception:
        sp.rollback()
        raise
    if res.get("ok"):
        sp.commit()
    else:
        sp.rollback()
    return res


def record_event(db, order, event_type, actor, *, client_time=None, payload=None, note=None):
    """在服务端串行事务内追加事件。seq 是先后判定的唯一依据。"""
    ev = models.OrderEvent(
        order_id=order.id,
        seq=_next_seq(db, order.id),
        event_type=event_type,
        actor=actor,
        server_time=now(),
        client_time=client_time,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        note=note,
    )
    db.add(ev)
    db.flush()
    return ev


# ---------------- 订单 ----------------

def create_order(db, data: dict) -> models.Order:
    snap = pricing.make_snapshot(db)
    ts = now()
    order = models.Order(
        code=data["code"],
        customer_name=data["customer_name"],
        service_type=data.get("service_type", "日常保洁"),
        service_address=data["service_address"],
        service_lat=float(data["service_lat"]),
        service_lng=float(data["service_lng"]),
        service_amount_cents=int(data["service_amount_cents"]),
        scheduled_start=float(data["scheduled_start"]),
        status="created",
        rule_snapshot_id=snap.id,
        prepaid_cents=int(data.get("prepaid_cents", 0)),
        paid=bool(data.get("paid", False)),
        created_server_time=ts,
        created_client_time=data.get("client_time"),
    )
    db.add(order)
    db.flush()
    record_event(db, order, "order_created", SYSTEM, payload={"rule_snapshot_id": snap.id})
    if order.paid:
        ledger.settle_prepayment(db, order)
    db.commit()
    db.refresh(order)
    return order


def get_order(db, order_id: str):
    return db.scalar(
        select(models.Order)
        .where(models.Order.id == order_id)
        .options(
            selectinload(models.Order.events),
            selectinload(models.Order.rule_snapshot),
        )
    )


def get_order_by_code(db, code: str):
    return db.scalar(select(models.Order).where(models.Order.code == code))


def list_orders(db):
    return list(db.scalars(select(models.Order).order_by(models.Order.created_server_time.desc())))


# ---------------- 状态与费用 ----------------

def _rules(order):
    return fee_engine.rules_from_snapshot(order.rule_snapshot)


def current_state(order) -> str:
    return fee_engine.state_from_events(
        order.events, arrival_disputed=order.arrival_disputed
    )


def _fee(order, state, cancel_time=None):
    return fee_engine.calculate(
        _rules(order),
        state,
        order.service_amount_cents,
        scheduled_start=order.scheduled_start,
        cancel_server_time=cancel_time or now(),
        rule_version=order.rule_snapshot.version,
    )


def preview_cancel(db, order) -> dict:
    """只读预览：不落事件、不建取消单、不动账。"""
    existing = _get_cancellation(db, order.id)
    if existing is not None:
        return {
            "already_cancelled": True,
            "state": existing.state_at_cancel,
            "fee": _cancellation_fee_payload(existing),
            "message": "该订单已取消，以下为已生效的费用结果（重复取消不会再次收费）。",
        }

    state = current_state(order)
    fee = _fee(order, state)
    return {"already_cancelled": False, "state": state, "fee": fee.to_dict()}


# ---------------- 取消 ----------------

def _get_cancellation(db, order_id):
    return db.scalar(
        select(models.Cancellation)
        .where(models.Cancellation.order_id == order_id)
        .options(selectinload(models.Cancellation.adjudications))
    )


def _cancellation_fee_payload(cancellation: models.Cancellation) -> dict:
    return {
        "result": cancellation.final_result,
        "amount_cents": cancellation.final_amount_cents,
        "amount_yuan": f"{cancellation.final_amount_cents / 100:.2f}",
        "state": cancellation.state_at_cancel,
        "state_label": fee_engine.STATE_LABELS.get(
            cancellation.state_at_cancel, cancellation.state_at_cancel
        ),
        "provisional": cancellation.status == "pending_review",
        "basis": json.loads(cancellation.original_basis_json),
        "original": {
            "result": cancellation.original_result,
            "amount_cents": cancellation.original_amount_cents,
            "amount_yuan": f"{cancellation.original_amount_cents / 100:.2f}",
        },
        "adjudicated": cancellation.adjudicated,
    }


def confirm_cancel(db, order, *, idempotency_key, reason=None, client_time=None) -> dict:
    """确认取消。幂等：同订单重复提交只返回原结果，不再收费。"""
    existing = _get_cancellation(db, order.id)
    if existing is not None:
        return {
            "created": False,
            "already": True,
            "cancellation": _serialize_cancellation(existing),
            "message": "取消已处理过，本次为幂等返回，未重复收费或退款。",
        }

    if order.status in ("cancelled", "completed"):
        raise Conflict(f"订单当前状态 {order.status}，不可取消")

    # 1) 先判定落库瞬间的状态，再追加取消事件——状态只由此前的服务端事件决定。
    state = current_state(order)
    server_ts = now()
    fee = _fee(order, state, cancel_time=server_ts)

    cancel_event = record_event(
        db, order, "customer_cancelled", CUSTOMER,
        client_time=client_time,
        payload={"state_at_cancel": state, "estimated_fee_cents": fee.amount_cents},
        note=reason,
    )

    disputed = state == fee_engine.DISPUTED
    cancellation = models.Cancellation(
        order_id=order.id,
        idempotency_key=idempotency_key,
        status="pending_review" if disputed else "confirmed",
        state_at_cancel=state,
        original_result=fee.result,
        original_amount_cents=fee.amount_cents,
        original_basis_json=json.dumps(fee.to_dict()["basis"], ensure_ascii=False),
        final_result=fee.result,
        final_amount_cents=fee.amount_cents,
        requested_server_time=cancel_event.server_time,
        client_time=client_time,
        reason=reason,
        settle_status="pending" if disputed else "pending",
    )
    db.add(cancellation)
    order.status = "cancelled"
    db.flush()

    settle_info = None
    if not disputed:
        settle_info = _settle_atomic(
            db, lambda: ledger.settle_cancellation(db, order, cancellation)
        )
        cancellation.settle_status = "settled" if settle_info.get("ok") else "failed"
        cancellation.settle_error = settle_info.get("error")
    else:
        cancellation.settle_status = "pending"
        cancellation.settle_error = "到场争议待客服判定，费用暂不结算"

    db.commit()
    db.refresh(cancellation)
    return {
        "created": True,
        "already": False,
        "disputed": disputed,
        "settle": settle_info,
        "cancellation": _serialize_cancellation(cancellation),
    }


# ---------------- 到场证据 / 争议 ----------------

def mark_dispatched(db, order, *, client_time=None, note=None):
    if order.status in ("cancelled", "completed"):
        raise Conflict("订单已结束，不能标记出发")
    if current_state(order) != fee_engine.NOT_DISPATCHED:
        raise Conflict("服务员已出发，不能重复出发")
    record_event(db, order, "attendant_dispatched", ATTENDANT,
                 client_time=client_time, note=note)
    order.status = "dispatched"
    db.commit()
    return {"state": fee_engine.DISPATCHED}


def attendant_checkin(db, order, *, lat, lng, accuracy_m=None, client_time=None):
    if order.status in ("cancelled", "completed"):
        raise Conflict("订单已结束，不能签到")
    ev = geo.evaluate(lat, lng, order.service_lat, order.service_lng)
    server_ts = now()
    within = bool(ev["within_geofence"])

    checkin_event = record_event(
        db, order, "attendant_checkin", ATTENDANT,
        client_time=client_time,
        payload={"lat": lat, "lng": lng, **ev},
    )
    evidence = models.ArrivalEvidence(
        order_id=order.id,
        event_id=checkin_event.id,
        kind="attendant_checkin",
        server_time=server_ts,
        lat=lat, lng=lng, accuracy_m=accuracy_m,
        distance_m=ev["distance_m"], within_geofence=within,
        claims_arrived=True,
        note=f"服务员签到，距服务点 {ev['distance_m']} 米，围栏内：{'是' if within else '否'}",
    )
    db.add(evidence)

    if within:
        record_event(db, order, "attendant_arrived", ATTENDANT,
                     client_time=client_time, payload={"lat": lat, "lng": lng})
        order.status = "arrived"
    else:
        # 定位在围栏外，到场存疑，挂起争议交客服核对
        order.arrival_disputed = True
        record_event(db, order, "arrival_disputed", SYSTEM,
                     payload={"reason": "checkin_outside_geofence", **ev})
    db.commit()
    return {"within_geofence": within, **ev, "arrival_disputed": order.arrival_disputed}


def raise_dispute(db, order, *, claims_arrived, note, client_time=None):
    order.arrival_disputed = True
    record_event(
        db, order, "arrival_disputed", CUSTOMER,
        client_time=client_time,
        payload={"claims_arrived": bool(claims_arrived)}, note=note,
    )
    db.add(models.ArrivalEvidence(
        order_id=order.id, kind="customer_claim", server_time=now(),
        claims_arrived=bool(claims_arrived), note=note,
    ))
    db.commit()
    return {"arrival_disputed": True}


def list_evidence(db, order_id):
    rows = db.scalars(
        select(models.ArrivalEvidence)
        .where(models.ArrivalEvidence.order_id == order_id)
        .order_by(models.ArrivalEvidence.server_time)
    )
    return [_serialize_evidence(e) for e in rows]


# ---------------- 客服改判 ----------------

VALID_STATES = {
    fee_engine.NOT_DISPATCHED,
    fee_engine.DISPATCHED,
    fee_engine.ARRIVED,
}


def adjudicate(db, order, *, operator, reason, arrival_confirmed=None,
               target_state=None, waive=None, amount_cents=None):
    cancellation = _get_cancellation(db, order.id)
    if cancellation is None:
        raise NotFound("订单尚未取消，无需改判")
    if cancellation.locked:
        raise Conflict("账单已锁定，不能改判费用结果；如需调整，请追加补收或退款单。")
    if not reason or not reason.strip():
        raise Conflict("改判必须填写理由（双方可见，留痕备查）")

    rules = _rules(order)

    # 确定目标结果与金额
    if waive:
        to_result, to_amount = "WAIVED", 0
    elif target_state in VALID_STATES:
        fr = fee_engine.calculate(
            rules, target_state, order.service_amount_cents,
            scheduled_start=order.scheduled_start,
            cancel_server_time=cancellation.requested_server_time,
            rule_version=order.rule_snapshot.version,
        )
        to_result, to_amount = fr.result, fr.amount_cents
    elif arrival_confirmed is not None:
        state = fee_engine.ARRIVED if arrival_confirmed else fee_engine.DISPATCHED
        fr = fee_engine.calculate(
            rules, state, order.service_amount_cents,
            scheduled_start=order.scheduled_start,
            cancel_server_time=cancellation.requested_server_time,
            rule_version=order.rule_snapshot.version,
        )
        to_result, to_amount = fr.result, fr.amount_cents
    elif amount_cents is not None:
        to_result, to_amount = "MANUAL", int(amount_cents)
    else:
        raise Conflict("需给出改判结论：到场判定 / 目标状态 / 免除 / 手动金额之一")

    to_amount = max(0, min(to_amount, order.service_amount_cents))

    adj = models.Adjudication(
        cancellation_id=cancellation.id,
        order_id=order.id,
        seq=len(cancellation.adjudications) + 1,
        operator=operator,
        from_result=cancellation.final_result,
        from_amount_cents=cancellation.final_amount_cents,
        to_result=to_result,
        to_amount_cents=to_amount,
        arrival_confirmed=arrival_confirmed,
        reason=reason.strip(),
        created_at=now(),
        settle_status="pending",
    )
    db.add(adj)
    db.flush()

    already_settled = cancellation.settle_status == "settled"
    if already_settled:
        # 账已结清：只新增差额调整单，绝不动原账。
        adj.settle_kind = "delta"
        res = _settle_atomic(
            db, lambda: ledger.apply_adjudication_delta(db, order, cancellation, adj)
        )
        if not res.get("ok"):
            adj.settle_status = "failed"
            adj.settle_error = res.get("error")
            record_event(db, order, "order_adjudicated", CS,
                         payload={"to": to_result, "settle": "failed"}, note=reason.strip())
            db.commit()
            raise SettlementPending(res.get("error"), adj.id)
    else:
        # 争议单首次结算：并入取消主结算，按最终额一次性扣费/退款。
        adj.settle_kind = "primary"
        cancellation.final_result = to_result
        cancellation.final_amount_cents = to_amount
        res = _settle_atomic(
            db, lambda: ledger.settle_cancellation(db, order, cancellation)
        )
        if not res.get("ok"):
            adj.settle_status = "failed"
            adj.settle_error = res.get("error")
            cancellation.settle_status = "failed"
            cancellation.settle_error = res.get("error")
            db.commit()
            raise SettlementPending(res.get("error"), adj.id)
        cancellation.settle_status = "settled"
        cancellation.settle_error = None

    adj.settle_status = "settled"
    adj.settle_error = None
    cancellation.final_result = to_result
    cancellation.final_amount_cents = to_amount
    cancellation.adjudicated = True
    cancellation.status = "confirmed"
    order.arrival_disputed = False
    record_event(
        db, order, "order_adjudicated", CS,
        payload={"from": adj.from_result, "to": to_result,
                 "from_cents": adj.from_amount_cents, "to_cents": to_amount,
                 "settle_kind": adj.settle_kind},
        note=f"{operator}：{reason.strip()}",
    )
    db.commit()
    db.refresh(adj)
    return adj


# ---------------- 锁账与锁后追加单 ----------------

def lock_account(db, order):
    cancellation = _get_cancellation(db, order.id)
    if cancellation is None:
        raise Conflict("订单尚未取消，无账可锁")
    if not _all_settled(db, order.id):
        raise Conflict("仍有待结算或失败的款项，请先重试结算成功后再锁账")
    cancellation.locked = True
    cancellation.locked_at = now()
    record_event(db, order, "account_locked", CS, note="锁账：原账冻结，仅可追加补收/退款单")
    db.commit()
    return locked_payload(cancellation)


def _all_settled(db, order_id) -> bool:
    c = _get_cancellation(db, order_id)
    if c is None or c.settle_status != "settled":
        return False
    failed = db.scalar(
        select(func.count(models.Adjudication.id)).where(
            models.Adjudication.order_id == order_id,
            models.Adjudication.settle_status != "settled",
        )
    )
    return failed == 0


def post_lock_adjustment(db, order, *, amount_cents, reason, operator, idempotency_key):
    """锁账后唯一允许的变动：追加补收（+）或退款（-）单，不改任何原有流水。"""
    if not reason or not reason.strip():
        raise Conflict("追加单必须填写理由")
    amount_cents = int(amount_cents)
    if amount_cents == 0:
        raise Conflict("金额不能为 0")

    cancellation = _get_cancellation(db, order.id)
    key = f"order:{order.id}:manual_adjust:{idempotency_key}"
    if amount_cents > 0:
        gw = payment.charge(db, key, amount_cents)
        if not gw.ok:
            raise SettlementPending(gw.error, None)
        entry, _ = ledger._add_entry(
            db, order_id=order.id, entry_type=ledger.SURCHARGE, amount_cents=amount_cents,
            stage="manual_adjust", idempotency_key=key, payment_ref=gw.payment_ref,
            reason=reason.strip(), operator=operator,
        )
    else:
        amount = -amount_cents
        gw = payment.refund(db, key, amount)
        if not gw.ok:
            raise SettlementPending(gw.error, None)
        entry, _ = ledger._add_entry(
            db, order_id=order.id, entry_type=ledger.REFUND, amount_cents=amount,
            stage="manual_adjust", idempotency_key=key, payment_ref=gw.payment_ref,
            reason=reason.strip(), operator=operator,
        )
    record_event(db, order, "manual_adjustment_appended", CS,
                 payload={"amount_cents": amount_cents}, note=f"{operator}：{reason.strip()}")
    db.commit()
    return entry


# ---------------- 重试 ----------------

def retry_settlement(db, order) -> dict:
    """对失败/挂起的结算做幂等重试。成功过的操作返回原交易号，绝不重复收退。"""
    cancellation = _get_cancellation(db, order.id)
    if cancellation is None:
        raise NotFound("订单尚未取消")
    report = {"cancellation": None, "adjudications": []}

    # 1) 取消主结算（含争议判定后的首次结算）
    if cancellation.settle_status != "settled":
        res = _settle_atomic(
            db, lambda: ledger.settle_cancellation(db, order, cancellation)
        )
        if not res.get("ok"):
            cancellation.settle_status = "failed"
            cancellation.settle_error = res.get("error")
            db.commit()
            raise SettlementPending(res.get("error"), None)
        cancellation.settle_status = "settled"
        cancellation.settle_error = None
        report["cancellation"] = "settled"

    # 2) 失败的改判（按 seq 依次找平）。
    #    primary（争议单首次结算并入主结算）：先把最终额对齐到该改判，由主结算幂等完成；
    #    delta（在已结账上改判）：只重试差额调整单。
    for adj in cancellation.adjudications:
        if adj.settle_status == "settled":
            continue
        if adj.settle_kind == "primary":
            cancellation.final_result = adj.to_result
            cancellation.final_amount_cents = adj.to_amount_cents
            res = _settle_atomic(
                db, lambda: ledger.settle_cancellation(db, order, cancellation)
            )
            if not res.get("ok"):
                cancellation.settle_status = "failed"
                cancellation.settle_error = res.get("error")
                db.commit()
                raise SettlementPending(res.get("error"), adj.id)
            cancellation.settle_status = "settled"
            cancellation.settle_error = None
        else:
            res = _settle_atomic(
                db, lambda: ledger.apply_adjudication_delta(db, order, cancellation, adj)
            )
            if not res.get("ok"):
                db.commit()
                raise SettlementPending(res.get("error"), adj.id)
            # 找平成功后再把最终额对齐到该改判
            cancellation.final_result = adj.to_result
            cancellation.final_amount_cents = adj.to_amount_cents
        adj.settle_status = "settled"
        adj.settle_error = None
        report["adjudications"].append(adj.seq)

    db.commit()
    return report


# ---------------- 序列化 ----------------

def _serialize_event(e: models.OrderEvent) -> dict:
    return {
        "seq": e.seq,
        "type": e.event_type,
        "actor": e.actor,
        "server_time": models.iso(e.server_time),
        "server_ts": e.server_time,
        "client_time": models.iso(e.client_time),
        "client_ts": e.client_time,
        "payload": json.loads(e.payload_json) if e.payload_json else None,
        "note": e.note,
    }


def _serialize_evidence(e: models.ArrivalEvidence) -> dict:
    return {
        "kind": e.kind,
        "server_time": models.iso(e.server_time),
        "lat": e.lat, "lng": e.lng, "accuracy_m": e.accuracy_m,
        "distance_m": e.distance_m,
        "within_geofence": e.within_geofence,
        "claims_arrived": e.claims_arrived,
        "note": e.note,
    }


def _serialize_cancellation(c: models.Cancellation) -> dict:
    return {
        "id": c.id,
        "status": c.status,
        "state_at_cancel": c.state_at_cancel,
        "original": {
            "result": c.original_result,
            "amount_cents": c.original_amount_cents,
            "amount_yuan": f"{c.original_amount_cents / 100:.2f}",
            "basis": json.loads(c.original_basis_json),
        },
        "final": {
            "result": c.final_result,
            "amount_cents": c.final_amount_cents,
            "amount_yuan": f"{c.final_amount_cents / 100:.2f}",
        },
        "adjudicated": c.adjudicated,
        "locked": c.locked,
        "locked_at": models.iso(c.locked_at),
        "settle_status": c.settle_status,
        "settle_error": c.settle_error,
        "requested_server_time": models.iso(c.requested_server_time),
        "client_time": models.iso(c.client_time),
        "reason": c.reason,
        "adjudications": [
            {
                "seq": a.seq,
                "operator": a.operator,
                "from_result": a.from_result,
                "from_amount_cents": a.from_amount_cents,
                "from_amount_yuan": f"{a.from_amount_cents / 100:.2f}",
                "to_result": a.to_result,
                "to_amount_cents": a.to_amount_cents,
                "to_amount_yuan": f"{a.to_amount_cents / 100:.2f}",
                "arrival_confirmed": a.arrival_confirmed,
                "reason": a.reason,
                "settle_status": a.settle_status,
                "settle_error": a.settle_error,
                "created_at": models.iso(a.created_at),
            }
            for a in c.adjudications
        ],
    }


def _serialize_entry(e: models.LedgerEntry) -> dict:
    return {
        "id": e.id,
        "entry_type": e.entry_type,
        "stage": e.stage,
        "amount_cents": e.amount_cents,
        "amount_yuan": f"{e.amount_cents / 100:.2f}",
        "status": e.status,
        "payment_ref": e.payment_ref,
        "reason": e.reason,
        "operator": e.operator,
        "created_at": models.iso(e.created_at),
    }


def serialize_order(db, order, *, evidence=None) -> dict:
    cancellation = _get_cancellation(db, order.id)
    entries = ledger.list_entries(db, order.id)
    totals = ledger.totals(db, order.id)

    # 对账：现金净支出应等于最终费用 + 锁后人工调整
    manual_s = sum(e.amount_cents for e in entries
                   if e.stage == "manual_adjust" and e.entry_type == ledger.SURCHARGE)
    manual_r = sum(e.amount_cents for e in entries
                   if e.stage == "manual_adjust" and e.entry_type == ledger.REFUND)
    final_fee = cancellation.final_amount_cents if cancellation else 0
    expected_net = final_fee + manual_s - manual_r
    balanced = totals["net_paid_cents"] == expected_net

    return {
        "id": order.id,
        "code": order.code,
        "customer_name": order.customer_name,
        "service_type": order.service_type,
        "service_address": order.service_address,
        "service_lat": order.service_lat,
        "service_lng": order.service_lng,
        "service_amount_cents": order.service_amount_cents,
        "service_amount_yuan": f"{order.service_amount_cents / 100:.2f}",
        "scheduled_start": models.iso(order.scheduled_start),
        "status": order.status,
        "arrival_disputed": order.arrival_disputed,
        "paid": order.paid,
        "prepaid_cents": order.prepaid_cents,
        "prepaid_yuan": f"{order.prepaid_cents / 100:.2f}",
        "rule_version": order.rule_snapshot.version,
        "current_state": current_state(order),
        "created_at": models.iso(order.created_server_time),
        "events": [_serialize_event(e) for e in order.events],
        "evidence": evidence if evidence is not None else list_evidence(db, order.id),
        "cancellation": _serialize_cancellation(cancellation) if cancellation else None,
        "ledger": {
            "entries": [_serialize_entry(e) for e in entries],
            "totals": {
                **totals,
                "net_paid_yuan": f"{totals['net_paid_cents'] / 100:.2f}",
                "expected_net_cents": expected_net,
                "expected_net_yuan": f"{expected_net / 100:.2f}",
                "balanced": balanced,
            },
        },
    }


def locked_payload(c: models.Cancellation) -> dict:
    return {"locked": c.locked, "locked_at": models.iso(c.locked_at)}


# ---------------- 错误 ----------------

class Conflict(Exception):
    def __init__(self, msg): self.msg = msg


class NotFound(Exception):
    def __init__(self, msg): self.msg = msg


class SettlementPending(Exception):
    def __init__(self, msg, adjudication_id):
        self.msg = msg
        self.adjudication_id = adjudication_id
