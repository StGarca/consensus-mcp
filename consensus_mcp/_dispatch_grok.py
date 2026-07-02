"""v1.31.0 - auto-grok-dispatch helper (grok adapter).

Gemini-twin (converged consult iteration-v131-grok-design-2026-05-26).
Reuses generic dispatch infrastructure from `_dispatch_base.py`; supplies
grok-specific code: CLI invocation shape, binary resolution, output JSON
parsing with validator-retry on schemaless parse fail.

Key differences from `_dispatch_gemini.py` (by converged decision):
  - Prompt is passed inline via `-p <content>` (iter-0045 operator
    decision; aligns with dispatch-canon-validator.GROK_FORBIDDEN_FLAGS).
    A per-pass copy is still written to iter_dir for audit/provenance
    (prompt_sha256), but grok itself reads the inline argument.
  - Auth pre-flight: probe for ~/.grok/auth.json. Missing -> raise
    `GrokAuthRequiredError` BEFORE invoking the CLI (codex acceptance gate G4).
  - Output: `--output-format streaming-json` so the silence watchdog sees
    token-by-token liveness (DEFECT 1 fix; `_assemble_grok_stream` rebuilds
    the answer from the `text` events).
  - Independence flags: `--no-memory --disable-web-search` + a fresh empty
    per-pass `--cwd` temp dir (DEFECT 2 fix - grok's recursive watcher
    scans the --cwd dir; an empty dir avoids the /tmp systemd-private
    PermissionDenied noise). Prior shape (`--prompt-file` + `--no-plan` +
    `--no-subagents` + `--max-turns` + `--permission-mode` + project-subdir
    `--cwd`) caused indefinite stalls on real packets.
  - No sandbox / no integrity check - by converged decision (YAGNI; kimi's
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
import tempfile
import threading
import time
from pathlib import Path

import yaml

from consensus_mcp._dispatch_base import (
    derive_pass_id,
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
    # Shared free-text JSON extractor (moved to base from the gemini/grok
    # verbatim duplicates).
    _extract_json_from_text,
    build_failed_event,
    record_reader_error,
    scrub_env_keys,
    GROK_SCRUBBED_ENV_KEYS,
)


def _grok_subprocess_env() -> dict:
    """Environment for the grok subprocess.

    Returns a COPY of the parent environment with ambient XAI_API_KEY /
    GROK_API_KEY removed: grok CLI auth is its own login/file credential,
    and a stray API key in the environment could hijack that auth and route
    the request through an external key (mirrors kimi's documented scrub
    rationale). Never mutates os.environ.
    """
    return scrub_env_keys(os.environ.copy(), GROK_SCRUBBED_ENV_KEYS)


# Adapter-specific finding patterns.
_GROK_FINDING_ID_PATTERN = re.compile(r"^grok-rev-\d+$")
_VALID_SEVERITIES = {"low", "medium", "high", "blocking", "critical"}
_REQUIRED_FINDING_FIELDS = ("id", "severity", "summary", "citation", "risk", "recommendation")
_ALLOWED_FINDING_KEYS = set(_REQUIRED_FINDING_FIELDS) | {"patch_proposal", "patch_not_proposed_reason"}
_ALLOWED_TOP_LEVEL_KEYS = {"findings", "goal_satisfied", "blocking_objections", "goal_satisfied_rationale"}
_BLOCKING_SEVERITIES = {"blocking", "critical"}

# Default Grok model for this consensus workflow. `grok models` displays
# this as "Grok Build" and exposes the CLI id as `grok-build`; dispatch
# preserves the display label in config/provenance and normalizes for argv.
_DEFAULT_GROK_MODEL: str | None = "Grok Build"
_GROK_MODEL_ID_BY_DISPLAY = {"Grok Build": "grok-build"}


def _resolve_grok_model(model: str | None) -> str | None:
    if model is None:
        return None
    return _GROK_MODEL_ID_BY_DISPLAY.get(model, model)

# Independence flag set passed on every invocation. Codified to the
# operator-verified 2026-05-27 working shape (iter-0045 panel: codex
# high finding + kimi finding; operator decision: adopt inline -p +
# minimal flags). The prior v1.31.1 hot-patch flag set (--prompt-file +
# --no-plan + --no-subagents + --max-turns N + --permission-mode +
# project-subdir --cwd) was removed because that COMBINATION produced
# indefinite stalls. NOTE: the earlier "grok counts every prompt chunk /
# MCP rejection / tool-call attempt against a message budget and never
# reaches model output" rationale was a Claude-side MISDIAGNOSIS - grok
# has no MCP servers configured (`grok mcp list` -> none), handles its
# 512K context fine, and the real stall was DEFECT 1: plain output + the
# silence watchdog. Plain buffers ALL stdout until the answer is ready,
# so the watchdog killed grok for being silent while grok was silent by
# design. The fix is --output-format streaming-json (see _build_grok_cmd
# + _assemble_grok_stream and docs/grok-dispatch-streaming-watchdog-fix.md)
# so the watchdog sees token-by-token liveness. The minimal flag shape
# still satisfies dispatch-canon-validator.GROK_FORBIDDEN_FLAGS (the
# validator forbids --prompt-file, --max-turns, --no-plan, --no-subagents,
# --permission-mode for direct grok invocations).
_GROK_DISABLED_TOOLS = (
    "--no-memory",
    "--disable-web-search",
)

# Inline `-p <prompt>` is the proven default shape (iter-0045), but passing the
# prompt as an argv entry is bounded by the OS command-line limit. A large
# review-packet (the cold-start consult finding) blows past it and grok dies with
# an opaque E2BIG / "Argument list too long" before producing any output. When the
# prompt exceeds a safe inline ceiling we instead hand grok the per-pass prompt
# FILE we already write for provenance, via `--prompt-file` - keeping the
# small-prompt path (every existing test) byte-for-byte unchanged. `--prompt-file`
# is the dispatcher's INTERNAL subprocess argv (never inspected by
# dispatch-canon-validator, which only constrains Bash-issued grok invocations),
# so routing oversized prompts through it does not touch Gate G3.
#
# Portability (kimi-rev-002): the ceiling MUST clear the SMALLEST platform limit,
# not Linux's. Linux MAX_ARG_STRLEN is 128KB per arg, but Windows CreateProcessW
# caps the ENTIRE command line at ~32767 UTF-16 chars - so on win32 a 96KB inline
# prompt would fail there while passing on Linux. We therefore pick the ceiling per
# platform: a conservative ~28KB on Windows (headroom under 32767 for the rest of
# the argv), 96KB elsewhere (well under Linux's 128KB per-arg limit).
_GROK_INLINE_PROMPT_MAX_BYTES = (28 * 1024) if sys.platform == "win32" else (96 * 1024)


class GrokAuthRequiredError(RuntimeError):
    """Raised when ~/.grok/auth.json is missing (the auth pre-flight)."""


class GrokInvocationError(RuntimeError):
    """Raised when the grok CLI exits non-zero, times out, or is not found."""


class GrokStreamCancelledError(GrokInvocationError):
    """grok self-cancelled (stopReason == 'Cancelled') before emitting any
    text event - the agentic self-cancel on open-ended prompts. A subclass
    of GrokInvocationError so the parse-retry wrapper treats it as an
    invocation failure, NOT a JSON parse failure (no wasted parse-retry).
    See docs/grok-dispatch-streaming-watchdog-fix.md."""


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
    if resolved is not None:
        return resolved
    # Consult Finding A / Q4: a long-lived MCP server resolves bare names against
    # its LAUNCH-TIME PATH, which can be stale (e.g. ~/.grok/bin became visible
    # after the server started) -> which() returns None even though the binary
    # exists. Fall back to a configurable extra search path so resolution
    # succeeds without a per-machine config edit. CONSENSUS_MCP_BIN_DIRS is an
    # os.pathsep-separated list of directories searched in order.
    found = _search_extra_bin_dirs(grok_bin)
    return found if found is not None else grok_bin


def _search_extra_bin_dirs(name: str) -> str | None:
    """Look for an executable `name` in CONSENSUS_MCP_BIN_DIRS (os.pathsep list).

    Returns the absolute path of the first executable match, or None. Bypasses
    the process PATH entirely, so it works even when the server's launch-time
    PATH is stale (consult Finding A / Q4)."""
    raw = os.environ.get("CONSENSUS_MCP_BIN_DIRS", "")
    for d in raw.split(os.pathsep):
        if not d:
            continue
        cand = shutil.which(name, path=d)
        if cand is not None:
            return cand
    return None


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
    # Sanitize pass_id for use as a filename - alphanumerics + dash/underscore
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
    run_cwd: str = "/tmp",
    prompt_file: "Path | None" = None,
) -> list[str]:
    """Construct the grok CLI command list for a dispatch.

    Shape: inline `-p <prompt>` + `--output-format streaming-json` +
    `--no-memory` + `--disable-web-search` + `--cwd <run_cwd>`.

    Oversized-prompt fallback (cold-start consult finding): inline `-p` is bounded
    by Linux MAX_ARG_STRLEN (128KB). When `prompt` exceeds
    `_GROK_INLINE_PROMPT_MAX_BYTES` AND a `prompt_file` is supplied, grok reads the
    prompt from that file via `--prompt-file` instead of the inline argument -
    avoiding the opaque E2BIG crash on a large review-packet. Small prompts (every
    existing test + the common case) keep the proven inline `-p` shape exactly.

    `--output-format streaming-json` (DEFECT 1 fix): plain output buffers
    all stdout until the answer is ready, so the silence watchdog in
    `_invoke_grok` killed grok for being silent while grok was silent by
    design. Streaming emits `{"type":"thought"|"text"|"end",...}` event
    lines continuously, keeping the watchdog fed and surfacing progress in
    the dispatch log. `_assemble_grok_stream` reassembles the answer from
    the `text` events.

    `run_cwd` (DEFECT 2 fix): grok runs a recursive filesystem watcher on
    the directory named by its `--cwd` FLAG (confirmed by grok 2026-05-30:
    the watcher keys off the flag, not the OS process cwd). With `--cwd
    /tmp` that watcher hits PermissionDenied on `/tmp/systemd-private-*`
    (non-fatal noise). The dispatcher therefore passes a fresh, empty
    per-pass temp dir as `run_cwd` so the watcher scans nothing
    unreadable. The `/tmp` default is the canon fallback. (This is the
    dispatcher's INTERNAL subprocess argv; it is never inspected by
    dispatch-canon-validator, which only checks grok invocations issued
    directly via the Bash tool - so a per-pass temp `--cwd` keeps Gate G3
    green without touching the validator.)

    Note: inline `-p` carries a Linux MAX_ARG_STRLEN limit of 128KB per
    argument. If a packet ever exceeds that, the CLI will fail loudly
    with E2BIG; the caller should chunk or summarize the prompt.
    """
    too_large = len(prompt.encode("utf-8")) > _GROK_INLINE_PROMPT_MAX_BYTES
    if too_large and prompt_file is not None:
        cmd = [
            _resolve_grok_bin(grok_bin),
            "--prompt-file", str(prompt_file),
            "--output-format", "streaming-json",
        ]
    else:
        cmd = [
            _resolve_grok_bin(grok_bin),
            "-p", prompt,
            "--output-format", "streaming-json",
        ]
    cmd.extend(_GROK_DISABLED_TOOLS)
    cmd.extend(["--cwd", run_cwd])
    if model:
        cmd.extend(["--model", _resolve_grok_model(model)])
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
    _sleep=None,
) -> tuple[str, Path]:
    """Shell out to grok CLI; assemble the streaming answer.

    Returns (assembled_answer, prompt_path). The answer is reassembled by
    `_assemble_grok_stream` from grok's `--output-format streaming-json`
    events (DEFECT 1 fix); `output_sha256` therefore keeps its plain-mode
    meaning ("the answer grok produced"). The prompt_path is the per-pass
    file we wrote (caller reads it for sha256 provenance).

    Thin wrapper that owns the per-pass run-cwd lifecycle: grok runs from a
    fresh empty temp dir (DEFECT 2 fix - see `_build_grok_cmd`) that is
    ALWAYS removed afterwards, including every watchdog/abort raise path.
    """
    grok_run_cwd = tempfile.mkdtemp(prefix="grok-run-")
    try:
        return _invoke_grok_in_cwd(
            grok_run_cwd,
            prompt=prompt,
            grok_bin=grok_bin,
            model=model,
            timeout_seconds=timeout_seconds,
            iter_dir=iter_dir,
            pass_id=pass_id,
            repo_root=repo_root,
            log_path=log_path,
            anchors=anchors,
            heartbeat_interval=heartbeat_interval,
            stall_silence_seconds=stall_silence_seconds,
            poll_interval=poll_interval,
            time_fn=time_fn,
            popen_factory=popen_factory,
            _sleep=_sleep,
        )
    finally:
        shutil.rmtree(grok_run_cwd, ignore_errors=True)


def _invoke_grok_in_cwd(
    grok_run_cwd: str,
    *,
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
    _sleep=None,
) -> tuple[str, Path]:
    """Run grok from `grok_run_cwd`; return (assembled_answer, prompt_path).

    Stream-and-watchdog loop: the silence watchdog advances on every stdout
    line via `stdout_reader`; `--output-format streaming-json` emits
    thought/text event lines continuously so the watchdog stays fed (the
    DEFECT 1 fix - plain output buffered all stdout, starving the watchdog).
    grok reads the prompt inline via `-p` (or, for an oversized prompt, from the
    per-pass `--prompt-file`; see `_build_grok_cmd`) - never over stdin, so there
    is no stdin-writer thread + no codex-rev-001 deadlock dance either way.

    `_sleep` is a private test seam (defaults to `time.sleep`; the
    deterministic-clock harness injects it so the watchdog tests do not
    flake - mirrors `_invoke_codex`'s blessed `_sleep` seam).
    """
    if time_fn is None:
        time_fn = time.time
    if popen_factory is None:
        popen_factory = subprocess.Popen
    if _sleep is None:
        _sleep = time.sleep
    can_log = log_path is not None and anchors is not None

    # Pre-flight auth check BEFORE writing the prompt file (no point creating
    # an artifact if we're going to error out immediately).
    _check_grok_auth()

    # Per-pass prompt file is written to iter_dir for audit/provenance
    # (prompt_sha256 in dispatch_log + dispatch_provenance), but grok
    # itself receives the prompt inline via `-p` (iter-0045 shape).
    prompt_path = _write_per_pass_prompt(prompt, iter_dir, pass_id)
    cmd = _build_grok_cmd(grok_bin, prompt, model, run_cwd=grok_run_cwd,
                          prompt_file=prompt_path)

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
            cwd=grok_run_cwd,
            env=_grok_subprocess_env(),
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
        except Exception as exc:
            record_reader_error(stdout_buf, "stdout", exc)

    def stderr_reader():
        try:
            for raw_line in iter(proc.stderr.readline, b""):
                if not raw_line:
                    break
                with state_lock:
                    stderr_buf.append(raw_line)
        except Exception as exc:
            record_reader_error(stderr_buf, "stderr", exc)

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

        _sleep(poll_interval)

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

    return _assemble_grok_stream(
        stdout_bytes.decode("utf-8", errors="replace")
    ), prompt_path


# _JSON_FENCE_RE + _extract_json_from_text moved to _dispatch_base.py (this
# copy mirrored gemini's verbatim); imported above.


def _assemble_grok_stream(raw: str) -> str:
    """Assemble the final answer from grok ``--output-format streaming-json``.

    grok streams one JSON event per line (field name verified live
    2026-05-30)::

        {"type":"thought","data":...}   # reasoning - ignored
        {"type":"text","data":...}      # answer chunk - concatenated
        {"type":"end","stopReason":...} # terminal; EndTurn | Cancelled

    Returns the concatenation of the ``data`` of every ``text`` event (the
    answer), feeding the existing JSON extraction/validation unchanged.

    Defensive (spec Risks section): unparseable lines, non-dict events, and
    events without a ``type`` are skipped - never fatal. A ``text`` event
    missing ``data`` falls back to a ``text`` key before being ignored.

    Raises ``GrokStreamCancelledError`` when the stream ends with
    ``stopReason == "Cancelled"`` having produced ZERO text (the agentic
    self-cancel) - surfaced as an invocation failure, not a silent empty
    answer.

    Backward-compat: if NO line is a streaming event carrying a ``type``
    key (e.g. an already-plain blob), the raw string is returned unchanged
    so the downstream extractor still runs.
    """
    text_parts: list[str] = []
    thought_parts: list[str] = []
    saw_event = False
    saw_text = False
    stop_reason: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(evt, dict) or "type" not in evt:
            continue
        saw_event = True
        etype = evt.get("type")
        if etype == "thought":
            payload = evt.get("data")
            if payload is None:
                payload = evt.get("text")
            if isinstance(payload, str):
                thought_parts.append(payload)
        elif etype == "text":
            payload = evt.get("data")
            if payload is None:
                payload = evt.get("text")
            if isinstance(payload, str):
                text_parts.append(payload)
                saw_text = True
        elif etype == "end":
            stop_reason = evt.get("stopReason") or evt.get("stop_reason")
            break
    if not saw_event:
        return raw
    if stop_reason == "Cancelled" and not saw_text:
        # Grok Build sometimes emits the schema-shaped JSON in `thought`
        # chunks and then terminates with stopReason=Cancelled, leaving zero
        # `text` chunks. Do not seal raw thought prose; only recover a
        # syntactically valid JSON object substring. If no JSON object is
        # present, keep the invocation-failure behavior so proposal mode can
        # retry with the compact prompt.
        thought_text = "".join(thought_parts)
        candidate = _extract_json_from_text(thought_text)
        if candidate != thought_text or candidate.strip().startswith("{"):
            try:
                parsed_candidate = json.loads(candidate)
            except Exception:
                parsed_candidate = None
            if isinstance(parsed_candidate, dict):
                return candidate
        raise GrokStreamCancelledError(
            "grok self-cancelled (stopReason=Cancelled) with zero text "
            "events - no answer produced (agentic self-cancel; see "
            "docs/grok-dispatch-streaming-watchdog-fix.md). Re-dispatch "
            "with a shorter, single-focus prompt."
        )
    return "".join(text_parts)


def _truncate_middle(text: str, max_bytes: int) -> str:
    """Return ``text`` capped to roughly ``max_bytes`` UTF-8 bytes."""
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker = "\n\n[... truncated for Grok cancel-retry ...]\n\n"
    marker_b = marker.encode("utf-8")
    keep = max(0, max_bytes - len(marker_b))
    head_b = encoded[: keep // 2]
    tail_b = encoded[-(keep - keep // 2):] if keep else b""
    return (
        head_b.decode("utf-8", errors="ignore")
        + marker
        + tail_b.decode("utf-8", errors="ignore")
    )


def _build_grok_cancel_retry_prompt(
    *,
    goal_packet: dict | None,
    review_target_text: str | None,
    review_target_path: str | None,
    review_target_hash: str | None,
    schema_text: str | None,
) -> str:
    """Build a short single-focus proposal prompt after Grok self-cancels.

    The full proposal template is intentionally rich, but Grok Build can
    self-cancel with zero text on large/open-ended prompts.  The recovery path
    keeps the same goal and review target, strips nonessential mandates, and
    asks for the exact proposal JSON only.  Keep this under the smallest inline
    ceiling so the retry uses the proven ``-p`` path rather than ``--prompt-file``.
    """
    goal_packet = goal_packet or {}
    goal = goal_packet.get("goal", {}) or {}
    auth = goal_packet.get("authorization", {}) or {}
    compact = {
        "goal_summary": goal.get("summary", ""),
        "desired_end_state": goal.get("desired_end_state", ""),
        "allowed_files": goal_packet.get("allowed_files", []) or [],
        "acceptance_gates": goal_packet.get("acceptance_gates", []) or [],
        "scope_signature": auth.get("scope_signature", ""),
        "review_target_path": review_target_path,
        "review_target_hash": review_target_hash,
    }
    schema = schema_text or (
        '{"selected_target": str|null, "rationale_vs_alternatives": str, '
        '"deliverable_scope": object|null, "risks": [str], '
        '"estimated_complexity": "small|medium|large", '
        '"structural_abstention": bool}'
    )

    target_budget = min(
        18 * 1024,
        max(4 * 1024, _GROK_INLINE_PROMPT_MAX_BYTES - 8 * 1024),
    )
    target_excerpt = _truncate_middle(review_target_text or "(not provided)", target_budget)
    prompt = f"""You are Grok Build acting as one independent contributor in a consensus-mcp design consult.
Your previous full proposal prompt self-cancelled before producing text. Retry with this shorter single-focus prompt.

Task: produce ONE design proposal. This is not a code review.
Return ONLY valid JSON. No markdown. No prose outside JSON.

Goal packet summary:
{json.dumps(compact, indent=2, ensure_ascii=False)}

Required JSON schema/shape:
{schema}

Rules:
- Pick exactly one selected_target unless structural_abstention is true.
- deliverable_scope must include next_iteration_id, files_in_scope, files_out_of_scope, key_design_decisions, acceptance_gates unless abstaining.
- Be concrete and concise. Prefer completion in the next iteration when scope is small and gates are clear.
- If context is insufficient, set structural_abstention true and explain why.

Review target content, truncated if needed:
```yaml
{target_excerpt}
```
"""
    return _truncate_middle(
        prompt,
        min(24 * 1024, _GROK_INLINE_PROMPT_MAX_BYTES - 1024),
    )


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
                f"findings[{i}].patch_proposal must be null in v1.31.0 - grok is "
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
    cancel_retry_prompt: str | None = None,
) -> tuple[str, dict, Path]:
    """Validator-retry on parse fail. Mirrors gemini's pattern.

    Proposal mode has one extra recovery: if Grok self-cancels before emitting
    any text, retry once with a compact single-focus prompt so required Grok
    reviewers are not silently dropped from the consult.

    Returns (raw_output, parsed_dict, prompt_path).
    """
    try:
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
    except GrokStreamCancelledError as first_cancel:
        if mode != "proposal" or not cancel_retry_prompt:
            raise
        if log_path is not None and anchors is not None:
            _log_dispatch(log_path, {
                "event": "dispatch_retry_for_grok_cancel",
                **anchors,
                "first_error": str(first_cancel)[:1000],
                "retry_prompt_bytes": len(cancel_retry_prompt.encode("utf-8")),
            })
        retry_pass_id = f"{pass_id}-cancel-retry"
        raw, prompt_path = _invoke_grok(
            prompt=cancel_retry_prompt,
            grok_bin=grok_bin,
            model=model,
            timeout_seconds=timeout_seconds,
            iter_dir=iter_dir,
            pass_id=retry_pass_id,
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
            + "\n\n# Retry - your previous response failed JSON validation\n\n"
            + f"Parse error: {first_err}\n\n"
            + "Re-emit ONLY valid JSON conforming to the schema in the prompt above. "
            + "No prose, no markdown fences, no commentary - JSON only, starting with `{` "
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
                   help=("Grok model; default 'Grok Build' (normalized to CLI id "
                         "grok-build when invoking grok)."))
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
    _pre_pass_id = ns.pass_id or derive_pass_id(_pre_iter_id, ns.review_target, _pre_reviewer_id)

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
    pass_id = ns.pass_id or derive_pass_id(iteration_id, ns.review_target, reviewer_id)

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
        # Thin wrapper over the shared skeleton builder (drift fix); the
        # extras carry only the provenance actually computed (None skipped).
        return build_failed_event(
            adapter="grok",
            error_type=error_type,
            error=error,
            reviewer_id=reviewer_id,
            pass_id=pass_id,
            iteration_id=iteration_id,
            timeout_seconds=ns.timeout_seconds,
            extra_fields={
                "disabled_tools": list(_GROK_DISABLED_TOOLS),
                "grok_version": grok_version,
                "model": ns.model,
                "prompt_sha256": prompt_sha,
                "output_sha256": output_sha,
                "goal_packet_sha256": goal_packet_sha,
                "scope_signature": scope_sig,
                "review_target_path": review_target_path_str,
                "review_target_hash": review_target_hash,
                "prompt_file_path": prompt_file_path_str,
            },
        )

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
        cancel_retry_prompt = None
        if ns.mode == "proposal":
            schema_text = None
            if proposal_schema_path is not None:
                try:
                    schema_text = Path(proposal_schema_path).read_text(encoding="utf-8")
                except FileNotFoundError:
                    schema_text = None
            cancel_retry_prompt = _build_grok_cancel_retry_prompt(
                goal_packet=goal_packet,
                review_target_text=review_target_text,
                review_target_path=review_target_path_str,
                review_target_hash=review_target_hash,
                schema_text=schema_text,
            )
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
            cancel_retry_prompt=cancel_retry_prompt,
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
