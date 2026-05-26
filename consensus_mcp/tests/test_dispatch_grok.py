"""Unit tests for consensus_mcp._dispatch_grok.

Focused on grok-specific behavior:
  - _check_grok_auth (auth pre-flight raises when ~/.grok/auth.json absent)
  - _build_grok_cmd (CLI flag shape: --prompt-file, disabled tools, --cwd, --model)
  - _write_per_pass_prompt (per-pass filename embeds pass_id; content sha256)
  - _extract_json_from_text (free-form → JSON substring)
  - _parse_grok_output (validates JSON shape; grok-rev-N ID pattern; patch_proposal MUST be null)
  - main() smoke env gate (--smoke without env var → refusal)
  - main() auth pre-flight + error code path

Does NOT run the real grok CLI (smoke env-gated to
CONSENSUS_MCP_RUN_REAL_GROK_SMOKE=1, in test_dispatch_grok_smoke.py).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consensus_mcp import _dispatch_grok  # noqa: E402


# ---------- _check_grok_auth ----------

def test_check_grok_auth_raises_when_auth_file_missing(monkeypatch, tmp_path):
    """G4 acceptance gate: missing ~/.grok/auth.json fails BEFORE invoking CLI."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    with pytest.raises(_dispatch_grok.GrokAuthRequiredError, match="grok login"):
        _dispatch_grok._check_grok_auth()


def test_check_grok_auth_passes_when_auth_file_present(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    grok_dir = tmp_path / ".grok"
    grok_dir.mkdir()
    (grok_dir / "auth.json").write_text('{"token": "test"}', encoding="utf-8")
    # No exception → pre-flight passes.
    _dispatch_grok._check_grok_auth()


# ---------- _build_grok_cmd ----------

def test_build_grok_cmd_uses_prompt_file_not_inline(tmp_path):
    """G5: per-pass prompt file (NOT inline --single)."""
    prompt_file = tmp_path / "grok-prompt-x.txt"
    iter_dir = tmp_path
    cmd = _dispatch_grok._build_grok_cmd("grok", prompt_file, iter_dir, model=None)
    assert "--prompt-file" in cmd
    assert str(prompt_file) in cmd
    assert "--single" not in cmd  # we never use --single in real dispatch
    assert "--prompt-json" not in cmd


def test_build_grok_cmd_contains_full_disabled_tool_set(tmp_path):
    """G5+G6: every disable-flag in the dispatch_provenance.disabled_tools set
    must be on the actual CLI command."""
    cmd = _dispatch_grok._build_grok_cmd(
        "grok", tmp_path / "p.txt", tmp_path, model=None,
    )
    # The root-cause-independent safeguard set.
    assert "--no-memory" in cmd
    assert "--no-plan" in cmd
    assert "--no-subagents" in cmd
    assert "--disable-web-search" in cmd
    assert "--max-turns" in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "1"
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"


def test_build_grok_cmd_passes_cwd(tmp_path):
    cmd = _dispatch_grok._build_grok_cmd(
        "grok", tmp_path / "p.txt", tmp_path, model=None,
    )
    assert "--cwd" in cmd
    assert cmd[cmd.index("--cwd") + 1] == str(tmp_path)


def test_build_grok_cmd_includes_model_when_set(tmp_path):
    cmd = _dispatch_grok._build_grok_cmd(
        "grok", tmp_path / "p.txt", tmp_path, model="grok-4-fast",
    )
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "grok-4-fast"


def test_build_grok_cmd_omits_model_when_none(tmp_path):
    """Per converged plan D3: --model is optional; let grok roll forward without
    dispatcher releases when operator doesn't pin a model."""
    cmd = _dispatch_grok._build_grok_cmd(
        "grok", tmp_path / "p.txt", tmp_path, model=None,
    )
    assert "--model" not in cmd


def test_build_grok_cmd_output_format_plain(tmp_path):
    cmd = _dispatch_grok._build_grok_cmd(
        "grok", tmp_path / "p.txt", tmp_path, model=None,
    )
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "plain"


# ---------- _write_per_pass_prompt ----------

def test_write_per_pass_prompt_embeds_pass_id_in_filename(tmp_path):
    """Codex D3 refinement: per-pass filename (no singleton collisions)."""
    path = _dispatch_grok._write_per_pass_prompt(
        "hello", tmp_path, pass_id="grok-iter-2026-05-26-pass1",
    )
    assert "grok-iter-2026-05-26-pass1" in path.name
    assert path.name.startswith("grok-prompt-")
    assert path.read_text(encoding="utf-8") == "hello"


def test_write_per_pass_prompt_sanitizes_unsafe_chars(tmp_path):
    """Defense-in-depth against operator-passed weird pass_id: even with
    traversal-shaped input, the file lands INSIDE iter_dir (not escaping)."""
    path = _dispatch_grok._write_per_pass_prompt(
        "x", tmp_path, pass_id="../../../etc/passwd",
    )
    # `..` chars in the FILENAME portion are harmless (Path doesn't traverse
    # within a filename); the safety we need is "file confined to iter_dir".
    assert path.parent.resolve() == tmp_path.resolve()
    assert "/" not in path.name  # path separators stripped
    assert path.read_text(encoding="utf-8") == "x"


# ---------- _extract_json_from_text (mirrors gemini's extractor) ----------

def test_extract_pure_json_passes_through():
    text = '{"findings": [], "goal_satisfied": true}'
    assert _dispatch_grok._extract_json_from_text(text) == text


def test_extract_strips_leading_trailing_whitespace():
    text = '  \n  {"k": 1}\n   '
    assert _dispatch_grok._extract_json_from_text(text) == '{"k": 1}'


def test_extract_handles_json_markdown_fence():
    text = 'Sure thing:\n```json\n{"findings": []}\n```\nhope that helps'
    out = _dispatch_grok._extract_json_from_text(text)
    assert out == '{"findings": []}'


def test_extract_handles_bare_markdown_fence():
    text = '```\n{"k": "v"}\n```'
    out = _dispatch_grok._extract_json_from_text(text)
    assert out == '{"k": "v"}'


def test_extract_falls_back_to_brace_scan():
    text = 'Here is what I found: {"findings": [], "goal_satisfied": true}. Done.'
    out = _dispatch_grok._extract_json_from_text(text)
    assert out == '{"findings": [], "goal_satisfied": true}'


def test_extract_returns_raw_when_no_braces():
    text = "no json here at all"
    assert _dispatch_grok._extract_json_from_text(text) == text


# ---------- _parse_grok_output ----------

def _minimal_valid():
    return {
        "findings": [],
        "goal_satisfied": True,
        "goal_satisfied_rationale": "No issues found.",
        "blocking_objections": [],
    }


def test_parse_minimal_valid():
    parsed = _dispatch_grok._parse_grok_output(json.dumps(_minimal_valid()))
    assert parsed["goal_satisfied"] is True
    assert parsed["findings"] == []


def test_parse_rejects_invalid_json():
    with pytest.raises(_dispatch_grok.GrokOutputParseError, match="not valid JSON"):
        _dispatch_grok._parse_grok_output("{not json")


def test_parse_rejects_array_root():
    with pytest.raises(_dispatch_grok.GrokOutputParseError, match="must be an object"):
        _dispatch_grok._parse_grok_output("[]")


def test_parse_rejects_unknown_top_level_key():
    data = _minimal_valid()
    data["extra_field"] = "x"
    with pytest.raises(_dispatch_grok.GrokOutputParseError, match="unexpected top-level keys"):
        _dispatch_grok._parse_grok_output(json.dumps(data))


def test_parse_requires_goal_satisfied_rationale_non_empty():
    data = _minimal_valid()
    data["goal_satisfied_rationale"] = "   "
    with pytest.raises(_dispatch_grok.GrokOutputParseError, match="non-empty"):
        _dispatch_grok._parse_grok_output(json.dumps(data))


def test_parse_finding_id_must_match_grok_rev_pattern():
    """The id pattern is grok-rev-NNN (not gemini-rev / codex-rev)."""
    data = _minimal_valid()
    data["findings"] = [{
        "id": "gemini-rev-001",  # WRONG family
        "severity": "low", "summary": "x", "citation": "f:1",
        "risk": "r", "recommendation": "r",
    }]
    with pytest.raises(_dispatch_grok.GrokOutputParseError, match="grok-rev"):
        _dispatch_grok._parse_grok_output(json.dumps(data))


def test_parse_finding_id_accepts_grok_rev_pattern():
    data = _minimal_valid()
    data["findings"] = [{
        "id": "grok-rev-001",
        "severity": "low", "summary": "x", "citation": "f:1",
        "risk": "r", "recommendation": "r",
    }]
    data["goal_satisfied"] = False  # non-blocking findings → still coherent
    parsed = _dispatch_grok._parse_grok_output(json.dumps(data))
    assert parsed["findings"][0]["id"] == "grok-rev-001"


def test_parse_patch_proposal_must_be_null_in_v1310():
    """Grok is review-only (gemini/kimi pattern); patch_proposal MUST be null."""
    data = _minimal_valid()
    data["findings"] = [{
        "id": "grok-rev-001",
        "severity": "low", "summary": "x", "citation": "f:1",
        "risk": "r", "recommendation": "r",
        "patch_proposal": {"some": "patch"},  # not null
    }]
    with pytest.raises(_dispatch_grok.GrokOutputParseError, match="patch_proposal must be null"):
        _dispatch_grok._parse_grok_output(json.dumps(data))


def test_parse_blocking_objections_invariant():
    """Set of blocking_objections must equal set of {f.id : severity ∈ blocking/critical}."""
    data = _minimal_valid()
    data["findings"] = [{
        "id": "grok-rev-001",
        "severity": "blocking", "summary": "x", "citation": "f:1",
        "risk": "r", "recommendation": "r",
    }]
    # Missing from blocking_objections
    data["blocking_objections"] = []
    data["goal_satisfied"] = False
    with pytest.raises(_dispatch_grok.GrokOutputParseError, match="blocking_objections invariant"):
        _dispatch_grok._parse_grok_output(json.dumps(data))


def test_parse_goal_satisfied_true_incoherent_with_blocking():
    data = _minimal_valid()
    data["findings"] = [{
        "id": "grok-rev-001",
        "severity": "blocking", "summary": "x", "citation": "f:1",
        "risk": "r", "recommendation": "r",
    }]
    data["blocking_objections"] = ["grok-rev-001"]
    data["goal_satisfied"] = True  # incoherent
    with pytest.raises(_dispatch_grok.GrokOutputParseError, match="incoherent"):
        _dispatch_grok._parse_grok_output(json.dumps(data))


# ---------- main() smoke env gate ----------

def test_main_smoke_refuses_without_env_var(monkeypatch, tmp_path):
    """G8 gate: --smoke requires CONSENSUS_MCP_RUN_REAL_GROK_SMOKE=1.

    Point CONSENSUS_MCP_REPO_ROOT at the actual repo root (the real repo
    has the required _REPO_ROOT_MARKERS: consensus-state, consensus_mcp,
    consensus_mcp/validators). The iteration dir lives inside it so the
    smoke gate (which is checked AFTER repo resolution) is the first
    refusal we hit.
    """
    monkeypatch.delenv("CONSENSUS_MCP_RUN_REAL_GROK_SMOKE", raising=False)
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(ROOT))
    iter_dir = ROOT / "consensus-state" / "active" / "iter-grok-smoke-gate-test"
    iter_dir.mkdir(parents=True, exist_ok=True)
    gp = iter_dir / "goal_packet.yaml"
    gp.write_text(
        "schema_version: 1\npilot_id: smoke-gate-test\n"
        "goal:\n  summary: t\n  desired_end_state: t\n  non_goals: []\n"
        "allowed_files: []\nacceptance_gates:\n  - id: A\n    description: x\n    check: 'true'\n"
        "stop_conditions: []\noperator_escalation_triggers: []\n"
        "authorization:\n  authorized_by: t\n  authorized_at_utc: '2026-05-26T00:00:00Z'\n",
        encoding="utf-8",
    )

    import io, contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _dispatch_grok.main([
                "--goal-packet", str(gp),
                "--iteration-dir", str(iter_dir),
                "--smoke",
            ])
        assert rc == 3
        result = json.loads(buf.getvalue().strip())
        assert result["ok"] is False
        assert result["error_type"] == "smoke_env_gate"
    finally:
        # Cleanup the iter dir we created in the real repo.
        import shutil
        shutil.rmtree(iter_dir, ignore_errors=True)


# ---------- _GROK_DISABLED_TOOLS provenance contract ----------

def test_grok_disabled_tools_list_is_stable():
    """Codex D8: the disabled-tools list is logged in provenance + is a
    root-cause-independent safeguard. Lock the exact set so a future bump
    is visible in tests."""
    assert _dispatch_grok._GROK_DISABLED_TOOLS == (
        "--no-memory",
        "--no-plan",
        "--no-subagents",
        "--disable-web-search",
        "--max-turns", "1",
        "--permission-mode", "dontAsk",
    )
