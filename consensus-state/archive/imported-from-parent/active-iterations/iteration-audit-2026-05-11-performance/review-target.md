# Performance audit — agent-loop stop-rule + dispatch hot paths

Audit dimension: **performance** (subprocess overhead, repeated yaml parse, n² stop-rule patterns, dispatch-log scan cost, redundant audit-log re-parse).

## Audit question for codex

Review the following code excerpts for performance defects. Specific concerns (non-exclusive):

1. **subprocess.run / Popen overhead**: `_self_drive.cmd_check_stop_rules` shells out to git 4× minimum per invocation (`diff --cached`, `diff`, `ls-files`, `diff --numstat HEAD`), each with a 30s timeout and process-creation cost. `_evaluate_one_gate` further shells out to a user-supplied check via `shell=True` per gate per invocation. Is there a way to batch git calls, cache by repo state, or detect "no changes" cheaply before forking?
2. **Repeated `yaml.safe_load` on hot paths**: `cmd_check_stop_rules` parses the same goal_packet once and then calls `_read_yaml_or_empty(claude_path)` / `_read_yaml_or_empty(codex_path)` repeatedly across stop-rule branches (claude_path read 3+ times across lines 445/584/728/812; codex similar). Same review-packet.yaml re-parsed across drift-finding branches. Each `_read_yaml_or_empty` re-opens, re-reads, and re-parses the file. Cost matters when stop-rules are evaluated in a tight orchestrator poll loop.
3. **n² patterns in stop-rule evaluation**: `patch_would_touch_forbidden_files` does `for forbidden in forbidden_list: for c in changed_files:` (O(F × C)). For a goal_packet with 50 forbidden patterns and a 500-file changeset, that's 25k `fnmatch.fnmatch` calls per check_stop_rules invocation. The drift-finding loop scans `iteration_artifacts` then `claude_reviewed_sha` then `consensus.reviewed_artifacts`, each re-computing `_canonical_sha256_of_yaml_file` (which itself re-parses + re-dumps yaml) on every referenced file.
4. **Large dispatch-log.jsonl scan cost as log grows**: `_visibility_watchdog._read_jsonl` does `path.read_text()` then `splitlines()` then `json.loads` per line, building the full event list in memory. With a months-old append-only log this is unbounded. `find_stalled` then builds a dict over ALL events. No time-window cutoff, no streaming parse, no index.
5. **Redundant re-parse of audit-log in multiple stop-rule branches**: `audit_path` (independence-audit.yaml) is read at line 403 (iter_count), and again at line 812 (closure_invariant). `yaml.safe_load(audit_path.read_bytes())` on a large audit yaml runs twice. Same applies to repeated `_read_yaml_or_empty(claude_path)` calls across the validator-disagreement and closure-invariant branches.
6. **Prompt build cost in `_dispatch_codex._build_prompt`**: Linear `str.replace` loop over the substitutions dict for every dispatch; for very large `{touched_files_contents_block}` (multi-file review-packets), `_format_touched_files_contents` likely traverses the dict and concatenates strings. Quadratic risk if the formatter does `result += chunk` instead of `"".join(...)`.

## Files in scope (excerpts below)

- `scripts/agent_loop_mcp/_self_drive.py` (cmd_check_stop_rules + helpers)
- `scripts/agent_loop_mcp/_dispatch_codex.py` (subprocess invocation, prompt build)
- `scripts/agent_loop_mcp/_visibility_watchdog.py` (dispatch-log scan)

---

## Excerpt 1: `_self_drive.py:175-218` — `_collect_changed_files` runs 3 git subprocesses serially

```python
def _collect_changed_files(repo_root: Path) -> list[str]:
    """Return the de-duplicated union of git's three change-views.
    ... covers all three:
      1. `git diff --cached --name-only`           (staged)
      2. `git diff --name-only`                    (unstaged tracked)
      3. `git ls-files --others --exclude-standard` (untracked, gitignore-honoring)
    """
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
            continue
    return sorted(seen)
```

Three subprocess.run calls back-to-back. Each git invocation pays its own fork+exec + libgit index-load. Used by `patch_would_touch_forbidden_files`, `cmd_verify_scope` — invoked once per `cmd_check_stop_rules`, which runs frequently in the orchestrator poll loop.

## Excerpt 2: `_self_drive.py:262-286` — `_evaluate_one_gate` shells out per gate with shell=True

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

Called inside the `any_acceptance_gate_returns_undefined` loop in `cmd_check_stop_rules:548-553` (one subprocess per gate per stop-rule check) AND inside `cmd_evaluate_gates:876` (one subprocess per gate per evaluate-gates call). A 10-gate goal_packet eats 10 forks per check_stop_rules invocation. shell=True adds an extra shell process layer.

## Excerpt 3: `_self_drive.py:289-297` — `_read_yaml_or_empty` re-reads + re-parses on every call

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

Call sites inside ONE invocation of `cmd_check_stop_rules` (line numbers from `_self_drive.py`):
- 445/446: claude_review + codex_review (disagreement check)
- 583/584/585/586: claude_prior + claude_curr + codex_prior + codex_curr (recurring-finding check)
- 650: review_packet (drift check)
- 661: claude_review_for_drift (drift check — re-reads claude_path)
- 669: consensus (drift check)
- 722: verification.yaml fallback
- 728/729: claude_v + codex_v (validator-disagreement check) — re-reads claude_path AND codex_path
- 822: review (closure-invariant check) — re-reads claude_path/codex_path

claude-review.yaml is re-parsed up to 4× per single `cmd_check_stop_rules` invocation. codex-review.yaml similar. No memoization.

## Excerpt 4: `_self_drive.py:339-348` — canonical-sha helper re-parses + re-dumps yaml

```python
def _canonical_sha256_of_yaml_file(p: Path) -> str:
    """Canonical-full sha256: hashlib.sha256(yaml.safe_dump(yaml.safe_load(...), sort_keys=True)).
    """
    loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    canonical = yaml.safe_dump(loaded, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Called by `_check_drift` for every claimed-sha in `review-packet.iteration_artifacts[]`, `claude-review.reviewed_packet_sha256`, and `consensus.reviewed_artifacts[]`. If the same referenced_path appears in two claims (common — review_packet AND consensus both anchor the same artifact), the file is re-loaded, re-dumped, re-hashed each time. No path-level cache.

## Excerpt 5: `_self_drive.py:407-409` + `:811-815` — audit_path read twice per stop-rule invocation

```python
audit_path = iter_dir / "independence-audit.yaml"
iter_count = 0
if audit_path.exists():
    audit = yaml.safe_load(audit_path.read_bytes()) or {}
    log = audit.get("audit_log", []) or []
    iter_count = sum(1 for e in log if e.get("event") == "patch_applied")
# ... 400+ lines later ...
if _check_inv is not None and audit_path.exists():
    audit = yaml.safe_load(audit_path.read_bytes()) or {}
    log = audit.get("audit_log", []) or []
    last_mutation = _lm_from_audit(log)
```

The same `independence-audit.yaml` is read and parsed twice in two separate stop-rule branches. For a long-running iteration with a large audit log, this is a noticeable double-cost.

## Excerpt 6: `_self_drive.py:410-421` — n² fnmatch loop over forbidden_files × changed

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

`_path_matches_pattern` calls `fnmatch.fnmatch` when wildcards are present. With F forbidden patterns and C changed files, this is O(F × C) regex compilations per call. fnmatch does NOT cache compiled patterns; it recompiles each invocation.

## Excerpt 7: `_self_drive.py:467-542` — patch_size_exceeds_max walks all untracked files synchronously

```python
max_patch_size = packet.get("max_patch_size")
if max_patch_size:
    try:
        total = 0
        ns = subprocess.run(
            ["git", "diff", "--numstat", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, check=False, timeout=30,
        )
        for line in ns.stdout.splitlines():
            # ... parse numstat
            total += added + deleted

        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(repo_root), capture_output=True, text=True, check=False, timeout=30,
        )
        for line in untracked.stdout.splitlines():
            rel = line.strip()
            if not rel:
                continue
            f = repo_root / rel
            try:
                if f.is_symlink():
                    total += 1
                    continue
                with f.open("rb") as fh:
                    content = fh.read()
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                lines = text.count("\n")
                if text and not text.endswith("\n"):
                    lines += 1
                total += lines
            except OSError:
                continue
        # ...
    except Exception:
        pass
```

This adds 2 more git subprocesses to the stop-rule check (on top of `_collect_changed_files`'s 3), AND reads every untracked file's full content from disk to count lines. For a session with 100+ untracked files (run/ artifacts, pytest tmp, etc.), this is meaningful I/O per stop-rule poll.

## Excerpt 8: `_self_drive.py:544-561` — `any_acceptance_gate_returns_undefined` re-runs every gate

```python
undefined_gates = []
for gate in packet.get("acceptance_gates", []) or []:
    result = _evaluate_one_gate(gate, repo_root)
    if "exception" in result:
        undefined_gates.append({"id": result["id"], "kind": "exception", "detail": result["exception"]})
    elif result.get("error") == "no_check_command":
        undefined_gates.append({"id": result["id"], "kind": "no_check_command"})
```

`cmd_check_stop_rules` runs ALL acceptance_gates via subprocess for the side-effect of checking "is any gate undefined". This is N subprocesses (typically 5-15 in real goal_packets). `cmd_evaluate_gates` ALSO runs all gates separately (line 876). If the orchestrator calls both, every gate gets shelled out twice.

## Excerpt 9: `_self_drive.py:568-606` — recurring-finding rule walks all sibling iter dirs

```python
parent_dir = iter_dir.parent
if parent_dir.exists():
    try:
        siblings = sorted(
            d.name for d in parent_dir.iterdir()
            if d.is_dir() and d.name.startswith("iteration-")
        )
    except OSError:
        siblings = []
    if iter_dir.name in siblings:
        idx = siblings.index(iter_dir.name)
        if idx > 0:
            prior_name = siblings[idx - 1]
            prior_dir = parent_dir / prior_name
            claude_prior = _read_yaml_or_empty(prior_dir / "claude-review.yaml")
            claude_curr = _read_yaml_or_empty(claude_path)
            codex_prior = _read_yaml_or_empty(prior_dir / "codex-review.yaml")
            codex_curr = _read_yaml_or_empty(codex_path)
            # ... 4 yaml parses
```

`parent_dir.iterdir()` lists every sibling iteration directory (could be hundreds in agent-loop/archive equivalents) plus stat per entry to test `is_dir()`. Then 4 more yaml-parse calls. claude_path is re-parsed here even though it was already parsed at line 445.

## Excerpt 10: `_dispatch_codex.py:431-517` — `_invoke_codex` fork+exec cost per dispatch

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
        result = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout_seconds,
        )
        # ... + earlier `_get_codex_version` shells out separately to `<codex> --version`
```

Per dispatch: at least 2 subprocess invocations (`<codex> --version` for audit metadata + `<codex> exec ...`). Both pay node.js startup cost (codex CLI is js). Could the version be cached across calls in the same Python process? `_resolve_codex_bin` also does `shutil.which` lookups multiple times across helper invocations.

## Excerpt 11: `_dispatch_codex.py:285-362` — `_build_prompt` linear replace loop

```python
def _build_prompt(goal_packet, template_text, iteration_dir=None, review_packet_path=None,
                  review_target_path=None, review_target_hash=None, review_packet=None) -> str:
    # ...
    substitutions = {
        "{goal_summary}": str(goal.get("summary", "")),
        "{desired_end_state}": str(goal.get("desired_end_state", "")),
        "{allowed_files}": _format_list(goal_packet.get("allowed_files", [])),
        "{acceptance_gates}": _format_gates(goal_packet.get("acceptance_gates", [])),
        "{scope_signature}": str(auth.get("scope_signature", "")),
        # ... ~10 keys total
        "{touched_files_contents_block}": _format_touched_files_contents(touched_contents),
    }
    out = template_text
    for placeholder, value in substitutions.items():
        out = out.replace(placeholder, value)
    return out
```

For each substitution, a full pass over `template_text` (and the growing `out` after each replace). For a template + touched_files_contents_block that runs to 50k+ chars, that's ~10 full-string scans per dispatch. Probably not hot, but worth checking the `_format_touched_files_contents` implementation for quadratic string concat.

## Excerpt 12: `_visibility_watchdog.py:83-140` — full dispatch-log read on every watchdog run

```python
def _read_jsonl(path: Path) -> list[dict]:
    """Read JSONL; tolerate missing file + malformed lines (mirrors TUI)."""
    if not path.exists():
        return []
    events: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def find_stalled(events, stall_threshold_seconds: float, now: datetime) -> list[dict]:
    """Return orphan dispatch_start events older than stall_threshold_seconds.
    """
    by_key: dict[tuple, dict] = {}
    for ev in events:
        pass_id = ev.get("pass_id") or ev.get("reviewer_id")
        if not pass_id:
            continue
        iter_id = ev.get("iteration_id")
        key = (iter_id, pass_id)
        entry = by_key.setdefault(key, {"start": None, "end": None})
        kind = ev.get("event")
        if kind == "dispatch_start":
            entry["start"] = ev
        elif kind in _TERMINAL_EVENTS:
            entry["end"] = ev
    # ... iterates by_key
```

Full file slurp into memory, full event-list build, then full-dict scan. As dispatch-log.jsonl grows monotonically (append-only across all iterations), every watchdog run pays the cost of the entire history. No tail-only mode, no offset/cursor, no time-window prefilter.

## What I expect codex to flag

Findings I'd consider valid:
- A memoize-once pattern that would eliminate 3-4 of the 5+ yaml.safe_load calls on claude-review.yaml within one `cmd_check_stop_rules` invocation
- The double-read of `independence-audit.yaml` at lines 403 and 812 collapsed into one parse
- A way to short-circuit `_collect_changed_files` when `git status --porcelain` reports clean
- An obvious win in batching `_evaluate_one_gate` calls or caching results within a single check_stop_rules
- Tail-only or cursor-based read for `_visibility_watchdog._read_jsonl` that doesn't slurp the whole file each invocation
- fnmatch pattern compilation cache (or hoisted regex) for `_path_matches_pattern`
- An n² hot path I missed (e.g., per-iteration sibling enumeration scaling badly)
- Whether `_canonical_sha256_of_yaml_file` can be memoized by `(path, mtime)`

## Out of scope for this audit

- **Security** (separate audit running in parallel)
- **Cross-platform / Windows correctness** (separate audit running in parallel)
- **Bare-except patterns** (separate audit running in parallel)
- Style / naming / docstrings
- Test coverage gaps
