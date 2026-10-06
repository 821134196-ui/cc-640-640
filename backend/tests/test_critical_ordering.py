"""重点检查 1：出发临界时间——相同客户端时间下，费用只由服务端事件顺序决定。"""
from app.services.fee_engine import NOT_DISPATCHED, DISPATCHED, ARRIVED

from conftest import make_order


def test_preview_is_readonly_and_shows_basis(db_env):
    db = db_env["db"]; svc = db_env["svc"]; T0 = db_env["T0"]
    o = make_order(db, code="P1")
    svc.mark_dispatched(db, o)

    p = svc.preview_cancel(db, o)
    assert p["already_cancelled"] is False
    assert p["state"] == DISPATCHED
    assert p["fee"]["amount_cents"] == 9000  # 30000 * 30%
    assert p["fee"]["basis"], "预览必须给出计算依据"
    # 预览不落任何数据
    d = svc.serialize_order(db, o)
    assert d["cancellation"] is None
    assert d["status"] != "cancelled"


def test_identical_client_time_dispatch_first_charges_dispatch_fee(db_env):
    """客户端时间完全相同；服务端顺序 = 先出发后取消 => 空跑费 30%。"""
    db = db_env["db"]; clock = db_env["clock"]; svc = db_env["svc"]; T0 = db_env["T0"]
    o = make_order(db, code="ORDER-A")
    same = T0 + 500

    clock.set_fixed(same)
    svc.mark_dispatched(db, o, client_time=same)
    clock.set_fixed(same)  # 两个事件服务端落库时刻被构造成同一瞬时
    r = svc.confirm_cancel(db, o, idempotency_key="a", client_time=same)

    c = r["cancellation"]
    assert c["state_at_cancel"] == DISPATCHED
    assert c["final"]["amount_cents"] == 9000


def test_identical_client_time_cancel_first_is_free(db_env):
    """同一客户端时间；服务端顺序 = 先取消后出发 => 免收，随后出发被拒绝。"""
    db = db_env["db"]; clock = db_env["clock"]; svc = db_env["svc"]; T0 = db_env["T0"]
    o = make_order(db, code="ORDER-B")
    same = T0 + 500

    clock.set_fixed(same)
    r = svc.confirm_cancel(db, o, idempotency_key="b", client_time=same)
    assert r["cancellation"]["state_at_cancel"] == NOT_DISPATCHED
    assert r["cancellation"]["final"]["amount_cents"] == 0

    import pytest
    with pytest.raises(svc.Conflict):
        clock.set_fixed(same)
        svc.mark_dispatched(db, o, client_time=same)


def test_client_time_cannot_override_server_ordering(db_env):
    """客户端声称的时间再乱，也不影响判定：取消事件的 client_time 早于出发，仍按服务端 seq。"""
    db = db_env["db"]; clock = db_env["clock"]; svc = db_env["svc"]; T0 = db_env["T0"]
    o = make_order(db, code="ORDER-C")

    # 服务端：先出发（客户端时间戳很“晚”）
    clock.set_fixed(T0 + 100)
    svc.mark_dispatched(db, o, client_time=T0 + 9999)
    # 后取消（客户端伪造一个更早的时间，想伪装成出发前取消）
    clock.set_fixed(T0 + 101)
    r = svc.confirm_cancel(db, o, idempotency_key="c", client_time=T0 - 9999)

    assert r["cancellation"]["state_at_cancel"] == DISPATCHED
    assert r["cancellation"]["final"]["amount_cents"] == 9000


def test_event_seq_strictly_increasing(db_env):
    db = db_env["db"]; clock = db_env["clock"]; svc = db_env["svc"]
    o = make_order(db, code="SEQ")
    clock.advance(10); svc.mark_dispatched(db, o)
    clock.advance(10); svc.confirm_cancel(db, o, idempotency_key="x")
    d = svc.serialize_order(db, o)
    seqs = [e["seq"] for e in d["events"]]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    types = [e["type"] for e in d["events"]]
    assert types.index("attendant_dispatched") < types.index("customer_cancelled")


def test_arrival_fee_tier(db_env):
    db = db_env["db"]; svc = db_env["svc"]
    o = make_order(db, code="ARR")
    svc.mark_dispatched(db, o)
    # 围栏内签到 => 到场
    res = svc.attendant_checkin(db, o, lat=31.2304, lng=121.4737)
    assert res["within_geofence"] is True
    r = svc.confirm_cancel(db, o, idempotency_key="arr")
    assert r["cancellation"]["state_at_cancel"] == ARRIVED
    assert r["cancellation"]["final"]["amount_cents"] == 24000  # 80%
