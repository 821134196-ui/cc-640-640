"""模拟的定位与支付接口（不可替换为真实服务，业务正确性不依赖外部）。"""
from __future__ import annotations

import hashlib
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PaymentTxn

# 模拟的服务地址（上海人民广场附近），电子围栏半径
SERVICE_LAT, SERVICE_LNG, GEOFENCE_RADIUS_M = 31.2304, 121.4737, 300


def check_in_location(lat: float, lng: float) -> dict:
    """等距投影估算距离，判定签到定位是否在服务地址围栏内。"""
    lat_rad = math.radians(SERVICE_LAT)
    dx = (lng - SERVICE_LNG) * 111_320.0 * math.cos(lat_rad)
    dy = (lat - SERVICE_LAT) * 110_540.0
    distance = int(math.hypot(dx, dy))
    return {
        "lat": lat,
        "lng": lng,
        "service_lat": SERVICE_LAT,
        "service_lng": SERVICE_LNG,
        "geofence_radius_m": GEOFENCE_RADIUS_M,
        "distance_m": distance,
        "within_geofence": distance <= GEOFENCE_RADIUS_M,
    }


def _external_ref(idempotency_key: str) -> str:
    return "MOCK-" + hashlib.sha1(idempotency_key.encode()).hexdigest()[:16].upper()


class MockPaymentGateway:
    """幂等的模拟支付网关：同一幂等键永远返回同一笔流水，不重复扣款/退款。"""

    def _record(
        self, db: Session, order_id: int, kind: str, amount_cents: int, key: str, now_ms: int
    ) -> PaymentTxn:
        existing = db.scalar(select(PaymentTxn).where(PaymentTxn.idempotency_key == key))
        if existing:
            return existing
        txn = PaymentTxn(
            order_id=order_id,
            kind=kind,
            amount_cents=amount_cents,
            external_ref=_external_ref(key),
            idempotency_key=key,
            status="succeeded",
            created_at_ms=now_ms,
        )
        db.add(txn)
        db.flush()
        return txn

    def pay(self, db, order_id, amount_cents, key, now_ms):
        return self._record(db, order_id, "pay", amount_cents, key, now_ms)

    def charge(self, db, order_id, amount_cents, key, now_ms):
        return self._record(db, order_id, "charge", amount_cents, key, now_ms)

    def refund(self, db, order_id, amount_cents, key, now_ms):
        return self._record(db, order_id, "refund", amount_cents, key, now_ms)


gateway = MockPaymentGateway()
