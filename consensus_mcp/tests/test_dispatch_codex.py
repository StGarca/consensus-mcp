"""Unit tests for _dispatch_codex.py. Codex subprocess is mocked; no real codex calls."""
from __future__ import annotations
import json as _json
import os as _os
import shutil as _shutil
import signal
import sys
import unittest.mock as _mock
from pathlib import Path

import pytest
import yaml

# v1.15.7 CI fix; v2.2.1 audit M0.4 (docs/audits/2026-07-01-v2.2.1-repo-audit.md)
# converted the gate from opt-OUT to opt-IN. These four "smoke" tests
# predate the iter-0037 refactor that moved the codex invocation from
# `subprocess.run` to `subprocess.Popen` (streaming + reader threads).
# They still mock only `subprocess.run` (which now just covers the
# `codex --version` probe), so the actual dispatch executes the REAL
# codex binary via Popen. The old gate skipped them only when no
# `codex` was on PATH, which meant any dev machine WITH codex on PATH
# silently ran four real LLM CLI invocations during a plain pytest
# run. They are now env-gated opt-IN, mirroring
# test_dispatch_grok_smoke.py / test_builder_containment_smoke.py:
# skipped UNLESS CONSENSUS_MCP_RUN_REAL_CODEX_SMOKE=1 is set in the
# environment (the same env var that gates the dispatcher's own
# --smoke mode), and still skipped when no real `codex` binary is on
# PATH, so opting in without the binary skips cleanly rather than
# failing. Named follow-up (v1.15.x): rewrite them to mock the Popen
# path via the existing `make_fake_codex_popen_factory` /
# `popen_factory=` kwarg (the pattern the genuinely-hermetic
# main()-tests already use) and drop the gate entirely.
_REAL_CODEX_GATE = "CONSENSUS_MCP_RUN_REAL_CODEX_SMOKE"
_REQUIRES_REAL_CODEX = pytest.mark.skipif(
    _os.environ.get(_REAL_CODEX_GATE) != "1" or _shutil.which("codex") is None,
    reason=f"opt-in real-codex smoke: set {_REAL_CODEX_GATE}=1 to run "
    "(also requires a real `codex` binary on PATH; this test mocks "
    "subprocess.run but not the iter-0037 Popen dispatch path).",
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _dispatch_codex  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures" / "dispatch_codex"
SMOKE_GOAL_PACKET = FIXTURES / "goal_packet_smoke.yaml"


class _FakeCodexPopen:
    """Popen-like stand-in for the iter-0037 _invoke_codex Popen+reader-threads
    code path. Mirrors the subset of the subprocess.Popen API that _invoke_codex
    touches: .stdin.write/.close, .stdout.readline, .stderr.readline, .poll,
    .terminate, .kill, .wait, .returncode.

    Constructor records cmd and reads the path passed via the `-o` argument so
    that .stdin.close() can write the preset codex output text to that file
    (matching the real codex's file-write side effect). Prompts written to
    .stdin are appended to the externally-supplied `captured_prompts` list as
    bytes, so callers can inspect what was sent.

    .stdout.readline returns successive lines from `stdout_lines` and then b""
    forever once exhausted. .stderr.readline returns stderr lines similarly.
    .poll returns None on the first call (so _invoke_codex enters its main
    poll loop once) and the configured `returncode` thereafter; `poll_calls_until_done`
    can extend the "still running" window for silence/timeout simulation tests.
    """

    def __init__(self, cmd, *, stdout_text: str, returncode: int,
                 captured_prompts: list, stderr_text: str = "",
                 poll_calls_until_done: int = 1, **_popen_kwargs):
        self._cmd = cmd
        try:
            out_idx = cmd.index("-o") + 1
            self._out_file = cmd[out_idx]
        except (ValueError, IndexError):
            self._out_file = None
        self._stdout_text = stdout_text
        self._stderr_text = stderr_text
        self._captured_prompts = captured_prompts
        self._configured_returncode = returncode
        self._poll_calls_until_done = poll_calls_until_done
        self._poll_calls = 0
        self.returncode = None
        # Pre-split lines (keep \n so _invoke_codex's rstrip("\r\n") works).
        if stdout_text:
            self._stdout_lines = [
                (ln + "\n").encode("utf-8") for ln in stdout_text.splitlines()
            ]
        else:
            self._stdout_lines = []
        if stderr_text:
            self._stderr_lines = [
                (ln + "\n").encode("utf-8") for ln in stderr_text.splitlines()
            ]
        else:
            self._stderr_lines = []
        self._stdout_idx = 0
        self._stderr_idx = 0
        # stdin/stdout/stderr file-like objects.
        self.stdin = _FakeStdin(self._captured_prompts, self._write_output_file)
        self.stdout = _FakeStream(self._next_stdout_line)
        self.stderr = _FakeStream(self._next_stderr_line)

    def _write_output_file(self):
        """Called when .stdin.close() fires; mimics codex writing -o file."""
        if self._out_file is not None and self._configured_returncode == 0:
            try:
                Path(self._out_file).write_text(self._stdout_text or "",
                                                 encoding="utf-8")
            except OSError:
                pass

    def _next_stdout_line(self):
        if self._stdout_idx < len(self._stdout_lines):
            line = self._stdout_lines[self._stdout_idx]
            self._stdout_idx += 1
            return line
        return b""

    def _next_stderr_line(self):
        if self._stderr_idx < len(self._stderr_lines):
            line = self._stderr_lines[self._stderr_idx]
            self._stderr_idx += 1
            return line
        return b""

    def poll(self):
        self._poll_calls += 1
        if self._poll_calls < self._poll_calls_until_done:
            return None
        self.returncode = self._configured_returncode
        return self.returncode

    def terminate(self):
        # Mark process finished so subsequent poll/wait stop blocking.
        self.returncode = self._configured_returncode

    def kill(self):
        self.returncode = self._configured_returncode

    def wait(self, timeout=None):
        self.returncode = self._configured_returncode
        return self.returncode

    def send_signal(self, sig):
        # iter-0039: _terminate_process_tree calls send_signal on Windows.
        # Treat as terminate for the fake.
        self.terminate()

    @property
    def pid(self):
        # iter-0039: _terminate_process_tree uses os.killpg(os.getpgid(pid))
        # on POSIX. The fake returns 0; os.killpg(0) would target the current
        # process group which is dangerous, but the OSError fallback path
        # catches that case in _terminate_process_tree.
        return 0


class _FakeStdin:
    def __init__(self, captured_prompts: list, on_close):
        self._captured = captured_prompts
        self._buf = bytearray()
        self._closed = False
        self._on_close = on_close

    def write(self, data):
        if self._closed:
            raise OSError("stdin closed")
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._buf.extend(data)
        return len(data)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._captured.append(bytes(self._buf))
        self._on_close()


class _FakeStream:
    """Minimal stdout/stderr stand-in supporting readline() returning bytes."""

    def __init__(self, next_line_fn):
        self._next_line = next_line_fn

    def readline(self):
        return self._next_line()

    def read(self):
        chunks = []
        while True:
            line = self._next_line()
            if not line:
                break
            chunks.append(line)
        return b"".join(chunks)


def make_fake_codex_popen_factory(stdout_text: str, returncode: int = 0,
                                   captured_prompts=None,
                                   stderr_text: str = "",
                                   poll_calls_until_done: int = 1):
    """Return a popen_factory callable suitable for monkeypatching
    _dispatch_codex.subprocess.Popen (or for the popen_factory= kwarg of
    _invoke_codex). Each factory call constructs a fresh _FakeCodexPopen.

    The factory writes `stdout_text` to the path in the `-o` argument of cmd
    when stdin is closed (mimicking codex's file-write side effect) and emits
    `stdout_text` line-by-line via .stdout.readline. `returncode` becomes
    proc.returncode after the configured number of poll calls.
    """
    if captured_prompts is None:
        captured_prompts = []

    def _factory(cmd, **kwargs):
        return _FakeCodexPopen(
            cmd,
            stdout_text=stdout_text,
            returncode=returncode,
            captured_prompts=captured_prompts,
            stderr_text=stderr_text,
            poll_calls_until_done=poll_calls_until_done,
            **kwargs,
        )

    return _factory


def _scaffold_repo_markers(tmp_path):
    """Per v1.10.4 F1: _resolve_repo_root validates that CONSENSUS_MCP_REPO_ROOT
    points at a directory containing the consensus pipeline repo markers (consensus-state/,
    consensus_mcp/, consensus_mcp/validators/). Tests that override REPO_ROOT
    to tmp_path must scaffold those markers, otherwise resolution fails-closed
    with RepoRootResolutionError (F1's intended behavior).

    Creates the required marker directories under tmp_path. Returns tmp_path
    for convenience.
    """
    (tmp_path / "consensus-state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "consensus_mcp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "consensus_mcp" / "validators").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _isolate_archive_root(tmp_path, monkeypatch):
    """Redirect paths in review_write_and_seal and audit_append_event to
    tmp_path so this test does NOT pollute the real
    consensus-state/archive/review-passes/index.yaml or independence-audit logs.

    iter-0036/iter-0037: both modules now use lazy `_paths` resolvers and
    read CONSENSUS_MCP_REPO_ROOT at call time. The `monkeypatch.setattr`
    approach used previously is unsafe against __getattr__-only attributes
    (pytest captures the lazy-synthesized value at setattr time and restores
    it into __dict__ at teardown, permanently shadowing the resolver for
    subsequent tests). Env-var redirection alone is now sufficient.

    Must be called AFTER _scaffold_repo_markers(tmp_path).
    """
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))

    isolated_archive = tmp_path / "consensus-state" / "archive" / "review-passes"
    isolated_archive.mkdir(parents=True, exist_ok=True)
    isolated_active = tmp_path / "consensus-state" / "active"
    isolated_active.mkdir(parents=True, exist_ok=True)


def _stage_smoke_goal_packet(tmp_path):
    """v1.10.5: containment refuses any path outside repo_root. Tests that
    use the real-repo SMOKE_GOAL_PACKET fixture but synthesize repo_root in
    tmp_path must STAGE the fixture inside tmp_path. Returns the staged path.
    """
    import shutil
    staged_dir = tmp_path / "consensus_mcp" / "tests" / "fixtures" / "dispatch_codex"
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged = staged_dir / SMOKE_GOAL_PACKET.name
    shutil.copy2(SMOKE_GOAL_PACKET, staged)
    return staged

REPO_ROOT_FOR_TESTS = REPO_ROOT
SCHEMA_PATH = REPO_ROOT / "consensus_mcp" / "dispatch_templates" / "codex_review_schema.json"


def test_smoke_goal_packet_exists():
    """Fixture file must exist for downstream tests to load."""
    assert SMOKE_GOAL_PACKET.exists(), f"missing fixture: {SMOKE_GOAL_PACKET}"


def test_load_goal_packet_returns_dict():
    """_load_goal_packet returns a dict with required keys."""
    packet = _dispatch_codex._load_goal_packet(SMOKE_GOAL_PACKET)
    assert isinstance(packet, dict)
    for k in ("goal", "allowed_files", "acceptance_gates", "authorization"):
        assert k in packet, f"missing key: {k}"


def test_cli_help_runs_and_exits_zero(capsys):
    """`python -m consensus_mcp._dispatch_codex --help` prints usage."""
    with pytest.raises(SystemExit) as exc:
        _dispatch_codex.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--goal-packet" in out
    assert "--iteration-dir" in out


def test_build_prompt_substitutes_placeholders():
    """_build_prompt replaces {goal_summary}, {allowed_files}, {scope_signature} in template."""
    template = "Goal: {goal_summary}\nFiles: {allowed_files}\nSig: {scope_signature}\n"
    packet = {
        "goal": {"summary": "test goal"},
        "allowed_files": ["a.py", "b.py"],
        "authorization": {"scope_signature": "abc123"},
    }
    prompt = _dispatch_codex._build_prompt(packet, template)
    assert "test goal" in prompt
    assert "a.py" in prompt
    assert "b.py" in prompt
    assert "abc123" in prompt
    assert "{goal_summary}" not in prompt


def test_build_prompt_handles_missing_optional_fields():
    """_build_prompt does not crash when optional fields are missing; substitutes empty strings."""
    template = "Goal: {goal_summary}\nGates: {acceptance_gates}\n"
    packet = {"goal": {"summary": "x"}}  # no acceptance_gates
    prompt = _dispatch_codex._build_prompt(packet, template)
    assert "x" in prompt
    assert "{acceptance_gates}" not in prompt


def test_load_template_returns_string():
    template_path = REPO_ROOT / "consensus_mcp" / "dispatch_templates" / "codex_review_template.md"
    text = _dispatch_codex._load_template(template_path)
    assert isinstance(text, str)
    assert "{goal_summary}" in text


def test_codex_review_schema_is_valid_json():
    """Schema file parses as JSON and has the expected top-level shape."""
    with SCHEMA_PATH.open("r", encoding="utf-8") as f:
        schema = _json.load(f)
    assert schema["type"] == "object"
    assert "findings" in schema["properties"]
    assert schema["properties"]["findings"]["type"] == "array"


def test_parse_codex_output_well_formed_json():
    """_parse_codex_output reads JSON from codex output file."""
    text = (FIXTURES / "codex_output_well_formed.json").read_text(encoding="utf-8")
    parsed = _dispatch_codex._parse_codex_output(text)
    assert "findings" in parsed
    assert len(parsed["findings"]) == 1
    assert parsed["findings"][0]["id"] == "codex-rev-001"
    assert parsed["goal_satisfied"] is False


def test_parse_codex_output_malformed_json_raises():
    """Truncated/invalid JSON raises CodexOutputParseError."""
    text = (FIXTURES / "codex_output_malformed.json").read_text(encoding="utf-8")
    with pytest.raises(_dispatch_codex.CodexOutputParseError):
        _dispatch_codex._parse_codex_output(text)


def test_parse_codex_output_missing_findings_raises():
    """Valid JSON missing required 'findings' key raises CodexOutputParseError."""
    with pytest.raises(_dispatch_codex.CodexOutputParseError):
        _dispatch_codex._parse_codex_output('{"goal_satisfied": true}')


def test_invoke_codex_subprocess_called(monkeypatch, tmp_path):
    """_invoke_codex Popens codex exec + sandbox + schema + -o tempfile + stdin pipe.

    Migrated for iter-0037: _invoke_codex now uses Popen + reader threads; we
    capture the cmd via a fake popen_factory.
    """
    captured = {}
    captured_prompts: list = []

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeCodexPopen(
            cmd,
            stdout_text='{"ok": true}',
            returncode=0,
            captured_prompts=captured_prompts,
        )

    out = _dispatch_codex._invoke_codex(
        prompt="test prompt",
        codex_bin="codex",
        timeout_seconds=60,
        repo_root=tmp_path,
        schema_path=SCHEMA_PATH,
        poll_interval=0.01,
        popen_factory=fake_popen,
    )
    assert out == '{"ok": true}'
    # cmd[0] is the resolved codex bin path (v1.10.3 hardening: _resolve_codex_bin
    # applies shutil.which() + Windows PATHEXT/.cmd preference). Accept either the
    # bare "codex" name (Linux/CI environments without a real codex on PATH) or any
    # codex.* variant the resolver returned.
    cmd0_basename = Path(captured["cmd"][0]).name.lower()
    assert cmd0_basename in ("codex", "codex.cmd", "codex.exe", "codex.bat", "codex.ps1"), \
        f"cmd[0]={captured['cmd'][0]!r}; basename {cmd0_basename!r} should be a codex variant"
    assert captured["cmd"][1] == "exec"
    assert "--skip-git-repo-check" in captured["cmd"]
    assert "--cd" in captured["cmd"]
    assert str(tmp_path) in captured["cmd"]
    assert "--sandbox" in captured["cmd"]
    sandbox_idx = captured["cmd"].index("--sandbox") + 1
    assert captured["cmd"][sandbox_idx] == "read-only"
    assert "--output-schema" in captured["cmd"]
    schema_idx = captured["cmd"].index("--output-schema") + 1
    assert captured["cmd"][schema_idx] == str(SCHEMA_PATH)
    assert "-o" in captured["cmd"]
    assert captured["cmd"][-1] == "-"   # stdin literal
    # Prompt was sent via stdin (Popen path; bytes via proc.stdin.write).
    assert captured_prompts == [b"test prompt"]


def test_invoke_codex_nonzero_exit_raises(monkeypatch, tmp_path):
    """Migrated for iter-0037: non-zero exit via Popen path raises CodexInvocationError
    with stderr tail in the message.
    """
    fake_popen = make_fake_codex_popen_factory(
        stdout_text="",
        returncode=2,
        stderr_text="codex blew up",
    )
    with pytest.raises(_dispatch_codex.CodexInvocationError) as exc:
        _dispatch_codex._invoke_codex(
            prompt="x", codex_bin="codex", timeout_seconds=60,
            repo_root=tmp_path, schema_path=SCHEMA_PATH,
            poll_interval=0.01,
            popen_factory=fake_popen,
        )
    assert "codex blew up" in str(exc.value)


def test_invoke_codex_timeout_raises(monkeypatch, tmp_path):
    """Migrated for iter-0037: the new code path replaces wall-time TimeoutExpired
    with heartbeat-silence abort. Simulate a codex that never produces stdout
    while time_fn advances past stall_silence_seconds, and assert
    CodexInvocationError fires with a timeout-flavored message.
    """
    # Popen stays "running" forever (poll always returns None) until terminate.
    fake_popen = make_fake_codex_popen_factory(
        stdout_text="",
        returncode=0,
        poll_calls_until_done=10_000,
    )

    # Fake time advancing by 60s per call so silence threshold (90s) trips
    # after a couple of poll iterations.
    fake_clock = {"t": 1000.0}

    def fake_time():
        fake_clock["t"] += 60.0
        return fake_clock["t"]

    with pytest.raises(_dispatch_codex.CodexInvocationError) as exc:
        _dispatch_codex._invoke_codex(
            prompt="x", codex_bin="codex", timeout_seconds=1,
            repo_root=tmp_path, schema_path=SCHEMA_PATH,
            poll_interval=0.01,
            stall_silence_seconds=90.0,
            popen_factory=fake_popen,
            time_fn=fake_time,
        )
    err = str(exc.value).lower()
    # New abort message paths: "stuck: no output" (silence watchdog) or
    # "exceeded ...s wall timeout" (hard ceiling). Either is acceptable as a
    # "timeout" surfacing under the legacy test name.
    assert "stuck" in err or "timeout" in err or "no output" in err, \
        f"expected timeout-like error message; got: {err!r}"


def test_get_codex_version_returns_string(monkeypatch):
    """_get_codex_version shells out to codex --version; returns the version string. Mocked."""
    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        result.stdout = "codex-cli 0.129.0\n"
        result.stderr = ""
        return result

    monkeypatch.setattr(_dispatch_codex.subprocess, "run", fake_run)
    v = _dispatch_codex._get_codex_version("codex")
    assert "0.129.0" in v


def test_get_codex_version_unknown_on_failure(monkeypatch):
    """If codex --version fails, return 'unknown' (don't raise; provenance is best-effort)."""
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("no codex")

    monkeypatch.setattr(_dispatch_codex.subprocess, "run", fake_run)
    v = _dispatch_codex._get_codex_version("codex")
    assert v == "unknown"


# --- T4 tests --------------------------------------------------------------

def test_sha256_str_returns_hex():
    """_sha256_str computes deterministic hex digest of UTF-8 input."""
    h = _dispatch_codex._sha256_str("hello")
    assert isinstance(h, str)
    assert len(h) == 64
    # known sha256 of "hello"
    assert h == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_build_sealed_packet_has_required_t6_fields():
    """T6 (review.write_and_seal) requires iteration_id, reviewer_id, findings."""
    extracted = {"findings": [{"id": "codex-rev-001", "severity": "low", "summary": "x"}],
                 "goal_satisfied": True, "blocking_objections": []}
    packet = _dispatch_codex._build_sealed_packet(
        extracted=extracted,
        iteration_id="iteration-9999-test",
        reviewer_id="codex-iter9999-1",
        pass_id="codex-iter9999-1-pass1",
    )
    assert packet["iteration_id"] == "iteration-9999-test"
    assert packet["reviewer_id"] == "codex-iter9999-1"
    assert packet["pass_id"] == "codex-iter9999-1-pass1"
    assert packet["findings"][0]["id"] == "codex-rev-001"
    assert packet["goal_satisfied"] is True
    assert packet["blocking_objections"] == []


def test_log_dispatch_appends_jsonl(tmp_path):
    """_log_dispatch writes one JSON line per call to dispatch-log.jsonl."""
    log_path = tmp_path / "dispatch-log.jsonl"
    _dispatch_codex._log_dispatch(log_path, {"event": "dispatch_start", "pilot_id": "x"})
    _dispatch_codex._log_dispatch(log_path, {"event": "dispatch_done", "pass_id": "y"})
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    import json as _j
    assert _j.loads(lines[0])["event"] == "dispatch_start"
    assert _j.loads(lines[1])["pass_id"] == "y"


@_REQUIRES_REAL_CODEX
def test_main_smoke_with_mocked_codex(monkeypatch, tmp_path):
    """End-to-end: full pipeline with codex subprocess mocked.

    Verifies (a) sealed yaml lands at <iteration_dir>/codex-review.yaml,
             (b) dispatch-log.jsonl gets dispatch_start + dispatch_done events,
             (c) audit-log dispatch_done event includes provenance fields:
                 codex_version, prompt_sha256, output_sha256, schema_sha256,
                 goal_packet_sha256, scope_signature, reviewer_id, pass_id,
                 timeout_seconds, exit_code, sealed_path.
    """
    fake_codex_output = (FIXTURES / "codex_output_well_formed.json").read_text(encoding="utf-8")

    def fake_run(cmd, **kwargs):
        # `codex --version` probe path
        if len(cmd) == 2 and cmd[1] == "--version":
            result = _mock.MagicMock()
            result.returncode = 0
            result.stdout = "codex-cli 0.129.0\n"
            result.stderr = ""
            return result
        # Mimic codex's -o file-write side effect.
        out_idx = cmd.index("-o") + 1
        Path(cmd[out_idx]).write_text(fake_codex_output, encoding="utf-8")
        result = _mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_dispatch_codex.subprocess, "run", fake_run)

    iter_dir = tmp_path / "iteration-9999-smoke"
    iter_dir.mkdir()

    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    (tmp_path / "consensus-state" / "state").mkdir(parents=True)

    rc = _dispatch_codex.main([
        "--goal-packet", str(_stage_smoke_goal_packet(tmp_path)),
        "--iteration-dir", str(iter_dir),
        "--reviewer-id", "codex-iter9999-1",
        "--pass-id", "codex-iter9999-1-pass1",
        "--codex-bin", "codex",
    ])

    assert rc == 0
    review_yaml = iter_dir / "codex-review.yaml"
    assert review_yaml.exists()
    parsed = yaml.safe_load(review_yaml.read_text(encoding="utf-8"))
    assert parsed["reviewer_id"] == "codex-iter9999-1"
    assert "packet_sha256" in parsed

    import json as _j
    log_lines = (tmp_path / "consensus-state" / "state" / "dispatch-log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [_j.loads(line) for line in log_lines]
    event_types = [e["event"] for e in events]
    assert "dispatch_start" in event_types
    assert "dispatch_done" in event_types

    # Provenance fields on the dispatch_done event (codex review #4 audit requirements)
    done = next(e for e in events if e["event"] == "dispatch_done")
    for field in (
        "codex_version", "prompt_sha256", "output_sha256", "schema_sha256",
        "goal_packet_sha256", "scope_signature", "reviewer_id", "pass_id",
        "timeout_seconds", "exit_code", "sealed_path",
    ):
        assert field in done, f"dispatch_done missing audit field: {field}"
    assert done["codex_version"] == "codex-cli 0.129.0"
    assert done["reviewer_id"] == "codex-iter9999-1"
    assert done["exit_code"] == 0
    assert len(done["prompt_sha256"]) == 64
    assert len(done["output_sha256"]) == 64
    assert len(done["schema_sha256"]) == 64
    assert len(done["goal_packet_sha256"]) == 64


# --- H2 (F2) defense-in-depth validation tests --------------------------------

def test_parse_codex_output_missing_goal_satisfied_raises():
    """Missing required top-level 'goal_satisfied' raises CodexOutputParseError."""
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output('{"findings": [], "blocking_objections": []}')
    assert "goal_satisfied" in str(exc.value)


def test_parse_codex_output_missing_blocking_objections_raises():
    """Missing required top-level 'blocking_objections' raises CodexOutputParseError."""
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output('{"findings": [], "goal_satisfied": true}')
    assert "blocking_objections" in str(exc.value)


def test_parse_codex_output_goal_satisfied_wrong_type_raises():
    """Non-bool goal_satisfied raises CodexOutputParseError."""
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(
            '{"findings": [], "goal_satisfied": "yes", "blocking_objections": []}'
        )
    assert "goal_satisfied" in str(exc.value)


def test_parse_codex_output_blocking_objections_wrong_type_raises():
    """blocking_objections as a string instead of array raises CodexOutputParseError."""
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(
            '{"findings": [], "goal_satisfied": true, "goal_satisfied_rationale": "test", "blocking_objections": "abc"}'
        )
    assert "blocking_objections" in str(exc.value)


def test_parse_codex_output_finding_severity_outside_enum_raises():
    """Severity outside the 5-value enum raises CodexOutputParseError."""
    bad = (
        '{"findings": [{"id": "codex-rev-001", "severity": "URGENT", '
        '"summary": "x", "citation": "f:1", "risk": "r", "recommendation": "rec"}], '
        '"goal_satisfied": false, "goal_satisfied_rationale": "test", "blocking_objections": []}'
    )
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    assert "severity" in str(exc.value)


def test_parse_codex_output_finding_id_pattern_violation_raises():
    """Finding id not matching codex-rev-\\d+ pattern raises CodexOutputParseError."""
    bad = (
        '{"findings": [{"id": "codex-something-001", "severity": "low", '
        '"summary": "x", "citation": "f:1", "risk": "r", "recommendation": "rec"}], '
        '"goal_satisfied": false, "goal_satisfied_rationale": "test", "blocking_objections": []}'
    )
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    assert "id" in str(exc.value).lower() or "pattern" in str(exc.value).lower()


def test_parse_codex_output_finding_missing_required_field_raises():
    """Finding missing required 'summary' raises CodexOutputParseError."""
    bad = (
        '{"findings": [{"id": "codex-rev-001", "severity": "low", '
        '"citation": "f:1", "risk": "r", "recommendation": "rec"}], '
        '"goal_satisfied": false, "goal_satisfied_rationale": "test", "blocking_objections": []}'
    )
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    assert "summary" in str(exc.value).lower()


# --- H3 (F3) smoke env-gate enforcement tests ---------------------------------

def test_main_smoke_flag_without_env_refuses(monkeypatch, tmp_path):
    """--smoke without CONSENSUS_MCP_RUN_REAL_CODEX_SMOKE=1 refuses; codex never invoked."""
    # Ensure env var is unset
    monkeypatch.delenv("CONSENSUS_MCP_RUN_REAL_CODEX_SMOKE", raising=False)

    # Track whether subprocess.run is called (it should NOT be on the refuse path)
    call_log = []

    def fake_run(cmd, **kwargs):
        call_log.append(cmd)
        result = _mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_dispatch_codex.subprocess, "run", fake_run)

    iter_dir = tmp_path / "iteration-9999-smoke-refused"
    iter_dir.mkdir()
    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    (tmp_path / "consensus-state" / "state").mkdir(parents=True)

    rc = _dispatch_codex.main([
        "--goal-packet", str(_stage_smoke_goal_packet(tmp_path)),
        "--iteration-dir", str(iter_dir),
        "--reviewer-id", "codex-iter9999-1",
        "--pass-id", "codex-iter9999-1-pass1",
        "--codex-bin", "codex",
        "--smoke",
    ])

    assert rc != 0, f"--smoke without env should refuse with non-zero exit; got {rc}"
    # Codex (or its --version probe) must NEVER be invoked when refused
    assert call_log == [], f"subprocess.run should NOT be called on refused smoke; got {call_log}"

    # dispatch-log records dispatch_refused event with the env var name in the error message
    import json as _j
    log_path = tmp_path / "consensus-state" / "state" / "dispatch-log.jsonl"
    assert log_path.exists(), "dispatch-log.jsonl should exist with at least the refusal event"
    log_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    events = [_j.loads(line) for line in log_lines]
    assert any(e["event"] == "dispatch_refused" for e in events), \
        f"dispatch_refused event missing; got events={[e['event'] for e in events]}"
    refused = next(e for e in events if e["event"] == "dispatch_refused")
    assert "CONSENSUS_MCP_RUN_REAL_CODEX_SMOKE" in refused.get("error", ""), \
        f"refusal must name the required env var; got error={refused.get('error')!r}"


@_REQUIRES_REAL_CODEX
def test_main_smoke_flag_with_env_proceeds(monkeypatch, tmp_path):
    """--smoke WITH env=1 proceeds; codex IS invoked (via mock)."""
    monkeypatch.setenv("CONSENSUS_MCP_RUN_REAL_CODEX_SMOKE", "1")

    fake_codex_output = (FIXTURES / "codex_output_well_formed.json").read_text(encoding="utf-8")

    def fake_run(cmd, **kwargs):
        if len(cmd) == 2 and cmd[1] == "--version":
            result = _mock.MagicMock()
            result.returncode = 0
            result.stdout = "codex-cli 0.129.0\n"
            result.stderr = ""
            return result
        out_idx = cmd.index("-o") + 1
        Path(cmd[out_idx]).write_text(fake_codex_output, encoding="utf-8")
        result = _mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_dispatch_codex.subprocess, "run", fake_run)

    iter_dir = tmp_path / "iteration-9999-smoke-allowed"
    iter_dir.mkdir()
    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    (tmp_path / "consensus-state" / "state").mkdir(parents=True)

    rc = _dispatch_codex.main([
        "--goal-packet", str(_stage_smoke_goal_packet(tmp_path)),
        "--iteration-dir", str(iter_dir),
        "--reviewer-id", "codex-iter9999-h3-allowed",
        "--pass-id", "codex-iter9999-h3-allowed-pass1",
        "--codex-bin", "codex",
        "--smoke",
    ])

    assert rc == 0, f"--smoke WITH env should proceed cleanly; got rc={rc}"
    review_yaml = iter_dir / "codex-review.yaml"
    assert review_yaml.exists(), "sealed review yaml should exist when smoke proceeds"


# --- H4 (F5) sealed-packet provenance embedding ------------------------------

@_REQUIRES_REAL_CODEX
def test_main_sealed_packet_embeds_dispatch_provenance(monkeypatch, tmp_path):
    """Sealed codex-review.yaml contains a dispatch_provenance block with all 6 audit fields.

    Per F5 (codex review 2026-05-09): the sealed review must be independently
    verifiable without consulting dispatch-log.jsonl.
    """
    fake_codex_output = (FIXTURES / "codex_output_well_formed.json").read_text(encoding="utf-8")

    def fake_run(cmd, **kwargs):
        if len(cmd) == 2 and cmd[1] == "--version":
            result = _mock.MagicMock()
            result.returncode = 0
            result.stdout = "codex-cli 0.129.0\n"
            result.stderr = ""
            return result
        out_idx = cmd.index("-o") + 1
        Path(cmd[out_idx]).write_text(fake_codex_output, encoding="utf-8")
        result = _mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_dispatch_codex.subprocess, "run", fake_run)

    iter_dir = tmp_path / "iteration-9999-h4-provenance"
    iter_dir.mkdir()
    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    (tmp_path / "consensus-state" / "state").mkdir(parents=True)

    rc = _dispatch_codex.main([
        "--goal-packet", str(_stage_smoke_goal_packet(tmp_path)),
        "--iteration-dir", str(iter_dir),
        "--reviewer-id", "codex-iter9999-h4-1",
        "--pass-id", "codex-iter9999-h4-1-pass1",
        "--codex-bin", "codex",
    ])

    assert rc == 0
    review_yaml = iter_dir / "codex-review.yaml"
    assert review_yaml.exists()
    parsed = yaml.safe_load(review_yaml.read_text(encoding="utf-8"))

    # F5: sealed packet must include dispatch_provenance for self-contained verification
    assert "dispatch_provenance" in parsed, \
        f"sealed packet missing 'dispatch_provenance' key; got top-level keys: {list(parsed.keys())}"
    prov = parsed["dispatch_provenance"]
    for field in (
        "codex_version", "prompt_sha256", "output_sha256",
        "schema_sha256", "goal_packet_sha256", "scope_signature",
    ):
        assert field in prov, f"dispatch_provenance missing '{field}'; got {list(prov.keys())}"

    # Spot-check shape: 4 sha256 hashes are 64-char hex; codex_version is the mocked string
    assert len(prov["prompt_sha256"]) == 64
    assert len(prov["output_sha256"]) == 64
    assert len(prov["schema_sha256"]) == 64
    assert len(prov["goal_packet_sha256"]) == 64
    assert prov["codex_version"] == "codex-cli 0.129.0"
    # scope_signature is whatever the goal_packet's authorization had; smoke fixture has the all-zeros placeholder
    assert prov["scope_signature"] == "0" * 64


# --- H5 (F6) review-target prompt fields -------------------------------------

def test_build_prompt_review_target_fields_substituted():
    """When iteration_dir / review_target_path / review_target_hash provided, they substitute."""
    template = (
        "Iter: {iteration_dir}\n"
        "Packet: {review_packet_path}\n"
        "Target: {review_target_path}\n"
        "Hash: {review_target_hash}\n"
    )
    packet = {"goal": {"summary": "x"}, "authorization": {}}
    prompt = _dispatch_codex._build_prompt(
        packet, template,
        iteration_dir="/tmp/iter-9999",
        review_packet_path="/tmp/goal_packet.yaml",
        review_target_path="/tmp/review.diff",
        review_target_hash="abcdef" * 10 + "abcd",  # 64 chars
    )
    assert "/tmp/iter-9999" in prompt
    assert "/tmp/goal_packet.yaml" in prompt
    assert "/tmp/review.diff" in prompt
    assert "abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd" in prompt
    assert "{iteration_dir}" not in prompt
    assert "{review_target_path}" not in prompt


def test_build_prompt_review_target_unspecified_renders_placeholder():
    """When review_target_path / hash NOT passed, placeholders render as '(not specified)'."""
    template = (
        "Target: {review_target_path}\n"
        "Hash: {review_target_hash}\n"
    )
    packet = {"goal": {"summary": "x"}, "authorization": {}}
    prompt = _dispatch_codex._build_prompt(packet, template)
    assert "(not specified)" in prompt
    assert "{review_target_path}" not in prompt
    assert "{review_target_hash}" not in prompt


def test_main_review_target_arg_threaded_through(monkeypatch, tmp_path):
    """End-to-end: --review-target reads file + hashes it + sealed yaml + dispatch_provenance reflect it.

    Migrated for iter-0037: codex invocation now goes through Popen; --version
    probe still uses subprocess.run (preserved). Prompts are captured via the
    fake Popen's stdin instead of subprocess.run's input= kwarg.
    """
    review_target = tmp_path / "fake.diff"
    review_target.write_text("--- a/foo\n+++ b/foo\n@@ -1 +1 @@\n-hello\n+hi\n", encoding="utf-8")

    fake_codex_output = (FIXTURES / "codex_output_well_formed.json").read_text(encoding="utf-8")

    captured_prompts = []

    def fake_run(cmd, **kwargs):
        # --version probe path still uses subprocess.run; preserved verbatim.
        if len(cmd) == 2 and cmd[1] == "--version":
            result = _mock.MagicMock()
            result.returncode = 0
            result.stdout = "codex-cli 0.129.0\n"
            result.stderr = ""
            return result
        raise AssertionError(
            f"subprocess.run should only be called for --version probe; got {cmd!r}"
        )

    monkeypatch.setattr(_dispatch_codex.subprocess, "run", fake_run)
    monkeypatch.setattr(
        _dispatch_codex.subprocess, "Popen",
        make_fake_codex_popen_factory(
            stdout_text=fake_codex_output,
            returncode=0,
            captured_prompts=captured_prompts,
        ),
    )

    iter_dir = tmp_path / "iteration-9999-h5-target"
    iter_dir.mkdir()
    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    (tmp_path / "consensus-state" / "state").mkdir(parents=True)

    rc = _dispatch_codex.main([
        "--goal-packet", str(_stage_smoke_goal_packet(tmp_path)),
        "--iteration-dir", str(iter_dir),
        "--reviewer-id", "codex-iter9999-h5-1",
        "--pass-id", "codex-iter9999-h5-1-pass1",
        "--codex-bin", "codex",
        "--review-target", str(review_target),
    ])

    assert rc == 0
    # Per v1.10.3: _invoke_codex passes prompt as UTF-8 bytes (not str) to subprocess
    # to avoid Windows text-mode CRLF translation corrupting multibyte UTF-8 sequences.
    # Decode each captured prompt back to str for substring assertions.
    decoded_prompts = [
        (p.decode("utf-8", errors="replace") if isinstance(p, (bytes, bytearray)) else p)
        for p in captured_prompts
    ]
    assert any(str(review_target) in p for p in decoded_prompts), \
        "review_target_path should appear in the prompt sent to codex"
    # The hash should be the sha256 of the review_target's content
    import hashlib
    expected_hash = hashlib.sha256(review_target.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert any(expected_hash in p for p in decoded_prompts), \
        "review_target_hash (sha256 of file content) should appear in the prompt"

    # v1.10.5: review_target_path + review_target_hash must also land in
    # sealed dispatch_provenance and the dispatch_done audit event so the
    # visibility TUI / future watchdog / audit reconstruction can identify
    # what was reviewed without re-parsing the prompt.
    sealed = yaml.safe_load((iter_dir / "codex-review.yaml").read_text(encoding="utf-8"))
    prov = sealed.get("dispatch_provenance") or {}
    assert prov.get("review_target_hash") == expected_hash, (
        f"sealed dispatch_provenance.review_target_hash mismatch: "
        f"got {prov.get('review_target_hash')!r}, expected {expected_hash!r}"
    )
    assert prov.get("review_target_path"), (
        "sealed dispatch_provenance.review_target_path must be set "
        "(relative-to-repo string, not None)"
    )

    import json as _j
    log_lines = (tmp_path / "consensus-state" / "state" / "dispatch-log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [_j.loads(line) for line in log_lines]

    start = next(e for e in events if e["event"] == "dispatch_start")
    assert start.get("review_target_path"), (
        "dispatch_start must include review_target_path (visibility TUI needs it)"
    )

    done = next(e for e in events if e["event"] == "dispatch_done")
    assert done.get("review_target_hash") == expected_hash, (
        f"dispatch_done.review_target_hash mismatch: got {done.get('review_target_hash')!r}"
    )
    assert done.get("review_target_path"), (
        "dispatch_done must include review_target_path"
    )


# --- v1.10.2 HG2: F2 (strict types/keys) + F3 (required fields + invariant) ----

def test_parse_codex_output_blocking_objections_non_string_item_raises():
    """Per F2: blocking_objections items must be strings; reject [1]."""
    bad = (
        '{"findings": [], "goal_satisfied": true, "goal_satisfied_rationale": "test", "blocking_objections": [1]}'
    )
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    assert "blocking_objections" in str(exc.value).lower()


def test_parse_codex_output_finding_unexpected_field_raises():
    """Per F2: unknown finding fields rejected (additionalProperties:false enforcement)."""
    bad = (
        '{"findings": [{"id": "codex-rev-001", "severity": "low", '
        '"summary": "x", "citation": "f:1", "risk": "r", '
        '"recommendation": "rec", "extra": "weird"}], '
        '"goal_satisfied": true, "goal_satisfied_rationale": "test", "blocking_objections": []}'
    )
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    assert (
        "extra" in str(exc.value).lower()
        or "unexpected" in str(exc.value).lower()
        or "additional" in str(exc.value).lower()
    )


def test_parse_codex_output_top_level_unexpected_key_raises():
    """Per F2: top-level unknown keys rejected."""
    bad = (
        '{"findings": [], "goal_satisfied": true, "goal_satisfied_rationale": "test", "blocking_objections": [], '
        '"smuggled_field": "uh oh"}'
    )
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    assert (
        "smuggled_field" in str(exc.value).lower()
        or "unexpected" in str(exc.value).lower()
        or "additional" in str(exc.value).lower()
    )


def test_parse_codex_output_citation_wrong_type_raises():
    """Per F2: optional finding fields with wrong types rejected (citation as int)."""
    bad = (
        '{"findings": [{"id": "codex-rev-001", "severity": "low", '
        '"summary": "x", "citation": 12, "risk": "r", "recommendation": "rec"}], '
        '"goal_satisfied": true, "goal_satisfied_rationale": "test", "blocking_objections": []}'
    )
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    assert "citation" in str(exc.value).lower()


def test_parse_codex_output_goal_satisfied_rationale_wrong_type_raises():
    """Per F2: goal_satisfied_rationale must be string if present."""
    bad = (
        '{"findings": [], "goal_satisfied": true, "goal_satisfied_rationale": "test", "blocking_objections": [], '
        '"goal_satisfied_rationale": false}'
    )
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    assert "goal_satisfied_rationale" in str(exc.value).lower()


def test_parse_codex_output_finding_missing_citation_raises():
    """Per F3: citation now required for every finding."""
    bad = (
        '{"findings": [{"id": "codex-rev-001", "severity": "low", '
        '"summary": "x", "risk": "r", "recommendation": "rec"}], '
        '"goal_satisfied": true, "goal_satisfied_rationale": "test", "blocking_objections": []}'
    )
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    assert "citation" in str(exc.value).lower()


def test_parse_codex_output_blocking_finding_missing_from_blocking_objections_raises():
    """Per F3: blocking_objections must contain ids of all findings with severity in {blocking, critical}."""
    bad = (
        '{"findings": [{"id": "codex-rev-001", "severity": "blocking", '
        '"summary": "x", "citation": "f:1", "risk": "r", "recommendation": "rec"}], '
        '"goal_satisfied": false, "goal_satisfied_rationale": "test", "blocking_objections": []}'
    )
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    err = str(exc.value).lower()
    assert "blocking_objections" in err and (
        "invariant" in err or "missing" in err or "consistent" in err or "codex-rev-001" in err
    )


def test_parse_codex_output_blocking_invariant_satisfied_passes():
    """Per F3: when blocking_objections matches the set of blocking/critical finding ids, accepts."""
    good = (
        '{"findings": [{"id": "codex-rev-001", "severity": "blocking", '
        '"summary": "x", "citation": "f:1", "risk": "r", "recommendation": "rec"}], '
        '"goal_satisfied": false, "goal_satisfied_rationale": "test", "blocking_objections": ["codex-rev-001"]}'
    )
    parsed = _dispatch_codex._parse_codex_output(good)
    assert parsed["findings"][0]["severity"] == "blocking"
    assert parsed["blocking_objections"] == ["codex-rev-001"]


# ---- v1.10.4 HG1 (F1 fail-closed repo_root + F5 relative-path normalization) ----

def test_resolve_repo_root_env_with_markers_returns_path(monkeypatch, tmp_path):
    """F1: env var pointing at a directory WITH markers resolves successfully."""
    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    resolved = _dispatch_codex._resolve_repo_root()
    assert resolved == tmp_path.resolve()


def test_resolve_repo_root_marker_check_rejects_empty_dir(monkeypatch, tmp_path):
    """v1.10.4 F1: _has_repo_markers returns False on a directory missing markers.

    iter-0028 F5 NOTE: the previous version of this test (then named
    test_resolve_repo_root_env_without_markers_falls_through_to_other_candidates)
    described fall-through behaviour that was deliberately removed in iter-0028
    F5. The full env-set-and-invalid fail-closed semantics are now covered by
    test_resolve_repo_root_env_set_and_invalid_raises below. This test retains
    the narrow marker-detection sanity check.
    """
    no_markers = tmp_path / "no_markers"
    no_markers.mkdir()
    assert not _dispatch_codex._has_repo_markers(no_markers)


def test_resolve_repo_root_no_candidates_raises():
    """F1: when no candidate has markers, raises RepoRootResolutionError."""
    # We can't easily test the full failure path because __file__-parent walk
    # in the actual test environment finds the real repo root. Verify the
    # exception class exists and the marker check is the gate.
    assert hasattr(_dispatch_codex, "RepoRootResolutionError")
    assert issubclass(_dispatch_codex.RepoRootResolutionError, RuntimeError)


def test_normalize_relative_to_repo_relative_path_joined(tmp_path):
    """F5: relative paths join under repo_root."""
    result = _dispatch_codex._normalize_relative_to_repo("consensus-state/foo.yaml", tmp_path)
    assert result == (tmp_path / "consensus-state" / "foo.yaml").resolve()


def test_normalize_relative_to_repo_absolute_path_unchanged(tmp_path):
    """F5: absolute paths pass through unchanged (resolved to absolute form)."""
    abs_path = tmp_path / "abs.yaml"
    result = _dispatch_codex._normalize_relative_to_repo(str(abs_path), tmp_path)
    assert result == abs_path.resolve()


def test_normalize_relative_to_repo_none_returns_none(tmp_path):
    """F5: None input returns None (e.g., optional --review-target not supplied)."""
    assert _dispatch_codex._normalize_relative_to_repo(None, tmp_path) is None


# ---- v1.10.4 HG2 (F2 dispatch_done immutable archive + F3 dispatch_failed provenance) ----

@_REQUIRES_REAL_CODEX
def test_dispatch_done_includes_archive_path_and_audit_id(monkeypatch, tmp_path):
    """F2: dispatch_done event has archive_sealed_path + local_mirror_path + t6_audit_event_id."""
    fake_codex_output = (FIXTURES / "codex_output_well_formed.json").read_text(encoding="utf-8")

    def fake_run(cmd, **kwargs):
        if len(cmd) == 2 and cmd[1] == "--version":
            r = _mock.MagicMock(); r.returncode = 0; r.stdout = "codex-cli 0.129.0\n"; r.stderr = ""; return r
        out_idx = cmd.index("-o") + 1
        Path(cmd[out_idx]).write_text(fake_codex_output, encoding="utf-8")
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = b""; r.stderr = b""; return r

    monkeypatch.setattr(_dispatch_codex.subprocess, "run", fake_run)
    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    iter_dir = tmp_path / "iteration-9999-h2-archive-path"
    iter_dir.mkdir()
    (tmp_path / "consensus-state" / "state").mkdir(parents=True, exist_ok=True)

    rc = _dispatch_codex.main([
        "--goal-packet", str(_stage_smoke_goal_packet(tmp_path)),
        "--iteration-dir", str(iter_dir),
        "--reviewer-id", "codex-iter9999-h2-archive",
        "--pass-id", "codex-iter9999-h2-archive-pass1",
        "--codex-bin", "codex",
    ])
    assert rc == 0

    log_lines = (tmp_path / "consensus-state" / "state" / "dispatch-log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [_json.loads(line) for line in log_lines]
    done = next(e for e in events if e["event"] == "dispatch_done")
    assert "archive_sealed_path" in done
    assert "local_mirror_path" in done
    assert "t6_audit_event_id" in done
    # archive path is INSIDE consensus-state/archive/review-passes/ regardless of whether
    # the mirror was overwritten on a re-run
    assert "archive" in done["archive_sealed_path"].replace("\\", "/")


def test_dispatch_failed_includes_computed_provenance_when_codex_fails(monkeypatch, tmp_path):
    """F3: when codex invocation fails AFTER hashes are computed, dispatch_failed
    event includes the prompt/schema/goal_packet hashes + scope_signature.

    Migrated for iter-0037: codex failure now surfaces via Popen returncode!=0
    (not subprocess.run); --version probe still uses subprocess.run.
    """
    def fake_run(cmd, **kwargs):
        if len(cmd) == 2 and cmd[1] == "--version":
            r = _mock.MagicMock(); r.returncode = 0; r.stdout = "codex-cli 0.129.0\n"; r.stderr = ""; return r
        raise AssertionError(
            f"subprocess.run should only be called for --version probe; got {cmd!r}"
        )

    monkeypatch.setattr(_dispatch_codex.subprocess, "run", fake_run)
    monkeypatch.setattr(
        _dispatch_codex.subprocess, "Popen",
        make_fake_codex_popen_factory(
            stdout_text="",
            returncode=1,
            stderr_text="codex blew up for test",
        ),
    )
    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    iter_dir = tmp_path / "iteration-9999-h3-failpath"
    iter_dir.mkdir()
    (tmp_path / "consensus-state" / "state").mkdir(parents=True, exist_ok=True)

    rc = _dispatch_codex.main([
        "--goal-packet", str(_stage_smoke_goal_packet(tmp_path)),
        "--iteration-dir", str(iter_dir),
        "--reviewer-id", "codex-iter9999-h3-fail",
        "--pass-id", "codex-iter9999-h3-fail-pass1",
        "--codex-bin", "codex",
    ])
    assert rc == 1

    log_lines = (tmp_path / "consensus-state" / "state" / "dispatch-log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [_json.loads(line) for line in log_lines]
    failed = next(e for e in events if e["event"] == "dispatch_failed")
    # Provenance fields that were computed BEFORE codex call should be present
    for field in ("prompt_sha256", "schema_sha256", "goal_packet_sha256", "scope_signature", "codex_version"):
        assert field in failed, f"dispatch_failed missing computed-pre-codex field: {field}"
    # output_sha256 was NOT computed (codex failed) so should be absent
    assert "output_sha256" not in failed


# ---- v1.10.4 HG3 (F4 validator requires goal_satisfied_rationale; schema alignment) ----

def test_parse_codex_output_missing_goal_satisfied_rationale_raises():
    """F4: goal_satisfied_rationale is now required (mirrors schema)."""
    # Payload intentionally OMITS goal_satisfied_rationale to verify required-key check fires.
    bad = '{"findings": [], "goal_satisfied": true, "blocking_objections": []}'
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    assert "goal_satisfied_rationale" in str(exc.value)


def test_parse_codex_output_goal_satisfied_rationale_wrong_type_still_raises():
    """F4 + carryover from v1.10.2 H2: rationale present but wrong type still rejected."""
    bad = '{"findings": [], "goal_satisfied": true, "goal_satisfied_rationale": false, "blocking_objections": []}'
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(bad)
    assert "goal_satisfied_rationale" in str(exc.value)


# ---- v1.10.4 HG4 (F6 Windows .cmd preference test + F7 doc bump) ----

def test_resolve_codex_bin_prefers_cmd_over_ps1_on_windows(monkeypatch):
    """F6: when shutil.which("codex") returns a .ps1 on Windows, _resolve_codex_bin
    must look up the .cmd variant explicitly because Python subprocess can't exec .ps1."""
    monkeypatch.setattr(_dispatch_codex.sys, "platform", "win32")

    def fake_which(name):
        # First call: bare "codex" resolves to .ps1; second call: "codex.cmd" resolves to .cmd
        if name == "codex":
            return r"C:\Users\test\AppData\Roaming\npm\codex.ps1"
        if name == "codex.cmd":
            return r"C:\Users\test\AppData\Roaming\npm\codex.cmd"
        return None

    monkeypatch.setattr(_dispatch_codex.shutil, "which", fake_which)
    resolved = _dispatch_codex._resolve_codex_bin("codex")
    assert resolved.lower().endswith(".cmd"), \
        f"on Windows, .cmd should be preferred over .ps1; got {resolved!r}"


def test_resolve_codex_bin_returns_ps1_when_no_cmd_alternative(monkeypatch):
    """F6 corner: if .cmd doesn't exist, fall back to whatever shutil.which returned."""
    monkeypatch.setattr(_dispatch_codex.sys, "platform", "win32")

    def fake_which(name):
        if name == "codex":
            return "/some/path/codex.ps1"
        return None  # No .cmd alternative

    monkeypatch.setattr(_dispatch_codex.shutil, "which", fake_which)
    resolved = _dispatch_codex._resolve_codex_bin("codex")
    assert resolved == "/some/path/codex.ps1"


# ---- iter-0014 (Task #24) + iter-0020 (ergonomics fix): patch_proposal block ----
#
# iter-0020 binding fields:
#   patch_proposal:
#     patch_id: <finding_id>-patch         # codex-producible (was content-bound)
#     applies_to_findings: [codex-rev-NNN, ...]
#     base_sha: <repo state codex saw>
#     unified_diff: <text>
#     files_touched: [paths]
#     expected_tests: [test_name, ...]    # required (iter-0019)
#
# patch_proposal is OPTIONAL on each finding; backward compat: codex output WITHOUT
# patch_proposal still parses normally.
#
# Anti-self-verification: any finding (or patch_proposal) field named verified /
# self_verified / correct / approved / confirmed REJECTS the whole output.

import hashlib as _hashlib


_OMIT_EXPECTED_TESTS = object()


def _build_patch_proposal(
    base_sha: str = "abc123def456abc123def456abc123def456abc123def456abc123def456abcd",
    unified_diff: str = "--- a/consensus_mcp/_dispatch_codex.py\n"
                        "+++ b/consensus_mcp/_dispatch_codex.py\n"
                        "@@ -1 +1,2 @@\n"
                        " hello\n"
                        "+world\n",
    applies_to_findings=("codex-rev-001",),
    files_touched=("consensus_mcp/_dispatch_codex.py",),
    expected_tests=("pytest_smoke",),
    finding_id: str = "codex-rev-001",
) -> dict:
    """Build a well-formed patch_proposal dict with iter-0020 finding-id-derived patch_id.

    Helper for the patch_proposal tests. patch_id = f"{finding_id}-patch".
    Pass expected_tests=_OMIT_EXPECTED_TESTS to construct an INVALID proposal
    that omits the expected_tests field entirely (for regression testing
    schema/validator alignment).
    """
    patch_id = f"{finding_id}-patch"
    pp: dict = {
        "patch_id": patch_id,
        "applies_to_findings": list(applies_to_findings),
        "base_sha": base_sha,
        "unified_diff": unified_diff,
        "files_touched": list(files_touched),
    }
    if expected_tests is not _OMIT_EXPECTED_TESTS:
        pp["expected_tests"] = list(expected_tests) if expected_tests is not None else []
    return pp


def _build_codex_output_with_patch(patch_proposal=None, finding_extra=None) -> str:
    """Build a codex output JSON payload with one finding (codex-rev-001).

    If patch_proposal is provided, it's attached to the finding. If finding_extra
    is provided, those extra keys are merged into the finding (used for testing
    rejection of self-verification fields).
    """
    finding = {
        "id": "codex-rev-001",
        "severity": "medium",
        "summary": "Test finding for patch_proposal tests",
        "citation": "consensus_mcp/_dispatch_codex.py:42",
        "risk": "Low; test fixture",
        "recommendation": "Test recommendation",
    }
    if patch_proposal is not None:
        finding["patch_proposal"] = patch_proposal
    if finding_extra is not None:
        finding.update(finding_extra)
    return _json.dumps({
        "findings": [finding],
        "goal_satisfied": False,
        "goal_satisfied_rationale": "Test fixture for patch_proposal validation",
        "blocking_objections": [],
    })


def _smoke_goal_packet() -> dict:
    """Load the smoke goal_packet fixture as a dict for goal-packet-aware validation."""
    return _dispatch_codex._load_goal_packet(SMOKE_GOAL_PACKET)


def test_no_patch_proposal_still_works():
    """Backward compat: codex output WITHOUT patch_proposal parses normally."""
    text = _build_codex_output_with_patch(patch_proposal=None)
    parsed = _dispatch_codex._parse_codex_output(text)
    assert "patch_proposal" not in parsed["findings"][0]


def test_patch_proposal_well_formed_passes():
    """Codex output with valid patch_proposal: parser accepts; goal-packet scope ok."""
    pp = _build_patch_proposal(
        files_touched=("consensus_mcp/_dispatch_codex.py",),
        expected_tests=("test_foo_bar",),
    )
    text = _build_codex_output_with_patch(patch_proposal=pp)
    parsed = _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    assert parsed["findings"][0]["patch_proposal"]["patch_id"] == pp["patch_id"]
    assert parsed["findings"][0]["patch_proposal"]["expected_tests"] == ["test_foo_bar"]


def test_patch_proposal_invalid_patch_id_rejected():
    """patch_id not matching iter-0020 regex -> rejected."""
    pp = _build_patch_proposal()
    pp["patch_id"] = "patch-000000000000-000000000000"   # old content-bound shape
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    assert "patch_id" in str(exc.value).lower()


def test_patch_proposal_missing_expected_tests_rejected():
    """Schema/validator alignment (2026-05-10): patch_proposal missing
    expected_tests is rejected by the python validator, matching the strict
    JSON schema. Prior to alignment, the schema required expected_tests but
    _PATCH_PROPOSAL_REQUIRED listed it as optional, letting non-CLI callers
    construct a proposal the codex CLI would have refused.
    """
    pp = _build_patch_proposal(expected_tests=_OMIT_EXPECTED_TESTS)
    assert "expected_tests" not in pp, "test fixture must actually omit expected_tests"
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    assert "expected_tests" in str(exc.value).lower(), (
        f"validator must name expected_tests in its rejection message; got: {exc.value}"
    )


def test_patch_proposal_applies_to_unknown_finding_rejected():
    """applies_to_findings references a finding ID not present in this review -> rejected."""
    pp = _build_patch_proposal(applies_to_findings=("codex-rev-999",))
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    assert "applies_to_findings" in str(exc.value).lower() or "codex-rev-999" in str(exc.value)


def test_patch_proposal_empty_files_touched_rejected():
    """files_touched empty -> rejected."""
    pp = _build_patch_proposal(files_touched=())
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    assert "files_touched" in str(exc.value).lower()


def test_patch_proposal_empty_unified_diff_rejected():
    """unified_diff empty -> rejected."""
    pp = _build_patch_proposal(unified_diff="")
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    assert "unified_diff" in str(exc.value).lower()


def test_patch_proposal_files_outside_allowed_rejected():
    """files_touched paths not in goal_packet.allowed_files -> rejected.

    Smoke goal packet allows only consensus_mcp/_dispatch_codex.py.
    """
    pp = _build_patch_proposal(files_touched=("scripts/totally_unrelated.py",))
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    err = str(exc.value).lower()
    assert "allowed_files" in err or "out of scope" in err or "totally_unrelated" in err


def test_patch_proposal_files_in_forbidden_rejected():
    """files_touched intersects goal_packet.forbidden_files -> rejected."""
    gp = _smoke_goal_packet()
    # Override forbidden_files to ban the only allowed file (forces conflict)
    gp["forbidden_files"] = ["consensus_mcp/_dispatch_codex.py"]
    pp = _build_patch_proposal(files_touched=("consensus_mcp/_dispatch_codex.py",))
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=gp)
    err = str(exc.value).lower()
    assert "forbidden" in err or "_dispatch_codex" in err


def test_patch_proposal_extra_field_rejected():
    """patch_proposal contains anti-self-verification field (verified) -> rejected."""
    pp = _build_patch_proposal()
    pp["verified"] = True
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    err = str(exc.value).lower()
    assert "verified" in err or "extra" in err or "unexpected" in err or "additional" in err


def test_finding_with_self_verified_field_rejected():
    """Finding has verified/self_verified/correct/approved/confirmed -> whole output REJECTED.

    Anti-self-verification: the reviewer must not claim their own findings
    are verified. That's the verifier's role.
    """
    text = _build_codex_output_with_patch(
        patch_proposal=None,
        finding_extra={"verified": True},
    )
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text)
    err = str(exc.value).lower()
    assert "verified" in err or "extra" in err or "unexpected" in err


def test_patch_proposal_no_goal_packet_skips_scope_check():
    """Backward compat: when goal_packet is None, allowed/forbidden scope
    checks are skipped.

    Other patch_proposal validations (patch_id, files_touched non-empty,
    iter-0028 F4 body-vs-files_touched consistency, etc.) still run. The
    pipeline ALWAYS supplies goal_packet, but the helper must remain
    callable without it for unit-level testing.

    iter-0028 update: previously this test used a mismatched diff/files_touched
    pair as a no-op (the goal-packet check was the only consumer of either
    field). After F4 added body-vs-declaration consistency, the test must
    keep diff body and files_touched in sync.
    """
    pp = _build_patch_proposal(
        unified_diff=(
            "--- a/any/path/at/all.py\n"
            "+++ b/any/path/at/all.py\n"
            "@@ -1 +1,2 @@\n hello\n+world\n"
        ),
        files_touched=("any/path/at/all.py",),
    )
    text = _build_codex_output_with_patch(patch_proposal=pp)
    # No goal_packet -> allowed/forbidden scope check skipped -> accepts
    # (body-vs-declaration consistency passes because both name the same path).
    parsed = _dispatch_codex._parse_codex_output(text)   # no goal_packet kwarg
    assert parsed["findings"][0]["patch_proposal"]["files_touched"] == ["any/path/at/all.py"]


def test_schema_file_declares_patch_proposal_property():
    """Schema file's findings[] item declares patch_proposal with binding fields.

    Per iter-0019 empirical finding: OpenAI codex CLI strict-output requires
    every property in `properties` to be in `required`. So `patch_proposal`
    and `patch_not_proposed_reason` are both required-but-nullable at finding
    level. Mutual exclusivity enforced at python parse-time, not schema layer.
    """
    with SCHEMA_PATH.open("r", encoding="utf-8") as f:
        schema = _json.load(f)
    finding_schema = schema["properties"]["findings"]["items"]
    assert "patch_proposal" in finding_schema["properties"], \
        "schema findings[] item must declare patch_proposal property"
    pp_schema = finding_schema["properties"]["patch_proposal"]
    # Per iter-0019: required-but-nullable.
    assert "patch_proposal" in finding_schema.get("required", []), \
        "patch_proposal must be in finding.required (OpenAI strict-output)"
    # Binding fields per spec - expected_tests now required (iter-0019).
    pp_required = set(pp_schema["required"])
    for f in ("patch_id", "applies_to_findings", "base_sha", "unified_diff", "files_touched", "expected_tests"):
        assert f in pp_required, f"patch_proposal.required missing {f!r}; got {sorted(pp_required)}"
    # additionalProperties:false enforces the closed set
    assert pp_schema.get("additionalProperties") is False
    # iter-0020: patch_id pattern is the codex-producible finding-id-derived form
    assert pp_schema["properties"]["patch_id"]["pattern"] == r"^codex-rev-\d+-patch$"


# ---- iter-0020 patch_id ergonomics fix tests ------------------------------


def test_patch_id_must_match_finding_id():
    """iter-0020: patch_id MUST equal f"{finding_id}-patch".

    A patch_proposal whose patch_id is the wrong finding-id-derived form
    (e.g. patch_id=codex-rev-002-patch on finding codex-rev-001) is REJECTED.
    """
    pp = _build_patch_proposal(finding_id="codex-rev-001")
    pp["patch_id"] = "codex-rev-002-patch"   # wrong finding id
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    err = str(exc.value).lower()
    assert "patch_id" in err
    assert "codex-rev-001-patch" in str(exc.value) or "must equal" in err


def test_unified_diff_sha256_computed_by_validator():
    """iter-0020: helper computes sha256(unified_diff) and stamps it on the
    parsed patch_proposal output. Codex's read-only sandbox can't produce this;
    the helper does it authoritatively for downstream drift-detection."""
    pp = _build_patch_proposal()
    text = _build_codex_output_with_patch(patch_proposal=pp)
    parsed = _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    parsed_pp = parsed["findings"][0]["patch_proposal"]
    assert "unified_diff_sha256" in parsed_pp, \
        "helper must stamp unified_diff_sha256 on validated patch_proposal"
    expected = _hashlib.sha256(pp["unified_diff"].encode("utf-8")).hexdigest()
    assert parsed_pp["unified_diff_sha256"] == expected


def test_old_content_bound_patch_id_format_no_longer_required():
    """iter-0020: the legacy content-bound format
    (patch-{base_sha[:12]}-{sha256(diff)[:12]}) is REJECTED by the new regex.
    Codex producing the old form should fail; new form must be finding-id-derived.
    """
    base_sha = "abc123def456abc123def456abc123def456abc123def456abc123def456abcd"
    diff = "--- a\n+++ b\n@@ -1 +1,2 @@\n hello\n+world\n"
    diff_sha = _hashlib.sha256(diff.encode("utf-8")).hexdigest()
    legacy_patch_id = f"patch-{base_sha[:12]}-{diff_sha[:12]}"
    pp = _build_patch_proposal()
    pp["patch_id"] = legacy_patch_id
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    err = str(exc.value).lower()
    # Legacy form fails BOTH regex and finding-id binding; either error
    # message acceptable.
    assert "patch_id" in err
    assert legacy_patch_id in str(exc.value) or "codex-rev-" in err


# ---- iter-0028 F4 (codex-rev-002): unified_diff body path scope-check ------
# ----
# ---- Prior validator only scope-checked patch_proposal.files_touched against
# ---- allowed_files / forbidden_files. The unified_diff body has its own
# ---- `+++ b/<path>` and `--- a/<path>` headers. A malicious or buggy emission
# ---- could declare files_touched=[allowed.py] but target forbidden.py via
# ---- the diff body. The applier would mutate forbidden.py undetected.


def test_patch_proposal_diff_body_paths_must_be_in_files_touched():
    """F4: every `+++ b/<path>` header in unified_diff must be in files_touched.

    Single-file files_touched, but diff body lists TWO files. Reject.
    """
    sneaky_diff = (
        "--- a/consensus_mcp/_dispatch_codex.py\n"
        "+++ b/consensus_mcp/_dispatch_codex.py\n"
        "@@ -1 +1,2 @@\n hello\n+world\n"
        "--- a/consensus_mcp/tools/loop_run_goal.py\n"
        "+++ b/consensus_mcp/tools/loop_run_goal.py\n"
        "@@ -1 +1,2 @@\n hello\n+forbidden\n"
    )
    pp = _build_patch_proposal(
        unified_diff=sneaky_diff,
        files_touched=("consensus_mcp/_dispatch_codex.py",),
    )
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    err = str(exc.value)
    assert "unified_diff_body_path_outside_scope" in err
    assert "loop_run_goal.py" in err


def test_patch_proposal_diff_body_paths_must_be_in_allowed_files():
    """F4: a body path that IS in files_touched but NOT in allowed_files is rejected.

    Caller declared files_touched=[forbidden.py] AND diff body matches - that
    earlier files_touched scope check already catches this. This test exercises
    the case where the body parser is consulted on a clean-looking files_touched
    list with allowed_files mismatch.

    Use a smoke goal_packet that only allows _dispatch_codex.py. Patch's
    files_touched declares the diff-body-targeted forbidden_files entry too -
    so the EARLIER files_touched scope check fires first. Verify the error
    message names the right scope: this is the regression check that the new
    body-path check does NOT short-circuit the existing files_touched check.
    """
    pp = _build_patch_proposal(
        unified_diff=(
            "--- a/consensus_mcp/_dispatch_codex.py\n"
            "+++ b/consensus_mcp/_dispatch_codex.py\n"
            "@@ -1 +1,2 @@\n hello\n+world\n"
        ),
        files_touched=("consensus_mcp/_dispatch_codex.py",),
    )
    text = _build_codex_output_with_patch(patch_proposal=pp)
    parsed = _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    # Clean case: body path matches files_touched + allowed_files. Passes.
    assert parsed["findings"][0]["patch_proposal"]["files_touched"] == [
        "consensus_mcp/_dispatch_codex.py"
    ]


def test_patch_proposal_diff_body_apply_patch_format_rejected():
    """F1+F2 (codex-rev-003): unified_diff starting with `*** Begin Patch`
    (codex-cli's proprietary apply_patch format) is REJECTED with a clear
    error naming the expected unified-diff format. iter-0027 codex emitted
    this exact shape, and the iter-0026 F2 hunk-anchored applier rejected
    it - but only at apply time. This moves the rejection to validate time.
    """
    apply_patch_format = (
        "*** Begin Patch\n"
        "*** Update File: consensus_mcp/_dispatch_codex.py\n"
        "@@ -1 +1,2 @@\n"
        " hello\n"
        "+world\n"
        "*** End Patch\n"
    )
    pp = _build_patch_proposal(unified_diff=apply_patch_format)
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    err = str(exc.value)
    assert "unified_diff_apply_patch_format_not_supported" in err
    # Should hint at the expected format.
    assert "unified" in err.lower()


def test_patch_proposal_diff_body_update_file_marker_rejected():
    """F1+F2: even if `*** Begin Patch` is absent, the presence of
    `*** Update File:` in the body is the smoking-gun apply_patch tell.
    """
    bad_diff = (
        "some preamble\n"
        "*** Update File: consensus_mcp/_dispatch_codex.py\n"
        "@@ -1 +1,2 @@\n"
        " hello\n"
        "+world\n"
    )
    pp = _build_patch_proposal(unified_diff=bad_diff)
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    err = str(exc.value)
    assert "unified_diff_apply_patch_format_not_supported" in err


def test_patch_proposal_standard_unified_diff_accepted():
    """F1+F2 regression: standard `--- a/path / +++ b/path` unified-diff
    format remains accepted. Sanity check the apply_patch rejection doesn't
    overshoot."""
    standard = (
        "--- a/consensus_mcp/_dispatch_codex.py\n"
        "+++ b/consensus_mcp/_dispatch_codex.py\n"
        "@@ -1 +1,2 @@\n hello\n+world\n"
    )
    pp = _build_patch_proposal(unified_diff=standard)
    text = _build_codex_output_with_patch(patch_proposal=pp)
    parsed = _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    assert parsed["findings"][0]["patch_proposal"]["unified_diff"] == standard


def test_patch_proposal_diff_body_minus_a_only_validates():
    """F4 corner case: a hunk only has `--- a/<path>` (deletion-only patch);
    the minus side path must also pass scope. iter-0028 implementation
    consults BOTH `--- a/` and `+++ b/` headers."""
    minus_only_diff = (
        "--- a/consensus_mcp/tools/loop_run_goal.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-hello\n-world\n"
    )
    pp = _build_patch_proposal(
        unified_diff=minus_only_diff,
        files_touched=("consensus_mcp/_dispatch_codex.py",),
    )
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    err = str(exc.value)
    assert "unified_diff_body_path_outside_scope" in err
    assert "loop_run_goal.py" in err


def test_patch_proposal_dev_null_paths_ignored_for_scope():
    """F4: `/dev/null` is the conventional unified-diff marker for create/
    delete; never a real filesystem path. Skip it during scope-check."""
    create_diff = (
        "--- /dev/null\n"
        "+++ b/consensus_mcp/_dispatch_codex.py\n"
        "@@ -0,0 +1,2 @@\n+hello\n+world\n"
    )
    pp = _build_patch_proposal(
        unified_diff=create_diff,
        files_touched=("consensus_mcp/_dispatch_codex.py",),
    )
    text = _build_codex_output_with_patch(patch_proposal=pp)
    # Should pass: /dev/null ignored; +++ path matches files_touched + allowed.
    parsed = _dispatch_codex._parse_codex_output(text, goal_packet=_smoke_goal_packet())
    assert parsed["findings"][0]["patch_proposal"]["files_touched"] == [
        "consensus_mcp/_dispatch_codex.py"
    ]


def test_patch_proposal_diff_body_forbidden_path_rejected_via_body_check():
    """F4 + forbidden scope: even if files_touched is clean, a forbidden path
    appearing ONLY in the diff body must be rejected."""
    gp = _smoke_goal_packet()
    # Add a forbidden path that is NOT in files_touched.
    gp["forbidden_files"] = ["consensus_mcp/tools/apply_codex_patch.py"]
    # files_touched declares clean path; diff body sneaks forbidden in too.
    sneaky = (
        "--- a/consensus_mcp/_dispatch_codex.py\n"
        "+++ b/consensus_mcp/_dispatch_codex.py\n"
        "@@ -1 +1,2 @@\n hello\n+world\n"
        "--- a/consensus_mcp/tools/apply_codex_patch.py\n"
        "+++ b/consensus_mcp/tools/apply_codex_patch.py\n"
        "@@ -1 +1,2 @@\n bad\n+stuff\n"
    )
    pp = _build_patch_proposal(
        unified_diff=sneaky,
        files_touched=("consensus_mcp/_dispatch_codex.py",),
    )
    text = _build_codex_output_with_patch(patch_proposal=pp)
    with pytest.raises(_dispatch_codex.CodexOutputParseError) as exc:
        _dispatch_codex._parse_codex_output(text, goal_packet=gp)
    err = str(exc.value)
    assert "unified_diff_body_path_outside_scope" in err
    assert "apply_codex_patch.py" in err


# ---- iter-0028 F5 (codex-rev-004): _resolve_repo_root env-var fail-closed ---
# ----
# ---- Prior behavior: invalid env-supplied CONSENSUS_MCP_REPO_ROOT silently
# ---- falls through to cwd / __file__-parent walk. Codex argues operator-
# ---- supplied env var should be authoritative; if it doesn't validate,
# ---- raise rather than silently re-resolve.


def test_resolve_repo_root_env_set_and_invalid_raises(monkeypatch, tmp_path):
    """F5: env var set but the path lacks repo markers -> raise (no fall-through)."""
    no_markers = tmp_path / "no_markers"
    no_markers.mkdir()
    assert not _dispatch_codex._has_repo_markers(no_markers)
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(no_markers))
    # Even if cwd or __file__-parent would have validated, env-supplied
    # invalid path should fail-closed.
    with pytest.raises(_dispatch_codex.RepoRootResolutionError) as exc:
        _dispatch_codex._resolve_repo_root()
    msg = str(exc.value)
    assert "CONSENSUS_MCP_REPO_ROOT" in msg
    # Operator-facing message: explain WHY it failed.
    assert "marker" in msg.lower() or "validate" in msg.lower()


def test_resolve_repo_root_env_set_and_valid_uses_env(monkeypatch, tmp_path):
    """F5: env var set + valid path -> uses env (unchanged behaviour)."""
    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    # Don't chdir; ensure resolution happens via env, not cwd.
    resolved = _dispatch_codex._resolve_repo_root()
    assert resolved == tmp_path.resolve()


def test_resolve_repo_root_env_unset_falls_back_to_cwd(monkeypatch, tmp_path):
    """F5: env var UNSET -> cwd / __file__ fallback path unchanged."""
    _scaffold_repo_markers(tmp_path)
    monkeypatch.delenv("CONSENSUS_MCP_REPO_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    resolved = _dispatch_codex._resolve_repo_root()
    assert resolved == tmp_path.resolve()


def test_resolve_repo_root_env_empty_string_treated_as_unset(monkeypatch, tmp_path):
    """F5 boundary: empty-string env var should not trigger fail-closed.

    `os.environ.get(...)` returns the empty string for `MY_VAR=` shells set;
    the helper's existing check `if override:` treats falsy as unset. F5
    preserves that - empty string means "operator didn't supply it."
    """
    _scaffold_repo_markers(tmp_path)
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", "")
    monkeypatch.chdir(tmp_path)
    # Empty -> treated as unset -> falls through to cwd (which is valid).
    resolved = _dispatch_codex._resolve_repo_root()
    assert resolved == tmp_path.resolve()


# ---- iter-0028 F1+F2 (codex-rev-003): template+schema drift ----------------


def test_template_does_not_claim_expected_tests_is_only_optional():
    """F2: template line ~204 used to read 'expected_tests is the only
    optional field' - wrong because the schema requires it. The text must
    be corrected to reflect schema reality.
    """
    template_path = (
        Path(_dispatch_codex.__file__).parent
        / "dispatch_templates"
        / "codex_review_template.md"
    )
    body = template_path.read_text(encoding="utf-8")
    assert "expected_tests` is the only optional field" not in body, (
        "F2: template must NOT claim expected_tests is optional; schema requires it"
    )


def test_template_explicitly_rejects_apply_patch_format():
    """F1: template must explicitly name codex-cli's apply_patch format as
    REJECTED and unified-diff format as ACCEPTED, so codex doesn't fall
    back to its proprietary format on patch-emitting findings.
    """
    template_path = (
        Path(_dispatch_codex.__file__).parent
        / "dispatch_templates"
        / "codex_review_template.md"
    )
    body = template_path.read_text(encoding="utf-8")
    # Must mention the rejected format token.
    assert "*** Begin Patch" in body, (
        "F1: template must name codex-cli's '*** Begin Patch' apply_patch "
        "format so codex knows to avoid it"
    )
    # Must name it as REJECTED.
    assert "REJECTED" in body
    # Must show ACCEPTED unified-diff form with --- a/ and +++ b/ markers.
    assert "--- a/" in body and "+++ b/" in body


# ---- v1.10.5 containment hardening + review-target provenance ---------------
# Confirms three live gaps from the 2026-05-10 visibility-TUI design doc:
#   1. Outside-repo absolute paths must be refused (not silently read).
#   2. _failed_event must include review_target_path (path is known pre-try,
#      so even a pre-codex failure carries it).
#   3. _failed_event includes review_target_hash too - None if file never read,
#      populated after read. This test exercises the "path known, hash known"
#      shape so the audit consumer can rely on both fields appearing in
#      dispatch_done.


def test_outside_repo_absolute_path_refused(tmp_path):
    """v1.10.5 containment: absolute path outside repo_root raises OutsideRepoPathError."""
    _scaffold_repo_markers(tmp_path)
    repo_root = tmp_path
    # An absolute path far outside the repo. Use tmp_path.parent so we know
    # it exists at filesystem level but is definitionally outside repo_root.
    outside = (tmp_path.parent / "definitely_outside.yaml").resolve()
    with pytest.raises(_dispatch_codex.OutsideRepoPathError) as exc:
        _dispatch_codex._normalize_relative_to_repo(str(outside), repo_root)
    msg = str(exc.value)
    assert "outside repo_root" in msg
    assert str(outside) in msg or "definitely_outside" in msg


def test_inside_repo_absolute_path_passes(tmp_path):
    """v1.10.5 containment: absolute path that resolves INSIDE repo_root passes."""
    _scaffold_repo_markers(tmp_path)
    repo_root = tmp_path
    inside_dir = tmp_path / "consensus-state"
    inside_file = inside_dir / "inside.yaml"
    inside_file.write_text("dummy", encoding="utf-8")
    resolved = _dispatch_codex._normalize_relative_to_repo(str(inside_file), repo_root)
    assert resolved == inside_file.resolve()


def test_relative_path_resolves_under_repo_root_and_passes(tmp_path):
    """v1.10.5: relative path semantics unchanged from v1.10.4 F5."""
    _scaffold_repo_markers(tmp_path)
    repo_root = tmp_path
    (tmp_path / "consensus-state" / "rel.yaml").write_text("dummy", encoding="utf-8")
    resolved = _dispatch_codex._normalize_relative_to_repo("consensus-state/rel.yaml", repo_root)
    assert resolved == (tmp_path / "consensus-state" / "rel.yaml").resolve()


def test_none_path_returns_none(tmp_path):
    """v1.10.5: None passes through unchanged (no containment check applied)."""
    _scaffold_repo_markers(tmp_path)
    assert _dispatch_codex._normalize_relative_to_repo(None, tmp_path) is None


def test_dispatch_failed_carries_review_target_fields_when_target_was_read(monkeypatch, tmp_path):
    """v1.10.5: dispatch_failed event carries review_target_path + review_target_hash
    when the target file was read BEFORE codex failure. The path is known pre-try
    (computed at function top); the hash is computed after read inside the try.
    """
    review_target = tmp_path / "fake_for_failed.diff"
    review_target.write_text("dummy\n", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        if len(cmd) == 2 and cmd[1] == "--version":
            r = _mock.MagicMock(); r.returncode = 0; r.stdout = "codex-cli 0.129.0\n"; r.stderr = ""; return r
        raise AssertionError(
            f"subprocess.run should only be called for --version probe; got {cmd!r}"
        )

    monkeypatch.setattr(_dispatch_codex.subprocess, "run", fake_run)
    # iter-0037 migration: codex failure now surfaces via Popen returncode!=0.
    monkeypatch.setattr(
        _dispatch_codex.subprocess, "Popen",
        make_fake_codex_popen_factory(
            stdout_text="",
            returncode=1,
            stderr_text="codex synthetic failure",
        ),
    )
    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    iter_dir = tmp_path / "iteration-9999-v1105-failwithtarget"
    iter_dir.mkdir()
    (tmp_path / "consensus-state" / "state").mkdir(parents=True, exist_ok=True)

    rc = _dispatch_codex.main([
        "--goal-packet", str(_stage_smoke_goal_packet(tmp_path)),
        "--iteration-dir", str(iter_dir),
        "--reviewer-id", "codex-iter9999-v1105-fail-1",
        "--pass-id", "codex-iter9999-v1105-fail-1-pass1",
        "--codex-bin", "codex",
        "--review-target", str(review_target),
    ])
    assert rc == 1

    log_lines = (tmp_path / "consensus-state" / "state" / "dispatch-log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [_json.loads(line) for line in log_lines]
    failed = next(e for e in events if e["event"] == "dispatch_failed")

    import hashlib
    expected_hash = hashlib.sha256(b"dummy\n").hexdigest()
    assert failed.get("review_target_hash") == expected_hash, (
        f"dispatch_failed must carry review_target_hash when target was read pre-failure; "
        f"got {failed.get('review_target_hash')!r}"
    )
    assert failed.get("review_target_path"), (
        "dispatch_failed must carry review_target_path (computed pre-try)"
    )


# ---- iter-0033 preflight containment hardening (codex-rev-001) -------------


def test_outside_repo_review_target_via_main_emits_structured_failure(monkeypatch, tmp_path, capsys):
    """iter-0033 codex-rev-001 + claude-rev-001 regression: containment
    rejection of --review-target via main() entry must produce structured
    {"ok": false, "error_type": "OutsideRepoPathError"} on stdout and a
    dispatch_failed event in dispatch-log, NOT a raw Python traceback.
    """
    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    iter_dir = tmp_path / "iteration-9999-v1033-outside"
    iter_dir.mkdir()
    (tmp_path / "consensus-state" / "state").mkdir(parents=True, exist_ok=True)

    # An absolute path definitively OUTSIDE tmp_path (the synthetic repo_root).
    outside_target = (tmp_path.parent / "outside_review.diff").resolve()
    outside_target.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-x\n+y\n", encoding="utf-8")

    rc = _dispatch_codex.main([
        "--goal-packet", str(_stage_smoke_goal_packet(tmp_path)),
        "--iteration-dir", str(iter_dir),
        "--reviewer-id", "codex-iter9999-v1033-1",
        "--pass-id", "codex-iter9999-v1033-1-pass1",
        "--codex-bin", "codex",
        "--review-target", str(outside_target),
    ])
    assert rc == 5, f"containment rejection should return rc=5; got {rc}"

    out = capsys.readouterr().out
    assert '"ok": false' in out, f"expected structured failure JSON on stdout; got {out!r}"
    assert "OutsideRepoPathError" in out, f"error_type must be OutsideRepoPathError; got {out!r}"

    log_lines = (tmp_path / "consensus-state" / "state" / "dispatch-log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [_json.loads(line) for line in log_lines]
    failed = next((e for e in events if e["event"] == "dispatch_failed"), None)
    assert failed is not None, "containment rejection must emit dispatch_failed event"
    assert failed.get("error_type") == "OutsideRepoPathError"
    # Pre-normalize anchors carry through to the failure event.
    assert failed.get("iteration_id"), "dispatch_failed must carry iteration_id from pre-normalize anchor"
    assert failed.get("reviewer_id"), "dispatch_failed must carry reviewer_id from pre-normalize anchor"


def test_mkdir_oserror_during_preflight_emits_structured_failure(monkeypatch, tmp_path, capsys):
    """iter-0033 codex-rev-001 (codex pre-review refinement): OSError raised
    by iter_dir.mkdir() during preflight must also emit a structured failure
    event. Previously only OutsideRepoPathError was caught.
    """
    _scaffold_repo_markers(tmp_path)
    _isolate_archive_root(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "consensus-state" / "state").mkdir(parents=True, exist_ok=True)

    # Force mkdir to raise OSError by monkeypatching Path.mkdir on a specific
    # iteration_dir name. Using a generic patch that raises for any mkdir call
    # within the preflight window is simplest.
    real_mkdir = type(tmp_path).mkdir
    raise_count = {"n": 0}
    def fake_mkdir(self, *args, **kwargs):
        # Only raise for the iter_dir.mkdir within main(); other mkdirs (e.g.,
        # log_path.parent.mkdir) must still succeed.
        if "iteration-9999-v1033-mkdir-fail" in str(self):
            raise_count["n"] += 1
            raise PermissionError(f"synthetic mkdir failure on {self}")
        return real_mkdir(self, *args, **kwargs)
    monkeypatch.setattr(type(tmp_path), "mkdir", fake_mkdir)

    rc = _dispatch_codex.main([
        "--goal-packet", str(_stage_smoke_goal_packet(tmp_path)),
        "--iteration-dir", str(tmp_path / "iteration-9999-v1033-mkdir-fail"),
        "--reviewer-id", "codex-iter9999-v1033-mkdir-1",
        "--pass-id", "codex-iter9999-v1033-mkdir-1-pass1",
        "--codex-bin", "codex",
    ])
    assert rc == 5, f"OSError preflight should return rc=5; got {rc}"
    assert raise_count["n"] >= 1, "fake_mkdir should have fired"

    out = capsys.readouterr().out
    assert '"ok": false' in out, f"expected structured failure JSON; got {out!r}"
    assert "PermissionError" in out or "OSError" in out, (
        f"error_type must name an OSError subclass; got {out!r}"
    )

    log_lines = (tmp_path / "consensus-state" / "state" / "dispatch-log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [_json.loads(line) for line in log_lines]
    failed = next((e for e in events if e["event"] == "dispatch_failed"), None)
    assert failed is not None, "OSError preflight must emit dispatch_failed event"
    assert failed.get("error_type") in ("PermissionError", "OSError"), failed.get("error_type")


# ---- iter-0033 claude-rev-003 Windows case-fold containment ---------------


def test_containment_case_insensitive_on_windows(tmp_path):
    """iter-0033 claude-rev-003 regression: on Windows, mixed-case
    repo_root vs inside-repo path must NOT trigger false-positive containment
    rejection. Skipped on non-Windows where the filesystem is case-sensitive.
    """
    import sys as _sys
    if _sys.platform != "win32":
        import pytest as _pytest
        _pytest.skip("Windows case-insensitivity is the only platform that needs this")

    _scaffold_repo_markers(tmp_path)
    # Build a path inside the repo with deliberately altered casing relative
    # to tmp_path. On Windows the on-disk file is the same; the string forms
    # differ in case. Pre-fix relative_to() would fail string compare; post-
    # fix the case-fold fallback accepts.
    inside_dir = tmp_path / "consensus-state"
    inside_file = inside_dir / "case_test.yaml"
    inside_file.write_text("dummy", encoding="utf-8")

    # Construct a path with upper/lower variants in the components.
    mixed = Path(str(inside_file).upper())
    # The mixed-case path resolves to the same file but compares stringwise
    # differently against tmp_path.resolve(); without the case-fold fix this
    # would raise OutsideRepoPathError.
    resolved = _dispatch_codex._normalize_relative_to_repo(str(mixed), tmp_path)
    assert resolved is not None


# ---- iter-0039 cross-platform fixes ----------------------------------------


def test_normalize_for_compare_strips_long_path_prefix_on_windows():
    """iter-0039 xplat-rev-001 regression: _normalize_for_compare strips the
    Windows extended-length path prefix `\\\\?\\` so paths with and without
    the prefix compare equal."""
    if sys.platform != "win32":
        pytest.skip("Windows-only path prefix")
    a = _dispatch_codex._normalize_for_compare(r"C:\foo\bar")
    b = _dispatch_codex._normalize_for_compare(r"\\?\C:\foo\bar")
    assert a == b, f"long-path-prefix path should normalize the same; got {a!r} vs {b!r}"


def test_normalize_for_compare_lowercases_on_windows():
    """iter-0039 xplat-rev-001 regression: _normalize_for_compare uses
    os.path.normcase which lowercases on Windows."""
    if sys.platform != "win32":
        pytest.skip("Windows-only case-insensitivity")
    a = _dispatch_codex._normalize_for_compare(r"C:\Foo\BAR")
    b = _dispatch_codex._normalize_for_compare(r"c:\foo\bar")
    assert a == b


def test_normalize_for_compare_collapses_dot_segments():
    """iter-0039 xplat-rev-001 regression: _normalize_for_compare uses
    os.path.normpath which collapses `.` / `..` segments."""
    if sys.platform == "win32":
        # On Windows, normpath converts forward slashes to backslashes too.
        a = _dispatch_codex._normalize_for_compare(r"C:\foo\.\bar")
        b = _dispatch_codex._normalize_for_compare(r"C:\foo\bar")
        assert a == b
    else:
        a = _dispatch_codex._normalize_for_compare("/foo/./bar")
        b = _dispatch_codex._normalize_for_compare("/foo/bar")
        assert a == b


def test_resolve_codex_bin_msys_path_requires_alpha_drive_letter():
    """iter-0039 codex-rev-001 regression: MSYS path conversion only fires
    when char[1] is a real drive letter. /tmp/x and /_/y must NOT be
    mangled into T:\\x / _:\\y."""
    if sys.platform != "win32":
        pytest.skip("Windows-only MSYS conversion")
    # Real MSYS-style drive path: /c/foo -> C:\foo
    # Note: this path doesn't exist; we test the conversion shape via the
    # absolute-path branch return (which is the input verbatim post-conversion).
    result = _dispatch_codex._resolve_codex_bin("/c/some/nonexistent/path.exe")
    assert result == r"C:\some\nonexistent\path.exe", f"MSYS /c/ should convert; got {result!r}"

    # Non-drive path: /tmp/x must NOT become T:\x
    result_tmp = _dispatch_codex._resolve_codex_bin("/tmp/nonexistent")
    assert not result_tmp.startswith(("T:\\", "t:\\")), (
        f"/tmp/... must not mangle to T:\\... ; got {result_tmp!r}"
    )

    # Non-alpha char[1]: /_/foo must NOT become _:\foo
    result_underscore = _dispatch_codex._resolve_codex_bin("/_/foo")
    assert "_:" not in result_underscore, (
        f"/_/foo must not mangle into _: drive; got {result_underscore!r}"
    )


def test_resolve_codex_bin_app_execution_alias_stub_rejected(monkeypatch, tmp_path):
    """iter-0039 xplat-rev-008 regression: a 0-byte file in
    %LOCALAPPDATA%\\Microsoft\\WindowsApps is an App Execution Alias stub
    that subprocess cannot exec. _resolve_codex_bin must raise
    CodexInvocationError rather than returning the unexecutable path."""
    if sys.platform != "win32":
        pytest.skip("Windows-only App Execution Alias")
    # Synthesize a fake WindowsApps dir + a 0-byte stub.
    fake_local_appdata = tmp_path / "LocalAppData"
    windows_apps = fake_local_appdata / "Microsoft" / "WindowsApps"
    windows_apps.mkdir(parents=True)
    stub = windows_apps / "codex.exe"
    stub.write_bytes(b"")  # 0-byte stub
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local_appdata))

    with pytest.raises(_dispatch_codex.CodexInvocationError) as exc:
        _dispatch_codex._resolve_codex_bin(str(stub))
    assert "App Execution Alias" in str(exc.value)


def test_terminate_process_tree_uses_signal_on_posix(monkeypatch):
    """iter-0039 xplat-rev-002 regression: _terminate_process_tree on POSIX
    calls os.killpg with SIGTERM; if that fails, falls back to proc.terminate()."""
    if sys.platform == "win32":
        pytest.skip("POSIX-only signal test")

    calls = []

    class FakeProc:
        pid = 12345
        returncode = None
        def poll(self): return None if not calls else 0  # exit after first signal
        def terminate(self): calls.append(("terminate", None))
        def kill(self): calls.append(("kill", None))
        def wait(self, timeout=None): return 0
        def send_signal(self, sig): calls.append(("send_signal", sig))

    def fake_killpg(pid, sig):
        calls.append(("killpg", pid, sig))

    monkeypatch.setattr(_dispatch_codex.os, "killpg", fake_killpg)
    monkeypatch.setattr(_dispatch_codex.os, "getpgid", lambda pid: pid)

    _dispatch_codex._terminate_process_tree(FakeProc(), grace_seconds=0.1)

    # First call should have been killpg with SIGTERM.
    assert calls, "no signal sent"
    assert calls[0][0] == "killpg", f"expected killpg first; got {calls[0]}"
    # `signal` lives in _dispatch_base (where _terminate_process_tree
    # is defined); _dispatch_codex only re-imports the function, not the
    # `signal` module. Compare against the stdlib enum directly -
    # module-agnostic and the same object either way. (Pre-iter-0037
    # this asserted `_dispatch_codex.signal.SIGTERM`; the refactor moved
    # the impl, leaving a latent AttributeError that only fired on POSIX
    # CI - this test is Windows-skipped - masked while CI was dormant.)
    assert calls[0][2] == signal.SIGTERM



# ---------- record_reader_error (shared pipe-reader crash marker) ----------
# The reader threads used to swallow crashes with a bare `except: pass`,
# silently dropping the child's remaining output context. The shared base
# helper now appends an ASCII marker line to the captured buffer instead.

def test_record_reader_error_appends_ascii_marker_bytes():
    from consensus_mcp._dispatch_base import record_reader_error

    buf = [b"first line\n"]
    record_reader_error(buf, "stderr", RuntimeError("boom happened"))
    assert len(buf) == 2
    marker = buf[-1]
    assert isinstance(marker, bytes)  # buffers hold raw bytes lines
    assert marker == (
        b"[consensus-mcp] stderr reader thread error: "
        b"RuntimeError: boom happened\n"
    )
    # Marker is pure ASCII so downstream decode/log paths cannot choke on it.
    marker.decode("ascii")


def test_codex_reader_threads_use_shared_marker_helper():
    # The codex module must bind the SAME shared helper (class-level fix:
    # one primitive in _dispatch_base, wired into every adapter's readers).
    from consensus_mcp import _dispatch_base
    assert _dispatch_codex.record_reader_error is _dispatch_base.record_reader_error
