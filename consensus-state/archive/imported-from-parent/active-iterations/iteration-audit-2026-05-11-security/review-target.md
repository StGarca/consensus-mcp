# Security audit — agent-loop dispatch + content-embedding code

Audit dimension: **security** (path traversal, command injection, prompt injection, content exposure, secret leakage in audit log).

## Audit question for codex

Review the following code excerpts for security defects. Specific concerns (non-exclusive):

1. **Path traversal**: Can operator-supplied paths to `--goal-packet`, `--review-target`, `--prompt-template`, `--schema`, or `--iteration-dir` reach files outside repo_root via symlinks, relative-with-`..`, UNC paths, or other tricks the containment check missed?
2. **Command injection**: subprocess.run / Popen invocations — is `shell=` ever True? Are user-provided strings interpolated into commands without quoting?
3. **Prompt injection**: when codex output is parsed back into structured findings (citation, summary, risk text), can a malicious codex emit content that breaks the parser or escapes downstream context? E.g., a citation containing yaml control characters that breaks `_seal_via_t6` writing.
4. **Content exposure in audit log**: dispatch-log.jsonl + sealed codex-review.yaml — is there any path where prompt content / goal_packet content / source code content leaks into the audit log unintentionally? (The contract is hashes only; check it holds.)
5. **Schema-injection**: codex_review_schema.json is loaded into prompt — if an attacker controlled it, what's the blast radius?
6. **Secret leakage**: are environment variables, AGENT_LOOP_MCP_REPO_ROOT, codex auth tokens, etc. logged anywhere?

## Files in scope (excerpts below)

The full files live at the cited paths. Only the security-relevant excerpts are embedded.

---

## Excerpt 1: `_dispatch_codex.py` lines 167-210 (containment)

```python
class OutsideRepoPathError(ValueError):
    """v1.10.5 containment hardening: operator-supplied path resolves outside repo_root."""


def _normalize_relative_to_repo(path_str: str | None, repo_root: Path) -> Path | None:
    """Normalize an operator-supplied path against repo_root.
    ... (docstring)
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

## Excerpt 2: `_dispatch_codex.py` subprocess invocation pattern

```python
def _invoke_codex(prompt, codex_bin, timeout_seconds, repo_root, schema_path):
    """Run codex via subprocess.run with --output-schema gated by JSON schema."""
    # ... codex_bin is resolved via shutil.which earlier; never shell=True
    proc = subprocess.run(
        [codex_bin, "exec", "--output-schema", str(schema_path), "--cd", str(repo_root), "-o", out_path],
        input=prompt.encode("utf-8"),
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    # ... output read from out_path, sha-hashed, parsed
```

## Excerpt 3: `_dispatch_codex.py` audit-log writing

```python
def _log_dispatch(log_path, event):
    """Append-only JSONL audit log.
    Secrets must NEVER be logged. The raw subprocess cmd list is NOT passed to
    this writer; callers log only codex_bin (string) + schema_path (string) +
    timeout_seconds. Raw prompt / codex output / goal_packet content are never
    logged; only their sha256 digests are.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    event_with_ts = {"timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event_with_ts) + "\n")
```

## Excerpt 4: codex output parsing (high-risk surface)

```python
def _parse_codex_output(text, goal_packet=None, review_packet=None, repo_root=None):
    """Parse codex's JSON output; validate against schema invariants."""
    data = json.loads(text)
    # ... iterates findings[], each finding has id/severity/summary/citation/risk/recommendation
    # ... patch_proposal validated against _PATCH_PROPOSAL_REQUIRED / _PATCH_PROPOSAL_ALLOWED
    # ... scope check against goal_packet allowed/forbidden_files
    # ... base_sha helper-stamped from review_packet.defect_target.base_sha
```

## Excerpt 5: review-packet content embedding (`_author_review_packet.py`)

```python
def author_review_packet(iteration_dir, files, repo_root):
    """Embed touched-file contents into review-packet.yaml so codex can read
    them in-prompt without filesystem access.
    """
    contents = {}
    for rel in files:
        full = repo_root / rel
        contents[rel] = full.read_text(encoding="utf-8")  # < attacker-controlled?
    # ... base_sha computed from contents; written to review-packet.yaml
```

## What I expect codex to flag

Findings I'd consider valid:
- A path traversal vector through the containment check I missed
- A way for codex's emitted finding text to break the yaml writer or pollute downstream context
- An audit-log path where prompt/code content leaks despite the hash-only contract
- A subprocess invocation where unquoted strings could be interpreted as shell metachars

Out of scope for this audit:
- Performance (separate audit running in parallel)
- Cross-platform (separate audit running in parallel)
- Style / naming / docstrings
- Bare-except patterns (separate audit running in parallel)
