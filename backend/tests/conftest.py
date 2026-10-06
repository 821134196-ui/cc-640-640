"""pytest 公共夹具：独立临时库、每例清表、可控服务端时钟与支付模拟。"""
import os
import pathlib
import tempfile

# 必须在导入 app.* 之前指定数据库
_TMP = tempfile.mkdtemp()
os.environ["HOUSEKEEPING_DB"] = str(pathlib.Path(_TMP) / "test.db")

import pytest  # noqa: E402

from app.database import SessionLocal, engine, init_db  # noqa: E402
from app.services import clock, payment, cancellation_service as svc  # noqa: E402
from app import models  # noqa: E402

init_db()
T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def db_env():
    # 清空所有表
    with engine.begin() as conn:
        for table in reversed(models.Base.metadata.sorted_tables):
            conn.execute(table.delete())
    clock.reset()
    payment.reset_sim()
    clock.set_fixed(T0)
    db = SessionLocal()
    try:
        yield {"db": db, "clock": clock, "payment": payment, "svc": svc, "T0": T0}
    finally:
        db.close()


def make_order(db, *, code, amount_cents=30000, paid=True, lead_hours=2, prepaid=None):
    return svc.create_order(
        db,
        {
            "code": code,
            "customer_name": "测试客户",
            "service_address": "测试地址",
            "service_lat": 31.2304,
            "service_lng": 121.4737,
            "service_amount_cents": amount_cents,
            "scheduled_start": T0 + lead_hours * 3600,
            "paid": paid,
            "prepaid_cents": prepaid if prepaid is not None else (amount_cents if paid else 0),
        },
    )
