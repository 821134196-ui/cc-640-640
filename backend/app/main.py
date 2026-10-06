"""FastAPI 入口：家政服务临时取消与空跑费核算系统。"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import schemas
from . import models
from .database import engine, get_db, init_db
from .services import cancellation_service as svc
from .services import clock, payment
from .services.locking import write_locked


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="家政服务临时取消与空跑费核算系统", version="1.0", lifespan=lifespan)


def _order_or_404(db, order_id):
    o = svc.get_order(db, order_id)
    if o is None:
        raise HTTPException(404, "订单不存在")
    return o


def _detail(db, order):
    return svc.serialize_order(db, order)


# ---------------- 订单 ----------------

@app.post("/api/orders", dependencies=[Depends(write_locked)])
def create_order(body: schemas.CreateOrderIn, db: Session = Depends(get_db)):
    scheduled = schemas.parse_time(body.scheduled_at)
    data = {
        "code": body.code or f"HK{uuid.uuid4().hex[:8].upper()}",
        "customer_name": body.customer_name,
        "service_type": body.service_type,
        "service_address": body.service_address,
        "service_lat": body.lat,
        "service_lng": body.lng,
        "service_amount_cents": schemas.yuan_to_cents(body.amount_yuan),
        "scheduled_start": scheduled,
        "paid": body.paid,
        "prepaid_cents": schemas.yuan_to_cents(body.prepaid_yuan)
        if body.prepaid_yuan is not None
        else (schemas.yuan_to_cents(body.amount_yuan) if body.paid else 0),
        "client_time": None,
    }
    order = svc.create_order(db, data)
    return _detail(db, order)


@app.get("/api/orders")
def list_orders(db: Session = Depends(get_db)):
    orders = svc.list_orders(db)
    return [
        {
            "id": o.id,
            "code": o.code,
            "customer_name": o.customer_name,
            "service_type": o.service_type,
            "status": o.status,
            "arrival_disputed": o.arrival_disputed,
            "paid": o.paid,
            "service_amount_yuan": f"{o.service_amount_cents / 100:.2f}",
            "scheduled_start": models.iso(o.scheduled_start),
        }
        for o in orders
    ]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str, db: Session = Depends(get_db)):
    return _detail(db, _order_or_404(db, order_id))


# ---------------- 服务员事件 ----------------

@app.post("/api/orders/{order_id}/dispatch", dependencies=[Depends(write_locked)])
def dispatch(order_id: str, body: schemas.ClientTimeIn = schemas.ClientTimeIn(),
             db: Session = Depends(get_db)):
    order = _order_or_404(db, order_id)
    try:
        svc.mark_dispatched(db, order, client_time=schemas.parse_time(body.client_time), note=body.note)
    except svc.Conflict as e:
        raise HTTPException(409, e.msg)
    return _detail(db, order)


@app.post("/api/orders/{order_id}/checkin", dependencies=[Depends(write_locked)])
def checkin(order_id: str, body: schemas.CheckinIn, db: Session = Depends(get_db)):
    order = _order_or_404(db, order_id)
    try:
        res = svc.attendant_checkin(
            db, order, lat=body.lat, lng=body.lng, accuracy_m=body.accuracy_m,
            client_time=schemas.parse_time(body.client_time),
        )
    except svc.Conflict as e:
        raise HTTPException(409, e.msg)
    return {**res, "order": _detail(db, order)}


@app.post("/api/orders/{order_id}/dispute", dependencies=[Depends(write_locked)])
def dispute(order_id: str, body: schemas.DisputeIn, db: Session = Depends(get_db)):
    order = _order_or_404(db, order_id)
    svc.raise_dispute(
        db, order, claims_arrived=body.claims_arrived, note=body.note,
        client_time=schemas.parse_time(body.client_time),
    )
    return _detail(db, order)


# ---------------- 取消：预览 → 确认 ----------------

@app.get("/api/orders/{order_id}/cancel/preview")
def cancel_preview(order_id: str, db: Session = Depends(get_db)):
    order = _order_or_404(db, order_id)
    return svc.preview_cancel(db, order)


@app.post("/api/orders/{order_id}/cancel", dependencies=[Depends(write_locked)])
def cancel(order_id: str, body: schemas.CancelIn, db: Session = Depends(get_db)):
    order = _order_or_404(db, order_id)
    key = body.idempotency_key or f"cancel:{order_id}:{uuid.uuid4().hex}"
    try:
        res = svc.confirm_cancel(
            db, order, idempotency_key=key, reason=body.reason,
            client_time=schemas.parse_time(body.client_time),
        )
    except svc.Conflict as e:
        raise HTTPException(409, e.msg)
    except svc.SettlementPending as e:
        raise HTTPException(
            402, {"message": "取消已记录，但支付/退款失败，可重试", "detail": e.msg,
                  "retryable": True}
        )
    return res


# ---------------- 客服：改判 / 锁账 / 锁后追加单 ----------------

@app.post("/api/orders/{order_id}/adjudicate", dependencies=[Depends(write_locked)])
def adjudicate(order_id: str, body: schemas.AdjudicateIn, db: Session = Depends(get_db)):
    order = _order_or_404(db, order_id)
    try:
        svc.adjudicate(
            db, order, operator=body.operator, reason=body.reason,
            arrival_confirmed=body.arrival_confirmed, target_state=body.target_state,
            waive=body.waive,
            amount_cents=schemas.yuan_to_cents(body.amount_yuan)
            if body.amount_yuan is not None else None,
        )
    except svc.NotFound as e:
        raise HTTPException(404, e.msg)
    except svc.Conflict as e:
        raise HTTPException(409, e.msg)
    except svc.SettlementPending as e:
        raise HTTPException(
            402, {"message": "改判已记录，但差额结算失败，可重试", "detail": e.msg,
                  "retryable": True, "adjudication_id": e.adjudication_id}
        )
    return _detail(db, order)


@app.post("/api/orders/{order_id}/lock", dependencies=[Depends(write_locked)])
def lock(order_id: str, db: Session = Depends(get_db)):
    order = _order_or_404(db, order_id)
    try:
        return svc.lock_account(db, order)
    except svc.Conflict as e:
        raise HTTPException(409, e.msg)


@app.post("/api/orders/{order_id}/adjustments", dependencies=[Depends(write_locked)])
def append_adjustment(order_id: str, body: schemas.AdjustmentIn,
                      db: Session = Depends(get_db)):
    order = _order_or_404(db, order_id)
    key = body.idempotency_key or uuid.uuid4().hex
    try:
        svc.post_lock_adjustment(
            db, order, amount_cents=schemas.yuan_to_cents(body.amount_yuan),
            reason=body.reason, operator=body.operator, idempotency_key=key,
        )
    except svc.Conflict as e:
        raise HTTPException(409, e.msg)
    except svc.SettlementPending as e:
        raise HTTPException(402, {"message": "追加单支付失败，可重试", "detail": e.msg})
    return _detail(db, order)


@app.post("/api/orders/{order_id}/retry-settlement", dependencies=[Depends(write_locked)])
def retry_settlement(order_id: str, db: Session = Depends(get_db)):
    order = _order_or_404(db, order_id)
    try:
        report = svc.retry_settlement(db, order)
    except svc.NotFound as e:
        raise HTTPException(404, e.msg)
    except svc.SettlementPending as e:
        raise HTTPException(402, {"message": "重试后仍失败，可再次重试", "detail": e.msg,
                                  "retryable": True})
    return {"report": report, "order": _detail(db, order)}


# ---------------- 演示控制：时钟与支付故障注入 ----------------

@app.post("/api/dev/clock")
def dev_clock(body: schemas.ClockIn):
    import datetime as _dt

    if body.reset:
        clock.reset()
    if body.fixed_iso is not None:
        clock.set_fixed(schemas.parse_time(body.fixed_iso))
    if body.advance_seconds is not None:
        clock.advance(float(body.advance_seconds))
    return {"now": clock.now(), "now_iso": _dt.datetime.fromtimestamp(clock.now()).isoformat()}


@app.post("/api/dev/payment/fail")
def dev_payment_fail(body: schemas.FailIn):
    payment.inject_failure(body.key_substring, body.times)
    return {"injected": body.model_dump()}


@app.post("/api/dev/payment/reset")
def dev_payment_reset():
    payment.reset_sim()
    return {"ok": True}


# ---------------- 前端静态托管 ----------------

from .config import FRONTEND_DIR  # noqa: E402

if FRONTEND_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(FRONTEND_DIR)),
        name="static",
    )

    @app.get("/")
    def index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))
