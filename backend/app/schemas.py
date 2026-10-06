"""Pydantic 入参/出参模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


def parse_time(v) -> float:
    """接受 Unix 秒或 ISO8601 字符串。"""
    if v is None:
        return None  # type: ignore[return-value]
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()


def yuan_to_cents(v) -> int:
    return int(round(float(v) * 100))


class CreateOrderIn(BaseModel):
    customer_name: str
    service_type: str = "日常保洁"
    service_address: str
    lat: float
    lng: float
    amount_yuan: float = Field(gt=0)
    scheduled_at: object  # ISO 字符串或 Unix 秒
    paid: bool = False
    prepaid_yuan: Optional[float] = None
    code: Optional[str] = None


class ClientTimeIn(BaseModel):
    client_time: Optional[object] = None
    note: Optional[str] = None


class CheckinIn(BaseModel):
    lat: float
    lng: float
    accuracy_m: Optional[float] = None
    client_time: Optional[object] = None


class DisputeIn(BaseModel):
    claims_arrived: bool
    note: str
    client_time: Optional[object] = None


class CancelIn(BaseModel):
    reason: Optional[str] = None
    client_time: Optional[object] = None
    idempotency_key: Optional[str] = None


class AdjudicateIn(BaseModel):
    operator: str = "客服01"
    reason: str
    arrival_confirmed: Optional[bool] = None
    target_state: Optional[str] = None
    waive: bool = False
    amount_yuan: Optional[float] = None


class AdjustmentIn(BaseModel):
    amount_yuan: float  # 正=补收，负=退款
    reason: str
    operator: str = "客服01"
    idempotency_key: Optional[str] = None


class ClockIn(BaseModel):
    fixed_iso: Optional[object] = None
    advance_seconds: Optional[float] = None
    reset: bool = False


class FailIn(BaseModel):
    key_substring: str
    times: int = 1
