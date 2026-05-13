"""Unit tests for consensus_mcp._dispatch_gemini.

Focused on gemini-specific behavior:
  - _extract_json_from_text (free-form output → JSON substring)
  - _parse_gemini_output (validates JSON shape; gemini-rev-N IDs; patch_proposal MUST be null)
  - _invoke_gemini_with_retry (validator-retry on first-pass parse fail)
  - _resolve_gemini_bin (Windows-aware lookup)
  - MCP wrapper (argv translation, rc reconciliation)

Does NOT re-test base helpers (covered by test_dispatch_codex.py); does NOT
run real gemini CLI (smoke env-gated to CONSENSUS_MCP_RUN_REAL_GEMINI_SMOKE=1).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Allow imports of consensus_mcp from the repo root without install.
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consensus_mcp import _dispatch_gemini  # noqa: E402
from consensus_mcp.tools import reviewer_dispatch_gemini  # noqa: E402


# ---------- _extract_json_from_text ----------

def test_extract_pure_json_passes_through():
    text = '{"findings": [], "goal_satisfied": true}'
    assert _dispatch_gemini._extract_json_from_text(text) == text


def test_extract_strips_leading_trailing_whitespace():
    text = '  \n  {"k": 1}\n   '
    assert _dispatch_gemini._extract_json_from_text(text) == '{"k": 1}'


def test_extract_handles_json_markdown_fence():
    text = 'Sure thing:\n```json\n{"findings": []}\n```\nhope that helps'
    out = _dispatch_gemini._extract_json_from_text(text)
    assert out == '{"findings": []}'


def test_extract_handles_bare_markdown_fence():
    text = '```\n{"k": "v"}\n```'
    out = _dispatch_gemini._extract_json_from_text(text)
    assert out == '{"k": "v"}'


def test_extract_falls_back_to_brace_scan():
    text = 'Here is what I found: {"findings": [], "goal_satisfied": true}. Done.'
    out = _dispatch_gemini._extract_json_from_text(text)
    assert out == '{"findings": [], "goal_satisfied": true}'


def test_extract_returns_raw_when_no_braces():
    text = "no json here at all"
    assert _dispatch_gemini._extract_json_from_text(text) == text


# ---------- _parse_gemini_output ----------

def _minimal_valid():
    return {
        "findings": [],
        "goal_satisfied": True,
        "goal_satisfied_rationale": "No issues found.",
        "blocking_objections": [],
    }


def test_parse_minimal_valid():
    parsed = _dispatch_gemini._parse_gemini_output(json.dumps(_minimal_valid()))
    assert parsed["goal_satisfied"] is True
    assert parsed["findings"] == []


def test_parse_rejects_invalid_json():
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError, match="not valid JSON"):
        _dispatch_gemini._parse_gemini_output("{not json")


def test_parse_rejects_non_object_root():
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError, match="must be an object"):
        _dispatch_gemini._parse_gemini_output("[1,2,3]")


def test_parse_rejects_unknown_top_level_key():
    payload = _minimal_valid()
    payload["unexpected"] = "field"
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError, match="unexpected top-level"):
        _dispatch_gemini._parse_gemini_output(json.dumps(payload))


def test_parse_rejects_missing_required():
    payload = _minimal_valid()
    del payload["goal_satisfied"]
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError, match="missing required"):
        _dispatch_gemini._parse_gemini_output(json.dumps(payload))


def test_parse_rejects_finding_with_codex_id_prefix():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "codex-rev-001",
        "severity": "low",
        "summary": "wrong adapter prefix",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "use gemini-rev-N",
    }]
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError, match=r"\^gemini-rev"):
        _dispatch_gemini._parse_gemini_output(json.dumps(payload))


def test_parse_accepts_gemini_rev_id():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "gemini-rev-001",
        "severity": "low",
        "summary": "minor stylistic suggestion",
        "citation": "consensus_mcp/foo.py:1",
        "risk": "very low",
        "recommendation": "consider renaming",
    }]
    parsed = _dispatch_gemini._parse_gemini_output(json.dumps(payload))
    assert parsed["findings"][0]["id"] == "gemini-rev-001"


def test_parse_rejects_non_null_patch_proposal():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "gemini-rev-001",
        "severity": "low",
        "summary": "x",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "rec",
        "patch_proposal": {"patch_id": "gemini-rev-001-patch"},
    }]
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError, match="patch_proposal must be null"):
        _dispatch_gemini._parse_gemini_output(json.dumps(payload))


def test_parse_allows_null_patch_proposal():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "gemini-rev-001",
        "severity": "low",
        "summary": "x",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "rec",
        "patch_proposal": None,
        "patch_not_proposed_reason": None,
    }]
    parsed = _dispatch_gemini._parse_gemini_output(json.dumps(payload))
    assert parsed["findings"][0]["patch_proposal"] is None


def test_parse_blocking_invariant_violated():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "gemini-rev-001",
        "severity": "blocking",
        "summary": "x",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "rec",
    }]
    # blocking_objections is empty but a blocking-severity finding exists.
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError, match="blocking_objections invariant"):
        _dispatch_gemini._parse_gemini_output(json.dumps(payload))


def test_parse_rejects_goal_satisfied_true_with_blocking_objections():
    """codex-rev-002 round-1 fix: goal_satisfied=true is incoherent with any blocking finding."""
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "gemini-rev-001",
        "severity": "blocking",
        "summary": "x",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "rec",
    }]
    payload["blocking_objections"] = ["gemini-rev-001"]
    payload["goal_satisfied"] = True  # incoherent
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError, match="incoherent"):
        _dispatch_gemini._parse_gemini_output(json.dumps(payload))


def test_parse_blocking_invariant_satisfied():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "gemini-rev-001",
        "severity": "critical",
        "summary": "x",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "rec",
    }]
    payload["blocking_objections"] = ["gemini-rev-001"]
    payload["goal_satisfied"] = False
    parsed = _dispatch_gemini._parse_gemini_output(json.dumps(payload))
    assert parsed["blocking_objections"] == ["gemini-rev-001"]


def test_parse_extracts_from_wrapped_output():
    # Simulates gemini emitting prose around the JSON.
    payload_str = json.dumps(_minimal_valid())
    wrapped = f"Sure, here's the review:\n```json\n{payload_str}\n```\nDone."
    parsed = _dispatch_gemini._parse_gemini_output(wrapped)
    assert parsed["goal_satisfied"] is True


# ---------- _invoke_gemini_with_retry ----------

class _FakeInvokeFactory:
    """Captures the sequence of prompts _invoke_gemini receives + returns scripted outputs."""
    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def __call__(self, *, prompt, **_kwargs):
        self.prompts.append(prompt)
        return self.outputs.pop(0)


def test_retry_succeeds_on_first_attempt(monkeypatch):
    factory = _FakeInvokeFactory([json.dumps(_minimal_valid())])
    monkeypatch.setattr(_dispatch_gemini, "_invoke_gemini", factory)
    raw, parsed = _dispatch_gemini._invoke_gemini_with_retry(
        prompt="initial",
        gemini_bin="gemini",
        model="gemini-2.5-pro",
        timeout_seconds=60,
        repo_root=Path("."),
    )
    assert parsed["goal_satisfied"] is True
    assert len(factory.prompts) == 1
    assert factory.prompts[0] == "initial"


def test_retry_succeeds_on_second_attempt(monkeypatch):
    """First call returns junk, second returns valid JSON. Retry succeeds."""
    factory = _FakeInvokeFactory([
        "not json at all{",                       # parse will fail
        json.dumps(_minimal_valid()),             # valid
    ])
    monkeypatch.setattr(_dispatch_gemini, "_invoke_gemini", factory)
    raw, parsed = _dispatch_gemini._invoke_gemini_with_retry(
        prompt="initial",
        gemini_bin="gemini",
        model="gemini-2.5-pro",
        timeout_seconds=60,
        repo_root=Path("."),
    )
    assert parsed["goal_satisfied"] is True
    assert len(factory.prompts) == 2
    # Retry prompt should include the parse error feedback.
    assert "Retry" in factory.prompts[1]
    assert "Parse error" in factory.prompts[1]


def test_retry_fails_when_both_attempts_invalid(monkeypatch):
    factory = _FakeInvokeFactory(["garbage 1", "garbage 2"])
    monkeypatch.setattr(_dispatch_gemini, "_invoke_gemini", factory)
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError):
        _dispatch_gemini._invoke_gemini_with_retry(
            prompt="initial",
            gemini_bin="gemini",
            model="gemini-2.5-pro",
            timeout_seconds=60,
            repo_root=Path("."),
        )
    assert len(factory.prompts) == 2  # exactly one retry; no further attempts


# ---------- _resolve_gemini_bin ----------

def test_resolve_returns_input_when_path_separator_present():
    # Caller-supplied full path with separator should pass through.
    if sys.platform == "win32":
        # On Windows the function checks for App Alias zero-byte stubs but
        # otherwise returns the input. Pass a non-existent path that has a
        # separator; the function returns it unchanged.
        out = _dispatch_gemini._resolve_gemini_bin(r"C:\nonexistent\gemini.exe")
        assert out == r"C:\nonexistent\gemini.exe"
    else:
        out = _dispatch_gemini._resolve_gemini_bin("/nonexistent/gemini")
        assert out == "/nonexistent/gemini"


def test_resolve_returns_input_when_which_misses(monkeypatch):
    monkeypatch.setattr(_dispatch_gemini.shutil, "which", lambda _: None)
    out = _dispatch_gemini._resolve_gemini_bin("definitely-not-gemini")
    assert out == "definitely-not-gemini"


# ---------- reviewer_dispatch_gemini wrapper ----------

def test_wrapper_argv_required_args_only():
    argv = reviewer_dispatch_gemini._build_argv(
        goal_packet_path="goal.yaml",
        iteration_dir="iter-dir",
        reviewer_id=None,
        pass_id=None,
        timeout_seconds=None,
        review_target_path=None,
        model=None,
        smoke=None,
    )
    assert argv == ["--goal-packet", "goal.yaml", "--iteration-dir", "iter-dir"]


def test_wrapper_argv_all_args():
    argv = reviewer_dispatch_gemini._build_argv(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
        reviewer_id="gemini-x-1",
        pass_id="gemini-x-1-pass1",
        timeout_seconds=600,
        review_target_path="rp.yaml",
        model="gemini-2.5-pro",
        smoke=True,
    )
    assert "--reviewer-id" in argv and "gemini-x-1" in argv
    assert "--pass-id" in argv and "gemini-x-1-pass1" in argv
    assert "--timeout-seconds" in argv and "600" in argv
    assert "--review-target" in argv and "rp.yaml" in argv
    assert "--model" in argv and "gemini-2.5-pro" in argv
    assert "--smoke" in argv


def test_wrapper_argv_smoke_false_omits_flag():
    argv = reviewer_dispatch_gemini._build_argv(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
        reviewer_id=None,
        pass_id=None,
        timeout_seconds=None,
        review_target_path=None,
        model=None,
        smoke=False,
    )
    assert "--smoke" not in argv


def test_wrapper_handle_helper_exception_returns_error_dict(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated helper crash")
    monkeypatch.setattr(_dispatch_gemini, "main", boom)
    result = reviewer_dispatch_gemini.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is False
    assert result["error_type"] == "RuntimeError"
    assert "simulated helper crash" in result["error"]


def test_wrapper_handle_rc_nonzero_forces_ok_false(monkeypatch):
    """If main() returns non-zero but stdout JSON claims ok=True, wrapper forces ok=False."""
    def fake_main(_argv):
        # Print a payload that claims ok=True
        print('{"ok": true, "pass_id": "x"}')
        return 1
    monkeypatch.setattr(_dispatch_gemini, "main", fake_main)
    result = reviewer_dispatch_gemini.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is False
    assert result.get("wrapper_forced_ok_false_due_to_nonzero_rc") is True


def test_wrapper_handle_rc_zero_passes_through(monkeypatch):
    def fake_main(_argv):
        print('{"ok": true, "pass_id": "x"}')
        return 0
    monkeypatch.setattr(_dispatch_gemini, "main", fake_main)
    result = reviewer_dispatch_gemini.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is True
    assert "wrapper_forced_ok_false_due_to_nonzero_rc" not in result


def test_parse_rejects_empty_summary():
    """codex-rev-002 round-2 fix: summary must be non-empty (matches schema minLength=1)."""
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "gemini-rev-001",
        "severity": "low",
        "summary": "",  # empty
        "citation": "x:1",
        "risk": "r",
        "recommendation": "rec",
    }]
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError, match="non-empty"):
        _dispatch_gemini._parse_gemini_output(json.dumps(payload))


def test_parse_rejects_empty_rationale():
    """codex-rev-002 round-2 fix: empty/whitespace rationale must be rejected."""
    payload = _minimal_valid()
    payload["goal_satisfied_rationale"] = ""
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError, match="non-empty"):
        _dispatch_gemini._parse_gemini_output(json.dumps(payload))
    payload["goal_satisfied_rationale"] = "   \n\t  "
    with pytest.raises(_dispatch_gemini.GeminiOutputParseError, match="non-empty"):
        _dispatch_gemini._parse_gemini_output(json.dumps(payload))


def test_wrapper_catches_systemexit(monkeypatch):
    """codex-rev-001 round-2 fix: SystemExit (e.g., bad argparse input) must
    not escape and kill the MCP server."""
    def fake_main(_argv):
        raise SystemExit(2)
    monkeypatch.setattr(_dispatch_gemini, "main", fake_main)
    result = reviewer_dispatch_gemini.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is False
    assert result["error_type"] == "ArgparseSystemExit"


def test_wrapper_handle_malformed_stdout(monkeypatch):
    def fake_main(_argv):
        print("not json")
        return 0
    monkeypatch.setattr(_dispatch_gemini, "main", fake_main)
    result = reviewer_dispatch_gemini.handle(
        goal_packet_path="g.yaml",
        iteration_dir="iter",
    )
    assert result["ok"] is False
    assert result["error_type"] == "WrapperJsonDecodeError"


# ---------- registry registration ----------

def test_registry_can_register_gemini_tool():
    class FakeRegistry:
        def __init__(self):
            self.registered: list[tuple] = []
        def register(self, name, schema, handler):
            self.registered.append((name, schema, handler))

    reg = FakeRegistry()
    reviewer_dispatch_gemini.register(reg)
    assert len(reg.registered) == 1
    name, schema, handler = reg.registered[0]
    assert name == "reviewer.dispatch_gemini"
    assert schema["name"] == "reviewer.dispatch_gemini"
    assert handler is reviewer_dispatch_gemini.handle


def test_schema_required_args():
    assert reviewer_dispatch_gemini.SCHEMA["input_schema"]["required"] == ["goal_packet_path", "iteration_dir"]
    props = reviewer_dispatch_gemini.SCHEMA["input_schema"]["properties"]
    assert "model" in props  # gemini-specific addition vs codex


def test_server_registers_gemini():
    """Verify server.py wires up the gemini tool alongside codex."""
    from consensus_mcp.server import registry
    names = [t["name"] for t in registry.list_tools()]
    assert "reviewer.dispatch_gemini" in names
