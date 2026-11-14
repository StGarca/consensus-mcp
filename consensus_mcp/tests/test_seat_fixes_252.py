"""2.5.2 seat fixes, each found in a real DriverLogs consult on Windows."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from consensus_mcp import _dispatch_codex as codex
from consensus_mcp import _dispatch_gemini as gem


# --- Codex: 180 s silence default killed a healthy Astra review ------------

def test_codex_silence_bounded_by_seat_timeout_by_default():
    ns = SimpleNamespace(stall_silence_seconds=180.0, timeout_seconds=1770, stall_silence_explicit=False)
    assert codex._codex_stall_silence(ns) == 1770


def test_codex_explicit_silence_limit_is_honoured():
    ns = SimpleNamespace(stall_silence_seconds=240.0, timeout_seconds=1770, stall_silence_explicit=True)
    assert codex._codex_stall_silence(ns) == 240


def test_codex_dispatch_passes_the_bounded_limit(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(codex, "_invoke_codex", lambda *a, **k: seen.update(k) or "{}")
    ns = SimpleNamespace(codex_bin="codex", timeout_seconds=1500, effort="high",
                         stall_silence_seconds=180.0, stall_silence_explicit=False)
    codex._provider_invoke(prompt="p", model="m", repo_root=tmp_path, schema_path=None,
                           ns=ns, log_path=None, anchors=None)
    assert seen["stall_silence_seconds"] == 1500


@pytest.mark.parametrize("env,argv,explicit", [
    (None, [], False),
    ("600", [], True),
    (None, ["--stall-silence-seconds", "90"], True),
    (None, ["--stall-silence-seconds=90"], True),
    ("not-a-number", [], False),
])
def test_runner_records_whether_silence_limit_was_explicit(monkeypatch, env, argv, explicit):
    from consensus_mcp import _dispatch_runner as runner
    if env is None:
        monkeypatch.delenv("CONSENSUS_MCP_STALL_SILENCE_SECONDS", raising=False)
    else:
        monkeypatch.setenv("CONSENSUS_MCP_STALL_SILENCE_SECONDS", env)
    captured = {}

    class Stop(Exception):
        pass

    def fake_repo_root():
        raise Stop

    spec = runner.ProviderSpec(name="probe", invoke=lambda **k: "", parse=lambda *a, **k: {},
                               default_model="m", template_name="codex")
    monkeypatch.setattr(runner.argparse.ArgumentParser, "parse_args",
                        (lambda orig: lambda self, a=None: captured.setdefault("ns", orig(self, a)))(
                            runner.argparse.ArgumentParser.parse_args))
    from consensus_mcp import _dispatch_base as base
    monkeypatch.setattr(base, "_resolve_repo_root", fake_repo_root)
    with pytest.raises(Stop):
        runner.main_for_provider(spec, ["--goal-packet", "g.yaml", "--iteration-dir", "d"] + argv)
    assert captured["ns"].stall_silence_explicit is explicit


# --- Gemini: YAML answers and refused tool calls ---------------------------

REVIEW = {"findings": [], "goal_satisfied": True, "blocking_objections": [],
          "goal_satisfied_rationale": "Plan applies the accepted objections."}


def test_gemini_valid_yaml_answer_is_accepted():
    import yaml
    text = "```yaml\n" + yaml.safe_dump(REVIEW, sort_keys=False) + "```\n"
    assert gem._parse_gemini_output(text)["goal_satisfied"] is True


def test_gemini_yaml_with_backtick_values_is_repaired_without_changing_text():
    text = ("```yaml\nfindings: []\ngoal_satisfied: false\nblocking_objections: []\n"
            "goal_satisfied_rationale: `code/x.py:12` still reads the old field\n```")
    out = gem._parse_gemini_output(text)
    assert out["goal_satisfied_rationale"] == "`code/x.py:12` still reads the old field"


def test_gemini_yaml_schema_violations_are_still_rejected():
    import yaml
    bad = dict(REVIEW, proposal={"x": 1})  # live: a plan review that also carried a proposal block
    with pytest.raises(gem.GeminiOutputParseError, match="unexpected top-level keys"):
        gem._parse_gemini_output("```yaml\n" + yaml.safe_dump(bad) + "```")


def test_gemini_non_mapping_text_still_fails():
    with pytest.raises(gem.GeminiOutputParseError, match="not valid JSON"):
        gem._parse_gemini_output("I could not find the task description.")


def test_gemini_json_answers_unchanged():
    assert gem._parse_gemini_output(json.dumps(REVIEW))["blocking_objections"] == []


def _transcript(tmp_path, conv, rows):
    d = tmp_path / conv / ".system_generated" / "logs"
    d.mkdir(parents=True)
    (d / "transcript_full.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_refused_agy_tool_call_is_named_instead_of_empty_response(tmp_path):
    _transcript(tmp_path, "conv-1", [
        {"type": "USER_INPUT", "status": "DONE", "content": "task"},
        {"type": "GENERIC", "status": "ERROR", "content": "Created At: x\nEncountered error in step execution: "
         "permission check failed for command \"Get-ChildItem -Recurse -Name\": user denied permission\n"
         "Do not attempt to circumvent this denial."},
    ])
    message = gem._agy_empty_response_message("conv-1", brain_dir=tmp_path)
    assert 'permission check failed for command "Get-ChildItem -Recurse -Name"' in message
    assert "skip_permissions: true" in message and "circumvent" not in message
    assert "conv-1" in message


def test_empty_agy_answer_without_recorded_errors_points_at_transcript(tmp_path):
    _transcript(tmp_path, "conv-2", [{"type": "USER_INPUT", "status": "DONE", "content": "task"}])
    message = gem._agy_empty_response_message("conv-2", brain_dir=tmp_path)
    assert message.startswith("agy ended without an answer (empty response)") and "conv-2" in message


@pytest.mark.parametrize("conv", [None, "", "../../etc", "a/b"])
def test_step_error_lookup_rejects_unsafe_conversation_ids(tmp_path, conv):
    assert gem._agy_step_errors(conv, brain_dir=tmp_path) == []


def test_empty_stream_response_raises_with_named_cause(tmp_path, monkeypatch):
    monkeypatch.setattr(gem, "_AGY_BRAIN", tmp_path)
    _transcript(tmp_path, "conv-3", [{"type": "GENERIC", "status": "ERROR",
        "content": "Encountered error in step execution: permission check failed for command \"ls\""}])
    raw = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "", "conversation_id": "conv-3"}})
    with pytest.raises(gem.GeminiInvocationError, match="permission check failed"):
        gem._agy_stream_response(raw)
