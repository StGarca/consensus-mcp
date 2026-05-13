# Cross-platform audit — agent-loop path / subprocess / encoding handling

Audit dimension: **cross-platform** (Windows-specific path quirks, subprocess signal differences, CRLF/LF, encoding, symlink/locked-file behavior).

## Audit question for codex

Review the following code excerpts for cross-platform defects. Specific concerns (non-exclusive):

1. **Windows path handling**: backslashes vs forward slashes, drive-letter quirks, case insensitivity, 8.3 short-name expansion, UNC paths (`\\server\share`), MAX_PATH-260 limit. `_normalize_relative_to_repo` claims to handle case-insensitive containment via a manual lower+replace; is it correct under all the edge cases (long-path prefix `\\?\`, mixed-case drive `c:\...` vs `C:\...`, junctions, mapped network drives)?
2. **subprocess signal/termination on Windows**: there is no SIGTERM on Windows; only `terminate()` (calls `TerminateProcess`, which is graceless) and `CTRL_BREAK_EVENT` (needs `CREATE_NEW_PROCESS_GROUP`). codex CLI is a long-running node.js process — does the dispatch helper correctly clean up when `timeout_seconds` fires? Does `subprocess.TimeoutExpired` actually kill child grandchildren on Windows?
3. **CRLF vs LF in file reads**: Python text-mode read on Windows defaults to universal newlines (`\r\n` → `\n` translation). `read_text(encoding="utf-8")` does this transparently, but `read_bytes()` does NOT. Mixed usage of `read_text` vs `read_bytes` for the same yaml file (e.g., `_self_drive.py:395` uses `read_text`, line 403 uses `read_bytes` on the same audit_path) — does this change the canonical-sha across platforms?
4. **Encoding defaults**: PEP 686 set utf-8 as default in 3.15 but most production deployments are <= 3.12. On Windows, `open(...)` without `encoding=` defaults to `locale.getpreferredencoding()` (often `cp1252`). Any read/write without explicit `encoding="utf-8"` is a Windows-only bug surface.
5. **Locked-file behavior on Windows**: Windows can't unlink a file with an open handle. `_invoke_codex` creates a `NamedTemporaryFile` with `delete=False`, then `Path(out_file).unlink()` in a `finally`. If codex still holds the handle (e.g., during a SIGKILL-equivalent), the unlink fails silently. Other helpers append to JSONL files — does any caller hold a handle while another process tries to read?
6. **Symlink support**: Windows requires Developer Mode or admin to create symlinks; `Path.is_symlink()` works, but `Path.resolve()` on a symlink with permissions issues returns inconsistent results. The path-containment check in `_normalize_relative_to_repo` calls `.resolve()` on user-supplied paths — what happens if `repo_root` itself is reached via a junction or mapped drive?
7. **shutil.which / PATHEXT**: `_resolve_codex_bin` has Windows-specific logic for `.cmd` vs `.ps1`, but does NOT consider `.bat` ordering, the `App Execution Aliases` (zero-byte `.exe` shims in `%LOCALAPPDATA%\Microsoft\WindowsApps\`), or wsl-wrapped binaries.

## Files in scope (excerpts below)

- `scripts/agent_loop_mcp/_dispatch_codex.py` (path normalization, subprocess invocation, temp file handling)
- `scripts/agent_loop_mcp/_self_drive.py` (subprocess calls, path comparisons, mixed read_text/read_bytes)
- `scripts/agent_loop_mcp/_visibility_watchdog.py` (Path normalization, repo-root resolution)

---

## Excerpt 1: `_dispatch_codex.py:171-210` — case-insensitive containment check

```python
def _normalize_relative_to_repo(path_str: str | None, repo_root: Path) -> Path | None:
    """Normalize an operator-supplied path against repo_root.
    ...
    None passes through as None.
    """
    if path_str is None:
        return None
    p = Path(path_str)
    resolved = p.resolve() if p.is_absolute() else (repo_root / p).resolve()
    repo_root_resolved = repo_root.resolve()
    contained = False
    try:
        resolved.relative_to(repo_root_resolved)
        contained = True
    except ValueError:
        # iter-0033 claude-rev-003 fix: Path.relative_to is case-sensitive
        # in string compare. Windows filesystem is case-insensitive; mixed-
        # case repo_root vs path can trigger a false-positive containment
        # rejection. On Windows, retry with case-folded string compare.
        if sys.platform == "win32":
            resolved_lc = str(resolved).lower().replace("\\", "/")
            root_lc = str(repo_root_resolved).lower().replace("\\", "/")
            if resolved_lc == root_lc or resolved_lc.startswith(root_lc.rstrip("/") + "/"):
                contained = True
    if not contained:
        raise OutsideRepoPathError(
            f"path {path_str!r} resolves to {resolved} which is outside repo_root "
            f"{repo_root_resolved}. agent-loop-mcp dispatch only reads files inside "
            f"the repo. Move the file into the repo or pass a path relative to it."
        )
    return resolved
```

Concerns:
- `str.lower()` is not the same as `casefold()`. For Turkish-locale ı/I edge cases or other Unicode normalization, `lower()` can mis-compare. The Windows filesystem uses a specific case mapping table; neither lower() nor casefold() exactly matches it.
- `Path.resolve()` on Windows handles 8.3 short names (`PROGRA~1` → `Program Files`) only when the path exists. For paths that don't exist yet (codex output file), resolution may differ.
- `\\?\` long-path prefix: `resolve()` MAY strip or add this prefix non-deterministically; equality compare on the string form then fails.
- The replace `\\` → `/` happens after `lower()`. Forward-slashes inside the original path are preserved either way, but what about UNC paths (`\\server\share\foo` → `//server/share/foo`)? Two leading slashes is intentional but `rstrip("/").startswith(root + "/")` won't match if root is also UNC.

## Excerpt 2: `_dispatch_codex.py:373-406` — `.cmd` vs `.ps1` preference

```python
def _resolve_codex_bin(codex_bin: str) -> str:
    """Resolve a codex binary spec to an actual executable file path.

    Per v1.10.3 Windows hardening (real-codex smoke 2026-05-09):
      - Python's subprocess on Windows does NOT apply PATHEXT lookup to bare
        names, so `codex` (which is on PATH only as `codex.cmd`/`.ps1`/etc.)
        fails with "binary not found". `shutil.which("codex")` DOES apply
        PATHEXT and returns the resolved path.
      - On Windows, `shutil.which` returns the FIRST PATHEXT match by default;
        order is `.COM; .EXE; .BAT; .CMD; .VBS; .JS; .WS; .PS1`. npm-installed
        CLIs ship `<name>.cmd` (directly executable by CreateProcess) AND
        `<name>.ps1` (needs powershell.exe wrapper). Without intervention,
        `shutil.which("codex")` could return `.ps1` (in some PATH orders) and
        Python's subprocess would fail with WinError 193 ("not a valid Win32
        application"). Prefer `.cmd` explicitly when the bare-name resolution
        lands on a script Python can't exec.
    """
    # If caller already gave a full path that exists, use it as-is.
    if os.path.sep in codex_bin or (len(codex_bin) > 1 and codex_bin[1] == ":"):
        return codex_bin
    resolved = shutil.which(codex_bin)
    if resolved is None:
        return codex_bin
    # On Windows, prefer .cmd over .ps1 (Python's subprocess can directly exec
    # .cmd via CreateProcess but not .ps1 which needs powershell.exe).
    if sys.platform == "win32" and resolved.lower().endswith(".ps1"):
        cmd_alt = shutil.which(codex_bin + ".cmd")
        if cmd_alt:
            return cmd_alt
    return resolved
```

Concerns:
- `os.path.sep` is `\` on Windows. A user-supplied POSIX-style absolute path `/c/Users/foo/codex` on Windows would NOT contain `\` and the check falls through to `shutil.which`. The `(len(codex_bin) > 1 and codex_bin[1] == ":")` drive-letter test catches `C:\...` but not `c:` lower-case explicit drive nor MSYS-style `/c/`.
- App Execution Aliases (Windows Store apps): `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` is a zero-byte AppX reparse point. `shutil.which` returns it; `subprocess.run` then fails with cryptic errors. Not specifically codex-relevant but the helper resolution pattern is reused.
- `.bat` files share the `.cmd` exec model but aren't covered by the preference rule.
- `resolved.lower().endswith(".ps1")` would also match a file named `foo.PS1` — fine on Windows (case-insensitive), but the logic implicitly assumes lower-cased extension matters.

## Excerpt 3: `_dispatch_codex.py:431-517` — temp file + subprocess kill semantics

```python
def _invoke_codex(prompt, codex_bin, timeout_seconds, repo_root, schema_path) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        out_file = tmp.name
    try:
        cmd = [
            _resolve_codex_bin(codex_bin),
            "exec",
            "--skip-git-repo-check",
            "--cd", str(repo_root),
            "--sandbox", "read-only",
            "--output-schema", str(schema_path),
            "-o", out_file,
            "-",
        ]
        try:
            # ... Windows binary-stdin fix to avoid \n -> \r\n translation
            result = subprocess.run(
                cmd,
                input=prompt.encode("utf-8"),
                capture_output=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise CodexInvocationError(f"codex timeout after {timeout_seconds}s") from None
        # ...
    finally:
        try:
            Path(out_file).unlink()
        except OSError:
            pass
```

Concerns:
- `subprocess.run(timeout=...)` raises `TimeoutExpired` AFTER `proc.kill()` is called. On Windows, `proc.kill()` calls `TerminateProcess`, which does NOT propagate to child processes. If codex (node.js) spawned helper processes, they orphan. `subprocess.run` also doesn't wait for grandchildren.
- `Path(out_file).unlink()` in the `finally`. If `TerminateProcess` is in-flight, the codex child may still hold an OS-level write handle to `out_file` for a fraction of a second. On Windows, `unlink` will fail with `WinError 32` (file in use). The `except OSError: pass` swallows it — leaks the temp file forever.
- `tempfile.NamedTemporaryFile(delete=False)` on Windows opens with `O_TEMPORARY` flag absence — but the file is created in the user's temp dir, which may have antivirus scanning. AV holding the file open at the moment codex tries to write the output JSON is a real Windows-only flake source.
- `repo_root` is passed to codex via `--cd`. If repo_root contains spaces (e.g., `C:\Users\<you>\My Documents\...`), the codex CLI's own path parsing must handle it correctly. `str(repo_root)` returns the native form with backslashes; codex (node.js) usually handles either, but worth verifying.

## Excerpt 4: `_dispatch_codex.py:417-428` — `_get_codex_version` uses different encoding strategy

```python
def _get_codex_version(codex_bin: str) -> str:
    try:
        result = subprocess.run(
            [_resolve_codex_bin(codex_bin), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"
```

Concerns:
- Uses `text=True, encoding="utf-8"` here, but `_invoke_codex` uses binary mode with explicit encode/decode. The two patterns are not interchangeable on Windows — text mode does CRLF translation; binary mode does not. Inconsistency between the two helpers — not necessarily wrong, but worth checking that the `text=True` mode doesn't mangle codex `--version` output if it contains non-ASCII (unlikely but possible for build-metadata).

## Excerpt 5: `_self_drive.py:395-405` — mixed `read_text` vs `read_bytes` on the same audit yaml

```python
packet = yaml.safe_load(Path(args.goal_packet).read_text(encoding="utf-8"))
iter_dir = Path(args.iteration_dir)

stop_rules_fired = []

audit_path = iter_dir / "independence-audit.yaml"
iter_count = 0
if audit_path.exists():
    audit = yaml.safe_load(audit_path.read_bytes()) or {}
    log = audit.get("audit_log", []) or []
    iter_count = sum(1 for e in log if e.get("event") == "patch_applied")
```

Concerns:
- `args.goal_packet` is loaded via `read_text(encoding="utf-8")` — universal newlines applies (\r\n → \n).
- `audit_path` two lines later is loaded via `read_bytes()` — no newline translation. yaml.safe_load on bytes vs str works in both cases, but if the audit yaml has any line that includes a literal `\r\n` in a quoted string, the two modes produce different in-memory dicts.
- More importantly: `_canonical_sha256_of_yaml_file` at `_self_drive.py:339` uses `read_text(encoding="utf-8")`. The drift check at line 632 calls `_canonical_sha256_of_yaml_file(target)` to compare against `claimed_sha`. If `claimed_sha` was originally computed elsewhere using `read_bytes()`, the canonical-sha values disagree on Windows due to CRLF translation in `read_text`. Cross-document drift may falsely fire (or false-pass).

## Excerpt 6: `_self_drive.py:200-218` — git subprocess uses `text=True` (default encoding on Windows = cp1252)

```python
for cmd in invocations:
    try:
        r = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        for ln in r.stdout.splitlines():
            p = ln.strip()
            if p:
                seen.add(p)
    except Exception:
        continue
```

Concerns:
- `text=True` with no `encoding=` argument uses `locale.getpreferredencoding(False)`, which is `cp1252` on most Windows systems. git outputs paths with `core.quotePath` defaulting to true, which escapes non-ASCII names as `\xxx`. Files with non-ASCII names (Unicode book titles in `ebooks/`, for instance) would round-trip through cp1252 → str, then be compared against `forbidden_files` and `allowed_files` patterns from the goal_packet (which IS utf-8). String compare disagrees.
- Same pattern appears at `_self_drive.py:472-501` for `patch_size_exceeds_max` git calls.

## Excerpt 7: `_self_drive.py:412-419` — _path_matches_pattern uses fnmatch (case-sensitive on Linux, case-insensitive on Windows)

```python
forbidden = packet.get("forbidden_files", [])
repo_root = _resolve_repo_root()
try:
    changed = _collect_changed_files(repo_root)
    for forbidden_path in forbidden:
        for c in changed:
            if _path_matches_pattern(c, forbidden_path):
                stop_rules_fired.append({"rule": "patch_would_touch_forbidden_files", "forbidden": forbidden_path, "changed": c})
```

(`_path_matches_pattern` at `_self_drive.py:155-168` uses `fnmatch.fnmatch`.)

Concerns:
- `fnmatch.fnmatch` is documented as case-insensitive on case-insensitive filesystems (calls `fnmatchcase` after `os.path.normcase`). On Windows, `forbidden: SCRIPTS/agent_loop_mcp/*.py` would match changed file `scripts/agent_loop_mcp/foo.py`. On Linux, it would not. Behavior diverges between dev (Windows) and CI (Linux). This may or may not be desired but should be intentional.
- Git always reports paths with `/` separators regardless of OS. forbidden_files patterns might use `\` (if hand-edited on Windows in some editor). `fnmatch` doesn't normalize separators.

## Excerpt 8: `_visibility_watchdog.py:66-71` — repo_root resolution via `__file__` walk

```python
def _default_repo_root() -> Path:
    """Same resolution heuristic as _visibility_tui._default_repo_root."""
    override = os.environ.get("AGENT_LOOP_MCP_REPO_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent.parent
```

Concerns:
- `Path(__file__).resolve()` on Windows: if the package was installed via `pip install -e .` into a venv, `__file__` points to the source tree (good). If installed normally, it points into `site-packages/agent_loop_mcp/_visibility_watchdog.py`, and `.parent.parent.parent` is `site-packages/`, NOT the repo. Cross-platform issue isn't really windows-specific but the heuristic relies on the layout being source-tree.
- `AGENT_LOOP_MCP_REPO_ROOT` env override — on Windows, env var values can contain `;` (path separator confusion if any caller splits on `;`). Path resolution itself is fine.

## Excerpt 9: `_visibility_watchdog.py:189-192` — append-to-jsonl without lock

```python
log_path.parent.mkdir(parents=True, exist_ok=True)
with log_path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(event) + "\n")
```

Concerns:
- POSIX guarantees atomic appends for write sizes < PIPE_BUF (typically 4096 bytes). Windows has no such guarantee — concurrent appenders can interleave bytes within one line. Watchdog runs in `--action=mark` mode are infrequent but `_dispatch_codex` also appends to the same log path simultaneously. With two writers a torn JSON line is possible on Windows.

## Excerpt 10: `_dispatch_codex.py` text-mode stdin pitfall (already mitigated, in-band documentation)

```python
# Per v1.10.3 Windows hardening (real-codex smoke 2026-05-09): even with
# text=True + encoding="utf-8", Python's subprocess on Windows opens stdin
# in TEXT MODE which performs newline translation (\n -> \r\n) on the byte
# stream AFTER UTF-8 encoding. This corrupts multibyte UTF-8 sequences if
# a \n falls mid-codepoint OR if the receiver counts bytes assuming no
# translation. Codex (which expects exact UTF-8 bytes) reports "invalid
# byte at offset N" when any non-ASCII codepoint is present.
#
# Fix: encode prompt to UTF-8 bytes ourselves and pass binary input
# (text=False, no encoding= kwarg). subprocess writes bytes verbatim to
# the stdin pipe, no translation. stdout/stderr are also bytes; we decode
# stderr for human-readable error messages.
```

Note: included for context. The mitigation is correct. The question is whether OTHER subprocess invocations in this codebase that pass `input=...` as a `str` have the same Windows-stdin-CRLF bug latent (`_self_drive.py` git calls don't pass `input=` so they're fine; check helper-files outside the package).

## What I expect codex to flag

Findings I'd consider valid:
- The `lower()` containment check is wrong for Unicode edge cases (Turkish ı, German ß) — use `casefold()` AND OS-level case-insensitive compare via `os.path.normcase`
- Long-path prefix `\\?\` not stripped before string compare, breaking `relative_to.startswith` test
- `text=True` git subprocess call lacks `encoding="utf-8"` — falls back to cp1252 on Windows, mangles non-ASCII paths
- Temp file unlink race after `TerminateProcess` on Windows timeout path — likely silent temp-file leak
- CRLF translation inconsistency: `read_text` vs `read_bytes` on the same yaml file produces different canonical-sha values on Windows
- `_default_repo_root` heuristic via `__file__.parent.parent.parent` breaks under `pip install` (non-editable) on any platform
- Concurrent jsonl append race on Windows (no atomic-append guarantee)

## Out of scope for this audit

- **Security** (separate audit running in parallel)
- **Performance** (separate audit running in parallel)
- **Bare-except patterns** (separate audit running in parallel)
- Style / naming / docstrings
- Test coverage
