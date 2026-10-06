"""数据库引擎与会话。

所有事务均以 ``BEGIN IMMEDIATE`` 开启：SQLite 下写操作互相串行，
配合事件序号(seq)，保证“出发/取消几乎同时”时服务端有唯一的事件顺序，
不会因为两个请求各自读取客户端时间而算出不同费用。
"""
import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("APP_DB", str(ROOT / "data" / "app.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"timeout": 10},
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=8000")
    cur.close()
    # 关掉 pysqlite 隐式 BEGIN，改由下面的 begin 事件显式开启 IMMEDIATE 事务。
    dbapi_connection.isolation_level = None


@event.listens_for(engine, "begin")
def _begin_immediate(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")


class Base(DeclarativeBase):
    pass


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from app import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(engine)
