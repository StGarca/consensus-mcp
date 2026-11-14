"""Extract auditable ideas from sealed reviews; the host supplies dispositions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from consensus_mcp._atomic_io import atomic_write_text
from consensus_mcp._iteration_paths import all_review_files, review_family

FILENAME = "dispositions.yaml"


def dispositions_hash(iter_dir: Path) -> str:
    """Bind the exact file bytes, including formatting and comments."""
    return hashlib.sha256((iter_dir / FILENAME).read_bytes()).hexdigest()


def sealed_reviews(iter_dir: Path):
    """Read only the established sealed-review filename forms."""
    for path in all_review_files(iter_dir):
        family = review_family(path.name)
        if family:
            review = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(review, dict):
                raise ValueError(f"{path.name}: sealed review must be a mapping")
            yield path, review


def review_model(review: dict) -> str | None:
    provenance = review.get("dispatch_provenance") or {}
    model = provenance.get("actual_model") or provenance.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def sealed_families(iter_dir: Path) -> list[str]:
    """Use the recorded model, falling back to the reviewer filename."""
    return sorted({
        family for path, review in sealed_reviews(iter_dir)
        if "claude" not in (family := (
            review_model(review) or review_family(path.name)
        ).lower())
    })


def extract_rows(iter_dir: Path) -> list[dict]:
    rows = []
    for path, review in sealed_reviews(iter_dir):
        reviewer = review.get("reviewer_id") or review_family(path.name)
        model = review_model(review)

        def add(pointer, value, blocking=False):
            if value is None or value == "" or value == [] or value == {}:
                return
            summary = value if isinstance(value, str) else json.dumps(
                value, sort_keys=True, ensure_ascii=True)
            rows.append({
                "id": f"{path.name}#/{pointer}",
                "reviewer": reviewer,
                **({"model": model} if model else {}),
                "summary": summary,
                "blocking": blocking,
                "disposition": "pending",
                "evidence": "",
            })

        objections = review.get("blocking_objections") or []
        finding_ids = set()
        for i, finding in enumerate(review.get("findings") or []):
            fid = finding.get("id") if isinstance(finding, dict) else None
            if fid:
                finding_ids.add(fid)
            blocking = isinstance(finding, dict) and (
                finding.get("severity") in ("blocking", "critical")
                or finding.get("blocking") is True
                or fid in objections
            )
            add(f"findings/{i}", finding, blocking)
        for i, objection in enumerate(objections):
            # A referenced finding is one idea, not a second copy of its ID.
            if not isinstance(objection, str) or objection not in finding_ids:
                add(f"blocking_objections/{i}", objection, True)

        def proposal_ideas(pointer, value):
            if isinstance(value, list):
                for i, item in enumerate(value):
                    add(f"{pointer}/{i}", item)
            elif isinstance(value, dict):
                for key, item in sorted(value.items()):
                    proposal_ideas(f"{pointer}/{key}", item)
            else:
                add(pointer, value)

        proposal = review.get("proposal", review)
        if isinstance(proposal, dict):
            for field in ("selected_target", "rationale_vs_alternatives",
                          "deliverable_scope", "risks", "recommendations", "alternatives"):
                prefix = "proposal/" if "proposal" in review else ""
                proposal_ideas(prefix + field, proposal.get(field))
    return rows


def init_dispositions(iter_dir: Path) -> Path:
    """Create the pending table once, preserving the host's existing decisions."""
    path = iter_dir / FILENAME
    if not path.exists():
        data = {"schema_version": 1, "dispositions": extract_rows(iter_dir)}
        atomic_write_text(path, yaml.safe_dump(data, sort_keys=False))
    return path


def check_dispositions(iter_dir: Path, *, check_plan_binding: bool = True) -> list[str]:
    from consensus_mcp.validators.validate_converged_plan import validate_dispositions

    data = yaml.safe_load((iter_dir / FILENAME).read_text(encoding="utf-8"))
    errors = validate_dispositions(data, extract_rows(iter_dir))
    # The existing approval revalidator calls this function on every marker
    # check. Preserve that trust boundary for the plan's objection table too.
    # Approval itself skips the old seal while replacing it transactionally.
    outcome_path = iter_dir / "iteration-outcome.yaml"
    if check_plan_binding and outcome_path.exists():
        outcome = yaml.safe_load(outcome_path.read_text(encoding="utf-8")) or {}
        bound_sha = outcome.get("plan_objections_sha256")
        marker = {}
        for parent in iter_dir.parents:
            marker_path = parent / ".consensus" / "design-approved"
            if marker_path.is_file():
                candidate = yaml.safe_load(marker_path.read_text(encoding="utf-8")) or {}
                if candidate.get("design_consensus_ref") == iter_dir.name:
                    marker = candidate
                    break
        if bound_sha or (iter_dir / "plan-objections.yaml").exists() or "plan_objections_sha256" in marker:
            from consensus_mcp._plan_passes import objections_hash

            if not bound_sha or objections_hash(iter_dir) != bound_sha:
                errors.append("plan_objections_sha256 mismatch for plan-objections.yaml")
            if marker and marker.get("plan_objections_sha256") != bound_sha:
                errors.append("plan_objections_sha256 differs from approval marker")
    return errors
