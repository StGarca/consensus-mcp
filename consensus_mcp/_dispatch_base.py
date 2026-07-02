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

import yaml


# v1.15.7 (A, corrected): serialize dispatch-log.jsonl appends ACROSS
# the threads of one dispatcher process.
_DISPATCH_LOG_LOCK = threading.Lock()

# Per-field char cap for dispatch-log values.
_MAX_DISPATCH_FIELD_CHARS = 16384


def cap_text_field(text: str, max_chars: int = _MAX_DISPATCH_FIELD_CHARS) -> str:
    if len(text) > max_chars:
        return text[:max_chars] + f"...[truncated {len(text)} chars]"
    return text


def _cap_dispatch_field(value):
    if isinstance(value, str):
        return cap_text_field(value)
    return value


# iter-0010: patch_proposal validation constants
_PATCH_PROPOSAL_REQUIRED = (
    "patch_id", "applies_to_findings", "base_sha",
    "unified_diff", "files_touched", "expected_tests",
)
_PATCH_PROPOSAL_OPTIONAL = ()
_PATCH_PROPOSAL_ALLOWED = set(_PATCH_PROPOSAL_REQUIRED) | set(_PATCH_PROPOSAL_OPTIONAL)
_PATCH_ID_PATTERN = re.compile(r"^codex-rev-\d+-patch$")

# iter-0028 F4 (codex-rev-002): unified-diff body header parser regex.
_DIFF_FILE_HEADER_RE = re.compile(
    r"^(?:---\s+a/|\+\+\+ b/)(?P<path>\S+)\s*$",
    re.MULTILINE,
)
_APPLY_PATCH_BEGIN_MARKER = "*** Begin Patch"
_APPLY_PATCH_UPDATE_MARKER = "*** Update File"


# Per v1.10.4 F1: repo_root resolution must fail-closed.
_REPO_ROOT_MARKERS = ("consensus-state", "consensus_mcp", "consensus_mcp/validators")


class RepoRootResolutionError(RuntimeError):
    """Raised when repo_root cannot be resolved to a valid repo (no markers found)."""


def _has_repo_markers(candidate: Path) -> bool:
    if all((candidate / marker).is_dir() for marker in _REPO_ROOT_MARKERS):
        return True
    if (candidate / ".consensus" / "config.yaml").is_file():
        return True
    return False


def derive_pass_id(iteration_id: str, review_target, reviewer_id: str) -> str:
    packet = "" if review_target is None else Path(review_target).name
    key = f"{iteration_id}\x1f{packet}\x1f{reviewer_id}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", str(reviewer_id))
    return f"{safe_prefix}-{digest}"


def _resolve_repo_root() -> Path:
    candidates_tried: list[tuple[str, Path]] = []
    override = (os.environ.get("CONSENSUS_MCP_REPO_ROOT")
                or os.environ.get("CONSENSUS_MCP_PROJECT_ROOT"))
    if override:
        candidate = Path(override).resolve()
        candidates_tried.append(("CONSENSUS_MCP_REPO_ROOT/PROJECT_ROOT", candidate))
        if _has_repo_markers(candidate):
            return candidate
        raise RepoRootResolutionError(f"Root {override!r} is not a consensus root.")

    cwd = Path.cwd().resolve()
    candidates_tried.append(("Path.cwd()", cwd))
    if _has_repo_markers(cwd):
        return cwd

    for parent in cwd.parents:
        candidates_tried.append((f"cwd ancestor ({parent.name})", parent))
        if _has_repo_markers(parent):
            return parent

    here = Path(__file__).resolve()
    for parent in (here.parent, here.parent.parent, here.parent.parent.parent):
        candidates_tried.append((f"parent of __file__ ({parent.name})", parent))
        if _has_repo_markers(parent):
            return parent

    tried_msg = "; ".join(f"{name}={path}" for name, path in candidates_tried)
    raise RepoRootResolutionError(f"Cannot resolve repo root. Tried: {tried_msg}")


def validate_explicit_repo_root(repo_root: str | os.PathLike) -> Path:
    candidate = Path(repo_root).resolve()
    if _has_repo_markers(candidate):
        return candidate
    raise RepoRootResolutionError(f"--repo-root {repo_root!r} is not a consensus root.")


class OutsideRepoPathError(ValueError):
    """v1.10.5 containment hardening: operator-supplied path resolves outside repo_root."""


def _normalize_for_compare(p) -> str:
    s = str(p)
    if sys.platform == "win32" and s.startswith('\\\\?'):
        s = s[4:]
    return os.path.normcase(os.path.normpath(s))


def _normalize_relative_to_repo(path_str: str | None, repo_root: Path) -> Path | None:
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
        if sys.platform == "win32":
            ncs_resolved = _normalize_for_compare(resolved)
            ncs_root = _normalize_for_compare(repo_root_resolved)
            if ncs_resolved == ncs_root or ncs_resolved.startswith(ncs_root + os.sep):
                contained = True
    if not contained:
        raise OutsideRepoPathError(f"Path {path_str!r} is outside repo_root.")
    return resolved


def _load_goal_packet(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"goal_packet must be a mapping, got {type(data).__name__}")
    return data


def _load_template(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


_FENCE_LANG_BY_EXT = {
    ".py": "python", ".pyi": "python", ".md": "markdown", ".yaml": "yaml",
    ".yml": "yaml", ".json": "json", ".toml": "toml", ".sh": "bash",
    ".bash": "bash", ".cmd": "batch", ".js": "javascript", ".ts": "typescript",
    ".html": "html", ".css": "css", ".sql": "sql",
}


def _format_touched_files_contents(contents: dict[str, str]) -> str:
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
    auth = goal_packet.get("authorization", {}) or {}
    goal = goal_packet.get("goal", {}) or {}

    def _format_list(xs):
        return "\n".join(f"  - {x}" for x in xs) if xs else "(none)"

    def _format_gates(gates):
        if not gates: return "(none)"
        lines = []
        for g in gates:
            lines.append(f"  - {g.get('id','?')}: {g.get('description','')}\n      check: {g.get('check','')}")
        return "\n".join(lines)

    def _or_unspecified(v):
        return v if v not in (None, "") else "(not specified)"

    touched_contents: dict[str, str] = {}
    if isinstance(review_packet, dict):
        defect_target = review_packet.get("defect_target")
        if isinstance(defect_target, dict):
            tfc = defect_target.get("touched_files_contents")
            if isinstance(tfc, dict):
                touched_contents = {k: v for k, v in tfc.items() if isinstance(k, str) and isinstance(v, str)}

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


def _terminate_process_tree(proc, grace_seconds: float = 10.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError, ValueError):
        try:
            proc.terminate()
        except OSError:
            pass

    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=5, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError, ValueError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _compute_per_patch_base_sha(defect_target: dict, patch_files_touched: list, repo_root: Path | None = None) -> str | None:
    if not patch_files_touched:
        return None
    if repo_root is not None:
        for raw_path in patch_files_touched:
            if not isinstance(raw_path, str): return None
        from consensus_mcp._closure_invariant import bundle_sha as _bundle_sha
        try:
            return _bundle_sha(repo_root, list(patch_files_touched))
        except ValueError:
            return None
    contents = defect_target.get("touched_files_contents")
    if not isinstance(contents, dict):
        return None
    from consensus_mcp._closure_invariant import _normalize_path
    normalised_pairs: list[tuple[str, str]] = []
    for raw_path in patch_files_touched:
        if not isinstance(raw_path, str) or raw_path not in contents:
            return None
        body = contents[raw_path]
        if not isinstance(body, str): return None
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
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_sealed_packet(extracted, iteration_id, reviewer_id, pass_id, provenance=None, attestation_method="auto_codex_dispatch", attestation_input_sources=None) -> dict:
    if attestation_input_sources is None:
        attestation_input_sources = [
            "goal_packet (path passed via --goal-packet)",
            "prompt_template (substituted by _build_prompt)",
            "review_target (path passed via --review-target; may be unspecified)",
        ]
    packet = {
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
        "pass_id": pass_id,
        "findings": extracted.get("findings", []),
        "goal_satisfied": extracted.get("goal_satisfied", False),
        "goal_satisfied_rationale": extracted.get("goal_satisfied_rationale", ""),
        "blocking_objections": extracted.get("blocking_objections", []),
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
    return packet


def _seal_via_t6(packet: dict, iteration_dir: Path, sealed_filename: str = "codex-review.yaml") -> dict:
    from consensus_mcp.tools.review_write_and_seal import handle as t6_handle
    result = t6_handle(iteration_id=packet["iteration_id"], reviewer_id=packet["reviewer_id"], pass_id=packet["pass_id"], packet=packet)
    if "error" in result:
        raise RuntimeError(f"T6 seal failed: {result}")
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


def _validate_patch_proposal(finding_index, finding_id, pp, all_finding_ids, goal_packet=None, review_packet=None, repo_root=None, error_class=ValueError) -> None:
    if not isinstance(pp, dict):
        raise error_class(f"findings[{finding_index}].patch_proposal must be object, got {type(pp).__name__}")
    unknown = set(pp.keys()) - _PATCH_PROPOSAL_ALLOWED
    if unknown:
        raise error_class(f"findings[{finding_index}].patch_proposal has unexpected keys: {sorted(unknown)}")
    for required in _PATCH_PROPOSAL_REQUIRED:
        if required not in pp:
            raise error_class(f"findings[{finding_index}].patch_proposal missing required field: {required!r}")
    for str_field in ("patch_id", "base_sha", "unified_diff"):
        if not isinstance(pp[str_field], str):
            raise error_class(f"findings[{finding_index}].patch_proposal.{str_field} must be string, got {type(pp[str_field]).__name__}")
    for list_field in ("applies_to_findings", "files_touched"):
        if not isinstance(pp[list_field], list):
            raise error_class(f"findings[{finding_index}].patch_proposal.{list_field} must be array, got {type(pp[list_field]).__name__}")
    if "expected_tests" in pp and not isinstance(pp["expected_tests"], list):
        raise error_class(f"findings[{finding_index}].patch_proposal.expected_tests must be array, got {type(pp['expected_tests']).__name__}")
    if not pp["unified_diff"] or not pp["files_touched"]:
        raise error_class(f"findings[{finding_index}].patch_proposal.unified_diff and files_touched must be non-empty")
    diff_text = pp["unified_diff"]
    if diff_text.lstrip().startswith(_APPLY_PATCH_BEGIN_MARKER) or (_APPLY_PATCH_UPDATE_MARKER in diff_text):
        raise error_class(f"findings[{finding_index}].patch_proposal: proprietary apply_patch format not supported.")
    body_paths: set[str] = set()
    for match in _DIFF_FILE_HEADER_RE.finditer(diff_text):
        path = match.group("path").strip()
        if path and path != "dev/null": body_paths.add(path)
    declared_files = set(pp["files_touched"])
    for body_path in sorted(body_paths):
        if body_path not in declared_files:
            raise error_class(f"findings[{finding_index}].patch_proposal: diff body path {body_path!r} not in files_touched.")
    if not pp["applies_to_findings"]:
        raise error_class(f"findings[{finding_index}].patch_proposal.applies_to_findings must be non-empty")
    for item in pp["applies_to_findings"]:
        if not isinstance(item, str):
            raise error_class(f"findings[{finding_index}].patch_proposal.applies_to_findings elements must be strings.")
    for item in pp["files_touched"]:
        if not isinstance(item, str):
            raise error_class(f"findings[{finding_index}].patch_proposal.files_touched elements must be strings.")
    if not _PATCH_ID_PATTERN.match(pp["patch_id"]) or pp["patch_id"] != f"{finding_id}-patch":
        raise error_class(f"findings[{finding_index}].patch_proposal.patch_id invalid or not bound to finding.")
    for ref in pp["applies_to_findings"]:
        if ref not in all_finding_ids:
            raise error_class(f"findings[{finding_index}].patch_proposal references unknown finding id {ref!r}.")
    if goal_packet is not None:
        from consensus_mcp._self_drive import _path_in_scope
        allowed = goal_packet.get("allowed_files") or []
        forbidden = goal_packet.get("forbidden_files") or []
        for path in pp["files_touched"]:
            if not _path_in_scope(path, allowed) or (forbidden and _path_in_scope(path, forbidden)):
                raise error_class(f"findings[{finding_index}].patch_proposal path {path!r} scope violation.")
        for body_path in sorted(body_paths):
            if not _path_in_scope(body_path, allowed) or (forbidden and _path_in_scope(body_path, forbidden)):
                raise error_class(f"findings[{finding_index}].patch_proposal diff body path {body_path!r} scope violation.")
    pp["unified_diff_sha256"] = hashlib.sha256(pp["unified_diff"].encode("utf-8")).hexdigest()
    if isinstance(review_packet, dict):
        defect_target = review_packet.get("defect_target")
        if isinstance(defect_target, dict):
            per_patch_sha = _compute_per_patch_base_sha(defect_target, pp.get("files_touched") or [], repo_root=repo_root)
            if per_patch_sha is not None:
                pp["base_sha"] = per_patch_sha
            else:
                stamped = defect_target.get("base_sha")
                if isinstance(stamped, str) and stamped:
                    pp["base_sha"] = stamped


def _log_dispatch(log_path: Path, event: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    event_with_ts = {"timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event_with_ts) + "\n")

# --- JSON EXTRACTION HELPERS ---
_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n(?P<code>[\s\S]*?)\n```",
    re.MULTILINE,
)

def _extract_json_from_text(text: str) -> str:
    """Extract JSON from a potential markdown-fenced or raw string."""
    if not text:
        return ""
    match = _JSON_FENCE_RE.search(text)
    if match:
        return match.group("code").strip()
    # Fallback: search for first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text.strip()

# ADDING MISSING HELPERS
def build_failed_event(anchors, error: Exception, timeout: bool = False) -> dict:
    return {
        "event": "dispatch_failed",
        **anchors,
        "error": str(error),
        "timeout": timeout,
    }

def record_reader_error(buf, stream_name, exc):
    buf.append(f"READER_ERROR_{stream_name}: {exc}".encode("utf-8"))

def scrub_env_keys(env: dict, keys: set) -> dict:
    return {k: v for k, v in env.items() if k not in keys}

CODEX_SCRUBBED_ENV_KEYS = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"}
GEMINI_SCRUBBED_ENV_KEYS = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"}
