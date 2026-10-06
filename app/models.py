"""数据模型。

金额一律以整数“分”存储，避免浮点误差。
事件(order_events)以服务端接收顺序编号 seq，是判定出发/到场/取消先后的唯一依据。
"""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PricingRule(Base):
    """当前可编辑的收费规则。下单时快照进订单，之后规则改动不影响历史订单。"""

    __tablename__ = "pricing_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 距预约开始 >= 该分钟数取消，免费
    free_cancel_before_minutes: Mapped[int] = mapped_column(Integer)
    # 已出发（未到场）空跑费 = 订单金额 × 该百分比
    departed_fee_percent: Mapped[int] = mapped_column(Integer)
    # 已到场空跑费 = 订单金额 × 该百分比
    arrived_fee_percent: Mapped[int] = mapped_column(Integer)
    min_fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    max_fee_cents: Mapped[int] = mapped_column(Integer, default=0)  # 0 表示不封顶
    created_at_ms: Mapped[int] = mapped_column(Integer)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(64))
    service_start_at_ms: Mapped[int] = mapped_column(Integer)
    total_cents: Mapped[int] = mapped_column(Integer)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    rule_snapshot: Mapped[dict] = mapped_column(JSON)
    # booked -> dispatched -> arrived -> cancelled（可由任意前置态取消）
    status: Mapped[str] = mapped_column(String(16), default="booked")
    ledger_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at_ms: Mapped[int] = mapped_column(Integer)

    events: Mapped[list["OrderEvent"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderEvent.seq"
    )


class OrderEvent(Base):
    """服务端事件流。seq 在同一事务内按订单递增，决定阶段判定。"""

    __tablename__ = "order_events"
    __table_args__ = (
        UniqueConstraint("order_id", "seq", name="uq_event_order_seq"),
        Index("ix_event_order_seq", "order_id", "seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    seq: Mapped[int] = mapped_column(Integer)
    # created / dispatched / arrived / cancelled
    type: Mapped[str] = mapped_column(String(24))
    # 判定唯一依据：服务端落库时间
    server_at_ms: Mapped[int] = mapped_column(Integer)
    # 仅作记录/审计，绝不参与费用判定
    client_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(255), default="")

    order: Mapped[Order] = relationship(back_populates="events")


class Arrival(Base):
    """到场签到证据（定位由模拟接口给出）。"""

    __tablename__ = "arrivals"
    __table_args__ = (UniqueConstraint("order_id", name="uq_arrival_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    event_seq: Mapped[int] = mapped_column(Integer)
    server_at_ms: Mapped[int] = mapped_column(Integer)
    lat: Mapped[float] = mapped_column()
    lng: Mapped[float] = mapped_column()
    distance_m: Mapped[int] = mapped_column(Integer)
    within_geofence: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Cancellation(Base):
    """取消单。每笔订单最多一张；金额字段记录的是“系统初判”，永不改变。

    客服改判后的结果追加在 reviews 中，current_fee_cents 始终等于最近一次
    有效改判后的金额；原始金额 original_fee_cents 保留不动，双方均可查看。
    """

    __tablename__ = "cancellations"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    event_seq: Mapped[int] = mapped_column(Integer)
    created_at_ms: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(255), default="")

    original_stage: Mapped[str] = mapped_column(String(16))
    original_fee_cents: Mapped[int] = mapped_column(Integer)
    basis: Mapped[dict] = mapped_column(JSON)

    current_stage: Mapped[str] = mapped_column(String(16))
    current_fee_cents: Mapped[int] = mapped_column(Integer)

    reviews: Mapped[list["Review"]] = relationship(
        back_populates="cancellation", cascade="all, delete-orphan",
        order_by="Review.id",
    )


class Review(Base):
    """客服改判记录：只追加，不改写。原金额、新金额、理由、核对证据全部留痕。"""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    cancellation_id: Mapped[int] = mapped_column(ForeignKey("cancellations.id"))
    created_at_ms: Mapped[int] = mapped_column(Integer)
    agent: Mapped[str] = mapped_column(String(64), default="客服")

    previous_stage: Mapped[str] = mapped_column(String(16))
    new_stage: Mapped[str] = mapped_column(String(16))
    previous_fee_cents: Mapped[int] = mapped_column(Integer)
    new_fee_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)

    cancellation: Mapped[Cancellation] = relationship(back_populates="reviews")


class LedgerEntry(Base):
    """账本流水：只追加，不更新、不删除。

    amount_signed_cents：扣费为正、退款为负。
    锁账后客服改判产生的差额只能以 additional_charge / additional_refund 追加。
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ledger_idem"),
        Index("ix_ledger_order", "order_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    kind: Mapped[str] = mapped_column(String(24))
    # payment / cancellation_fee / refund / additional_charge / additional_refund
    amount_signed_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(255))
    ref_type: Mapped[str] = mapped_column(String(24), default="")
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at_ms: Mapped[int] = mapped_column(Integer)


class PaymentTxn(Base):
    """模拟支付/退款网关流水，幂等键唯一。"""

    __tablename__ = "payment_txns"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_pay_idem"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    kind: Mapped[str] = mapped_column(String(16))  # pay / charge / refund
    amount_cents: Mapped[int] = mapped_column(Integer)  # 恒为正
    external_ref: Mapped[str] = mapped_column(String(48), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="succeeded")
    created_at_ms: Mapped[int] = mapped_column(Integer)


class IdempotencyRecord(Base):
    """通用请求幂等记录（取消确认、退款重试）。"""

    __tablename__ = "idempotency_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    response: Mapped[dict] = mapped_column(JSON)
    created_at_ms: Mapped[int] = mapped_column(Integer)
