"""patch.stage_and_dry_run MCP tool. Phase 1 G2 partial (dry-run; not apply).

Implements the canonical-006 anti_regression_criterion.iteration_applied_changes_extension
as a technical gate. Replaces manual orchestrator staging mechanism with
mediated staging + isolated subprocess validation.

CONCURRENCY NOTE: validators write to shared consensus-state/state/validate-*-report.yaml
paths. This tool redirects all validator output to temp paths, so concurrent
calls do NOT race on the shared report files. Phase 0 is single-writer anyway.
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from consensus_mcp._paths import project_root, active_dir, spec_path

# iter-0035 (Phase B step 5 per iter-0024 plan): migrated from module-level
# REPO_ROOT / SPEC_PATH / ACTIVE_DIR captures to lazy `_paths` resolvers.


def __getattr__(name: str):
    """Lazy module-level constants (PEP 562 backward compat for callers
    that referenced REPO_ROOT / SPEC_PATH / ACTIVE_DIR as module attributes)."""
    if name == "REPO_ROOT":
        return project_root()
    if name == "SPEC_PATH":
        return spec_path()
    if name == "ACTIVE_DIR":
        return active_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

DEFAULT_VALIDATORS = [
    "validate_disposition_index",
    "validate_review",
    "validate_consensus",
    "validate_iteration",
]

def _validator_scripts() -> dict:
    """Resolve validator script paths under the current project root.

    iter-0035: lazy resolution per-call so monkeypatch.setenv() works.
    The validators dir lives under project_root (source-tree-relative),
    not state_root.
    """
    base = project_root() / "consensus_mcp" / "validators"
    return {
        "validate_disposition_index": str(base / "validate_disposition_index.py"),
        "validate_review":             str(base / "validate_review.py"),
        "validate_consensus":          str(base / "validate_consensus.py"),
        "validate_iteration":          str(base / "validate_iteration.py"),
    }

SCHEMA = {
    "name": "patch.stage_and_dry_run",
    "description": (
        "Stage proposed patches to a temp dir + run validators on hypothetical "
        "post-edit state + return findings + gate decision per canonical-006 "
        "anti-regression rule."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "iteration_id": {
                "type": ["string", "null"],
                "description": "Iteration this dry-run targets; null for spec-only patches",
            },
            "proposed_patches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["file", "old_string", "new_string"],
                },
            },
            "validators_to_run": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Validator names to run; defaults to all 4 main validators",
            },
        },
        "required": ["proposed_patches"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "staging_dir_used": {
                "type": "string",
                "description": (
                    "Temp dir used for staging (already deleted by return time). "
                    "Past tense: signals the dir was used and cleaned, not that it still exists."
                ),
            },
            "dry_run_findings": {"type": "array"},
            "gate_decision": {"type": "string", "enum": ["APPROVED", "BLOCKED"]},
            "dry_run_isolation_caveats": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Validator checks that are NOT fully isolated from the real repo during "
                    "dry-run. Callers should inspect this list before trusting dry-run results "
                    "for patches that touch archived_at entries, gitignore, or archive index."
                ),
            },
        },
        "required": ["staging_dir_used", "dry_run_findings", "gate_decision", "dry_run_isolation_caveats"],
    },
}


def _apply_patch(text: str, old_string: str, new_string: str, file_label: str) -> tuple[str, str | None]:
    """Apply one old->new substitution. Returns (new_text, error_or_None)."""
    if old_string not in text:
        return text, f"old_string not found in {file_label}"
    return text.replace(old_string, new_string, 1), None


def _read_report_findings(report_path: Path) -> list[dict]:
    """Parse a validator YAML report; return its findings list (empty on error)."""
    if not report_path.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(report_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        return []
    return [f for f in findings if isinstance(f, dict)]


def _run_validator(name: str, extra_args: list[str], out_path: Path) -> tuple[list[dict], str | None]:
    """Run one validator subprocess; return (findings, error_or_None)."""
    script = _validator_scripts().get(name)
    if script is None:
        return [], f"unknown validator: {name}"
    cmd = [sys.executable, script] + extra_args + ["--out", str(out_path)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(project_root()),
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return [], f"validator {name} timed out"
    except Exception as exc:
        return [], f"validator {name} failed: {exc}"
    if result.returncode not in (0, 1):
        stderr_snippet = result.stderr.strip()[:300]
        return [], f"validator {name} exited {result.returncode}: {stderr_snippet}"
    return _read_report_findings(out_path), None


_DRY_RUN_ISOLATION_CAVEATS: list[str] = [
    "Validator 1 (archived_at file existence): resolves paths against real REPO_ROOT, not staging dir. "
    "Patches that add/remove archived_at entries will produce inaccurate dry-run findings.",
    "Validator 5 (phase_0 deliverables gitignore status): resolves against real .gitignore. "
    "Patches modifying gitignore + script set are not fully dry-run-testable.",
    "Validator 6 (archive index pass list): cross-checks against real consensus-state/archive/review-passes/index.yaml.",
]


def handle(
    iteration_id: str | None = None,
    proposed_patches: list | None = None,
    validators_to_run: list | None = None,
) -> dict:
    """Stage patches; run validators against staged state; return findings + gate.

    Empty proposed_patches runs validators against real (un-patched) state.

    Isolation limitation: REPO_ROOT is resolved at module import time. Validators 1
    (archived_at file existence), 5 (phase_0 script gitignore), and 6 (archive index
    xref) resolve file paths against the real filesystem, not the staging dir. Dry-run
    results for patches that add/remove archived_at entries, change gitignore rules, or
    modify the archive pass index are therefore inaccurate. The output field
    dry_run_isolation_caveats documents which checks are affected; callers should inspect
    it before trusting dry-run results for those patch classes.
    """
    if proposed_patches is None:
        proposed_patches = []
    if validators_to_run is None:
        validators_to_run = list(DEFAULT_VALIDATORS)

    # Refuse empty validator list: canonical-006 requires SOME validation; an empty
    # list would silently bypass the gate (every patch returns APPROVED with no
    # findings). To run zero validators, the caller must say so explicitly via the
    # _force_zero_validators=True escape hatch reserved for diagnostics; production
    # callers must never pass that flag.
    if not validators_to_run:
        return {
            "error": (
                "validators_to_run is empty; canonical-006 requires at least one "
                "validator. Pass None to use DEFAULT_VALIDATORS, or name specific "
                "validators from _validator_scripts()."
            )
        }

    # Validate requested validator names early.
    unknown = [v for v in validators_to_run if v not in _validator_scripts()]
    if unknown:
        return {"error": f"unknown validator(s): {', '.join(unknown)}"}

    # Determine which iteration dir to stage (may be None).
    iter_dir: Path | None = None
    if iteration_id is not None:
        iter_dir = active_dir() / iteration_id
        if not iter_dir.exists():
            return {"error": f"iteration dir not found: {iter_dir}"}

    # Build a map: real_path -> patched_text for every patched file.
    patched: dict[Path, str] = {}
    for patch in proposed_patches:
        file_rel = patch.get("file", "")
        old_str = patch.get("old_string", "")
        new_str = patch.get("new_string", "")
        real_path = project_root() / file_rel
        if real_path in patched:
            current_text = patched[real_path]
        else:
            if not real_path.exists():
                if old_str == "":
                    # New-file creation: empty old_string is the "before" state.
                    current_text = ""
                else:
                    return {"error": f"file not found: {file_rel}"}
            else:
                current_text = real_path.read_text(encoding="utf-8")
        new_text, err = _apply_patch(current_text, old_str, new_str, file_rel)
        if err:
            return {"error": err}
        patched[real_path] = new_text

    # Write staged copies to a temp dir (auto-cleaned on exit via try/finally).
    staging = tempfile.mkdtemp(prefix="mcp-stage-")
    staging_path = Path(staging)

    all_findings: list[dict] = []
    staged_dir_str = staging  # reported back; cleaned before return

    try:
        # Stage patched files.
        for real_path, text in patched.items():
            # Mirror directory structure under staging_path.
            rel = real_path.relative_to(project_root())
            dest = staging_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")

        # Determine staged spec path: use staged copy if it was patched, else real.
        _spec = spec_path()
        staged_spec = staging_path / _spec.relative_to(project_root()) if _spec in patched else _spec

        # Determine staged iteration dir: copy real dir if iteration_id given, then overlay patches.
        staged_iter_dir: Path | None = None
        if iter_dir is not None:
            staged_iter_dir = staging_path / iteration_id
            shutil.copytree(str(iter_dir), str(staged_iter_dir))
            # Overlay any iteration-file patches.
            for real_path, text in patched.items():
                if real_path.is_relative_to(iter_dir):
                    rel_in_iter = real_path.relative_to(iter_dir)
                    (staged_iter_dir / rel_in_iter).write_text(text, encoding="utf-8")

        # Run each requested validator.
        for name in validators_to_run:
            out_path = staging_path / f"{name}-report.yaml"

            if name == "validate_disposition_index":
                extra = ["--spec", str(staged_spec)]
                findings, err = _run_validator(name, extra, out_path)
                if err:
                    return {"error": err}
                all_findings.extend(_tag_findings(findings, name))

            elif name == "validate_iteration":
                if staged_iter_dir is None:
                    # No iteration context; skip silently.
                    continue
                extra = ["--iteration-dir", str(staged_iter_dir)]
                findings, err = _run_validator(name, extra, out_path)
                if err:
                    return {"error": err}
                all_findings.extend(_tag_findings(findings, name))

            elif name == "validate_review":
                if staged_iter_dir is None:
                    continue
                for review_file in ("codex-review.yaml", "claude-review.yaml"):
                    review_path = staged_iter_dir / review_file
                    if not review_path.exists():
                        continue
                    rout = staging_path / f"validate_review-{review_file}.yaml"
                    extra = ["--review", str(review_path)]
                    findings, err = _run_validator(name, extra, rout)
                    if err:
                        return {"error": err}
                    all_findings.extend(_tag_findings(findings, f"{name}:{review_file}"))

            elif name == "validate_consensus":
                if staged_iter_dir is None:
                    continue
                consensus_path = staged_iter_dir / "consensus.yaml"
                if not consensus_path.exists():
                    continue
                extra = ["--consensus", str(consensus_path)]
                findings, err = _run_validator(name, extra, out_path)
                if err:
                    return {"error": err}
                all_findings.extend(_tag_findings(findings, name))

    finally:
        shutil.rmtree(staging, ignore_errors=True)

    gate = "APPROVED" if all(f.get("severity") not in ("high", "blocking") for f in all_findings) else "BLOCKED"

    return {
        "staging_dir_used": staged_dir_str,
        "dry_run_findings": all_findings,
        "gate_decision": gate,
        "dry_run_isolation_caveats": _DRY_RUN_ISOLATION_CAVEATS,
    }


def _tag_findings(findings: list[dict], source: str) -> list[dict]:
    """Add a _validator tag to each finding for traceability."""
    tagged = []
    for f in findings:
        f2 = dict(f)
        f2["_validator"] = source
        tagged.append(f2)
    return tagged


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
