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
    assert captured["argv"] == ["fake-opencode", "run", "-m", "dgx/DeepSeek-V4.1-Flash-1017", "review"]
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["env"]["PWD"] == str(tmp_path.resolve())
    assert not captured["env"]["PWD"].startswith("/c/")


def test_opencode_spec_requires_disposable_copy(monkeypatch):
    from consensus_mcp import _dispatch_runner as runner
    monkeypatch.setattr(runner, "main_for_provider", lambda spec, argv: spec)
    spec = opencode.main([])
    assert spec.disposable is True
    assert spec.name == "opencode-deepseek"
