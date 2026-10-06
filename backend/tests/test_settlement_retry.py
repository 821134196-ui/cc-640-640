"""重点检查 3：重复取消与退款重试只处理一次，金额始终与原订单对得上；扣费/退款分别入账。"""
import pytest

from app.services.fee_engine import DISPATCHED, ARRIVED
from conftest import make_order


def _dispatched_paid(db, svc, code="R", amount=30000):
    o = make_order(db, code=code, amount_cents=amount)
    svc.mark_dispatched(db, o)
    return o


def test_paid_order_charge_and_refund_separate_entries_and_balanced(db_env):
    db = db_env["db"]; svc = db_env["svc"]
    o = _dispatched_paid(db, svc, "BAL")
    svc.confirm_cancel(db, o, idempotency_key="bal")
    d = svc.serialize_order(db, o)
    t = d["ledger"]["totals"]
    types = [e["entry_type"] for e in d["ledger"]["entries"]]
    assert "CHARGE" in types and "FEE" in types and "REFUND" in types
    assert t["charge_cents"] == 30000
    assert t["fee_alloc_cents"] == 9000
    assert t["refund_cents"] == 21000
    assert t["net_paid_cents"] == 9000
    assert t["balanced"] is True


def test_duplicate_cancel_charges_once(db_env):
    db = db_env["db"]; svc = db_env["svc"]
    o = _dispatched_paid(db, svc, "DUP")
    svc.confirm_cancel(db, o, idempotency_key="same-key")
    before = svc.serialize_order(db, o)["ledger"]["totals"]

    r2 = svc.confirm_cancel(db, o, idempotency_key="same-key")
    assert r2["already"] is True
    # 换一把幂等键也不能二次收费（同一订单唯一取消单）
    r3 = svc.confirm_cancel(db, o, idempotency_key="different-key")
    assert r3["already"] is True

    after = svc.serialize_order(db, o)["ledger"]["totals"]
    assert after["count"] == before["count"]
    assert after["net_paid_cents"] == before["net_paid_cents"] == 9000
    # 支付网关侧也只有一次成功
    from app import models
    from sqlalchemy import select
    attempts = db.scalars(select(models.PaymentAttempt)).all()
    succeeded = [a for a in attempts if a.status == "succeeded"]
    assert len(succeeded) == 2  # 一次预付收款 + 一次退款


def test_refund_failure_then_retry_amounts_tie_out(db_env):
    """退款首次失败：不产生半截流水；重试成功后净额精确等于最终费用；再次重试不重复退款。"""
    db = db_env["db"]; payment = db_env["payment"]; svc = db_env["svc"]
    o = _dispatched_paid(db, svc, "RETRY", amount=20000)

    payment.inject_failure(":cancel:", 1)
    r = svc.confirm_cancel(db, o, idempotency_key="retry")
    # 取消主结算失败不抛异常，取消已记录、状态标记 failed，供客户稍后重试
    assert r["settle"] and r["settle"]["ok"] is False

    # 失败时取消已记录，但退款未入账（只留预付收款）
    d0 = svc.serialize_order(db, o)
    assert d0["cancellation"]["settle_status"] == "failed"
    assert d0["ledger"]["totals"]["count"] == 1
    assert d0["ledger"]["totals"]["refund_cents"] == 0

    svc.retry_settlement(db, o)
    d1 = svc.serialize_order(db, o)
    t1 = d1["ledger"]["totals"]
    assert d1["cancellation"]["settle_status"] == "settled"
    assert t1["refund_cents"] == 14000     # 20000 - 6000(30%)
    assert t1["net_paid_cents"] == 6000
    assert t1["balanced"] is True
    refs = [e["payment_ref"] for e in d1["ledger"]["entries"] if e["entry_type"] == "REFUND"]
    assert len(refs) == 1

    # 再重试：幂等，不重复退款
    svc.retry_settlement(db, o)
    d2 = svc.serialize_order(db, o)
    t2 = d2["ledger"]["totals"]
    assert t2["count"] == t1["count"]
    assert t2["refund_cents"] == 14000
    assert t2["net_paid_cents"] == 6000
    assert t2["balanced"] is True


def test_adjudication_delta_refund_retried_once(db_env):
    """已结账后改判下调（退差额），网关失败→重试只退一次，对账仍平。"""
    db = db_env["db"]; payment = db_env["payment"]; svc = db_env["svc"]
    o = _dispatched_paid(db, svc, "ADJR", amount=30000)
    svc.confirm_cancel(db, o, idempotency_key="x")   # 9000

    payment.inject_failure("adjudication", 1)
    with pytest.raises(svc.SettlementPending):
        svc.adjudicate(db, o, operator="k", reason="改判免除", waive=True)  # 应退 9000

    d0 = svc.serialize_order(db, o)
    # 失败的改判记录保留，但没有退款流水
    assert d0["cancellation"]["adjudications"][0]["settle_status"] == "failed"
    assert d0["ledger"]["totals"]["refund_cents"] == 21000  # 仍只有取消退款

    svc.retry_settlement(db, o)
    d1 = svc.serialize_order(db, o)
    t = d1["ledger"]["totals"]
    assert t["refund_cents"] == 30000   # 21000 + 9000
    assert t["net_paid_cents"] == 0
    assert t["balanced"] is True

    svc.retry_settlement(db, o)
    d2 = svc.serialize_order(db, o)
    assert d2["ledger"]["totals"]["count"] == t["count"]
    assert d2["ledger"]["totals"]["net_paid_cents"] == 0


def test_unpaid_order_cancel_charges_fee_and_balances(db_env):
    db = db_env["db"]; svc = db_env["svc"]
    o = make_order(db, code="UNPAID", amount_cents=10000, paid=False)
    svc.mark_dispatched(db, o)
    svc.confirm_cancel(db, o, idempotency_key="u")
    d = svc.serialize_order(db, o)
    t = d["ledger"]["totals"]
    # 30%，最低 20 元：10000*30%=3000，取最低 2000
    assert d["cancellation"]["final"]["amount_cents"] == 3000
    assert t["charge_cents"] == 3000
    assert t["net_paid_cents"] == 3000 and t["balanced"]


def test_min_fee_floor_applied(db_env):
    db = db_env["db"]; svc = db_env["svc"]
    o = make_order(db, code="MIN", amount_cents=5000, paid=False)  # 30%=1500 < 最低 2000
    svc.mark_dispatched(db, o)
    svc.confirm_cancel(db, o, idempotency_key="m")
    d = svc.serialize_order(db, o)
    assert d["cancellation"]["final"]["amount_cents"] == 2000


def test_free_cancel_before_dispatch_has_no_entries(db_env):
    db = db_env["db"]; svc = db_env["svc"]
    o = make_order(db, code="FREE", paid=True)
    svc.confirm_cancel(db, o, idempotency_key="f")
    d = svc.serialize_order(db, o)
    # 未出发免费：只有预付收款，随后应全额退回
    t = d["ledger"]["totals"]
    assert t["charge_cents"] == 30000
    assert t["refund_cents"] == 30000
    assert t["net_paid_cents"] == 0 and t["balanced"]
    assert d["cancellation"]["final"]["amount_cents"] == 0


def test_rule_snapshot_isolated_from_later_change(db_env):
    """规则后续改版不影响已下单订单：用快照计算。"""
    from app.services import pricing
    db = db_env["db"]; svc = db_env["svc"]
    o = make_order(db, code="SNAP")
    snap_rules = __import__("json").loads(o.rule_snapshot.rules_json)
    assert snap_rules["tiers"][1]["percent_bp"] == 3000
    # 即便内存中的当前规则被改动，订单仍按快照
    pricing.DEFAULT_RULES["tiers"][1]["percent_bp"] = 5500
    try:
        svc.mark_dispatched(db, o)
        r = svc.confirm_cancel(db, o, idempotency_key="snap")
        assert r["cancellation"]["original"]["amount_cents"] == 9000  # 仍是 30%
    finally:
        pricing.DEFAULT_RULES["tiers"][1]["percent_bp"] = 3000
