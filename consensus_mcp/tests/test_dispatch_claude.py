"""Fresh Claude reviewer, fake invocation only."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

from consensus_mcp import _dispatch_claude as claude
from consensus_mcp.contributors.base import DispatchError


@pytest.fixture
def schema(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text('{"type":"object","required":["findings"]}', encoding="utf-8")
    return path


def test_claude_argv_stdin_and_shared_runner(monkeypatch, tmp_path, schema):
    from consensus_mcp import _dispatch_runner as runner
    seen = {}
    def fake(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return "fake"
    monkeypatch.setattr(runner, "run_process", fake)
    ns = SimpleNamespace(claude_bin="fake-claude", timeout_seconds=30, stall_silence_seconds=10)
    assert claude._invoke_claude(prompt="review", model="claude-fable-5-2", repo_root=tmp_path,
        schema_path=schema, ns=ns, log_path=None, anchors=None) == "fake"
    assert seen["argv"] == ["fake-claude", "-p", "--model", "claude-fable-5-2", "--output-format",
                             "json", "--allowedTools", "Read,Grep,Glob"]
    assert seen["prompt"] == "review"
    assert seen["cwd"] == tmp_path


@pytest.mark.parametrize("quota", [False, True])
def test_claude_fake_binary(tmp_path, schema, quota):
    script = tmp_path / "fake_claude.py"
    envelope = ({"result": "You've reached your Fable limit", "is_error": True, "api_error_status": 429}
                if quota else {"result": '{"findings": []}', "is_error": False})
    script.write_text("import json,sys\nsys.stdin.read()\nprint(" + repr(json.dumps(envelope)) +
                      f")\nsys.exit({1 if quota else 0})\n", encoding="utf-8")
    if os.name == "nt":
        binary = tmp_path / "fake-claude.cmd"
        binary.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        binary = tmp_path / "fake-claude"
        binary.write_text(f'#!{sys.executable}\n' + script.read_text(), encoding="utf-8")
        binary.chmod(0o755)
    ns = SimpleNamespace(claude_bin=str(binary), timeout_seconds=10, stall_silence_seconds=5, profile_data={})
    if quota:
        with pytest.raises(DispatchError) as raised:
            claude._invoke_claude(prompt="review", model="fake", repo_root=tmp_path,
                schema_path=schema, ns=ns, log_path=None, anchors=None)
        assert raised.value.failure_class == "quota"
    else:
        raw = claude._invoke_claude(prompt="review", model="fake", repo_root=tmp_path,
            schema_path=schema, ns=ns, log_path=None, anchors=None)
        assert claude._parse_claude(raw, {}, None, tmp_path, ns, schema) == {"findings": []}


@pytest.mark.parametrize("status,result,kind", [
    (429, "You've reached your Fable limit", "quota"),
    (429, "too many requests", "rate_limit"),
    (401, "invalid API key", "auth"),
    (401, "quota credit requires authentication", "auth"),
    (403, "quota credit requires authentication", "auth"),
])
def test_claude_api_failures(schema, status, result, kind):
    with pytest.raises(DispatchError) as raised:
        claude._parse_claude(json.dumps({"is_error": True, "api_error_status": status, "result": result}),
            {}, None, schema.parent, SimpleNamespace(profile_data={}), schema)
    assert raised.value.failure_class == kind


def test_claude_spec_requires_disposable_copy(monkeypatch):
    from consensus_mcp import _dispatch_runner as runner
    monkeypatch.setattr(runner, "main_for_provider", lambda spec, argv: spec)
    spec = claude.main([])
    assert spec.disposable is True
    assert spec.name == "claude"


def test_claude_nonzero_json_quota_is_preserved(monkeypatch, tmp_path, schema):
    from consensus_mcp import _dispatch_runner as runner
    def fake(*_, **__):
        error = DispatchError("exit 1", failure_class="crash")
        error.stdout = json.dumps({"is_error": True, "api_error_status": 429,
                                   "result": "You've reached your Fable limit"})
        raise error
    monkeypatch.setattr(runner, "run_process", fake)
    ns = SimpleNamespace(claude_bin="fake", timeout_seconds=10, stall_silence_seconds=5, profile_data={})
    with pytest.raises(DispatchError) as raised:
        claude._invoke_claude(prompt="review", model="fake", repo_root=tmp_path,
            schema_path=schema, ns=ns, log_path=None, anchors=None)
    assert raised.value.failure_class == "quota"


@pytest.mark.parametrize("seat", ["claude", "custom-http", "opencode-deepseek"])
def test_new_seats_seal_model_provenance_and_isolate_tool_runs(monkeypatch, tmp_path, seat):
    from consensus_mcp import _dispatch_base as base, _dispatch_runner as runner
    from consensus_mcp import _dispatch_openai_compat as http
    from consensus_mcp.contributors import opencode
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").write_text("gitdir: unused", encoding="utf-8")
    goal = root / "goal.yaml"
    goal.write_text("goal: fake\n", encoding="utf-8")
    workdir = tmp_path / "disposable"
    workdir.mkdir()
    sealed = {}
    invoked = []
    monkeypatch.setattr(base, "_resolve_repo_root", lambda: root)
    monkeypatch.setattr(base, "_load_goal_packet", lambda _: {})
    monkeypatch.setattr(base, "_build_prompt", lambda *_, **__: "review")
    def seal(packet, iteration, **kwargs):
        sealed.update(packet)
        return {"sealed_path": str(iteration / "review.yaml")}
    monkeypatch.setattr(base, "_seal_via_t6", seal)
    monkeypatch.setattr(runner, "repo_status_snapshot", lambda _: {})
    monkeypatch.setattr(runner, "make_disposable_workdir", lambda _: workdir)
    monkeypatch.setattr(runner, "cleanup_disposable_workdir", lambda _: None)
    monkeypatch.setattr(runner, "rewrite_prompt_paths", lambda prompt, *_: prompt)
    review = json.dumps({"findings": [], "goal_satisfied": True,
                         "goal_satisfied_rationale": "verified", "blocking_objections": []})
    profile = {"name": seat, "kind": "openai_compatible" if seat == "custom-http" else "cli_reviewer",
               "model": "first-model", "fallback_models": ["second-model"]}
    def invoke(**kwargs):
        invoked.append(kwargs)
        if len(invoked) == 1:
            raise DispatchError("quota exhausted", failure_class="quota")
        return json.dumps({"result": review}) if seat == "claude" else review
    if seat == "claude":
        monkeypatch.setattr(claude, "_invoke_claude", invoke)
        main = claude.main
    elif seat == "custom-http":
        monkeypatch.setattr(http, "invoke_http", lambda profile, prompt, **kwargs: invoke(prompt=prompt, **kwargs))
        main = http.main
    else:
        monkeypatch.setattr(opencode, "_invoke_opencode", invoke)
        main = opencode.main
    rc = main(["--goal-packet", str(goal), "--iteration-dir", str(root / "iteration"),
               "--profile", seat, "--profile-json", json.dumps(profile)])
    assert rc == 0
    provenance = sealed["dispatch_provenance"]
    assert provenance["model_requested"] == "first-model"
    assert provenance["model_used"] == "second-model"
    assert provenance["fallback_reason"] == "quota"
    assert provenance["family"] == sealed["family"] == "second-model"
    assert sealed["reviewer_id"].startswith(seat)
    if seat != "custom-http":
        assert all(call["repo_root"] == workdir for call in invoked)
