"""重点检查 2：客服改判后，原金额、调整结果、理由完整保留，双方可见；锁账后只能追加。"""
import pytest

from app.services.fee_engine import DISPATCHED, ARRIVED, NOT_DISPATCHED
from conftest import make_order


def _cancel_dispatched(db, svc, code="X"):
    o = make_order(db, code=code)
    svc.mark_dispatched(db, o)
    svc.confirm_cancel(db, o, idempotency_key=code)
    return o


def test_adjudication_preserves_original_and_records_reason(db_env):
    db = db_env["db"]; svc = db_env["svc"]
    o = _cancel_dispatched(db, svc, "ADJ1")
    d0 = svc.serialize_order(db, o)
    assert d0["cancellation"]["original"]["amount_cents"] == 9000

    svc.adjudicate(db, o, operator="客服02", reason="定位显示已在门口，认定到场",
                   target_state=ARRIVED)
    d = svc.serialize_order(db, o)

    # 原金额、原结果、原依据永不改变
    assert d["cancellation"]["original"]["amount_cents"] == 9000
    assert d["cancellation"]["original"]["result"] == "DISPATCH_FEE"
    assert d["cancellation"]["original"]["basis"]
    # 最终结果更新为 80%
    adj = d["cancellation"]["adjudications"]
    assert len(adj) == 1
    assert adj[0]["from_amount_cents"] == 9000
    assert adj[0]["to_amount_cents"] == 24000
    assert adj[0]["operator"] == "客服02"
    assert adj[0]["reason"] == "定位显示已在门口，认定到场"
    assert d["cancellation"]["final"]["amount_cents"] == 24000
    assert d["cancellation"]["adjudicated"] is True


def test_adjudication_requires_reason(db_env):
    db = db_env["db"]; svc = db_env["svc"]
    o = _cancel_dispatched(db, svc, "ADJ2")
    with pytest.raises(svc.Conflict):
        svc.adjudicate(db, o, operator="k", reason="   ", target_state=ARRIVED)


def test_multiple_adjudications_chain_and_all_visible(db_env):
    db = db_env["db"]; svc = db_env["svc"]
    o = _cancel_dispatched(db, svc, "ADJ3")
    svc.adjudicate(db, o, operator="k1", reason="改判到场", target_state=ARRIVED)   # 9000 ->24000
    svc.adjudicate(db, o, operator="k2", reason="复核未到场", arrival_confirmed=False)  # -> 9000 (30%)
    svc.adjudicate(db, o, operator="k3", reason="客户关怀全免", waive=True)          # -> 0

    d = svc.serialize_order(db, o)
    assert d["cancellation"]["original"]["amount_cents"] == 9000
    assert [a["seq"] for a in d["cancellation"]["adjudications"]] == [1, 2, 3]
    assert [a["to_amount_cents"] for a in d["cancellation"]["adjudications"]] == [24000, 9000, 0]
    assert d["cancellation"]["final"]["amount_cents"] == 0
    # 每次改判理由都在（客户可见）
    assert all(a["reason"] for a in d["cancellation"]["adjudications"])


def test_lock_blocks_adjudication_and_original_entries_immutable(db_env):
    db = db_env["db"]; svc = db_env["svc"]
    o = _cancel_dispatched(db, svc, "LOCK1")
    svc.adjudicate(db, o, operator="k", reason="改到场", target_state=ARRIVED)
    before = [(e["stage"], e["entry_type"], e["amount_cents"]) for e in svc.serialize_order(db, o)["ledger"]["entries"]]
    svc.lock_account(db, o)

    # 锁账后不能再改判
    with pytest.raises(svc.Conflict):
        svc.adjudicate(db, o, operator="k", reason="锁后还想改", waive=True)

    # 锁账后只能追加补收/退款单
    svc.post_lock_adjustment(db, o, amount_cents=-2000, reason="锁账后退 20 元",
                             operator="k", idempotency_key="m1")
    d = svc.serialize_order(db, o)
    after = [(e["stage"], e["entry_type"], e["amount_cents"]) for e in d["ledger"]["entries"][: len(before)]]
    assert after == before, "原有流水不得被修改"
    assert d["cancellation"]["locked"] is True
    assert d["ledger"]["totals"]["net_paid_cents"] == 22000  # 24000 - 2000


def test_cannot_lock_with_unsettled_amount(db_env):
    db = db_env["db"]; payment = db_env["payment"]; svc = db_env["svc"]
    o = _cancel_dispatched(db, svc, "LOCK2")
    payment.inject_failure("adjudication", 1)
    with pytest.raises(svc.SettlementPending):
        svc.adjudicate(db, o, operator="k", reason="改到场但补收失败", target_state=ARRIVED)
    with pytest.raises(svc.Conflict):
        svc.lock_account(db, o)


def test_dispute_adjudication_sets_final_and_keeps_provisional_original(db_env):
    db = db_env["db"]; clock = db_env["clock"]; svc = db_env["svc"]
    o = make_order(db, code="DISP")
    svc.mark_dispatched(db, o)
    r = svc.attendant_checkin(db, o, lat=31.2404, lng=121.4737)  # 围栏外
    assert r["arrival_disputed"] is True
    cr = svc.confirm_cancel(db, o, idempotency_key="disp")
    assert cr["disputed"] is True
    assert cr["cancellation"]["settle_status"] == "pending"
    orig = cr["cancellation"]["original"]["amount_cents"]

    # 客服核对后判定“未到场”，按 30%
    svc.adjudicate(db, o, operator="核查员", reason="签到定位在围栏外 1km，不认定到场",
                   arrival_confirmed=False)
    d = svc.serialize_order(db, o)
    assert d["cancellation"]["original"]["amount_cents"] == orig
    assert d["cancellation"]["final"]["amount_cents"] == 9000
    assert d["cancellation"]["adjudications"][0]["reason"].startswith("签到定位")
    assert d["status"] == "cancelled" and d["arrival_disputed"] is False
