"""Tests for the external-CLI retry/fallback policy (sp-optimization B6)."""
from __future__ import annotations

import pytest

from consensus_mcp import _dispatch_retry_policy as rp


def test_auth_required_aborts_never_retries():
    d = rp.decide(rp.AUTH_REQUIRED, attempt=1, priority=rp.HIGH)
    assert d["action"] == rp.ABORT


@pytest.mark.parametrize("ftype", [rp.QUOTA_EXCEEDED])
def test_deterministic_failures_do_not_retry(ftype):
    # no fallback -> skip; never RETRY (don't burn the budget on a deterministic failure)
    assert rp.decide(ftype, attempt=1, priority=rp.HIGH)["action"] == rp.SKIP
    # with a fallback available -> swap in a substitute
    assert rp.decide(ftype, attempt=1, priority=rp.HIGH,
                     fallback_available=True)["action"] == rp.FALLBACK


def test_transient_high_priority_retries_once_then_falls_back():
    assert rp.decide(rp.RATE_LIMIT, attempt=1, priority=rp.HIGH)["action"] == rp.RETRY
    # budget exhausted on the 2nd attempt
    assert rp.decide(rp.RATE_LIMIT, attempt=2, priority=rp.HIGH)["action"] == rp.SKIP
    assert rp.decide(rp.RATE_LIMIT, attempt=2, priority=rp.HIGH,
                     fallback_available=True)["action"] == rp.FALLBACK


def test_transient_low_priority_does_not_retry():
    # a chore isn't worth a retry - skip immediately on the first transient failure
    assert rp.decide(rp.RATE_LIMIT, attempt=1, priority=rp.LOW)["action"] == rp.SKIP


@pytest.mark.parametrize("ftype", [rp.EMPTY_OUTPUT, rp.INTEGRITY_FALSE_POSITIVE, rp.STALL_TIMEOUT])
def test_legacy_failure_names_follow_typed_failure_policy(ftype):
    for attempt in (1, 2):
        assert rp.decide(ftype, attempt=attempt, priority=rp.HIGH,
                         fallback_available=True, fallback_model_available=True)["action"] == rp.SKIP


def test_unknown_failure_or_priority_raises():
    with pytest.raises(ValueError):
        rp.decide("meteor_strike", attempt=1, priority=rp.HIGH)
    with pytest.raises(ValueError):
        rp.decide(rp.RATE_LIMIT, attempt=1, priority="whenever")


def test_quota_prefers_same_seat_model_before_replacement_contributor():
    from consensus_mcp._dispatch_retry_policy import decide, FALLBACK_MODEL
    assert decide("quota", attempt=1, fallback_available=True,
                  fallback_model_available=True)["action"] == FALLBACK_MODEL
