"""Persistent process-backed jobs for the existing long-running MCP tools.

A detached supervisor owns each job record and reaps its worker, so jobs keep
running (and recording their outcome) when the stdio server restarts. Cancel
requests are files consumed by that supervisor; only it writes running state.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from consensus_mcp._atomic_io import atomic_write_text
from consensus_mcp._paths import state_root


ALLOWED_TOOLS = (
    "reviewer.dispatch_codex", "reviewer.dispatch_gemini",
    "reviewer.dispatch_grok", "reviewer.dispatch_kimi", "consensus.run_iteration",
    "reviewer.dispatch_claude", "reviewer.dispatch_openai_compat",
    "reviewer.dispatch_opencode",
    "consensus.consult_panel",
)
TERMINAL = {"succeeded", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, record: dict) -> None:
    for attempt in range(100):
        try:
            atomic_write_text(path, json.dumps(record))
            return
        except PermissionError:
            # Windows readers briefly prevent replacement of the open record.
            if os.name != "nt" or attempt == 99:
                raise
            time.sleep(0.01)


def _read(path: Path) -> dict:
    for attempt in range(100):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            # Windows: a reader that opens the record while the supervisor's
            # atomic replace is in flight gets a sharing violation. Retry briefly.
            if os.name != "nt" or attempt == 99:
                raise
            time.sleep(0.01)
    raise AssertionError("unreachable")


def _path(job_id: str) -> Path:
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("invalid job_id")
    return state_root() / "state" / "jobs" / f"{job_id}.json"


def _summary(record: dict) -> dict:
    return {key: value for key, value in record.items()
            if key not in {"arguments", "handler", "result"}}


def _popen(path: Path, *, worker: bool = False) -> subprocess.Popen:
    command = [sys.executable, "-m", "consensus_mcp._jobs", str(path)]
    if worker:
        command.append("--worker")
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {"start_new_session": True})
    return subprocess.Popen(command, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **options)


def _windows_kill_tree(pid: int) -> None:
    # Toolhelp avoids taskkill's dependency on WMI, which can hang in sandboxes.
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    api.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    api.OpenProcess.restype = wintypes.HANDLE
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = api.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    pairs = []
    entry = Entry()
    entry.size = ctypes.sizeof(entry)
    try:
        more = api.Process32FirstW(snapshot, ctypes.byref(entry))
        if not more:
            raise ctypes.WinError(ctypes.get_last_error())
        while more:
            pairs.append((entry.pid, entry.parent))
            more = api.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        api.CloseHandle(snapshot)
    descendants = [pid]
    for parent in descendants:
        descendants.extend(child for child, ppid in pairs if ppid == parent and child not in descendants)
    for child in reversed(descendants):
        handle = api.OpenProcess(1 | 0x1000, False, child)  # TERMINATE | QUERY_LIMITED_INFORMATION
        if not handle:
            if ctypes.get_last_error() == 87:  # Process already exited.
                continue
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not api.TerminateProcess(handle, 1):
                error = ctypes.get_last_error()
                code = wintypes.DWORD()
                if not api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259:
                    raise ctypes.WinError(error)  # Ignore a concurrent natural exit.
        finally:
            api.CloseHandle(handle)


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        _windows_kill_tree(proc.pid)
    else:
        # Dispatchers create their own sessions. Walk parentage as well as
        # killing the worker group, so those descendants are also cancelled.
        # If the process listing fails (slow or missing `ps`), still kill the
        # worker and its group below instead of leaving them running.
        descendants = [proc.pid]
        try:
            table = subprocess.check_output(["ps", "-eo", "pid=,ppid="], text=True, timeout=2)
            pairs = [tuple(map(int, line.split())) for line in table.splitlines()]
        except (OSError, subprocess.SubprocessError, ValueError):
            pairs = []
        for parent in descendants:
            descendants.extend(pid for pid, ppid in pairs if ppid == parent)
        for pid in reversed(descendants):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=2)


class Jobs:
    def __init__(self, registry):
        self.registry = registry

    def start(self, tool_name: str, arguments: dict) -> dict:
        if tool_name not in ALLOWED_TOOLS:
            raise ValueError(f"tool is not job-allowlisted: {tool_name}")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        handler = self.registry.get_handler(tool_name)
        job_id = uuid4().hex
        path = _path(job_id)
        record = {
            "job_id": job_id, "tool_name": tool_name, "arguments": arguments,
            "handler": [handler.__module__, handler.__name__],
            "status": "queued", "created_at": _now(), "started_at": None,
            "finished_at": None, "exit_code": None, "result": None, "error": None,
        }
        _write(path, record)
        try:
            proc = _popen(path)
        except Exception as exc:
            record.update(status="failed", finished_at=_now(), error=str(exc))
            _write(path, record)
        else:
            # Reap without tying job lifetime to the server process.
            threading.Thread(target=proc.wait, daemon=True).start()
        return _summary(record)

    def status(self, job_id: str) -> dict:
        return _summary(_read(_path(job_id)))

    def result(self, job_id: str) -> dict:
        record = _read(_path(job_id))
        return {**_summary(record), "result": record["result"]}

    def cancel(self, job_id: str) -> dict:
        path = _path(job_id)
        record = _read(path)
        if record["status"] not in TERMINAL:
            atomic_write_text(path.with_suffix(".cancel"), "cancel")
            return {**_summary(record), "cancel_requested": True}
        return _summary(record)

    def watch(self, job_id: str, progress_token, notify) -> None:
        path = _path(job_id)

        def watch():
            previous = None
            while True:
                record = _read(path)
                status = record["status"]
                if status != previous:
                    try:
                        notify({
                            "jsonrpc": "2.0", "method": "notifications/progress",
                            "params": {"progressToken": progress_token,
                                       "progress": 1 if status in TERMINAL else (0.5 if status == "running" else 0),
                                       "total": 1, "message": status},
                        })
                    except (OSError, ValueError):
                        return  # Client disconnected.
                    previous = status
                if status in TERMINAL:
                    return
                time.sleep(0.1)

        threading.Thread(target=watch, daemon=True).start()


def register(registry) -> Jobs:
    jobs = Jobs(registry)
    for name, handler, properties, required, description in (
        ("start", jobs.start,
         {"tool_name": {"type": "string", "enum": list(ALLOWED_TOOLS)},
          "arguments": {"type": "object"}}, ["tool_name", "arguments"],
         "Start an allowlisted long-running tool in a child process; return a job_id immediately."),
        ("status", jobs.status, {"job_id": {"type": "string"}}, ["job_id"],
         "Read persisted job status and timestamps, including after a server restart."),
        ("result", jobs.result, {"job_id": {"type": "string"}}, ["job_id"],
         "Read a job's status, result or error without waiting for completion."),
        ("cancel", jobs.cancel, {"job_id": {"type": "string"}}, ["job_id"],
         "Request process-tree cancellation. Status becomes cancelled after the worker is killed."),
    ):
        registry.register(f"consensus.job_{name}", {
            "description": description,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required, "additionalProperties": False},
        }, handler)
    return jobs


def _worker(path: Path) -> int:
    record = _read(path)
    output = path.with_suffix(".output")
    try:
        module, name = record["handler"]
        result = getattr(importlib.import_module(module), name)(**record["arguments"])
        failed = isinstance(result, dict) and result.get("ok") is False
        _write(output, {"result": result, "error": result.get("error") if failed else None})
        return 1 if failed else 0
    except BaseException as exc:
        _write(output, {"result": None, "error": f"{type(exc).__name__}: {exc}"})
        return 1


def _supervise(path: Path) -> int:
    record = _read(path)
    proc = None
    cancel = path.with_suffix(".cancel")
    output = path.with_suffix(".output")
    try:
        if cancel.exists():
            record.update(status="cancelled")
        else:
            proc = _popen(path, worker=True)
            record.update(status="running", started_at=_now(), pid=proc.pid)
            _write(path, record)
            while proc.poll() is None:
                if cancel.exists():
                    _kill_tree(proc)
                    record.update(status="cancelled")
                    break
                time.sleep(0.1)
            record["exit_code"] = proc.wait()
            if record["status"] != "cancelled":
                payload = _read(output) if output.exists() else {
                    "result": None, "error": f"worker exited without a result (exit {proc.returncode})",
                }
                record.update(payload)
                record["status"] = "succeeded" if proc.returncode == 0 else "failed"
    except BaseException as exc:
        if proc is not None and proc.poll() is None:
            try:
                _kill_tree(proc)
            except Exception as cleanup_error:
                exc = RuntimeError(f"{exc}; cleanup failed: {cleanup_error}")
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    record["finished_at"] = _now()
    _write(path, record)
    output.unlink(missing_ok=True)
    cancel.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    path = Path(sys.argv[1])
    sys.exit(_worker(path) if "--worker" in sys.argv[2:] else _supervise(path))
