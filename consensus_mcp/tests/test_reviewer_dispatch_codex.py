"""Unit tests for tools/reviewer_dispatch_codex.py — the v1.1.x MCP wrapper.

The wrapper is THIN: it translates MCP-tool kwargs into argv for
_dispatch_codex.main, calls main in-process, captures stdout, and returns
the parsed JSON dict. Tests mock _dispatch_codex.main; no real codex calls.

Per project_phase_4_v1_1_x_mcp_wrapper_followup memory:
  - Tool name: reviewer.dispatch_codex
  - Tool params: goal_packet_path, iteration_dir, reviewer_id (optional),
    pass_id (optional), timeout_seconds (optional)
  - Return: same shape as helper's stdout JSON
  - Anti-scope: NO logic re-implementation; helper is source of truth
"""
from __future__ import annotations
import json as _json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _dispatch_codex  # noqa: E402
from consensus_mcp.tool_registry import ToolRegistry  # noqa: E402
from consensus_mcp.tools import reviewer_dispatch_codex  # noqa: E402


# ---- SCHEMA shape ----

def test_schema_name_is_reviewer_dispatch_codex():
    assert reviewer_dispatch_codex.SCHEMA["name"] == "reviewer.dispatch_codex"


def test_schema_required_fields_are_goal_packet_path_and_iteration_dir():
    required = set(reviewer_dispatch_codex.SCHEMA["input_schema"]["required"])
    assert required == {"goal_packet_path", "iteration_dir"}


def test_schema_optional_fields_present():
    props = reviewer_dispatch_codex.SCHEMA["input_schema"]["properties"]
    for opt in ("reviewer_id", "pass_id", "timeout_seconds", "review_target_path", "smoke"):
        assert opt in props, f"missing optional schema property: {opt}"


def test_schema_disallows_additional_properties():
    assert reviewer_dispatch_codex.SCHEMA["input_schema"]["additionalProperties"] is False


# ---- register() integration with the registry ----

def test_register_adds_tool_to_registry():
    registry = ToolRegistry()
    reviewer_dispatch_codex.register(registry)
    names = [t["name"] for t in registry.list_tools()]
    assert "reviewer.dispatch_codex" in names


def test_register_handler_is_callable():
    registry = ToolRegistry()
    reviewer_dispatch_codex.register(registry)
    handler = registry.get_handler("reviewer.dispatch_codex")
    assert callable(handler)


# ---- handle() argv translation ----

def _make_fake_main(captured_argv: list, output_dict: dict, return_code: int = 0):
    """Factory: returns a fake main() that captures argv and prints output_dict."""
    def fake_main(argv):
        captured_argv.append(list(argv))
        print(_json.dumps(output_dict))
        return return_code
    return fake_main


def test_handle_required_args_become_argv_flags(monkeypatch):
    captured = []
    fake = _make_fake_main(captured, {"ok": True, "pass_id": "p1"})
    monkeypatch.setattr(_dispatch_codex, "main", fake)

    reviewer_dispatch_codex.handle(
        goal_packet_path="path/to/goal.yaml",
        iteration_dir="path/to/iter",
    )

    argv = captured[0]
    assert "--goal-packet" in argv
    gp_idx = argv.index("--goal-packet")
    assert argv[gp_idx + 1] == "path/to/goal.yaml"
    assert "--iteration-dir" in argv
    id_idx = argv.index("--iteration-dir")
    assert argv[id_idx + 1] == "path/to/iter"


def test_handle_omits_optional_args_when_none(monkeypatch):
    captured = []
    monkeypatch.setattr(_dispatch_codex, "main", _make_fake_main(captured, {"ok": True}))

    reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )

    argv = captured[0]
    for flag in ("--reviewer-id", "--pass-id", "--timeout-seconds", "--review-target", "--smoke"):
        assert flag not in argv, f"{flag} should be omitted when not supplied"


def test_handle_includes_reviewer_id_when_set(monkeypatch):
    captured = []
    monkeypatch.setattr(_dispatch_codex, "main", _make_fake_main(captured, {"ok": True}))

    reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
        reviewer_id="codex-iter0009-1",
    )

    argv = captured[0]
    assert "--reviewer-id" in argv
    assert argv[argv.index("--reviewer-id") + 1] == "codex-iter0009-1"


def test_handle_includes_pass_id_when_set(monkeypatch):
    captured = []
    monkeypatch.setattr(_dispatch_codex, "main", _make_fake_main(captured, {"ok": True}))

    reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
        pass_id="codex-iter0009-1-pass1",
    )

    argv = captured[0]
    assert "--pass-id" in argv
    assert argv[argv.index("--pass-id") + 1] == "codex-iter0009-1-pass1"


def test_handle_includes_timeout_seconds_as_str_when_set(monkeypatch):
    captured = []
    monkeypatch.setattr(_dispatch_codex, "main", _make_fake_main(captured, {"ok": True}))

    reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
        timeout_seconds=300,
    )

    argv = captured[0]
    assert "--timeout-seconds" in argv
    assert argv[argv.index("--timeout-seconds") + 1] == "300"


def test_handle_includes_review_target_when_set(monkeypatch):
    captured = []
    monkeypatch.setattr(_dispatch_codex, "main", _make_fake_main(captured, {"ok": True}))

    reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
        review_target_path="path/to/diff.md",
    )

    argv = captured[0]
    assert "--review-target" in argv
    assert argv[argv.index("--review-target") + 1] == "path/to/diff.md"


def test_handle_includes_smoke_flag_when_true(monkeypatch):
    captured = []
    monkeypatch.setattr(_dispatch_codex, "main", _make_fake_main(captured, {"ok": True}))

    reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
        smoke=True,
    )

    argv = captured[0]
    assert "--smoke" in argv


def test_handle_omits_smoke_flag_when_false(monkeypatch):
    captured = []
    monkeypatch.setattr(_dispatch_codex, "main", _make_fake_main(captured, {"ok": True}))

    reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
        smoke=False,
    )

    argv = captured[0]
    assert "--smoke" not in argv


# ---- handle() return value ----

def test_handle_returns_parsed_json_on_success(monkeypatch):
    captured = []
    expected = {"ok": True, "pass_id": "codex-x-pass1", "packet_sha256": "abc123",
                "sealed_path": "path/to/sealed", "archive_sealed_path": "path/to/archive",
                "audit_event_id": "evt_001"}
    monkeypatch.setattr(_dispatch_codex, "main",
                        _make_fake_main(captured, expected, return_code=0))

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )

    assert result == expected


def test_handle_returns_failure_dict_when_main_returns_nonzero(monkeypatch):
    captured = []
    failure = {"ok": False, "error": "codex CLI not found", "error_type": "FileNotFoundError"}
    monkeypatch.setattr(_dispatch_codex, "main",
                        _make_fake_main(captured, failure, return_code=2))

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )

    assert result == failure
    assert result["ok"] is False


# ---- handle() does not leak helper stdout to its own caller's stdout ----

def test_handle_does_not_print_to_stdout(monkeypatch, capsys):
    captured = []
    monkeypatch.setattr(_dispatch_codex, "main",
                        _make_fake_main(captured, {"ok": True, "pass_id": "p"}))

    reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )

    out = capsys.readouterr().out
    assert out == "", (
        f"wrapper must capture helper stdout, not pass through to caller; got: {out!r}"
    )


# ---- handle() malformed-stdout hardening (Task #15) ----

def _make_fake_main_raw_stdout(raw: str, return_code: int = 0):
    """Factory: returns a fake main() that prints raw (non-JSON) stdout."""
    def fake_main(argv):
        # Use sys.stdout.write so we can emit literal-empty as well.
        sys.stdout.write(raw)
        return return_code
    return fake_main


def test_handle_wraps_malformed_stdout_as_error_dict(monkeypatch):
    monkeypatch.setattr(_dispatch_codex, "main",
                        _make_fake_main_raw_stdout("not json at all"))

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )

    assert result["ok"] is False
    assert result["error_type"] == "WrapperJsonDecodeError"
    assert "error" in result and isinstance(result["error"], str)
    assert result["raw_stdout_sample"].startswith("not json")


def test_handle_wraps_empty_stdout_as_error_dict(monkeypatch):
    monkeypatch.setattr(_dispatch_codex, "main",
                        _make_fake_main_raw_stdout(""))

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )

    assert result["ok"] is False
    assert result["error_type"] == "WrapperJsonDecodeError"
    assert "raw_stdout_sample" in result


def test_handle_truncates_long_stdout_in_sample(monkeypatch):
    long_garbage = "x" * 500
    monkeypatch.setattr(_dispatch_codex, "main",
                        _make_fake_main_raw_stdout(long_garbage))

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )

    assert result["ok"] is False
    assert result["error_type"] == "WrapperJsonDecodeError"
    assert len(result["raw_stdout_sample"]) == 200


# ---- iter-0012 F3: helper-exception leak hardening --------------------------

def test_handle_wraps_helper_runtime_exception_as_error_dict(monkeypatch):
    """If _dispatch_codex.main itself raises (not just bad stdout), handle()
    must NOT propagate; it returns a structured {ok: False, error_type, error}
    dict so direct MCP callers don't get JSON-RPC errors."""
    def boom_main(argv):
        raise RuntimeError("test boom")

    monkeypatch.setattr(_dispatch_codex, "main", boom_main)

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is False
    assert result["error_type"] == "RuntimeError"
    assert result["error"] == "test boom"


def test_handle_wraps_helper_import_error_as_error_dict(monkeypatch):
    """ImportError raised by helper main() (e.g., missing optional dep during
    pre-checks) is also captured, not propagated."""
    def importerror_main(argv):
        raise ImportError("missing yaml")

    monkeypatch.setattr(_dispatch_codex, "main", importerror_main)

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is False
    assert result["error_type"] == "ImportError"
    assert "missing yaml" in result["error"]


def test_handle_wraps_helper_value_error_as_error_dict(monkeypatch):
    """Generic exception type — helper raises ValueError, wrapper captures."""
    def value_main(argv):
        raise ValueError("bad arg")

    monkeypatch.setattr(_dispatch_codex, "main", value_main)

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is False
    assert result["error_type"] == "ValueError"


def test_handle_helper_exception_preserves_existing_jsondecodeerror_path(monkeypatch):
    """Verify the new outer except doesn't break the existing
    WrapperJsonDecodeError path (regression check on F3 fix)."""
    def fake_main(argv):
        sys.stdout.write("not json")
    monkeypatch.setattr(_dispatch_codex, "main", fake_main)

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is False
    assert result["error_type"] == "WrapperJsonDecodeError"
    assert result["raw_stdout_sample"].startswith("not json")


# ---- iter-0028 F3 (codex-rev-001): wrapper must capture helper rc and force
# ---- ok=False when rc != 0 AND stdout JSON claims ok=True. The prior wrapper
# ---- ignored the helper's return code entirely; a non-zero rc with stdout
# ---- containing `{"ok": true, ...}` would have been reported as success.

def test_handle_rc_zero_with_ok_true_passes_through(monkeypatch):
    """rc=0 + ok=True -> result is the parsed dict verbatim, no forcing."""
    captured = []
    success = {"ok": True, "pass_id": "p1", "packet_sha256": "abc"}
    monkeypatch.setattr(_dispatch_codex, "main",
                        _make_fake_main(captured, success, return_code=0))

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result == success
    assert "wrapper_forced_ok_false_due_to_nonzero_rc" not in result


def test_handle_rc_nonzero_with_ok_true_forces_ok_false(monkeypatch):
    """rc=1 + ok=True -> wrapper forces ok=False, adds marker key.

    This is the F3 defense-in-depth gap codex flagged: stdout JSON could
    claim success even when the helper exited non-zero. The wrapper must
    not trust stdout alone.
    """
    captured = []
    lying_success = {"ok": True, "pass_id": "p1", "packet_sha256": "abc"}
    monkeypatch.setattr(_dispatch_codex, "main",
                        _make_fake_main(captured, lying_success, return_code=1))

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is False
    assert result["wrapper_forced_ok_false_due_to_nonzero_rc"] is True
    # Original fields preserved alongside the forcing.
    assert result["pass_id"] == "p1"
    assert result["packet_sha256"] == "abc"


def test_handle_rc_nonzero_with_ok_false_passes_through(monkeypatch):
    """rc=1 + ok=False (helper-reported failure) -> verbatim passthrough.

    No forcing needed; the helper already correctly reported the failure.
    The marker key MUST NOT be added when ok was already False.
    """
    captured = []
    honest_failure = {"ok": False, "error": "codex timeout", "error_type": "CodexInvocationError"}
    monkeypatch.setattr(_dispatch_codex, "main",
                        _make_fake_main(captured, honest_failure, return_code=1))

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result == honest_failure
    assert "wrapper_forced_ok_false_due_to_nonzero_rc" not in result


def test_handle_rc_nonzero_with_malformed_stdout_wraps_as_before(monkeypatch):
    """rc=2 + non-JSON stdout -> WrapperJsonDecodeError path unchanged.

    The new rc-handling code only runs after stdout JSON parses successfully;
    the malformed-stdout error path is independent. Regression check.
    """
    monkeypatch.setattr(_dispatch_codex, "main",
                        _make_fake_main_raw_stdout("not json garbage", return_code=2))

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is False
    assert result["error_type"] == "WrapperJsonDecodeError"
    assert "raw_stdout_sample" in result
    # We don't add the marker on the malformed-stdout path; the wrapper
    # already reports failure via WrapperJsonDecodeError.
    assert "wrapper_forced_ok_false_due_to_nonzero_rc" not in result


def test_handle_rc_nonzero_with_missing_ok_key_forces_ok_false(monkeypatch):
    """rc=1 + stdout JSON missing ok key entirely -> wrapper inserts ok=False.

    Defense-in-depth: a malformed-success-shaped JSON (parses but lacks `ok`)
    plus non-zero rc must NOT be treated as ambiguous success.
    """
    captured = []
    weird = {"pass_id": "p1"}  # no `ok` key at all
    monkeypatch.setattr(_dispatch_codex, "main",
                        _make_fake_main(captured, weird, return_code=3))

    result = reviewer_dispatch_codex.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is False
    assert result["wrapper_forced_ok_false_due_to_nonzero_rc"] is True
