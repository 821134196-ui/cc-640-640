"""重点核查 2：客服改判 —— 原金额/新金额/理由/证据全部保留，只追加，不改原账。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import services
from app.models import Cancellation, LedgerEntry, Order, Review
from tests.conftest import get_order

T = 2_000_000_000_000
START = T + 60 * 60_000


def _cancel_after_dispatch(db, code, paid=True, total=20000):
    from app.models import PricingRule
    rule = db.scalar(select(PricingRule))
    order = services.create_order(
        db, customer_name=code, service_start_at_ms=START,
        total_cents=total, paid=paid, rule=rule, now_ms=T, code=code)
    services.dispatch(db, order, now_ms=T)
    services.confirm_cancel(db, order, reason="客户临时取消", now_ms=T)
    return order


def test_review_preserves_original_and_appends(db):
    order = _cancel_after_dispatch(db, "RV-1")
    before_entries = services.reconciliation(db, order)["entries"]
    before_ids = {e["id"]: e["amount_signed_cents"] for e in before_entries}

    review = services.agent_review(
        db, order, new_stage="arrived",
        reason="核对签到定位在围栏内 80m，确认取消前已到场。", agent="客服001", now_ms=T + 1)

    cancel = db.scalar(select(Cancellation).where(Cancellation.order_id == order.id))
    # 原金额保留不动
    assert cancel.original_stage == "departed"
    assert cancel.original_fee_cents == 6000
    # 当前金额更新为 60%
    assert cancel.current_stage == "arrived"
    assert cancel.current_fee_cents == 12000
    # 改判记录含旧值、新值、理由
    assert review.previous_fee_cents == 6000
    assert review.new_fee_cents == 12000
    assert "围栏" in review.reason
    assert review.agent == "客服001"

    # 再次改判（改回未出发）：形成第二条追加记录，第一次的记录不被覆盖
    services.agent_review(db, order, new_stage="booked",
                          reason="客户提供完整监控，服务员未出发，撤销空跑费。",
                          agent="客服002", now_ms=T + 2)
    reviews = db.scalars(select(Review).where(Review.cancellation_id == cancel.id)
                         .order_by(Review.id)).all()
    assert len(reviews) == 2
    assert [(r.previous_fee_cents, r.new_fee_cents) for r in reviews] == [
        (6000, 12000), (12000, 0)]
    db.refresh(cancel)
    assert cancel.original_fee_cents == 6000  # 初判金额始终保留
    assert cancel.current_fee_cents == 0

    # 原账条目一条未改、未删，只是追加了补收/退款单
    after = services.reconciliation(db, order)["entries"]
    for eid, amt in before_ids.items():
        assert next(e for e in after if e["id"] == eid)["amount_signed_cents"] == amt
    kinds = [e["kind"] for e in after]
    assert "additional_charge" in kinds and "additional_refund" in kinds


def test_review_requires_reason(db):
    order = _cancel_after_dispatch(db, "RV-2")
    with pytest.raises(services.BizError):
        services.agent_review(db, order, new_stage="booked", reason="   ",
                              agent="x", now_ms=T)


def test_review_before_cancel_rejected(db):
    from app.models import PricingRule
    rule = db.scalar(select(PricingRule))
    order = services.create_order(
        db, customer_name="RV-3", service_start_at_ms=START, total_cents=20000,
        paid=True, rule=rule, now_ms=T, code="RV-3")
    with pytest.raises(services.BizError):
        services.agent_review(db, order, new_stage="arrived", reason="x",
                              agent="a", now_ms=T)


def test_review_evidence_includes_arrival_checkins(db):
    """到场争议：客服改判时证据中必须包含签到和定位核对结果。"""
    from app.models import PricingRule
    rule = db.scalar(select(PricingRule))
    order = services.create_order(
        db, customer_name="争议客户", service_start_at_ms=START, total_cents=20000,
        paid=True, rule=rule, now_ms=T, code="RV-DISP")
    services.dispatch(db, order, now_ms=T)
    # 围栏外签到：不被系统确认为到场
    res = services.arrive(db, order, lat=31.2340, lng=121.4737, now_ms=T + 1)
    assert res["accepted_as_arrived"] is False
    services.confirm_cancel(db, order, reason="客户否认到场", now_ms=T + 2)

    review = services.agent_review(
        db, order, new_stage="departed",
        reason="核对签到定位在围栏 420m 外，不能认定到场，按已出发计。",
        agent="客服001", now_ms=T + 3)
    arrivals = review.evidence["arrivals_checked"]
    assert len(arrivals) == 1
    assert arrivals[0]["within_geofence"] is False
    assert arrivals[0]["distance_m"] > 300
