"""模拟支付/退款网关。

- 真实环境应替换为第三方支付 SDK；这里只在内存里记账并返回一个交易号。
- 以幂等键保证“一次操作只成功一次”：同一把钥匙重试，永远返回同一结果，
  因此取消重试、退款重试不会重复扣款或重复退款。
- 支持注入失败（测试/演示）：某把钥匙前 N 次调用返回失败，之后成功。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .. import models  # noqa: TCH001  (避免循环：仅类型)

_lock = threading.Lock()

# key 子串 -> 还要失败多少次
_fail_plan: dict[str, int] = {}
_call_log: dict[str, int] = {}


def reset_sim() -> None:
    with _lock:
        _fail_plan.clear()
        _call_log.clear()


def inject_failure(key_substring: str, times: int = 1) -> None:
    with _lock:
        _fail_plan[key_substring] = times


def _should_fail(key: str) -> tuple[bool, str | None]:
    with _lock:
        _call_log[key] = _call_log.get(key, 0) + 1
        for substr, remaining in list(_fail_plan.items()):
            if substr in key and remaining > 0:
                _fail_plan[substr] = remaining - 1
                return True, f"模拟网关故障（{substr} 计划失败中，剩余 {remaining - 1} 次）"
    return False, None


@dataclass
class GatewayResult:
    ok: bool
    payment_ref: str | None
    error: str | None


def _gateway(key: str, kind: str, amount_cents: int, db) -> GatewayResult:
    """执行一次网关调用，并以 PaymentAttempt 做持久化幂等。

    已成功的钥匙直接返回原交易号；失败的钥匙允许重试。
    """
    attempt = db.get(models.PaymentAttempt, key)
    if attempt is not None and attempt.status == "succeeded":
        return GatewayResult(True, attempt.payment_ref, None)

    failed, err = _should_fail(key)
    ref = f"PAY-{key[-10:]}-{_call_log.get(key, 0)}" if not failed else None

    if attempt is None:
        attempt = models.PaymentAttempt(
            idempotency_key=key, kind=kind, amount_cents=amount_cents, attempts=0
        )
        db.add(attempt)
    attempt.attempts += 1
    from .clock import now

    attempt.updated_at = now()
    if not failed:
        attempt.status = "succeeded"
        attempt.payment_ref = ref
        attempt.last_error = None
        return GatewayResult(True, ref, None)

    attempt.status = "failed"
    attempt.last_error = err
    return GatewayResult(False, None, err)


def charge(db, key: str, amount_cents: int) -> GatewayResult:
    if amount_cents <= 0:
        return GatewayResult(True, "PAY-ZERO", None)
    return _gateway(key, "CHARGE", amount_cents, db)


def refund(db, key: str, amount_cents: int) -> GatewayResult:
    if amount_cents <= 0:
        return GatewayResult(True, "PAY-ZERO", None)
    return _gateway(key, "REFUND", amount_cents, db)
