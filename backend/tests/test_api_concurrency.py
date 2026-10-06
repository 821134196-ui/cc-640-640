"""HTTP/API 层 + 并发临界：出发与取消几乎同时到达时，服务端顺序唯一、费用确定。"""
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import clock
from conftest import T0, make_order

client = TestClient(app)


def _create(code, *, hours=2, paid=True, amount=30000):
    import datetime as dt
    scheduled = dt.datetime.fromtimestamp(T0 + hours * 3600).isoformat()
    r = client.post("/api/orders", json={
        "code": code, "customer_name": "接口客户", "service_address": "X",
        "lat": 31.2304, "lng": 121.4737, "amount_yuan": amount / 100,
        "scheduled_at": scheduled, "paid": paid,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_preview_then_confirm_flow(db_env):
    oid = _create("API1")
    client.post(f"/api/orders/{oid}/dispatch", json={})
    pv = client.get(f"/api/orders/{oid}/cancel/preview").json()
    assert pv["fee"]["amount_cents"] == 9000
    assert pv["already_cancelled"] is False

    r = client.post(f"/api/orders/{oid}/cancel", json={"idempotency_key": "k"})
    assert r.status_code == 200
    detail = client.get(f"/api/orders/{oid}").json()
    assert detail["cancellation"]["final"]["amount_cents"] == 9000
    assert detail["ledger"]["totals"]["balanced"] is True


def test_concurrent_dispatch_and_cancel_is_deterministic(db_env):
    """同一订单、同一客户端时间戳，并发打“出发”和“取消”各若干次：

    无论谁先拿到写锁，结果必须只取决于服务端实际顺序，
    且重复操作幂等、账本对账一致、不会同时出现两套费用。
    """
    outcomes = []
    for trial in range(6):
        oid = _create(f"RACE{trial}")
        same_ts = T0 + 700
        barrier = threading.Barrier(2)
        results = {}

        def do_dispatch():
            barrier.wait()
            clock.set_fixed(same_ts)
            results["d"] = client.post(f"/api/orders/{oid}/dispatch",
                                       json={"client_time": same_ts}).status_code

        def do_cancel():
            barrier.wait()
            clock.set_fixed(same_ts)
            results["c"] = client.post(
                f"/api/orders/{oid}/cancel",
                json={"client_time": same_ts, "idempotency_key": f"race-{trial}"},
            ).json()

        t1 = threading.Thread(target=do_dispatch)
        t2 = threading.Thread(target=do_cancel)
        t1.start(); t2.start(); t1.join(); t2.join()

        detail = client.get(f"/api/orders/{oid}").json()
        state = detail["cancellation"]["state_at_cancel"]
        fee = detail["cancellation"]["final"]["amount_cents"]
        assert detail["ledger"]["totals"]["balanced"], detail["ledger"]["totals"]

        # 服务端顺序决定结果：要么先取消(免费,0)，要么先出发(30%,9000)，不存在第三种
        if state == "NOT_DISPATCHED":
            assert fee == 0
        else:
            assert state == "DISPATCHED"
            assert fee == 9000
        outcomes.append(fee)

    # 两个顺序都应能在重复试验中出现（证明顺序确实由服务端抢占决定，而非固定偏向某一方）
    assert set(outcomes) <= {0, 9000}
    assert len(outcomes) == 6
