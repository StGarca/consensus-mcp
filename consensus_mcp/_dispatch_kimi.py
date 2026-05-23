"""auto-kimi-dispatch helper (kimi adapter).

UX-parity sibling of _dispatch_gemini.py. To the operator, dispatching kimi
looks and behaves EXACTLY like consensus-mcp-dispatch-gemini: identical CLI
flags, an identically-shaped sealed review artifact (via the shared T6 path),
and the same log/timing lines. All kimi-specific transport quirks are hidden
inside this module.

It reuses the generic dispatch infrastructure from _dispatch_base.py
(repo-root resolution, path normalization, goal_packet/template loading,
prompt building, process-tree termination, sealing, dispatch-log writing) and
imports gemini's `_extract_json_from_text` (kimi, like gemini, lacks a native
output-schema enforcer, so its review JSON may be wrapped in free text).

FIX TRACK 2 (B4/H1/H3/L3 — converged-plan 2026-05-22): the kimi reviewer is
now READ-ONLY + portable. The CLI transport changed from the print/-p form to
the verified kimi.yaml profile (stdin transport, --quiet --thinking,
prompt_flag null):

  kimi --quiet --thinking --work-dir <TEMP_COPY_OF_REPO>      # prompt on STDIN

  - PROMPT ON STDIN (not -p). --quiet does NOT auto-enable --afk (the tool
    auto-approval that --print did), so a REVIEW can no longer mutate the
    workspace (B4). Putting the full prompt on stdin also removes the
    ~128KB POSIX / 32767-char Windows single-arg limit (H3).
  - OUTPUT: --quiet emits the model's final answer as PLAIN TEXT (verified
    against the real CLI 2026-05-22), followed by a "To resume this session:
    kimi -r <id>" trailer. We apply the kimi.yaml `strip_patterns` to peel
    that trailer, then run the remaining text through `_extract_json_from_text`
    exactly like gemini does with free text. (The legacy stream-json
    `_peel_assistant_content` peel is retained as a robustness fallback for
    any older CLI that still emits the assistant envelope.)
  - DISPOSABLE TEMP WORKDIR (B4): before invoking kimi we make a throwaway
    copy of the repo (git clone --local --shared, fallback shutil.copytree)
    and pass it as --work-dir so kimi physically cannot touch the real repo.
    The temp copy is removed after the dispatch.
  - POST-DISPATCH INTEGRITY CHECK (B4 independent safeguard): after kimi
    returns we run `git status --short` in the REAL repo_root; if it is
    non-empty (kimi mutated the real repo despite the temp workdir) we REJECT
    the review (KimiIntegrityError) and log the violation. This is valuable
    even if the temp-copy isolation fails for any reason.
  - ENV: kimi auth is OAuth file creds at ~/.kimi/credentials/kimi-code.json.
    Do NOT set KIMI_API_KEY. CRITICAL (inverse of gemini's
    GEMINI_CLI_TRUST_WORKSPACE injection): SCRUB KIMI_API_KEY and
    OPENAI_API_KEY from the subprocess env so a stray external key can't
    hijack the OAuth call (_kimi_subprocess_env).
  - EXIT CODES: 0 = success, 1 = non-retryable (auth/quota/config), 75 =
    retryable (429/5xx/timeout). 75 maps to the same retry path gemini uses
    for 429; 1 maps to hard fail.

H1: the sealed local-mirror filename is now bound to reviewer+pass (not a
fixed kimi-review.yaml) so multi-pass dispatches no longer overwrite.

L3: the pre-first-byte watchdog is relaxed (cold-start headroom = the wall
timeout budget plus the stall grace); the post-first-byte stall threshold is
unchanged.

Default stall_silence_seconds is 240 (cold start); honors
CONSENSUS_MCP_STALL_SILENCE_SECONDS like the others. timeout_seconds default
matches kimi.yaml's 1800.

USAGE
-----
  python -m consensus_mcp._dispatch_kimi \\
      --goal-packet <path/to/goal_packet.yaml> \\
      --iteration-dir <path/to/iteration-XXXX/> \\
      [--reviewer-id kimi-iterXXXX-N] \\
      [--pass-id kimi-iterXXXX-N-passN] \\
      [--prompt-template <path>] \\
      [--mode {review,proposal}] \\
      [--schema <path>]                    # proposal-mode only \\
      [--kimi-bin <path>] \\
      [--timeout-seconds 1800] \\
      [--review-target <path>] \\
      [--smoke]

Exit 0 = sealed pass produced; non-zero = failure (kimi error, parse fail,
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
# kimi, like gemini, has no native output-schema enforcer; reuse gemini's
# free-text JSON extractor rather than reinventing it (per task: reuse, not
# replicate).
from consensus_mcp._dispatch_gemini import _extract_json_from_text


# Adapter-specific finding patterns. kimi emits kimi-rev-N IDs; mirror the
# severity enum + allowed-key set from gemini/codex since the semantic
# contract (severity values, required fields) is identical.
_KIMI_FINDING_ID_PATTERN = re.compile(r"^kimi-rev-\d+$")
_VALID_SEVERITIES = {"low", "medium", "high", "blocking", "critical"}
_REQUIRED_FINDING_FIELDS = ("id", "severity", "summary", "citation", "risk", "recommendation")
_ALLOWED_FINDING_KEYS = set(_REQUIRED_FINDING_FIELDS) | {"patch_proposal", "patch_not_proposed_reason"}
_ALLOWED_TOP_LEVEL_KEYS = {"findings", "goal_satisfied", "blocking_objections", "goal_satisfied_rationale"}
_BLOCKING_SEVERITIES = {"blocking", "critical"}

# Default stall-silence (cold start can exceed gemini's 180s; kimi.yaml notes a
# managed +thinking model). Honors CONSENSUS_MCP_STALL_SILENCE_SECONDS override.
_DEFAULT_STALL_SILENCE_SECONDS = 240.0
# Default wall timeout: match kimi.yaml's timeout_seconds: 1800.
_DEFAULT_TIMEOUT_SECONDS = 1800

# kimi retryable exit code (429/5xx/timeout). 0 = success; everything else
# non-retryable EXCEPT this code.
_KIMI_RETRYABLE_EXIT = 75

# Output chrome to strip before _extract_json_from_text. Sourced from the
# verified kimi.yaml profile (output.strip_patterns). --quiet emits the final
# answer text followed by a "To resume this session: kimi -r <id>" trailer;
# this peels that trailer. Applied with no flags: the trailing `$` anchors at
# end-of-string and `\s*` consumes the trailing newline.
_KIMI_STRIP_PATTERNS = (
    re.compile(r"\n*To resume this session:\s*kimi -r \S+\s*$"),
)

# Directory names skipped by the shutil.copytree fallback when cloning the repo
# into a disposable temp work-dir (git clone is preferred; this list only
# matters when git is unavailable). Keeps the copy fast + small.
_TEMP_WORKDIR_IGNORE_DIRS = (
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "dist",
    "build",
    ".ruff_cache",
)


def _strip_kimi_output_chrome(text: str) -> str:
    """Strip kimi.yaml output chrome (the resume-session trailer) from `text`.

    Applies the verified kimi.yaml `strip_patterns` BEFORE _extract_json_from_text
    so the JSON extractor never trips on the "To resume this session: kimi -r ..."
    line that --quiet appends after the final answer.
    """
    for pat in _KIMI_STRIP_PATTERNS:
        text = pat.sub("", text)
    return text


class KimiInvocationError(RuntimeError):
    """Raised when the kimi CLI exits non-zero, times out, or is not found.

    Carries a `retryable` flag so the with-retry wrapper can distinguish a
    transient failure (kimi exit 75: 429/5xx/timeout) from a hard failure
    (exit 1: auth/quota/config). Mirrors gemini's 429-retry path for 75.
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class KimiOutputParseError(ValueError):
    """Raised when kimi output is not parseable JSON or lacks the expected top-level shape."""


class KimiIntegrityError(RuntimeError):
    """Raised when the post-dispatch integrity check finds the REAL repo mutated.

    Independent safeguard (converged-plan B4): even though kimi runs against a
    disposable temp copy of the repo, after the dispatch returns we run
    `git status --short` in the REAL repo_root. A non-empty result means kimi
    (or some bug) mutated the real workspace despite the isolation, so the
    review output is REJECTED rather than trusted. Valuable even if the
    temp-copy isolation or the --quiet/no-afk assumption is wrong.
    """


def _kimi_subprocess_env() -> dict:
    """Environment for the kimi subprocess.

    INVERSE of gemini's _gemini_subprocess_env: where gemini INJECTS a trust
    var, kimi must SCRUB stray API keys. kimi auth is OAuth file creds at
    ~/.kimi/credentials/kimi-code.json (the machine is logged in). A stray
    KIMI_API_KEY or OPENAI_API_KEY in the environment could hijack the OAuth
    call and route the request through an external key, so both are removed.

    Returns a COPY of the parent environment with KIMI_API_KEY and
    OPENAI_API_KEY removed. Never mutates os.environ. Does NOT set
    KIMI_API_KEY (per task: OAuth file creds are authoritative).
    """
    env = os.environ.copy()
    env.pop("KIMI_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    return env


def _make_disposable_workdir(repo_root: Path) -> Path:
    """Create a disposable temp COPY of the repo to use as kimi's --work-dir.

    Converged-plan B4: kimi must run against a throwaway copy so it physically
    cannot touch the real repo. Strategy ladder:

      1. `git clone --local --shared <repo_root> <tmp>` — fast + hardlinked
         (objects shared via the source repo's object store; the working tree
         is a fresh checkout). Preferred when the repo is a git work-tree and
         the git binary is available.
      2. shutil.copytree fallback (git unavailable / repo is not a git tree):
         copies the tree skipping .git + heavy/derived dirs
         (_TEMP_WORKDIR_IGNORE_DIRS) so the copy stays fast + small.

    Returns the Path to the temp work-dir copy. The caller MUST remove it via
    _cleanup_disposable_workdir in a finally block. Raises OSError on a copy
    failure the caller can surface as a dispatch failure.
    """
    import tempfile

    tmp_root = Path(tempfile.mkdtemp(prefix="kimi-workdir-"))
    dest = tmp_root / "repo"

    git_bin = shutil.which("git")
    cloned = False
    if git_bin is not None and (repo_root / ".git").exists():
        try:
            result = subprocess.run(
                [git_bin, "clone", "--local", "--shared",
                 str(repo_root), str(dest)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            cloned = result.returncode == 0 and dest.exists()
        except (subprocess.TimeoutExpired, OSError):
            cloned = False

    if not cloned:
        # Fallback: copytree, skipping .git + heavy/derived dirs.
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)

        def _ignore(_dir, names):
            return [n for n in names if n in _TEMP_WORKDIR_IGNORE_DIRS]

        shutil.copytree(str(repo_root), str(dest), ignore=_ignore, symlinks=True)

    return dest


def _cleanup_disposable_workdir(workdir: Path | None) -> None:
    """Remove a disposable temp work-dir (and its tempfile parent) best-effort.

    `workdir` is the `<tmp>/repo` path returned by _make_disposable_workdir;
    we remove its PARENT (the mkdtemp root) so no stray tempdir is left behind.
    Never raises.
    """
    if workdir is None:
        return
    try:
        target = workdir.parent if workdir.name == "repo" else workdir
        shutil.rmtree(str(target), ignore_errors=True)
    except OSError:
        pass


def _real_repo_is_dirty(repo_root: Path) -> tuple[bool, str]:
    """Return (is_dirty, raw_status) for the REAL repo via `git status --short`.

    Independent post-dispatch safeguard (converged-plan B4): a non-empty
    `git status --short` after kimi returns means the real repo was mutated.

    Fail-SAFE on inability to check: if git is missing, the path is not a git
    work-tree, or the command errors/times out, we CANNOT prove cleanliness, so
    we report dirty=False (do not block) — the temp-workdir isolation is the
    primary control and a missing git binary is an environment issue, not a
    kimi violation. (The check is a backstop, not the only control.) The raw
    status text is returned for logging.
    """
    git_bin = shutil.which("git")
    if git_bin is None or not (repo_root / ".git").exists():
        return False, ""
    try:
        result = subprocess.run(
            [git_bin, "status", "--short"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False, ""
    if result.returncode != 0:
        return False, ""
    raw = result.stdout.strip()
    return (bool(raw), raw)


def _effective_stall_silence(default: float = _DEFAULT_STALL_SILENCE_SECONDS) -> float:
    """Return the stall-silence threshold, honoring the env override.

    Mirrors the codex/gemini CONSENSUS_MCP_STALL_SILENCE_SECONDS contract:
    operators with slow models can extend the watchdog threshold without a
    code change. Invalid values keep the default.
    """
    env_silence = os.environ.get("CONSENSUS_MCP_STALL_SILENCE_SECONDS")
    if env_silence:
        try:
            return float(env_silence)
        except ValueError:
            pass
    return default


def _resolve_kimi_bin(kimi_bin: str) -> str:
    """Resolve a kimi binary spec to an actual executable file path.

    Mirrors _resolve_gemini_bin / _resolve_codex_bin Windows hardening:
    MSYS-path conversion, PATHEXT-aware lookup, .cmd preference over .ps1,
    App Execution Alias rejection.
    """
    if (
        sys.platform == "win32"
        and len(kimi_bin) >= 3
        and kimi_bin[0] == "/"
        and kimi_bin[1].isalpha()
        and kimi_bin[2] == "/"
    ):
        drive = kimi_bin[1].upper()
        rest = kimi_bin[3:].replace("/", "\\")
        kimi_bin = f"{drive}:\\{rest}"

    if os.path.sep in kimi_bin or (len(kimi_bin) > 1 and kimi_bin[1] == ":"):
        if sys.platform == "win32":
            p = Path(kimi_bin)
            try:
                if p.exists() and p.stat().st_size == 0:
                    windows_apps = (
                        os.environ.get("LOCALAPPDATA", "")
                        + r"\Microsoft\WindowsApps"
                    )
                    if windows_apps and str(p).lower().startswith(windows_apps.lower()):
                        raise KimiInvocationError(
                            f"kimi binary {kimi_bin!r} is a Windows App Execution "
                            f"Alias stub (0-byte file in WindowsApps). subprocess cannot "
                            f"exec App Aliases — install the real kimi CLI binary or "
                            f"adjust PATH so a non-stub variant is preferred."
                        )
            except OSError:
                pass
        return kimi_bin

    if sys.platform == "win32" and "." not in Path(kimi_bin).name:
        for ext in (".exe", ".cmd", ".bat", ".ps1"):
            cand = shutil.which(kimi_bin + ext)
            if cand:
                return _resolve_kimi_bin(cand)

    resolved = shutil.which(kimi_bin)
    if resolved is None:
        return kimi_bin
    if sys.platform == "win32" and resolved.lower().endswith(".ps1"):
        cmd_alt = shutil.which(kimi_bin + ".cmd")
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
                    raise KimiInvocationError(
                        f"kimi binary resolved to {resolved!r} which is a Windows "
                        f"App Execution Alias stub (0-byte file in WindowsApps). "
                        f"subprocess cannot exec App Aliases — install the real "
                        f"kimi CLI binary or adjust PATH so a non-stub variant is "
                        f"preferred."
                    )
        except OSError:
            pass
    return resolved


def _get_kimi_version(kimi_bin: str) -> str:
    """Best-effort: shell out to `<kimi> --version` and return the version string.

    Returns 'unknown' on any failure. Used for audit-log provenance only.
    """
    try:
        result = subprocess.run(
            [_resolve_kimi_bin(kimi_bin), "--version"],
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


def _peel_assistant_content(raw_output: str) -> str:
    """Peel the final assistant `content` off kimi's stream-json output.

    kimi --print --output-format stream-json --final-message-only emits a
    single JSONL line:  {"role":"assistant","content":"<final text>"}. The
    contributor review JSON lives INSIDE `content` as a string. This helper
    returns that `content` string so the caller can run it through
    `_extract_json_from_text` exactly like gemini does with free text.

    Robustness ladder:
      1. Scan lines; return the `content` of the LAST line whose role is
         "assistant" (final-message-only should emit exactly one, but be
         resilient to a leading system/status line).
      2. If no assistant envelope is found but the raw output already looks
         like JSON (kimi changed shape, or a test passes raw), return it
         unchanged so the downstream extractor still gets a chance.

    Raises KimiOutputParseError on empty output.
    """
    if not raw_output or not raw_output.strip():
        raise KimiOutputParseError(
            "kimi produced empty output; expected a stream-json line "
            '{"role":"assistant","content":"..."}'
        )

    last_content: str | None = None
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("role") == "assistant" and "content" in obj:
            content = obj["content"]
            if isinstance(content, str):
                last_content = content
            elif content is not None:
                # Defensive: some CLIs nest content as a list of parts.
                last_content = json.dumps(content)

    if last_content is not None:
        return last_content

    # No assistant envelope found. If the raw output already looks like a JSON
    # object/array, hand it back so _extract_json_from_text can try.
    stripped = raw_output.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return stripped

    raise KimiOutputParseError(
        "kimi output did not contain an assistant stream-json envelope and is "
        f"not bare JSON; first 500 chars: {raw_output[:500]!r}"
    )


def _invoke_kimi(
    prompt: str,
    kimi_bin: str,
    timeout_seconds: int,
    repo_root: Path,
    log_path=None,
    anchors=None,
    heartbeat_interval: float = 30.0,
    stall_silence_seconds: float = _DEFAULT_STALL_SILENCE_SECONDS,
    poll_interval: float = 0.5,
    time_fn=None,
    popen_factory=None,
) -> str:
    """Shell out to kimi CLI via Popen + reader threads.

    Pattern mirrors _invoke_gemini's stream-and-watchdog loop:
      - Popen with stdout + stderr reader threads (prevents pipe-buffer deadlock)
      - dispatch_streamed_line per stdout line + dispatch_heartbeat at interval
      - Auto-abort on heartbeat-silence
      - log_path + anchors optional (None disables streaming events for tests)

    kimi CLI shape (verified kimi.yaml profile — stdin transport):
      kimi --quiet --thinking --work-dir <WORKDIR>      # prompt written to STDIN

    The FULL PROMPT is written to the subprocess STDIN (prompt_flag is null in
    the profile — there is no -p). `--quiet` does NOT auto-enable --afk (the
    tool auto-approval --print enabled), so the reviewer is READ-ONLY (B4); the
    stdin transport also removes the single-arg size limit (H3). `repo_root`
    here is the --work-dir, which the caller sets to a DISPOSABLE TEMP COPY of
    the repo so kimi physically cannot touch the real tree. The subprocess env
    scrubs KIMI_API_KEY/OPENAI_API_KEY (see _kimi_subprocess_env) so the OAuth
    file creds are authoritative.

    EXIT-CODE MAPPING:
      0  -> success (return stdout)
      75 -> retryable failure (KimiInvocationError(retryable=True)); the
            with-retry wrapper re-invokes, mirroring gemini's 429 path.
      *  -> non-retryable hard fail (KimiInvocationError(retryable=False)).

    stall_silence defaults to 240s (cold start); honors
    CONSENSUS_MCP_STALL_SILENCE_SECONDS. L3: the pre-first-byte watchdog is
    relaxed (cold-start headroom) — see the silence block below.
    """
    if time_fn is None:
        time_fn = time.time
    if popen_factory is None:
        popen_factory = subprocess.Popen
    can_log = log_path is not None and anchors is not None

    stall_silence_seconds = _effective_stall_silence(stall_silence_seconds)

    # Verified kimi.yaml profile: stdin transport, --quiet --thinking, no -p.
    # --quiet (unlike --print) does NOT auto-enable --afk, so kimi runs the
    # review READ-ONLY. --work-dir points at a disposable temp copy.
    cmd = [
        _resolve_kimi_bin(kimi_bin),
        "--quiet",
        "--thinking",
        "--work-dir", str(repo_root),
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
            env=_kimi_subprocess_env(),
            **popen_kwargs,
        )
    except FileNotFoundError:
        raise KimiInvocationError(f"kimi binary not found: {kimi_bin}", retryable=False) from None

    # STDIN TRANSPORT: write the FULL PROMPT to kimi's stdin, then close it so
    # kimi sees EOF and begins the turn (no -p; no arg-size limit). A large
    # (>128KB) prompt flows through here, not through argv (H3 fix). Writing to
    # a 0-buffered binary pipe needs bytes; tolerate a closed pipe (kimi may
    # exit early on a hard error).
    try:
        proc.stdin.write(prompt.encode("utf-8"))
    except (BrokenPipeError, OSError, AttributeError):
        pass
    try:
        proc.stdin.close()
    except (BrokenPipeError, OSError, AttributeError):
        pass

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

    t_stdout = threading.Thread(target=stdout_reader, daemon=True, name="kimi-stdout-reader")
    t_stderr = threading.Thread(target=stderr_reader, daemon=True, name="kimi-stderr-reader")
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
            raise KimiInvocationError(f"dispatch aborted by operator: {abort_reason}", retryable=False)

        with state_lock:
            lst = last_streamed_ts[0]
            seq_snap = streamed_seq[0]
        if lst is not None:
            # Inter-line silence: kimi already started emitting; use the
            # configured stall_silence threshold.
            silence_age = now - lst
            silence_trigger_threshold = stall_silence_seconds
        else:
            # Pre-first-byte silence. L3 (converged-plan): the kimi cold-start
            # (managed +thinking model, on stdin so the whole prompt must be
            # consumed before the first token) can be slow; the previous
            # threshold (timeout_seconds) was too aggressive and could abort a
            # valid in-progress review. Relax it to the FULL wall budget
            # (timeout_seconds + the stall grace) so the only thing that ends a
            # silent-but-alive cold start is the hard wall-time ceiling below.
            # The post-first-byte stall threshold (stall_silence_seconds) is
            # unchanged.
            silence_age = now - start_ts
            silence_trigger_threshold = float(timeout_seconds) + stall_silence_seconds

        if silence_age >= silence_trigger_threshold:
            _terminate_process_tree(proc)
            if can_log:
                _log_dispatch(log_path, {
                    "event": "dispatch_aborted",
                    **anchors,
                    "abort_source": "watchdog_silence",
                    "abort_reason": f"no kimi stdout for {silence_age:.0f}s (threshold {silence_trigger_threshold:.0f}s)",
                    "age_seconds": now - start_ts,
                    "last_streamed_line_age_seconds": silence_age if lst is not None else None,
                })
            raise KimiInvocationError(
                f"kimi stuck: no output for {silence_age:.0f}s", retryable=True
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
            raise KimiInvocationError(
                f"kimi exceeded {timeout_seconds}s wall timeout + {stall_silence_seconds}s grace",
                retryable=True,
            )

        time.sleep(poll_interval)

    t_stdout.join(timeout=5)
    t_stderr.join(timeout=5)
    if t_stdout.is_alive() or t_stderr.is_alive():
        raise KimiInvocationError(
            "kimi exited but reader thread did not drain within 5s; "
            "possible partial output or pipe-handling defect",
            retryable=False,
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
        # EXIT-CODE MAPPING: 75 -> retryable (429/5xx/timeout), maps to
        # gemini's 429 retry path; everything else -> non-retryable hard fail.
        retryable = proc.returncode == _KIMI_RETRYABLE_EXIT
        kind = "retryable (429/5xx/timeout)" if retryable else "non-retryable (auth/quota/config)"
        raise KimiInvocationError(
            f"kimi exit={proc.returncode} [{kind}]; stderr_tail={stderr_tail!r}{stdout_hint}",
            retryable=retryable,
        )

    return stdout_bytes.decode("utf-8", errors="replace")


def _parse_kimi_output(text: str) -> dict:
    """Parse kimi's review JSON + locally validate shape.

    `text` is the peeled assistant `content` string (see
    _peel_assistant_content). kimi lacks a native output-schema enforcer, so
    this parser is lenient on extraction (handles fenced/wrapped JSON via the
    reused _extract_json_from_text) but equally strict on the parsed shape.

    Rules (mirrors _parse_gemini_output with kimi-rev IDs):
      - root is a JSON object
      - top-level keys exactly {findings, goal_satisfied, blocking_objections,
        goal_satisfied_rationale}
      - findings is a list; each finding has all 6 required fields
      - id matches ^kimi-rev-\\d+$
      - severity in the canonical enum
      - patch_proposal MUST be null (kimi is review-only)
      - blocking_objections invariant: set equals blocking/critical finding IDs
      - goal_satisfied bool; goal_satisfied_rationale non-empty string
      - goal_satisfied=true is incoherent with non-empty blocking_objections

    Raises KimiOutputParseError on any violation.
    """
    candidate = _extract_json_from_text(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise KimiOutputParseError(
            f"kimi output is not valid JSON: {exc}; first 500 chars of raw: {text[:500]!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise KimiOutputParseError(
            f"kimi output JSON root must be an object, got {type(parsed).__name__}"
        )

    unknown_top = set(parsed.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_top:
        raise KimiOutputParseError(
            f"kimi output JSON has unexpected top-level keys: {sorted(unknown_top)}"
        )

    for required in ("findings", "goal_satisfied", "blocking_objections", "goal_satisfied_rationale"):
        if required not in parsed:
            raise KimiOutputParseError(f"kimi output JSON missing required key: {required!r}")

    if not isinstance(parsed["findings"], list):
        raise KimiOutputParseError("'findings' must be an array")
    if not isinstance(parsed["goal_satisfied"], bool):
        raise KimiOutputParseError(
            f"'goal_satisfied' must be boolean, got {type(parsed['goal_satisfied']).__name__}"
        )
    if not isinstance(parsed["blocking_objections"], list):
        raise KimiOutputParseError(
            f"'blocking_objections' must be an array, got {type(parsed['blocking_objections']).__name__}"
        )
    if not isinstance(parsed["goal_satisfied_rationale"], str):
        raise KimiOutputParseError(
            f"'goal_satisfied_rationale' must be string, got "
            f"{type(parsed['goal_satisfied_rationale']).__name__}"
        )
    if not parsed["goal_satisfied_rationale"].strip():
        raise KimiOutputParseError(
            "'goal_satisfied_rationale' must be a non-empty string (prompt contract)"
        )

    for i, item in enumerate(parsed["blocking_objections"]):
        if not isinstance(item, str):
            raise KimiOutputParseError(
                f"blocking_objections[{i}] must be string, got {type(item).__name__}"
            )

    blocking_finding_ids = []
    for i, finding in enumerate(parsed["findings"]):
        if not isinstance(finding, dict):
            raise KimiOutputParseError(
                f"findings[{i}] must be an object, got {type(finding).__name__}"
            )
        unknown_keys = set(finding.keys()) - _ALLOWED_FINDING_KEYS
        if unknown_keys:
            raise KimiOutputParseError(
                f"findings[{i}] has unexpected keys: {sorted(unknown_keys)}"
            )
        for required in _REQUIRED_FINDING_FIELDS:
            if required not in finding:
                raise KimiOutputParseError(
                    f"findings[{i}] missing required field: {required!r}"
                )
        for str_field in ("id", "severity", "summary", "citation", "risk", "recommendation"):
            if not isinstance(finding[str_field], str):
                raise KimiOutputParseError(
                    f"findings[{i}].{str_field} must be string, got "
                    f"{type(finding[str_field]).__name__}"
                )
        if not finding["summary"].strip():
            raise KimiOutputParseError(
                f"findings[{i}].summary must be a non-empty string (schema contract)"
            )
        if finding["severity"] not in _VALID_SEVERITIES:
            raise KimiOutputParseError(
                f"findings[{i}] has invalid severity {finding['severity']!r}; "
                f"must be one of {sorted(_VALID_SEVERITIES)}"
            )
        if not _KIMI_FINDING_ID_PATTERN.match(finding["id"]):
            raise KimiOutputParseError(
                f"findings[{i}] id {finding['id']!r} does not match pattern "
                f"^kimi-rev-\\d+$"
            )
        pp = finding.get("patch_proposal")
        if pp is not None:
            raise KimiOutputParseError(
                f"findings[{i}].patch_proposal must be null — kimi is review-only"
            )
        reason = finding.get("patch_not_proposed_reason")
        if reason is not None and not isinstance(reason, str):
            raise KimiOutputParseError(
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
        raise KimiOutputParseError("; ".join(msg_parts))

    if parsed["goal_satisfied"] is True and actual_blocking:
        raise KimiOutputParseError(
            f"goal_satisfied=true is incoherent with non-empty blocking_objections "
            f"{sorted(actual_blocking)}; a successful review cannot have blocking findings"
        )

    return parsed


_KIMI_PROPOSAL_SCHEMA_PATH = (
    Path(__file__).parent / "dispatch_templates" / "gemini_proposal_schema.json"
)


def _parse_kimi_proposal_output(text: str, schema_path: Path | None = None) -> dict:
    """Parse + validate kimi proposal-mode output.

    `text` is the peeled assistant `content` string. Validates against
    `schema_path` (operator --schema override) when provided, else against the
    shared proposal schema (gemini_proposal_schema.json — proposal shape is
    adapter-agnostic: it uses selected_target / rationale_vs_alternatives, not
    rev-IDs, so the gemini schema is reused per the task's reuse mandate).

    Uses the reused _extract_json_from_text for JSON extraction.

    Raises KimiOutputParseError on shape mismatch.
    """
    try:
        cleaned = _extract_json_from_text(text)
    except ValueError as exc:
        raise KimiOutputParseError(
            f"kimi proposal output: could not extract JSON: {exc}"
        ) from exc

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise KimiOutputParseError(
            f"kimi proposal output is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise KimiOutputParseError(
            f"kimi proposal output root must be a JSON object; got {type(parsed).__name__}"
        )

    effective_schema_path = schema_path or _KIMI_PROPOSAL_SCHEMA_PATH
    try:
        import jsonschema
    except ImportError as exc:
        raise KimiOutputParseError(
            f"jsonschema package required for proposal-mode validation; "
            f"install with `pip install jsonschema` or reinstall consensus-mcp: {exc}"
        ) from exc
    try:
        schema = json.loads(Path(effective_schema_path).read_text(encoding="utf-8"))
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise KimiOutputParseError(
            f"kimi proposal output failed schema validation at "
            f"{'/'.join(str(p) for p in exc.absolute_path) or '<root>'}: {exc.message}"
        ) from exc
    except FileNotFoundError as exc:
        raise KimiOutputParseError(
            f"proposal schema not found at {effective_schema_path}: {exc}"
        ) from exc

    if not isinstance(parsed.get("rationale_vs_alternatives"), str) or not parsed["rationale_vs_alternatives"].strip():
        raise KimiOutputParseError(
            "rationale_vs_alternatives must be a non-empty string (parser invariant)"
        )
    if "structural_abstention" not in parsed or not isinstance(parsed["structural_abstention"], bool):
        raise KimiOutputParseError(
            "structural_abstention must be present and boolean (parser invariant)"
        )

    if not parsed["structural_abstention"]:
        if parsed["selected_target"] is None:
            raise KimiOutputParseError(
                "selected_target is required when structural_abstention is false"
            )
        if parsed["deliverable_scope"] is None:
            raise KimiOutputParseError(
                "deliverable_scope is required when structural_abstention is false"
            )

    return parsed


def _invoke_kimi_with_retry(
    prompt: str,
    kimi_bin: str,
    timeout_seconds: int,
    repo_root: Path,
    log_path=None,
    anchors=None,
    mode: str = "review",
    proposal_schema_path: Path | None = None,
) -> tuple[str, dict]:
    """Run kimi once; retry once on a parse failure OR a retryable invocation
    failure (exit 75 — 429/5xx/timeout, mirroring gemini's 429 path).

    A non-retryable invocation failure (exit 1: auth/quota/config) propagates
    immediately without a retry.

    Returns (raw_output_of_last_attempt, parsed_dict). The raw output is the
    verbatim kimi stdout; the parsed dict is the chrome-stripped + JSON-extracted
    + validated review.
    """
    def _one_call(call_prompt: str) -> tuple[str, dict]:
        raw = _invoke_kimi(
            prompt=call_prompt,
            kimi_bin=kimi_bin,
            timeout_seconds=timeout_seconds,
            repo_root=repo_root,
            log_path=log_path,
            anchors=anchors,
        )
        # OUTPUT HANDLING (stdin/--quiet): kimi emits the final answer as plain
        # text + a "To resume this session: kimi -r ..." trailer. Strip that
        # chrome (kimi.yaml strip_patterns) BEFORE extraction. _peel_assistant_content
        # is retained only as a robustness fallback: if an older CLI still
        # emits a stream-json {"role":"assistant","content":...} envelope it
        # peels `content`; otherwise it returns the (stripped) text unchanged
        # so _extract_json_from_text (inside the parser) sees the free text.
        content = _peel_assistant_content(_strip_kimi_output_chrome(raw))
        if mode == "proposal":
            parsed = _parse_kimi_proposal_output(content, schema_path=proposal_schema_path)
        else:
            parsed = _parse_kimi_output(content)
        return raw, parsed

    try:
        return _one_call(prompt)
    except KimiInvocationError as inv_err:
        # 75 (retryable) -> re-invoke once with the SAME prompt (mirrors
        # gemini's 429 retry path). 1 (non-retryable) -> propagate.
        if not inv_err.retryable:
            raise
        if log_path is not None and anchors is not None:
            _log_dispatch(log_path, {
                "event": "dispatch_retry_for_retryable_invocation",
                **anchors,
                "first_invocation_error": str(inv_err)[:1000],
            })
        return _one_call(prompt)
    except KimiOutputParseError as first_err:
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
        return _one_call(retry_prompt)


def _sealed_mirror_filename(reviewer_id: str, pass_id: str) -> str:
    """H1: bind the iteration-local sealed mirror filename to reviewer+pass.

    The previous fixed `kimi-review.yaml` meant a multi-pass dispatch (pass1,
    pass2, ...) overwrote the prior pass's local mirror. Mirroring the
    codex/gemini convention of a per-reviewer artifact, we key the local mirror
    on the pass_id (which already encodes reviewer + pass, e.g.
    `kimi-iter0007-1-pass2`) so each pass writes its own
    `kimi-review-<pass_id>.yaml`. Falls back to reviewer_id, then the legacy
    fixed name, if those anchors are somehow empty. Filesystem-unsafe chars in
    the anchor are normalized to '-'.
    """
    anchor = (pass_id or reviewer_id or "").strip()
    if not anchor:
        return "kimi-review.yaml"
    # Allow [A-Za-z0-9_-]; map everything else (incl. '.', '/', whitespace) to
    # '-'. Dropping '.' too removes any '..' path-traversal segment from the
    # mirror filename. Collapse repeats + trim leading/trailing '-'.
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", anchor)
    safe = re.sub(r"-{2,}", "-", safe).strip("-")
    if not safe:
        return "kimi-review.yaml"
    return f"kimi-review-{safe}.yaml"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus_mcp._dispatch_kimi",
        description="Auto-dispatch kimi CLI as a reviewer; auto-seal via T6.",
    )
    p.add_argument("--goal-packet", required=True)
    p.add_argument("--iteration-dir", required=True)
    p.add_argument("--reviewer-id", default=None)
    p.add_argument("--pass-id", default=None)
    p.add_argument("--prompt-template", default=None)
    p.add_argument("--mode", default="review", choices=["review", "proposal"],
                   help=("Dispatch mode. 'review' (default): use "
                         "kimi_review_template.md for code-review tasks. "
                         "'proposal': use gemini_proposal_template.md (shared, "
                         "adapter-agnostic) for design-consult / workflow #4 "
                         "proposal tasks. --prompt-template override takes "
                         "precedence over --mode."))
    p.add_argument("--schema", default=None,
                   help=("Optional path to a JSON schema for validating "
                         "PROPOSAL-mode output. Ignored in review mode "
                         "(review-mode validation is template-embedded). When "
                         "unset in proposal mode, defaults to "
                         "dispatch_templates/gemini_proposal_schema.json "
                         "(shared proposal schema)."))
    p.add_argument("--kimi-bin", default="kimi")
    p.add_argument("--timeout-seconds", type=int, default=_DEFAULT_TIMEOUT_SECONDS)
    p.add_argument("--review-target", default=None)
    p.add_argument("--smoke", action="store_true",
                   help="Smoke mode: gated by CONSENSUS_MCP_RUN_REAL_KIMI_SMOKE=1 env var")

    ns = p.parse_args(argv)

    try:
        repo_root = _resolve_repo_root()
    except RepoRootResolutionError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "error_type": "RepoRootResolutionError"}))
        return 4

    log_path = repo_root / "consensus-state" / "state" / "dispatch-log.jsonl"
    _pre_iter_id = Path(ns.iteration_dir).name or "unknown-iteration"
    _pre_reviewer_id = ns.reviewer_id or f"kimi-{_pre_iter_id}-1"
    _pre_pass_id = ns.pass_id or f"{_pre_reviewer_id}-pass1"

    try:
        iter_dir = _normalize_relative_to_repo(ns.iteration_dir, repo_root)
        iter_dir.mkdir(parents=True, exist_ok=True)
        iteration_id = iter_dir.name

        _default_template_name = (
            "gemini_proposal_template.md" if ns.mode == "proposal"
            else "kimi_review_template.md"
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
                else (Path(__file__).parent / "dispatch_templates" / "gemini_proposal_schema.json")
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

    reviewer_id = ns.reviewer_id or f"kimi-{iteration_id}-1"
    pass_id = ns.pass_id or f"{reviewer_id}-pass1"

    if ns.smoke and os.environ.get("CONSENSUS_MCP_RUN_REAL_KIMI_SMOKE") != "1":
        refuse_msg = (
            "--smoke requires CONSENSUS_MCP_RUN_REAL_KIMI_SMOKE=1 in the environment. "
            "This gate prevents accidental real-kimi invocation under cost/auth/session "
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
        "kimi_bin": ns.kimi_bin,
        "review_target_path": review_target_path_str,
        "adapter": "kimi",
    })

    prompt_sha: str | None = None
    goal_packet_sha: str | None = None
    scope_sig: str | None = None
    kimi_version: str | None = None
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
            "adapter": "kimi",
        }
        for k, v in (
            ("kimi_version", kimi_version),
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

    landed_seconds: float | None = None
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

        kimi_version = _get_kimi_version(ns.kimi_bin)
        _t0 = time.monotonic()

        # DISPOSABLE TEMP WORKDIR (B4): run kimi against a throwaway copy of the
        # repo (git clone --local --shared, fallback shutil.copytree) so it
        # physically cannot touch the real tree. Cleaned up in finally.
        kimi_workdir: Path | None = None
        try:
            kimi_workdir = _make_disposable_workdir(repo_root)
            _log_dispatch(log_path, {
                "event": "dispatch_workdir_prepared",
                "iteration_id": iteration_id,
                "reviewer_id": reviewer_id,
                "pass_id": pass_id,
                "workdir": str(kimi_workdir),
                "adapter": "kimi",
            })
            raw_output, extracted = _invoke_kimi_with_retry(
                prompt=prompt,
                kimi_bin=ns.kimi_bin,
                timeout_seconds=ns.timeout_seconds,
                repo_root=kimi_workdir,
                log_path=log_path,
                anchors={
                    "iteration_id": iteration_id,
                    "reviewer_id": reviewer_id,
                    "pass_id": pass_id,
                },
                mode=ns.mode,
                proposal_schema_path=proposal_schema_path,
            )
        finally:
            _cleanup_disposable_workdir(kimi_workdir)

        landed_seconds = time.monotonic() - _t0
        output_sha = _sha256_str(raw_output)

        # POST-DISPATCH INTEGRITY CHECK (B4, independent safeguard): if the REAL
        # repo's `git status --short` is non-empty after the dispatch, kimi
        # mutated the real workspace despite the temp-copy isolation — REJECT the
        # review + log the violation. Backstop regardless of root cause.
        dirty, status_raw = _real_repo_is_dirty(repo_root)
        if dirty:
            _log_dispatch(log_path, {
                "event": "dispatch_integrity_violation",
                "iteration_id": iteration_id,
                "reviewer_id": reviewer_id,
                "pass_id": pass_id,
                "git_status_short": status_raw[:4000],
                "adapter": "kimi",
            })
            raise KimiIntegrityError(
                "post-dispatch integrity check FAILED: the real repo is dirty "
                "after the kimi dispatch (kimi mutated the workspace despite the "
                "disposable temp work-dir). Review output REJECTED. "
                f"git status --short:\n{status_raw[:2000]}"
            )

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
            "kimi_version": kimi_version,
            "prompt_sha256": prompt_sha,
            "output_sha256": output_sha,
            "goal_packet_sha256": goal_packet_sha,
            "scope_signature": scope_sig,
            "review_target_path": review_target_path_str,
            "review_target_hash": review_target_hash,
            "adapter": "kimi",
            "mode": ns.mode,
            "proposal_schema_path": proposal_schema_path_str,
            "proposal_schema_sha256": proposal_schema_sha,
        }
        packet = _build_sealed_packet(
            extracted, iteration_id, reviewer_id, pass_id,
            provenance=provenance,
            attestation_method="auto_kimi_dispatch",
            attestation_input_sources=[
                "goal_packet (path passed via --goal-packet)",
                "prompt_template (substituted by _build_prompt)",
                "review_target (path passed via --review-target; may be unspecified)",
                "kimi CLI --quiet --thinking (stdin transport, read-only; no native output-schema enforcement)",
            ],
        )
        result = _seal_via_t6(
            packet, iter_dir,
            sealed_filename=_sealed_mirror_filename(reviewer_id, pass_id),
        )
    except (KimiInvocationError, KimiOutputParseError, KimiIntegrityError) as exc:
        _log_dispatch(log_path, _failed_event(type(exc).__name__, str(exc)))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 1
    except Exception as exc:
        _log_dispatch(log_path, _failed_event(type(exc).__name__, str(exc)))
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}))
        return 2

    # Timing line (UX parity with gemini/codex): landed-in-Ns vs the ceiling.
    if landed_seconds is not None:
        ceiling = ns.timeout_seconds + int(_effective_stall_silence())
        print(
            f"[kimi-timing] landed in {landed_seconds:.1f}s (ceiling {ceiling}s)",
            file=sys.stderr,
        )

    _log_dispatch(log_path, {
        "event": "dispatch_done",
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "kimi_version": kimi_version,
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
        "adapter": "kimi",
    })
    print(json.dumps({"ok": True, "pass_id": pass_id, **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
