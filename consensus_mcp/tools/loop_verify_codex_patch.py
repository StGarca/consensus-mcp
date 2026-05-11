"""loop.verify_codex_patch MCP tool — Task #25 (iter-0015).

Per codex 2026-05-10 v4 directive (memory/project_codex_fix_author_directive.md):
claude verifies codex-emitted patches per CLAUDE.md. The verifier subagent
receives reproducibility-bounded inputs (NOT codex's reasoning trail), and
emits a structured verdict {approved | corrected_resubmit}.

This MCP tool does NOT itself dispatch a subagent — that's the orchestrator's
job. The tool BUILDS the verifier-input bundle (mode=build_inputs) and
RECORDS the eventual verdict back into the iteration_dir
(mode=record_verdict).

Independence property (preserved): the build_inputs bundle includes the
codex finding TEXT (the WHAT — summary + recommendation + citation +
risk + severity) but EXCLUDES codex's `goal_satisfied_rationale` and any
prior reviewer reasoning. The verifier subagent re-derives the verdict
from the patch + touched files + CLAUDE.md, not from codex's narrative.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from consensus_mcp._self_drive import _read_yaml_or_empty


# iter-0026 F4-005 (claude-iter0025-005 fix): module-level constant replaces
# the inline `import re` + recompile inside _record_verdict_handler.
_PATCH_ID_PATTERN = re.compile(r"^codex-rev-\d+-patch$")


SCHEMA = {
    "name": "loop.verify_codex_patch",
    "description": (
        "Claude verifies a codex-emitted patch_proposal per CLAUDE.md. "
        "Subagent receives reproducibility-bounded inputs (NOT codex's reasoning); "
        "emits structured verdict: approved | corrected_resubmit. "
        "On corrected_resubmit, the verdict embeds claude's corrections; "
        "the supervisor MUST route through codex_re_reviewing_after_claude_correction "
        "before any close attempt. Two modes: mode=build_inputs (default) reads "
        "codex-review.yaml + builds the verifier-input bundle + computes "
        "review_scope_hash; mode=record_verdict validates the subagent's verdict "
        "and writes it to iteration_dir/codex-patch-verifications/<patch_id>.yaml."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "iteration_dir": {"type": "string"},
            "codex_finding_id": {"type": "string"},
            "mode": {
                "type": ["string", "null"],
                "enum": ["build_inputs", "record_verdict", None],
                "description": (
                    "build_inputs (default) returns the verifier_inputs bundle + "
                    "review_scope_hash; record_verdict writes the subagent's verdict "
                    "to a yaml file under codex-patch-verifications/."
                ),
            },
            "claude_md_path": {"type": ["string", "null"]},
            "test_runner": {"type": ["string", "null"]},
            "repo_root": {
                "type": ["string", "null"],
                "description": (
                    "Repo root for resolving touched_files paths in build_inputs. "
                    "Defaults to the supervisor's repo root resolution."
                ),
            },
            # record_verdict-mode params:
            # iter-0026 F4-006 (claude-iter0025-006 fix): tightened input enum
            # to {approved, corrected_resubmit, None}. "blocked" is an
            # internal placeholder produced by build_inputs mode (see
            # output_schema below); it is not a legal record_verdict INPUT.
            # Schema-implementation mismatch is gone: schema and handler agree.
            "verdict": {
                "type": ["string", "null"],
                "enum": ["approved", "corrected_resubmit", None],
            },
            "rationale": {"type": ["string", "null"]},
            "review_scope_hash": {"type": ["string", "null"]},
            "corrections": {"type": ["string", "null"]},
            "approved_patch_id": {"type": ["string", "null"]},
        },
        "required": ["iteration_dir", "codex_finding_id"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "verdict": {"enum": ["approved", "corrected_resubmit", "blocked"]},
            "review_scope_hash": {"type": ["string", "null"]},
            "rationale": {"type": "string"},
            "corrections": {"type": ["string", "null"]},
            "approved_patch_id": {"type": ["string", "null"]},
            "verifier_inputs": {"type": ["object", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["verdict", "rationale"],
    },
}


_VALID_VERDICTS = {"approved", "corrected_resubmit", "blocked"}


def _read_codex_review(iter_dir: Path) -> dict | None:
    """Read iteration_dir/codex-review.yaml; return dict or None if missing."""
    p = iter_dir / "codex-review.yaml"
    if not p.exists():
        return None
    return _read_yaml_or_empty(p)


def _find_finding_by_id(review: dict, finding_id: str) -> dict | None:
    findings = review.get("findings") or []
    for f in findings:
        if isinstance(f, dict) and f.get("id") == finding_id:
            return f
    return None


def _build_codex_finding_text(finding: dict) -> str:
    """Render the finding as a plain-text block — the WHAT, not the WHY.

    Excludes any reasoning fields (goal_satisfied_rationale lives at the
    review root, not on a finding, but we deliberately do not pull from
    review-level fields anyway). Includes only the structured-output
    schema-required finding fields: id, severity, summary, citation,
    risk, recommendation.
    """
    parts = [
        f"id: {finding.get('id', '')}",
        f"severity: {finding.get('severity', '')}",
        f"summary: {finding.get('summary', '')}",
        f"citation: {finding.get('citation', '')}",
        f"risk: {finding.get('risk', '')}",
        f"recommendation: {finding.get('recommendation', '')}",
    ]
    return "\n".join(parts)


def _read_touched_files(repo_root: Path, files: list[str]) -> dict[str, str]:
    """Read each file's full contents (UTF-8); missing files map to ""."""
    out: dict[str, str] = {}
    for rel in files:
        full = repo_root / rel
        try:
            out[rel] = full.read_text(encoding="utf-8") if full.exists() else ""
        except OSError:
            out[rel] = ""
    return out


def _read_claude_md(claude_md_path: Path | None) -> str:
    """Read CLAUDE.md content as a string; return "" if missing or unreadable."""
    if claude_md_path is None:
        return ""
    try:
        if claude_md_path.exists():
            return claude_md_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return ""


def _compute_review_scope_hash(bundle: dict) -> str:
    """Compute sha256 of the verifier-input bundle.

    Canonicalize via json.dumps with sort_keys=True so the hash is
    deterministic across calls with identical inputs and sensitive to any
    input change (file content, finding text, CLAUDE.md, etc.).
    """
    canonical = json.dumps(bundle, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bundle_sha_for_files(repo_root: Path, files_touched: list[str]) -> str:
    """Mirror of _closure_invariant.bundle_sha — used for post_sha_hint.

    Imported lazily to avoid a hard dep on _closure_invariant from this
    tool (consistent with the loop_run_goal lazy-import pattern).
    """
    try:
        from consensus_mcp._closure_invariant import bundle_sha
        return bundle_sha(repo_root, files_touched)
    except Exception:
        # Best-effort; the orchestrator can recompute downstream.
        parts = []
        for rel in sorted(files_touched):
            full = repo_root / rel
            content = full.read_bytes() if full.exists() else b""
            parts.append(f"{rel}\0{hashlib.sha256(content).hexdigest()}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _read_goal_packet(iter_dir: Path) -> dict:
    """Read goal_packet.yaml from the iteration dir; return {} if missing."""
    p = iter_dir / "goal_packet.yaml"
    if not p.exists():
        return {}
    return _read_yaml_or_empty(p)


def _build_inputs_handler(
    iter_dir: Path,
    codex_finding_id: str,
    claude_md_path: Path | None,
    repo_root: Path,
) -> dict:
    """mode=build_inputs: assemble the verifier-input bundle + hash.

    Independence property: the bundle EXCLUDES codex's reasoning trail
    (goal_satisfied_rationale, prior reviewer conclusions). Only the
    finding TEXT is included.
    """
    review = _read_codex_review(iter_dir)
    if review is None:
        return {
            "verdict": "blocked",
            "rationale": "codex-review.yaml not found in iteration_dir",
            "error": "codex-review.yaml not found in iteration_dir",
        }
    finding = _find_finding_by_id(review, codex_finding_id)
    if finding is None:
        return {
            "verdict": "blocked",
            "rationale": f"codex_finding_id {codex_finding_id!r} not present in codex-review.yaml",
            "error": f"codex_finding_id {codex_finding_id!r} not present in codex-review.yaml",
        }
    pp = finding.get("patch_proposal")
    if pp is None or not isinstance(pp, dict):
        return {
            "verdict": "blocked",
            "rationale": (
                f"finding {codex_finding_id!r} has no patch_proposal; verification "
                f"requires a patch_proposal block"
            ),
            "error": (
                f"finding {codex_finding_id!r} has no patch_proposal; verification "
                f"requires a patch_proposal block"
            ),
        }

    goal_packet = _read_goal_packet(iter_dir)
    acceptance_gates = goal_packet.get("acceptance_gates", []) or []
    files_touched = pp.get("files_touched", []) or []
    touched_files = _read_touched_files(repo_root, list(files_touched))
    claude_md_excerpt = _read_claude_md(claude_md_path)
    finding_text = _build_codex_finding_text(finding)
    base_sha = pp.get("base_sha", "")
    post_sha_hint = _bundle_sha_for_files(repo_root, list(files_touched))

    bundle = {
        "goal_packet": goal_packet,
        "acceptance_gates": acceptance_gates,
        "patch_proposal": pp,
        "touched_files": touched_files,
        "claude_md_excerpt": claude_md_excerpt,
        "codex_finding_text": finding_text,
        "base_sha": base_sha,
        "post_sha_hint": post_sha_hint,
    }

    review_scope_hash = _compute_review_scope_hash(bundle)

    return {
        "verdict": "blocked",
        "rationale": "inputs built; awaiting subagent verdict",
        "review_scope_hash": review_scope_hash,
        "verifier_inputs": bundle,
    }


def _record_verdict_handler(
    iter_dir: Path,
    codex_finding_id: str,
    verdict: str | None,
    rationale: str | None,
    review_scope_hash: str | None,
    corrections: str | None,
    approved_patch_id: str | None,
) -> dict:
    """mode=record_verdict: validate + persist the subagent's verdict."""
    if verdict not in {"approved", "corrected_resubmit"}:
        return {
            "verdict": "blocked",
            "rationale": "invalid verdict",
            "error": (
                f"verdict must be one of {{approved, corrected_resubmit}}; got {verdict!r}"
            ),
        }
    if not approved_patch_id:
        return {
            "verdict": "blocked",
            "rationale": "missing approved_patch_id",
            "error": "approved_patch_id is required to author the verification yaml",
        }
    # Defense-in-depth: validate patch_id format BEFORE constructing out_path.
    # Per iter-0020 ergonomics fix: codex-producible form (finding-id-derived).
    # Codex's output schema enforces ^codex-rev-\d+-patch$ at dispatch time,
    # but record_verdict accepts arbitrary string. Re-validate to prevent
    # path-traversal if a malformed value slips past the orchestrator.
    # iter-0026 F4-005: pattern is module-level (_PATCH_ID_PATTERN); no
    # function-local re-compile.
    if not _PATCH_ID_PATTERN.match(approved_patch_id):
        return {
            "verdict": "blocked",
            "rationale": "approved_patch_id format invalid",
            "error": (
                f"approved_patch_id must match ^codex-rev-\\d+-patch$; "
                f"got {approved_patch_id!r}"
            ),
        }
    expected_patch_id = f"{codex_finding_id}-patch"
    if approved_patch_id != expected_patch_id:
        return {
            "verdict": "blocked",
            "rationale": "approved_patch_id does not match codex_finding_id",
            "error": f"approved_patch_id must equal {expected_patch_id!r}; got {approved_patch_id!r}",
        }
    if verdict == "corrected_resubmit" and not corrections:
        return {
            "verdict": "blocked",
            "rationale": "corrected_resubmit requires corrections",
            "error": "verdict=corrected_resubmit requires a non-empty corrections diff",
        }

    record: dict[str, Any] = {
        "schema_version": 1,
        "verifier": "claude",
        "verdict": verdict,
        "review_scope_hash": review_scope_hash or "",
        "rationale": rationale or "",
        "approved_patch_id": approved_patch_id,
        "codex_finding_id": codex_finding_id,
    }
    if corrections is not None:
        record["corrections"] = corrections

    out_dir = iter_dir / "codex-patch-verifications"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{approved_patch_id}.yaml"
    out_path.write_text(yaml.safe_dump(record, sort_keys=True), encoding="utf-8")

    response: dict[str, Any] = {
        "verdict": verdict,
        "rationale": rationale or "",
        "approved_patch_id": approved_patch_id,
        "review_scope_hash": review_scope_hash,
    }
    if corrections is not None:
        response["corrections"] = corrections
    return response


def handle(
    iteration_dir: str,
    codex_finding_id: str,
    mode: str | None = None,
    claude_md_path: str | None = None,
    test_runner: str | None = None,
    repo_root: str | None = None,
    verdict: str | None = None,
    rationale: str | None = None,
    review_scope_hash: str | None = None,
    corrections: str | None = None,
    approved_patch_id: str | None = None,
) -> dict:
    """Tool entry point. mode default = build_inputs."""
    iter_dir = Path(iteration_dir)
    effective_mode = mode or "build_inputs"

    if effective_mode == "build_inputs":
        # Resolve repo_root: explicit param > iter_dir's parent (typical
        # iteration layout iter_dir == <repo>/consensus-state/active/iteration-NNNN/).
        if repo_root is not None:
            repo_root_path = Path(repo_root)
        else:
            # Fall back to iter_dir.parent.parent.parent (the conventional
            # repo layout). Best-effort; tests pass repo_root explicitly.
            try:
                repo_root_path = iter_dir.resolve().parent.parent.parent
            except Exception:
                repo_root_path = iter_dir
        claude_md_path_obj = Path(claude_md_path) if claude_md_path else (repo_root_path / "CLAUDE.md")
        return _build_inputs_handler(
            iter_dir=iter_dir,
            codex_finding_id=codex_finding_id,
            claude_md_path=claude_md_path_obj,
            repo_root=repo_root_path,
        )
    if effective_mode == "record_verdict":
        return _record_verdict_handler(
            iter_dir=iter_dir,
            codex_finding_id=codex_finding_id,
            verdict=verdict,
            rationale=rationale,
            review_scope_hash=review_scope_hash,
            corrections=corrections,
            approved_patch_id=approved_patch_id,
        )
    return {
        "verdict": "blocked",
        "rationale": f"unknown mode {effective_mode!r}",
        "error": f"mode must be 'build_inputs' or 'record_verdict'; got {effective_mode!r}",
    }


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
