"""A dropped connection gets one automatic retry; nothing else does.

Live 2.5.6 consult: the OpenCode->DGX seat failed after 19 s with
"Transport: ECONNRESET" while the DGX was serving normally (three direct
probes succeeded). The panel recorded it as a crash and lost that reviewer
for the whole pass.
"""
from consensus_mcp.tests.test_consult_panel import (  # noqa: F401
    _panel_detail, configure, dispatchers, project, run)


def _seat(result, name):
    return next(row for row in result["seats"] if row["seat"] == name)


def test_network_reset_is_retried_once_and_seals(project, dispatchers):
    options, launches, _ = dispatchers
    options["codex"] = {"network": "once"}
    result = run(project, seats=["codex"])
    assert result["ok"] is True, _panel_detail(result)
    row = _seat(result, "codex")
    assert launches == ["codex", "codex"]
    assert row["status"] == "sealed" and row["failure_class"] is None
    first = row["network_retry_of"]
    assert "ECONNRESET" in first["error"] and first["pass_id"] != row["pass_id"]


def test_network_reset_twice_fails_as_network_without_a_third_try(project, dispatchers):
    options, launches, _ = dispatchers
    options["codex"] = {"network": "always"}
    options["kimi"] = {"packet": {"findings": [], "goal_satisfied": True}}
    result = run(project, seats=["codex", "kimi"])
    row = _seat(result, "codex")
    assert launches.count("codex") == 2
    assert row["status"] == "failed" and row["failure_class"] == "network", row
    assert "ECONNRESET" in row["error"]


def test_ordinary_crash_is_not_retried(project, dispatchers):
    options, launches, _ = dispatchers
    options["codex"] = {"failure": "crash"}
    options["kimi"] = {"packet": {"findings": [], "goal_satisfied": True}}
    result = run(project, seats=["codex", "kimi"])
    assert launches.count("codex") == 1
    assert _seat(result, "codex")["failure_class"] == "crash"
    assert "network_retry_of" not in _seat(result, "codex")


def test_no_retry_when_too_little_seat_time_remains(project, dispatchers):
    options, launches, _ = dispatchers
    options["codex"] = {"network": "once"}
    options["kimi"] = {"packet": {"findings": [], "goal_satisfied": True}}
    result = run(project, seats=["codex", "kimi"], timeout_seconds=50, deadline_seconds=55)
    assert launches.count("codex") == 1
    assert _seat(result, "codex")["failure_class"] == "network"


def test_qwen_output_budget_fits_a_full_review():
    # Live 2.5.6 consult: qwen4-max-preview spends part of its output on reasoning and
    # hit the old 16,384 cap mid-review ("HTTP response hit max_tokens"). The API
    # accepts up to 131,072; the seat now allows 65,536.
    from consensus_mcp._contributor_profiles import load_builtin_profiles
    assert load_builtin_profiles()["qwen"]["max_tokens"] >= 65536
