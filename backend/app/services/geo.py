"""定位校验（模拟）。

用经纬度球面距离与服务地址比较，给出距服务点距离与是否在围栏内，
供客服在“是否到场”争议中核对签到与定位。
"""
from __future__ import annotations

import math

EARTH_R = 6371000.0
DEFAULT_GEOFENCE_M = 200.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def evaluate(lat, lng, order_lat, order_lng, geofence_m=DEFAULT_GEOFENCE_M) -> dict:
    if lat is None or lng is None:
        return {"distance_m": None, "within_geofence": False, "geofence_m": geofence_m}
    d = haversine_m(lat, lng, order_lat, order_lng)
    return {
        "distance_m": round(d, 1),
        "within_geofence": d <= geofence_m,
        "geofence_m": geofence_m,
    }
