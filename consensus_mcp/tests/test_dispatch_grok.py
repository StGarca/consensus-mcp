"""Unit tests for consensus_mcp._dispatch_grok.

Focused on grok-specific behavior:
  - _check_grok_auth (auth pre-flight raises when ~/.grok/auth.json absent)
  - _build_grok_cmd (CLI flag shape: inline -p, disabled tools, --cwd /tmp, --model)
  - _write_per_pass_prompt (per-pass filename embeds pass_id; content sha256)
  - _extract_json_from_text (free-form -> JSON substring)
  - _parse_grok_output (validates JSON shape; grok-rev-N ID pattern; patch_proposal MUST be null)
  - main() smoke env gate (--smoke without env var -> refusal)
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
    # No exception -> pre-flight passes.
    _dispatch_grok._check_grok_auth()


# ---------- _build_grok_cmd ----------

def test_build_grok_cmd_uses_inline_p_not_prompt_file():
    """iter-0045 shape: inline -p (NOT --prompt-file)."""
    cmd = _dispatch_grok._build_grok_cmd("grok", "hello prompt", model=None)
    assert "-p" in cmd
    assert "hello prompt" in cmd
    assert cmd[cmd.index("-p") + 1] == "hello prompt"
    assert "--prompt-file" not in cmd
    assert "--single" not in cmd
    assert "--prompt-json" not in cmd


def test_build_grok_cmd_contains_minimal_disabled_tool_set():
    """iter-0045 minimal shape: only --no-memory + --disable-web-search.
    The dropped flags (--no-plan, --no-subagents, --max-turns,
    --permission-mode) are what caused the prior stall behavior; they are
    forbidden by dispatch-canon-validator.GROK_FORBIDDEN_FLAGS."""
    cmd = _dispatch_grok._build_grok_cmd("grok", "p", model=None)
    assert "--no-memory" in cmd
    assert "--disable-web-search" in cmd
    # Stall-causing flags MUST NOT be present (canon-aligned).
    assert "--no-plan" not in cmd
    assert "--no-subagents" not in cmd
    assert "--max-turns" not in cmd
    assert "--permission-mode" not in cmd
    assert "--prompt-file" not in cmd


def test_build_grok_cmd_cwd_defaults_to_tmp():
    """Default run_cwd is /tmp (canon fallback). The dispatcher overrides
    this per-pass with a fresh empty temp dir (DEFECT 2 fix: grok's
    recursive watcher scans the --cwd dir, so an empty dir avoids the
    /tmp systemd-private PermissionDenied noise - confirmed by grok)."""
    cmd = _dispatch_grok._build_grok_cmd("grok", "p", model=None)
    assert "--cwd" in cmd
    assert cmd[cmd.index("--cwd") + 1] == "/tmp"


def test_build_grok_cmd_uses_given_run_cwd():
    """When the dispatcher passes a fresh per-pass temp dir, it becomes the
    --cwd value (Option A - grok ruled that its recursive watcher keys off
    the --cwd FLAG, not the OS process cwd, so an empty --cwd dir is what
    eliminates the systemd-private watcher noise)."""
    cmd = _dispatch_grok._build_grok_cmd(
        "grok", "p", model=None, run_cwd="/tmp/grok-run-abc123",
    )
    assert cmd[cmd.index("--cwd") + 1] == "/tmp/grok-run-abc123"


def test_build_grok_cmd_includes_model_when_set():
    cmd = _dispatch_grok._build_grok_cmd("grok", "p", model="grok-4-fast")
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "grok-4-fast"


def test_build_grok_cmd_includes_effort():
    cmd = _dispatch_grok._build_grok_cmd(
        "grok", "p", model="grok-4.5", effort="max",
    )
    assert cmd[cmd.index("--effort") + 1] == "max"


def test_build_grok_cmd_omits_model_when_none():
    """Per converged plan D3: --model is optional; let grok roll forward without
    dispatcher releases when operator doesn't pin a model."""
    cmd = _dispatch_grok._build_grok_cmd("grok", "p", model=None)
    assert "--model" not in cmd


def test_build_grok_cmd_output_format_streaming_json():
    """Streaming-json (NOT plain) is required: plain buffers all stdout until
    the answer is ready, so the silence watchdog killed grok for being silent
    while grok was silent by design (DEFECT 1). Streaming emits thought/text
    event lines continuously, keeping the watchdog fed. See
    docs/grok-dispatch-streaming-watchdog-fix.md."""
    cmd = _dispatch_grok._build_grok_cmd("grok", "p", model=None)
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "streaming-json"


def test_build_grok_cmd_oversized_prompt_uses_prompt_file(tmp_path):
    """Cold-start consult finding: a prompt larger than the inline argv ceiling is
    routed through --prompt-file (the per-pass file) instead of inline -p, avoiding
    the opaque E2BIG / 'Argument list too long' crash on a big review-packet."""
    big = "x" * (_dispatch_grok._GROK_INLINE_PROMPT_MAX_BYTES + 1)
    pf = tmp_path / "grok-prompt-passX.txt"
    pf.write_text(big, encoding="utf-8")
    cmd = _dispatch_grok._build_grok_cmd("grok", big, model=None, prompt_file=pf)
    assert "--prompt-file" in cmd
    assert cmd[cmd.index("--prompt-file") + 1] == str(pf)
    assert "-p" not in cmd                      # the huge prompt is NOT inline
    assert big not in cmd
    # streaming-json + disabled tools still present on the file path.
    assert cmd[cmd.index("--output-format") + 1] == "streaming-json"
    assert "--no-memory" in cmd


def test_inline_prompt_ceiling_is_platform_safe():
    """kimi-rev-002: the inline-prompt ceiling must clear the SMALLEST platform
    cmdline limit. On Windows (CreateProcessW ~32767 chars) it must be well under
    32KB; elsewhere it stays under Linux's 128KB per-arg limit."""
    import sys as _sys
    ceiling = _dispatch_grok._GROK_INLINE_PROMPT_MAX_BYTES
    if _sys.platform == "win32":
        assert ceiling <= 30 * 1024     # safely under the ~32767 CreateProcessW cap
    else:
        assert 32 * 1024 <= ceiling <= 120 * 1024


def test_build_grok_cmd_oversized_without_file_stays_inline(tmp_path):
    """Defensive: if no prompt_file is supplied we cannot use --prompt-file, so we
    fall back to inline -p (the caller always supplies the per-pass file in
    practice; this only guards a direct unit call)."""
    big = "y" * (_dispatch_grok._GROK_INLINE_PROMPT_MAX_BYTES + 1)
    cmd = _dispatch_grok._build_grok_cmd("grok", big, model=None)
    assert "-p" in cmd
    assert "--prompt-file" not in cmd


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
    data["goal_satisfied"] = False  # non-blocking findings -> still coherent
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
    """Set of blocking_objections must equal set of {f.id : severity  in  blocking/critical}."""
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
    is visible in tests.

    iter-0045 (panel: codex high finding + kimi finding; operator
    decision): the dispatch-canon-validator forbids --prompt-file,
    --no-plan, --no-subagents, --max-turns, and --permission-mode for
    direct grok invocations - those flags caused the stall behavior.
    The dispatcher now ships the minimal verified-working shape:
    inline -p (in _build_grok_cmd) + --no-memory + --disable-web-search
    + --cwd /tmp.
    """
    assert _dispatch_grok._GROK_DISABLED_TOOLS == (
        "--no-memory",
        "--disable-web-search",
    )


# ---------- _assemble_grok_stream (streaming-json parser) ----------
#
# Ground-truth event shape (captured live from
# `grok -p ... --output-format streaming-json` 2026-05-30): each event is a
# JSON object on its own line, e.g. {"type":"thought","data":"..."},
# {"type":"text","data":"..."}, terminating with
# {"type":"end","stopReason":"EndTurn"|"Cancelled",...}. The answer is the
# concatenation of the `data` of every `text` event.

def _stream(*events):
    """Join streaming-json events (dicts) into a newline-delimited stream."""
    return "\n".join(json.dumps(e) for e in events)


def _valid_proposal():
    return {
        "selected_target": "fix grok retry",
        "rationale_vs_alternatives": "One bounded retry preserves the panel member without hiding persistent empty output.",
        "deliverable_scope": {
            "next_iteration_id": "iteration-grok-empty-retry",
            "files_in_scope": ["consensus_mcp/_dispatch_grok.py"],
            "files_out_of_scope": [],
            "key_design_decisions": ["retry empty output once"],
            "acceptance_gates": ["two-call ceiling is tested"],
        },
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": False,
    }


def test_assemble_stream_concatenates_text_events_and_ignores_thoughts():
    raw = _stream(
        {"type": "thought", "data": "let me reason about this"},
        {"type": "text", "data": '{"goal_satisfied":'},
        {"type": "text", "data": ' true, "findings": []}'},
        {"type": "thought", "data": "ok done"},
        {"type": "end", "stopReason": "EndTurn"},
    )
    out = _dispatch_grok._assemble_grok_stream(raw)
    assert out == '{"goal_satisfied": true, "findings": []}'
    # The assembled answer must itself be valid JSON for downstream parse.
    assert json.loads(out)["goal_satisfied"] is True


def test_assemble_stream_cancel_before_text_raises_invocation_error():
    """grok thinks then self-cancels with zero text events (the agentic
    self-cancel). This must surface as an INVOCATION failure, not a silent
    empty answer and not a JSON parse error (so the retry wrapper does not
    waste a parse-retry on it)."""
    raw = _stream(
        {"type": "thought", "data": "thinking 1"},
        {"type": "thought", "data": "thinking 2"},
        {"type": "end", "stopReason": "Cancelled"},
    )
    with pytest.raises(_dispatch_grok.GrokStreamCancelledError):
        _dispatch_grok._assemble_grok_stream(raw)
    assert issubclass(
        _dispatch_grok.GrokStreamCancelledError,
        _dispatch_grok.GrokInvocationError,
    )


def test_assemble_stream_cancel_with_json_in_thought_recovers_json():
    """Grok Build can put the final schema JSON in thought chunks, then cancel.
    Recover only a syntactically valid JSON object; do not seal arbitrary prose.
    """
    raw = _stream(
        {"type": "thought", "data": "reasoning... "},
        {"type": "thought", "data": '{"selected_target": "x", '},
        {"type": "thought", "data": '"structural_abstention": false}'},
        {"type": "end", "stopReason": "Cancelled"},
    )
    out = _dispatch_grok._assemble_grok_stream(raw)
    assert json.loads(out) == {"selected_target": "x", "structural_abstention": False}


@pytest.mark.parametrize("stop_reason", ["EndTurn", "Cancelled", "Unknown", None])
def test_assemble_stream_recovers_thought_json_without_text_for_any_stop_reason(stop_reason):
    events = [
        {"type": "thought", "data": "draft... "},
        {"type": "thought", "data": '{"selected_target":"x"}'},
    ]
    if stop_reason is not None:
        events.append({"type": "end", "stopReason": stop_reason})

    assert json.loads(_dispatch_grok._assemble_grok_stream(_stream(*events))) == {
        "selected_target": "x"
    }


@pytest.mark.parametrize(
    ("thought", "detail"),
    [
        ("reasoning only", "no valid JSON object"),
        ('{"broken":', "no valid JSON object"),
        ('["not", "an", "object"]', "list, not object"),
        ('"not an object"', "str, not object"),
    ],
)
def test_assemble_stream_refuses_unusable_thought_only_answers(thought, detail):
    raw = _stream(
        {"type": "thought", "data": thought},
        {"type": "end", "stopReason": "EndTurn"},
    )
    with pytest.raises(_dispatch_grok.GrokInvocationError) as exc_info:
        _dispatch_grok._assemble_grok_stream(raw)

    message = str(exc_info.value)
    assert "stopReason='EndTurn'" in message
    assert "thought_events=1" in message
    assert detail in message


def test_assemble_stream_empty_stdout_is_invocation_error():
    with pytest.raises(_dispatch_grok.GrokEmptyOutputError, match="empty stdout"):
        _dispatch_grok._assemble_grok_stream("\n  \t")


def test_assemble_stream_cancel_after_planning_text_raises_cancelled():
    raw = _stream(
        {"type": "thought", "data": "I should inspect the evidence"},
        {"type": "text", "data": "I will now emit schema-valid JSON only."},
        {"type": "end", "stopReason": "Cancelled"},
    )
    with pytest.raises(_dispatch_grok.GrokStreamCancelledError, match="text_events=1"):
        _dispatch_grok._assemble_grok_stream(raw)


def test_assemble_stream_cancelled_text_json_returns_object():
    raw = _stream(
        {"type": "text", "data": 'completed: {"selected_target":"x"}'},
        {"type": "end", "stopReason": "Cancelled"},
    )
    assert json.loads(_dispatch_grok._assemble_grok_stream(raw)) == {
        "selected_target": "x"
    }


def test_assemble_stream_cancelled_prose_text_falls_back_to_thought_json():
    raw = _stream(
        {"type": "thought", "data": '{"selected_target":"thought-answer"}'},
        {"type": "text", "data": "I was about to emit the final answer."},
        {"type": "end", "stopReason": "Cancelled"},
    )
    assert json.loads(_dispatch_grok._assemble_grok_stream(raw)) == {
        "selected_target": "thought-answer"
    }


def test_proposal_cancel_retries_with_compact_prompt(monkeypatch, tmp_path):
    """Planning text followed by Cancelled uses compact retry, not parse retry."""
    calls = []
    proposal = {
        "selected_target": "fix grok retry",
        "rationale_vs_alternatives": "Retrying with a compact prompt preserves the intended Grok panel member instead of degrading quorum.",
        "deliverable_scope": {
            "next_iteration_id": "iteration-grok-cancel-retry",
            "files_in_scope": ["consensus_mcp/_dispatch_grok.py"],
            "files_out_of_scope": [],
            "key_design_decisions": ["retry only proposal self-cancel with compact prompt"],
            "acceptance_gates": ["unit test covers cancel then success"],
        },
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": False,
    }

    def fake_invoke(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _dispatch_grok._assemble_grok_stream(_stream(
                {"type": "thought", "data": "reasoning"},
                {"type": "text", "data": "I will now emit schema-valid JSON only."},
                {"type": "end", "stopReason": "Cancelled"},
            )), tmp_path / "unreachable.txt"
        prompt_path = tmp_path / "retry-prompt.txt"
        prompt_path.write_text(kwargs["prompt"], encoding="utf-8")
        return json.dumps(proposal), prompt_path

    monkeypatch.setattr(_dispatch_grok, "_invoke_grok", fake_invoke)
    log_path = tmp_path / "dispatch-log.jsonl"

    raw, parsed, prompt_path = _dispatch_grok._invoke_grok_with_retry(
        prompt="large open-ended prompt" * 1000,
        grok_bin="grok",
        model="Grok Build",
        timeout_seconds=30,
        iter_dir=tmp_path,
        pass_id="grok-pass1",
        repo_root=tmp_path,
        log_path=log_path,
        anchors={"iteration_id": "iter", "reviewer_id": "grok", "pass_id": "grok-pass1"},
        mode="proposal",
        cancel_retry_prompt="short proposal retry prompt",
    )

    assert parsed["selected_target"] == "fix grok retry"
    assert json.loads(raw)["estimated_complexity"] == "small"
    assert prompt_path.read_text(encoding="utf-8") == "short proposal retry prompt"
    assert calls[1]["pass_id"] == "grok-pass1-cancel-retry"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "dispatch_retry_for_grok_cancel" for e in events)


def test_review_cancel_without_retry_prompt_still_fails(monkeypatch, tmp_path):
    """The compact retry is proposal-specific; review-mode cancel remains fatal."""

    def fake_invoke(**_kwargs):
        raise _dispatch_grok.GrokStreamCancelledError("cancelled before text")

    monkeypatch.setattr(_dispatch_grok, "_invoke_grok", fake_invoke)
    with pytest.raises(_dispatch_grok.GrokStreamCancelledError):
        _dispatch_grok._invoke_grok_with_retry(
            prompt="review prompt",
            grok_bin="grok",
            model="Grok Build",
            timeout_seconds=30,
            iter_dir=tmp_path,
            pass_id="grok-pass1",
            repo_root=tmp_path,
            mode="review",
        )


def test_empty_output_retries_once_then_succeeds(monkeypatch, tmp_path):
    calls = []

    def fake_invoke(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _dispatch_grok.GrokEmptyOutputError("initial empty stdout")
        prompt_path = tmp_path / "empty-retry-prompt.txt"
        prompt_path.write_text(kwargs["prompt"], encoding="utf-8")
        return json.dumps(_valid_proposal()), prompt_path

    monkeypatch.setattr(_dispatch_grok, "_invoke_grok", fake_invoke)
    log_path = tmp_path / "dispatch-log.jsonl"
    raw, parsed, _ = _dispatch_grok._invoke_grok_with_retry(
        prompt="proposal prompt",
        grok_bin="grok",
        model="grok-4.5",
        timeout_seconds=30,
        iter_dir=tmp_path,
        pass_id="grok-pass1",
        repo_root=tmp_path,
        log_path=log_path,
        anchors={"iteration_id": "iter", "reviewer_id": "grok", "pass_id": "grok-pass1"},
        mode="proposal",
    )

    assert len(calls) == 2
    assert calls[1]["pass_id"] == "grok-pass1-empty-retry"
    assert "ONLY valid JSON" in calls[1]["prompt"]
    assert parsed["selected_target"] == "fix grok retry"
    assert json.loads(raw)["structural_abstention"] is False
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert any(e["event"] == "dispatch_retry_for_grok_empty_output" for e in events)


def test_empty_output_twice_stops_after_two_attempts(monkeypatch, tmp_path):
    calls = []

    def fake_invoke(**kwargs):
        calls.append(kwargs)
        raise _dispatch_grok.GrokEmptyOutputError(f"empty attempt {len(calls)}")

    monkeypatch.setattr(_dispatch_grok, "_invoke_grok", fake_invoke)
    with pytest.raises(_dispatch_grok.GrokEmptyOutputError) as exc_info:
        _dispatch_grok._invoke_grok_with_retry(
            prompt="proposal prompt",
            grok_bin="grok",
            model="grok-4.5",
            timeout_seconds=30,
            iter_dir=tmp_path,
            pass_id="grok-pass1",
            repo_root=tmp_path,
            mode="proposal",
        )

    assert len(calls) == 2
    assert "empty attempt 1" in str(exc_info.value)
    assert "empty attempt 2" in str(exc_info.value)


def test_empty_retry_parse_failure_does_not_dispatch_third_attempt(monkeypatch, tmp_path):
    calls = []

    def fake_invoke(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _dispatch_grok.GrokEmptyOutputError("initial empty stdout")
        prompt_path = tmp_path / "malformed-retry-prompt.txt"
        prompt_path.write_text(kwargs["prompt"], encoding="utf-8")
        return "not json", prompt_path

    monkeypatch.setattr(_dispatch_grok, "_invoke_grok", fake_invoke)
    with pytest.raises(_dispatch_grok.GrokOutputParseError):
        _dispatch_grok._invoke_grok_with_retry(
            prompt="proposal prompt",
            grok_bin="grok",
            model="grok-4.5",
            timeout_seconds=30,
            iter_dir=tmp_path,
            pass_id="grok-pass1",
            repo_root=tmp_path,
            mode="proposal",
        )

    assert len(calls) == 2


def test_assemble_stream_skips_malformed_and_typeless_lines():
    """Defensive parse (spec Risks section ): non-JSON lines, JSON without a
    `type`, and truncated lines are skipped, never fatal."""
    raw = "\n".join([
        "not json at all",
        json.dumps({"no_type": 1}),
        '{"type":"text","data":"par',  # truncated -> unparseable
        "",                              # blank line
        json.dumps({"type": "text", "data": "ok"}),
        json.dumps({"type": "end", "stopReason": "EndTurn"}),
    ])
    assert _dispatch_grok._assemble_grok_stream(raw) == "ok"


def test_assemble_stream_field_fallback_to_text_key():
    """Defensive field fallback: if a text event lacks `data`, fall back to
    a `text` key before giving up (the live field is `data`, but the parser
    must not hard-fail if grok's event shape shifts)."""
    raw = _stream(
        {"type": "text", "text": "fallback-field"},
        {"type": "end", "stopReason": "EndTurn"},
    )
    assert _dispatch_grok._assemble_grok_stream(raw) == "fallback-field"


def test_assemble_stream_legacy_plain_passthrough():
    """Backward-compat / G4: input with NO streaming events at all (e.g. a
    bare JSON blob from a hypothetical plain run) passes through unchanged
    so the downstream JSON extractor still runs."""
    raw = '{"goal_satisfied": true, "findings": []}'
    assert _dispatch_grok._assemble_grok_stream(raw) == raw


def test_assemble_stream_stops_at_end_event():
    """Text events after the terminal `end` event are ignored."""
    raw = _stream(
        {"type": "text", "data": "kept"},
        {"type": "end", "stopReason": "EndTurn"},
        {"type": "text", "data": "ignored-after-end"},
    )
    assert _dispatch_grok._assemble_grok_stream(raw) == "kept"


# ----- Q4 / Finding A: configurable search-path fallback for bare-name resolution ---

def test_resolve_grok_bin_uses_configurable_search_path(tmp_path, monkeypatch):
    """When the (long-lived server's) PATH is stale and bare-name which() fails,
    resolution falls back to CONSENSUS_MCP_BIN_DIRS so grok is found without a
    per-machine config edit (consult Finding A / Q4).

    Portability: `shutil.which` only treats a file as executable on Windows when it
    carries a PATHEXT extension (an extensionless `grok` is invisible to it there),
    so the fixture binary is named `grok.exe` on Windows - mirroring a real native
    grok install - for the search-path fallback to find it.
    """
    if sys.platform == "win32":
        binp = tmp_path / "grok.exe"
        binp.write_bytes(b"MZ")  # content irrelevant; which() matches on PATHEXT
    else:
        binp = tmp_path / "grok"
        binp.write_text("#!/bin/sh\necho ok\n")
        binp.chmod(0o755)
    monkeypatch.setenv("PATH", "nonexistent-sentinel-dir")  # which("grok") -> None
    monkeypatch.setenv("CONSENSUS_MCP_BIN_DIRS", str(tmp_path))
    resolved = _dispatch_grok._resolve_grok_bin("grok")
    # os.path.normcase: on Windows `shutil.which` returns the PATHEXT casing
    # (`grok.EXE`) while the fixture is `grok.exe`, and the FS is case-insensitive -
    # so compare case- and separator-normalized (a no-op on POSIX).
    assert os.path.normcase(resolved) == os.path.normcase(str(binp))


def test_resolve_grok_bin_no_search_path_returns_bare(monkeypatch):
    """No override + which() miss -> unchanged behavior (returns the bare name)."""
    monkeypatch.setenv("PATH", "/nonexistent-sentinel-dir")
    monkeypatch.delenv("CONSENSUS_MCP_BIN_DIRS", raising=False)
    assert _dispatch_grok._resolve_grok_bin("grok") == "grok"
