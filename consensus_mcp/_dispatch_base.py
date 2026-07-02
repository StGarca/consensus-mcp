"""Phase 4 v1.14 - shared dispatch infrastructure.

Generic helpers reused by every reviewer adapter (codex today; gemini and
any future adapter from iter-0011+). Extracted from _dispatch_codex.py per
iter-0009 verdict Q1: F1b (extract _dispatch_base.py). NO behavior change
versus the pre-extraction codex path - every helper here was copied verbatim
from the original _dispatch_codex.py file.

Adapters import what they need:

    from consensus_mcp._dispatch_base import (
        RepoRootResolutionError, OutsideRepoPathError,
        _resolve_repo_root, _normalize_relative_to_repo,
        _load_goal_packet, _load_template,
        _build_prompt, _terminate_process_tree,
        _compute_per_patch_base_sha,
        _sha256_str, _build_sealed_packet, _seal_via_t6, _log_dispatch,
    )

The reviewer adapter contributes its own:
  - CLI invocation (e.g. _invoke_codex / _invoke_gemini)
  - CLI binary resolution / version probe
  - Output parser specific to the adapter's JSON shape
  - Error class hierarchy
  - main() entrypoint
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# v1.15.7 (A, corrected): serialize dispatch-log.jsonl appends ACROSS
# the threads of one dispatcher process. The streaming _invoke_codex
# emits dispatch events from the main thread AND the stdout/stderr
# reader threads concurrently; an abrupt wall-time-ceiling teardown
# could interleave a bare append -> a torn JSONL line (windows-py3.10
# CI surfaced exactly that). The first corrected attempt reused the
# audit log's `_locked_append` (msvcrt.locking LK_LOCK / fcntl.flock)
# - but that is a BLOCKING CROSS-PROCESS lock; contended across the
# *same* process's threads on Windows it stalled the runner thread
# (windows-py3.12 regression: "runner did not finish"). The actual
# concurrency here is intra-process, so a plain in-process lock is
# the correct, hazard-free primitive. (Cross-process serialization
# of a shared dispatch-log under parallel dispatchers is a separate,
# unobserved concern - deliberately NOT solved with a blocking OS
# lock here; see CHANGELOG v1.15.7.)
_DISPATCH_LOG_LOCK = threading.Lock()

# Per-field char cap for dispatch-log values. A failing adapter can hand the
# writer a multi-megabyte string (e.g. a shutil.Error whose str() embeds the
# entire copied-file manifest from a kimi workdir copytree failure). Logging it
# verbatim produced a single 188 MB JSON line and a 702 MB append-only log in
# the field (2026-06-04 Codex-hosted consult). Cap here, at the one writer every
# adapter shares, so an oversized field is truncated with a marker that preserves
# the original length for debugging.
_MAX_DISPATCH_FIELD_CHARS = 16384


def cap_text_field(text: str, max_chars: int = _MAX_DISPATCH_FIELD_CHARS) -> str:
    """Truncate an oversized text field, appending a marker that preserves the
    original length. The SINGLE source of the cap discipline, shared by the
    dispatch-log writer and the convergence-packet builder (consult
    iteration-approve-two-...-f641f060 Q3 / grok DRY refinement) so the two cannot
    drift apart."""
    if len(text) > max_chars:
        return text[:max_chars] + f"...[truncated {len(text)} chars]"
    return text


def _cap_dispatch_field(value):
    """Truncate an oversized string value, leaving everything else untouched."""
    if isinstance(value, str):
        return cap_text_field(value)
    return value


import yaml


# iter-0010: patch_proposal validation constants moved from _dispatch_codex.py
# so _validate_patch_proposal (also moved here) is fully self-contained at the
# base layer. Per iter-0009 verdict + iter-0010 codex-rev-001 blocking finding.
_PATCH_PROPOSAL_REQUIRED = (
    "patch_id", "applies_to_findings", "base_sha",
    "unified_diff", "files_touched", "expected_tests",
)
_PATCH_PROPOSAL_OPTIONAL = ()
_PATCH_PROPOSAL_ALLOWED = set(_PATCH_PROPOSAL_REQUIRED) | set(_PATCH_PROPOSAL_OPTIONAL)
_PATCH_ID_PATTERN = re.compile(r"^codex-rev-\d+-patch$")

# iter-0028 F4 (codex-rev-002): unified-diff body header parser regex.
_DIFF_FILE_HEADER_RE = re.compile(
    r"^(?:---\s+a/|\+\+\+\s+b/)(?P<path>\S+)\s*$",
    re.MULTILINE,
)
# iter-0028 F1+F2 (codex-rev-003): tokens that signal codex-cli's proprietary
# `apply_patch` format. Naming retains the codex-cli reference because that's
# the originating format; the validator applies the rejection regardless of
# which adapter calls it (gemini, future adapters).
_APPLY_PATCH_BEGIN_MARKER = "*** Begin Patch"
_APPLY_PATCH_UPDATE_MARKER = "*** Update File"


# M1 (consult iteration-m1-hardening-design-4d7d2469) Q2: the blessed shared
# resolver + error class. Imported at module level (no cycle: _paths imports
# only stdlib).
from consensus_mcp._paths import RepoRootError as _SharedRepoRootError
from consensus_mcp._paths import resolve_repo_root as _shared_resolve_repo_root

# Per v1.10.4 F1 (codex review on 0ae7b80d): repo_root resolution must fail-closed.
# Repo markers: directories that MUST exist at the resolved root for it to be a valid
# consensus-mcp / consensus-mcp repo. site-packages doesn't have these; cwd in a
# random directory doesn't either; the actual repo does.
_REPO_ROOT_MARKERS = ("consensus-state", "consensus_mcp", "consensus_mcp/validators")


class RepoRootResolutionError(_SharedRepoRootError):
    """Raised when repo_root cannot be resolved to a valid repo (no markers found).

    M1 (consult iteration-m1-hardening-design-4d7d2469) Q2: now subclasses the
    shared _paths.RepoRootError (itself a RuntimeError, preserving the pinned
    issubclass(RepoRootResolutionError, RuntimeError) contract) so callers can
    treat the whole resolver family with one except clause.
    """


def _has_repo_markers(candidate: Path) -> bool:
    """True iff candidate is a valid consensus root - EITHER the consensus-mcp
    source tree (all _REPO_ROOT_MARKERS) OR a CONSUMING project that ran
    `consensus init` (has `.consensus/config.yaml`).

    The consuming-project case is essential and was the cold-start blocker: every
    consensus operation (scaffold, dispatch, seal, approve, the gate marker)
    targets the consuming PROJECT root (where `consensus-state/` + `.consensus/`
    live), which NEVER contains consensus-mcp's own source markers. Without this,
    a tool run in a consuming project either failed marker validation or silently
    resolved to consensus-mcp's OWN repo (observed: a dry-run scaffold armed the
    gate in the wrong tree). Still rejects site-packages (neither marker set),
    preserving the v1.10.4 fix. Cross-platform (pathlib only)."""
    if all((candidate / marker).is_dir() for marker in _REPO_ROOT_MARKERS):
        return True
    if (candidate / ".consensus" / "config.yaml").is_file():
        return True
    return False


def derive_pass_id(iteration_id: str, review_target, reviewer_id: str) -> str:
    """Default dispatch pass_id = hash of (iteration#, packet name, contributor).

    The T6 seal index is a GLOBAL pass_id namespace (a content-identity tamper
    guard: a given pass_id must always carry the same content). The legacy default
    `<reviewer>-pass1` is identical across EVERY iteration, so two consults collide
    with a cryptic `index_collision` (observed twice 2026-06-02). Hashing the
    dispatch coordinates makes the pass_id globally unique per (iteration, packet,
    contributor) yet deterministic + idempotent for an identical re-dispatch.
    Operator design 2026-06-02. Result: `<reviewer>-<16 hex>` (filesystem-safe,
    readable; reviewer_id is also folded into the hash).
    """
    packet = "" if review_target is None else Path(review_target).name
    key = f"{iteration_id}\x1f{packet}\x1f{reviewer_id}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", str(reviewer_id))
    return f"{safe_prefix}-{digest}"


def _resolve_repo_root() -> Path:
    """Resolve the repo root, fail-closed if no valid candidate is found.

    Per v1.10.4 F1 hardening: the prior fallback to
    Path(__file__).resolve().parent.parent was unsafe - when the helper
    runs as an installed module from python_env/Lib/site-packages, that fallback
    landed at python_env/Lib (NOT the repo root), causing codex --cd, T6 archive
    writes, and dispatch-log writes to all target the wrong tree.

    M1 (consult iteration-m1-hardening-design-4d7d2469) Q2: now a shim over
    the ONE blessed resolver (_paths.resolve_repo_root). This site's
    DOCUMENTED extras are preserved: it keeps BOTH env keys AND additionally
    VALIDATES the operator env override against repo markers (authoritative
    raise, pinned by test_dispatch_codex's F5 tests), which the shared
    resolver deliberately does not do. The old marker-validated
    Path(__file__)-parent walk is GONE (never __file__-derived roots in
    package code); discovery is the shared cwd-ancestor containment-marker
    walk.

    If no candidate validates, raise RepoRootResolutionError with a clear
    operator-facing message naming the env var to set. Never silently fall
    back to site-packages.
    """
    # CONSENSUS_MCP_PROJECT_ROOT is what `consensus init` writes into a consuming
    # project's .mcp.json (the consensus-mcp server is launched with it); honor it
    # alongside the legacy CONSENSUS_MCP_REPO_ROOT so consuming-project tools
    # resolve the PROJECT root, not consensus-mcp's install.
    override = (os.environ.get("CONSENSUS_MCP_REPO_ROOT")
                or os.environ.get("CONSENSUS_MCP_PROJECT_ROOT"))
    if override:
        candidate = Path(override).resolve()
        if _has_repo_markers(candidate):
            return candidate
        # iter-0028 F5 (codex-rev-004): operator-supplied env var is
        # authoritative. If set but the path fails marker validation, do NOT
        # silently fall through to cwd / __file__ candidates - the operator's
        # intent was an explicit override, and silent re-resolution invites
        # the very confusion the env var was meant to eliminate. Raise with
        # a clear message naming the env var. Empty-string env (treated as
        # falsy above) is the "unset" case and DOES fall through.
        raise RepoRootResolutionError(
            f"CONSENSUS_MCP_REPO_ROOT/CONSENSUS_MCP_PROJECT_ROOT={override!r} was "
            f"set but the path {candidate} is not a consensus root: it has neither "
            f"the consensus-mcp source markers {_REPO_ROOT_MARKERS} (as "
            f"subdirectories) NOR a consuming-project '.consensus/config.yaml' "
            f"(written by `consensus init`). Not falling through to cwd / __file__ "
            f"discovery - the operator-supplied env var is authoritative. Either "
            f"fix the path, run `consensus init` there first, or unset the env var "
            f"to use automatic discovery."
        )

    # M1 Q2: env keys were consumed (and validated) above, so the shared walk
    # runs with env_keys=(). Nearest cwd-ancestor with `.consensus/` or
    # `consensus-state/` wins - the same walk every other entry point uses.
    try:
        return _shared_resolve_repo_root(env_keys=())
    except _SharedRepoRootError as exc:
        shared_diag = str(exc)

    raise RepoRootResolutionError(
        f"Cannot resolve consensus-mcp repo root. {shared_diag}\n"
        f"\n"
        f"-- BOOTSTRAP A CONSUMER PROJECT (v1.32.0 - Section 3.1 friction fix) --\n"
        f"If you installed consensus-mcp via pipx and want to use it ON another\n"
        f"project (not the consensus-mcp repo itself), run these 4 commands at\n"
        f"the project root to create the required containment markers, then\n"
        f"re-run the dispatch:\n"
        f"\n"
        f"    mkdir -p consensus_mcp/validators consensus-state\n"
        f"    printf '%s\\n' '/consensus_mcp/' '/consensus-state/' '/.consensus/' >> .gitignore\n"
        f"    export CONSENSUS_MCP_REPO_ROOT=\"$PWD\"\n"
        f"    # then re-run the dispatch from this directory\n"
        f"\n"
        f"Or use `consensus-init` to bootstrap a fully managed install:\n"
        f"    pipx install git+https://github.com/StGarca/consensus-mcp.git@v1.32.0\n"
        f"    consensus-init  # interactive setup\n"
        f"\n"
        f"See docs/consensus/operations/first-consult-quickstart.md (packaged with\n"
        f"the install) for the full Path A workflow."
    )


def validate_explicit_repo_root(repo_root: str | os.PathLike) -> Path:
    """Validate an OPERATOR-SUPPLIED repo root the same way auto-discovery does.

    Resolves `repo_root` and requires consensus repo markers (the consensus-mcp
    source markers OR a consuming project's `.consensus/config.yaml`). Raises
    `RepoRootResolutionError` naming PROJECT_ROOT/.consensus on failure.

    Codex finding (cold-start blind consult): the composed flows
    (`_approve_consult`, `_start_consult`) previously accepted an explicit
    `--repo-root` VERBATIM (`Path(repo_root).resolve()`) with no marker check, so
    a typo'd or arbitrary path could arm the gate / seal an outcome into a tree
    that is not a consensus project at all. The env-var branch of
    `_resolve_repo_root` already validates; an explicit `--repo-root` must be held
    to the SAME bar (one resolver, one contract - Finding #7)."""
    candidate = Path(repo_root).resolve()
    if _has_repo_markers(candidate):
        return candidate
    raise RepoRootResolutionError(
        f"--repo-root {str(repo_root)!r} resolved to {candidate} but it is not a "
        f"consensus project root: it has neither the consensus-mcp source markers "
        f"{_REPO_ROOT_MARKERS} nor a consuming-project '.consensus/config.yaml' "
        f"(written by `consensus init`). Run `consensus init` there first, or point "
        f"--repo-root / CONSENSUS_MCP_PROJECT_ROOT at an initialized project root."
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
    too - NOT against the process cwd (which may differ if a future MCP wrapper
    or service caller invokes us from elsewhere).

    Per v1.10.5 containment hardening: after resolution the path MUST be inside
    repo_root. Absolute paths previously passed through unchanged via p.resolve(),
    which let any caller supply (or have an MCP tool wrapper pass through) an
    out-of-tree absolute path and have its contents pulled into the codex prompt.
    That's a real boundary leak - review-target/goal-packet/schema/template are
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
            f"the repo. Move the file into the repo or pass a path relative to it.\n"
            f"\n"
            f"-- DEBUG TIP (v1.32.0) --\n"
            f"If you bootstrapped containment markers under a subdirectory (e.g.\n"
            f"`.consensus/runtime/`), repo_root resolves to that subdir and the\n"
            f"iteration files under `.consensus/iterations/<iter>` become\n"
            f"OUT-OF-REPO siblings (Section 3.2 friction). Put the markers at\n"
            f"the PROJECT ROOT instead:\n"
            f"\n"
            f"    mkdir -p consensus_mcp/validators consensus-state\n"
            f"    export CONSENSUS_MCP_REPO_ROOT=\"$PWD\""
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
    review_target_content: str | None = None,
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
                # rather than raising - the helper writer is the type guard.
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
    # Bug B fix (v1.30.2, COMPLETED v1.30.5): embed the review-target's CONTENT, not just
    # its path+hash. A read-only reviewer sandbox (codex) cannot open the convergence packet
    # under consensus-state/, so naming the path is not enough. The (unsandboxed) dispatcher
    # already read this text to compute review_target_hash; we inline it here.
    #
    # v1.30.5: ALWAYS embed when we have the content - the old `and not touched_contents`
    # guard had a deadlock hole. In a CONVERGENCE dispatch the touched_files_contents are the
    # round's CONSTITUENT files (the prior round's review artifacts), NOT the review-target
    # (the convergence-packet YAML), so the guard SUPPRESSED the target embed exactly when
    # touched_files was present; the reviewer, pointed at convergence-packet-round-N.yaml
    # (which is not in the touched set), got "canonical target not provided" and every
    # multi-round consult deadlocked. The target block is ADDITIVE to the touched-files block
    # (distinct content) - embedding both is correct, not a double-embed.
    if review_target_content:
        out += (
            "\n\n## REVIEW TARGET CONTENT (embedded - the reviewer sandbox cannot read "
            "files under consensus-state/)\n\n```\n"
            + review_target_content
            + "\n```\n"
        )
    return out


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

    # Legacy text-encoding fallback (no repo_root supplied - typically a
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


def _sha256_str(text: str) -> str:
    """Return hex-digest sha256 of UTF-8 input. Used for provenance hashing."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_sealed_packet(
    extracted: dict,
    iteration_id: str,
    reviewer_id: str,
    pass_id: str,
    provenance: dict | None = None,
    attestation_method: str = "auto_codex_dispatch",
    attestation_input_sources: list[str] | None = None,
) -> dict:
    """Wrap a reviewer's findings dict in the T6-required outer structure.

    T6 (review.write_and_seal) requires top-level: iteration_id, reviewer_id, findings.
    Optional but conventional: pass_id, goal_satisfied, blocking_objections,
    goal_satisfied_rationale.

    Per F5 (codex review 2026-05-09): an optional `provenance` dict is embedded
    as the `dispatch_provenance` key. T6 SHA-hashes the whole packet, so
    provenance becomes part of the seal - the sealed YAML is independently
    verifiable without consulting dispatch-log.jsonl. Pass None (or omit) to
    skip the provenance block (e.g., in the unit test for _build_sealed_packet
    itself which doesn't run the full pipeline).

    iter-0010 adapter-agnostic: attestation_method defaults to "auto_codex_dispatch"
    and attestation_input_sources defaults to the codex-specific list, preserving
    pre-extraction behavior for the codex caller. Future adapters (gemini, ...)
    pass their own values.
    """
    if attestation_input_sources is None:
        attestation_input_sources = [
            "goal_packet (path passed via --goal-packet)",
            "prompt_template (substituted by _build_prompt)",
            "review_target (path passed via --review-target; may be unspecified)",
        ]

    # iter-0028: detect proposal-mode payload. A proposal-shape `extracted`
    # has `selected_target` (or sets `structural_abstention=true`) and
    # lacks the review-shape fields. Sealed packet keeps the T6-required
    # outer structure (findings stays present-and-empty so audit-event
    # schema doesn't change) and additionally embeds the proposal payload
    # under a top-level `proposal` key so the workflow engine and
    # converged-plan authoring can read it directly.
    is_proposal = (
        "selected_target" in extracted
        or extracted.get("structural_abstention") is True
        or "rationale_vs_alternatives" in extracted
    )

    packet = {
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "findings": extracted.get("findings", []),
        "goal_satisfied": (
            # In proposal mode, a non-abstaining contributor "satisfies" by
            # producing a target; an abstention is treated as not-satisfied.
            (not extracted.get("structural_abstention", False))
            if is_proposal
            else extracted.get("goal_satisfied", False)
        ),
        "goal_satisfied_rationale": (
            extracted.get("rationale_vs_alternatives", "")
            if is_proposal
            else extracted.get("goal_satisfied_rationale", "")
        ),
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
            "method": attestation_method,
            "reviewer_isolated_by_construction": True,
            "no_peer_review_visible_at_dispatch": True,
            "input_sources": attestation_input_sources,
            "see_dispatch_provenance_for_input_hashes": True,
        },
    }
    if provenance is not None:
        packet["dispatch_provenance"] = provenance

    if is_proposal:
        packet["proposal"] = {
            "selected_target": extracted.get("selected_target"),
            "rationale_vs_alternatives": extracted.get("rationale_vs_alternatives", ""),
            "deliverable_scope": extracted.get("deliverable_scope"),
            "risks": extracted.get("risks", []),
            "estimated_complexity": extracted.get("estimated_complexity"),
            "structural_abstention": extracted.get("structural_abstention", False),
        }
    return packet


def _seal_via_t6(
    packet: dict,
    iteration_dir: Path,
    sealed_filename: str = "codex-review.yaml",
) -> dict:
    """Call T6's handle in-process; mirror the sealed YAML to <iteration_dir>/<sealed_filename>.

    T6's actual signature is handle(iteration_id, reviewer_id, pass_id, packet); it
    writes the sealed YAML to its own deterministic archive path under
    consensus-state/archive/review-passes/. We additionally COPY the sealed file to
    <iteration_dir>/<sealed_filename> so the iteration directory has a local copy
    keyed by its iteration name (the auto-dispatch convention).

    iter-0010 adapter-agnostic: sealed_filename defaults to "codex-review.yaml"
    preserving pre-extraction behavior. Future adapters pass e.g. "gemini-review.yaml".

    Returns a dict with sealed_path (== iteration_dir/<sealed_filename> - the
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
    local_path = iteration_dir / sealed_filename
    shutil.copyfile(str(archive_path), str(local_path))

    return {
        "sealed_path": str(local_path),
        "archive_sealed_path": str(archive_path),
        "packet_sha256": result["packet_sha256"],
        "index_updated": result.get("index_updated"),
        "audit_event_id": result.get("audit_event_id"),
    }


def _validate_patch_proposal(
    finding_index: int,
    finding_id: str,
    pp: dict,
    all_finding_ids: set,
    goal_packet: dict | None,
    review_packet: dict | None = None,
    repo_root: Path | None = None,
    error_class: type = ValueError,
) -> None:
    """Validate a single patch_proposal block per Task #24 (iter-0014) binding rules.

    iter-0010: moved from _dispatch_codex.py into the shared base. The
    ``error_class`` kwarg lets each adapter pass its own parser error class
    (e.g. codex passes CodexOutputParseError) so adapter-specific exception
    hierarchies are preserved. Default ValueError is the safe generic fallback
    for callers that don't care which error type is raised.

    Raises ``error_class`` on any violation. MUTATES the supplied ``pp`` dict
    by stamping ``unified_diff_sha256`` (helper-computed) so downstream consumers
    see the canonical diff hash without recomputing.

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
        raise error_class(
            f"findings[{finding_index}].patch_proposal must be object, got {type(pp).__name__}"
        )
    # Closed-key set; reject extras (anti-self-verification claims like 'verified')
    unknown = set(pp.keys()) - _PATCH_PROPOSAL_ALLOWED
    if unknown:
        raise error_class(
            f"findings[{finding_index}].patch_proposal has unexpected keys: {sorted(unknown)}"
        )
    # Required fields present
    for required in _PATCH_PROPOSAL_REQUIRED:
        if required not in pp:
            raise error_class(
                f"findings[{finding_index}].patch_proposal missing required field: {required!r}"
            )
    # Type checks on string fields
    for str_field in ("patch_id", "base_sha", "unified_diff"):
        if not isinstance(pp[str_field], str):
            raise error_class(
                f"findings[{finding_index}].patch_proposal.{str_field} must be string, "
                f"got {type(pp[str_field]).__name__}"
            )
    # Type checks on list fields
    for list_field in ("applies_to_findings", "files_touched"):
        if not isinstance(pp[list_field], list):
            raise error_class(
                f"findings[{finding_index}].patch_proposal.{list_field} must be array, "
                f"got {type(pp[list_field]).__name__}"
            )
    if "expected_tests" in pp and not isinstance(pp["expected_tests"], list):
        raise error_class(
            f"findings[{finding_index}].patch_proposal.expected_tests must be array, "
            f"got {type(pp['expected_tests']).__name__}"
        )

    # Non-empty constraints
    if not pp["unified_diff"]:
        raise error_class(
            f"findings[{finding_index}].patch_proposal.unified_diff must be non-empty"
        )
    if not pp["files_touched"]:
        raise error_class(
            f"findings[{finding_index}].patch_proposal.files_touched must be non-empty"
        )

    # iter-0028 F1+F2 (codex-rev-003): reject codex-cli's proprietary
    # `apply_patch` format at validate time.
    diff_text = pp["unified_diff"]
    if diff_text.lstrip().startswith(_APPLY_PATCH_BEGIN_MARKER) or (
        _APPLY_PATCH_UPDATE_MARKER in diff_text
    ):
        raise error_class(
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
            raise error_class(
                f"findings[{finding_index}].patch_proposal: "
                f"unified_diff_body_path_outside_scope: {body_path!r} appears "
                f"in the diff body (--- a/ or +++ b/ header) but is not in "
                f"files_touched {sorted(declared_files)}. Every diff-body path "
                f"must be declared in files_touched so scope-check semantics "
                f"agree across both surfaces."
            )
    if not pp["applies_to_findings"]:
        raise error_class(
            f"findings[{finding_index}].patch_proposal.applies_to_findings must be non-empty"
        )

    # Items in the lists must be strings
    for j, item in enumerate(pp["applies_to_findings"]):
        if not isinstance(item, str):
            raise error_class(
                f"findings[{finding_index}].patch_proposal.applies_to_findings[{j}] "
                f"must be string, got {type(item).__name__}"
            )
    for j, item in enumerate(pp["files_touched"]):
        if not isinstance(item, str):
            raise error_class(
                f"findings[{finding_index}].patch_proposal.files_touched[{j}] "
                f"must be string, got {type(item).__name__}"
            )

    # patch_id regex (iter-0020: codex-producible form, not content-bound)
    if not _PATCH_ID_PATTERN.match(pp["patch_id"]):
        raise error_class(
            f"findings[{finding_index}].patch_proposal.patch_id {pp['patch_id']!r} does not "
            f"match pattern ^codex-rev-\\d+-patch$"
        )

    # patch_id finding-id binding (iter-0020): must equal f"{finding_id}-patch"
    expected_patch_id = f"{finding_id}-patch"
    if pp["patch_id"] != expected_patch_id:
        raise error_class(
            f"findings[{finding_index}].patch_proposal.patch_id {pp['patch_id']!r} must "
            f"equal {expected_patch_id!r} (derived from this finding's id field)"
        )

    # applies_to_findings must reference IDs present in this review
    for ref in pp["applies_to_findings"]:
        if ref not in all_finding_ids:
            raise error_class(
                f"findings[{finding_index}].patch_proposal.applies_to_findings references "
                f"unknown finding id {ref!r}; known ids: {sorted(all_finding_ids)}"
            )

    # Goal-packet scope checks (skip when goal_packet is None - backward compat
    # for unit-level test invocation; the main pipeline always supplies it).
    if goal_packet is not None:
        # Reuse the same matcher used by the supervisor stop rules so the
        # allowed/forbidden semantics agree across enforcement layers.
        from consensus_mcp._self_drive import _path_in_scope
        allowed = goal_packet.get("allowed_files") or []
        forbidden = goal_packet.get("forbidden_files") or []
        for path in pp["files_touched"]:
            if not _path_in_scope(path, allowed):
                raise error_class(
                    f"findings[{finding_index}].patch_proposal.files_touched path "
                    f"{path!r} is not in goal_packet.allowed_files {allowed}"
                )
            if forbidden and _path_in_scope(path, forbidden):
                raise error_class(
                    f"findings[{finding_index}].patch_proposal.files_touched path "
                    f"{path!r} matches goal_packet.forbidden_files {forbidden}"
                )
        for body_path in sorted(body_paths):
            if not _path_in_scope(body_path, allowed):
                raise error_class(
                    f"findings[{finding_index}].patch_proposal: "
                    f"unified_diff_body_path_outside_scope: {body_path!r} "
                    f"is in the diff body but not in goal_packet.allowed_files "
                    f"{allowed}"
                )
            if forbidden and _path_in_scope(body_path, forbidden):
                raise error_class(
                    f"findings[{finding_index}].patch_proposal: "
                    f"unified_diff_body_path_outside_scope: {body_path!r} "
                    f"is in the diff body and matches goal_packet.forbidden_files "
                    f"{forbidden}"
                )

    # iter-0020: helper computes the canonical diff hash and stamps it on the
    # validated patch_proposal output.
    pp["unified_diff_sha256"] = hashlib.sha256(
        pp["unified_diff"].encode("utf-8")
    ).hexdigest()

    # iter-0024 F2 fix + iter-0026 F1: per-patch base_sha stamp against the
    # patch's OWN files_touched subset, using disk bytes when repo_root supplied.
    if isinstance(review_packet, dict):
        defect_target = review_packet.get("defect_target")
        if isinstance(defect_target, dict):
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
    capped = {k: _cap_dispatch_field(v) for k, v in event.items()}
    event_with_ts = {"timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **capped}
    line = json.dumps(event_with_ts) + "\n"
    # v1.15.7 (A, corrected): serialize concurrent emitters (main +
    # stdout/stderr reader threads) of THIS process with an in-process
    # lock so a line is never torn/interleaved - including during an
    # abrupt teardown. A plain locked text append (no blocking OS
    # syscall) is the correct, deadlock-free primitive for intra-process
    # thread concurrency. See `_DISPATCH_LOG_LOCK` rationale above.
    with _DISPATCH_LOG_LOCK:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)


def record_reader_error(buf_list: list, stream_name: str, exc: BaseException) -> None:
    """Record a pipe-reader-thread crash INTO the captured output buffer.

    Dispatcher reader threads previously swallowed exceptions with a bare
    `except Exception: pass`, so a reader crash silently stopped draining the
    pipe and the child's real error context was lost. Instead, append an ASCII
    marker line so the failure surfaces in the captured stdout/stderr that
    downstream logging/diagnosis already inspects. The buffers hold raw bytes
    lines (binary pipes), so the marker is encoded; appending is GIL-atomic so
    no extra locking is needed. Never raises (a logging failure must not take
    down the reader's except handler).
    """
    try:
        marker = (
            f"[consensus-mcp] {stream_name} reader thread error: "
            f"{type(exc).__name__}: {exc}\n"
        )
        buf_list.append(marker.encode("utf-8", errors="replace"))
    except Exception:
        pass


# Ambient API-key env vars scrubbed from each adapter subprocess. Every CLI
# authenticates via its own file/OAuth credentials; a stray API key in the
# parent environment could hijack that auth and route the request through an
# external key (kimi's documented rationale, now applied to every adapter).
CODEX_SCRUBBED_ENV_KEYS = ("OPENAI_API_KEY",)
GEMINI_SCRUBBED_ENV_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
GROK_SCRUBBED_ENV_KEYS = ("XAI_API_KEY", "GROK_API_KEY")
KIMI_SCRUBBED_ENV_KEYS = ("KIMI_API_KEY", "OPENAI_API_KEY")

# Union of every provider's keys - the scrub set for any subprocess that
# runs builder-authored or otherwise-untrusted code (architect-build's
# write-enabled builder dispatch + the frozen verification gate). The
# decisive experiment (2026-06-10) proved that subprocess is effectively
# unsandboxed with network access, so it must not inherit ANY provider
# credential it could exfiltrate; codex's own auth is token-based (~/.codex),
# not these env keys, so scrubbing them does not break the builder.
ALL_PROVIDER_SCRUBBED_ENV_KEYS = tuple(dict.fromkeys(
    CODEX_SCRUBBED_ENV_KEYS + GEMINI_SCRUBBED_ENV_KEYS
    + GROK_SCRUBBED_ENV_KEYS + KIMI_SCRUBBED_ENV_KEYS
))


def scrub_env_keys(env: dict, keys: tuple[str, ...]) -> dict:
    """Pop each key in `keys` from `env` (if present) and return `env`.

    Shared primitive for removing ambient API keys from an adapter subprocess
    environment so file/OAuth-based CLI auth cannot be hijacked. Mutates and
    returns the passed dict (callers pass a fresh os.environ copy).
    """
    for key in keys:
        env.pop(key, None)
    return env


# ---- Default-deny env isolation (consult iteration-architect-hardening-
# 2026-06-11, Q2 - unanimous panel dissent from a denylist floor) ----
#
# Both the write-enabled builder dispatch AND the frozen verification gate
# execute builder-authored code with network access; a denylist's
# false-negative surface is unbounded (KUBECONFIG, NETRC, CUSTOM_CORP_SECRET,
# future provider names), so both get default-deny composition:
#   env = (BASE allowlist [+ Windows set on nt] [+ verification toolchain
#          preset] [+ operator CONSENSUS_MCP_<ROLE>_ENV_ALLOW])
#         minus the HARD-FLOOR credential set, which explicit allows can
#         NEVER override (exact names only - suffix-pattern names like a
#         dummy FOO_TOKEN a suite needs CAN pass via explicit allow, so the
#         escape hatch stays usable for false positives).

SUBPROCESS_ENV_ALLOW_EXACT = (
    "HOME", "USER", "LOGNAME", "PATH", "SHELL", "TMPDIR", "TEMP", "TMP",
    "TERM", "LANG", "TZ", "PYTHONUTF8", "PYTHONIOENCODING", "NO_COLOR",
    "CODEX_HOME",
)
SUBPROCESS_ENV_ALLOW_PREFIXES = ("LC_",)
WINDOWS_ENV_ALLOW_EXACT = (
    "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "OS",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)
# Non-credential toolchain roots real test suites commonly need; anything
# beyond these is an explicit, auditable operator ALLOW.
VERIFICATION_TOOLCHAIN_ALLOW_EXACT = (
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "JAVA_HOME", "GOPATH",
    "GOROOT", "CARGO_HOME", "RUSTUP_HOME", "NODE_PATH", "NVM_DIR",
    "GRADLE_USER_HOME", "M2_HOME", "CI",
)
# Exact-name hard floor: known-real credentials and credential-file/path
# vectors (grok). Uppercase-compared; an explicit allow cannot readmit them.
CREDENTIAL_ENV_HARD_SCRUB = tuple(dict.fromkeys(
    ALL_PROVIDER_SCRUBBED_ENV_KEYS + (
        "GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "SSH_AUTH_SOCK", "NPM_TOKEN", "NODE_AUTH_TOKEN", "PYPI_TOKEN",
        "TWINE_USERNAME", "TWINE_PASSWORD", "HF_TOKEN",
        "OPENROUTER_API_KEY", "CLOUDFLARE_API_TOKEN", "VERCEL_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS", "KUBECONFIG", "DOCKER_HOST",
        "DOCKER_CONFIG", "NETRC", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    )
))


def build_isolated_env(role: str) -> dict:
    """Default-deny subprocess environment for `role` ('builder' or
    'verification'). See the composition note above. Key matching is
    case-insensitive (Windows env semantics); original key case is kept."""
    allow = set(SUBPROCESS_ENV_ALLOW_EXACT)
    if os.name == "nt":
        allow.update(WINDOWS_ENV_ALLOW_EXACT)
    if role == "verification":
        allow.update(VERIFICATION_TOOLCHAIN_ALLOW_EXACT)
        extra = os.environ.get("CONSENSUS_MCP_VERIFICATION_ENV_ALLOW", "")
    else:
        extra = os.environ.get("CONSENSUS_MCP_BUILDER_ENV_ALLOW", "")
    allow.update(k.strip() for k in extra.split(",") if k.strip())
    allow_upper = {k.upper() for k in allow}
    hard_upper = {k.upper() for k in CREDENTIAL_ENV_HARD_SCRUB}
    env: dict = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in hard_upper:
            continue
        if upper in allow_upper or any(
            upper.startswith(p) for p in SUBPROCESS_ENV_ALLOW_PREFIXES
        ):
            env[key] = value
    return env


def build_failed_event(
    *,
    adapter: str,
    error_type: str,
    error: str,
    reviewer_id: str,
    pass_id: str,
    iteration_id: str,
    timeout_seconds,
    extra_fields: dict | None = None,
) -> dict:
    """Construct the shared dispatch_failed event skeleton.

    The four adapters each defined their own `_failed_event` closure and the
    shapes had drifted (codex lacked `adapter`; grok adds disabled_tools and
    prompt_file_path; etc.). This builder owns the true intersection plus the
    `adapter` discriminator; adapter-specific provenance goes in
    `extra_fields`, whose None values are SKIPPED - matching the existing
    "include only the hashes that were actually computed" behavior.
    """
    ev = {
        "event": "dispatch_failed",
        "error_type": error_type,
        "error": error,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "iteration_id": iteration_id,
        "timeout_seconds": timeout_seconds,
        "adapter": adapter,
    }
    for k, v in (extra_fields or {}).items():
        if v is not None:
            ev[k] = v
    return ev


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_from_text(text: str) -> str:
    """Extract a JSON object substring from a CLI's free-form output.

    gemini/grok/kimi lack codex's --output-schema enforcement, so their
    responses may include leading prose, markdown fences, or trailing
    commentary even when explicitly told to emit JSON only. This helper
    applies a small ladder of recoveries:

      1. Whole-string trim - if the trimmed text starts with `{` and ends
         with `}`, return as-is.
      2. Fenced-code-block extraction - first ```json ... ``` (or bare
         ``` ... ```) containing a `{...}` block.
      3. Greedy outermost-brace match - first `{` to last `}`. Fragile
         (won't handle nested unbalanced braces), but a final fallback.

    Returns the candidate JSON string. Does NOT validate that it parses;
    the caller does. If nothing matches, returns the original text so the
    JSONDecodeError downstream carries the actual CLI output as diagnostic.
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
