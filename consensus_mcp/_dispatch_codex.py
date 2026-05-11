"""Phase 4 v1.1 — auto-codex-dispatch helper.

Replaces the operator paste-buffer flow for codex reviews. Reads a goal_packet
for context, substitutes a markdown prompt template, shells out to `codex` CLI,
parses the JSON response (constrained via codex's --output-schema), wraps it in
a sealed-packet outer structure, and calls T6 (review.write_and_seal) directly.
Writes a richly-attributed audit trail to consensus-state/state/dispatch-log.jsonl.

NOT an MCP tool. CLI-only. Per Phase 3 anti-scope, consensus_mcp/tools/*
is FROZEN; this helper is in the package root and calls T6's handle in-process.

USAGE
-----
  python -m consensus_mcp._dispatch_codex \
      --goal-packet <path/to/goal_packet.yaml> \
      --iteration-dir <path/to/iteration-XXXX/> \
      [--reviewer-id codex-iterXXXX-N] \
      [--pass-id codex-iterXXXX-N-passN] \
      [--prompt-template <path>] \
      [--schema <path>] \
      [--codex-bin <path>] \
      [--timeout-seconds 600] \
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
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml


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

# Per Task #24 (iter-0014): patch_proposal binding fields per codex 2026-05-10 v4.
# 2026-05-10 schema/validator alignment: expected_tests promoted to required to
# match codex_review_schema.json:24 (strict-output schema). The two layers
# previously disagreed (schema required, validator optional); a non-CLI caller
# could construct a patch_proposal missing expected_tests and pass the python
# validator while codex CLI would reject the same shape upstream.
_PATCH_PROPOSAL_REQUIRED = ("patch_id", "applies_to_findings", "base_sha", "unified_diff", "files_touched", "expected_tests")
_PATCH_PROPOSAL_OPTIONAL = ()
_PATCH_PROPOSAL_ALLOWED = set(_PATCH_PROPOSAL_REQUIRED) | set(_PATCH_PROPOSAL_OPTIONAL)
_PATCH_ID_PATTERN = re.compile(r"^codex-rev-\d+-patch$")

# iter-0028 F4 (codex-rev-002): unified-diff body header parser regex.
# Captures the path after `--- a/` or `+++ b/`. The `/dev/null` marker
# (conventional unified-diff create/delete) is filtered out by the caller.
# Tolerates leading whitespace on the line (unlikely but cheap).
_DIFF_FILE_HEADER_RE = re.compile(
    r"^(?:---\s+a/|\+\+\+\s+b/)(?P<path>\S+)\s*$",
    re.MULTILINE,
)
# iter-0028 F1+F2 (codex-rev-003): tokens that signal codex-cli's proprietary
# `apply_patch` format (NOT standard unified-diff). The iter-0026 F2 hunk-
# anchored applier rejects this shape at apply time; iter-0028 moves the
# rejection to validate time and explains the expected format.
_APPLY_PATCH_BEGIN_MARKER = "*** Begin Patch"
_APPLY_PATCH_UPDATE_MARKER = "*** Update File"


# Per v1.10.4 F1 (codex review on 0ae7b80d): repo_root resolution must fail-closed.
# Repo markers: directories that MUST exist at the resolved root for it to be a valid
# consensus-mcp / consensus-mcp repo. site-packages doesn't have these; cwd in a
# random directory doesn't either; the actual repo does.
_REPO_ROOT_MARKERS = ("consensus-state", "consensus_mcp", "consensus_mcp/validators")


class RepoRootResolutionError(RuntimeError):
    """Raised when repo_root cannot be resolved to a valid repo (no markers found)."""


def _has_repo_markers(candidate: Path) -> bool:
    """Return True iff candidate contains all _REPO_ROOT_MARKERS as subpaths."""
    return all((candidate / marker).is_dir() for marker in _REPO_ROOT_MARKERS)


def _resolve_repo_root() -> Path:
    """Resolve the repo root, fail-closed if no valid candidate is found.

    Per v1.10.4 F1 hardening: the prior fallback to
    Path(__file__).resolve().parent.parent was unsafe — when the helper
    runs as an installed module from python_env/Lib/site-packages, that fallback
    landed at python_env/Lib (NOT the repo root), causing codex --cd, T6 archive
    writes, and dispatch-log writes to all target the wrong tree.

    Resolution order (first candidate with all repo markers wins):
      1. CONSENSUS_MCP_REPO_ROOT env var (must validate)
      2. Path.cwd() (operator usually invokes from repo root)
      3. Walk parents of Path(__file__) (only succeeds when running in-tree;
         the in-tree __file__ is consensus_mcp/_dispatch_codex.py so
         3-up = repo root with markers)

    If no candidate validates, raise RepoRootResolutionError with a clear
    operator-facing message naming the env var to set. Never silently fall
    back to site-packages.
    """
    candidates_tried: list[tuple[str, Path]] = []

    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        candidate = Path(override).resolve()
        candidates_tried.append(("CONSENSUS_MCP_REPO_ROOT", candidate))
        if _has_repo_markers(candidate):
            return candidate
        # iter-0028 F5 (codex-rev-004): operator-supplied env var is
        # authoritative. If set but the path fails marker validation, do NOT
        # silently fall through to cwd / __file__ candidates — the operator's
        # intent was an explicit override, and silent re-resolution invites
        # the very confusion the env var was meant to eliminate. Raise with
        # a clear message naming the env var. Empty-string env (treated as
        # falsy above) is the "unset" case and DOES fall through.
        raise RepoRootResolutionError(
            f"CONSENSUS_MCP_REPO_ROOT={override!r} was set but the path "
            f"{candidate} does not contain all required repo markers "
            f"{_REPO_ROOT_MARKERS}. Not falling through to cwd / __file__ "
            f"discovery — operator-supplied env var is authoritative. "
            f"Either fix the path (it must contain {_REPO_ROOT_MARKERS} as "
            f"subdirectories) or unset CONSENSUS_MCP_REPO_ROOT to use "
            f"automatic discovery."
        )

    cwd = Path.cwd().resolve()
    candidates_tried.append(("Path.cwd()", cwd))
    if _has_repo_markers(cwd):
        return cwd

    # __file__-parent walk: only finds repo root when source-tree-installed.
    here = Path(__file__).resolve()
    for parent in (here.parent, here.parent.parent, here.parent.parent.parent):
        candidates_tried.append((f"parent of __file__ ({parent.name})", parent))
        if _has_repo_markers(parent):
            return parent

    tried_msg = "; ".join(f"{name}={path}" for name, path in candidates_tried)
    raise RepoRootResolutionError(
        f"Cannot resolve consensus-mcp repo root. None of the candidates contain "
        f"all required markers {_REPO_ROOT_MARKERS}. Set CONSENSUS_MCP_REPO_ROOT "
        f"to the repo root directory (e.g., the directory containing 'consensus-state/' "
        f"and 'scripts/'). Candidates tried: {tried_msg}"
    )


class OutsideRepoPathError(ValueError):
    """v1.10.5 containment hardening: operator-supplied path resolves outside repo_root."""


def _normalize_for_compare(p) -> str:
    """iter-0039 xplat-rev-001 fix: canonicalize a path for cross-platform
    containment comparison.

    On Windows the filesystem is case-insensitive, the canonical separator
    is backslash but forward slashes also work, and extended-length paths
    can be prefixed with `\\\\?\\`. The prior lower+replace fallback didn't
    handle the long-path prefix or 8.3 short-name expansion edge cases.

    This helper:
      1. Strips the `\\\\?\\` long-path prefix if present.
      2. Calls os.path.normpath to collapse `.` / `..` segments and
         normalize separators.
      3. Calls os.path.normcase to lowercase on Windows (no-op on POSIX).

    The returned string is suitable for startswith-with-separator
    containment checks.
    """
    s = str(p)
    if sys.platform == "win32" and s.startswith("\\\\?\\"):
        s = s[4:]
    return os.path.normcase(os.path.normpath(s))


def _normalize_relative_to_repo(path_str: str | None, repo_root: Path) -> Path | None:
    """Normalize an operator-supplied path against repo_root.

    Per v1.10.4 F5 hardening: operator may supply relative paths to --goal-packet,
    --review-target, --prompt-template, --schema. The codex subprocess runs with
    --cd repo_root, so relative paths must be interpreted in the repo_root frame
    too — NOT against the process cwd (which may differ if a future MCP wrapper
    or service caller invokes us from elsewhere).

    Per v1.10.5 containment hardening: after resolution the path MUST be inside
    repo_root. Absolute paths previously passed through unchanged via p.resolve(),
    which let any caller supply (or have an MCP tool wrapper pass through) an
    out-of-tree absolute path and have its contents pulled into the codex prompt.
    That's a real boundary leak — review-target/goal-packet/schema/template are
    all read with read_text(). Containment fails closed with a clear diagnostic.

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
        # iter-0033 claude-rev-003 + iter-0039 xplat-rev-001 fix:
        # Path.relative_to is case-sensitive in string compare. Windows
        # filesystem is case-insensitive AND extended-length paths can
        # carry a `\\?\` prefix that breaks naive lower+replace. Use the
        # canonical normalization helper for Windows compare.
        if sys.platform == "win32":
            ncs_resolved = _normalize_for_compare(resolved)
            ncs_root = _normalize_for_compare(repo_root_resolved)
            if ncs_resolved == ncs_root or ncs_resolved.startswith(ncs_root + os.sep):
                contained = True
    if not contained:
        raise OutsideRepoPathError(
            f"path {path_str!r} resolves to {resolved} which is outside repo_root "
            f"{repo_root_resolved}. consensus-mcp dispatch only reads files inside "
            f"the repo. Move the file into the repo or pass a path relative to it."
        )
    return resolved


def _load_goal_packet(path: Path) -> dict:
    """Load + minimally validate a goal_packet.yaml. Returns the parsed dict."""
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"goal_packet root must be a mapping, got {type(data).__name__}")
    return data


def _load_template(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


# Per iter-0021: language fence map for touched-file embedding. Codex's
# read-only sandbox cannot reliably read repo files; the helper embeds
# touched-file contents directly in the prompt as fenced code blocks. The
# extension -> fence-language mapping is conservative (text fence for
# unknown extensions); covers the file types this project's iterations
# actually touch.
_FENCE_LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".sh": "bash",
    ".bash": "bash",
    ".cmd": "batch",
    ".js": "javascript",
    ".ts": "typescript",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
}


def _format_touched_files_contents(contents: dict[str, str]) -> str:
    """Render touched-file contents as ``## File: <path>`` + fenced code block.

    Per iter-0021 spec: codex receives file contents inline because its
    sandbox cannot reliably perform filesystem reads. The format is plain
    markdown so the prompt renders correctly when codex reads the prompt.

    File order is sorted by path for deterministic prompt SHA. Unknown
    extensions fall back to a ``text`` fence.
    """
    if not contents:
        return "(no touched-file contents embedded)"
    lines: list[str] = []
    for path in sorted(contents.keys()):
        ext = Path(path).suffix.lower()
        lang = _FENCE_LANG_BY_EXT.get(ext, "text")
        lines.append(f"## File: {path}")
        lines.append("")
        lines.append(f"```{lang}")
        body = contents[path]
        # Trim a single trailing newline so the closing fence sits on its own
        # line without a blank gap; preserve internal newlines verbatim.
        if body.endswith("\n"):
            body = body[:-1]
        lines.append(body)
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_prompt(
    goal_packet: dict,
    template_text: str,
    iteration_dir: str | None = None,
    review_packet_path: str | None = None,
    review_target_path: str | None = None,
    review_target_hash: str | None = None,
    review_packet: dict | None = None,
) -> str:
    """Substitute goal_packet fields + review-target fields into the template's {placeholders}.

    Per F6 (codex review 2026-05-09): explicit review-target fields tell codex
    exactly which input it should review, preventing wrong-scope inference in
    dirty repositories.

    Per iter-0021: when ``review_packet`` is supplied and contains
    ``defect_target.touched_files_contents``, those file bodies are embedded
    inline at the ``{touched_files_contents_block}`` placeholder. Codex's
    sandbox cannot reliably read repo files; embedded contents replace the
    filesystem read.

    Missing optional fields render as empty strings or "(not specified)" so the
    template keeps formatting.
    """
    auth = goal_packet.get("authorization", {}) or {}
    goal = goal_packet.get("goal", {}) or {}

    def _format_list(xs):
        if not xs:
            return "(none)"
        return "\n".join(f"  - {x}" for x in xs)

    def _format_gates(gates):
        if not gates:
            return "(none)"
        lines = []
        for g in gates:
            gid = g.get("id", "?")
            desc = g.get("description", "")
            check = g.get("check", "")
            lines.append(f"  - {gid}: {desc}\n      check: {check}")
        return "\n".join(lines)

    def _or_unspecified(v):
        return v if v is not None and v != "" else "(not specified)"

    # iter-0021: extract touched-file contents from review_packet if present.
    touched_contents: dict[str, str] = {}
    if isinstance(review_packet, dict):
        defect_target = review_packet.get("defect_target")
        if isinstance(defect_target, dict):
            tfc = defect_target.get("touched_files_contents")
            if isinstance(tfc, dict):
                # Coerce values to strings; reject non-string entries silently
                # rather than raising — the helper writer is the type guard.
                touched_contents = {
                    k: v for k, v in tfc.items()
                    if isinstance(k, str) and isinstance(v, str)
                }

    substitutions = {
        "{goal_summary}": str(goal.get("summary", "")),
        "{desired_end_state}": str(goal.get("desired_end_state", "")),
        "{allowed_files}": _format_list(goal_packet.get("allowed_files", [])),
        "{acceptance_gates}": _format_gates(goal_packet.get("acceptance_gates", [])),
        "{scope_signature}": str(auth.get("scope_signature", "")),
        "{authorized_by}": str(auth.get("authorized_by", "")),
        "{authorized_at_utc}": str(auth.get("authorized_at_utc", "")),
        "{iteration_dir}": _or_unspecified(iteration_dir),
        "{review_packet_path}": _or_unspecified(review_packet_path),
        "{review_target_path}": _or_unspecified(review_target_path),
        "{review_target_hash}": _or_unspecified(review_target_hash),
        "{touched_files_contents_block}": _format_touched_files_contents(touched_contents),
    }
    out = template_text
    for placeholder, value in substitutions.items():
        out = out.replace(placeholder, value)
    return out


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


def _terminate_process_tree(proc, grace_seconds: float = 10.0) -> None:
    """iter-0039 xplat-rev-002 fix: cross-platform process tree termination.

    Per the cross-platform audit, plain proc.terminate() only kills the
    immediate codex process. codex-cli is a Node binary that spawns child
    processes; those orphan on abort. The Popen call now creates a new
    process group (CREATE_NEW_PROCESS_GROUP on Windows, start_new_session
    on POSIX); this helper sends the appropriate group-wide signal,
    waits a grace period, then force-kills the whole group.

    On Windows: send CTRL_BREAK_EVENT to the process group (children
    inherit the group via CREATE_NEW_PROCESS_GROUP). If still alive after
    grace, shell out to `taskkill /F /T /PID <pid>` (uses CREATE_NO_WINDOW
    flag per iter-0039 codex-rev-002 to avoid console flash).

    On POSIX: send SIGTERM to the process group via os.killpg, then
    SIGKILL after grace.

    Always returns; never raises.
    """
    if proc.poll() is not None:
        return

    try:
        if sys.platform == "win32":
            # CTRL_BREAK_EVENT propagates to children in the same process group.
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError, ValueError):
        # Fallback to single-process terminate if the group call failed
        # (e.g., process already dead, no permission, etc.).
        try:
            proc.terminate()
        except OSError:
            pass

    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass  # fall through to force-kill

    # Grace expired; force-kill the entire tree.
    try:
        if sys.platform == "win32":
            # taskkill /F /T kills the tree. CREATE_NO_WINDOW prevents a
            # console window flash (iter-0039 codex-rev-002).
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError, ValueError, subprocess.SubprocessError):
        # Fallback to direct kill if killpg / taskkill failed.
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        # The process is wedged; we've done what we can. Caller will
        # surface the failure via the existing return-code check.
        pass


def _invoke_codex(
    prompt: str,
    codex_bin: str,
    timeout_seconds: int,
    repo_root: Path,
    schema_path: Path,
    log_path=None,
    anchors=None,
    heartbeat_interval: float = 30.0,
    stall_silence_seconds: float = 45.0,
    poll_interval: float = 0.5,
    time_fn=None,
    popen_factory=None,
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
    can_log = log_path is not None and anchors is not None

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

            time.sleep(poll_interval)

        # Process exited; drain reader threads.
        t_stdout.join(timeout=5)
        t_stderr.join(timeout=5)
        if t_stdout.is_alive() or t_stderr.is_alive():
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


def _compute_per_patch_base_sha(
    defect_target: dict,
    patch_files_touched: list,
    repo_root: Path | None = None,
) -> str | None:
    """iter-0024 F2 + iter-0026 F1: compute per-patch bundle_sha matching the
    on-disk bytes hash that apply.codex_patch produces at drift-check time.

    iter-0026 F1 fix:
      The prior helper computed bundle_sha by encoding text strings from
      ``defect_target.touched_files_contents`` (UTF-8). On Windows with CRLF
      line endings on disk, ``apply.codex_patch`` (which hashes raw disk bytes
      via ``_closure_invariant.bundle_sha``) saw a DIFFERENT hash, forcing
      manual per-patch re-anchor for every codex-applied patch. The fix:
      when ``repo_root`` is supplied, defer entirely to
      ``_closure_invariant.bundle_sha(repo_root, patch_files_touched)`` so the
      stamp matches apply-time disk bytes exactly.

      When ``repo_root`` is None (legacy unit-test path), fall back to the
      iter-0024 text-encoding behaviour. This preserves backward compatibility
      for tests that don't materialise files on disk.

    Returns None when:
      - patch_files_touched is empty, OR
      - (text fallback only) defect_target has no touched_files_contents dict,
        OR any patch file is missing / non-string content.

    On None return, caller falls back to defect_target.base_sha (iter-0022
    behaviour) so legacy tests / callers without touched_files_contents still
    work.
    """
    if not patch_files_touched:
        return None

    # iter-0026 F1: disk-bytes path matches apply.codex_patch exactly.
    if repo_root is not None:
        # Validate path types early so we never feed bundle_sha a non-string
        # entry (it would TypeError on PurePath construction).
        for raw_path in patch_files_touched:
            if not isinstance(raw_path, str):
                return None
        # Defer import: keeps _closure_invariant a soft dependency at module
        # load time (mirrors apply_codex_patch's import discipline).
        from consensus_mcp._closure_invariant import bundle_sha as _bundle_sha
        try:
            return _bundle_sha(repo_root, list(patch_files_touched))
        except ValueError:
            # Path contained forbidden \0 / \n separator chars; fall through
            # to None so caller falls back to defect_target.base_sha.
            return None

    # Legacy text-encoding fallback (no repo_root supplied — typically a
    # unit-level test that doesn't materialise files on disk).
    contents = defect_target.get("touched_files_contents")
    if not isinstance(contents, dict):
        return None

    # Defer import to avoid circular dependency at module load time.
    from consensus_mcp._closure_invariant import _normalize_path

    normalised_pairs: list[tuple[str, str]] = []
    for raw_path in patch_files_touched:
        if not isinstance(raw_path, str):
            return None
        # Look up content via the ORIGINAL path key (review-packet
        # touched_files_contents keys are the operator-supplied paths). Then
        # normalise for the canonical bundle form.
        if raw_path not in contents:
            return None
        body = contents[raw_path]
        if not isinstance(body, str):
            return None
        try:
            norm = _normalize_path(raw_path)
        except ValueError:
            return None
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        normalised_pairs.append((norm, content_hash))

    parts = [f"{p}\0{h}" for p, h in sorted(normalised_pairs)]
    canonical = "\n".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_patch_proposal(
    finding_index: int,
    finding_id: str,
    pp: dict,
    all_finding_ids: set,
    goal_packet: dict | None,
    review_packet: dict | None = None,
    repo_root: Path | None = None,
) -> None:
    """Validate a single patch_proposal block per Task #24 (iter-0014) binding rules.

    Raises CodexOutputParseError on any violation. MUTATES the supplied `pp`
    dict by stamping `unified_diff_sha256` (helper-computed) so downstream
    consumers see the canonical diff hash without recomputing.

    Per iter-0022: when ``review_packet`` is supplied AND has a string
    ``defect_target.base_sha``, the helper OVERWRITES ``pp["base_sha"]`` with
    that canonical value. Codex's read-only sandbox emits a hallucinated
    base_sha (iter-0021 empirical finding); the operator-stamped
    ``defect_target.base_sha`` (computed via ``bundle_sha`` at review-packet
    author time) is the authoritative repo-state hash. When review_packet is
    absent or lacks a string defect_target.base_sha, codex's emission is kept
    (backward compat for unit tests + legacy callers).

    Rules (per codex 2026-05-10 v4 + iter-0020 ergonomics fix + iter-0022 base_sha stamp):
      - patch_proposal is dict (None already accepted by caller bypass)
      - keys are EXACTLY the closed set {patch_id, applies_to_findings, base_sha,
        unified_diff, files_touched, expected_tests}; extras rejected
      - patch_id matches ^codex-rev-\\d+-patch$
      - patch_id == f"{finding_id}-patch" (derived from this finding's id;
        replaces the old content-bound formula codex's sandbox couldn't compute)
      - applies_to_findings non-empty list of strings, each in all_finding_ids
      - files_touched non-empty list of strings
      - unified_diff non-empty string
      - if goal_packet supplied: every files_touched path must be in
        goal_packet.allowed_files (matched via _self_drive._path_in_scope), and
        no path may match goal_packet.forbidden_files
      - helper computes sha256(unified_diff) and stamps it on pp as
        `unified_diff_sha256` (not produced by codex; drift-detection field)
      - iter-0022: helper overwrites pp["base_sha"] from review_packet
        defect_target.base_sha when present (string-typed)
    """
    if not isinstance(pp, dict):
        raise CodexOutputParseError(
            f"findings[{finding_index}].patch_proposal must be object, got {type(pp).__name__}"
        )
    # Closed-key set; reject extras (anti-self-verification claims like 'verified')
    unknown = set(pp.keys()) - _PATCH_PROPOSAL_ALLOWED
    if unknown:
        raise CodexOutputParseError(
            f"findings[{finding_index}].patch_proposal has unexpected keys: {sorted(unknown)}"
        )
    # Required fields present
    for required in _PATCH_PROPOSAL_REQUIRED:
        if required not in pp:
            raise CodexOutputParseError(
                f"findings[{finding_index}].patch_proposal missing required field: {required!r}"
            )
    # Type checks on string fields
    for str_field in ("patch_id", "base_sha", "unified_diff"):
        if not isinstance(pp[str_field], str):
            raise CodexOutputParseError(
                f"findings[{finding_index}].patch_proposal.{str_field} must be string, "
                f"got {type(pp[str_field]).__name__}"
            )
    # Type checks on list fields
    for list_field in ("applies_to_findings", "files_touched"):
        if not isinstance(pp[list_field], list):
            raise CodexOutputParseError(
                f"findings[{finding_index}].patch_proposal.{list_field} must be array, "
                f"got {type(pp[list_field]).__name__}"
            )
    if "expected_tests" in pp and not isinstance(pp["expected_tests"], list):
        raise CodexOutputParseError(
            f"findings[{finding_index}].patch_proposal.expected_tests must be array, "
            f"got {type(pp['expected_tests']).__name__}"
        )

    # Non-empty constraints
    if not pp["unified_diff"]:
        raise CodexOutputParseError(
            f"findings[{finding_index}].patch_proposal.unified_diff must be non-empty"
        )
    if not pp["files_touched"]:
        raise CodexOutputParseError(
            f"findings[{finding_index}].patch_proposal.files_touched must be non-empty"
        )

    # iter-0028 F1+F2 (codex-rev-003): reject codex-cli's proprietary
    # `apply_patch` format at validate time. iter-0027 codex emitted this
    # shape on its sole patch_proposal (the iter-0026 F2 applier rejected
    # it at apply time, but only after seal). The standard unified-diff
    # form (--- a/<path> / +++ b/<path> / @@ -L,N +L,N @@) is the only
    # accepted format. The named-error string is grep-friendly for callers.
    diff_text = pp["unified_diff"]
    if diff_text.lstrip().startswith(_APPLY_PATCH_BEGIN_MARKER) or (
        _APPLY_PATCH_UPDATE_MARKER in diff_text
    ):
        raise CodexOutputParseError(
            f"findings[{finding_index}].patch_proposal: "
            f"unified_diff_apply_patch_format_not_supported. The unified_diff "
            f"field uses codex-cli's proprietary apply_patch format "
            f"({_APPLY_PATCH_BEGIN_MARKER!r}/{_APPLY_PATCH_UPDATE_MARKER!r}). "
            f"Use standard unified-diff format with '--- a/<path>' and "
            f"'+++ b/<path>' headers and '@@ -L,N +L,N @@' hunks instead."
        )

    # iter-0028 F4 (codex-rev-002): parse `--- a/<path>` and `+++ b/<path>`
    # headers in the diff body. Every body-referenced path must be in
    # files_touched (declared) AND (when goal_packet is supplied) in
    # allowed_files AND NOT in forbidden_files. /dev/null is conventional
    # create/delete marker and skipped.
    body_paths: set[str] = set()
    for match in _DIFF_FILE_HEADER_RE.finditer(diff_text):
        path = match.group("path").strip()
        if not path or path == "dev/null":
            continue
        body_paths.add(path)
    declared_files = set(pp["files_touched"])
    for body_path in sorted(body_paths):
        if body_path not in declared_files:
            raise CodexOutputParseError(
                f"findings[{finding_index}].patch_proposal: "
                f"unified_diff_body_path_outside_scope: {body_path!r} appears "
                f"in the diff body (--- a/ or +++ b/ header) but is not in "
                f"files_touched {sorted(declared_files)}. Every diff-body path "
                f"must be declared in files_touched so scope-check semantics "
                f"agree across both surfaces."
            )
    if not pp["applies_to_findings"]:
        raise CodexOutputParseError(
            f"findings[{finding_index}].patch_proposal.applies_to_findings must be non-empty"
        )

    # Items in the lists must be strings
    for j, item in enumerate(pp["applies_to_findings"]):
        if not isinstance(item, str):
            raise CodexOutputParseError(
                f"findings[{finding_index}].patch_proposal.applies_to_findings[{j}] "
                f"must be string, got {type(item).__name__}"
            )
    for j, item in enumerate(pp["files_touched"]):
        if not isinstance(item, str):
            raise CodexOutputParseError(
                f"findings[{finding_index}].patch_proposal.files_touched[{j}] "
                f"must be string, got {type(item).__name__}"
            )

    # patch_id regex (iter-0020: codex-producible form, not content-bound)
    if not _PATCH_ID_PATTERN.match(pp["patch_id"]):
        raise CodexOutputParseError(
            f"findings[{finding_index}].patch_proposal.patch_id {pp['patch_id']!r} does not "
            f"match pattern ^codex-rev-\\d+-patch$"
        )

    # patch_id finding-id binding (iter-0020): must equal f"{finding_id}-patch"
    # Replaces the old content-bound formula (patch-{base_sha[:12]}-{sha256(diff)[:12]})
    # which codex's read-only sandbox couldn't compute. Drift detection still
    # works via base_sha + post_sha + the helper-computed unified_diff_sha256
    # stamped on the apply event.
    expected_patch_id = f"{finding_id}-patch"
    if pp["patch_id"] != expected_patch_id:
        raise CodexOutputParseError(
            f"findings[{finding_index}].patch_proposal.patch_id {pp['patch_id']!r} must "
            f"equal {expected_patch_id!r} (derived from this finding's id field)"
        )

    # applies_to_findings must reference IDs present in this review
    for ref in pp["applies_to_findings"]:
        if ref not in all_finding_ids:
            raise CodexOutputParseError(
                f"findings[{finding_index}].patch_proposal.applies_to_findings references "
                f"unknown finding id {ref!r}; known ids: {sorted(all_finding_ids)}"
            )

    # Goal-packet scope checks (skip when goal_packet is None — backward compat
    # for unit-level test invocation; the main pipeline always supplies it).
    if goal_packet is not None:
        # Reuse the same matcher used by the supervisor stop rules so the
        # allowed/forbidden semantics agree across enforcement layers.
        from consensus_mcp._self_drive import _path_in_scope
        allowed = goal_packet.get("allowed_files") or []
        forbidden = goal_packet.get("forbidden_files") or []
        for path in pp["files_touched"]:
            if not _path_in_scope(path, allowed):
                raise CodexOutputParseError(
                    f"findings[{finding_index}].patch_proposal.files_touched path "
                    f"{path!r} is not in goal_packet.allowed_files {allowed}"
                )
            if forbidden and _path_in_scope(path, forbidden):
                raise CodexOutputParseError(
                    f"findings[{finding_index}].patch_proposal.files_touched path "
                    f"{path!r} matches goal_packet.forbidden_files {forbidden}"
                )
        # iter-0028 F4: body-path scope check. body_paths is a strict subset of
        # files_touched (enforced above), but the allowed/forbidden semantics
        # MUST be re-checked against the body-extracted set so the named-error
        # string clearly attributes scope failures to the diff body when they
        # originate there. (For paths already in files_touched, this is a
        # redundant pass — the earlier loop fired. The body-only path here is
        # the sneaky-emission case the validate-time gate exists to catch.)
        for body_path in sorted(body_paths):
            if not _path_in_scope(body_path, allowed):
                raise CodexOutputParseError(
                    f"findings[{finding_index}].patch_proposal: "
                    f"unified_diff_body_path_outside_scope: {body_path!r} "
                    f"is in the diff body but not in goal_packet.allowed_files "
                    f"{allowed}"
                )
            if forbidden and _path_in_scope(body_path, forbidden):
                raise CodexOutputParseError(
                    f"findings[{finding_index}].patch_proposal: "
                    f"unified_diff_body_path_outside_scope: {body_path!r} "
                    f"is in the diff body and matches goal_packet.forbidden_files "
                    f"{forbidden}"
                )

    # iter-0020: helper computes the canonical diff hash and stamps it on the
    # validated patch_proposal output. Codex can't compute this from a
    # read-only sandbox; the helper authoritatively supplies it for downstream
    # drift detection (apply layer's last_mutation.unified_diff_sha256).
    pp["unified_diff_sha256"] = hashlib.sha256(
        pp["unified_diff"].encode("utf-8")
    ).hexdigest()

    # iter-0024 F2 fix: helper now stamps a PER-PATCH base_sha computed against
    # the patch's OWN files_touched subset (a strict subset of the review-packet
    # defect_target.files). The prior iter-0022 logic stamped every patch with
    # the FULL multi-file defect_target.base_sha — but apply.codex_patch
    # computes bundle_sha against the patch's files_touched alone, so a single-
    # file patch derived from a 5-file defect_target failed base_sha_drift on
    # apply. Documented as operational_caveat per_patch_base_sha_reanchor_required
    # in iter-0023/iteration-outcome.yaml.
    #
    # Computation source: review_packet.defect_target.touched_files_contents
    # (authored by _author_review_packet — maps each defect_target file to its
    # full text at review-packet author time). The per-patch bundle_sha is
    # computed against the subset map, matching bundle_sha's canonical form
    # (sorted (normalised_path, sha256(content)) pairs).
    #
    # Backward compat: when touched_files_contents is missing OR any patch
    # file is absent from it, the helper falls back to iter-0022 behaviour
    # (stamps defect_target.base_sha verbatim) so legacy tests + legacy
    # callers don't break.
    if isinstance(review_packet, dict):
        defect_target = review_packet.get("defect_target")
        if isinstance(defect_target, dict):
            # iter-0026 F1: pass repo_root so helper hashes disk bytes (matches
            # apply.codex_patch's bundle_sha contract; eliminates the Windows
            # CRLF mismatch that forced manual re-anchor on every codex patch
            # in iter-0023/0025).
            per_patch_sha = _compute_per_patch_base_sha(
                defect_target,
                pp.get("files_touched") or [],
                repo_root=repo_root,
            )
            if per_patch_sha is not None:
                pp["base_sha"] = per_patch_sha
            else:
                stamped = defect_target.get("base_sha")
                if isinstance(stamped, str) and stamped:
                    pp["base_sha"] = stamped


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


def _sha256_str(text: str) -> str:
    """Return hex-digest sha256 of UTF-8 input. Used for provenance hashing."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_sealed_packet(
    extracted: dict,
    iteration_id: str,
    reviewer_id: str,
    pass_id: str,
    provenance: dict | None = None,
) -> dict:
    """Wrap codex's findings dict in the T6-required outer structure.

    T6 (review.write_and_seal) requires top-level: iteration_id, reviewer_id, findings.
    Optional but conventional: pass_id, goal_satisfied, blocking_objections,
    goal_satisfied_rationale.

    Per F5 (codex review 2026-05-09): an optional `provenance` dict is embedded
    as the `dispatch_provenance` key. T6 SHA-hashes the whole packet, so
    provenance becomes part of the seal — the sealed YAML is independently
    verifiable without consulting dispatch-log.jsonl. Pass None (or omit) to
    skip the provenance block (e.g., in the unit test for _build_sealed_packet
    itself which doesn't run the full pipeline).
    """
    packet = {
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "findings": extracted.get("findings", []),
        "goal_satisfied": extracted.get("goal_satisfied", False),
        "goal_satisfied_rationale": extracted.get("goal_satisfied_rationale", ""),
        "blocking_objections": extracted.get("blocking_objections", []),
        # Per v1.10.3 Windows real-codex smoke: T6's audit_append_event requires
        # `independence_attestation` for review_returned_and_sealed events (schema
        # type: object|null). For auto-codex-dispatch, isolation is guaranteed by
        # construction (codex is spawned with --sandbox read-only, --cd <repo_root>,
        # no peer-review state visible; codex only sees the prompt + goal_packet +
        # review_target). The attestation records this guarantee + references the
        # dispatch_provenance block (which has the cryptographic hashes proving
        # what was reviewed).
        "independence_attestation": {
            "method": "auto_codex_dispatch",
            "reviewer_isolated_by_construction": True,
            "no_peer_review_visible_at_dispatch": True,
            "input_sources": [
                "goal_packet (path passed via --goal-packet)",
                "prompt_template (substituted by _build_prompt)",
                "review_target (path passed via --review-target; may be unspecified)",
            ],
            "see_dispatch_provenance_for_input_hashes": True,
        },
    }
    if provenance is not None:
        packet["dispatch_provenance"] = provenance
    return packet


def _seal_via_t6(packet: dict, iteration_dir: Path) -> dict:
    """Call T6's handle in-process; mirror the sealed YAML to <iteration_dir>/codex-review.yaml.

    T6's actual signature is handle(iteration_id, reviewer_id, pass_id, packet); it
    writes the sealed YAML to its own deterministic archive path under
    consensus-state/archive/review-passes/. We additionally COPY the sealed file to
    <iteration_dir>/codex-review.yaml so the iteration directory has a local copy
    keyed by its iteration name (the auto-dispatch convention).

    Returns a dict with sealed_path (== iteration_dir/codex-review.yaml — the
    iteration-local copy), packet_sha256, archive_sealed_path (T6's path),
    index_updated, audit_event_id.
    """
    from consensus_mcp.tools.review_write_and_seal import handle as t6_handle

    result = t6_handle(
        iteration_id=packet["iteration_id"],
        reviewer_id=packet["reviewer_id"],
        pass_id=packet["pass_id"],
        packet=packet,
    )
    if "error" in result:
        raise RuntimeError(f"T6 seal failed: {result}")

    # Mirror to iteration-local path (auto-dispatch convention).
    archive_path = Path(result["sealed_path"])
    local_path = iteration_dir / "codex-review.yaml"
    shutil.copyfile(str(archive_path), str(local_path))

    return {
        "sealed_path": str(local_path),
        "archive_sealed_path": str(archive_path),
        "packet_sha256": result["packet_sha256"],
        "index_updated": result.get("index_updated"),
        "audit_event_id": result.get("audit_event_id"),
    }


def _log_dispatch(log_path: Path, event: dict) -> None:
    """Append one JSON line to dispatch-log.jsonl.

    Per codex architecture review #4 (2026-05-09), dispatch_done events MUST include:
      - codex_version, prompt_sha256, output_sha256, schema_sha256
      - goal_packet_sha256, scope_signature
      - reviewer_id, pass_id, timeout_seconds, exit_code, sealed_path
    Caller is responsible for populating these fields; this writer just appends.
    Secrets must NEVER be logged. The raw subprocess cmd list is NOT passed to
    this writer; callers log only codex_bin (string) + schema_path (string) +
    timeout_seconds. Raw prompt / codex output / goal_packet content are never
    logged; only their sha256 digests are.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    event_with_ts = {"timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event_with_ts) + "\n")


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

        template_path = (
            _normalize_relative_to_repo(ns.prompt_template, repo_root)
            if ns.prompt_template
            else (Path(__file__).parent / "dispatch_templates" / "codex_review_template.md")
        )
        schema_path = (
            _normalize_relative_to_repo(ns.schema, repo_root)
            if ns.schema
            else (Path(__file__).parent / "dispatch_templates" / "codex_review_schema.json")
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
        goal_packet_text = goal_packet_path.read_text(encoding="utf-8")
        goal_packet = yaml.safe_load(goal_packet_text)
        template_text = _load_template(template_path)

        # review_target_hash is pre-initialized to None outside the try block
        # (v1.10.5) so dispatch_failed events can include it.
        review_packet_data: dict | None = None
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
