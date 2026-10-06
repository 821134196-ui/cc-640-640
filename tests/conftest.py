import os
import tempfile

# 在导入 app 之前指定测试数据库
_tmp = tempfile.mkdtemp(prefix="cleaning-test-")
os.environ["APP_DB"] = os.path.join(_tmp, "test.db")

import pytest  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.seed import reset_and_seed  # noqa: E402


@pytest.fixture()
def db():
    reset_and_seed()
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def get_order(db, code):
    from app.models import Order
    from sqlalchemy import select
    return db.scalar(select(Order).where(Order.code == code))
