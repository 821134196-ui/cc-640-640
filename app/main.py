from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services
from app.clock import now_ms
from app.database import ROOT, SessionLocal, init_db
from app.models import Arrival, Cancellation, Order, PaymentTxn, PricingRule
from app.schemas import ArriveIn, CancelIn, DispatchIn, OrderIn, ReviewIn, RuleIn
from app.seed import reset_and_seed


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    db = SessionLocal()
    need_seed = db.scalar(select(PricingRule)) is None
    db.close()
    if need_seed:
        reset_and_seed()
    yield


app = FastAPI(title="家政临时取消与空跑费核算系统", lifespan=lifespan)


@app.exception_handler(services.BizError)
async def biz_error_handler(_request, exc: services.BizError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


def _get_order(db: Session, ident: str) -> Order:
    try:
        order = db.get(Order, int(ident))
    except ValueError:
        order = db.scalar(select(Order).where(Order.code == ident))
    if order is None:
        raise HTTPException(404, "订单不存在")
    return order


def _order_dict(order: Order, db: Session) -> dict:
    events = sorted(order.events, key=lambda e: e.seq)
    cancel = db.scalar(select(Cancellation).where(Cancellation.order_id == order.id))
    if order.status == "cancelled" and cancel:
        stage = cancel.current_stage
    else:
        stage = services.stage_from_events(events)
    return {
        "id": order.id,
        "code": order.code,
        "customer_name": order.customer_name,
        "service_start_at_ms": order.service_start_at_ms,
        "total_cents": order.total_cents,
        "paid": order.paid,
        "status": order.status,
        "ledger_locked": order.ledger_locked,
        "rule_snapshot": order.rule_snapshot,
        "stage": stage,
        "events": [
            {"seq": e.seq, "type": e.type, "server_at_ms": e.server_at_ms,
             "client_at_ms": e.client_at_ms, "note": e.note, "payload": e.payload}
            for e in events
        ],
        "created_at_ms": order.created_at_ms,
    }


@app.post("/api/admin/reseed")
def reseed():
    return reset_and_seed()


@app.get("/api/rules/current")
def current_rule(db: Session = Depends(get_db)):
    rule = db.scalar(select(PricingRule).where(PricingRule.active == True)
                     .order_by(PricingRule.id.desc()))  # noqa: E712
    return {
        "id": rule.id, "name": rule.name,
        "free_cancel_before_minutes": rule.free_cancel_before_minutes,
        "departed_fee_percent": rule.departed_fee_percent,
        "arrived_fee_percent": rule.arrived_fee_percent,
        "min_fee_cents": rule.min_fee_cents, "max_fee_cents": rule.max_fee_cents,
    }


@app.put("/api/rules")
def update_rule(body: RuleIn, db: Session = Depends(get_db)):
    """改规则只影响之后新下的订单；历史订单使用各自快照。"""
    old = db.scalar(select(PricingRule).where(PricingRule.active == True))  # noqa: E712
    if old:
        old.active = False
    rule = PricingRule(
        name=body.name, active=True,
        free_cancel_before_minutes=body.free_cancel_before_minutes,
        departed_fee_percent=body.departed_fee_percent,
        arrived_fee_percent=body.arrived_fee_percent,
        min_fee_cents=body.min_fee_cents, max_fee_cents=body.max_fee_cents,
        created_at_ms=now_ms(),
    )
    db.add(rule)
    db.commit()
    return {"ok": True, "id": rule.id,
            "note": "新规则仅对之后创建的订单生效，历史订单仍按下单时快照计费。"}


@app.get("/api/orders")
def list_orders(db: Session = Depends(get_db)):
    orders = db.scalars(select(Order).order_by(Order.id)).all()
    return [_order_dict(o, db) for o in orders]


@app.post("/api/orders", status_code=201)
def create_order(body: OrderIn, db: Session = Depends(get_db)):
    rule = db.scalar(select(PricingRule).where(PricingRule.active == True)  # noqa: E712
                     .order_by(PricingRule.id.desc()))
    if rule is None:
        raise HTTPException(409, "没有生效的收费规则，请先创建规则")
    order = services.create_order(
        db, customer_name=body.customer_name,
        service_start_at_ms=body.service_start_at_ms,
        total_cents=body.total_cents, paid=body.paid, rule=rule, now_ms=now_ms(),
    )
    return _order_dict(order, db)


@app.get("/api/orders/{ident}")
def get_order(ident: str, db: Session = Depends(get_db)):
    return _order_dict(_get_order(db, ident), db)


@app.post("/api/orders/{ident}/dispatch")
def dispatch(ident: str, body: DispatchIn, db: Session = Depends(get_db)):
    order = _get_order(db, ident)
    ev = services.dispatch(db, order, now_ms=now_ms(),
                           client_at_ms=body.client_at_ms, note=body.note)
    return {"ok": True, "seq": ev.seq, "server_at_ms": ev.server_at_ms}


@app.post("/api/orders/{ident}/arrive")
def arrive(ident: str, body: ArriveIn, db: Session = Depends(get_db)):
    order = _get_order(db, ident)
    return services.arrive(db, order, lat=body.lat, lng=body.lng, now_ms=now_ms(),
                           client_at_ms=body.client_at_ms, note=body.note)


@app.get("/api/orders/{ident}/arrivals")
def arrivals(ident: str, db: Session = Depends(get_db)):
    order = _get_order(db, ident)
    rows = db.scalars(select(Arrival).where(Arrival.order_id == order.id)
                      .order_by(Arrival.event_seq)).all()
    return [
        {"event_seq": a.event_seq, "server_at_ms": a.server_at_ms,
         "lat": a.lat, "lng": a.lng, "distance_m": a.distance_m,
         "within_geofence": a.within_geofence, "detail": a.detail}
        for a in rows
    ]


@app.get("/api/orders/{ident}/cancel-preview")
def cancel_preview(ident: str, client_at_ms: int | None = None,
                   db: Session = Depends(get_db)):
    """第一步：客户先看到可能产生的空跑费与计算依据（本接口不产生任何费用/状态变化）。"""
    order = _get_order(db, ident)
    return services.preview_cancel(db, order, now_ms=now_ms())


@app.post("/api/orders/{ident}/cancel")
def cancel(ident: str, body: CancelIn, db: Session = Depends(get_db)):
    """第二步：客户看过预览后再确认提交；重复提交只处理一次。"""
    order = _get_order(db, ident)
    return services.confirm_cancel(
        db, order, reason=body.reason, now_ms=now_ms(),
        client_at_ms=body.client_at_ms, idempotency_key=body.idempotency_key,
    )


@app.get("/api/orders/{ident}/cancellation")
def get_cancellation(ident: str, db: Session = Depends(get_db)):
    """取消单与全部改判记录 —— 客户和客服都可以查看。"""
    order = _get_order(db, ident)
    c = db.scalar(select(Cancellation).where(Cancellation.order_id == order.id))
    if c is None:
        return None
    return {
        "id": c.id, "created_at_ms": c.created_at_ms, "reason": c.reason,
        "original_stage": c.original_stage, "original_fee_cents": c.original_fee_cents,
        "current_stage": c.current_stage, "current_fee_cents": c.current_fee_cents,
        "basis": c.basis,
        "reviews": [
            {"id": r.id, "created_at_ms": r.created_at_ms, "agent": r.agent,
             "previous_stage": r.previous_stage, "new_stage": r.new_stage,
             "previous_fee_cents": r.previous_fee_cents, "new_fee_cents": r.new_fee_cents,
             "reason": r.reason, "evidence": r.evidence}
            for r in c.reviews
        ],
    }


@app.post("/api/orders/{ident}/reviews")
def add_review(ident: str, body: ReviewIn, db: Session = Depends(get_db)):
    order = _get_order(db, ident)
    r = services.agent_review(
        db, order, new_stage=body.new_stage, reason=body.reason,
        agent=body.agent, new_fee_cents=body.new_fee_cents, now_ms=now_ms(),
    )
    return {"ok": True, "review_id": r.id,
            "note": "改判已追加记录；原金额保留，差额以补收/追加退款单入账，原账未改动。"}


@app.post("/api/orders/{ident}/refund-retry")
def refund_retry(ident: str, idempotency_key: str | None = None,
                 db: Session = Depends(get_db)):
    order = _get_order(db, ident)
    return services.retry_refund(db, order, now_ms=now_ms(),
                                 idempotency_key=idempotency_key)


@app.get("/api/orders/{ident}/ledger")
def ledger(ident: str, db: Session = Depends(get_db)):
    order = _get_order(db, ident)
    rec = services.reconciliation(db, order)
    txns = db.scalars(select(PaymentTxn).where(PaymentTxn.order_id == order.id)
                      .order_by(PaymentTxn.id)).all()
    rec["gateway_txns"] = [
        {"id": t.id, "kind": t.kind, "amount_cents": t.amount_cents,
         "external_ref": t.external_ref, "idempotency_key": t.idempotency_key,
         "status": t.status, "created_at_ms": t.created_at_ms}
        for t in txns
    ]
    return rec


# ---------- 并发临界演示：出发与取消同时到达 ----------

@app.post("/api/orders/{ident}/race-demo")
def race_demo(ident: str, force: str | None = None, db: Session = Depends(get_db)):
    """同毫秒内“出发”和“取消”两个请求并发到达。

    两边请求携带的客户端时间互相矛盾（出发方故意报早、取消方故意报晚）。
    真实网络中两个请求到达服务端的先后是随机的：这里用随机先后放行模拟到达
    顺序（force=dispatch/cancel 可指定），但费用只由抢锁后落库的服务端事件
    seq 决定，客户端时间戳一律忽略。
    """
    import random
    import threading

    order = _get_order(db, ident)
    db.close()
    ts = now_ms()
    results: dict = {}
    winner = force if force in ("dispatch", "cancel") else random.choice(["dispatch", "cancel"])
    results["simulated_arrival_winner"] = winner
    gate = {"dispatch": threading.Event(), "cancel": threading.Event()}
    gate[winner].set()

    def _await(side: str):
        gate[side].wait()

    def do_dispatch():
        s = None
        try:
            _await("dispatch")
            s = SessionLocal()
            o = s.get(Order, order.id)
            ev = services.dispatch(s, o, now_ms=ts,
                                   client_at_ms=ts - 999_999,  # 客户端故意报更早
                                   note="临界：服务员点击出发")
            results["dispatch"] = {"seq": ev.seq, "server_at_ms": ev.server_at_ms}
            gate["cancel"].set()  # 后到请求紧接着进入，仍是同一毫秒
        except Exception as e:  # noqa: BLE001
            results["dispatch_error"] = str(e)
            gate["cancel"].set()
        finally:
            if s: s.close()

    def do_cancel():
        s = None
        try:
            _await("cancel")
            s = SessionLocal()
            o = s.get(Order, order.id)
            r = services.confirm_cancel(s, o, reason="临界：客户同时点击取消",
                                        now_ms=ts, client_at_ms=ts + 999_999,
                                        idempotency_key=f"race-cancel-{order.id}")
            results["cancel"] = {"stage": r["current_stage"],
                                 "fee_cents": r["current_fee_cents"]}
            gate["dispatch"].set()
        except Exception as e:  # noqa: BLE001
            results["cancel_error"] = str(e)
            gate["dispatch"].set()
        finally:
            if s: s.close()

    t1, t2 = threading.Thread(target=do_dispatch), threading.Thread(target=do_cancel)
    t1.start(); t2.start(); t1.join(); t2.join()
    return {"server_at_ms": ts, **results}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
