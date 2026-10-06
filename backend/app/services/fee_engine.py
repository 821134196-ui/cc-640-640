"""空跑费计算引擎。

关键约束：
1. 取消时服务员“处于什么状态”，只看订单事件流（按 seq 排序的服务端事件），
   不采信任何客户端时间。因此“出发”和“取消”几乎同时发生时，谁先落库谁生效，
   客户端时间再怎么接近或被篡改，都不会算出不同费用。
2. 使用下单锁定的规则快照，当前规则改版不影响本单。
3. 返回结构化“计算依据”，供取消预览直接展示。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# 取消时服务员可能的状态
NOT_DISPATCHED = "NOT_DISPATCHED"
DISPATCHED = "DISPATCHED"
ARRIVED = "ARRIVED"
DISPUTED = "DISPUTED"  # 是否到场存在争议、待客服判定；费用先按保守档预估


@dataclass
class FeeResult:
    result: str
    amount_cents: int
    state: str
    provisional: bool
    basis: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "result": self.result,
            "amount_cents": self.amount_cents,
            "amount_yuan": _yuan(self.amount_cents),
            "state": self.state,
            "state_label": STATE_LABELS.get(self.state, self.state),
            "provisional": self.provisional,
            "basis": self.basis,
        }


STATE_LABELS = {
    NOT_DISPATCHED: "服务员尚未出发",
    DISPATCHED: "服务员已出发、尚未到场",
    ARRIVED: "服务员已到场",
    DISPUTED: "是否到场存在争议（待客服核对）",
}


def _half_up(n: int, d: int) -> int:
    """正整数除以 d，四舍五入（半数向上）。"""
    return (2 * n + d) // (2 * d)


def _yuan(cents: int) -> str:
    return f"{cents / 100:.2f}"


def state_from_events(events, *, arrival_disputed: bool = False) -> str:
    """依据服务端事件流判定取消发生时的状态。

    events 需按 seq 升序。只认事件类型与顺序，不看 client_time。
    """
    has_dispatch = False
    has_arrival = False
    for e in events:
        if e.event_type == "attendant_dispatched":
            has_dispatch = True
        elif e.event_type == "attendant_arrived":
            has_arrival = True
        elif e.event_type == "attendant_dispatch_undone":
            # 正常流程不会出现；保留语义：撤回出发
            has_dispatch = False

    if arrival_disputed:
        return DISPUTED
    if has_arrival:
        return ARRIVED
    if has_dispatch:
        return DISPATCHED
    return NOT_DISPATCHED


def _tier_for_state(rules: dict, state: str) -> tuple[dict | None, bool]:
    """返回 (命中的规则档, 是否为争议下的保守兜底)。"""
    provisional = False
    lookup_state = state
    if state == DISPUTED:
        # 争议未决时，先按“已出发未到场”保守预估；最终以客服判定为准
        lookup_state = DISPATCHED
        provisional = True
    for tier in rules["tiers"]:
        if tier["state"] == lookup_state:
            return tier, provisional
    return None, provisional


def calculate(
    rules: dict,
    state: str,
    service_amount_cents: int,
    *,
    scheduled_start: float | None = None,
    cancel_server_time: float | None = None,
    rule_version: int | None = None,
) -> FeeResult:
    """按锁定规则计算空跑费，并生成计算依据。纯函数，便于测试。"""
    basis: list[dict] = []
    basis.append(
        {
            "k": "rule",
            "label": "计费规则",
            "text": f"按下单时锁定的规则版本 v{rule_version if rule_version else 1} 计算（之后规则调整不影响本单）",
        }
    )
    basis.append(
        {
            "k": "state",
            "label": "取消时状态（按服务端事件顺序判定）",
            "text": STATE_LABELS.get(state, state),
        }
    )

    lead_hours = None
    if scheduled_start and cancel_server_time:
        lead_hours = (scheduled_start - cancel_server_time) / 3600.0
        basis.append(
            {
                "k": "lead",
                "label": "距服务开始",
                "text": f"{lead_hours:.1f} 小时（免费窗口 {rules['free_window_hours']:.0f} 小时，且仅在服务员未出发时适用）",
            }
        )

    tier, provisional = _tier_for_state(rules, state)
    if tier is None:  # pragma: no cover - 规则数据保证覆盖
        return FeeResult("NO_RULE", 0, state, provisional, basis)

    mode = tier["fee_mode"]
    amount = 0
    if mode == "fixed":
        amount = int(tier["fixed_cents"])
        basis.append(
            {"k": "formula", "label": "计算方式", "text": f"{tier['label']}：固定 {_yuan(amount)} 元"}
        )
    elif mode == "percent":
        bp = int(tier["percent_bp"])
        raw = _half_up(service_amount_cents * bp, 10000)
        amount = raw
        detail = (
            f"{tier['label']}：服务费 {_yuan(service_amount_cents)} 元 × {bp / 100:.0f}% "
            f"= {_yuan(raw)} 元"
        )
        min_c = tier.get("min_cents")
        max_c = tier.get("max_cents")
        if min_c is not None and raw < min_c:
            amount = min_c
            detail += f"；低于最低空跑费 {_yuan(min_c)} 元，按 {_yuan(amount)} 元计"
        if max_c is not None and raw > max_c:
            amount = max_c
            detail += f"；超过封顶 {_yuan(max_c)} 元，按 {_yuan(amount)} 元计"
        basis.append({"k": "formula", "label": "计算方式", "text": detail})

    if provisional:
        basis.append(
            {
                "k": "dispute",
                "label": "争议提示",
                "text": "“是否到场”存在争议，当前金额为系统保守预估；客服核对签到与定位后改判，以最终结果为准。",
            }
        )

    # 费用不可能超过服务费
    if amount > service_amount_cents:
        basis.append(
            {
                "k": "cap",
                "label": "封顶",
                "text": f"空跑费不超过服务费 {_yuan(service_amount_cents)} 元",
            }
        )
        amount = service_amount_cents

    basis.append(
        {"k": "total", "label": "预计空跑费", "text": f"{_yuan(amount)} 元", "amount_cents": amount}
    )
    return FeeResult(tier["result"], amount, state, provisional, basis)


def rules_from_snapshot(snapshot) -> dict:
    return json.loads(snapshot.rules_json)
