"""Opencode invokes only a fake runner seam; it never contacts a model."""
from types import SimpleNamespace

from consensus_mcp.contributors import opencode


def test_opencode_argv_and_windows_pwd(monkeypatch, tmp_path):
    from consensus_mcp import _dispatch_runner as runner
    captured = {}
    def fake(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return "{}"
    monkeypatch.setattr(runner, "run_process", fake)
    ns = SimpleNamespace(opencode_bin="fake-opencode", timeout_seconds=10, stall_silence_seconds=5)
    opencode._invoke_opencode(prompt="review", model="dgx/DeepSeek-V4.1-Flash-1017", repo_root=tmp_path,
        schema_path=None, ns=ns, log_path=None, anchors=None)
    # The prompt travels on stdin, never argv (Windows caps a command line at
    # ~32k characters: WinError 206 on a real packet); read-only plan agent.
    assert captured["argv"] == ["fake-opencode", "run", "--agent", "plan", "-m", "dgx/DeepSeek-V4.1-Flash-1017"]
    assert captured["prompt"] == "review"
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["env"]["PWD"] == str(tmp_path.resolve())
    assert not captured["env"]["PWD"].startswith("/c/")


def test_opencode_spec_requires_disposable_copy(monkeypatch):
    from consensus_mcp import _dispatch_runner as runner
    monkeypatch.setattr(runner, "main_for_provider", lambda spec, argv: spec)
    spec = opencode.main([])
    assert spec.disposable is True
    assert spec.name == "opencode-deepseek"



def test_opencode_bare_name_resolves_like_a_shell(monkeypatch, tmp_path):
    # Windows CreateProcess ignores PATHEXT, so bare `opencode` could not
    # start npm's opencode.cmd launcher (WinError 2).
    import shutil
    from consensus_mcp import _dispatch_runner as runner
    captured = {}
    monkeypatch.setattr(shutil, "which", lambda name: r"C:\tools\node\opencode.CMD" if name == "opencode" else None)
    monkeypatch.setattr(runner, "run_process", lambda argv, **kw: captured.update(argv=argv) or "{}")
    ns = SimpleNamespace(opencode_bin="opencode", timeout_seconds=10, stall_silence_seconds=5)
    opencode._invoke_opencode(prompt="x" * 100_000, model="DeepSeek-V4.1-Flash-1017", repo_root=tmp_path,
        schema_path=None, ns=ns, log_path=None, anchors=None)
    assert captured["argv"][0] == r"C:\tools\node\opencode.CMD"
    assert sum(len(a) for a in captured["argv"]) < 1000


def test_opencode_parse_recovers_json_after_agent_narration(monkeypatch):
    # Live DeepSeek-through-opencode output: prose, then a fenced JSON block.
    seen = {}
    monkeypatch.setattr(opencode, "parse_json_output", lambda text, *a, **k: seen.setdefault("text", text))
    raw = ("> plan . DeepSeek-V4.1-Flash-1017\nI'll ground this in the code first.\n\n"
           "```json\n{\"findings\": [], \"goal_satisfied\": true}\n```\nDone.\n")
    ns = SimpleNamespace(schema=None, profile_data={})
    opencode._parse(raw, None, None, None, ns, None)
    assert seen["text"] == "{\"findings\": [], \"goal_satisfied\": true}"


def test_opencode_silence_watchdog_waits_for_full_timeout(monkeypatch, tmp_path):
    # Live: the plan agent was silent >180 s while reading files and the
    # watchdog killed a healthy review ("stuck: no output for 180s").
    from consensus_mcp import _dispatch_runner as runner
    seen = {}
    monkeypatch.setattr(runner, "run_process", lambda argv, **kw: seen.update(kw) or "{}")
    ns = SimpleNamespace(opencode_bin="opencode", timeout_seconds=900, stall_silence_seconds=180)
    opencode._invoke_opencode(prompt="x", model="DeepSeek-V4.1-Flash-1017", repo_root=tmp_path,
        schema_path=None, ns=ns, log_path=None, anchors=None)
    assert seen["stall_silence_seconds"] == 900
