"""空跑费计算核心 —— 只使用下单时锁定的规则快照与服务端事件时间。"""
from __future__ import annotations

from typing import Any

STAGE_BOOKED = "booked"
STAGE_DEPARTED = "departed"
STAGE_ARRIVED = "arrived"

STAGE_LABELS = {
    STAGE_BOOKED: "服务员未出发",
    STAGE_DEPARTED: "服务员已出发、未到场",
    STAGE_ARRIVED: "服务员已到场",
}

MINUTE_MS = 60_000


def stage_from_events(events) -> str:
    """按服务端事件序号(seq)判定取消前的最后阶段。

    不看客户端时间：哪怕两个事件的客户端/服务端时间戳完全相同，
    seq 较小者为先 —— 这是“出发临界”唯一的判定口径。
    """
    ordered = sorted(events, key=lambda e: e.seq)
    stage = STAGE_BOOKED
    for e in ordered:
        if e.type == "cancelled":
            break
        if e.type == "dispatched":
            stage = STAGE_DEPARTED
        elif e.type == "arrived":
            stage = STAGE_ARRIVED
    return stage


def _round_half_up(total_cents: int, percent: int) -> int:
    return (total_cents * percent + 50) // 100


def compute_fee(
    rule_snapshot: dict[str, Any],
    stage: str,
    cancel_server_at_ms: int,
    service_start_at_ms: int,
    total_cents: int,
) -> dict[str, Any]:
    """依据规则快照计算空跑费，返回金额与完整计算依据（供预览/留档）。"""
    free_before = int(rule_snapshot["free_cancel_before_minutes"])
    minutes_before_start = (service_start_at_ms - cancel_server_at_ms) / MINUTE_MS

    percent = 0
    reason = ""
    if stage == STAGE_BOOKED:
        reason = "取消事件排在出发事件之前（或服务员尚未出发），无空跑成本，免费。"
    elif minutes_before_start >= free_before:
        reason = (
            f"虽已{STAGE_LABELS[stage]}，但取消时间距预约开始 "
            f"{minutes_before_start:.0f} 分钟 ≥ 免费取消时限 {free_before} 分钟，按规则免费。"
        )
    else:
        if stage == STAGE_DEPARTED:
            percent = int(rule_snapshot["departed_fee_percent"])
            reason = f"取消晚于出发、且早于到场，按订单金额 {percent}% 计空跑费。"
        else:
            percent = int(rule_snapshot["arrived_fee_percent"])
            reason = f"服务员已到场，按订单金额 {percent}% 计空跑费。"

    raw_fee = _round_half_up(total_cents, percent) if percent else 0
    fee = raw_fee
    min_fee = int(rule_snapshot.get("min_fee_cents", 0) or 0)
    max_fee = int(rule_snapshot.get("max_fee_cents", 0) or 0)
    clamp_note = ""
    if fee and min_fee and fee < min_fee:
        fee, clamp_note = min_fee, f"低于最低空跑费 {min_fee} 分，按最低费用收取。"
    if max_fee and fee > max_fee:
        fee, clamp_note = max_fee, f"超过封顶 {max_fee} 分，按封顶收取。"

    formula = (
        f"{total_cents} 分 × {percent}% = {raw_fee} 分（四舍五入）"
        if percent
        else "0 分"
    )
    return {
        "stage": stage,
        "stage_label": STAGE_LABELS[stage],
        "cancel_server_at_ms": cancel_server_at_ms,
        "service_start_at_ms": service_start_at_ms,
        "minutes_before_start": round(minutes_before_start, 1),
        "rule": {
            "name": rule_snapshot["name"],
            "version": rule_snapshot.get("version"),
            "free_cancel_before_minutes": free_before,
            "departed_fee_percent": rule_snapshot["departed_fee_percent"],
            "arrived_fee_percent": rule_snapshot["arrived_fee_percent"],
            "min_fee_cents": min_fee,
            "max_fee_cents": max_fee,
        },
        "total_cents": total_cents,
        "percent": percent,
        "raw_fee_cents": raw_fee,
        "fee_cents": fee,
        "formula": formula,
        "reason": reason + (clamp_note or ""),
    }
