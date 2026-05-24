"""Tests for the external-CLI retry/fallback policy (sp-optimization B6)."""
from __future__ import annotations

import pytest

from consensus_mcp import _dispatch_retry_policy as rp


def test_auth_required_aborts_never_retries():
    d = rp.decide(rp.AUTH_REQUIRED, attempt=1, priority=rp.HIGH)
    assert d["action"] == rp.ABORT


@pytest.mark.parametrize("ftype", [rp.QUOTA_EXCEEDED, rp.EMPTY_OUTPUT])
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
    # a chore isn't worth a retry — skip immediately on the first transient failure
    assert rp.decide(rp.RATE_LIMIT, attempt=1, priority=rp.LOW)["action"] == rp.SKIP


def test_integrity_false_positive_is_transient():
    assert rp.decide(rp.INTEGRITY_FALSE_POSITIVE, attempt=1, priority=rp.HIGH)["action"] == rp.RETRY


def test_unknown_failure_or_priority_raises():
    with pytest.raises(ValueError):
        rp.decide("meteor_strike", attempt=1, priority=rp.HIGH)
    with pytest.raises(ValueError):
        rp.decide(rp.RATE_LIMIT, attempt=1, priority="whenever")
