"""Phase 4 v1.1 — auto-codex-dispatch helper (codex adapter).

Replaces the operator paste-buffer flow for codex reviews. Reads a goal_packet
for context, substitutes a markdown prompt template, shells out to `codex` CLI,
parses the JSON response (constrained via codex's --output-schema), wraps it in
a sealed-packet outer structure, and calls T6 (review.write_and_seal) directly.
Writes a richly-attributed audit trail to consensus-state/state/dispatch-log.jsonl.

iter-0010 (v1.14.0): generic dispatch helpers (repo-root resolution, path
normalization, goal_packet/template loading, prompt building, process-tree
termination, per-patch base_sha, sealing, dispatch-log writing) live in
consensus_mcp/_dispatch_base.py and are reused by every reviewer adapter.
This module keeps only codex-specific code: CLI invocation, version probe,
binary resolution, codex-output JSON parsing + patch_proposal validation,
and the codex CLI entrypoint. Codex behavior is preserved bit-for-bit.

NOT an MCP tool. CLI-only. Per Phase 3 anti-scope, consensus_mcp/tools/*
is FROZEN; this helper is in the package root and calls T6's handle in-process.

USAGE
-----
  python -m consensus_mcp._dispatch_codex \\
      --goal-packet <path/to/goal_packet.yaml> \\
      --iteration-dir <path/to/iteration-XXXX/> \\
      [--reviewer-id codex-iterXXXX-N] \\
      [--pass-id codex-iterXXXX-N-passN] \\
      [--prompt-template <path>] \\
      [--schema <path>] \\
      [--codex-bin <path>] \\
      [--timeout-seconds 600] \\
      [--smoke]

Exit 0 = sealed pass produced; non-zero = failure (codex error, parse fail,
schema fail, seal fail). JSON to stdout on success.
"""
from __future__ import annotations

import argparse
import hashlib
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

# iter-0010: generic dispatch helpers extracted to _dispatch_base.py per
# iter-0009 verdict Q1: F1b. Re-imported into this module's namespace so
# (a) existing call sites in _invoke_codex / _validate_patch_proposal /
# main don't need to be rewritten and (b) tests that access these via
# `_dispatch_codex.<name>` continue to resolve through the import binding.
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
    _compute_per_patch_base_sha,
    _sha256_str,
    _build_sealed_packet,
    _seal_via_t6,
    _log_dispatch,
    # iter-0010 codex-rev-001 fix: patch_proposal validation moved to base.
    _PATCH_PROPOSAL_REQUIRED,
    _PATCH_PROPOSAL_OPTIONAL,
    _PATCH_PROPOSAL_ALLOWED,
    _PATCH_ID_PATTERN,
    _DIFF_FILE_HEADER_RE,
    _APPLY_PATCH_BEGIN_MARKER,
    _APPLY_PATCH_UPDATE_MARKER,
    _validate_patch_proposal as _base_validate_patch_proposal,
)


def _validate_patch_proposal(
    finding_index: int,
    finding_id: str,
    pp: dict,
    all_finding_ids: set,
    goal_packet: dict | None,
    review_packet: dict | None = None,
    repo_root: Path | None = None,
) -> None:
    """Codex adapter wrapper: forwards to base validator with CodexOutputParseError.

    The base helper accepts an ``error_class`` kwarg defaulting to ValueError;
    codex callers want CodexOutputParseError to preserve the existing test
    contract (``pytest.raises(CodexOutputParseError)``). This wrapper threads
    the codex error class through so test imports of
    ``_dispatch_codex._validate_patch_proposal`` continue to behave identically
    to the pre-extraction state.
    """
    _base_validate_patch_proposal(
        finding_index=finding_index,
        finding_id=finding_id,
        pp=pp,
        all_finding_ids=all_finding_ids,
        goal_packet=goal_packet,
        review_packet=review_packet,
        repo_root=repo_root,
        error_class=CodexOutputParseError,
    )


_VALID_SEVERITIES = {"low", "medium", "high", "blocking", "critical"}
_FINDING_ID_PATTERN = re.compile(r"^codex-rev-\d+$")
_REQUIRED_FINDING_FIELDS = ("id", "severity", "summary", "citation", "risk", "recommendation")
# Per Task #24 (iter-0014): patch_proposal is the optional finding key.
# iter-0018 Finding 4: patch_not_proposed_reason is added — used in strict mode
# to record why a patch wasn't authored. Mutually exclusive with patch_proposal
# (a finding can't both have a patch and a reason for not having one).
# Anti-self-verification: verified / self_verified / correct / approved / confirmed
# are NOT in this set; they remain rejected by the unknown-key gate as before.
_ALLOWED_FINDING_KEYS = set(_REQUIRED_FINDING_FIELDS) | {"patch_proposal", "patch_not_proposed_reason"}
_ALLOWED_TOP_LEVEL_KEYS = {"findings", "goal_satisfied", "blocking_objections", "goal_satisfied_rationale"}
_BLOCKING_SEVERITIES = {"blocking", "critical"}

# iter-0010: patch_proposal validation constants moved to _dispatch_base.py
# and re-imported above (per iter-0010 codex-rev-001 blocking finding).
# _PATCH_PROPOSAL_REQUIRED / _PATCH_PROPOSAL_OPTIONAL / _PATCH_PROPOSAL_ALLOWED /
# _PATCH_ID_PATTERN / _DIFF_FILE_HEADER_RE / _APPLY_PATCH_BEGIN_MARKER /
# _APPLY_PATCH_UPDATE_MARKER all live in _dispatch_base.py now and are
# accessible as `_dispatch_codex.<name>` via the import binding above for
# backward compat with tests.


class CodexInvocationError(RuntimeError):
    """Raised when the codex CLI exits non-zero, times out, or is not found."""


class CodexOutputParseError(ValueError):
    """Raised when codex output is not parseable JSON or lacks the expected top-level shape."""


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

    Returns the resolved path. If no resolution is possible (codex_bin already
    looks like an absolute path OR shutil.which returns nothing), returns the
    input unchanged so subprocess.run produces a clear "binary not found".
    Per iter-0039 xplat-rev-008 + codex-iter0039-1 codex-rev-001 refinement:
      - MSYS-style `/c/foo/bar` paths converted to `C:\\foo\\bar` on Windows.
        Conversion gated on `codex_bin[1].isalpha()` so unrelated POSIX-shaped
        absolute paths like `/tmp/x` or `/_/y` aren't mangled into `T:\\x` /
        `_:\\y`.
      - Bare names with no extension on Windows: try .exe, .cmd, .bat, .ps1
        in PATH explicitly so callers don't get bitten by PATHEXT ordering
        ambiguity.
      - App Execution Alias stub rejection on Windows: 0-byte files in
        `%LOCALAPPDATA%\\Microsoft\\WindowsApps` are App Aliases that
        subprocess cannot exec; raise a clear error rather than the cryptic
        WinError 193 / file-not-found that bare exec produces.
    """
    # iter-0039 xplat-rev-008: MSYS-style drive-path conversion on Windows.
    # Only convert if char[1] is a real drive letter (codex-iter0039-1
    # codex-rev-001 refinement: don't mangle /tmp, /_/foo, etc.).
    if (
        sys.platform == "win32"
        and len(codex_bin) >= 3
        and codex_bin[0] == "/"
        and codex_bin[1].isalpha()
        and codex_bin[2] == "/"
    ):
        drive = codex_bin[1].upper()
        rest = codex_bin[3:].replace("/", "\\")
        codex_bin = f"{drive}:\\{rest}"

    # If caller already gave a full path that exists, use it as-is.
    if os.path.sep in codex_bin or (len(codex_bin) > 1 and codex_bin[1] == ":"):
        # iter-0039 xplat-rev-008: App Execution Alias check on Windows.
        # 0-byte stub in WindowsApps cannot be exec'd by subprocess.
        if sys.platform == "win32":
            p = Path(codex_bin)
            try:
                if p.exists() and p.stat().st_size == 0:
                    windows_apps = (
                        os.environ.get("LOCALAPPDATA", "")
                        + r"\Microsoft\WindowsApps"
                    )
                    if windows_apps and str(p).lower().startswith(windows_apps.lower()):
                        raise CodexInvocationError(
                            f"codex binary {codex_bin!r} is a Windows App Execution "
                            f"Alias stub (0-byte file in WindowsApps). subprocess cannot "
                            f"exec App Aliases — install the real codex CLI binary or "
                            f"adjust PATH so a non-stub variant is preferred."
                        )
            except OSError:
                pass  # stat failed; treat as not-stub and let subprocess fail downstream
        return codex_bin

    # iter-0039 xplat-rev-008: bare-name resolution on Windows. If the name
    # has no extension, try common Windows-executable extensions explicitly
    # so we don't depend on PATHEXT ordering producing the right hit.
    if sys.platform == "win32" and "." not in Path(codex_bin).name:
        for ext in (".exe", ".cmd", ".bat", ".ps1"):
            cand = shutil.which(codex_bin + ext)
            if cand:
                # Recurse for the App-Alias / .ps1 quirks.
                return _resolve_codex_bin(cand)

    resolved = shutil.which(codex_bin)
    if resolved is None:
        return codex_bin
    # On Windows, prefer .cmd over .ps1 (Python's subprocess can directly exec
    # .cmd via CreateProcess but not .ps1 which needs powershell.exe).
    if sys.platform == "win32" and resolved.lower().endswith(".ps1"):
        cmd_alt = shutil.which(codex_bin + ".cmd")
        if cmd_alt:
            resolved = cmd_alt
    # iter-0039 xplat-rev-008: App Execution Alias check post-PATH-resolution.
    if sys.platform == "win32":
        p = Path(resolved)
        try:
            if p.exists() and p.stat().st_size == 0:
                windows_apps = (
                    os.environ.get("LOCALAPPDATA", "")
                    + r"\Microsoft\WindowsApps"
                )
                if windows_apps and str(p).lower().startswith(windows_apps.lower()):
                    raise CodexInvocationError(
                        f"codex binary resolved to {resolved!r} which is a Windows "
                        f"App Execution Alias stub (0-byte file in WindowsApps). "
                        f"subprocess cannot exec App Aliases — install the real "
                        f"codex CLI binary or adjust PATH so a non-stub variant is "
                        f"preferred."
                    )
        except OSError:
            pass
    return resolved


def _get_codex_version(codex_bin: str) -> str:
    """Best-effort: shell out to `<codex> --version` and return the version string.

    Returns 'unknown' on any failure (FileNotFoundError, non-zero exit, parse fail).
    Used for audit-log provenance only; never blocks dispatch.
    """
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


def _invoke_codex(
    prompt: str,
    codex_bin: str,
    timeout_seconds: int,
    repo_root: Path,
    schema_path: Path,
    log_path=None,
    anchors=None,
    heartbeat_interval: float = 30.0,
    stall_silence_seconds: float = 180.0,
    poll_interval: float = 0.5,
    time_fn=None,
    popen_factory=None,
    *,
    _sleep=None,
    _drain_stderr: bool = True,
) -> str:
    """Shell out to codex CLI via Popen + reader threads (iter-0037 bidirectional).

    Per iter-0036 lesson + iter-0037 codex pre-review (codex-iter0037-1):
      - Replace blocking subprocess.run with Popen + 2 reader threads (stdout
        + stderr; both drained to prevent pipe-buffer deadlock — codex-rev-001).
      - Emit dispatch_streamed_line per stdout line (truncated to 200 chars)
        and dispatch_heartbeat every heartbeat_interval seconds.
      - Auto-abort on heartbeat-silence (no stdout for stall_silence_seconds);
        wall-time is a soft ceiling that raises but does NOT terminate
        preemptively (codex-rev-002 — operator preference).
      - Silence check fires on EVERY poll, not just heartbeat tick
        (codex-rev-003) so the 90s threshold actually bounds the recovery time.
      - stdout_buf / stderr_buf / streaming counters protected by state_lock
        (codex-rev-004); still-alive reader after proc exit = failure.

    log_path + anchors are optional for back-compat with callers that don't
    have them (tests using the legacy mock pattern). When None: streaming
    events / heartbeats / abort-signal-file are all disabled and the function
    still returns the final output.

    Verified codex-cli 0.129.0 CLI shape unchanged from prior subprocess.run
    invocation: `codex exec --skip-git-repo-check --cd <REPO> --sandbox
    read-only --output-schema <FILE> -o <OUT> -` (prompt via stdin).
    """
    if time_fn is None:
        time_fn = time.time
    if popen_factory is None:
        popen_factory = subprocess.Popen
    # Private, keyword-only sleep seam (v1.15.9 converged plan
    # q1_sync_mechanism). Defaults to `time.sleep` → ZERO change to
    # production behavior. The streaming test harness injects a
    # SyncClock-backed blocker so the poll loop has a deterministic
    # happens-before with the test driver instead of racing real
    # wall-clock on loaded CI runners. Chosen over monkeypatching
    # global `time.sleep` (no global-state leak — see the
    # monkeypatch-pollution lesson).
    if _sleep is None:
        _sleep = time.sleep
    can_log = log_path is not None and anchors is not None

    # iter-0007 F4 (deferred infra): allow env-var override of the stall-
    # silence threshold so operators with large prompts / slow models can
    # extend without code change. Codex cold-start on 50KB+ prompts often
    # exceeds the 180s default.
    env_silence = os.environ.get("CONSENSUS_MCP_STALL_SILENCE_SECONDS")
    if env_silence:
        try:
            stall_silence_seconds = float(env_silence)
        except ValueError:
            pass  # keep default

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
        # iter-0039 xplat-rev-002: spawn codex in its own process group so
        # _terminate_process_tree can signal the whole tree (codex spawns
        # Node descendants that orphan otherwise).
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
                **popen_kwargs,
            )
        except FileNotFoundError:
            raise CodexInvocationError(f"codex binary not found: {codex_bin}") from None

        # Write prompt + close stdin. Binary mode preserves UTF-8 bytes verbatim
        # (per v1.10.3 Windows hardening — text-mode \n→\r\n translation corrupts
        # multibyte sequences).
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            # codex died before we finished writing; failure will surface via
            # returncode check after process exits.
            pass

        state_lock = threading.Lock()
        stdout_buf: list = []
        stderr_buf: list = []
        last_streamed_ts: list = [None]    # 1-element list for nonlocal mutability
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
                pass  # reader exits silently; main loop sees proc.poll()

        def stderr_reader():
            """codex-rev-001 fix: drain stderr to prevent pipe-buffer deadlock.
            Codex's --output-schema config + prompt echo go to stderr first,
            then any error tail. ~64KB pipe buffer would block codex's write
            if not drained, deadlocking the dispatch.
            """
            try:
                for raw_line in iter(proc.stderr.readline, b""):
                    if not raw_line:
                        break
                    with state_lock:
                        stderr_buf.append(raw_line)
            except Exception:
                pass

        t_stdout = threading.Thread(target=stdout_reader, daemon=True, name="codex-stdout-reader")
        t_stderr = threading.Thread(target=stderr_reader, daemon=True, name="codex-stderr-reader")
        t_stdout.start()
        # _drain_stderr defaults True (zero behaviour change — the
        # stderr reader always runs in production). It is a private,
        # keyword-only TEST seam (v1.15.9 `_sleep=` precedent): the
        # backpressure mutant gate sets it False to NOT drain stderr,
        # deterministically reproducing the full-pipe deadlock real
        # codex would hit. Never exposed on the CLI/public contract.
        if _drain_stderr:
            t_stderr.start()

        start_ts = time_fn()
        last_heartbeat = start_ts

        while proc.poll() is None:
            now = time_fn()

            # 1) Operator abort-signal file (polled every iteration).
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
                raise CodexInvocationError(f"dispatch aborted by operator: {abort_reason}")

            # 2) Heartbeat-silence check on EVERY poll (codex-rev-003 fix).
            # Only fires after codex has emitted at least one line; if nothing
            # streamed yet, fall back to age-since-start so a startup-stuck
            # codex still gets caught.
            with state_lock:
                lst = last_streamed_ts[0]
                seq_snap = streamed_seq[0]
            if lst is not None:
                silence_age = now - lst
                silence_trigger_threshold = stall_silence_seconds
            else:
                silence_age = now - start_ts
                # Pre-first-line silence: same threshold as post-first-line.
                silence_trigger_threshold = stall_silence_seconds

            if silence_age >= silence_trigger_threshold:
                _terminate_process_tree(proc)
                if can_log:
                    _log_dispatch(log_path, {
                        "event": "dispatch_aborted",
                        **anchors,
                        "abort_source": "watchdog_silence",
                        "abort_reason": f"no codex stdout for {silence_age:.0f}s (threshold {silence_trigger_threshold:.0f}s)",
                        "age_seconds": now - start_ts,
                        "last_streamed_line_age_seconds": silence_age if lst is not None else None,
                    })
                raise CodexInvocationError(
                    f"codex stuck: no output for {silence_age:.0f}s"
                )

            # 3) Heartbeat emission on heartbeat_interval cadence.
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

            # 4) Wall-time soft ceiling (codex-rev-002 fix: no preemptive
            # terminate per operator preference; heartbeat-silence is the only
            # auto-killer. But we cannot wait forever — raise after wall_time
            # + grace if codex is somehow streaming-but-runaway).
            if now - start_ts >= timeout_seconds + stall_silence_seconds:
                # Hard ceiling reached. Tree-terminate to avoid zombie + raise.
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
                raise CodexInvocationError(
                    f"codex exceeded {timeout_seconds}s wall timeout + {stall_silence_seconds}s grace"
                )

            _sleep(poll_interval)

        # Process exited; drain reader threads. (Joining a never-
        # started thread raises RuntimeError, so the t_stderr join +
        # liveness check are guarded by the same _drain_stderr gate.)
        t_stdout.join(timeout=5)
        if _drain_stderr:
            t_stderr.join(timeout=5)
        stderr_alive = _drain_stderr and t_stderr.is_alive()
        if t_stdout.is_alive() or stderr_alive:
            # codex-rev-004 fix: still-alive reader after proc exit = invocation failure.
            raise CodexInvocationError(
                "codex exited but reader thread did not drain within 5s; "
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
            raise CodexInvocationError(
                f"codex exit={proc.returncode}; stderr_tail={stderr_tail!r}{stdout_hint}"
            )
        try:
            return Path(out_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise CodexInvocationError(f"codex output file unreadable: {exc}") from exc
    finally:
        try:
            Path(out_file).unlink()
        except OSError:
            pass


def _parse_codex_output(
    text: str,
    goal_packet: dict | None = None,
    review_packet: dict | None = None,
    repo_root: Path | None = None,
) -> dict:
    """Parse codex's structured JSON output + locally validate the shape.

    Codex is invoked with --output-schema; the final assistant message MUST be JSON
    matching the schema. This function adds defense-in-depth validation per
    codex review F2 (2026-05-09) AND v1.10.2 hardening F2/F3:
      - root is a JSON object
      - top-level keys are EXACTLY {findings, goal_satisfied, blocking_objections}
        (+ optional goal_satisfied_rationale); unknown keys rejected
      - findings is a list; each finding has ALL 6 required fields (id, severity,
        summary, citation, risk, recommendation) — citation/risk/recommendation are
        REQUIRED per F3 to prevent weak/non-actionable reviews
      - each finding has NO extra keys (additionalProperties:false enforcement);
        only optional NEW key allowed is patch_proposal (Task #24 / iter-0014).
        Anti-self-verification: verified / self_verified / correct / approved /
        confirmed are NOT in the allowed set; they remain rejected.
      - severity in {low,medium,high,blocking,critical} enum
      - id matches ^codex-rev-\\d+$
      - all string fields (id, severity, summary, citation, risk, recommendation,
        goal_satisfied_rationale) are actual strings (per F2)
      - goal_satisfied is bool
      - blocking_objections is a list of STRINGS (per F2 — list-items typed)
      - blocking_objections invariant (per F3): set(blocking_objections) ==
        set(f.id for f in findings if f.severity in {"blocking", "critical"})
      - if a finding has patch_proposal: its keys/types/content-binding are
        validated, and (when goal_packet is supplied) files_touched paths must
        be in allowed_files and not in forbidden_files (Task #24 / iter-0014).

    goal_packet is OPTIONAL for backward-compat / unit-level testing; the main
    pipeline always supplies it so scope checks run.

    Per iter-0022: ``review_packet`` is OPTIONAL. When supplied AND it carries
    a string ``defect_target.base_sha``, every parsed patch_proposal has its
    ``base_sha`` field OVERWRITTEN with that canonical operator-stamped value.
    The schema permits codex to emit any string for ``base_sha``; the helper
    authoritatively replaces it post-parse. Backward compat: when review_packet
    is None or lacks a string defect_target.base_sha, codex's emission is kept.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CodexOutputParseError(f"codex output is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise CodexOutputParseError(
            f"codex output JSON root must be an object, got {type(parsed).__name__}"
        )

    # Reject unknown top-level keys (F2)
    unknown_top = set(parsed.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_top:
        raise CodexOutputParseError(
            f"codex output JSON has unexpected top-level keys: {sorted(unknown_top)}"
        )

    # Required top-level keys
    # Per v1.10.4 F4: validator now requires goal_satisfied_rationale, mirroring
    # the schema (which requires it for OpenAI structured-output strictness).
    # Prior: optional + type-checked-if-present; new: required + type-checked.
    for required in ("findings", "goal_satisfied", "blocking_objections", "goal_satisfied_rationale"):
        if required not in parsed:
            raise CodexOutputParseError(f"codex output JSON missing required key: {required!r}")

    # Type checks on top-level
    if not isinstance(parsed["findings"], list):
        raise CodexOutputParseError("'findings' must be an array")
    if not isinstance(parsed["goal_satisfied"], bool):
        raise CodexOutputParseError(
            f"'goal_satisfied' must be boolean, got {type(parsed['goal_satisfied']).__name__}"
        )
    if not isinstance(parsed["blocking_objections"], list):
        raise CodexOutputParseError(
            f"'blocking_objections' must be an array, got {type(parsed['blocking_objections']).__name__}"
        )

    # blocking_objections items must all be strings (F2)
    for i, item in enumerate(parsed["blocking_objections"]):
        if not isinstance(item, str):
            raise CodexOutputParseError(
                f"blocking_objections[{i}] must be string, got {type(item).__name__}"
            )

    # Per v1.10.4 F4: goal_satisfied_rationale is now REQUIRED (not optional);
    # presence already enforced above. Type-check it here.
    if not isinstance(parsed["goal_satisfied_rationale"], str):
        raise CodexOutputParseError(
            f"'goal_satisfied_rationale' must be string, got "
            f"{type(parsed['goal_satisfied_rationale']).__name__}"
        )

    # Per-finding validation
    # First, collect all finding IDs (for patch_proposal.applies_to_findings cross-check)
    all_finding_ids: set = set()
    for i, finding in enumerate(parsed["findings"]):
        if isinstance(finding, dict) and isinstance(finding.get("id"), str):
            all_finding_ids.add(finding["id"])

    blocking_finding_ids = []
    for i, finding in enumerate(parsed["findings"]):
        if not isinstance(finding, dict):
            raise CodexOutputParseError(
                f"findings[{i}] must be an object, got {type(finding).__name__}"
            )
        # Reject unknown finding keys (F2). _ALLOWED_FINDING_KEYS now includes
        # patch_proposal as the ONLY new optional key (Task #24 / iter-0014).
        # Anti-self-verification fields (verified / self_verified / correct /
        # approved / confirmed) remain rejected here.
        unknown_keys = set(finding.keys()) - _ALLOWED_FINDING_KEYS
        if unknown_keys:
            raise CodexOutputParseError(
                f"findings[{i}] has unexpected keys: {sorted(unknown_keys)}"
            )
        # Required finding fields (F3: citation/risk/recommendation now required)
        for required in _REQUIRED_FINDING_FIELDS:
            if required not in finding:
                raise CodexOutputParseError(
                    f"findings[{i}] missing required field: {required!r}"
                )
        # All required string fields must be actual strings (F2)
        for str_field in ("id", "severity", "summary", "citation", "risk", "recommendation"):
            if not isinstance(finding[str_field], str):
                raise CodexOutputParseError(
                    f"findings[{i}].{str_field} must be string, got "
                    f"{type(finding[str_field]).__name__}"
                )
        # Severity enum
        if finding["severity"] not in _VALID_SEVERITIES:
            raise CodexOutputParseError(
                f"findings[{i}] has invalid severity {finding['severity']!r}; "
                f"must be one of {sorted(_VALID_SEVERITIES)}"
            )
        # ID pattern
        if not _FINDING_ID_PATTERN.match(finding["id"]):
            raise CodexOutputParseError(
                f"findings[{i}] id {finding['id']!r} does not match pattern "
                f"^codex-rev-\\d+$"
            )
        # Optional patch_proposal validation (Task #24 / iter-0014).
        # Schema permits null; treat null as absent.
        pp = finding.get("patch_proposal")
        if pp is not None:
            _validate_patch_proposal(
                finding_index=i,
                finding_id=finding["id"],
                pp=pp,
                all_finding_ids=all_finding_ids,
                goal_packet=goal_packet,
                review_packet=review_packet,
                repo_root=repo_root,
            )
        # iter-0018 Finding 4: patch_not_proposed_reason field validation.
        # When present, must be a non-empty string. Mutually exclusive with
        # patch_proposal — a finding can't have both.
        reason = finding.get("patch_not_proposed_reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise CodexOutputParseError(
                    f"findings[{i}].patch_not_proposed_reason must be string, "
                    f"got {type(reason).__name__}"
                )
            if not reason.strip():
                raise CodexOutputParseError(
                    f"findings[{i}].patch_not_proposed_reason must be non-empty"
                )
            if pp is not None:
                raise CodexOutputParseError(
                    f"findings[{i}]: patch_proposal and patch_not_proposed_reason "
                    f"are mutually exclusive (both provided)"
                )
        # Track blocking-class findings for invariant check
        if finding["severity"] in _BLOCKING_SEVERITIES:
            blocking_finding_ids.append(finding["id"])

    # iter-0018 Finding 4: strict fix_author_policy enforcement.
    # When goal_packet.fix_author_policy == "strict", every finding MUST have
    # either a patch_proposal OR a patch_not_proposed_reason. Permissive (the
    # default + only prior behavior) does not require either. Unrecognized
    # policy values are rejected.
    if goal_packet is not None:
        policy = goal_packet.get("fix_author_policy", "permissive")
        if policy not in ("permissive", "strict"):
            raise CodexOutputParseError(
                f"goal_packet.fix_author_policy={policy!r} is not a recognized "
                f"value; allowed: 'permissive' (default), 'strict'"
            )
        if policy == "strict":
            for i, finding in enumerate(parsed["findings"]):
                has_patch = isinstance(finding.get("patch_proposal"), dict)
                has_reason = bool(finding.get("patch_not_proposed_reason"))
                if not (has_patch or has_reason):
                    raise CodexOutputParseError(
                        f"fix_author_policy=strict: findings[{i}] (id={finding.get('id')!r}) "
                        f"must have either patch_proposal OR patch_not_proposed_reason; "
                        f"neither present"
                    )

    # Blocking objections invariant (F3): set must match blocking-class finding ids
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
        raise CodexOutputParseError("; ".join(msg_parts))

    return parsed


_CODEX_PROPOSAL_SCHEMA_PATH = (
    Path(__file__).parent / "dispatch_templates" / "codex_proposal_schema.json"
)


def _parse_codex_proposal_output(text: str, schema_path: Path | None = None) -> dict:
    """Parse + validate codex proposal-mode output (iter-0028).

    Validation runs against `schema_path` (operator override via --schema)
    when provided, else against the built-in `codex_proposal_schema.json`.
    The override contract from the dispatcher CLI must thread through here
    so a custom schema actually constrains the validator (codex pass-3
    rev-001: previously this was hard-coded, breaking the override
    promise).

    selected_target and deliverable_scope may be null when
    structural_abstention is true; required to be non-null when
    structural_abstention is false. The schema allows null but the
    cross-field semantic check enforces presence.

    Raises CodexOutputParseError on shape mismatch.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CodexOutputParseError(
            f"codex proposal output is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise CodexOutputParseError(
            f"codex proposal output root must be a JSON object; got {type(parsed).__name__}"
        )

    effective_schema_path = schema_path or _CODEX_PROPOSAL_SCHEMA_PATH
    try:
        import jsonschema
    except ImportError as exc:
        raise CodexOutputParseError(
            f"jsonschema package required for proposal-mode validation; "
            f"install with `pip install jsonschema` or reinstall consensus-mcp: {exc}"
        ) from exc
    try:
        schema = json.loads(Path(effective_schema_path).read_text(encoding="utf-8"))
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise CodexOutputParseError(
            f"codex proposal output failed schema validation at "
            f"{'/'.join(str(p) for p in exc.absolute_path) or '<root>'}: {exc.message}"
        ) from exc
    except FileNotFoundError as exc:
        raise CodexOutputParseError(
            f"proposal schema not found at {effective_schema_path}: {exc}"
        ) from exc

    # Parser-level invariants (codex pass-4 rev-002 + pass-6 rev-001):
    # defense-in-depth against operator-supplied schemas that relax these.
    # The built-in schema enforces them, but an override could weaken
    # minLength on rationale, omit structural_abstention entirely, or
    # let through a non-boolean truthy value. Enforce here regardless of
    # which schema is in effect.
    if not isinstance(parsed.get("rationale_vs_alternatives"), str) or not parsed["rationale_vs_alternatives"].strip():
        raise CodexOutputParseError(
            "rationale_vs_alternatives must be a non-empty string (parser invariant)"
        )
    if "structural_abstention" not in parsed or not isinstance(parsed["structural_abstention"], bool):
        raise CodexOutputParseError(
            "structural_abstention must be present and boolean (parser invariant)"
        )

    # Cross-field semantic check the schema can't express:
    # when NOT abstaining, selected_target and deliverable_scope must be non-null.
    if not parsed["structural_abstention"]:
        if parsed["selected_target"] is None:
            raise CodexOutputParseError(
                "selected_target is required when structural_abstention is false"
            )
        if parsed["deliverable_scope"] is None:
            raise CodexOutputParseError(
                "deliverable_scope is required when structural_abstention is false"
            )

    return parsed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus_mcp._dispatch_codex",
        description="Auto-dispatch codex CLI as the second reviewer; auto-seal via T6.",
    )
    p.add_argument("--goal-packet", required=True)
    p.add_argument("--iteration-dir", required=True)
    p.add_argument("--reviewer-id", default=None)
    p.add_argument("--pass-id", default=None)
    p.add_argument("--prompt-template", default=None)
    p.add_argument("--schema", default=None, help="Path to JSON output schema (default: dispatch_templates/codex_review_schema.json)")
    p.add_argument("--mode", default="review", choices=["review", "proposal"],
                   help=("Dispatch mode (iter-0028 per iter-0027 converged plan). "
                         "'review' (default): use codex_review_template.md + schema "
                         "for code-review tasks. 'proposal': use codex_proposal_template.md "
                         "+ schema for design-consult / workflow #4 proposal tasks. "
                         "--prompt-template and --schema overrides take precedence over --mode."))
    p.add_argument("--codex-bin", default="codex")
    p.add_argument("--timeout-seconds", type=int, default=600)
    p.add_argument("--review-target", default=None,
                   help="Optional path to a file containing the review input "
                        "(diff, patch, etc.); helper computes sha256 and "
                        "passes both path + hash to the prompt for codex.")
    p.add_argument("--smoke", action="store_true",
                   help="Smoke mode: gated by CONSENSUS_MCP_RUN_REAL_CODEX_SMOKE=1 env var (codex review #3)")

    ns = p.parse_args(argv)

    # Per v1.10.4 F1: fail-closed if repo_root can't be validated.
    try:
        repo_root = _resolve_repo_root()
    except RepoRootResolutionError as exc:
        # We don't have a valid log_path yet; print error to stderr-equivalent JSON.
        print(json.dumps({"ok": False, "error": str(exc), "error_type": "RepoRootResolutionError"}))
        return 4

    # iter-0033 codex-rev-001/002: compute log_path immediately after repo_root
    # resolves so preflight failures can emit a structured dispatch_failed
    # event. Derive PROVISIONAL reviewer/pass identifiers from --iteration-dir
    # so early-fail events have anchors; these get RECOMPUTED below once
    # iter_dir.name canonicalizes (e.g., when --iteration-dir is "." or a
    # symlink). Codex iter-0033 review feedback: use _pre_iter_id ONLY for
    # the early failure event; recompute canonical reviewer_id/pass_id from
    # iter_dir.name after normalization succeeds.
    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"
    _pre_iter_id = Path(ns.iteration_dir).name or "unknown-iteration"
    _pre_reviewer_id = ns.reviewer_id or f"codex-{_pre_iter_id}-1"
    _pre_pass_id = ns.pass_id or f"{_pre_reviewer_id}-pass1"

    # Per v1.10.4 F5: normalize all operator-supplied relative paths against
    # repo_root, NOT the process cwd. The codex subprocess runs with --cd repo_root,
    # so all path frames must agree.
    # iter-0033 codex-rev-001: wrap in try/except so containment rejections
    # AND OSError filesystem failures (e.g., unwritable iter_dir) emit a
    # structured dispatch_failed event instead of an uncaught traceback.
    try:
        iter_dir = _normalize_relative_to_repo(ns.iteration_dir, repo_root)
        iter_dir.mkdir(parents=True, exist_ok=True)
        iteration_id = iter_dir.name

        # iter-0028: mode selects the default template/schema pair when no
        # explicit overrides are passed. --prompt-template and --schema
        # overrides still win, preserving backward compatibility.
        _default_template_name = (
            "codex_proposal_template.md" if ns.mode == "proposal"
            else "codex_review_template.md"
        )
        _default_schema_name = (
            "codex_proposal_schema.json" if ns.mode == "proposal"
            else "codex_review_schema.json"
        )
        template_path = (
            _normalize_relative_to_repo(ns.prompt_template, repo_root)
            if ns.prompt_template
            else (Path(__file__).parent / "dispatch_templates" / _default_template_name)
        )
        schema_path = (
            _normalize_relative_to_repo(ns.schema, repo_root)
            if ns.schema
            else (Path(__file__).parent / "dispatch_templates" / _default_schema_name)
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

    # v1.10.5: compute review_target_path display string (relative to repo_root
    # when possible) up front, so dispatch_start / dispatch_failed / dispatch_done
    # all see the same value. Hash is computed inside the try block (requires
    # reading the file).
    review_target_path_str: str | None = None
    if review_target_normalized is not None:
        try:
            review_target_path_str = str(
                review_target_normalized.relative_to(repo_root.resolve())
            ).replace("\\", "/")
        except ValueError:
            review_target_path_str = str(review_target_normalized)

    # iter-0033 codex-rev-002: recompute canonical reviewer_id/pass_id from
    # the now-normalized iteration_id. The pre-normalize values were used only
    # for the early-fail event above.
    reviewer_id = ns.reviewer_id or f"codex-{iteration_id}-1"
    pass_id = ns.pass_id or f"{reviewer_id}-pass1"

    # Per codex review F3 (2026-05-09): if --smoke is passed without the env-gate set,
    # refuse before invoking codex. Operators/automation that pass --smoke are signaling
    # "this is a deliberate real-codex smoke run"; the env var is the explicit consent.
    if ns.smoke and os.environ.get("CONSENSUS_MCP_RUN_REAL_CODEX_SMOKE") != "1":
        refuse_msg = (
            "--smoke requires CONSENSUS_MCP_RUN_REAL_CODEX_SMOKE=1 in the environment. "
            "This gate prevents accidental real-codex invocation under cost/auth/session "
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

    # Audit-log dispatch_start with the inputs that don't require codex execution
    _log_dispatch(log_path, {
        "event": "dispatch_start",
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "smoke": ns.smoke,
        "timeout_seconds": ns.timeout_seconds,
        "codex_bin": ns.codex_bin,
        "schema_path": str(schema_path),
        # v1.10.5: visibility TUI / stall watchdog read this to display what
        # the current dispatch is reviewing. Hash isn't available yet (file
        # hasn't been read); it lands in dispatch_done / dispatch_failed.
        "review_target_path": review_target_path_str,
    })

    # Per v1.10.4 F3: initialize provenance vars to None BEFORE try block so
    # dispatch_failed paths can include whatever was computed before failure.
    prompt_sha: str | None = None
    schema_sha: str | None = None
    goal_packet_sha: str | None = None
    scope_sig: str | None = None
    codex_version: str | None = None
    output_sha: str | None = None
    # v1.10.5: review_target_hash gets stamped after the file is read; init to
    # None so dispatch_failed includes it even when the read never happened.
    review_target_hash: str | None = None

    def _failed_event(error_type: str, error: str) -> dict:
        """Per v1.10.4 F3: dispatch_failed events carry every provenance field
        that was successfully computed before the failure, plus reviewer/pass ids.
        Never logs raw prompt / output / goal_packet content."""
        ev = {
            "event": "dispatch_failed",
            "error_type": error_type,
            "error": error,
            "reviewer_id": reviewer_id,
            "pass_id": pass_id,
            "iteration_id": iteration_id,
            "timeout_seconds": ns.timeout_seconds,
        }
        # Include only the hashes that were actually computed.
        for k, v in (
            ("codex_version", codex_version),
            ("prompt_sha256", prompt_sha),
            ("output_sha256", output_sha),
            ("schema_sha256", schema_sha),
            ("goal_packet_sha256", goal_packet_sha),
            ("scope_signature", scope_sig),
            # v1.10.5: review-target identity so dispatch_failed events can be
            # correlated to the target that was being reviewed when the failure
            # fired. Path is known pre-try; hash only after the file is read.
            ("review_target_path", review_target_path_str),
            ("review_target_hash", review_target_hash),
        ):
            if v is not None:
                ev[k] = v
        return ev

    try:
        # iter-0010 codex-rev-001 (medium): route goal_packet parsing through
        # the shared _load_goal_packet helper so root-type validation is the
        # single source of truth across all adapters. text is read separately
        # because provenance needs the raw bytes for goal_packet_sha256.
        goal_packet_text = goal_packet_path.read_text(encoding="utf-8")
        goal_packet = _load_goal_packet(goal_packet_path)
        template_text = _load_template(template_path)

        # review_target_hash is pre-initialized to None outside the try block
        # (v1.10.5) so dispatch_failed events can include it.
        review_packet_data: dict | None = None
        review_target_text: str | None = None  # Bug B (v1.30.2): embed content, not just path
        if review_target_normalized is not None:
            review_target_text = review_target_normalized.read_text(encoding="utf-8")
            review_target_hash = _sha256_str(review_target_text)
            # iter-0021: when --review-target points at a YAML review-packet
            # (the new convention), parse it so {touched_files_contents_block}
            # can be substituted in _build_prompt. If parsing fails (e.g., the
            # operator passed a raw .diff or .patch file), silently fall back
            # to the legacy behavior — the prompt still renders, just without
            # embedded file contents.
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
        schema_text = schema_path.read_text(encoding="utf-8")

        # Compute provenance hashes BEFORE codex call (so we have them even if codex fails)
        prompt_sha = _sha256_str(prompt)
        schema_sha = _sha256_str(schema_text)
        goal_packet_sha = _sha256_str(goal_packet_text)
        scope_sig = ((goal_packet or {}).get("authorization", {}) or {}).get("scope_signature", "")

        codex_version = _get_codex_version(ns.codex_bin)
        codex_output = _invoke_codex(
            prompt=prompt,
            codex_bin=ns.codex_bin,
            timeout_seconds=ns.timeout_seconds,
            repo_root=repo_root,
            schema_path=schema_path,
            # iter-0037: pass log_path + anchors so streaming events + heartbeats
            # + abort-signal-file polling fire. Tests that don't supply these
            # operate in legacy mode (subprocess.run-compat, no streaming events).
            log_path=log_path,
            anchors={
                "iteration_id": iteration_id,
                "reviewer_id": reviewer_id,
                "pass_id": pass_id,
            },
        )
        output_sha = _sha256_str(codex_output)

        # Per Task #24 (iter-0014): pass goal_packet so patch_proposal scope
        # checks (allowed_files / forbidden_files) can run.
        # Per iter-0022: pass review_packet so helper stamps patch_proposal.base_sha
        # from review_packet.defect_target.base_sha (canonical operator-stamped value)
        # instead of trusting codex's hallucinated emission.
        # iter-0026 F1: pass repo_root so the per-patch base_sha stamp uses
        # disk bytes (matches apply.codex_patch's bundle_sha contract; eliminates
        # the CRLF mismatch surfaced in iter-0025).
        # iter-0028: route output parsing based on --mode. Review mode uses
        # the existing review-shape parser; proposal mode uses a separate
        # proposal-shape parser that validates against codex_proposal_schema.
        if ns.mode == "proposal":
            # Thread the operator's effective --schema override through so
            # a custom proposal schema actually constrains the validator.
            extracted = _parse_codex_proposal_output(codex_output, schema_path=schema_path)
        else:
            extracted = _parse_codex_output(
                codex_output,
                goal_packet=goal_packet,
                review_packet=review_packet_data,
                repo_root=repo_root,
            )
        # v1.10.5: persist review_target_path + review_target_hash in sealed
        # provenance so audit reconstruction and the visibility TUI can show
        # which target was reviewed. (path is computed pre-try at function
        # top; hash was computed at line ~1340 after reading review_target.)
        provenance = {
            "codex_version": codex_version,
            "prompt_sha256": prompt_sha,
            "output_sha256": output_sha,
            "schema_sha256": schema_sha,
            "goal_packet_sha256": goal_packet_sha,
            "scope_signature": scope_sig,
            "review_target_path": review_target_path_str,
            "review_target_hash": review_target_hash,
            # iter-0028 codex pass-6 rev-002: record mode for parity with gemini.
            "mode": ns.mode,
        }
        packet = _build_sealed_packet(extracted, iteration_id, reviewer_id, pass_id, provenance=provenance)
        result = _seal_via_t6(packet, iter_dir)
    except (CodexInvocationError, CodexOutputParseError) as exc:
        _log_dispatch(log_path, _failed_event(type(exc).__name__, str(exc)))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 1
    except Exception as exc:
        _log_dispatch(log_path, _failed_event(type(exc).__name__, str(exc)))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 2

    # Per v1.10.4 F2: dispatch_done logs the IMMUTABLE archive path + the audit
    # event id, in addition to the local mirror path. The local mirror is
    # overwritten by each successful re-run; the archive copy is the audit anchor.
    _log_dispatch(log_path, {
        "event": "dispatch_done",
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "codex_version": codex_version,
        "timeout_seconds": ns.timeout_seconds,
        "exit_code": 0,
        "prompt_sha256": prompt_sha,
        "output_sha256": output_sha,
        "schema_sha256": schema_sha,
        "goal_packet_sha256": goal_packet_sha,
        "scope_signature": scope_sig,
        # v1.10.5: review-target identity in the audit event so the visibility
        # TUI / watchdog / log scanners can identify what was reviewed without
        # opening the sealed packet.
        "review_target_path": review_target_path_str,
        "review_target_hash": review_target_hash,
        "packet_sha256": result.get("packet_sha256"),
        "archive_sealed_path": result.get("archive_sealed_path"),  # F2: immutable T6 archive
        "local_mirror_path": result.get("sealed_path"),            # F2: convenience copy
        "sealed_path": result.get("sealed_path"),                  # backcompat
        "t6_audit_event_id": result.get("audit_event_id"),         # F2: T6's own audit anchor
    })
    print(json.dumps({"ok": True, "pass_id": pass_id, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

