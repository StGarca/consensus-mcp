"""Shared dispatch lifecycle and same-seat failure policy, using local fakes."""
import json
import sys

import pytest

from consensus_mcp import _dispatch_runner as runner
from consensus_mcp.contributors.base import DispatchError


def test_process_stdin_utf8_and_flag_transport(tmp_path):
    prompt = "review \u03bb\nnext"
    output = runner.run_process(
        [sys.executable, "-I", "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        prompt=prompt, cwd=tmp_path, timeout_seconds=5,
    )
    assert output == prompt
    output = runner.run_process(
        [sys.executable, "-I", "-c", "import sys; sys.stdout.buffer.write(sys.argv[1].encode('utf-8'))", prompt],
        cwd=tmp_path, timeout_seconds=5,
    )
    assert output.strip() == prompt


@pytest.mark.parametrize("timeout,stall", [(0.1, 0), (5, 0.1)])
def test_process_timeout_kills_tree(tmp_path, monkeypatch, timeout, stall):
    killed = []
    original = runner.base._terminate_process_tree
    def terminate(proc):
        killed.append(proc.pid)
        original(proc)
    monkeypatch.setattr(runner.base, "_terminate_process_tree", terminate)
    with pytest.raises(DispatchError) as failure:
        runner.run_process([sys.executable, "-I", "-c", "import time; time.sleep(20)"],
                           cwd=tmp_path, timeout_seconds=timeout,
                           stall_silence_seconds=stall, poll_interval=.01)
    assert failure.value.failure_class == "timeout"
    assert len(killed) == 1


def test_partial_stderr_bytes_keep_watchdog_alive(tmp_path):
    code = ("import sys,time; "
            "[(sys.stderr.buffer.write(b'x'),sys.stderr.buffer.flush(),time.sleep(.03)) for _ in range(8)]; "
            "print('done')")
    # Interpreter startup gets its own grace (it can exceed 150 ms on a loaded
    # Windows host); the 150 ms limit then applies between partial bytes, which
    # is what this test is about.
    assert runner.run_process([sys.executable, "-I", "-c", code], cwd=tmp_path,
                              timeout_seconds=3, stall_silence_seconds=.15,
                              startup_silence_seconds=2.5).strip() == "done"


def test_env_scrubs_ambient_keys_and_python_hosts_without_mutation(monkeypatch, tmp_path):
    ambient = {"OPENAI_API_KEY": "secret", "other_api_key": "secret", "PYTHONPATH": "bad",
               "PYTHONHOME": "bad", "VIRTUAL_ENV": "bad", "DGX_TOKEN": "secret", "PATH": "keep"}
    token = runner._ACTIVE_PROFILE.set({"_api_key_envs": ["DGX_TOKEN"]})
    try:
        cleaned = runner.subprocess_env(ambient)
        for key, value in ambient.items():
            if key != "PATH":
                monkeypatch.setenv(key, value)
        code = ("import json,os; print(json.dumps({k:os.environ.get(k) for k in "
                "['OPENAI_API_KEY','other_api_key','PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','DGX_TOKEN','PYTHONUTF8','PYTHONIOENCODING']}))")
        child = json.loads(runner.run_process([sys.executable, "-I", "-c", code],
                                              cwd=tmp_path, timeout_seconds=5))
    finally:
        runner._ACTIVE_PROFILE.reset(token)
    assert cleaned == {"PATH": "keep", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    assert ambient["DGX_TOKEN"] == "secret"
    assert child == {**{key: None for key in ambient if key != "PATH"},
                     "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def test_quota_chain_records_actual_seat_model_and_custom_signature():
    calls = []
    def invoke(model):
        calls.append(model)
        if model == "primary":
            raise DispatchError("bespoke vendor capacity exhausted")
        return "review"
    result, provenance = runner.run_model_chain(invoke, "primary", ["fallback"],
                                                quota_signatures=["vendor capacity exhausted"])
    assert result == "review"
    assert calls == ["primary", "fallback"]
    assert provenance == {"model_requested": "primary", "model_used": "fallback",
                          "fallback_reason": "quota", "family": "fallback"}


@pytest.mark.parametrize("kind", ["auth", "parse", "integrity", "crash"])
def test_nonquota_failure_never_changes_model(kind):
    calls = []
    def invoke(model):
        calls.append(model)
        raise DispatchError("failure", kind)
    with pytest.raises(DispatchError) as failure:
        runner.run_model_chain(invoke, "primary", ["fallback"])
    assert failure.value.failure_class == kind
    assert calls == ["primary"]


def test_rate_limit_gets_only_one_bounded_backoff():
    calls, delays = [], []
    def invoke(model):
        calls.append(model)
        raise DispatchError("429 too many requests", "rate_limit")
    with pytest.raises(DispatchError):
        runner.run_model_chain(invoke, "primary", ["fallback"], sleep=delays.append)
    assert calls == ["primary", "primary"]
    assert delays == [.25]


def test_sealed_provenance_uses_model_that_succeeded(tmp_path, monkeypatch, capsys):
    # Packet construction has real-Git coverage in test_dispatch_review_packet;
    # this unit fixture supplies only a repository marker for the model chain.
    monkeypatch.setattr(runner.base, "_diff_review_target", lambda *args: None)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".consensus").mkdir()
    (tmp_path / ".consensus" / "config.yaml").write_text(
        "contributors:\n  profiles:\n    custom:\n      model: first\n      fallback_models: [second]\n      quota_signatures: [vendor-only-limit]\n      timeout_seconds: 17\n",
        encoding="utf-8",
    )
    (tmp_path / "goal.yaml").write_text("goal: {summary: test}\n", encoding="utf-8")
    monkeypatch.setattr(runner.base, "_resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner.base, "_seal_via_t6", lambda packet, *a, **kw: {"sealed_path": "fake", "packet": packet})
    monkeypatch.setenv("CONSENSUS_MCP_STALL_SILENCE_SECONDS", "invalid")
    calls = []
    def invoke(**kwargs):
        calls.append((kwargs["model"], kwargs["ns"].timeout_seconds))
        if kwargs["model"] == "first":
            raise DispatchError("vendor-only-limit")
        return "{}"
    spec = runner.ProviderSpec("custom", invoke, lambda *a, **kw: {})
    assert runner.main_for_provider(spec, ["--profile", "custom", "--goal-packet", "goal.yaml",
                                           "--iteration-dir", "iteration"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert calls == [("first", 17), ("second", 17)]
    provenance = result["packet"]["dispatch_provenance"]
    assert provenance["model_requested"] == "first"
    assert provenance["model_used"] == result["packet"]["family"] == "second"
    assert provenance["fallback_reason"] == "quota"

def _capture_main(provider, monkeypatch, tmp_path, mode, schema_override=None):
    """Exercise the shared main and provider parser seams without a model call."""
    monkeypatch.setattr(runner.base, "_diff_review_target", lambda *args: None)
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "goal.yaml").write_text("goal: {summary: test}\n", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(provider, "_resolve_repo_root", lambda: tmp_path)
    def load_template(path):
        captured["template"] = path
        return "Review {goal_summary}"
    monkeypatch.setattr(provider, "_load_template", load_template)
    def invoke(**kwargs):
        captured["invoke_schema"] = kwargs["schema_path"]
        return "{}"
    monkeypatch.setattr(provider, "_provider_invoke", invoke)
    name = provider.__name__.rsplit("_", 1)[-1]
    monkeypatch.setattr(provider, f"_get_{name}_version", lambda binary: "fake")
    def parse(raw, **kwargs):
        captured["schema"] = kwargs.get("schema_path")
        return {}
    monkeypatch.setattr(provider, f"_parse_{name}_proposal_output", parse)
    monkeypatch.setattr(provider, f"_parse_{name}_output", parse)
    def seal(packet, *args, **kwargs):
        captured["packet"] = packet
        return {"sealed_path": "fake"}
    monkeypatch.setattr(provider, "_seal_via_t6", seal)
    argv = ["--goal-packet", "goal.yaml", "--iteration-dir", "iteration", "--mode", mode]
    if schema_override is not None:
        argv += ["--schema", str(schema_override)]
    assert provider.main(argv) == 0
    return captured
