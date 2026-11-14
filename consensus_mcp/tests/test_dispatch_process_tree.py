"""Cancellation must reap descendants after their immediate parent exits."""
import signal
import subprocess
import sys
import time

import pytest

from consensus_mcp import _dispatch_base as base


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Toolhelp process-tree regression")
@pytest.mark.parametrize("parent_exited", [False, True])
def test_kills_grandchild_when_parent_and_child_exit_on_soft_signal(tmp_path, parent_exited):
    import ctypes
    from ctypes import wintypes
    from consensus_mcp._jobs import _windows_kill_tree

    script = tmp_path / "tree.py"
    script.write_text(
        "import os,signal,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "role, directory = sys.argv[1:]\n"
        "signal.signal(signal.SIGBREAK, signal.SIG_IGN if role == 'grandchild' else "
        "lambda *_: sys.exit(0))\n"
        "Path(directory, role + '.pid').write_text(str(os.getpid()))\n"
        "if role != 'grandchild':\n"
        " subprocess.Popen([sys.executable, '-I', __file__, "
        "'child' if role == 'parent' else 'grandchild', directory])\n"
        "if role == 'parent' and Path(directory, 'exit-parent').exists():\n"
        " while not Path(directory, 'grandchild.pid').exists(): time.sleep(.01)\n"
        " sys.exit(0)\n"
        "while True: time.sleep(.02)\n", encoding="utf-8")
    if parent_exited:
        (tmp_path / "exit-parent").touch()
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    def alive(pid):
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            status = wintypes.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(status))) and status.value == 259
        finally:
            api.CloseHandle(handle)
    # Avoid the Windows venv launcher adding an unrelated, exited intermediary.
    executable = getattr(sys, "_base_executable", sys.executable)
    proc = subprocess.Popen([executable, "-I", str(script), "parent", str(tmp_path)],
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = []
    try:
        deadline = time.monotonic() + 5
        while not all((tmp_path / f"{role}.pid").exists() for role in ("parent", "child", "grandchild")):
            assert time.monotonic() < deadline, "fake process tree did not become ready"
            time.sleep(.02)
        pids = [int((tmp_path / f"{role}.pid").read_text()) for role in ("parent", "child", "grandchild")]
        if parent_exited:
            proc.wait(timeout=5)
        base._terminate_process_tree(proc, grace_seconds=.1)
        deadline = time.monotonic() + 3
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(.02)
        assert proc.poll() is not None
        assert not any(alive(pid) for pid in pids), "grandchild survived cancellation"
    finally:
        for path in tmp_path.glob("*.pid"):
            pid = int(path.read_text())
            if alive(pid):
                _windows_kill_tree(pid)
        proc.wait(timeout=5)


def test_posix_force_kills_original_group_after_parent_exits(monkeypatch):
    calls = []
    class Parent:
        pid = 12345
        def poll(self):
            return None
        def wait(self, timeout=None):
            return 0
        def kill(self):
            calls.append("direct-kill")
    monkeypatch.setattr(base.sys, "platform", "linux")
    monkeypatch.setattr(base.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(base.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(base.os, "killpg", lambda group, sig: calls.append((group, sig)), raising=False)
    base._terminate_process_tree(Parent(), grace_seconds=.1)
    assert calls == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Toolhelp termination path")
def test_windows_kills_tree_even_after_parent_exited(monkeypatch):
    from consensus_mcp import _jobs
    calls = []
    class Parent:
        pid = 12345
        def poll(self):
            return 0
        def kill(self):
            calls.append("kill")
        def wait(self, timeout=None):
            calls.append("wait")
            return 0
    monkeypatch.setattr(_jobs, "_windows_kill_tree", lambda pid: calls.append(("tree", pid)))
    base._terminate_process_tree(Parent())
    assert calls == [("tree", 12345), "kill", "wait"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Toolhelp termination fallback")
def test_windows_tree_failure_is_bounded_and_never_raises(monkeypatch):
    from consensus_mcp import _jobs
    calls = []
    class Parent:
        pid = 12345
        def kill(self):
            calls.append("kill")
            raise OSError("already exited")
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("fake", timeout)
    def fail(pid):
        raise OSError("snapshot unavailable")
    def taskkill(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(_jobs, "_windows_kill_tree", fail)
    monkeypatch.setattr(base.subprocess, "run", taskkill)
    base._terminate_process_tree(Parent())
    assert calls == [(["taskkill", "/F", "/T", "/PID", "12345"], 5), "kill"]
