"""重点核查 3：退款重试与金额对账 —— 重复取消/重复退款只处理一次，金额恒等于原订单口径。"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app import services
from app.database import SessionLocal
from app.models import (
    Cancellation, IdempotencyRecord, LedgerEntry, Order, PaymentTxn, PricingRule,
)

T = 3_000_000_000_000
START = T + 60 * 60_000


def _make(db, code, paid=True, total=20000, start=START):
    rule = db.scalar(select(PricingRule))
    return services.create_order(
        db, customer_name=code, service_start_at_ms=start,
        total_cents=total, paid=paid, rule=rule, now_ms=T, code=code)


def _ledger_rows(db, order_id):
    return db.scalars(select(LedgerEntry).where(LedgerEntry.order_id == order_id)
                      .order_by(LedgerEntry.id)).all()


def _gw_sums(db, order_id):
    chg = db.scalar(select(func.coalesce(func.sum(PaymentTxn.amount_cents), 0)).where(
        PaymentTxn.order_id == order_id, PaymentTxn.kind.in_(["pay", "charge"])))
    ref = db.scalar(select(func.coalesce(func.sum(PaymentTxn.amount_cents), 0)).where(
        PaymentTxn.order_id == order_id, PaymentTxn.kind == "refund"))
    return chg, ref


def test_paid_departed_cancel_settlement_and_retry(db):
    order = _make(db, "RF-1", paid=True, total=20000)
    services.dispatch(db, order, now_ms=T)
    r = services.confirm_cancel(db, order, reason="临时取消", now_ms=T)
    assert r["current_fee_cents"] == 6000

    rec = services.reconciliation(db, order)
    assert rec["balanced"] is True
    assert rec["platform_retained_cents"] == 6000
    assert rec["gateway_net_cents"] == 6000
    # 扣费 6000 与退款 14000 分别入账
    kinds = {e["kind"]: e["amount_signed_cents"] for e in rec["entries"]}
    assert kinds["payment"] == 20000
    assert kinds["cancellation_fee"] == 6000
    assert kinds["refund"] == -14000

    rows_before = [(e.id, e.kind, e.amount_signed_cents, e.idempotency_key)
                   for e in _ledger_rows(db, order.id)]

    # 退款重试多次：首笔已成功 → 全部幂等返回，不新增流水、不重复退款
    for _ in range(5):
        rr = services.retry_refund(db, order, now_ms=T + 10)
        assert rr["retried"] is False
    rows_after = [(e.id, e.kind, e.amount_signed_cents, e.idempotency_key)
                  for e in _ledger_rows(db, order.id)]
    assert rows_after == rows_before

    chg, ref = _gw_sums(db, order.id)
    assert (chg, ref) == (20000, 14000)
    rec2 = services.reconciliation(db, order)
    assert rec2["balanced"] and rec2["gateway_balanced"]
    assert rec2["refunds_out_cents"] == 14000


def test_duplicate_cancel_charged_once(db):
    order = _make(db, "RF-2", paid=True)
    services.dispatch(db, order, now_ms=T)
    r1 = services.confirm_cancel(db, order, reason="第一次", now_ms=T)
    r2 = services.confirm_cancel(db, order, reason="第二次", now_ms=T + 1)
    r3 = services.confirm_cancel(db, order, reason="第三次",
                                 now_ms=T + 2, idempotency_key=f"cancel-{order.id}")
    assert r1["deduplicated"] is False
    assert r2["deduplicated"] is True
    assert r3["deduplicated"] is True
    assert r2["current_fee_cents"] == 6000
    # 取消单唯一、账本条目数固定（payment/扣费/退款各一）
    assert db.scalar(select(func.count()).select_from(Cancellation)
                     .where(Cancellation.order_id == order.id)) == 1
    rows = _ledger_rows(db, order.id)
    assert sorted(e.kind for e in rows) == ["cancellation_fee", "payment", "refund"]


def test_unpaid_cancel_then_review_down_refunds_via_gateway(db):
    """未付款：空跑费经模拟网关收取；改判核减后从网关退回，净额对得上。"""
    order = _make(db, "RF-3", paid=False)
    services.dispatch(db, order, now_ms=T)
    services.confirm_cancel(db, order, reason="x", now_ms=T)
    rec = services.reconciliation(db, order)
    assert rec["balanced"] and rec["gateway_net_cents"] == 6000

    services.agent_review(db, order, new_stage="booked",
                          reason="核实服务员未出发，免除空跑费。", agent="客服", now_ms=T + 1)
    rec2 = services.reconciliation(db, order)
    assert rec2["current_fee_cents"] == 0
    assert rec2["platform_retained_cents"] == 0
    assert rec2["gateway_net_cents"] == 0 and rec2["gateway_balanced"]
    # 原扣款 6000 仍保留，另有一笔追加退款 6000，原账未被改
    chg, ref = _gw_sums(db, order.id)
    assert (chg, ref) == (6000, 6000)

    # 重试退款：没有应退余额，不再产生任何流水
    rr = services.retry_refund(db, order, now_ms=T + 2)
    assert rr["retried"] is False
    assert services.reconciliation(db, order)["gateway_balanced"]


def test_free_cancel_full_refund_once(db):
    # 预约在 3 小时后：提前 120 分钟以上取消，免费
    order = _make(db, "RF-4", paid=True, start=T + 180 * 60_000)
    services.dispatch(db, order, now_ms=T)
    r = services.confirm_cancel(db, order, reason="提前取消", now_ms=T)
    assert r["current_fee_cents"] == 0
    rec = services.reconciliation(db, order)
    assert rec["refunds_out_cents"] == 20000
    assert rec["platform_retained_cents"] == 0
    for _ in range(3):
        assert services.retry_refund(db, order, now_ms=T + 1)["retried"] is False
    assert _gw_sums(db, order.id) == (20000, 20000)


def test_arrived_fee_sixty_percent(db):
    order = _make(db, "RF-5", paid=True, total=30000)
    services.dispatch(db, order, now_ms=T)
    services.arrive(db, order, lat=31.2308, lng=121.4739, now_ms=T + 1)
    services.confirm_cancel(db, order, reason="x", now_ms=T + 2)
    rec = services.reconciliation(db, order)
    assert rec["current_fee_cents"] == 18000  # 30000 × 60%
    assert rec["platform_retained_cents"] == 18000
    assert rec["gateway_net_cents"] == 18000


def test_ledger_locked_and_append_only(db):
    order = _make(db, "RF-6", paid=True)
    services.dispatch(db, order, now_ms=T)
    services.confirm_cancel(db, order, reason="x", now_ms=T)
    assert order.ledger_locked is True
    before = {(e.id, e.kind, e.amount_signed_cents) for e in _ledger_rows(db, order.id)}

    services.agent_review(db, order, new_stage="arrived", reason="补传轨迹确认到场",
                          agent="k", now_ms=T + 1)
    services.agent_review(db, order, new_stage="departed", reason="复核后维持已出发",
                          agent="k", now_ms=T + 2)
    after = {(e.id, e.kind, e.amount_signed_cents) for e in _ledger_rows(db, order.id)}
    # 旧条目原封不动；新增的只能是补收/追加退款
    assert before.issubset(after)
    new_kinds = [k for (_, k, _) in after - before]
    assert set(new_kinds) <= {"additional_charge", "additional_refund"}
    # 最终回到初判 6000，金额仍对得上
    rec = services.reconciliation(db, order)
    assert rec["current_fee_cents"] == 6000
    assert rec["balanced"] and rec["gateway_balanced"]


def test_gateway_idempotency_keys_unique_and_stable(db):
    order = _make(db, "RF-7", paid=True)
    services.dispatch(db, order, now_ms=T)
    services.confirm_cancel(db, order, reason="x", now_ms=T)
    services.agent_review(db, order, new_stage="arrived", reason="r", agent="k", now_ms=T + 1)
    keys = [t.idempotency_key for t in db.scalars(select(PaymentTxn)
            .where(PaymentTxn.order_id == order.id)).all()]
    assert len(keys) == len(set(keys))
    # 外部流水号由幂等键派生，重试不变
    txns1 = [(t.kind, t.amount_cents, t.external_ref) for t in
             db.scalars(select(PaymentTxn).where(PaymentTxn.order_id == order.id)).all()]
    services.retry_refund(db, order, now_ms=T + 2)
    txns2 = [(t.kind, t.amount_cents, t.external_ref) for t in
             db.scalars(select(PaymentTxn).where(PaymentTxn.order_id == order.id)).all()]
    assert txns1 == txns2
