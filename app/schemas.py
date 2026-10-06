from pydantic import BaseModel, Field


class OrderIn(BaseModel):
    customer_name: str = Field(min_length=1, max_length=64)
    service_start_at_ms: int
    total_cents: int = Field(gt=0)
    paid: bool = True


class DispatchIn(BaseModel):
    client_at_ms: int | None = None
    note: str = ""


class ArriveIn(BaseModel):
    lat: float
    lng: float
    client_at_ms: int | None = None
    note: str = ""


class CancelIn(BaseModel):
    reason: str = ""
    client_at_ms: int | None = None
    idempotency_key: str | None = None


class ReviewIn(BaseModel):
    new_stage: str
    reason: str = Field(min_length=1)
    agent: str = "客服001"
    new_fee_cents: int | None = None


class RuleIn(BaseModel):
    name: str
    free_cancel_before_minutes: int = Field(ge=0)
    departed_fee_percent: int = Field(ge=0, le=100)
    arrived_fee_percent: int = Field(ge=0, le=100)
    min_fee_cents: int = Field(ge=0, default=0)
    max_fee_cents: int = Field(ge=0, default=0)
