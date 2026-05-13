# iter-0039 proposed patches — _dispatch_codex.py cross-plat fixes

Workflow #4 pre-review of 3 deferred cross-platform findings from the
2026-05-11 audit sweep (codex-audit-crossplat-1). Deferred because they
overlapped with iter-0037's bidirectional-dispatch rewrite; now that
iter-0037 has landed, the file is stable and these fixes apply cleanly.

Prose-first per iter-0036 stall lesson. Diffs reference current
post-iter-0037 code (commit `2faa5fb7`).

---

## Patch 1 — xplat-rev-001 HIGH (Windows containment canonical paths)

**Defect**: `_normalize_relative_to_repo` at `_dispatch_codex.py:190-210`
uses `str(resolved).lower().replace("\\", "/")` as a Windows-case
fallback. This is lossy:
- Doesn't strip Windows long-path prefix `\\?\` (extended-length paths
  on Windows can start with this; lowercase-compare diverges).
- Doesn't handle 8.3 short names that `resolve()` doesn't expand
  consistently.
- Doesn't normalize separator slashes vs backslashes at the OS level
  (`os.path.normcase` is the canonical form).

**Approach**: Replace the lower+replace fallback with a helper
`_normalize_for_compare(p)` that:
1. Strips `\\\\?\\` long-path prefix if present (Windows extended path)
2. Calls `os.path.normcase(os.path.normpath(str(p)))` — normcase
   lowercases + normalizes separators on Windows, no-op on POSIX
3. Returns the canonical string

Then `_normalize_relative_to_repo` containment check becomes:
```python
contained = False
try:
    resolved.relative_to(repo_root_resolved)
    contained = True
except ValueError:
    if sys.platform == "win32":
        ncs_resolved = _normalize_for_compare(resolved)
        ncs_root = _normalize_for_compare(repo_root_resolved)
        # exact match OR resolved is under root + separator
        if ncs_resolved == ncs_root or ncs_resolved.startswith(ncs_root + os.sep):
            contained = True
```

**Regression tests**:
- `test_containment_strips_long_path_prefix_on_windows` (skip on non-win32)
- `test_containment_handles_mixed_case_drive_on_windows` (skip on non-win32)
- `test_containment_handles_normalized_separators_on_windows` (skip on non-win32)
- `test_containment_unaffected_on_posix` (sanity check)

---

## Patch 2 — xplat-rev-002 HIGH (process tree termination + temp file leak)

**Defect**: The new Popen call (iter-0037, line ~480) doesn't set
process-group creation flags. `proc.terminate()` on Windows calls
TerminateProcess which only kills the immediate process. codex-cli
spawns Node descendants that orphan after abort. On POSIX, lack of
`start_new_session=True` means signals don't propagate to children
either.

Additionally, `proc.wait(timeout=10)` is called after terminate, but
on Windows if children hold the `out_file` handle, `Path(out_file).unlink()`
in the finally block silently fails (`except OSError: pass`).

**Approach**:

1. Add process-group creation:
```python
if sys.platform == "win32":
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    popen_kwargs = {"creationflags": creationflags}
else:
    popen_kwargs = {"start_new_session": True}

proc = popen_factory(
    cmd,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    bufsize=0,
    **popen_kwargs,
)
```

2. Add a helper `_terminate_process_tree(proc, grace_seconds=10)`:
```python
def _terminate_process_tree(proc, grace_seconds: float = 10.0):
    """Cross-platform: send SIGTERM-equivalent to the codex process group,
    wait for grace, then SIGKILL-equivalent if still alive."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # CTRL_BREAK_EVENT goes to the process group created with
            # CREATE_NEW_PROCESS_GROUP. Children receive it too.
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError, ValueError):
        # Fallback to single-process terminate.
        try:
            proc.terminate()
        except OSError:
            pass

    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass

    # Force-kill the group.
    try:
        if sys.platform == "win32":
            # On Windows, terminate() is the closest we have to SIGKILL;
            # CREATE_NEW_PROCESS_GROUP children may need taskkill /T /F.
            import subprocess as _sp
            _sp.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=5, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError, ValueError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
```

3. Replace every `proc.terminate(); proc.wait(...); proc.kill()` block
   with `_terminate_process_tree(proc)`.

4. Temp file cleanup: change the swallowed except to a logged warning
   so the operator sees the leak:
```python
finally:
    try:
        Path(out_file).unlink()
    except OSError as exc:
        # Log to stderr but don't raise (we're in finally).
        print(f"WARN: codex output temp file unlink failed: {exc}",
              file=sys.stderr)
```

**Regression tests**:
- `test_terminate_process_tree_sends_appropriate_signal_per_platform`
  (use mock signal/sp.run)
- `test_terminate_process_tree_force_kills_after_grace`
- `test_temp_file_cleanup_logs_warning_on_failure` (capsys)

---

## Patch 3 — xplat-rev-008 LOW (binary resolution edge cases)

**Defect**: Current `_resolve_codex_bin` at line ~395 only special-cases
.ps1 -> .cmd on Windows. Missing:
- Bare-name resolution (no extension): try .exe, .cmd, .bat in PATH
- App Execution Alias stub detection (0-byte files in WindowsApps)
- MSYS-style path conversion (`/c/foo/bar` -> `C:\foo\bar`)

**Approach**: Extend `_resolve_codex_bin` to handle these cases:
```python
def _resolve_codex_bin(codex_bin: str) -> str:
    """Resolve codex binary path with Windows-specific quirk handling."""
    if sys.platform != "win32":
        return codex_bin

    # MSYS-style path conversion: /c/foo/bar -> C:\foo\bar
    if codex_bin.startswith("/") and len(codex_bin) >= 3 and codex_bin[2] == "/":
        drive = codex_bin[1].upper()
        rest = codex_bin[3:].replace("/", "\\")
        codex_bin = f"{drive}:\\{rest}"

    # If bare name, try common Windows extensions in PATH.
    if not Path(codex_bin).is_absolute() and "." not in Path(codex_bin).name:
        for ext in (".exe", ".cmd", ".bat", ".ps1"):
            candidate = shutil.which(codex_bin + ext)
            if candidate:
                codex_bin = candidate
                break

    # Existing .ps1 -> .cmd preference logic
    resolved = shutil.which(codex_bin) or codex_bin
    if resolved.lower().endswith(".ps1"):
        # Look for sibling .cmd
        cmd_variant = Path(resolved).with_suffix(".cmd")
        if cmd_variant.exists():
            resolved = str(cmd_variant)

    # App Execution Alias detection: 0-byte file in WindowsApps is a stub.
    p = Path(resolved)
    if p.exists() and p.stat().st_size == 0:
        windows_apps = (os.environ.get("LOCALAPPDATA", "")
                        + r"\Microsoft\WindowsApps")
        if str(p).lower().startswith(windows_apps.lower()):
            raise CodexInvocationError(
                f"codex binary {resolved!r} appears to be a Windows App "
                f"Execution Alias stub (0-byte file in WindowsApps). "
                f"subprocess cannot execute App Aliases — install the "
                f"real codex CLI binary or adjust PATH."
            )

    return resolved
```

**Regression tests**:
- `test_resolve_codex_bin_msys_path_conversion_on_windows` (skip non-win32)
- `test_resolve_codex_bin_bare_name_finds_cmd_on_windows` (skip non-win32; mock shutil.which)
- `test_resolve_codex_bin_app_alias_stub_rejected` (skip non-win32; mock Path.stat)
- `test_resolve_codex_bin_unchanged_on_posix` (sanity)

---

## Cross-cutting verification

After all 3 patches:
- pytest: +10 new regression tests (4 + 3 + 4 minus overlapping platform skips)
- smoke: 60/60
- gates: 11/11 after commit
- `G_pytest_dispatch_codex` baseline: bump from 90 -> ~100 depending on
  win32 skip behavior

## Reviewer questions for codex pre-review

1. **`os.path.normcase` semantics**: on Windows it lowercases + converts
   forward slashes to backslashes. Is that the right canonical form for
   `startswith` comparison, or should we ALSO call `os.path.normpath`
   to collapse `..` / `.` segments first?
2. **`CREATE_NEW_PROCESS_GROUP` impact on signal delivery**: with this
   flag, Ctrl-C in the operator's terminal does NOT propagate to the
   codex subprocess (because it's now in a separate group). Is this
   acceptable? (codex is read-only and short-lived; operator abort via
   abort-signal-file is the canonical mechanism).
3. **`taskkill /F /T` reliability**: shells out to an external binary
   for force-kill. Acceptable, or should we use Win32 API via
   ctypes/`subprocess.CREATE_NO_WINDOW`?
4. **MSYS path detection**: `/c/foo` is the most common form but
   Cygwin uses `/cygdrive/c/foo`. Should we handle both, or scope
   to MSYS only (Git Bash on Windows)?
5. **App Alias 0-byte detection**: relies on file size. Some real
   binaries could be 0 bytes (unlikely but possible). Should we also
   check for the specific path prefix as the primary signal and only
   use the 0-byte check as a confirmation?
