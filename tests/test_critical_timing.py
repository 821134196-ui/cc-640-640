"""重点核查 1：出发临界时间 —— 只按服务端事件顺序(seq)判定。"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from app import services
from app.database import SessionLocal
from app.models import Cancellation, Order, OrderEvent
from app.pricing import stage_from_events

from tests.conftest import get_order

T = 1_000_000_000_000  # 固定“现在”，避免时间敏感
START = T + 60 * 60_000  # 预约在 1 小时后（120 分钟免费窗口外）


def _new_paid_order(db, paid=True):
    from app.models import PricingRule
    rule = db.scalar(select(PricingRule))
    return services.create_order(
        db, customer_name="测试客户", service_start_at_ms=START,
        total_cents=20000, paid=paid, rule=rule, now_ms=T,
    )


def test_stage_uses_seq_not_timestamp(db):
    """两个事件的客户端时间与服务端时间再怎么矛盾，阶段只看 seq。"""
    order = _new_paid_order(db)
    # 出发事件：客户端声称的时间极晚
    services.dispatch(db, order, now_ms=T, client_at_ms=T + 9_999_999)
    assert stage_from_events(order.events) == "departed"


def test_dispatch_then_cancel_same_millisecond_charges_departed(db):
    """同一服务端毫秒：先落 seq 的出发生效 → 30% 空跑费。"""
    order = _new_paid_order(db)
    services.dispatch(db, order, now_ms=T, client_at_ms=T + 999_999)
    result = services.confirm_cancel(
        db, order, reason="同时", now_ms=T, client_at_ms=T - 999_999)
    assert result["current_stage"] == "departed"
    assert result["current_fee_cents"] == 6000  # 20000 × 30%
    evs = db.scalars(select(OrderEvent).where(OrderEvent.order_id == order.id)
                     .order_by(OrderEvent.seq)).all()
    assert [e.type for e in evs] == ["created", "dispatched", "cancelled"]
    # seq 单调连续
    assert [e.seq for e in evs] == [1, 2, 3]


def test_cancel_before_dispatch_is_free(db):
    """取消 seq 在出发之前：未出发，免费；之后的出发被拒。"""
    order = _new_paid_order(db)
    result = services.confirm_cancel(db, order, reason="先取消", now_ms=T)
    assert result["current_stage"] == "booked"
    assert result["current_fee_cents"] == 0
    with pytest.raises(services.BizError):
        services.dispatch(db, order, now_ms=T)


def test_preview_is_readonly(db):
    """预览不得产生状态、事件、费用变化。"""
    order = _new_paid_order(db)
    before = len(order.events)
    p1 = services.preview_cancel(db, order, now_ms=T)
    p2 = services.preview_cancel(db, order, now_ms=T)
    db.expire_all()
    order2 = get_order(db, order.code)
    assert len(order2.events) == before
    assert p1["fee_cents"] == p2["fee_cents"] == 0  # 未出发
    assert db.scalar(select(Cancellation).where(Cancellation.order_id == order.id)) is None


def test_preview_after_dispatch_shows_fee_and_refund(db):
    order = _new_paid_order(db, paid=True)
    services.dispatch(db, order, now_ms=T)
    p = services.preview_cancel(db, order, now_ms=T)
    assert p["fee_cents"] == 6000
    assert p["refund_cents"] == 14000
    assert p["basis"]["rule"]["departed_fee_percent"] == 30


def _race(db_factory, order_id, winner: str):
    """两个独立事务并发：用 gate 决定服务端到达先后，模拟临界竞争。"""
    ts = T
    out = {}
    gate = {"dispatch": threading.Event(), "cancel": threading.Event()}
    gate[winner].set()

    def dispatch_side():
        s = db_factory()
        try:
            gate["dispatch"].wait()
            o = s.get(Order, order_id)
            services.dispatch(s, o, now_ms=ts, client_at_ms=ts - 10 ** 9)
            out["dispatch"] = "ok"
            gate["cancel"].set()
        except Exception as e:  # noqa: BLE001
            out["dispatch"] = f"err:{e}"
            gate["cancel"].set()
        finally:
            s.close()

    def cancel_side():
        s = db_factory()
        try:
            gate["cancel"].wait()
            o = s.get(Order, order_id)
            r = services.confirm_cancel(s, o, reason="并发取消", now_ms=ts,
                                        client_at_ms=ts + 10 ** 9)
            out["cancel"] = r
            gate["dispatch"].set()
        except Exception as e:  # noqa: BLE001
            out["cancel"] = f"err:{e}"
            gate["dispatch"].set()
        finally:
            s.close()

    t1, t2 = threading.Thread(target=dispatch_side), threading.Thread(target=cancel_side)
    t1.start(); t2.start(); t1.join(timeout=15); t2.join(timeout=15)
    return out


def test_concurrent_dispatch_wins(db):
    order = _new_paid_order(db)
    db.commit()
    out = _race(SessionLocal, order.id, "dispatch")
    assert out["dispatch"] == "ok"
    assert out["cancel"]["current_stage"] == "departed"
    assert out["cancel"]["current_fee_cents"] == 6000


def test_concurrent_cancel_wins(db):
    order = _new_paid_order(db)
    db.commit()
    out = _race(SessionLocal, order.id, "cancel")
    assert out["cancel"]["current_stage"] == "booked"
    assert out["cancel"]["current_fee_cents"] == 0
    assert "已取消" in out["dispatch"]
    # 无论哪边赢，取消单只有一张、费用唯一
    s = SessionLocal()
    try:
        cs = s.scalars(select(Cancellation).where(Cancellation.order_id == order.id)).all()
        assert len(cs) == 1
    finally:
        s.close()


def test_rule_snapshot_not_affected_by_later_rule_change(db):
    """下单后改规则，临界取消仍按下单时快照（30%）计算。"""
    from app.models import PricingRule
    order = _new_paid_order(db)
    services.dispatch(db, order, now_ms=T)
    # 新建一条 50% 的新规则并停用旧规则
    old = db.scalar(select(PricingRule).where(PricingRule.active == True))  # noqa: E712
    old.active = False
    db.add(PricingRule(name="新规50%", active=True, free_cancel_before_minutes=120,
                       departed_fee_percent=50, arrived_fee_percent=90,
                       min_fee_cents=0, max_fee_cents=0, created_at_ms=T + 1))
    db.commit()
    p = services.preview_cancel(db, order, now_ms=T)
    assert p["basis"]["rule"]["departed_fee_percent"] == 30
    assert p["fee_cents"] == 6000


def test_min_fee_clamp(db):
    from app.models import PricingRule
    rule = db.scalar(select(PricingRule))
    # 小额订单：30% 低于最低空跑费 2000 分
    order = services.create_order(
        db, customer_name="小额", service_start_at_ms=START, total_cents=3000,
        paid=False, rule=rule, now_ms=T)
    services.dispatch(db, order, now_ms=T)
    p = services.preview_cancel(db, order, now_ms=T)
    assert p["fee_cents"] == 2000
