"""v1.31.0 — auto-grok-dispatch helper (grok adapter).

Gemini-twin (converged consult iteration-v131-grok-design-2026-05-26).
Reuses generic dispatch infrastructure from `_dispatch_base.py`; supplies
grok-specific code: CLI invocation shape, binary resolution, output JSON
parsing with validator-retry on schemaless parse fail.

Key differences from `_dispatch_gemini.py` (by converged decision):
  - Prompt is passed inline via `-p <content>` (iter-0045 operator
    decision; aligns with dispatch-canon-validator.GROK_FORBIDDEN_FLAGS).
    A per-pass copy is still written to iter_dir for audit/provenance
    (prompt_sha256), but grok itself reads the inline argument.
  - Auth pre-flight: probe for ~/.grok/auth.json. Missing → raise
    `GrokAuthRequiredError` BEFORE invoking the CLI (codex acceptance gate G4).
  - Independence flags: `--no-memory --disable-web-search` + `--cwd /tmp`
    — the minimal verified-working safeguard set. Prior shape
    (`--prompt-file` + `--no-plan` + `--no-subagents` + `--max-turns` +
    `--permission-mode` + project-subdir `--cwd`) caused indefinite
    stalls on real packets.
  - No sandbox / no integrity check — by converged decision (YAGNI; kimi's
    hardening earned complexity from a real field bug, grok has no such
    history). The disable-flag set IS the day-one safeguard.

Scope (v1.31.0): REVIEW + PROPOSAL modes via --mode {review,proposal}.
Grok does NOT author patch_proposal blocks (same scope as gemini).

USAGE
-----
  python -m consensus_mcp._dispatch_grok \\
      --goal-packet <path/to/goal_packet.yaml> \\
      --iteration-dir <path/to/iteration-XXXX/> \\
      [--reviewer-id grok-iterXXXX-N] \\
      [--pass-id grok-iterXXXX-N-passN] \\
      [--prompt-template <path>] \\
      [--mode {review,proposal}] \\
      [--schema <path>]                    # proposal-mode only \\
      [--grok-bin <path>] \\
      [--model <model_id>] \\
      [--timeout-seconds 600] \\
      [--review-target <path>] \\
      [--smoke]

Review-mode output schema: embedded in
dispatch_templates/grok_review_template.md.

Proposal-mode output schema: dispatch_templates/grok_proposal_schema.json
by default; --schema overrides.

Exit 0 = sealed pass produced; non-zero = failure (grok error, parse fail,
seal fail, auth missing). JSON to stdout on success.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

from consensus_mcp._dispatch_base import (
    RepoRootResolutionError,
    _resolve_repo_root,
    OutsideRepoPathError,
    _normalize_relative_to_repo,
    _load_goal_packet,
    _load_template,
    _build_prompt,
    _terminate_process_tree,
    _sha256_str,
    _build_sealed_packet,
    _seal_via_t6,
    _log_dispatch,
)


# Adapter-specific finding patterns.
_GROK_FINDING_ID_PATTERN = re.compile(r"^grok-rev-\d+$")
_VALID_SEVERITIES = {"low", "medium", "high", "blocking", "critical"}
_REQUIRED_FINDING_FIELDS = ("id", "severity", "summary", "citation", "risk", "recommendation")
_ALLOWED_FINDING_KEYS = set(_REQUIRED_FINDING_FIELDS) | {"patch_proposal", "patch_not_proposed_reason"}
_ALLOWED_TOP_LEVEL_KEYS = {"findings", "goal_satisfied", "blocking_objections", "goal_satisfied_rationale"}
_BLOCKING_SEVERITIES = {"blocking", "critical"}

# Grok's CLI default model resolves to whatever `grok` itself picks; we
# pass --model only when the operator overrides via --model on this
# dispatcher (per converged plan D3 — let grok roll forward without
# dispatcher releases). `None` here means "do not pass --model".
_DEFAULT_GROK_MODEL: str | None = None

# Independence flag set passed on every invocation. Codified to the
# operator-verified 2026-05-27 working shape (iter-0045 panel: codex
# high finding + kimi finding; operator decision: adopt inline -p +
# minimal flags). The prior v1.31.1 hot-patch flag set (--prompt-file +
# --no-plan + --no-subagents + --max-turns N + --permission-mode +
# project-subdir --cwd) caused indefinite stalls on real packets —
# grok counts every prompt chunk + MCP rejection + tool-call attempt
# against the message budget and never reaches model output. The
# minimal shape (inline -p + --no-memory + --disable-web-search +
# --cwd /tmp) returns answers on real workloads. This flag set
# satisfies dispatch-canon-validator.GROK_FORBIDDEN_FLAGS (the
# validator forbids --prompt-file, --max-turns, --no-plan,
# --no-subagents, --permission-mode for direct grok invocations).
_GROK_DISABLED_TOOLS = (
    "--no-memory",
    "--disable-web-search",
)


class GrokAuthRequiredError(RuntimeError):
    """Raised when ~/.grok/auth.json is missing (the auth pre-flight)."""


class GrokInvocationError(RuntimeError):
    """Raised when the grok CLI exits non-zero, times out, or is not found."""


class GrokOutputParseError(ValueError):
    """Raised when grok output is not parseable JSON or lacks the expected shape."""


def _check_grok_auth() -> None:
    """Pre-flight: confirm ~/.grok/auth.json exists.

    Per converged plan D4: existence-check only, no token-expiry probe.
    The grok CLI handles its own refresh + reauth flow when the token
    expires; surfacing the underlying CLI error is cleaner than trying to
    second-guess the OAuth state from outside. Mirrors codex's auth model.
    """
    auth_path = Path.home() / ".grok" / "auth.json"
    if not auth_path.exists():
        raise GrokAuthRequiredError(
            f"no grok authentication found at {auth_path}. "
            f"Run `grok login` to authenticate, then re-run."
        )


def _resolve_grok_bin(grok_bin: str) -> str:
    """Resolve a grok binary spec to an actual executable file path.

    Grok is a Rust binary (single-file native exe), not an npm package, so
    we skip the Windows App-Execution-Alias / .cmd-vs-.ps1 dance the gemini
    dispatcher needed. Plain `shutil.which` is sufficient.
    """
    if os.path.sep in grok_bin or (len(grok_bin) > 1 and grok_bin[1] == ":"):
        return grok_bin
    resolved = shutil.which(grok_bin)
    return resolved if resolved is not None else grok_bin


def _get_grok_version(grok_bin: str) -> str:
    """Best-effort: shell out to `<grok> --version` and return the string.

    Returns 'unknown' on any failure. Used for audit-log provenance only.
    Logging grok_version is a converged-plan acceptance gate (G6) +
    risk-R1 mitigation.
    """
    try:
        result = subprocess.run(
            [_resolve_grok_bin(grok_bin), "--version"],
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


def _write_per_pass_prompt(prompt: str, iter_dir: Path, pass_id: str) -> Path:
    """Write the prompt to a PER-PASS file inside iter_dir and return path.

    Codex D3 refinement: per-pass filename (embeds pass_id) avoids any
    collision risk across concurrent dispatches. Content sha256 is logged
    in provenance.prompt_sha256 (computed by caller; this helper just
    writes the file).
    """
    # Sanitize pass_id for use as a filename — alphanumerics + dash/underscore
    # only. The full pass_id format is already filesystem-safe in practice
    # (claude/codex/gemini/kimi pass IDs use `<adapter>-<iteration>-<n>-passN`)
    # but defense-in-depth against an operator-passed weird pass_id.
    safe_pass_id = re.sub(r"[^A-Za-z0-9._-]+", "_", pass_id)
    prompt_path = iter_dir / f"grok-prompt-{safe_pass_id}.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def _build_grok_cmd(
    grok_bin: str,
    prompt: str,
    model: str | None,
) -> list[str]:
    """Construct the grok CLI command list for a dispatch.

    Operator-codified iter-0045 working shape: inline `-p <prompt>` +
    `--no-memory` + `--disable-web-search` + `--cwd /tmp`. The `/tmp`
    cwd prevents grok from scanning the project dir (the multi-message-
    budget hang source); the operator-confirmed assumption is that /tmp
    exists and is writable (standard on Linux; not guaranteed in highly
    restricted containers — surface as ARG_MAX/exec error if missing).

    Note: inline `-p` carries a Linux MAX_ARG_STRLEN limit of 128KB per
    argument. If a packet ever exceeds that, the CLI will fail loudly
    with E2BIG; the caller should chunk or summarize the prompt.
    """
    cmd = [
        _resolve_grok_bin(grok_bin),
        "-p", prompt,
        "--output-format", "plain",
    ]
    cmd.extend(_GROK_DISABLED_TOOLS)
    cmd.extend(["--cwd", "/tmp"])
    if model:
        cmd.extend(["--model", model])
    return cmd


def _invoke_grok(
    prompt: str,
    grok_bin: str,
    model: str | None,
    timeout_seconds: int,
    iter_dir: Path,
    pass_id: str,
    repo_root: Path,
    log_path=None,
    anchors=None,
    heartbeat_interval: float = 30.0,
    stall_silence_seconds: float = 180.0,
    poll_interval: float = 0.5,
    time_fn=None,
    popen_factory=None,
) -> tuple[str, Path]:
    """Shell out to grok CLI via Popen + reader threads.

    Returns (raw_stdout, prompt_path). The prompt_path is the per-pass file
    we wrote (caller reads it for sha256 provenance).

    Pattern mirrors _invoke_gemini's stream-and-watchdog loop but simpler:
    grok reads the prompt inline via `-p` (no stdin piping), so no
    stdin-writer thread + no codex-rev-001 deadlock dance.
    """
    if time_fn is None:
        time_fn = time.time
    if popen_factory is None:
        popen_factory = subprocess.Popen
    can_log = log_path is not None and anchors is not None

    # Pre-flight auth check BEFORE writing the prompt file (no point creating
    # an artifact if we're going to error out immediately).
    _check_grok_auth()

    # Per-pass prompt file is written to iter_dir for audit/provenance
    # (prompt_sha256 in dispatch_log + dispatch_provenance), but grok
    # itself receives the prompt inline via `-p` (iter-0045 shape).
    prompt_path = _write_per_pass_prompt(prompt, iter_dir, pass_id)
    cmd = _build_grok_cmd(grok_bin, prompt, model)

    if sys.platform == "win32":
        popen_kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        popen_kwargs = {"start_new_session": True}
    try:
        proc = popen_factory(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=str(repo_root),
            **popen_kwargs,
        )
    except FileNotFoundError:
        raise GrokInvocationError(f"grok binary not found: {grok_bin}") from None

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

    t_stdout = threading.Thread(target=stdout_reader, daemon=True, name="grok-stdout-reader")
    t_stderr = threading.Thread(target=stderr_reader, daemon=True, name="grok-stderr-reader")
    t_stdout.start()
    t_stderr.start()

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
            raise GrokInvocationError(f"dispatch aborted by operator: {abort_reason}")

        with state_lock:
            lst = last_streamed_ts[0]
            seq_snap = streamed_seq[0]
        if lst is not None:
            silence_age = now - lst
            silence_trigger_threshold = stall_silence_seconds
        else:
            silence_age = now - start_ts
            silence_trigger_threshold = float(timeout_seconds)

        if silence_age >= silence_trigger_threshold:
            _terminate_process_tree(proc)
            if can_log:
                _log_dispatch(log_path, {
                    "event": "dispatch_aborted",
                    **anchors,
                    "abort_source": "watchdog_silence",
                    "abort_reason": f"no grok stdout for {silence_age:.0f}s (threshold {silence_trigger_threshold:.0f}s)",
                    "age_seconds": now - start_ts,
                    "last_streamed_line_age_seconds": silence_age if lst is not None else None,
                })
            raise GrokInvocationError(
                f"grok stuck: no output for {silence_age:.0f}s"
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
            raise GrokInvocationError(
                f"grok exceeded {timeout_seconds}s wall timeout + {stall_silence_seconds}s grace"
            )

        time.sleep(poll_interval)

    t_stdout.join(timeout=5)
    t_stderr.join(timeout=5)
    if t_stdout.is_alive() or t_stderr.is_alive():
        raise GrokInvocationError(
            "grok exited but reader thread did not drain within 5s; "
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
        raise GrokInvocationError(
            f"grok exit={proc.returncode}; stderr_tail={stderr_tail!r}{stdout_hint}"
        )

    return stdout_bytes.decode("utf-8", errors="replace"), prompt_path


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_from_text(text: str) -> str:
    """Extract a JSON object substring from grok's free-form output.

    Like gemini, grok lacks codex's --output-schema enforcement, so its
    response may include leading prose, markdown fences, or trailing
    commentary even when explicitly told to emit JSON only. Mirrors
    gemini's extractor:
      1. Whole-string trim — if the trimmed text starts with `{` and ends
         with `}`, return as-is.
      2. Fenced-code-block extraction — first ```json ... ``` (or bare
         ``` ... ```) containing a `{...}` block.
      3. Greedy outermost-brace match — first `{` to last `}`.
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


def _parse_grok_output(text: str, goal_packet: dict | None = None) -> dict:
    """Parse grok's JSON response + locally validate shape.

    Rules mirror gemini's parser (review-mode):
      - root is a JSON object
      - top-level keys exactly {findings, goal_satisfied, blocking_objections,
        goal_satisfied_rationale}
      - findings is a list; each finding has all 6 required fields
      - id matches ^grok-rev-\\d+$
      - severity in the canonical enum
      - patch_proposal/patch_not_proposed_reason MUST be null in v1.31.0
        (grok is review-only, same scope as gemini/kimi)
      - blocking_objections invariant: set equals blocking/critical finding IDs
      - goal_satisfied is bool; goal_satisfied_rationale is non-empty string

    Raises GrokOutputParseError on any violation.
    """
    candidate = _extract_json_from_text(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise GrokOutputParseError(
            f"grok output is not valid JSON: {exc}; first 500 chars of raw: {text[:500]!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise GrokOutputParseError(
            f"grok output JSON root must be an object, got {type(parsed).__name__}"
        )

    unknown_top = set(parsed.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_top:
        raise GrokOutputParseError(
            f"grok output JSON has unexpected top-level keys: {sorted(unknown_top)}"
        )

    for required in ("findings", "goal_satisfied", "blocking_objections", "goal_satisfied_rationale"):
        if required not in parsed:
            raise GrokOutputParseError(f"grok output JSON missing required key: {required!r}")

    if not isinstance(parsed["findings"], list):
        raise GrokOutputParseError("'findings' must be an array")
    if not isinstance(parsed["goal_satisfied"], bool):
        raise GrokOutputParseError(
            f"'goal_satisfied' must be boolean, got {type(parsed['goal_satisfied']).__name__}"
        )
    if not isinstance(parsed["blocking_objections"], list):
        raise GrokOutputParseError(
            f"'blocking_objections' must be an array, got {type(parsed['blocking_objections']).__name__}"
        )
    if not isinstance(parsed["goal_satisfied_rationale"], str):
        raise GrokOutputParseError(
            f"'goal_satisfied_rationale' must be string, got "
            f"{type(parsed['goal_satisfied_rationale']).__name__}"
        )
    if not parsed["goal_satisfied_rationale"].strip():
        raise GrokOutputParseError(
            "'goal_satisfied_rationale' must be a non-empty string (prompt contract)"
        )

    for i, item in enumerate(parsed["blocking_objections"]):
        if not isinstance(item, str):
            raise GrokOutputParseError(
                f"blocking_objections[{i}] must be string, got {type(item).__name__}"
            )

    blocking_finding_ids = []
    for i, finding in enumerate(parsed["findings"]):
        if not isinstance(finding, dict):
            raise GrokOutputParseError(
                f"findings[{i}] must be an object, got {type(finding).__name__}"
            )
        unknown_keys = set(finding.keys()) - _ALLOWED_FINDING_KEYS
        if unknown_keys:
            raise GrokOutputParseError(
                f"findings[{i}] has unexpected keys: {sorted(unknown_keys)}"
            )
        for required in _REQUIRED_FINDING_FIELDS:
            if required not in finding:
                raise GrokOutputParseError(
                    f"findings[{i}] missing required field: {required!r}"
                )
        for str_field in ("id", "severity", "summary", "citation", "risk", "recommendation"):
            if not isinstance(finding[str_field], str):
                raise GrokOutputParseError(
                    f"findings[{i}].{str_field} must be string, got "
                    f"{type(finding[str_field]).__name__}"
                )
        if not finding["summary"].strip():
            raise GrokOutputParseError(
                f"findings[{i}].summary must be a non-empty string (schema contract)"
            )
        if finding["severity"] not in _VALID_SEVERITIES:
            raise GrokOutputParseError(
                f"findings[{i}] has invalid severity {finding['severity']!r}; "
                f"must be one of {sorted(_VALID_SEVERITIES)}"
            )
        if not _GROK_FINDING_ID_PATTERN.match(finding["id"]):
            raise GrokOutputParseError(
                f"findings[{i}] id {finding['id']!r} does not match pattern "
                f"^grok-rev-\\d+$"
            )
        pp = finding.get("patch_proposal")
        if pp is not None:
            raise GrokOutputParseError(
                f"findings[{i}].patch_proposal must be null in v1.31.0 — grok is "
                f"review-only (patch authoring deferred)"
            )
        reason = finding.get("patch_not_proposed_reason")
        if reason is not None and not isinstance(reason, str):
            raise GrokOutputParseError(
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
        raise GrokOutputParseError("; ".join(msg_parts))

    if parsed["goal_satisfied"] is True and actual_blocking:
        raise GrokOutputParseError(
            f"goal_satisfied=true is incoherent with non-empty blocking_objections "
            f"{sorted(actual_blocking)}; a successful review cannot have blocking findings"
        )

    return parsed


_GROK_PROPOSAL_SCHEMA_PATH = (
    Path(__file__).parent / "dispatch_templates" / "grok_proposal_schema.json"
)


def _parse_grok_proposal_output(text: str, schema_path: Path | None = None) -> dict:
    """Parse + validate grok proposal-mode output (v1.31.0).

    Validates against `schema_path` (operator --schema override) when
    provided, else against the built-in `grok_proposal_schema.json`.
    """
    try:
        cleaned = _extract_json_from_text(text)
    except ValueError as exc:
        raise GrokOutputParseError(
            f"grok proposal output: could not extract JSON: {exc}"
        ) from exc

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GrokOutputParseError(
            f"grok proposal output is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise GrokOutputParseError(
            f"grok proposal output root must be a JSON object; got {type(parsed).__name__}"
        )

    effective_schema_path = schema_path or _GROK_PROPOSAL_SCHEMA_PATH
    try:
        import jsonschema
    except ImportError as exc:
        raise GrokOutputParseError(
            f"jsonschema package required for proposal-mode validation; "
            f"install with `pip install jsonschema` or reinstall consensus-mcp: {exc}"
        ) from exc
    try:
        schema = json.loads(Path(effective_schema_path).read_text(encoding="utf-8"))
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise GrokOutputParseError(
            f"grok proposal output failed schema validation at "
            f"{'/'.join(str(p) for p in exc.absolute_path) or '<root>'}: {exc.message}"
        ) from exc
    except FileNotFoundError as exc:
        raise GrokOutputParseError(
            f"proposal schema not found at {effective_schema_path}: {exc}"
        ) from exc

    if not isinstance(parsed.get("rationale_vs_alternatives"), str) or not parsed["rationale_vs_alternatives"].strip():
        raise GrokOutputParseError(
            "rationale_vs_alternatives must be a non-empty string (parser invariant)"
        )
    if "structural_abstention" not in parsed or not isinstance(parsed["structural_abstention"], bool):
        raise GrokOutputParseError(
            "structural_abstention must be present and boolean (parser invariant)"
        )

    if not parsed["structural_abstention"]:
        if parsed["selected_target"] is None:
            raise GrokOutputParseError(
                "selected_target is required when structural_abstention is false"
            )
        if parsed["deliverable_scope"] is None:
            raise GrokOutputParseError(
                "deliverable_scope is required when structural_abstention is false"
            )

    return parsed


def _invoke_grok_with_retry(
    prompt: str,
    grok_bin: str,
    model: str | None,
    timeout_seconds: int,
    iter_dir: Path,
    pass_id: str,
    repo_root: Path,
    goal_packet: dict | None = None,
    log_path=None,
    anchors=None,
    mode: str = "review",
    proposal_schema_path: Path | None = None,
) -> tuple[str, dict, Path]:
    """Validator-retry on parse fail. Mirrors gemini's pattern.

    Returns (raw_output, parsed_dict, prompt_path).
    """
    raw, prompt_path = _invoke_grok(
        prompt=prompt,
        grok_bin=grok_bin,
        model=model,
        timeout_seconds=timeout_seconds,
        iter_dir=iter_dir,
        pass_id=pass_id,
        repo_root=repo_root,
        log_path=log_path,
        anchors=anchors,
    )
    try:
        if mode == "proposal":
            parsed = _parse_grok_proposal_output(raw, schema_path=proposal_schema_path)
        else:
            parsed = _parse_grok_output(raw, goal_packet=goal_packet)
        return raw, parsed, prompt_path
    except GrokOutputParseError as first_err:
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
        retry_pass_id = f"{pass_id}-retry"
        raw_retry, prompt_path_retry = _invoke_grok(
            prompt=retry_prompt,
            grok_bin=grok_bin,
            model=model,
            timeout_seconds=timeout_seconds,
            iter_dir=iter_dir,
            pass_id=retry_pass_id,
            repo_root=repo_root,
            log_path=log_path,
            anchors=anchors,
        )
        if mode == "proposal":
            parsed_retry = _parse_grok_proposal_output(raw_retry, schema_path=proposal_schema_path)
        else:
            parsed_retry = _parse_grok_output(raw_retry, goal_packet=goal_packet)
        return raw_retry, parsed_retry, prompt_path_retry


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus_mcp._dispatch_grok",
        description="Auto-dispatch grok CLI as a reviewer; auto-seal via T6.",
    )
    p.add_argument("--goal-packet", required=True)
    p.add_argument("--iteration-dir", required=True)
    p.add_argument("--reviewer-id", default=None)
    p.add_argument("--pass-id", default=None)
    p.add_argument("--prompt-template", default=None)
    p.add_argument("--mode", default="review", choices=["review", "proposal"],
                   help=("Dispatch mode. 'review' (default): grok_review_template.md. "
                         "'proposal': grok_proposal_template.md for Workflow A "
                         "propose-converge tasks. --prompt-template overrides."))
    p.add_argument("--schema", default=None,
                   help=("Optional path to a JSON schema for validating PROPOSAL-mode "
                         "output. Ignored in review mode (review-mode validation is "
                         "template-embedded). Defaults to "
                         "dispatch_templates/grok_proposal_schema.json."))
    p.add_argument("--grok-bin", default="grok")
    p.add_argument("--model", default=_DEFAULT_GROK_MODEL,
                   help=("Optional model id passed via grok --model. When unset, grok "
                         "uses its own default — letting it roll forward without "
                         "dispatcher releases."))
    p.add_argument("--timeout-seconds", type=int, default=600)
    p.add_argument("--review-target", default=None)
    p.add_argument("--smoke", action="store_true",
                   help="Smoke mode: gated by CONSENSUS_MCP_RUN_REAL_GROK_SMOKE=1 env var")

    ns = p.parse_args(argv)

    try:
        repo_root = _resolve_repo_root()
    except RepoRootResolutionError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "error_type": "RepoRootResolutionError"}))
        return 4

    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"
    _pre_iter_id = Path(ns.iteration_dir).name or "unknown-iteration"
    _pre_reviewer_id = ns.reviewer_id or f"grok-{_pre_iter_id}-1"
    _pre_pass_id = ns.pass_id or f"{_pre_reviewer_id}-pass1"

    try:
        iter_dir = _normalize_relative_to_repo(ns.iteration_dir, repo_root)
        iter_dir.mkdir(parents=True, exist_ok=True)
        iteration_id = iter_dir.name

        _default_template_name = (
            "grok_proposal_template.md" if ns.mode == "proposal"
            else "grok_review_template.md"
        )
        template_path = (
            _normalize_relative_to_repo(ns.prompt_template, repo_root)
            if ns.prompt_template
            else (Path(__file__).parent / "dispatch_templates" / _default_template_name)
        )
        proposal_schema_path = None
        if ns.mode == "proposal":
            proposal_schema_path = (
                _normalize_relative_to_repo(ns.schema, repo_root)
                if ns.schema
                else (Path(__file__).parent / "dispatch_templates" / "grok_proposal_schema.json")
            )
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

    reviewer_id = ns.reviewer_id or f"grok-{iteration_id}-1"
    pass_id = ns.pass_id or f"{reviewer_id}-pass1"

    if ns.smoke and os.environ.get("CONSENSUS_MCP_RUN_REAL_GROK_SMOKE") != "1":
        refuse_msg = (
            "--smoke requires CONSENSUS_MCP_RUN_REAL_GROK_SMOKE=1 in the environment. "
            "This gate prevents accidental real-grok invocation under cost/auth/session "
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
        "grok_bin": ns.grok_bin,
        "model": ns.model,
        "review_target_path": review_target_path_str,
        "adapter": "grok",
        "disabled_tools": list(_GROK_DISABLED_TOOLS),
    })

    prompt_sha: str | None = None
    goal_packet_sha: str | None = None
    scope_sig: str | None = None
    grok_version: str | None = None
    output_sha: str | None = None
    review_target_hash: str | None = None
    prompt_file_path_str: str | None = None

    def _failed_event(error_type: str, error: str) -> dict:
        ev = {
            "event": "dispatch_failed",
            "error_type": error_type,
            "error": error,
            "reviewer_id": reviewer_id,
            "pass_id": pass_id,
            "iteration_id": iteration_id,
            "timeout_seconds": ns.timeout_seconds,
            "adapter": "grok",
            "disabled_tools": list(_GROK_DISABLED_TOOLS),
        }
        for k, v in (
            ("grok_version", grok_version),
            ("model", ns.model),
            ("prompt_sha256", prompt_sha),
            ("output_sha256", output_sha),
            ("goal_packet_sha256", goal_packet_sha),
            ("scope_signature", scope_sig),
            ("review_target_path", review_target_path_str),
            ("review_target_hash", review_target_hash),
            ("prompt_file_path", prompt_file_path_str),
        ):
            if v is not None:
                ev[k] = v
        return ev

    try:
        goal_packet_text = goal_packet_path.read_text(encoding="utf-8")
        goal_packet = _load_goal_packet(goal_packet_path)
        template_text = _load_template(template_path)

        review_packet_data: dict | None = None
        review_target_text: str | None = None
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
            review_target_content=review_target_text,
        )
        prompt_sha = _sha256_str(prompt)
        goal_packet_sha = _sha256_str(goal_packet_text)
        scope_sig = ((goal_packet or {}).get("authorization", {}) or {}).get("scope_signature", "")

        grok_version = _get_grok_version(ns.grok_bin)
        raw_output, extracted, prompt_file_path = _invoke_grok_with_retry(
            prompt=prompt,
            grok_bin=ns.grok_bin,
            model=ns.model,
            timeout_seconds=ns.timeout_seconds,
            iter_dir=iter_dir,
            pass_id=pass_id,
            repo_root=repo_root,
            goal_packet=goal_packet,
            log_path=log_path,
            anchors={
                "iteration_id": iteration_id,
                "reviewer_id": reviewer_id,
                "pass_id": pass_id,
            },
            mode=ns.mode,
            proposal_schema_path=proposal_schema_path,
        )
        output_sha = _sha256_str(raw_output)
        try:
            prompt_file_path_str = str(prompt_file_path.relative_to(repo_root.resolve())).replace("\\", "/")
        except ValueError:
            prompt_file_path_str = str(prompt_file_path)

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
            "grok_version": grok_version,
            "model": ns.model,
            "prompt_sha256": prompt_sha,
            "output_sha256": output_sha,
            "goal_packet_sha256": goal_packet_sha,
            "scope_signature": scope_sig,
            "review_target_path": review_target_path_str,
            "review_target_hash": review_target_hash,
            "adapter": "grok",
            "mode": ns.mode,
            "proposal_schema_path": proposal_schema_path_str,
            "proposal_schema_sha256": proposal_schema_sha,
            "prompt_file_path": prompt_file_path_str,
            "disabled_tools": list(_GROK_DISABLED_TOOLS),
        }
        packet = _build_sealed_packet(
            extracted, iteration_id, reviewer_id, pass_id,
            provenance=provenance,
            attestation_method="auto_grok_dispatch",
            attestation_input_sources=[
                "goal_packet (path passed via --goal-packet)",
                "prompt_template (substituted by _build_prompt)",
                "review_target (path passed via --review-target; may be unspecified)",
                "model (grok CLI --model flag; no --output-schema enforcement)",
            ],
        )
        result = _seal_via_t6(packet, iter_dir, sealed_filename="grok-review.yaml")
    except GrokAuthRequiredError as exc:
        _log_dispatch(log_path, _failed_event(type(exc).__name__, str(exc)))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 6
    except (GrokInvocationError, GrokOutputParseError) as exc:
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
        "grok_version": grok_version,
        "model": ns.model,
        "timeout_seconds": ns.timeout_seconds,
        "exit_code": 0,
        "prompt_sha256": prompt_sha,
        "output_sha256": output_sha,
        "goal_packet_sha256": goal_packet_sha,
        "scope_signature": scope_sig,
        "review_target_path": review_target_path_str,
        "review_target_hash": review_target_hash,
        "prompt_file_path": prompt_file_path_str,
        "disabled_tools": list(_GROK_DISABLED_TOOLS),
        "packet_sha256": result.get("packet_sha256"),
        "archive_sealed_path": result.get("archive_sealed_path"),
        "local_mirror_path": result.get("sealed_path"),
        "sealed_path": result.get("sealed_path"),
        "t6_audit_event_id": result.get("audit_event_id"),
        "adapter": "grok",
    })
    print(json.dumps({"ok": True, "pass_id": pass_id, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
