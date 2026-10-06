"""收费规则（当前版本）与下单时快照。

规则用结构化数据表达；订单只引用下单那一刻的快照，之后规则改版不影响存量订单。
金额单位：分。percent_bp 为基点（10000bp = 100%）。
"""
from __future__ import annotations

import copy
import json

from ..services import clock

RULE_CODE = "housekeeping_cancel"

DEFAULT_RULES = {
    "currency": "CNY",
    # 距服务开始不少于该小时数取消，且服务员尚未出发：免收
    "free_window_hours": 24,
    "tiers": [
        {
            "state": "NOT_DISPATCHED",
            "result": "FREE_NOT_DISPATCHED",
            "label": "服务员尚未出发",
            "fee_mode": "fixed",
            "fixed_cents": 0,
        },
        {
            "state": "DISPATCHED",
            "result": "DISPATCH_FEE",
            "label": "服务员已出发、未到场（空跑费）",
            "fee_mode": "percent",
            "percent_bp": 3000,
            "min_cents": 2000,
            "max_cents": None,
        },
        {
            "state": "ARRIVED",
            "result": "ARRIVAL_FEE",
            "label": "服务员已到场（空跑费）",
            "fee_mode": "percent",
            "percent_bp": 8000,
            "min_cents": 5000,
            "max_cents": None,
        },
    ],
}


def current_rules() -> dict:
    return copy.deepcopy(DEFAULT_RULES)


def make_snapshot(db, *, version: int = 1) -> "object":
    """落库一份规则快照并返回 ORM 对象。"""
    from ..models import PricingRuleSnapshot

    snap = PricingRuleSnapshot(
        code=RULE_CODE,
        name="家政服务临时取消与空跑费规则",
        version=version,
        rules_json=json.dumps(current_rules(), ensure_ascii=False),
        currency="CNY",
        created_at=clock.now(),
    )
    db.add(snap)
    db.flush()
    return snap
