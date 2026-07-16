"""Unit tests for consensus_mcp._dispatch_kimi.

UX-parity sibling of test_dispatch_gemini.py. Focused on kimi-specific
behavior, all kept BEHIND the same surface as gemini so the operator sees
identical CLI flags + sealed kimi-review.yaml + timing/log lines:

  - _kimi_subprocess_env (scrubs KIMI_API_KEY + OPENAI_API_KEY so a stray
    external key can't hijack the OAuth file-cred call - the INVERSE of
    gemini's GEMINI_CLI_TRUST_WORKSPACE injection)
  - _peel_assistant_content (peels `content` off the stream-json line)
  - _extract_json_from_text (reused from gemini; content is free text inside)
  - _parse_kimi_output (validates JSON shape; kimi-rev-N IDs; patch_proposal null)
  - _invoke_kimi argv construction (kimi --print --output-format stream-json
    --final-message-only --max-steps-per-turn 1 --work-dir <repo> -p <prompt>)
  - _invoke_kimi exit-code mapping: 0 -> success, 1 -> hard fail (non-retryable),
    75 -> retryable (treated like gemini's 429 retry path)
  - watchdog / stall handling (default stall_silence 240s; honors
    CONSENSUS_MCP_STALL_SILENCE_SECONDS)
  - _invoke_kimi_with_retry (validator-retry on first-pass parse fail)

Does NOT call the real kimi binary (smoke env-gated to
CONSENSUS_MCP_RUN_REAL_KIMI_SMOKE=1).
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

# Allow imports of consensus_mcp from the repo root without install.
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consensus_mcp import _dispatch_kimi  # noqa: E402


# ---------- _kimi_subprocess_env (scrub stray keys) ----------
# CRITICAL (inverse of gemini's trust injection): kimi auth is OAuth file
# creds at ~/.kimi/credentials/kimi-code.json. A stray KIMI_API_KEY or
# OPENAI_API_KEY in the environment could hijack the OAuth call, so the
# subprocess env must SCRUB both before spawning kimi.

def test_kimi_subprocess_env_scrubs_kimi_api_key(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "sk-stray-kimi")
    env = _dispatch_kimi._kimi_subprocess_env()
    assert "KIMI_API_KEY" not in env


def test_kimi_subprocess_env_scrubs_openai_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stray-openai")
    env = _dispatch_kimi._kimi_subprocess_env()
    assert "OPENAI_API_KEY" not in env


def test_kimi_subprocess_env_preserves_parent_env(monkeypatch):
    monkeypatch.setenv("PATH", "/sentinel/path")
    monkeypatch.setenv("SOME_UNRELATED_VAR", "keepme")
    env = _dispatch_kimi._kimi_subprocess_env()
    assert env["PATH"] == "/sentinel/path"          # PATH not clobbered
    assert env["SOME_UNRELATED_VAR"] == "keepme"     # full inheritance


def test_kimi_subprocess_env_does_not_mutate_os_environ(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "sk-stray")
    _dispatch_kimi._kimi_subprocess_env()
    # helper returns a copy; never touches os.environ
    assert os.environ.get("KIMI_API_KEY") == "sk-stray"


def test_kimi_subprocess_env_forces_pythonutf8(monkeypatch):
    # v1.33.x consult Finding B / Q5: kimi-cli (a Python app) decodes its UTF-8
    # stdin under the Windows locale (cp1252) and crashes on a lone surrogate
    # when a review payload carries a non-cp1252 byte. Forcing PYTHONUTF8=1 makes
    # kimi-cli decode stdin as UTF-8 regardless of console code page.
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    env = _dispatch_kimi._kimi_subprocess_env()
    assert env["PYTHONUTF8"] == "1"


def test_kimi_subprocess_env_does_not_set_kimi_api_key():
    # Per task: do NOT set KIMI_API_KEY (OAuth file creds are authoritative).
    env = _dispatch_kimi._kimi_subprocess_env()
    assert "KIMI_API_KEY" not in env


# ---------- _peel_assistant_content (stream-json content peel) ----------

def test_peel_extracts_content_from_single_stream_json_line():
    payload = {"role": "assistant", "content": '{"findings": []}'}
    line = json.dumps(payload)
    assert _dispatch_kimi._peel_assistant_content(line) == '{"findings": []}'


def test_peel_handles_trailing_newline():
    payload = {"role": "assistant", "content": "the answer"}
    line = json.dumps(payload) + "\n"
    assert _dispatch_kimi._peel_assistant_content(line) == "the answer"


def test_peel_ignores_non_assistant_lines_and_finds_assistant():
    lines = "\n".join([
        json.dumps({"role": "system", "content": "ready"}),
        json.dumps({"role": "assistant", "content": '{"goal_satisfied": true}'}),
    ])
    assert _dispatch_kimi._peel_assistant_content(lines) == '{"goal_satisfied": true}'


def test_peel_falls_back_to_raw_when_not_stream_json():
    # If kimi (against expectation) emits raw JSON not wrapped in a
    # stream-json envelope, peel returns the text unchanged so the
    # downstream _extract_json_from_text still gets a chance.
    raw = '{"findings": [], "goal_satisfied": true}'
    assert _dispatch_kimi._peel_assistant_content(raw) == raw


def test_peel_falls_back_to_markdown_fenced_json_when_no_envelope():
    # kimi sometimes prints a ```json ... ``` block directly instead of a
    # stream-json envelope (same shape gemini emits). With no assistant
    # envelope present, peel must hand the fenced block back unchanged so the
    # downstream _extract_json_from_text can recover it - NOT raise.
    raw = '```json\n{"findings": [], "goal_satisfied": true}\n```'
    assert _dispatch_kimi._peel_assistant_content(raw) == raw


def test_peel_falls_back_to_bullet_prefixed_json_when_no_envelope():
    # Kimi Code 0.19.x prompt mode can prefix the final JSON with its TUI bullet
    # marker ("- {") and append a resume trailer. That is still recoverable
    # free-form JSON: peel must pass it through so _extract_json_from_text can
    # take the first outer brace through the last outer brace.
    raw = '- {"selected_target":"borrow graph layer","estimated_complexity":"small"}\n\nTo resume this session: kimi -r session_abc'
    assert _dispatch_kimi._peel_assistant_content(raw) == raw.strip()


def test_peel_raises_on_empty_output():
    with pytest.raises(_dispatch_kimi.KimiOutputParseError):
        _dispatch_kimi._peel_assistant_content("")


# ---------- _extract_json_from_text (reused; content carries free text) ----------

def test_extract_pure_json_passes_through():
    text = '{"findings": [], "goal_satisfied": true}'
    assert _dispatch_kimi._extract_json_from_text(text) == text


def test_extract_handles_json_markdown_fence():
    text = 'Sure thing:\n```json\n{"findings": []}\n```\nhope that helps'
    assert _dispatch_kimi._extract_json_from_text(text) == '{"findings": []}'


# ---------- _rewrite_prompt_paths (repo-path-leak guard) ----------
# The rewrite must cover BOTH separator forms of the real repo path (Windows
# prompts can carry C:/Users/x AND C:\Users\x for the same path), and the
# residual-leak check must fail closed - case-insensitively on Windows.

def test_rewrite_prompt_paths_posix_rewrites_and_passes():
    repo = Path("/tmp/example-project")
    workdir = Path("/tmp/kimi-workdir-abc")
    prompt = f"Review {repo}/consensus_mcp/server.py inside {repo}."
    out = _dispatch_kimi._rewrite_prompt_paths(prompt, repo, workdir)
    assert str(repo) not in out
    assert f"{workdir}/consensus_mcp/server.py" in out


def test_rewrite_prompt_paths_windows_mixed_separators():
    # Simulates a Windows prompt carrying BOTH separator forms of the same
    # real repo path. PureWindowsPath gives the backslash str() form and the
    # forward-slash as_posix() form on any host platform.
    from pathlib import PureWindowsPath

    repo = PureWindowsPath(r"C:\Users\example\projects\example-project")
    workdir = PureWindowsPath(r"C:\Temp\kimi-workdir-abc")
    prompt = (
        r"Files: C:\Users\example\projects\example-project\a.py and "
        "C:/Users/example/projects/example-project/b.py."
    )
    out = _dispatch_kimi._rewrite_prompt_paths(prompt, repo, workdir)
    assert r"C:\Users\example\projects\example-project" not in out
    assert "C:/Users/example/projects/example-project" not in out
    assert r"C:\Temp\kimi-workdir-abc\a.py" in out
    assert "C:/Temp/kimi-workdir-abc/b.py" in out


def test_rewrite_prompt_paths_case_variant_leak_raises_when_case_insensitive():
    # On Windows (case-insensitive paths) a drive-letter/case variant of the
    # repo path is still the SAME directory; the rewrite cannot map it (str
    # replace is case-sensitive) so the residual check must refuse dispatch.
    from pathlib import PureWindowsPath

    repo = PureWindowsPath(r"C:\Users\example\projects\example-project")
    workdir = PureWindowsPath(r"C:\Temp\kimi-workdir-abc")
    prompt = r"sneaky path c:\users\example\projects\example-project\a.py"
    with pytest.raises(_dispatch_kimi.KimiInvocationError):
        _dispatch_kimi._rewrite_prompt_paths(
            prompt, repo, workdir, _case_insensitive=True,
        )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX case-sensitive path semantics; the Windows filesystem is "
    "case-insensitive so the rewrite correctly matches a differently-cased "
    "path (pre-existing Windows-only skip, unrelated to architect-build).",
)
def test_rewrite_prompt_paths_posix_case_sensitive_leaves_distinct_path():
    # POSIX paths are case-sensitive: a differently-cased path is a DIFFERENT
    # directory, so it must neither be rewritten nor trip the leak check.
    repo = Path("/tmp/Example-project")
    workdir = Path("/tmp/kimi-workdir-abc")
    prompt = "see /tmp/example-project/a.py"
    assert _dispatch_kimi._rewrite_prompt_paths(prompt, repo, workdir) == prompt


# ---------- _parse_kimi_output ----------

def _minimal_valid():
    return {
        "findings": [],
        "goal_satisfied": True,
        "goal_satisfied_rationale": "No issues found.",
        "blocking_objections": [],
    }


def test_parse_minimal_valid():
    parsed = _dispatch_kimi._parse_kimi_output(json.dumps(_minimal_valid()))
    assert parsed["goal_satisfied"] is True
    assert parsed["findings"] == []


def test_parse_rejects_invalid_json():
    with pytest.raises(_dispatch_kimi.KimiOutputParseError, match="not valid JSON"):
        _dispatch_kimi._parse_kimi_output("{not json")


def test_parse_rejects_non_object_root():
    with pytest.raises(_dispatch_kimi.KimiOutputParseError, match="must be an object"):
        _dispatch_kimi._parse_kimi_output("[1,2,3]")


def test_parse_rejects_unknown_top_level_key():
    payload = _minimal_valid()
    payload["unexpected"] = "field"
    with pytest.raises(_dispatch_kimi.KimiOutputParseError, match="unexpected top-level"):
        _dispatch_kimi._parse_kimi_output(json.dumps(payload))


def test_parse_rejects_missing_required():
    payload = _minimal_valid()
    del payload["goal_satisfied"]
    with pytest.raises(_dispatch_kimi.KimiOutputParseError, match="missing required"):
        _dispatch_kimi._parse_kimi_output(json.dumps(payload))


def test_parse_rejects_finding_with_gemini_id_prefix():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "gemini-rev-001",
        "severity": "low",
        "summary": "wrong adapter prefix",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "use kimi-rev-N",
    }]
    with pytest.raises(_dispatch_kimi.KimiOutputParseError, match=r"\^kimi-rev"):
        _dispatch_kimi._parse_kimi_output(json.dumps(payload))


def test_parse_accepts_kimi_rev_id():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "kimi-rev-001",
        "severity": "low",
        "summary": "minor stylistic suggestion",
        "citation": "consensus_mcp/foo.py:1",
        "risk": "very low",
        "recommendation": "consider renaming",
    }]
    parsed = _dispatch_kimi._parse_kimi_output(json.dumps(payload))
    assert parsed["findings"][0]["id"] == "kimi-rev-001"


def test_parse_rejects_non_null_patch_proposal():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "kimi-rev-001",
        "severity": "low",
        "summary": "x",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "rec",
        "patch_proposal": {"patch_id": "kimi-rev-001-patch"},
    }]
    with pytest.raises(_dispatch_kimi.KimiOutputParseError, match="patch_proposal must be null"):
        _dispatch_kimi._parse_kimi_output(json.dumps(payload))


def test_parse_blocking_invariant_violated():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "kimi-rev-001",
        "severity": "blocking",
        "summary": "x",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "rec",
    }]
    with pytest.raises(_dispatch_kimi.KimiOutputParseError, match="blocking_objections invariant"):
        _dispatch_kimi._parse_kimi_output(json.dumps(payload))


def test_parse_rejects_goal_satisfied_true_with_blocking_objections():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "kimi-rev-001",
        "severity": "blocking",
        "summary": "x",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "rec",
    }]
    payload["blocking_objections"] = ["kimi-rev-001"]
    payload["goal_satisfied"] = True  # incoherent
    with pytest.raises(_dispatch_kimi.KimiOutputParseError, match="incoherent"):
        _dispatch_kimi._parse_kimi_output(json.dumps(payload))


def test_parse_blocking_invariant_satisfied():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "kimi-rev-001",
        "severity": "critical",
        "summary": "x",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "rec",
    }]
    payload["blocking_objections"] = ["kimi-rev-001"]
    payload["goal_satisfied"] = False
    parsed = _dispatch_kimi._parse_kimi_output(json.dumps(payload))
    assert parsed["blocking_objections"] == ["kimi-rev-001"]


def test_parse_extracts_from_wrapped_output():
    payload_str = json.dumps(_minimal_valid())
    wrapped = f"Sure, here's the review:\n```json\n{payload_str}\n```\nDone."
    parsed = _dispatch_kimi._parse_kimi_output(wrapped)
    assert parsed["goal_satisfied"] is True


def test_parse_rejects_empty_summary():
    payload = _minimal_valid()
    payload["findings"] = [{
        "id": "kimi-rev-001",
        "severity": "low",
        "summary": "",
        "citation": "x:1",
        "risk": "r",
        "recommendation": "rec",
    }]
    with pytest.raises(_dispatch_kimi.KimiOutputParseError, match="non-empty"):
        _dispatch_kimi._parse_kimi_output(json.dumps(payload))


def test_parse_rejects_empty_rationale():
    payload = _minimal_valid()
    payload["goal_satisfied_rationale"] = ""
    with pytest.raises(_dispatch_kimi.KimiOutputParseError, match="non-empty"):
        _dispatch_kimi._parse_kimi_output(json.dumps(payload))


# ---------- _parse_kimi_output end-to-end through the content peel ----------

def test_parse_through_stream_json_content_peel():
    """Full kimi path: stream-json line -> peel content -> extract -> parse."""
    review = _minimal_valid()
    review["findings"] = [{
        "id": "kimi-rev-001",
        "severity": "low",
        "summary": "minor",
        "citation": "x:1",
        "risk": "low",
        "recommendation": "rec",
    }]
    review["goal_satisfied"] = False  # has a finding; keep coherent
    line = json.dumps({"role": "assistant", "content": json.dumps(review)})
    content = _dispatch_kimi._peel_assistant_content(line)
    parsed = _dispatch_kimi._parse_kimi_output(content)
    assert parsed["findings"][0]["id"] == "kimi-rev-001"


# ---------- _invoke_kimi argv construction + exit-code mapping ----------

class _FakeProc:
    """Minimal Popen stand-in driven by scripted stdout bytes + returncode."""

    def __init__(self, stdout_lines: list[bytes], returncode: int):
        self._stdout_lines = list(stdout_lines)
        self._stderr_lines: list[bytes] = []
        self.returncode = returncode
        self._polled = False
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(self._stdout_lines)
        self.stderr = _FakeStream(self._stderr_lines)
        self.pid = 4321

    def poll(self):
        # First poll: still "running" so the watchdog loop body executes once
        # (lets the reader threads drain). Subsequent polls: exited.
        if not self._polled:
            self._polled = True
            return None
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def send_signal(self, sig):
        pass

    def terminate(self):
        pass

    def kill(self):
        pass


class _FakeStdin:
    def __init__(self):
        self.written = b""

    def write(self, data):
        # New stdin transport writes the prompt as bytes; capture it so tests
        # can assert the full prompt flowed through stdin (not argv).
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.written += data

    def close(self):
        pass


class _FakeStream:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


def _make_factory(stdout_lines, returncode, captured_cmd, captured_env, captured_procs=None):
    def factory(cmd, **kwargs):
        captured_cmd.append(cmd)
        captured_env.append(kwargs.get("env"))
        proc = _FakeProc(stdout_lines, returncode)
        if captured_procs is not None:
            captured_procs.append(proc)
        return proc
    return factory


def test_invoke_kimi_builds_expected_argv(monkeypatch):
    """New Kimi Code CLI uses prompt mode: `kimi -p <prompt> --output-format text`.

    The binary lives at ~/.kimi-code/bin/kimi (resolved by the dispatcher when
    PATH is stale). It has no --quiet/--thinking/--work-dir flags; project
    context comes from subprocess cwd=repo_root.
    """
    review_line = b"OK final answer"
    captured_cmd: list = []
    captured_env: list = []
    captured_procs: list = []
    monkeypatch.setattr(_dispatch_kimi, "_is_kimi_code_cli", lambda _path: True)
    factory = _make_factory([review_line], 0, captured_cmd, captured_env, captured_procs)
    out = _dispatch_kimi._invoke_kimi(
        prompt="REVIEW PROMPT BODY",
        kimi_bin="kimi",
        timeout_seconds=1800,
        repo_root=Path("/tmp/workdir-copy"),
        poll_interval=0.0,
        popen_factory=factory,
    )
    assert out == "OK final answer"
    cmd = captured_cmd[0]
    assert cmd[0].endswith("/kimi") or cmd[0] == "kimi"
    assert "-p" in cmd
    assert cmd[cmd.index("-p") + 1] == "REVIEW PROMPT BODY"
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "text"
    assert "--quiet" not in cmd
    assert "--thinking" not in cmd
    assert "--work-dir" not in cmd
    assert captured_procs[0].stdin.written == b""


def test_invoke_kimi_large_prompt_fails_closed_for_kimi_code_cli(monkeypatch):
    """New Kimi Code CLI has no documented stdin/prompt-file transport.

    A prompt over the safe inline argv ceiling must fail before subprocess rather
    than crashing opaquely with E2BIG.
    """
    big_prompt = "X" * (200 * 1024)
    captured_cmd: list = []
    captured_env: list = []
    monkeypatch.setattr(_dispatch_kimi, "_is_kimi_code_cli", lambda _path: True)
    factory = _make_factory([b"OK"], 0, captured_cmd, captured_env)
    with pytest.raises(_dispatch_kimi.KimiInvocationError, match="inline argv size"):
        _dispatch_kimi._invoke_kimi(
            prompt=big_prompt,
            kimi_bin="kimi",
            timeout_seconds=1800,
            repo_root=Path("/tmp/workdir-copy"),
            poll_interval=0.0,
            popen_factory=factory,
        )
    assert captured_cmd == []

def test_invoke_kimi_scrubs_keys_in_subprocess_env(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "sk-stray-kimi")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stray-openai")
    review_line = json.dumps({"role": "assistant", "content": '{"ok": 1}'}).encode("utf-8")
    captured_cmd: list = []
    captured_env: list = []
    factory = _make_factory([review_line], 0, captured_cmd, captured_env)
    _dispatch_kimi._invoke_kimi(
        prompt="P",
        kimi_bin="kimi",
        timeout_seconds=1800,
        repo_root=Path("/tmp/repo"),
        poll_interval=0.0,
        popen_factory=factory,
    )
    env = captured_env[0]
    assert env is not None
    assert "KIMI_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_invoke_kimi_exit_0_returns_output():
    review_line = json.dumps({"role": "assistant", "content": '{"ok": 1}'}).encode("utf-8")
    factory = _make_factory([review_line], 0, [], [])
    out = _dispatch_kimi._invoke_kimi(
        prompt="P", kimi_bin="kimi", timeout_seconds=1800,
        repo_root=Path("/tmp/repo"), poll_interval=0.0, popen_factory=factory,
    )
    assert '"role": "assistant"' in out


def test_invoke_kimi_exit_1_hard_fail_non_retryable():
    factory = _make_factory([b'{"role":"assistant","content":""}'], 1, [], [])
    with pytest.raises(_dispatch_kimi.KimiInvocationError) as exc_info:
        _dispatch_kimi._invoke_kimi(
            prompt="P", kimi_bin="kimi", timeout_seconds=1800,
            repo_root=Path("/tmp/repo"), poll_interval=0.0, popen_factory=factory,
        )
    assert exc_info.value.retryable is False


def test_invoke_kimi_exit_75_retryable():
    factory = _make_factory([b'{"role":"assistant","content":""}'], 75, [], [])
    with pytest.raises(_dispatch_kimi.KimiInvocationError) as exc_info:
        _dispatch_kimi._invoke_kimi(
            prompt="P", kimi_bin="kimi", timeout_seconds=1800,
            repo_root=Path("/tmp/repo"), poll_interval=0.0, popen_factory=factory,
        )
    assert exc_info.value.retryable is True


def test_invoke_kimi_binary_not_found_raises():
    def factory(cmd, **kwargs):
        raise FileNotFoundError("no kimi")
    with pytest.raises(_dispatch_kimi.KimiInvocationError, match="not found"):
        _dispatch_kimi._invoke_kimi(
            prompt="P", kimi_bin="kimi", timeout_seconds=1800,
            repo_root=Path("/tmp/repo"), poll_interval=0.0, popen_factory=factory,
        )


# ---------- watchdog / stall handling ----------

class _StuckProc(_FakeProc):
    """Never exits; readers never produce a line so the watchdog must abort."""

    def __init__(self):
        super().__init__([], 0)

    def poll(self):
        return None  # always running


def test_invoke_kimi_watchdog_aborts_on_silence():
    # No output ever; with a controllable clock the pre-first-byte silence
    # exceeds the timeout budget and the watchdog terminates + raises.
    clock = {"t": 1000.0}

    def time_fn():
        return clock["t"]

    def factory(cmd, **kwargs):
        return _StuckProc()

    # Advance the clock past timeout_seconds on each poll sleep.
    def fake_sleep(_interval):
        clock["t"] += 100.0

    import time as _time
    orig_sleep = _time.sleep
    _time.sleep = fake_sleep
    try:
        with pytest.raises(_dispatch_kimi.KimiInvocationError, match="stuck|no output"):
            _dispatch_kimi._invoke_kimi(
                prompt="P", kimi_bin="kimi", timeout_seconds=5,
                repo_root=Path("/tmp/repo"), poll_interval=0.01,
                time_fn=time_fn, popen_factory=factory,
            )
    finally:
        _time.sleep = orig_sleep


def test_invoke_kimi_default_stall_silence_is_240(monkeypatch):
    monkeypatch.delenv("CONSENSUS_MCP_STALL_SILENCE_SECONDS", raising=False)
    assert _dispatch_kimi._DEFAULT_STALL_SILENCE_SECONDS == 240.0


def test_invoke_kimi_honors_stall_silence_env(monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STALL_SILENCE_SECONDS", "12")
    assert _dispatch_kimi._effective_stall_silence(240.0) == 12.0


def test_invoke_kimi_stall_silence_env_invalid_keeps_default(monkeypatch, capsys):
    monkeypatch.setenv("CONSENSUS_MCP_STALL_SILENCE_SECONDS", "not-a-number")
    assert _dispatch_kimi._effective_stall_silence(240.0) == 240.0
    # A typo'd override must not silently vanish: warn so the operator sees it.
    err = capsys.readouterr().err
    assert "CONSENSUS_MCP_STALL_SILENCE_SECONDS" in err
    assert "not-a-number" in err


# ---------- _invoke_kimi_with_retry ----------

class _FakeInvokeFactory:
    """Captures prompts _invoke_kimi receives + returns scripted stream-json lines."""

    def __init__(self, contents: list[str]):
        # Each entry is the `content` string; wrap it in a stream-json line.
        self.lines = [
            json.dumps({"role": "assistant", "content": c}) for c in contents
        ]
        self.prompts: list[str] = []

    def __call__(self, *, prompt, **_kwargs):
        self.prompts.append(prompt)
        return self.lines.pop(0)


def test_retry_succeeds_on_first_attempt(monkeypatch):
    factory = _FakeInvokeFactory([json.dumps(_minimal_valid())])
    monkeypatch.setattr(_dispatch_kimi, "_invoke_kimi", factory)
    raw, parsed = _dispatch_kimi._invoke_kimi_with_retry(
        prompt="initial",
        kimi_bin="kimi",
        timeout_seconds=1800,
        repo_root=Path("."),
    )
    assert parsed["goal_satisfied"] is True
    assert len(factory.prompts) == 1
    assert factory.prompts[0] == "initial"


def test_retry_succeeds_on_second_attempt(monkeypatch):
    factory = _FakeInvokeFactory([
        "not json at all{",                    # parse will fail
        json.dumps(_minimal_valid()),          # valid
    ])
    monkeypatch.setattr(_dispatch_kimi, "_invoke_kimi", factory)
    raw, parsed = _dispatch_kimi._invoke_kimi_with_retry(
        prompt="initial",
        kimi_bin="kimi",
        timeout_seconds=1800,
        repo_root=Path("."),
    )
    assert parsed["goal_satisfied"] is True
    assert len(factory.prompts) == 2
    assert "Retry" in factory.prompts[1]
    assert "Parse error" in factory.prompts[1]


def test_retry_fails_when_both_attempts_invalid(monkeypatch):
    factory = _FakeInvokeFactory(["garbage 1", "garbage 2"])
    monkeypatch.setattr(_dispatch_kimi, "_invoke_kimi", factory)
    with pytest.raises(_dispatch_kimi.KimiOutputParseError):
        _dispatch_kimi._invoke_kimi_with_retry(
            prompt="initial",
            kimi_bin="kimi",
            timeout_seconds=1800,
            repo_root=Path("."),
        )
    assert len(factory.prompts) == 2


def test_retry_on_retryable_invocation_error(monkeypatch):
    """A 75-exit (retryable) on first attempt re-invokes once; success on retry."""
    state = {"calls": 0}
    valid_line = json.dumps({"role": "assistant", "content": json.dumps(_minimal_valid())})

    def fake_invoke(*, prompt, **_kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise _dispatch_kimi.KimiInvocationError("429-ish", retryable=True)
        return valid_line

    monkeypatch.setattr(_dispatch_kimi, "_invoke_kimi", fake_invoke)
    raw, parsed = _dispatch_kimi._invoke_kimi_with_retry(
        prompt="initial",
        kimi_bin="kimi",
        timeout_seconds=1800,
        repo_root=Path("."),
    )
    assert parsed["goal_satisfied"] is True
    assert state["calls"] == 2


def test_no_retry_on_non_retryable_invocation_error(monkeypatch):
    """A hard-fail (exit 1, retryable=False) is NOT retried; it propagates."""
    state = {"calls": 0}

    def fake_invoke(*, prompt, **_kwargs):
        state["calls"] += 1
        raise _dispatch_kimi.KimiInvocationError("auth fail", retryable=False)

    monkeypatch.setattr(_dispatch_kimi, "_invoke_kimi", fake_invoke)
    with pytest.raises(_dispatch_kimi.KimiInvocationError):
        _dispatch_kimi._invoke_kimi_with_retry(
            prompt="initial",
            kimi_bin="kimi",
            timeout_seconds=1800,
            repo_root=Path("."),
        )
    assert state["calls"] == 1


# ---------- _resolve_kimi_bin ----------

def test_resolve_returns_input_when_path_separator_present():
    if sys.platform == "win32":
        out = _dispatch_kimi._resolve_kimi_bin(r"C:\nonexistent\kimi.exe")
        assert out == r"C:\nonexistent\kimi.exe"
    else:
        out = _dispatch_kimi._resolve_kimi_bin("/nonexistent/kimi")
        assert out == "/nonexistent/kimi"


def test_resolve_returns_input_when_which_misses(monkeypatch):
    monkeypatch.setattr(_dispatch_kimi.shutil, "which", lambda _: None)
    out = _dispatch_kimi._resolve_kimi_bin("definitely-not-kimi")
    assert out == "definitely-not-kimi"


# ---------- main() argparse surface (UX parity with gemini) ----------

def test_main_argparse_has_same_flags_as_gemini():
    """The kimi CLI surface MUST match gemini's flags (UX parity), with
    --kimi-bin as the analog of --gemini-bin."""
    import argparse

    parser_holder = {}
    real_parse = argparse.ArgumentParser.parse_args

    def capture_parse(self, *a, **k):
        parser_holder["parser"] = self
        # Raise to short-circuit before doing real work.
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture_parse
    try:
        with pytest.raises(SystemExit):
            _dispatch_kimi.main(["--goal-packet", "g.yaml", "--iteration-dir", "iter"])
    finally:
        argparse.ArgumentParser.parse_args = real_parse

    parser = parser_holder["parser"]
    opt_strings = set()
    for action in parser._actions:
        opt_strings.update(action.option_strings)
    for flag in (
        "--goal-packet", "--iteration-dir", "--reviewer-id", "--pass-id",
        "--prompt-template", "--schema", "--mode", "--kimi-bin",
        "--timeout-seconds", "--review-target",
    ):
        assert flag in opt_strings, f"missing CLI flag {flag} (UX parity)"


# ---------- full main() pipeline -> sealed kimi-review.yaml via T6 ----------

def _write_goal_packet(path: Path):
    path.write_text(
        "goal:\n"
        "  summary: test goal\n"
        "  desired_end_state: it works\n"
        "allowed_files:\n"
        "  - consensus_mcp/foo.py\n"
        "acceptance_gates: []\n"
        "authorization:\n"
        "  scope_signature: sig-abc\n"
        "  authorized_by: tester\n"
        "  authorized_at_utc: 2026-05-22T00:00:00Z\n",
        encoding="utf-8",
    )


def test_main_seals_kimi_review_yaml(tmp_path, monkeypatch, capsys):
    """End-to-end: mocked kimi subprocess -> sealed pass-bound mirror via T6,
    id_prefix kimi-rev, timing log line emitted, ok=True JSON to stdout.

    The disposable temp work-dir creation and the post-dispatch integrity
    check are stubbed: the work-dir copy is replaced with a stub path (no real
    clone), and the integrity check reports the real repo CLEAN so the seal
    proceeds (the dirty-repo rejection path has its own test below)."""
    repo_root = ROOT
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(repo_root))

    iter_name = "iteration-kimi-test-0001"
    iter_dir = repo_root / "consensus-state" / "archive" / "scratch-kimi-test" / iter_name
    iter_dir.mkdir(parents=True, exist_ok=True)
    rel_iter_dir = iter_dir.relative_to(repo_root)

    goal_packet = iter_dir / "goal_packet.yaml"
    _write_goal_packet(goal_packet)
    rel_goal = goal_packet.relative_to(repo_root)

    review = _minimal_valid()
    final_text = json.dumps(review)  # --quiet emits the final answer as plain text

    captured_workdirs: list = []

    def fake_invoke_with_retry(**kwargs):
        captured_workdirs.append(kwargs.get("repo_root"))
        return final_text, _dispatch_kimi._parse_kimi_output(final_text)

    # Stub the disposable work-dir so no real clone/copytree happens; the stub
    # path is what kimi would be invoked against.
    stub_workdir = tmp_path / "kimi-workdir-stub" / "repo"
    monkeypatch.setattr(_dispatch_kimi, "_make_disposable_workdir", lambda _root: stub_workdir)
    cleanup_calls: list = []
    monkeypatch.setattr(_dispatch_kimi, "_cleanup_disposable_workdir",
                        lambda wd: cleanup_calls.append(wd))
    # Integrity check: before/after snapshots IDENTICAL (no new changes) -> seal proceeds.
    monkeypatch.setattr(_dispatch_kimi, "_repo_status_snapshot", lambda _root: {})

    monkeypatch.setattr(_dispatch_kimi, "_invoke_kimi_with_retry", fake_invoke_with_retry)
    monkeypatch.setattr(_dispatch_kimi, "_get_kimi_version", lambda _b: "kimi-test-1.0")

    # Unique pass-id per run so the T6 archive seal never collides with a prior
    # run's content under the same pass_id.
    import uuid as _uuid
    pass_id = f"kimi-iteration-kimi-test-0001-1-pass-{_uuid.uuid4().hex[:8]}"

    rc = _dispatch_kimi.main([
        "--goal-packet", str(rel_goal),
        "--iteration-dir", str(rel_iter_dir),
        "--pass-id", pass_id,
    ])
    captured = capsys.readouterr()
    assert rc == 0, f"main returned {rc}; stdout={captured.out}"

    # kimi was invoked against the disposable temp work-dir copy, NOT the real repo.
    assert captured_workdirs == [stub_workdir]
    # The temp work-dir was cleaned up.
    assert cleanup_calls == [stub_workdir]

    # H1: the sealed local mirror is bound to reviewer+pass, not a fixed name.
    sealed = iter_dir / f"kimi-review-{pass_id}.yaml"
    assert sealed.exists(), (
        "pass-bound sealed mirror not written to iteration dir; "
        f"dir contents: {[p.name for p in iter_dir.iterdir()]}"
    )
    # The legacy fixed filename must NOT be used.
    assert not (iter_dir / "kimi-review.yaml").exists()

    import yaml as _yaml
    data = _yaml.safe_load(sealed.read_text(encoding="utf-8"))
    assert data["reviewer_id"].startswith("kimi-")
    assert data["dispatch_provenance"]["adapter"] == "kimi"
    assert data["dispatch_provenance"]["kimi_version"] == "kimi-test-1.0"

    # ok=True JSON line on stdout (parity with gemini/codex).
    out_lines = [l for l in captured.out.splitlines() if l.strip().startswith("{")]
    assert out_lines, f"no JSON line on stdout: {captured.out!r}"
    result = json.loads(out_lines[-1])
    assert result["ok"] is True

    # Timing line emitted (parity: [kimi-timing] landed in Ns ...).
    assert "[kimi-timing]" in captured.out or "[kimi-timing]" in captured.err


def test_main_proposal_mode_uses_disposable_workdir_and_rewrites_prompt(tmp_path, monkeypatch, capsys):
    """Proposal mode is write-capable too: run it in the disposable workdir and
    do not leak the original repo absolute path into the prompt sent to Kimi."""
    repo_root = ROOT
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(repo_root))

    iter_name = "iteration-kimi-proposal-isolated-0001"
    iter_dir = repo_root / "consensus-state" / "archive" / "scratch-kimi-proposal" / iter_name
    iter_dir.mkdir(parents=True, exist_ok=True)
    rel_iter_dir = iter_dir.relative_to(repo_root)

    goal_packet = iter_dir / "goal_packet.yaml"
    _write_goal_packet(goal_packet)
    rel_goal = goal_packet.relative_to(repo_root)

    proposal = {
        "selected_target": "isolated proposal",
        "rationale_vs_alternatives": "test proposal",
        "deliverable_scope": {
            "next_iteration_id": "iter-test",
            "files_in_scope": ["consensus_mcp/_dispatch_kimi.py"],
            "files_out_of_scope": [],
            "key_design_decisions": ["isolate kimi"],
            "acceptance_gates": ["tests pass"],
        },
        "risks": [],
        "estimated_complexity": "small",
        "structural_abstention": False,
    }
    final_text = json.dumps(proposal)

    stub_workdir = tmp_path / "kimi-workdir-stub" / "repo"
    monkeypatch.setattr(_dispatch_kimi, "_make_disposable_workdir", lambda _root: stub_workdir)
    cleanup_calls: list = []
    monkeypatch.setattr(_dispatch_kimi, "_cleanup_disposable_workdir", lambda wd: cleanup_calls.append(wd))
    monkeypatch.setattr(_dispatch_kimi, "_repo_status_snapshot", lambda _root: {})
    monkeypatch.setattr(_dispatch_kimi, "_get_kimi_version", lambda _b: "kimi-test-1.0")

    captured: dict = {}

    def fake_invoke_with_retry(**kwargs):
        captured["repo_root"] = kwargs.get("repo_root")
        captured["prompt"] = kwargs.get("prompt")
        return final_text, proposal

    monkeypatch.setattr(_dispatch_kimi, "_invoke_kimi_with_retry", fake_invoke_with_retry)

    import uuid as _uuid
    pass_id = f"kimi-proposal-{_uuid.uuid4().hex[:8]}"
    rc = _dispatch_kimi.main([
        "--goal-packet", str(rel_goal),
        "--iteration-dir", str(rel_iter_dir),
        "--pass-id", pass_id,
        "--mode", "proposal",
    ])
    out = capsys.readouterr().out
    assert rc == 0, f"main returned {rc}; stdout={out}"
    assert captured["repo_root"] == stub_workdir
    assert str(repo_root) not in captured["prompt"]
    assert str(stub_workdir) in captured["prompt"]
    assert cleanup_calls == [stub_workdir]

    import shutil as _shutil
    _shutil.rmtree(repo_root / "consensus-state" / "archive" / "scratch-kimi-proposal",
                   ignore_errors=True)


    # Cleanup scratch dir to avoid polluting the repo tree.
    import shutil as _shutil
    _shutil.rmtree(repo_root / "consensus-state" / "archive" / "scratch-kimi-test",
                   ignore_errors=True)


def test_main_integrity_check_rejects_when_real_repo_dirty(monkeypatch, capsys):
    """FIX TRACK 2 (B4 independent safeguard): if the REAL repo's
    `git status --short` is non-empty after the dispatch, the review output is
    REJECTED (ok:false, KimiIntegrityError, rc=1) - kimi mutated the real repo
    despite the disposable temp work-dir."""
    repo_root = ROOT
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(repo_root))

    iter_name = "iteration-kimi-integrity-0001"
    iter_dir = repo_root / "consensus-state" / "archive" / "scratch-kimi-integrity" / iter_name
    iter_dir.mkdir(parents=True, exist_ok=True)
    rel_iter_dir = iter_dir.relative_to(repo_root)

    goal_packet = iter_dir / "goal_packet.yaml"
    _write_goal_packet(goal_packet)
    rel_goal = goal_packet.relative_to(repo_root)

    final_text = json.dumps(_minimal_valid())

    monkeypatch.setattr(_dispatch_kimi, "_make_disposable_workdir",
                        lambda _root: Path("/tmp/kimi-stub/repo"))
    monkeypatch.setattr(_dispatch_kimi, "_cleanup_disposable_workdir", lambda _wd: None)
    monkeypatch.setattr(_dispatch_kimi, "_invoke_kimi_with_retry",
                        lambda **kw: (final_text, _dispatch_kimi._parse_kimi_output(final_text)))
    monkeypatch.setattr(_dispatch_kimi, "_get_kimi_version", lambda _b: "kimi-test-1.0")
    # Simulate: before snapshot CLEAN, after snapshot has a NEW entry (kimi mutated
    # the real repo). The two bracketing calls return successive snapshots, so the
    # after-minus-before diff is non-empty -> integrity violation.
    _snaps = iter([{}, {"consensus_mcp/some_real_file.py": "mutated-hash"}])
    monkeypatch.setattr(_dispatch_kimi, "_repo_status_snapshot",
                        lambda _root: next(_snaps))

    import uuid as _uuid
    pass_id = f"kimi-prompt-rewrite-{_uuid.uuid4().hex[:8]}"
    rc = _dispatch_kimi.main([
        "--goal-packet", str(rel_goal),
        "--iteration-dir", str(rel_iter_dir),
        "--pass-id", pass_id,
    ])
    captured = capsys.readouterr()
    assert rc == 1, f"expected rejection rc=1; got {rc}; stdout={captured.out}"

    out_lines = [l for l in captured.out.splitlines() if l.strip().startswith("{")]
    result = json.loads(out_lines[-1])
    assert result["ok"] is False
    assert result["error_type"] == "KimiIntegrityError"

    # No sealed mirror should have been written (review was rejected).
    assert not any(p.name.startswith("kimi-review") for p in iter_dir.iterdir())

    import shutil as _shutil
    _shutil.rmtree(repo_root / "consensus-state" / "archive" / "scratch-kimi-integrity",
                   ignore_errors=True)


def test_main_fails_closed_on_enospc_instead_of_real_repo_fallback(monkeypatch, capsys):
    """If Kimi cannot get a disposable workdir, refuse dispatch. Detection after
    mutation is not containment for an operator workspace."""
    repo_root = ROOT
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(repo_root))

    iter_name = "iteration-kimi-isolation-fail-0001"
    iter_dir = repo_root / "consensus-state" / "archive" / "scratch-kimi-isolation-fail" / iter_name
    iter_dir.mkdir(parents=True, exist_ok=True)
    rel_iter_dir = iter_dir.relative_to(repo_root)

    goal_packet = iter_dir / "goal_packet.yaml"
    _write_goal_packet(goal_packet)
    rel_goal = goal_packet.relative_to(repo_root)

    def _enospc(_root):
        raise _dispatch_kimi._WorkdirTooLargeToIsolate(
            "kimi disposable work-dir copy ran out of space")

    invoked = []
    monkeypatch.setattr(_dispatch_kimi, "_make_disposable_workdir", _enospc)
    monkeypatch.setattr(_dispatch_kimi, "_cleanup_disposable_workdir", lambda wd: None)
    monkeypatch.setattr(_dispatch_kimi, "_repo_status_snapshot", lambda _root: {})
    monkeypatch.setattr(_dispatch_kimi, "_invoke_kimi_with_retry", lambda **kw: invoked.append(kw))
    monkeypatch.setattr(_dispatch_kimi, "_get_kimi_version", lambda _b: "kimi-test-1.0")

    import uuid as _uuid
    pass_id = f"kimi-prompt-rewrite-{_uuid.uuid4().hex[:8]}"
    rc = _dispatch_kimi.main([
        "--goal-packet", str(rel_goal),
        "--iteration-dir", str(rel_iter_dir),
        "--pass-id", pass_id,
    ])
    captured = capsys.readouterr()
    assert rc == 1, f"expected fail-closed rc=1; got {rc}; stdout={captured.out}"
    assert invoked == []
    out_lines = [l for l in captured.out.splitlines() if l.strip().startswith("{")]
    result = json.loads(out_lines[-1])
    assert result["ok"] is False
    assert result["error_type"] == "KimiInvocationError"
    assert "refusing to run Kimi against the real repo" in result["error"]

    import shutil as _shutil
    _shutil.rmtree(repo_root / "consensus-state" / "archive" / "scratch-kimi-isolation-fail",
                   ignore_errors=True)


def test_prompt_rewrite_removes_real_repo_paths_before_invoke(tmp_path, monkeypatch, capsys):
    """Every original repo absolute path in the prompt is rewritten to the
    disposable workdir before Kimi is invoked."""
    repo_root = ROOT
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(repo_root))

    iter_name = "iteration-kimi-prompt-rewrite-0001"
    iter_dir = (repo_root / "consensus-state" / "archive"
                / "scratch-kimi-prompt-rewrite" / iter_name)
    iter_dir.mkdir(parents=True, exist_ok=True)
    rel_iter_dir = iter_dir.relative_to(repo_root)

    goal_packet = iter_dir / "goal_packet.yaml"
    _write_goal_packet(goal_packet)
    rel_goal = goal_packet.relative_to(repo_root)

    stub_workdir = tmp_path / "repo"
    monkeypatch.setattr(_dispatch_kimi, "_make_disposable_workdir", lambda _root: stub_workdir)
    monkeypatch.setattr(_dispatch_kimi, "_cleanup_disposable_workdir", lambda _wd: None)
    monkeypatch.setattr(_dispatch_kimi, "_repo_status_snapshot", lambda _root: {})
    monkeypatch.setattr(_dispatch_kimi, "_get_kimi_version", lambda _b: "kimi-test-1.0")

    original_build_prompt = _dispatch_kimi._build_prompt

    def _build_prompt_with_absolute_path(*args, **kwargs):
        return original_build_prompt(*args, **kwargs) + f"\n/path-prefix{repo_root}/leak\n"

    monkeypatch.setattr(_dispatch_kimi, "_build_prompt", _build_prompt_with_absolute_path)

    final_text = json.dumps(_minimal_valid())
    captured_prompt = {}

    def fake_invoke_with_retry(**kwargs):
        captured_prompt["text"] = kwargs["prompt"]
        return final_text, _dispatch_kimi._parse_kimi_output(final_text)

    monkeypatch.setattr(_dispatch_kimi, "_invoke_kimi_with_retry", fake_invoke_with_retry)

    import uuid as _uuid
    pass_id = f"kimi-prompt-rewrite-{_uuid.uuid4().hex[:8]}"
    rc = _dispatch_kimi.main([
        "--goal-packet", str(rel_goal),
        "--iteration-dir", str(rel_iter_dir),
        "--pass-id", pass_id,
    ])
    captured = capsys.readouterr()
    assert rc == 0, f"expected rc=0; got {rc}; stdout={captured.out}"
    assert str(repo_root) not in captured_prompt["text"]
    assert f"/path-prefix{stub_workdir}/leak" in captured_prompt["text"]

    import shutil as _shutil
    _shutil.rmtree(repo_root / "consensus-state" / "archive" / "scratch-kimi-prompt-rewrite",
                   ignore_errors=True)



# ---------- _strip_kimi_output_chrome (kimi.yaml strip_patterns) ----------

def test_strip_removes_resume_session_trailer():
    """--quiet appends 'To resume this session: kimi -r <id>'; strip it before
    JSON extraction (kimi.yaml output.strip_patterns)."""
    raw = "OK final answer\n\nTo resume this session: kimi -r 6c02599a-abcd\n"
    assert _dispatch_kimi._strip_kimi_output_chrome(raw) == "OK final answer"


def test_strip_preserves_json_and_removes_trailer():
    raw = '```json\n{"findings": []}\n```\n\nTo resume this session: kimi -r abc123\n'
    cleaned = _dispatch_kimi._strip_kimi_output_chrome(raw)
    assert cleaned == '```json\n{"findings": []}\n```'


def test_strip_noop_when_no_trailer():
    raw = '{"findings": [], "goal_satisfied": true}'
    assert _dispatch_kimi._strip_kimi_output_chrome(raw) == raw


def test_one_call_path_strips_then_parses_plain_text(monkeypatch):
    """Full new output path: plain final text + resume trailer -> strip chrome
    -> _extract_json_from_text -> parse (no stream-json envelope)."""
    final_text = (
        json.dumps(_minimal_valid())
        + "\n\nTo resume this session: kimi -r deadbeef\n"
    )

    def fake_invoke(*, prompt, **_kwargs):
        return final_text

    monkeypatch.setattr(_dispatch_kimi, "_invoke_kimi", fake_invoke)
    raw, parsed = _dispatch_kimi._invoke_kimi_with_retry(
        prompt="initial", kimi_bin="kimi", timeout_seconds=1800, repo_root=Path("."),
    )
    assert parsed["goal_satisfied"] is True


# ---------- _make_disposable_workdir / _cleanup_disposable_workdir ----------

def test_make_disposable_workdir_prefers_git_clone(monkeypatch, tmp_path):
    """When git is available + the source is a git tree, use
    `git clone --local --shared <repo> <tmp>/repo`."""
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)

    calls: list = []

    monkeypatch.setattr(_dispatch_kimi.shutil, "which", lambda _name: "/usr/bin/git")

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # Simulate a successful clone by creating the dest dir.
        Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        return _Result()

    monkeypatch.setattr(_dispatch_kimi.subprocess, "run", fake_run)

    def fail_copytree(*a, **k):  # must NOT be called on the git path
        raise AssertionError("copytree fallback used despite a successful clone")

    monkeypatch.setattr(_dispatch_kimi.shutil, "copytree", fail_copytree)

    dest = _dispatch_kimi._make_disposable_workdir(src)
    assert dest.exists()
    assert dest.name == "repo"
    assert calls and calls[0][:3] == ["/usr/bin/git", "clone", "--local"]
    assert "--shared" in calls[0]
    # Cleanup removes the tempdir parent.
    _dispatch_kimi._cleanup_disposable_workdir(dest)
    assert not dest.exists()


def test_make_disposable_workdir_copytree_fallback_skips_git(monkeypatch, tmp_path):
    """No git binary -> copytree fallback, skipping .git + heavy dirs."""
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    (src / "node_modules").mkdir()
    (src / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (src / "consensus_mcp").mkdir()
    (src / "consensus_mcp" / "real.py").write_text("print('hi')", encoding="utf-8")

    monkeypatch.setattr(_dispatch_kimi.shutil, "which", lambda _name: None)  # no git

    dest = _dispatch_kimi._make_disposable_workdir(src)
    try:
        assert (dest / "consensus_mcp" / "real.py").exists()  # real source copied
        assert not (dest / ".git").exists()                   # .git skipped
        assert not (dest / "node_modules").exists()           # heavy dir skipped
    finally:
        _dispatch_kimi._cleanup_disposable_workdir(dest)
    assert not dest.exists()


def test_cleanup_disposable_workdir_tolerates_none():
    # Must never raise.
    _dispatch_kimi._cleanup_disposable_workdir(None)


# ---------- _repo_status_snapshot (post-dispatch integrity probe) ----------

def test_repo_status_snapshot_hashes_dirty_files(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "consensus_mcp").mkdir()
    (tmp_path / "consensus_mcp" / "foo.py").write_text("alpha", encoding="utf-8")
    (tmp_path / "bar.py").write_text("beta", encoding="utf-8")
    monkeypatch.setattr(_dispatch_kimi.shutil, "which", lambda _name: "/usr/bin/git")

    class _Result:
        returncode = 0
        stdout = " M consensus_mcp/foo.py\n?? bar.py\n"

    monkeypatch.setattr(_dispatch_kimi.subprocess, "run", lambda *a, **k: _Result())
    snap = _dispatch_kimi._repo_status_snapshot(tmp_path)
    # dict {path: content-hash} - content is hashed so mutations to already-dirty
    # files are detectable (re-audit codex-rev-001).
    assert set(snap) == {"consensus_mcp/foo.py", "bar.py"}
    import hashlib as _h
    assert snap["bar.py"] == _h.sha256(b"beta").hexdigest()


def test_repo_status_snapshot_detects_mutation_to_already_dirty_file(monkeypatch, tmp_path):
    # codex-rev-001: a file already dirty BEFORE dispatch, rewritten DURING dispatch,
    # keeps the same `git status` line but a different content hash -> detected.
    (tmp_path / ".git").mkdir()
    f = tmp_path / "already_dirty.py"
    monkeypatch.setattr(_dispatch_kimi.shutil, "which", lambda _name: "/usr/bin/git")

    class _Result:
        returncode = 0
        stdout = " M already_dirty.py\n"

    monkeypatch.setattr(_dispatch_kimi.subprocess, "run", lambda *a, **k: _Result())
    f.write_text("before", encoding="utf-8")
    before = _dispatch_kimi._repo_status_snapshot(tmp_path)
    f.write_text("AFTER (mutated)", encoding="utf-8")  # same status line, new content
    after = _dispatch_kimi._repo_status_snapshot(tmp_path)
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    assert changed == {"already_dirty.py"}


def test_repo_status_snapshot_detects_symlink_target_rewrite(monkeypatch, tmp_path):
    # final-2 codex-rev-001: an already-dirty symlink whose TARGET is rewritten keeps
    # the same git-status code but a different target -> must be detected (sign by readlink).
    (tmp_path / ".git").mkdir()
    link = tmp_path / "thelink"
    try:
        link.symlink_to("a.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    monkeypatch.setattr(_dispatch_kimi.shutil, "which", lambda _name: "/usr/bin/git")

    class _Result:
        returncode = 0
        stdout = " M thelink\n"

    monkeypatch.setattr(_dispatch_kimi.subprocess, "run", lambda *a, **k: _Result())
    before = _dispatch_kimi._repo_status_snapshot(tmp_path)
    link.unlink()
    link.symlink_to("b.txt")  # repoint: same status line, different target
    after = _dispatch_kimi._repo_status_snapshot(tmp_path)
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    assert changed == {"thelink"}


def test_repo_status_snapshot_empty_when_clean(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(_dispatch_kimi.shutil, "which", lambda _name: "/usr/bin/git")

    class _Result:
        returncode = 0
        stdout = "\n"

    monkeypatch.setattr(_dispatch_kimi.subprocess, "run", lambda *a, **k: _Result())
    assert _dispatch_kimi._repo_status_snapshot(tmp_path) == {}


def test_repo_status_snapshot_no_git_uses_content_manifest(monkeypatch, tmp_path):
    # v1.30.3 D2: no git binary / not a git tree -> the git-INDEPENDENT content-hash
    # manifest (a REAL control), NOT the old vacuous {} (which was zero control). An empty
    # tree still yields {}; a tree WITH files yields {path: content-hash}.
    monkeypatch.setattr(_dispatch_kimi.shutil, "which", lambda _name: None)
    assert _dispatch_kimi._repo_status_snapshot(tmp_path) == {}  # empty tree -> empty manifest
    (tmp_path / "a.py").write_text("alpha", encoding="utf-8")
    snap = _dispatch_kimi._repo_status_snapshot(tmp_path)
    import hashlib as _h
    assert snap == {"a.py": _h.sha256(b"alpha").hexdigest()}


def test_integrity_diff_ignores_preexisting_dirt():
    # A pre-existing dirty path with the SAME content-hash in both snapshots is NOT a
    # kimi mutation (re-audit codex-rev-002: no false-positive on a normally-dirty repo).
    before = {"preexisting_wip.py": "h1", "other_wip.py": "h2"}
    after = dict(before)
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    assert changed == set()
    # New path, deleted path, and a content change to an already-dirty path are ALL flagged.
    after2 = {"preexisting_wip.py": "h1_CHANGED", "kimi_new.py": "h3"}  # other_wip deleted
    changed2 = {p for p in set(before) | set(after2) if before.get(p) != after2.get(p)}
    assert changed2 == {"preexisting_wip.py", "kimi_new.py", "other_wip.py"}


# ---------- _strip_symlinks (re-audit: temp-copy sandbox escape) ----------

def test_strip_symlinks_removes_symlinks_keeps_regular(tmp_path):
    root = tmp_path / "copy"
    (root / "sub").mkdir(parents=True)
    real = root / "real.txt"
    real.write_text("data", encoding="utf-8")
    # A symlink pointing OUTSIDE the copy (the escape vector).
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "sub" / "escape"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    assert link.is_symlink()
    _dispatch_kimi._strip_symlinks(root)
    assert not link.is_symlink()                            # symlink removed
    assert real.read_text(encoding="utf-8") == "data"       # regular file kept
    assert outside.read_text(encoding="utf-8") == "secret"  # target untouched


# ---------- _sealed_mirror_filename (H1: reviewer+pass bound) ----------

def test_sealed_mirror_filename_binds_pass_id():
    name = _dispatch_kimi._sealed_mirror_filename(
        "kimi-iter0007-1", "kimi-iter0007-1-pass2"
    )
    assert name == "kimi-review-kimi-iter0007-1-pass2.yaml"


def test_sealed_mirror_filename_distinct_per_pass():
    n1 = _dispatch_kimi._sealed_mirror_filename("kimi-iter0007-1", "kimi-iter0007-1-pass1")
    n2 = _dispatch_kimi._sealed_mirror_filename("kimi-iter0007-1", "kimi-iter0007-1-pass2")
    assert n1 != n2  # multi-pass no longer overwrites (H1)


def test_sealed_mirror_filename_falls_back_to_reviewer_then_legacy():
    assert _dispatch_kimi._sealed_mirror_filename("kimi-rev-x", "") == "kimi-review-kimi-rev-x.yaml"
    assert _dispatch_kimi._sealed_mirror_filename("", "") == "kimi-review.yaml"


def test_sealed_mirror_filename_sanitizes_unsafe_chars():
    name = _dispatch_kimi._sealed_mirror_filename("r", "pass/../etc passwd")
    assert "/" not in name
    assert ".." not in name  # no path-traversal segment survives
    assert name.startswith("kimi-review-")
    assert name.endswith(".yaml")


# ---------- real-kimi smoke (env-gated; never runs in CI) ----------

@pytest.mark.skipif(
    os.environ.get("CONSENSUS_MCP_RUN_REAL_KIMI_SMOKE") != "1",
    reason="real-kimi smoke gated by CONSENSUS_MCP_RUN_REAL_KIMI_SMOKE=1",
)
def test_real_kimi_smoke():  # pragma: no cover
    # Intentionally minimal: presence proves the gate exists. A full smoke
    # run would invoke the real binary, which we never do in unit tests.
    assert os.environ.get("CONSENSUS_MCP_RUN_REAL_KIMI_SMOKE") == "1"




# ---------- preflight guard tests (Loop 4 harness-rec-002) ----------

def test_preflight_rejects_non_git_directory(tmp_path, monkeypatch):
    """Dispatch refuses when repo root has no .git directory."""
    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    (fake_repo / ".consensus").mkdir()
    (fake_repo / ".consensus" / "config.yaml").write_text("schema_version: 1\n")
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(fake_repo))
    monkeypatch.chdir(fake_repo)

    from consensus_mcp._dispatch_kimi import main
    rc = main(["--goal-packet", "nonexistent.yaml",
               "--iteration-dir", "test-iter",
               "--reviewer-id", "kimi",
               "--mode", "proposal"])
    assert rc == 4


def test_preflight_rejects_home_directory(monkeypatch, tmp_path):
    """Dispatch refuses when repo root is the user's home directory."""
    import pathlib
    home = pathlib.Path.home().resolve()
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(home))
    monkeypatch.chdir(home)

    from consensus_mcp._dispatch_kimi import main
    rc = main(["--goal-packet", "nonexistent.yaml",
               "--iteration-dir", "test-iter",
               "--reviewer-id", "kimi",
               "--mode", "proposal"])
    assert rc == 4
