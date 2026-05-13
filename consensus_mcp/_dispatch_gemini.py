"""Phase 4 v1.14.0 — auto-gemini-dispatch helper (gemini adapter).

Sibling of _dispatch_codex.py. Reuses generic dispatch infrastructure from
_dispatch_base.py (extracted in iter-0010); supplies gemini-specific code:
CLI invocation shape, binary resolution, output JSON parsing with
validator-retry on schemaless parse fail (per iter-0009 verdict Q2: F2b).

Scope (iter-0011): REVIEW-ONLY for patch authoring. Gemini does NOT author
patch_proposal blocks in v1.14.0; codex remains the only patch-authoring
adapter. Patch authoring across adapters is deferred to iter-0013 capability
metadata.

iter-0028 extension: dispatcher gained `--mode {review,proposal}` (default
'review') per the iter-0027 converged plan. Proposal mode uses
`gemini_proposal_template.md` + `gemini_proposal_schema.json` and accepts a
`--schema` flag to override the proposal schema (proposal-mode only;
ignored in review mode, where validation is template-embedded).

USAGE
-----
  python -m consensus_mcp._dispatch_gemini \\
      --goal-packet <path/to/goal_packet.yaml> \\
      --iteration-dir <path/to/iteration-XXXX/> \\
      [--reviewer-id gemini-iterXXXX-N] \\
      [--pass-id gemini-iterXXXX-N-passN] \\
      [--prompt-template <path>] \\
      [--mode {review,proposal}] \\
      [--schema <path>]                    # proposal-mode only \\
      [--gemini-bin <path>] \\
      [--model gemini-2.5-pro] \\
      [--timeout-seconds 600] \\
      [--review-target <path>] \\
      [--smoke]

Review-mode output schema: embedded in
dispatch_templates/gemini_review_template.md (under "Schema reference"),
authoritative — review mode has no separate --schema flag.

Proposal-mode output schema: dispatch_templates/gemini_proposal_schema.json
by default; --schema overrides. The helper validates parsed output against
the effective schema and records its path + sha256 in dispatch_provenance.

Exit 0 = sealed pass produced; non-zero = failure (gemini error, parse fail,
seal fail). JSON to stdout on success.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

from consensus_mcp._dispatch_base import (
    _REPO_ROOT_MARKERS,
    RepoRootResolutionError,
    _has_repo_markers,
    _resolve_repo_root,
    OutsideRepoPathError,
    _normalize_for_compare,
    _normalize_relative_to_repo,
    _load_goal_packet,
    _load_template,
    _FENCE_LANG_BY_EXT,
    _format_touched_files_contents,
    _build_prompt,
    _terminate_process_tree,
    _sha256_str,
    _build_sealed_packet,
    _seal_via_t6,
    _log_dispatch,
)


# Adapter-specific finding patterns. Gemini emits gemini-rev-N IDs; the
# codex_review_schema.json id pattern is codex-rev-N, so we cannot share
# the validator directly. Mirror codex's enum + allowed-key set since the
# semantic contract (severity values, required fields) is identical.
_GEMINI_FINDING_ID_PATTERN = re.compile(r"^gemini-rev-\d+$")
_VALID_SEVERITIES = {"low", "medium", "high", "blocking", "critical"}
_REQUIRED_FINDING_FIELDS = ("id", "severity", "summary", "citation", "risk", "recommendation")
# iter-0011 scope: gemini does NOT emit patch_proposal in v1.14.0. Both
# patch_proposal and patch_not_proposed_reason are still in the allowed set
# (must be null per schema) but the parser enforces patch_proposal IS null
# (since the codex-rev- pattern wouldn't match gemini-rev- patch IDs anyway).
_ALLOWED_FINDING_KEYS = set(_REQUIRED_FINDING_FIELDS) | {"patch_proposal", "patch_not_proposed_reason"}
_ALLOWED_TOP_LEVEL_KEYS = {"findings", "goal_satisfied", "blocking_objections", "goal_satisfied_rationale"}
_BLOCKING_SEVERITIES = {"blocking", "critical"}

# Default gemini model. Operator can override via --model.
_DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"


class GeminiInvocationError(RuntimeError):
    """Raised when the gemini CLI exits non-zero, times out, or is not found."""


class GeminiOutputParseError(ValueError):
    """Raised when gemini output is not parseable JSON or lacks the expected top-level shape."""


def _resolve_gemini_bin(gemini_bin: str) -> str:
    """Resolve a gemini binary spec to an actual executable file path.

    Mirrors _resolve_codex_bin's Windows hardening: PATHEXT-aware lookup,
    .cmd preference over .ps1, App Execution Alias rejection, MSYS-path
    conversion. Independent of codex's implementation because the npm
    install layout / executable shape is the same for gemini-cli.
    """
    if (
        sys.platform == "win32"
        and len(gemini_bin) >= 3
        and gemini_bin[0] == "/"
        and gemini_bin[1].isalpha()
        and gemini_bin[2] == "/"
    ):
        drive = gemini_bin[1].upper()
        rest = gemini_bin[3:].replace("/", "\\")
        gemini_bin = f"{drive}:\\{rest}"

    if os.path.sep in gemini_bin or (len(gemini_bin) > 1 and gemini_bin[1] == ":"):
        if sys.platform == "win32":
            p = Path(gemini_bin)
            try:
                if p.exists() and p.stat().st_size == 0:
                    windows_apps = (
                        os.environ.get("LOCALAPPDATA", "")
                        + r"\Microsoft\WindowsApps"
                    )
                    if windows_apps and str(p).lower().startswith(windows_apps.lower()):
                        raise GeminiInvocationError(
                            f"gemini binary {gemini_bin!r} is a Windows App Execution "
                            f"Alias stub (0-byte file in WindowsApps). subprocess cannot "
                            f"exec App Aliases — install the real gemini CLI binary or "
                            f"adjust PATH so a non-stub variant is preferred."
                        )
            except OSError:
                pass
        return gemini_bin

    if sys.platform == "win32" and "." not in Path(gemini_bin).name:
        for ext in (".exe", ".cmd", ".bat", ".ps1"):
            cand = shutil.which(gemini_bin + ext)
            if cand:
                return _resolve_gemini_bin(cand)

    resolved = shutil.which(gemini_bin)
    if resolved is None:
        return gemini_bin
    if sys.platform == "win32" and resolved.lower().endswith(".ps1"):
        cmd_alt = shutil.which(gemini_bin + ".cmd")
        if cmd_alt:
            resolved = cmd_alt
    if sys.platform == "win32":
        p = Path(resolved)
        try:
            if p.exists() and p.stat().st_size == 0:
                windows_apps = (
                    os.environ.get("LOCALAPPDATA", "")
                    + r"\Microsoft\WindowsApps"
                )
                if windows_apps and str(p).lower().startswith(windows_apps.lower()):
                    raise GeminiInvocationError(
                        f"gemini binary resolved to {resolved!r} which is a Windows "
                        f"App Execution Alias stub (0-byte file in WindowsApps). "
                        f"subprocess cannot exec App Aliases — install the real "
                        f"gemini CLI binary or adjust PATH so a non-stub variant is "
                        f"preferred."
                    )
        except OSError:
            pass
    return resolved


def _get_gemini_version(gemini_bin: str) -> str:
    """Best-effort: shell out to `<gemini> --version` and return the version string.

    Returns 'unknown' on any failure. Used for audit-log provenance only.
    """
    try:
        result = subprocess.run(
            [_resolve_gemini_bin(gemini_bin), "--version"],
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


def _invoke_gemini(
    prompt: str,
    gemini_bin: str,
    model: str,
    timeout_seconds: int,
    repo_root: Path,
    log_path=None,
    anchors=None,
    heartbeat_interval: float = 30.0,
    stall_silence_seconds: float = 180.0,
    poll_interval: float = 0.5,
    time_fn=None,
    popen_factory=None,
) -> str:
    """Shell out to gemini CLI via Popen + reader threads.

    Pattern mirrors _invoke_codex's stream-and-watchdog loop (iter-0037):
      - Popen with stdout + stderr reader threads (prevents pipe-buffer deadlock)
      - dispatch_streamed_line per stdout line + dispatch_heartbeat at interval
      - Auto-abort on heartbeat-silence
      - log_path + anchors optional (None disables streaming events for tests)

    Gemini-specific CLI shape:
      gemini -p "<short directive>" -m <model> --approval-mode plan --skip-trust
    The actual prompt body is piped via stdin (gemini appends -p text after
    stdin). -p is required to trigger headless mode. --approval-mode plan is
    gemini's read-only mode (no file writes, no shell exec) — the parallel
    to codex's --sandbox read-only. --skip-trust suppresses the workspace-trust
    interactive prompt.

    stall_silence_seconds defaults to 180s (gemini cold-start can exceed 45s
    on large prompts; matches the codex adapter's iter-0010 default bump).
    """
    if time_fn is None:
        time_fn = time.time
    if popen_factory is None:
        popen_factory = subprocess.Popen
    can_log = log_path is not None and anchors is not None

    cmd = [
        _resolve_gemini_bin(gemini_bin),
        "-p", "Now respond. Output ONLY the JSON described above; no prose, no markdown fences.",
        "-m", model,
        "--approval-mode", "plan",
        "--skip-trust",
    ]
    if sys.platform == "win32":
        popen_kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        popen_kwargs = {"start_new_session": True}
    try:
        proc = popen_factory(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=str(repo_root),
            **popen_kwargs,
        )
    except FileNotFoundError:
        raise GeminiInvocationError(f"gemini binary not found: {gemini_bin}") from None

    # codex-rev-001 round-1 fix: start readers + watchdog BEFORE writing stdin.
    # Gemini may emit output (or refuse to drain its stdin) before we finish
    # sending the prompt; without readers running, that deadlocks.
    state_lock = threading.Lock()
    stdout_buf: list = []
    stderr_buf: list = []
    last_streamed_ts: list = [None]
    streamed_seq: list = [0]

    abort_path = None
    if can_log:
        abort_path = repo_root / "consensus-state" / "state" / f"abort-dispatch-{anchors['pass_id']}.signal"

    def stdout_reader():
        try:
            for raw_line in iter(proc.stdout.readline, b""):
                if not raw_line:
                    break
                with state_lock:
                    stdout_buf.append(raw_line)
                    seq = streamed_seq[0]
                    streamed_seq[0] = seq + 1
                    last_streamed_ts[0] = time_fn()
                if can_log:
                    line_str = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    full_len = len(line_str)
                    _log_dispatch(log_path, {
                        "event": "dispatch_streamed_line",
                        **anchors,
                        "stream": "stdout",
                        "line_truncated": line_str[:200],
                        "line_full_length": full_len,
                        "truncated": full_len > 200,
                        "seq": seq,
                    })
        except Exception:
            pass

    def stderr_reader():
        try:
            for raw_line in iter(proc.stderr.readline, b""):
                if not raw_line:
                    break
                with state_lock:
                    stderr_buf.append(raw_line)
        except Exception:
            pass

    t_stdout = threading.Thread(target=stdout_reader, daemon=True, name="gemini-stdout-reader")
    t_stderr = threading.Thread(target=stderr_reader, daemon=True, name="gemini-stderr-reader")
    t_stdout.start()
    t_stderr.start()

    # codex-rev-001 round-1 fix: write stdin AFTER readers are running so
    # gemini-side stdout/stderr pressure can't deadlock us before drain begins.
    # Use a writer thread (daemon) so a slow-to-drain stdin doesn't block the
    # watchdog loop; the watchdog will still abort if the process stalls.
    def stdin_writer():
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    t_stdin = threading.Thread(target=stdin_writer, daemon=True, name="gemini-stdin-writer")
    t_stdin.start()

    start_ts = time_fn()
    last_heartbeat = start_ts

    while proc.poll() is None:
        now = time_fn()

        if abort_path is not None and abort_path.exists():
            try:
                abort_reason = abort_path.read_text(encoding="utf-8").strip() or "operator_signal_file"
            except OSError:
                abort_reason = "operator_signal_file (unreadable)"
            _terminate_process_tree(proc)
            with state_lock:
                silence_age = (now - last_streamed_ts[0]) if last_streamed_ts[0] is not None else None
            if can_log:
                _log_dispatch(log_path, {
                    "event": "dispatch_aborted",
                    **anchors,
                    "abort_source": "operator_signal_file",
                    "abort_reason": abort_reason,
                    "age_seconds": now - start_ts,
                    "last_streamed_line_age_seconds": silence_age,
                })
            try:
                abort_path.unlink()
            except OSError:
                pass
            raise GeminiInvocationError(f"dispatch aborted by operator: {abort_reason}")

        with state_lock:
            lst = last_streamed_ts[0]
            seq_snap = streamed_seq[0]
        if lst is not None:
            # Inter-line silence: gemini already started emitting; use the
            # configured stall_silence threshold.
            silence_age = now - lst
            silence_trigger_threshold = stall_silence_seconds
        else:
            # codex-rev-001 round-2 fix: pre-first-byte silence. Gemini may
            # process a long prompt without emitting anything for an extended
            # period (50KB+ prompts can take 200s+ before first token). Use
            # the operator-visible timeout_seconds budget as the threshold
            # here, not stall_silence_seconds — otherwise the watchdog aborts
            # a valid in-progress review.
            silence_age = now - start_ts
            silence_trigger_threshold = float(timeout_seconds)

        if silence_age >= silence_trigger_threshold:
            _terminate_process_tree(proc)
            if can_log:
                _log_dispatch(log_path, {
                    "event": "dispatch_aborted",
                    **anchors,
                    "abort_source": "watchdog_silence",
                    "abort_reason": f"no gemini stdout for {silence_age:.0f}s (threshold {silence_trigger_threshold:.0f}s)",
                    "age_seconds": now - start_ts,
                    "last_streamed_line_age_seconds": silence_age if lst is not None else None,
                })
            raise GeminiInvocationError(
                f"gemini stuck: no output for {silence_age:.0f}s"
            )

        if now - last_heartbeat >= heartbeat_interval:
            if can_log:
                _log_dispatch(log_path, {
                    "event": "dispatch_heartbeat",
                    **anchors,
                    "age_seconds": now - start_ts,
                    "last_streamed_line_age_seconds": silence_age if lst is not None else None,
                    "last_streamed_line_seq": seq_snap - 1 if seq_snap > 0 else None,
                })
            last_heartbeat = now

        if now - start_ts >= timeout_seconds + stall_silence_seconds:
            _terminate_process_tree(proc)
            if can_log:
                _log_dispatch(log_path, {
                    "event": "dispatch_aborted",
                    **anchors,
                    "abort_source": "wall_time_hard_ceiling",
                    "abort_reason": f"wall_time={now - start_ts:.0f}s exceeded timeout_seconds={timeout_seconds}s + grace={stall_silence_seconds}s",
                    "age_seconds": now - start_ts,
                    "last_streamed_line_age_seconds": silence_age if lst is not None else None,
                })
            raise GeminiInvocationError(
                f"gemini exceeded {timeout_seconds}s wall timeout + {stall_silence_seconds}s grace"
            )

        time.sleep(poll_interval)

    t_stdout.join(timeout=5)
    t_stderr.join(timeout=5)
    if t_stdout.is_alive() or t_stderr.is_alive():
        raise GeminiInvocationError(
            "gemini exited but reader thread did not drain within 5s; "
            "possible partial output or pipe-handling defect"
        )

    with state_lock:
        stdout_bytes = b"".join(stdout_buf)
        stderr_bytes = b"".join(stderr_buf)

    if proc.returncode != 0:
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")
        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_tail = stderr_str.strip()[-4000:]
        stdout_hint = (
            f"; stdout_tail={stdout_str.strip()[-500:]!r}"
            if stdout_str.strip()
            else ""
        )
        raise GeminiInvocationError(
            f"gemini exit={proc.returncode}; stderr_tail={stderr_tail!r}{stdout_hint}"
        )

    return stdout_bytes.decode("utf-8", errors="replace")


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_from_text(text: str) -> str:
    """Extract a JSON object substring from gemini's free-form output.

    Gemini lacks codex's --output-schema enforcement, so its response may
    include leading prose, markdown fences, or trailing commentary even
    when explicitly told to emit JSON only. This helper applies a small
    ladder of recoveries:

      1. Whole-string trim — if the trimmed text starts with `{` and ends
         with `}`, return as-is.
      2. Fenced-code-block extraction — first ```json ... ``` (or bare
         ``` ... ```) containing a `{...}` block.
      3. Greedy outermost-brace match — first `{` to last `}`. Fragile
         (won't handle nested unbalanced braces), but a final fallback.

    Returns the candidate JSON string. Does NOT validate that it parses;
    the caller does. If nothing matches, returns the original text so the
    JSONDecodeError downstream carries the actual gemini output as diagnostic.
    """
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        return fenced.group(1)
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        return text[first_brace:last_brace + 1]
    return text


def _parse_gemini_output(
    text: str,
    goal_packet: dict | None = None,
) -> dict:
    """Parse gemini's JSON response + locally validate shape.

    Gemini lacks codex's --output-schema, so this parser is intentionally
    more lenient on extraction (handles fenced/wrapped JSON via
    _extract_json_from_text) but equally strict on the parsed shape.

    Rules (mirrors codex's _parse_codex_output minus patch_proposal):
      - root is a JSON object
      - top-level keys exactly {findings, goal_satisfied, blocking_objections,
        goal_satisfied_rationale}
      - findings is a list; each finding has all 6 required fields
        (id, severity, summary, citation, risk, recommendation)
      - id matches ^gemini-rev-\\d+$ (NOT codex-rev-)
      - severity in the canonical enum
      - patch_proposal/patch_not_proposed_reason MUST be null in v1.14.0
        (iter-0011 scope: gemini is review-only)
      - blocking_objections invariant: set equals blocking/critical finding IDs
      - goal_satisfied is bool; goal_satisfied_rationale is non-empty string

    Raises GeminiOutputParseError on any violation.
    """
    candidate = _extract_json_from_text(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise GeminiOutputParseError(
            f"gemini output is not valid JSON: {exc}; first 500 chars of raw: {text[:500]!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise GeminiOutputParseError(
            f"gemini output JSON root must be an object, got {type(parsed).__name__}"
        )

    unknown_top = set(parsed.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_top:
        raise GeminiOutputParseError(
            f"gemini output JSON has unexpected top-level keys: {sorted(unknown_top)}"
        )

    for required in ("findings", "goal_satisfied", "blocking_objections", "goal_satisfied_rationale"):
        if required not in parsed:
            raise GeminiOutputParseError(f"gemini output JSON missing required key: {required!r}")

    if not isinstance(parsed["findings"], list):
        raise GeminiOutputParseError("'findings' must be an array")
    if not isinstance(parsed["goal_satisfied"], bool):
        raise GeminiOutputParseError(
            f"'goal_satisfied' must be boolean, got {type(parsed['goal_satisfied']).__name__}"
        )
    if not isinstance(parsed["blocking_objections"], list):
        raise GeminiOutputParseError(
            f"'blocking_objections' must be an array, got {type(parsed['blocking_objections']).__name__}"
        )
    if not isinstance(parsed["goal_satisfied_rationale"], str):
        raise GeminiOutputParseError(
            f"'goal_satisfied_rationale' must be string, got "
            f"{type(parsed['goal_satisfied_rationale']).__name__}"
        )
    # codex-rev-002 round-2 fix: enforce non-empty rationale per the prompt
    # contract. Empty/whitespace-only is incoherent with "always populate".
    if not parsed["goal_satisfied_rationale"].strip():
        raise GeminiOutputParseError(
            "'goal_satisfied_rationale' must be a non-empty string (prompt contract)"
        )

    for i, item in enumerate(parsed["blocking_objections"]):
        if not isinstance(item, str):
            raise GeminiOutputParseError(
                f"blocking_objections[{i}] must be string, got {type(item).__name__}"
            )

    blocking_finding_ids = []
    for i, finding in enumerate(parsed["findings"]):
        if not isinstance(finding, dict):
            raise GeminiOutputParseError(
                f"findings[{i}] must be an object, got {type(finding).__name__}"
            )
        unknown_keys = set(finding.keys()) - _ALLOWED_FINDING_KEYS
        if unknown_keys:
            raise GeminiOutputParseError(
                f"findings[{i}] has unexpected keys: {sorted(unknown_keys)}"
            )
        for required in _REQUIRED_FINDING_FIELDS:
            if required not in finding:
                raise GeminiOutputParseError(
                    f"findings[{i}] missing required field: {required!r}"
                )
        for str_field in ("id", "severity", "summary", "citation", "risk", "recommendation"):
            if not isinstance(finding[str_field], str):
                raise GeminiOutputParseError(
                    f"findings[{i}].{str_field} must be string, got "
                    f"{type(finding[str_field]).__name__}"
                )
        # codex-rev-002 round-2 fix: schema requires summary minLength=1.
        # Enforce non-empty on the parser side too (gemini has no --schema).
        if not finding["summary"].strip():
            raise GeminiOutputParseError(
                f"findings[{i}].summary must be a non-empty string (schema contract)"
            )
        if finding["severity"] not in _VALID_SEVERITIES:
            raise GeminiOutputParseError(
                f"findings[{i}] has invalid severity {finding['severity']!r}; "
                f"must be one of {sorted(_VALID_SEVERITIES)}"
            )
        if not _GEMINI_FINDING_ID_PATTERN.match(finding["id"]):
            raise GeminiOutputParseError(
                f"findings[{i}] id {finding['id']!r} does not match pattern "
                f"^gemini-rev-\\d+$"
            )
        # iter-0011 scope: gemini is review-only; patch_proposal and
        # patch_not_proposed_reason must both be null (if present).
        pp = finding.get("patch_proposal")
        if pp is not None:
            raise GeminiOutputParseError(
                f"findings[{i}].patch_proposal must be null in v1.14.0 — gemini is "
                f"review-only (patch authoring deferred to iter-0013)"
            )
        reason = finding.get("patch_not_proposed_reason")
        if reason is not None and not isinstance(reason, str):
            raise GeminiOutputParseError(
                f"findings[{i}].patch_not_proposed_reason must be string or null, "
                f"got {type(reason).__name__}"
            )
        if finding["severity"] in _BLOCKING_SEVERITIES:
            blocking_finding_ids.append(finding["id"])

    expected_blocking = set(blocking_finding_ids)
    actual_blocking = set(parsed["blocking_objections"])
    if expected_blocking != actual_blocking:
        missing = expected_blocking - actual_blocking
        extra = actual_blocking - expected_blocking
        msg_parts = ["blocking_objections invariant violated:"]
        if missing:
            msg_parts.append(f"missing finding ids: {sorted(missing)}")
        if extra:
            msg_parts.append(f"unexpected ids (not blocking/critical findings): {sorted(extra)}")
        msg_parts.append(
            f"expected = {{f.id : f.severity in {{blocking, critical}}}} = {sorted(expected_blocking)}"
        )
        msg_parts.append(f"actual = {sorted(actual_blocking)}")
        raise GeminiOutputParseError("; ".join(msg_parts))

    # codex-rev-002 round-1 fix: goal_satisfied=true with non-empty
    # blocking_objections is incoherent — reject so a malformed gemini
    # response can't be sealed as a successful review.
    if parsed["goal_satisfied"] is True and actual_blocking:
        raise GeminiOutputParseError(
            f"goal_satisfied=true is incoherent with non-empty blocking_objections "
            f"{sorted(actual_blocking)}; a successful review cannot have blocking findings"
        )

    return parsed


_GEMINI_PROPOSAL_SCHEMA_PATH = (
    Path(__file__).parent / "dispatch_templates" / "gemini_proposal_schema.json"
)


def _parse_gemini_proposal_output(text: str, schema_path: Path | None = None) -> dict:
    """Parse + validate gemini proposal-mode output (iter-0028).

    Validates against `schema_path` (operator --schema override) when
    provided, else against the built-in `gemini_proposal_schema.json`.
    The override path is required for symmetry with the codex dispatcher
    and to honor the schema-override contract from the goal_packet
    (codex pass-3 rev-002).

    Uses `_extract_json_from_text` for JSON extraction (gemini-rev-001
    pass-2: previously hand-rolled fence stripping was brittle).

    Raises GeminiOutputParseError on shape mismatch.
    """
    try:
        cleaned = _extract_json_from_text(text)
    except ValueError as exc:
        raise GeminiOutputParseError(
            f"gemini proposal output: could not extract JSON: {exc}"
        ) from exc

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GeminiOutputParseError(
            f"gemini proposal output is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise GeminiOutputParseError(
            f"gemini proposal output root must be a JSON object; got {type(parsed).__name__}"
        )

    effective_schema_path = schema_path or _GEMINI_PROPOSAL_SCHEMA_PATH
    try:
        import jsonschema
        schema = json.loads(Path(effective_schema_path).read_text(encoding="utf-8"))
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise GeminiOutputParseError(
            f"gemini proposal output failed schema validation at "
            f"{'/'.join(str(p) for p in exc.absolute_path) or '<root>'}: {exc.message}"
        ) from exc
    except FileNotFoundError as exc:
        raise GeminiOutputParseError(
            f"proposal schema not found at {effective_schema_path}: {exc}"
        ) from exc

    # Parser-level invariants (codex pass-4 rev-002 + pass-6 rev-001):
    # defense-in-depth against operator-supplied schemas that relax these.
    if not isinstance(parsed.get("rationale_vs_alternatives"), str) or not parsed["rationale_vs_alternatives"].strip():
        raise GeminiOutputParseError(
            "rationale_vs_alternatives must be a non-empty string (parser invariant)"
        )
    if "structural_abstention" not in parsed or not isinstance(parsed["structural_abstention"], bool):
        raise GeminiOutputParseError(
            "structural_abstention must be present and boolean (parser invariant)"
        )

    if not parsed["structural_abstention"]:
        if parsed["selected_target"] is None:
            raise GeminiOutputParseError(
                "selected_target is required when structural_abstention is false"
            )
        if parsed["deliverable_scope"] is None:
            raise GeminiOutputParseError(
                "deliverable_scope is required when structural_abstention is false"
            )

    return parsed


def _invoke_gemini_with_retry(
    prompt: str,
    gemini_bin: str,
    model: str,
    timeout_seconds: int,
    repo_root: Path,
    goal_packet: dict | None = None,
    log_path=None,
    anchors=None,
    mode: str = "review",
    proposal_schema_path: Path | None = None,
) -> tuple[str, dict]:
    """Per iter-0009 verdict Q2: F2b — validator-retry on schemaless parse fail.

    Runs gemini once. If output parses cleanly, return (raw_output, parsed).
    If parse fails, re-prompt gemini ONCE with the parse error appended,
    re-parse, and return. Second parse failure raises.

    Returns (raw_output_of_last_attempt, parsed_dict). The raw output is what
    the seal records; the parsed dict feeds into _build_sealed_packet.
    """
    raw = _invoke_gemini(
        prompt=prompt,
        gemini_bin=gemini_bin,
        model=model,
        timeout_seconds=timeout_seconds,
        repo_root=repo_root,
        log_path=log_path,
        anchors=anchors,
    )
    try:
        if mode == "proposal":
            parsed = _parse_gemini_proposal_output(raw, schema_path=proposal_schema_path)
        else:
            parsed = _parse_gemini_output(raw, goal_packet=goal_packet)
        return raw, parsed
    except GeminiOutputParseError as first_err:
        # Retry once with the parse error in the prompt.
        if log_path is not None and anchors is not None:
            _log_dispatch(log_path, {
                "event": "dispatch_retry_for_parse_fail",
                **anchors,
                "first_parse_error": str(first_err)[:1000],
            })
        retry_prompt = (
            prompt
            + "\n\n# Retry — your previous response failed JSON validation\n\n"
            + f"Parse error: {first_err}\n\n"
            + "Re-emit ONLY valid JSON conforming to the schema in the prompt above. "
            + "No prose, no markdown fences, no commentary — JSON only, starting with `{` "
            + "and ending with `}`."
        )
        raw_retry = _invoke_gemini(
            prompt=retry_prompt,
            gemini_bin=gemini_bin,
            model=model,
            timeout_seconds=timeout_seconds,
            repo_root=repo_root,
            log_path=log_path,
            anchors=anchors,
        )
        if mode == "proposal":
            parsed_retry = _parse_gemini_proposal_output(raw_retry, schema_path=proposal_schema_path)
        else:
            parsed_retry = _parse_gemini_output(raw_retry, goal_packet=goal_packet)
        return raw_retry, parsed_retry


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus_mcp._dispatch_gemini",
        description="Auto-dispatch gemini CLI as a reviewer; auto-seal via T6.",
    )
    p.add_argument("--goal-packet", required=True)
    p.add_argument("--iteration-dir", required=True)
    p.add_argument("--reviewer-id", default=None)
    p.add_argument("--pass-id", default=None)
    p.add_argument("--prompt-template", default=None)
    p.add_argument("--mode", default="review", choices=["review", "proposal"],
                   help=("Dispatch mode (iter-0028 per iter-0027 converged plan). "
                         "'review' (default): use gemini_review_template.md for "
                         "code-review tasks. 'proposal': use gemini_proposal_template.md "
                         "for design-consult / workflow #4 proposal tasks. "
                         "--prompt-template override takes precedence over --mode."))
    p.add_argument("--schema", default=None,
                   help=("Optional path to a JSON schema for validating PROPOSAL-mode "
                         "output (iter-0028 codex pass-3 rev-002). Ignored in review "
                         "mode (review-mode validation is template-embedded per the "
                         "existing gemini contract). When unset in proposal mode, "
                         "defaults to dispatch_templates/gemini_proposal_schema.json."))
    p.add_argument("--gemini-bin", default="gemini")
    p.add_argument("--model", default=_DEFAULT_GEMINI_MODEL)
    p.add_argument("--timeout-seconds", type=int, default=600)
    p.add_argument("--review-target", default=None)
    p.add_argument("--smoke", action="store_true",
                   help="Smoke mode: gated by CONSENSUS_MCP_RUN_REAL_GEMINI_SMOKE=1 env var")

    ns = p.parse_args(argv)

    try:
        repo_root = _resolve_repo_root()
    except RepoRootResolutionError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "error_type": "RepoRootResolutionError"}))
        return 4

    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"
    _pre_iter_id = Path(ns.iteration_dir).name or "unknown-iteration"
    _pre_reviewer_id = ns.reviewer_id or f"gemini-{_pre_iter_id}-1"
    _pre_pass_id = ns.pass_id or f"{_pre_reviewer_id}-pass1"

    try:
        iter_dir = _normalize_relative_to_repo(ns.iteration_dir, repo_root)
        iter_dir.mkdir(parents=True, exist_ok=True)
        iteration_id = iter_dir.name

        # iter-0028: mode selects the default template when no --prompt-template
        # override is passed. Override still wins, preserving backward compat.
        _default_template_name = (
            "gemini_proposal_template.md" if ns.mode == "proposal"
            else "gemini_review_template.md"
        )
        template_path = (
            _normalize_relative_to_repo(ns.prompt_template, repo_root)
            if ns.prompt_template
            else (Path(__file__).parent / "dispatch_templates" / _default_template_name)
        )
        # iter-0028 codex pass-3 rev-002: in proposal mode, --schema (if
        # supplied) overrides the built-in gemini_proposal_schema.json. In
        # review mode the schema is template-embedded (per the historical
        # codex-rev-003 round-1 decision) so --schema is ignored there.
        proposal_schema_path = None
        if ns.mode == "proposal":
            proposal_schema_path = (
                _normalize_relative_to_repo(ns.schema, repo_root)
                if ns.schema
                else (Path(__file__).parent / "dispatch_templates" / "gemini_proposal_schema.json")
            )
        # codex-rev-003 round-1 fix: schema is part of the prompt template
        # (embedded under "Schema reference" header), NOT a separate file the
        # helper passes to gemini. The prior `--schema` flag computed
        # schema_sha256 but never embedded the file's content in the prompt,
        # so operators thought it mattered when it didn't. Removed entirely;
        # the schema contract lives in dispatch_templates/gemini_review_template.md.
        goal_packet_path = _normalize_relative_to_repo(ns.goal_packet, repo_root)
        review_target_normalized = _normalize_relative_to_repo(ns.review_target, repo_root)
    except (OutsideRepoPathError, OSError) as exc:
        error_type = type(exc).__name__
        _log_dispatch(log_path, {
            "event": "dispatch_failed",
            "error_type": error_type,
            "error": str(exc),
            "reviewer_id": _pre_reviewer_id,
            "pass_id": _pre_pass_id,
            "iteration_id": _pre_iter_id,
            "timeout_seconds": ns.timeout_seconds,
        })
        print(json.dumps({"ok": False, "error": str(exc), "error_type": error_type}))
        return 5

    review_target_path_str: str | None = None
    if review_target_normalized is not None:
        try:
            review_target_path_str = str(
                review_target_normalized.relative_to(repo_root.resolve())
            ).replace("\\", "/")
        except ValueError:
            review_target_path_str = str(review_target_normalized)

    reviewer_id = ns.reviewer_id or f"gemini-{iteration_id}-1"
    pass_id = ns.pass_id or f"{reviewer_id}-pass1"

    if ns.smoke and os.environ.get("CONSENSUS_MCP_RUN_REAL_GEMINI_SMOKE") != "1":
        refuse_msg = (
            "--smoke requires CONSENSUS_MCP_RUN_REAL_GEMINI_SMOKE=1 in the environment. "
            "This gate prevents accidental real-gemini invocation under cost/auth/session "
            "side effects. Set the env var explicitly to opt in, or drop --smoke for "
            "normal (non-smoke) usage."
        )
        _log_dispatch(log_path, {
            "event": "dispatch_refused",
            "error_type": "smoke_env_gate",
            "error": refuse_msg,
            "reviewer_id": reviewer_id,
            "pass_id": pass_id,
        })
        print(json.dumps({"ok": False, "error": refuse_msg, "error_type": "smoke_env_gate"}))
        return 3

    _log_dispatch(log_path, {
        "event": "dispatch_start",
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "smoke": ns.smoke,
        "timeout_seconds": ns.timeout_seconds,
        "gemini_bin": ns.gemini_bin,
        "model": ns.model,
        # schema_path removed per codex-rev-003 round-1: was misleading provenance.
        "review_target_path": review_target_path_str,
        "adapter": "gemini",
    })

    prompt_sha: str | None = None
    goal_packet_sha: str | None = None
    scope_sig: str | None = None
    gemini_version: str | None = None
    output_sha: str | None = None
    review_target_hash: str | None = None

    def _failed_event(error_type: str, error: str) -> dict:
        ev = {
            "event": "dispatch_failed",
            "error_type": error_type,
            "error": error,
            "reviewer_id": reviewer_id,
            "pass_id": pass_id,
            "iteration_id": iteration_id,
            "timeout_seconds": ns.timeout_seconds,
            "adapter": "gemini",
        }
        for k, v in (
            ("gemini_version", gemini_version),
            ("model", ns.model),
            ("prompt_sha256", prompt_sha),
            ("output_sha256", output_sha),
            ("goal_packet_sha256", goal_packet_sha),
            ("scope_signature", scope_sig),
            ("review_target_path", review_target_path_str),
            ("review_target_hash", review_target_hash),
        ):
            if v is not None:
                ev[k] = v
        return ev

    try:
        goal_packet_text = goal_packet_path.read_text(encoding="utf-8")
        goal_packet = _load_goal_packet(goal_packet_path)
        template_text = _load_template(template_path)

        review_packet_data: dict | None = None
        if review_target_normalized is not None:
            review_target_text = review_target_normalized.read_text(encoding="utf-8")
            review_target_hash = _sha256_str(review_target_text)
            if review_target_normalized.suffix.lower() in (".yaml", ".yml"):
                try:
                    candidate = yaml.safe_load(review_target_text)
                    if isinstance(candidate, dict):
                        review_packet_data = candidate
                except yaml.YAMLError:
                    review_packet_data = None

        prompt = _build_prompt(
            goal_packet,
            template_text,
            iteration_dir=str(iter_dir),
            review_packet_path=str(goal_packet_path),
            review_target_path=str(review_target_normalized) if review_target_normalized else None,
            review_target_hash=review_target_hash,
            review_packet=review_packet_data,
        )
        prompt_sha = _sha256_str(prompt)
        goal_packet_sha = _sha256_str(goal_packet_text)
        scope_sig = ((goal_packet or {}).get("authorization", {}) or {}).get("scope_signature", "")

        gemini_version = _get_gemini_version(ns.gemini_bin)
        raw_output, extracted = _invoke_gemini_with_retry(
            prompt=prompt,
            gemini_bin=ns.gemini_bin,
            model=ns.model,
            timeout_seconds=ns.timeout_seconds,
            repo_root=repo_root,
            goal_packet=goal_packet,
            log_path=log_path,
            anchors={
                "iteration_id": iteration_id,
                "reviewer_id": reviewer_id,
                "pass_id": pass_id,
            },
            mode=ns.mode,  # iter-0028
            proposal_schema_path=proposal_schema_path,  # iter-0028 codex pass-3 rev-002
        )
        output_sha = _sha256_str(raw_output)

        # iter-0028 codex pass-4 rev-001: in proposal mode, include the
        # effective proposal_schema_path + its sha256 in provenance so the
        # sealed packet records which schema constrained the output.
        # Auditors can distinguish built-in vs overridden schema without
        # consulting external state.
        proposal_schema_sha = None
        proposal_schema_path_str = None
        if ns.mode == "proposal" and proposal_schema_path is not None:
            proposal_schema_path_str = str(proposal_schema_path)
            try:
                proposal_schema_sha = _sha256_str(
                    Path(proposal_schema_path).read_text(encoding="utf-8")
                )
            except FileNotFoundError:
                proposal_schema_sha = None

        provenance = {
            "gemini_version": gemini_version,
            "model": ns.model,
            "prompt_sha256": prompt_sha,
            "output_sha256": output_sha,
            "goal_packet_sha256": goal_packet_sha,
            "scope_signature": scope_sig,
            "review_target_path": review_target_path_str,
            "review_target_hash": review_target_hash,
            "adapter": "gemini",
            "mode": ns.mode,
            "proposal_schema_path": proposal_schema_path_str,
            "proposal_schema_sha256": proposal_schema_sha,
        }
        packet = _build_sealed_packet(
            extracted, iteration_id, reviewer_id, pass_id,
            provenance=provenance,
            attestation_method="auto_gemini_dispatch",
            attestation_input_sources=[
                "goal_packet (path passed via --goal-packet)",
                "prompt_template (substituted by _build_prompt)",
                "review_target (path passed via --review-target; may be unspecified)",
                "model (gemini CLI --model flag; no --output-schema enforcement)",
            ],
        )
        result = _seal_via_t6(packet, iter_dir, sealed_filename="gemini-review.yaml")
    except (GeminiInvocationError, GeminiOutputParseError) as exc:
        _log_dispatch(log_path, _failed_event(type(exc).__name__, str(exc)))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 1
    except Exception as exc:
        _log_dispatch(log_path, _failed_event(type(exc).__name__, str(exc)))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 2

    _log_dispatch(log_path, {
        "event": "dispatch_done",
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "gemini_version": gemini_version,
        "model": ns.model,
        "timeout_seconds": ns.timeout_seconds,
        "exit_code": 0,
        "prompt_sha256": prompt_sha,
        "output_sha256": output_sha,
        "goal_packet_sha256": goal_packet_sha,
        "scope_signature": scope_sig,
        "review_target_path": review_target_path_str,
        "review_target_hash": review_target_hash,
        "packet_sha256": result.get("packet_sha256"),
        "archive_sealed_path": result.get("archive_sealed_path"),
        "local_mirror_path": result.get("sealed_path"),
        "sealed_path": result.get("sealed_path"),
        "t6_audit_event_id": result.get("audit_event_id"),
        "adapter": "gemini",
    })
    print(json.dumps({"ok": True, "pass_id": pass_id, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
