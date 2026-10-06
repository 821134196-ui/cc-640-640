"""数据模型。

金额统一使用整数“分”（cents）存储，杜绝浮点误差。
所有时间戳为 Unix 秒（float），业务判定只用 server_time。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def uid() -> str:
    return uuid.uuid4().hex


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------- 规则快照 ----------

class PricingRuleSnapshot(Base):
    """下单时锁定的收费规则快照（不可变）。"""

    __tablename__ = "pricing_rule_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, default=1)
    # 规范的规则结构，是费用引擎的唯一数据源
    rules_json: Mapped[str] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    created_at: Mapped[float] = mapped_column(Float)


# ---------- 订单 ----------

class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(64))
    service_type: Mapped[str] = mapped_column(String(64))
    service_address: Mapped[str] = mapped_column(String(256))
    service_lat: Mapped[float] = mapped_column(Float)
    service_lng: Mapped[float] = mapped_column(Float)
    service_amount_cents: Mapped[int] = mapped_column(Integer)
    scheduled_start: Mapped[float] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(24), default="created", index=True)
    # created -> dispatched -> arrived -> cancelled / completed；争议为状态修饰位
    arrival_disputed: Mapped[bool] = mapped_column(Boolean, default=False)

    rule_snapshot_id: Mapped[str] = mapped_column(ForeignKey("pricing_rule_snapshots.id"))
    rule_snapshot: Mapped[PricingRuleSnapshot] = relationship()

    prepaid_cents: Mapped[int] = mapped_column(Integer, default=0)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)

    created_server_time: Mapped[float] = mapped_column(Float)
    created_client_time: Mapped[float | None] = mapped_column(Float, nullable=True)

    events: Mapped[list["OrderEvent"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderEvent.seq"
    )


class OrderEvent(Base):
    """服务端事件流。seq 在订单内单调递增，是判定“谁先发生”的唯一依据。"""

    __tablename__ = "order_events"
    __table_args__ = (
        UniqueConstraint("order_id", "seq", name="uq_event_order_seq"),
        Index("ix_event_order_seq", "order_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(32))  # customer / attendant / cs / system
    server_time: Mapped[float] = mapped_column(Float, index=True)
    client_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    order: Mapped[Order] = relationship(back_populates="events")


class ArrivalEvidence(Base):
    """到场签到与定位证据，供客服在“是否到场”争议中核对。"""

    __tablename__ = "arrival_evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("order_events.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(24))  # attendant_checkin / customer_claim / cs_note
    server_time: Mapped[float] = mapped_column(Float)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    within_geofence: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    claims_arrived: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------- 取消与改判 ----------

class Cancellation(Base):
    __tablename__ = "cancellations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    status: Mapped[str] = mapped_column(String(24), default="confirmed")
    # confirmed（直接生效）/ pending_review（到场争议，待客服判定）/ locked（已锁账）
    state_at_cancel: Mapped[str] = mapped_column(String(24))  # NOT_DISPATCHED/DISPATCHED/ARRIVED/DISPUTED

    # 系统按锁定规则算出的“原金额/原结果/依据”，永远保留
    original_result: Mapped[str] = mapped_column(String(32))
    original_amount_cents: Mapped[int] = mapped_column(Integer)
    original_basis_json: Mapped[str] = mapped_column(Text)

    # 最终结果：未改判时等于 original；改判后更新，但 original 不动
    final_result: Mapped[str] = mapped_column(String(32))
    final_amount_cents: Mapped[int] = mapped_column(Integer)
    adjudicated: Mapped[bool] = mapped_column(Boolean, default=False)

    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_at: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 结算状态：pending（争议未决/网关失败待重试）/ settled
    settle_status: Mapped[str] = mapped_column(String(16), default="pending")
    settle_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    requested_server_time: Mapped[float] = mapped_column(Float)
    client_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    adjudications: Mapped[list["Adjudication"]] = relationship(
        back_populates="cancellation", cascade="all, delete-orphan", order_by="Adjudication.created_at"
    )


class Adjudication(Base):
    """客服改判记录，只追加。每一条都保留原金额、改判结果与理由，双方可见。"""

    __tablename__ = "adjudications"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    cancellation_id: Mapped[str] = mapped_column(ForeignKey("cancellations.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)

    operator: Mapped[str] = mapped_column(String(64))
    # 改判时点上的“原金额/原结果”（可能是系统值，也可能是上一次改判值）
    from_result: Mapped[str] = mapped_column(String(32))
    from_amount_cents: Mapped[int] = mapped_column(Integer)
    to_result: Mapped[str] = mapped_column(String(32))
    to_amount_cents: Mapped[int] = mapped_column(Integer)
    arrival_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)

    # 该次改判差额是否已找平（网关失败时为 failed，可重试；改判记录本身已留存）
    settle_status: Mapped[str] = mapped_column(String(16), default="settled")
    settle_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # primary=争议单首次结算并入主结算；delta=在已结算基础上找平差额
    settle_kind: Mapped[str] = mapped_column(String(16), default="delta")

    cancellation: Mapped[Cancellation] = relationship(back_populates="adjudications")


# ---------- 账本（只追加） ----------

class LedgerEntry(Base):
    """账单流水。CHARGE 客户应付/已付，REFUND 退款，SURCHARGE 补收。

    一旦 settled 即不可变；锁账后要改金额，只能新增 REFUND/SURCHARGE 调整单。
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (Index("ix_ledger_order", "order_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    cancellation_id: Mapped[str | None] = mapped_column(
        ForeignKey("cancellations.id"), nullable=True
    )
    adjudication_id: Mapped[str | None] = mapped_column(
        ForeignKey("adjudications.id"), nullable=True
    )

    entry_type: Mapped[str] = mapped_column(String(16))  # CHARGE / REFUND / SURCHARGE
    stage: Mapped[str] = mapped_column(String(32))
    # prepay / cancel_settlement / adjudication_adjust / manual_adjust
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    status: Mapped[str] = mapped_column(String(16), default="settled")  # settled / failed
    payment_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


class PaymentAttempt(Base):
    """模拟支付/退款网关的调用记录，按幂等键去重，支持失败后重试。"""

    __tablename__ = "payment_attempts"

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # CHARGE / REFUND
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # succeeded / failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    payment_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float)
