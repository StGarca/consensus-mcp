# Bare-except / broad-except audit — agent-loop policy sweep

Audit dimension: **bare-except** (overly-broad `except Exception:` or `except:` that silences errors and hides defects).

## Audit question for codex

Sweep the agent_loop_mcp package for `except` clauses that catch too widely. Specific concerns (non-exclusive):

1. **`except Exception:` with `pass`** — silently absorbs all errors including programming bugs (TypeError, AttributeError, KeyError) that should surface during dev.
2. **`except Exception:` with `return None` / `return {}` / `return False`** — converts ANY exception (including KeyboardInterrupt-adjacent or assertion failures) into a successful-looking sentinel. Caller can't tell "no data" from "code is broken".
3. **`except Exception as exc:` that logs `str(exc)` only** — drops the traceback. Caller sees a one-line error and never finds out where the bug actually lives.
4. **Catching `Exception` when only `OSError`/`yaml.YAMLError`/`subprocess.CalledProcessError` should be expected** — pulls in unintended sibling classes (e.g., RecursionError, MemoryError, TypeError-from-mismatched-API).
5. **Catching `Exception` to gate a fallback when a narrower exception would suffice** — e.g., a `_read_yaml_or_empty` that catches `Exception` instead of `(OSError, yaml.YAMLError, UnicodeDecodeError)`. A bug in yaml parsing logic itself becomes "file unreadable" and the caller silently uses an empty dict.

claude-rev-005 (iter-0034) flagged this as a policy-inconsistency theme. This audit is the comprehensive sweep across all `agent_loop_mcp/*.py` files (excluding tests and top-level CLI exception handlers — see Out-of-scope).

## Files in scope (excerpts below)

- `scripts/agent_loop_mcp/_self_drive.py`
- `scripts/agent_loop_mcp/_dispatch_codex.py`
- `scripts/agent_loop_mcp/_visibility_watchdog.py` — note: bare-except already narrow throughout (OSError / JSONDecodeError / ValueError / TypeError); no findings expected
- `scripts/agent_loop_mcp/_visibility_tui.py` — note: already narrow (OSError / json.JSONDecodeError / (ValueError, TypeError) / KeyboardInterrupt); no findings expected
- `scripts/agent_loop_mcp/_author_review_packet.py`
- `scripts/agent_loop_mcp/_closure_invariant.py` — note: already narrow (OSError / ValueError); no findings expected
- `scripts/agent_loop_mcp/_validate_closure_invariant.py`
- `scripts/agent_loop_mcp/_release_gate_check.py`
- `scripts/agent_loop_mcp/_sync_section_24.py` — note: no try/except blocks at all; no findings expected

---

## Excerpt 1: `_self_drive.py:213` — silently skip git subprocess failure

```python
def _collect_changed_files(repo_root: Path) -> list[str]:
    """Return the de-duplicated union of git's three change-views."""
    seen: set[str] = set()
    invocations = [
        ["git", "diff", "--cached", "--name-only"],
        ["git", "diff", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]
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
            # Best-effort: silently skip this view, like the prior cached-only
            # path did via the outer try/except in callers. Callers may surface
            # the failure as a 'git_check_failed' breadcrumb separately.
            continue
    return sorted(seen)
```

Comment claims "callers may surface" but the inner loop drops the exception entirely with no logging. Narrow would be `(subprocess.TimeoutExpired, OSError, FileNotFoundError)`.

## Excerpt 2: `_self_drive.py:285` — `_evaluate_one_gate` catches Exception

```python
def _evaluate_one_gate(gate: dict, repo_root: Path) -> dict:
    gate_id = gate.get("id", "?")
    check = gate.get("check", "")
    if not check:
        return {"id": gate_id, "passed": False, "error": "no_check_command"}
    try:
        r = subprocess.run(check, shell=True, cwd=str(repo_root), capture_output=True, text=True, timeout=120)
        return {
            "id": gate_id,
            "passed": r.returncode == 0,
            "exit_code": r.returncode,
            "stdout_tail": r.stdout.splitlines()[-1] if r.stdout else "",
        }
    except Exception as exc:
        return {"id": gate_id, "passed": False, "exception": str(exc)}
```

Catches Exception to capture `str(exc)` into the result dict. A TypeError from `gate.get(...)` returning a non-string would silently be reported as a "gate failed". Narrow to `(subprocess.TimeoutExpired, OSError, subprocess.SubprocessError)` to distinguish "gate timed out / process error" from "bug in this function".

## Excerpt 3: `_self_drive.py:289-297` — `_read_yaml_or_empty` swallows everything

```python
def _read_yaml_or_empty(path: Path) -> dict:
    """Best-effort yaml load; returns {} on missing/parse failure rather than raising."""
    try:
        if not path.exists():
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}
```

This is called from many stop-rule branches. If yaml.safe_load itself has a bug (or a memory-error from a giant input), the caller silently sees `{}` and proceeds as if the file were empty. Narrow to `(OSError, yaml.YAMLError, UnicodeDecodeError)`. Anything else (TypeError, AttributeError) indicates a programming bug that should propagate.

## Excerpt 4: `_self_drive.py:420` — git_check exception breadcrumb

```python
forbidden = packet.get("forbidden_files", [])
repo_root = _resolve_repo_root()
try:
    changed = _collect_changed_files(repo_root)
    for forbidden_path in forbidden:
        for c in changed:
            if _path_matches_pattern(c, forbidden_path):
                stop_rules_fired.append({"rule": "patch_would_touch_forbidden_files", "forbidden": forbidden_path, "changed": c})
except Exception as exc:
    stop_rules_fired.append({"rule": "git_check_failed", "exception": str(exc)})
```

This one is at least surfacing the exception in the output. But it wraps a call that internally already catches Exception (line 213). So this outer except only sees exceptions from `_path_matches_pattern` or the loop body itself — `fnmatch` shouldn't raise; this catch arguably won't fire for real reasons.

## Excerpt 5: `_self_drive.py:438` — `review_yaml_parse_failed` breadcrumb

```python
for review_path in (claude_path, codex_path):
    if review_path.exists():
        try:
            yaml.safe_load(review_path.read_text(encoding="utf-8"))
        except Exception as exc:
            stop_rules_fired.append({
                "rule": "review_yaml_parse_failed",
                "path": str(review_path),
                "exception": str(exc),
            })
```

Narrow to `yaml.YAMLError`. As written, a UnicodeDecodeError or OSError is also reported as "yaml parse failed", which mis-labels the actual problem.

## Excerpt 6: `_self_drive.py:539` — patch_size_exceeds_max swallows everything silently

```python
        if total > max_patch_size:
            stop_rules_fired.append({
                "rule": "patch_size_exceeds_max",
                "patch_size": total,
                "max": max_patch_size,
                "reason": f"patch_size={total}, max={max_patch_size}",
            })
    except Exception:
        # Treat git failure as missing-data; do not fire (matches existing
        # git_check_failed precedent for the forbidden-files branch).
        pass
```

`pass` with no breadcrumb. If git is broken, no stop-rule fires, and the operator never knows. At least a `stop_rules_fired.append({"rule": "patch_size_check_failed", "exception": ...})` would surface the failure.

## Excerpt 7: `_self_drive.py:633` — drift-finding canonical-sha compute failure

```python
def _check_drift(claim_label: str, ref_path: str, claimed_sha: str):
    """Append a drift finding if the claimed_sha doesn't match the actual file."""
    if not claimed_sha or not ref_path:
        return
    target = _resolve_referenced_path(ref_path, iter_dir)
    if not target.exists():
        drift_findings.append({...})
        return
    try:
        actual = _canonical_sha256_of_yaml_file(target)
    except Exception as exc:
        drift_findings.append({
            "claim": claim_label,
            "referenced_path": ref_path,
            "claimed_sha": claimed_sha,
            "reason": f"canonical_sha_compute_failed: {exc}",
        })
        return
```

Reports the exception so this is better. Still, narrow `(OSError, yaml.YAMLError)` would distinguish "file can't be read" from "yaml is malformed" from "we have a bug".

## Excerpt 8: `_self_drive.py:894` — `cmd_verify_scope`

```python
try:
    changed = _collect_changed_files(repo_root)
except Exception as exc:
    print(json.dumps({"in_scope": False, "error": str(exc)}))
    return 1
```

`_collect_changed_files` already absorbs its own exceptions (line 213). This outer except is unlikely to fire — but if it does (e.g., `_resolve_repo_root` raising), the catch is broad. Narrow OR demote to a guard against specific known failures.

## Excerpt 9: `_dispatch_codex.py:1468` — catch-all in main()

```python
except (CodexInvocationError, CodexOutputParseError) as exc:
    _log_dispatch(log_path, _failed_event(type(exc).__name__, str(exc)))
    print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
    return 1
except Exception as exc:
    _log_dispatch(log_path, _failed_event(type(exc).__name__, str(exc)))
    print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
    return 2
```

This is the top-level CLI catch-all. Different exit codes (1 for expected, 2 for unexpected) is the right pattern. **Likely intentional** — boundary catch-all so the helper always emits structured json instead of a Python traceback. The audit-log captures the exception type+message so debugging is still possible. Recommend: leave OR add traceback.format_exc() into the audit event (not the printed JSON) for unexpected branch only.

## Excerpt 10: `_author_review_packet.py:160` — top-level CLI catch-all

```python
try:
    path = author_review_packet(
        iteration_dir=Path(ns.iteration_dir),
        files=files,
        repo_root=repo_root,
    )
except FileNotFoundError as exc:
    print(f"error: {exc}", file=sys.stderr)
    return 1
except Exception as exc:  # pragma: no cover — defensive
    print(f"error: unexpected failure: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 2
```

`pragma: no cover` comment acknowledges it's a defensive top-level. Same structure as Excerpt 9. **Likely intentional CLI boundary**.

## Excerpt 11: `_validate_closure_invariant.py:77` — review-yaml read

```python
if not path.exists():
    return None
try:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
except Exception:
    return None
```

Should be `(OSError, yaml.YAMLError, UnicodeDecodeError)`. Returning `None` for any exception hides bugs in this function from callers.

## Excerpt 12: `_validate_closure_invariant.py:180` — audit-yaml read

```python
try:
    audit_data = yaml.safe_load(audit_path.read_text(encoding="utf-8")) or {}
except Exception:
    return {
        "iteration_id": iteration_id,
        "has_apply_step_landed": False,
        "last_mutation": None,
        "closing_verdict_present": False,
        "invariant_check": None,
        "verdict": "n/a (no audit)",
    }
```

Returns the same fallback dict regardless of whether the audit was missing, malformed yaml, an unrelated OSError, or a real bug. Narrow + log distinction would matter for diagnostics.

## Excerpt 13: `_release_gate_check.py:85, 103, 128, 141, 160, 300, 324, 357, 405, 435` — subprocess gates

Pattern (representative example from gate_smoke):

```python
def gate_smoke(repo_root: Path, python: str) -> tuple[bool, str]:
    """G_smoke: in-tree smoke 60/60."""
    try:
        result = subprocess.run(
            [python, str(repo_root / "scripts" / "agent_loop_mcp" / "_smoke_test.py")],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=120,
        )
    except Exception as exc:
        return False, f"exception: {exc}"
    tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
    last = tail[0]
    ok = result.returncode == 0 and "60/60 tests passed" in last
    return ok, f"exit={result.returncode} last={last!r}"
```

Repeats across ~10 gate functions. Should be `(subprocess.TimeoutExpired, OSError, subprocess.SubprocessError)`. A TypeError caused by a stale arg passed to subprocess.run would silently be reported as "gate failed: TypeError(...)" rather than crashing the gate harness loud-enough to fix.

## Excerpt 14: `_release_gate_check.py:128-129` — gate_frontmatter file-read catch

```python
for path in sorted(set(md_files)):
    if not path.exists():
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        bad.append(f"{path.name}: read failed ({exc})")
        continue
    if not text.startswith("---"):
        continue
    end = text.find("\n---", 3)
    if end == -1:
        bad.append(f"{path.name}: frontmatter unterminated")
        continue
    block = text[3:end]
    try:
        yaml.safe_load(block)
    except Exception as exc:
        bad.append(f"{path.name}: yaml parse error ({exc})")
```

Two broad excepts. First should be `(OSError, UnicodeDecodeError)`; second should be `yaml.YAMLError`. As written, a regex-engine bug in `text.find` would be reported as "yaml parse error" — wrong attribution.

## Excerpt 15: `server.py:230` and `server.py:284` — JSON-RPC boundary + boot-gate

```python
try:
    result = handler(**arguments)
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
    }
except Exception as exc:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32000, "message": str(exc)},
    }
```

And:
```python
try:
    findings = _run_disposition_check()
except Exception as exc:
    print(f"ERROR: disposition check failed: {exc}", file=sys.stderr)
    return 2
```

Both are **MCP protocol boundary / top-level CLI catch-alls** — the JSON-RPC handler MUST return a structured error rather than crashing the server. **Likely intentional**. Recommend: log traceback to audit log (not protocol response) so debug data isn't lost.

## What I expect codex to flag

Findings I'd consider valid (priority order):
1. `_self_drive._read_yaml_or_empty` line 296 — broad except, hot path used 10+ times per stop-rule check
2. `_self_drive` line 539 `patch_size_exceeds_max` — broad except + bare `pass` with NO breadcrumb
3. `_validate_closure_invariant.py` line 77 + 180 — broad except returning sentinel hides real bugs
4. `_release_gate_check.py` subprocess gate pattern (~10 callsites) — narrow to `(subprocess.TimeoutExpired, OSError, subprocess.SubprocessError)`
5. `_self_drive._evaluate_one_gate` line 285 — same pattern as gates
6. `_self_drive` line 438 `review_yaml_parse_failed` — narrow to `yaml.YAMLError`
7. `_release_gate_check.gate_frontmatter` lines 128 + 141 — narrow file-read and yaml-parse excepts independently
8. `_self_drive._check_drift` line 633 — narrow to `(OSError, yaml.YAMLError)`

## Out of scope for this audit

- **Security** (separate audit running in parallel)
- **Performance** (separate audit running in parallel)
- **Cross-platform** (separate audit running in parallel)
- **Tests** (`tests/*.py`, `_smoke_test.py`) — these often catch + ignore intentionally for test-isolation reasons; not flagged here
- **Top-level CLI boundary handlers** that are explicitly there to convert tracebacks into structured JSON / exit codes — Excerpts 9, 10, 15 above are noted but **likely intentional** and should be defended, not narrowed. Codex may confirm or contest the intent assessment.
- Style / naming / docstrings
- Adding traceback.format_exc() audit logging — out of scope (separate refactor)
