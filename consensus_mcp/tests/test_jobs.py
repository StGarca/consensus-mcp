"""Job contract tests use importable fake callables, never reviewer CLIs."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

from consensus_mcp import _jobs, server
from consensus_mcp.tool_registry import ToolRegistry


FAKE_MODULE = '''
import os
from pathlib import Path
import subprocess
import sys
import time

def handle(seconds=0, mode="success", ready=None, heartbeat=None):
    if heartbeat:
        child = "import pathlib,time; p=pathlib.Path(__import__('sys').argv[1]); "
        child += "\\nwhile True: p.write_text(str(time.time())); time.sleep(0.05)"
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([sys.executable, "-c", child, heartbeat], **options)
        while not Path(heartbeat).exists():
            time.sleep(0.01)
    if ready:
        Path(ready).write_text(str(os.getpid()))
        print("worker output must never enter the MCP wire", flush=True)
    time.sleep(seconds)
    if mode == "exception":
        raise RuntimeError("fake failure")
    if mode == "crash":
        os._exit(7)
    return {"ok": mode != "failure", "pid": os.getpid(), "error": "fake failure" if mode == "failure" else None}

def capture(seconds):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        time.sleep(seconds)
    return {"captured": buf.getvalue()}
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    (tmp_path / "fake_job_callable.py").write_text(FAKE_MODULE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path / "consensus-state"))
    sys.modules.pop("fake_job_callable", None)
    module = importlib.import_module("fake_job_callable")
    registry = ToolRegistry()
    for name in _jobs.ALLOWED_TOOLS:
        registry.register(name, {}, module.handle)
    jobs = _jobs.register(registry)
    started = []
    original_start = jobs.start

    def start(*args, **kwargs):
        record = original_start(*args, **kwargs)
        started.append(record["job_id"])
        return record

    monkeypatch.setattr(jobs, "start", start)
    yield jobs
    for job_id in started:
        jobs.cancel(job_id)
        wait_for(lambda: jobs.status(job_id)["status"] in _jobs.TERMINAL)
    sys.modules.pop("fake_job_callable", None)


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("timed out waiting for fake job")


def finished(jobs, job_id):
    wait_for(lambda: jobs.status(job_id)["status"] in _jobs.TERMINAL)
    return jobs.result(job_id)


@pytest.mark.parametrize("tool_name", _jobs.ALLOWED_TOOLS)
def test_start_status_result_and_persistence(fake, tool_name):
    initial = fake.start(tool_name, {"seconds": 0.1})
    assert initial["status"] == "queued"
    job_id = initial["job_id"]
    assert fake.result(job_id)["result"] is None
    record = finished(fake, job_id)
    assert record["status"] == "succeeded"
    assert record["exit_code"] == 0
    assert record["result"]["pid"] != os.getpid()
    assert record["created_at"] <= record["started_at"] <= record["finished_at"]
    assert json.loads(_jobs._path(job_id).read_text())["result"] == record["result"]
    restarted = _jobs.Jobs(ToolRegistry())
    assert restarted.status(job_id) == fake.status(job_id)
    assert restarted.result(job_id) == record
    assert fake.cancel(job_id)["status"] == "succeeded"


@pytest.mark.parametrize("mode,exit_code", [("failure", 1), ("exception", 1), ("crash", 7)])
def test_failed_job(fake, mode, exit_code):
    record = finished(fake, fake.start(_jobs.ALLOWED_TOOLS[0], {"mode": mode})["job_id"])
    assert record["status"] == "failed"
    assert record["exit_code"] == exit_code
    assert record["error"]
    if mode == "failure":
        assert record["result"]["ok"] is False


def test_start_failure_persists(fake, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("cannot spawn")
    monkeypatch.setattr(_jobs, "_popen", fail)
    record = fake.start(_jobs.ALLOWED_TOOLS[0], {})
    assert record["status"] == "failed"
    assert fake.result(record["job_id"])["error"] == "cannot spawn"


def test_allowlist_and_job_id_validation(fake):
    for tool_name in ("audit.append_event", "consensus.job_start", "reviewer.dispatch_unknown"):
        with pytest.raises(ValueError, match="allowlisted"):
            fake.start(tool_name, {})
    with pytest.raises(ValueError, match="object"):
        fake.start(_jobs.ALLOWED_TOOLS[0], [])
    for job_id in ("../outside", "", "A" * 32):
        for method in (fake.status, fake.result, fake.cancel):
            with pytest.raises(ValueError, match="invalid job_id"):
                method(job_id)
    with pytest.raises(FileNotFoundError):
        fake.status("0" * 32)


def test_open_status_reader_does_not_drop_state_update(tmp_path):
    path = tmp_path / "job.json"
    _jobs._write(path, {"status": "running"})
    opened = threading.Event()
    def reader():
        with path.open(encoding="utf-8"):
            opened.set()
            time.sleep(0.05)
    thread = threading.Thread(target=reader)
    thread.start()
    assert opened.wait(timeout=1)
    _jobs._write(path, {"status": "succeeded"})
    thread.join()
    assert _jobs._read(path)["status"] == "succeeded"


def test_cancel_kills_worker_and_detached_descendant(fake, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    ready = tmp_path / "ready"
    job_id = fake.start(_jobs.ALLOWED_TOOLS[0], {
        "seconds": 10, "ready": str(ready), "heartbeat": str(heartbeat),
    })["job_id"]
    wait_for(ready.exists)
    # A new server instance can cancel a job started by the previous one.
    restarted = _jobs.Jobs(ToolRegistry())
    response = restarted.cancel(job_id)
    assert response["cancel_requested"] is True
    record = finished(restarted, job_id)
    assert record["status"] == "cancelled"
    assert record["exit_code"] != 0
    last_heartbeat = heartbeat.read_text()
    time.sleep(0.3)
    assert heartbeat.read_text() == last_heartbeat
    assert restarted.cancel(job_id)["status"] == "cancelled"


def test_cancel_queued_job_before_launch(fake, monkeypatch):
    captured = []
    class FakeProcess:
        def wait(self):
            return 0
    monkeypatch.setattr(_jobs, "_popen", lambda path: captured.append(path) or FakeProcess())
    job_id = fake.start(_jobs.ALLOWED_TOOLS[0], {})["job_id"]
    fake.cancel(job_id)
    _jobs._supervise(captured[0])
    assert fake.status(job_id)["status"] == "cancelled"
    assert fake.status(job_id)["started_at"] is None


class Client:
    def __init__(self):
        script = (
            "from consensus_mcp import server; import fake_job_callable; "
            "server.registry.register('reviewer.dispatch_codex', {}, fake_job_callable.handle); "
            "server.registry.register('fake.capture', {}, fake_job_callable.capture); "
            "server._serve_stdio()"
        )
        self.proc = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.messages = queue.Queue()
        self.notifications = []
        self.counter = 0
        def read():
            for line in self.proc.stdout:
                self.messages.put(json.loads(line))
        threading.Thread(target=read, daemon=True).start()
        self.call("initialize")

    def receive(self, timeout=10):
        return self.messages.get(timeout=timeout)

    def call(self, method, params=None, timeout=10):
        self.counter += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.counter,
                                         "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            message = self.receive(max(0.01, deadline - time.monotonic()))
            if "id" in message:
                assert message["id"] == self.counter
                assert "error" not in message, message
                return message["result"]
            self.notifications.append(message)

    def tool(self, name, arguments, meta=None, timeout=10):
        result = self.call("tools/call", {"name": name, "arguments": arguments,
                                         "_meta": meta or {}}, timeout=timeout)
        return json.loads(result["content"][0]["text"])

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        assert self.proc.returncode == 0, self.proc.stderr.read()


def test_stdio_responsive_progress_and_live_restart(fake, tmp_path):
    client = Client()
    job_id = None
    try:
        ready = tmp_path / "wire-ready"
        start = time.monotonic()
        initial = client.tool("consensus.job_start", {
            "tool_name": "reviewer.dispatch_codex",
            "arguments": {"seconds": 10, "ready": str(ready)},
        }, {"progressToken": "token-1"}, timeout=1)
        assert time.monotonic() - start < 1
        job_id = initial["job_id"]
        wait_for(ready.exists)
        start = time.monotonic()
        tools = client.call("tools/list", timeout=1)["tools"]
        assert {"consensus.job_start", "consensus.job_status", "consensus.job_result",
                "consensus.job_cancel"} <= {tool["name"] for tool in tools}
        assert client.tool("consensus.job_status", {"job_id": job_id}, timeout=1)["status"] == "running"
        # Existing synchronous handlers still answer normally while a job runs.
        assert client.tool("reviewer.dispatch_codex", {}, timeout=1)["ok"]
        assert time.monotonic() - start < 1
        assert client.notifications
        assert all(n["method"] == "notifications/progress" for n in client.notifications)
        assert all(n["params"]["progressToken"] == "token-1" for n in client.notifications)
        client.close()
        client = Client()
        assert client.tool("consensus.job_status", {"job_id": job_id})["status"] == "running"
        client.tool("consensus.job_cancel", {"job_id": job_id})
        assert finished(fake, job_id)["status"] == "cancelled"
        assert client.tool("consensus.job_result", {"job_id": job_id})["status"] == "cancelled"
    finally:
        if job_id:
            fake.cancel(job_id)
            finished(fake, job_id)
        client.close()


def test_progress_does_not_corrupt_synchronous_stdout_capture(fake, tmp_path):
    client = Client()
    job_id = None
    try:
        ready = tmp_path / "capture-ready"
        job_id = client.tool("consensus.job_start", {
            "tool_name": "reviewer.dispatch_codex",
            "arguments": {"seconds": 0.3, "ready": str(ready)},
        }, {"progressToken": "capture-token"})["job_id"]
        wait_for(ready.exists)
        assert client.tool("fake.capture", {"seconds": 0.8}) == {"captured": ""}
        assert any(n["params"]["message"] == "succeeded" for n in client.notifications)
    finally:
        if job_id:
            fake.cancel(job_id)
            finished(fake, job_id)
        client.close()


def test_progress_completes_and_absent_token_is_silent(fake, monkeypatch):
    messages = []
    monkeypatch.setattr(server, "registry", fake.registry)
    monkeypatch.setattr(server, "jobs", fake)
    monkeypatch.setattr(server, "_send", messages.append)
    request = {"id": 1, "method": "tools/call", "params": {
        "name": "consensus.job_start",
        "arguments": {"tool_name": _jobs.ALLOWED_TOOLS[0], "arguments": {}},
    }}
    response = server._handle_request(request)
    job_id = json.loads(response["result"]["content"][0]["text"])["job_id"]
    finished(fake, job_id)
    assert messages == []
    request["params"]["_meta"] = {"progressToken": 0}
    response = server._handle_request(request)
    job_id = json.loads(response["result"]["content"][0]["text"])["job_id"]
    finished(fake, job_id)
    wait_for(lambda: messages and messages[-1]["params"]["progress"] == 1)
    assert messages[-1]["params"] == {
        "progressToken": 0, "progress": 1, "total": 1, "message": "succeeded",
    }
    values = [message["params"]["progress"] for message in messages]
    assert all(before < after for before, after in zip(values, values[1:]))


@pytest.mark.parametrize("strict,expected", [(None, 0), ("0", 0), ("1", 2)])
def test_boot_findings_warn_and_audit(tmp_path, monkeypatch, capsys, strict, expected):
    monkeypatch.setattr(server, "_run_disposition_check", lambda: 3)
    monkeypatch.setattr(server, "AUDIT_LOG", tmp_path / "audit.jsonl")
    monkeypatch.delenv("CONSENSUS_MCP_STRICT_BOOT", raising=False)
    if strict is not None:
        monkeypatch.setenv("CONSENSUS_MCP_STRICT_BOOT", strict)
    assert server.main(["--boot-and-exit"]) == expected
    stderr = capsys.readouterr().err
    assert ("ERROR" if expected else "WARNING") in stderr
    events = [json.loads(line) for line in server.AUDIT_LOG.read_text().splitlines()]
    assert events[0] == {
        "event": "mcp_server_disposition_warning", "timestamp_utc": events[0]["timestamp_utc"],
        "findings_at_boot": 3, "strict_boot": expected == 2,
    }
    assert any(e["event"] == "mcp_server_started" for e in events) == (expected == 0)


def test_boot_clean_and_check_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server, "AUDIT_LOG", tmp_path / "audit.jsonl")
    monkeypatch.setenv("CONSENSUS_MCP_STRICT_BOOT", "1")
    monkeypatch.setattr(server, "_run_disposition_check", lambda: 0)
    assert server.main(["--boot-and-exit"]) == 0
    assert capsys.readouterr().err == ""
    def fail():
        raise OSError("missing spec")
    monkeypatch.setattr(server, "_run_disposition_check", fail)
    assert server.main(["--boot-and-exit"]) == 2
    assert "disposition check failed" in capsys.readouterr().err


def test_read_retries_transient_windows_sharing_violation(tmp_path, monkeypatch):
    """A status poll that races the supervisor's atomic replace must not fail."""
    path = tmp_path / "rec.json"
    path.write_text('{"status": "running"}', encoding="utf-8")
    real = Path.read_text
    calls = {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(32, "sharing violation")
        return real(self, *a, **k)

    monkeypatch.setattr(_jobs.os, "name", "nt")
    monkeypatch.setattr(Path, "read_text", flaky)
    assert _jobs._read(path) == {"status": "running"}
    assert calls["n"] == 3
