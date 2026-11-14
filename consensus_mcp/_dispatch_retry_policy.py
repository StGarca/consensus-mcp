"""External-CLI retry / fallback policy (sp-consensus-optimization B6).

A pure decision function used by the shared dispatch runner. Quota can advance
the model chain within one seat before considering a replacement contributor.

Principle: a failure that is DETERMINISTIC (auth, trust/empty-output, quota) must NOT
burn the retry budget - retry only TRANSIENT failures, and tier the retry budget by
priority (a design/security consult is worth one retry; a chore is not). When a
contributor is skipped, prefer a same-role FALLBACK so the panel keeps >=2 independent
families rather than silently shrinking.
"""
from __future__ import annotations

# failure classes
RATE_LIMIT = "rate_limit"            # 429 - transient
STALL_TIMEOUT = "stall_timeout"      # watchdog kill - transient
INTEGRITY_FALSE_POSITIVE = "integrity_false_positive"  # kimi git-status race - transient (after quiescing)
AUTH_REQUIRED = "auth_required"      # deterministic config issue
QUOTA_EXCEEDED = "quota_exceeded"    # deterministic (plan limit)
EMPTY_OUTPUT = "empty_output"        # deterministic (gemini trust/parse) - NOT a 429

_TRANSIENT = {RATE_LIMIT, STALL_TIMEOUT, INTEGRITY_FALSE_POSITIVE}
_DETERMINISTIC = {AUTH_REQUIRED, QUOTA_EXCEEDED, EMPTY_OUTPUT}

# actions
RETRY = "retry"        # try the same contributor again
FALLBACK_MODEL = "fallback_model"
FALLBACK = "fallback"  # skip this contributor, swap in an available same-role one
SKIP = "skip"          # proceed without this contributor (panel still meets the floor)
ABORT = "abort"        # operator-actionable config problem; stop and surface it

LOW, HIGH = "low", "high"
_RETRY_BUDGET = {LOW: 0, HIGH: 1}  # consecutive retries allowed for a TRANSIENT failure


def decide(failure_type: str, *, attempt: int, priority: str = HIGH,
           fallback_available: bool = False, fallback_model_available: bool = False) -> dict:
    """Decide the action for a failed dispatch.

    attempt: 1-based count of attempts already made for this contributor.
    priority: LOW (chore) | HIGH (design/security/blocking).
    fallback_available: is a same-role substitute contributor available?
    Returns {action, reason}."""
    if priority not in (LOW, HIGH):
        raise ValueError(f"priority must be {LOW!r} or {HIGH!r}, got {priority!r}")

    failure_type = {
        EMPTY_OUTPUT: "parse",
        INTEGRITY_FALSE_POSITIVE: "integrity",
        STALL_TIMEOUT: "timeout",
    }.get(failure_type, failure_type)
    if failure_type in {"quota", QUOTA_EXCEEDED} and fallback_model_available:
        return _r(FALLBACK_MODEL, "quota exhausted; try the next model in the same seat")
    if failure_type == "auth":
        failure_type = AUTH_REQUIRED
    elif failure_type == "quota":
        failure_type = QUOTA_EXCEEDED
    elif failure_type in {"parse", "integrity", "crash", "timeout"}:
        return _r(SKIP, f"{failure_type} does not allow a model fallback or automatic retry")
    if failure_type == AUTH_REQUIRED:
        return _r(ABORT, "auth is a config problem (often a nested MCP) - retrying won't help; surface it")
    if failure_type in _DETERMINISTIC:  # quota / empty-output(trust)
        return _r(FALLBACK if fallback_available else SKIP,
                  f"{failure_type} is deterministic, not transient - don't burn the retry budget")
    if failure_type in _TRANSIENT:
        if attempt <= _RETRY_BUDGET.get(priority, 0):
            return _r(RETRY, f"transient {failure_type}; retry budget for {priority} priority")
        return _r(FALLBACK if fallback_available else SKIP,
                  f"transient {failure_type} exhausted the {priority}-priority retry budget")
    raise ValueError(f"unknown failure_type {failure_type!r}")


def _r(action: str, reason: str) -> dict:
    return {"action": action, "reason": reason}
