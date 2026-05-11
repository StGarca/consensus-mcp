"""state.update_decision_ledger MCP tool. Phase 2 G5 (canonical ledger writer).

The only authorized writer to consensus-state/state/disposition-ledger.yaml after
Phase 2 G5 lands. Replaces direct yaml.safe_dump of the ledger file with
mediated validate-then-write + audit emission.

VALIDATE-THEN-WRITE PROTOCOL
----------------------------
Per design spec (docs/architecture/phase-1-completion.md
lines 345-361):
  1. Parse proposed_ledger_yaml (refuse if not parseable as a YAML mapping).
  2. Stage to a temp file (no real-path write yet).
  3. Run validate_disposition_index against the spec under the staged ledger
     (the validator reads the spec; the ledger contributes sha256 to provenance).
  4. If post-write findings > 0: refuse; real ledger bytes unchanged.
  5. If post-write findings == 0: atomic write (temp + os.replace) to canonical path.
  6. Emit audit event (apply_step_landed) if iteration_id supplied.

VALIDATOR COVERAGE LIMITATION (assumption surfaced 2026-05-09)
--------------------------------------------------------------
validate_disposition_index.py validates the SPEC's section 24 disposition index
against on-disk archived_at files, git tracking, archive index, and known_blockers.
It does NOT load disposition-ledger.yaml content into its validation passes; the
ledger participates only via its sha256 in the provenance block.

Implication: post-write findings count is effectively independent of ledger
content. The validate-then-write gate enforced here is a contract per the design
spec. When the validator gains ledger-content checks (planned in a future spec
revision), this tool's gate becomes meaningful without code changes here.

FAILURE MODES
-------------
  - invalid_yaml: proposed_ledger_yaml does not parse, or parses to a non-dict.
    No file written; no audit event.
  - no_consensus_sha_provided: consensus_yaml_sha256 missing or empty.
    No file written; no audit event.
  - validate_post_findings_nonzero: validator returned >0 findings against staged
    state. No file written; no audit event. Returns findings list for caller diagnosis.
  - audit_write_failed: ledger HAS been written atomically, but audit.append_event
    failed (e.g., iteration dir missing). Operator must reconcile manually; the
    ledger update is on disk and not rolled back. Includes ledger_canonical_sha256_post_write
    so the operator can record state. Mirrors T5's audit_write_failed semantics.

CONCURRENCY (v1.0)
------------------
Single-writer. Per-iteration filelock is deferred to Phase 1.x. Concurrent calls
to this tool can race on the read-validate-write window: two writes can both pass
validation against pre-write state and the second os.replace wins, silently
discarding the first. The consensus pipeline is single-writer by design (one orchestrator
at a time). Do not invoke concurrently.

MISSING-REQUIRED-FIELD CONTRACT (Round 6 F9 v1.9.2 disclosure): missing
required positional arguments raise Python TypeError, NOT a structured
{"error": "missing_*_field"} return. The output_schema enumerates structured
error codes for documented failure modes (validate_post_findings_nonzero,
invalid_yaml, audit_write_failed, etc.) but signature-level TypeError on
missing required kwargs is the documented contract for missing-input cases.
Callers that route arguments through the MCP server's handler(**arguments)
dispatch will see TypeError if the input dict omits a required field; this is
loud and easy to debug. Same contract applies to T9/T10/T11.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import yaml

from consensus_mcp.tools.audit_append_event import handle as audit_handle  # noqa: E402

def _resolve_repo_root() -> Path:
    """CONSENSUS_MCP_REPO_ROOT env-var override -> fallback to source-tree-relative discovery.

    Source-tree fallback walks 4 parents up from this module file (matches the
    consensus_mcp/tools/<name>.py layout). Env override is required when
    the package is installed via wheel into a venv where the 4-parents-up walk
    lands outside the source repo. (Round 7 follow-up; tightly-scoped fix
    authorized 2026-05-09 per operator decision after P3 T5 install-smoke surfaced
    the hidden coupling.)
    """
    import os
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent


REPO_ROOT = _resolve_repo_root()
LEDGER_PATH = REPO_ROOT / "consensus-state" / "state" / "disposition-ledger.yaml"
SPEC_PATH = (
    REPO_ROOT
    / "docs"
    / "architecture"
    / "orchestration-spec.md"
)

SCHEMA = {
    "name": "state.update_decision_ledger",
    "description": (
        "Validate-then-write the disposition ledger. Stages proposed YAML to a "
        "temp file, runs validate_disposition_index against the spec under the "
        "staged ledger, and only commits the atomic write if post-write findings "
        "are zero. Emits an apply_step_landed audit event when iteration_id is "
        "supplied. Single authorized writer for the ledger after Phase 2 G5."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "proposed_ledger_yaml": {
                "type": "string",
                "description": "Full new file content for disposition-ledger.yaml (YAML mapping).",
            },
            "consensus_yaml_sha256": {
                "type": "string",
                "description": (
                    "Canonical SHA-256 of the consensus.yaml that authorizes this "
                    "ledger update. Recorded in the audit event."
                ),
            },
            "iteration_id": {
                "type": ["string", "null"],
                "description": (
                    "Optional iteration the audit event lands under. If None, "
                    "no audit event is emitted (audit_event_id will be null)."
                ),
            },
        },
        "required": ["proposed_ledger_yaml", "consensus_yaml_sha256"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "description": (
            "Success: {written: True, validate_disposition_index_findings_pre, "
            "validate_disposition_index_findings_post: 0, ledger_path, "
            "ledger_canonical_sha256_post_write, audit_event_id}. "
            "Failure: {error, ...}."
        ),
        "oneOf": [
            {
                "title": "success",
                "type": "object",
                "properties": {
                    "written": {"type": "boolean", "enum": [True]},
                    "validate_disposition_index_findings_pre": {"type": "integer"},
                    "validate_disposition_index_findings_post": {"type": "integer", "enum": [0]},
                    "ledger_path": {"type": "string"},
                    "ledger_canonical_sha256_post_write": {"type": "string"},
                    "audit_event_id": {"type": ["string", "null"]},
                },
                "required": [
                    "written",
                    "validate_disposition_index_findings_pre",
                    "validate_disposition_index_findings_post",
                    "ledger_path",
                    "ledger_canonical_sha256_post_write",
                    "audit_event_id",
                ],
            },
            {
                "title": "failure",
                "type": "object",
                "properties": {
                    "error": {
                        "type": "string",
                        "enum": [
                            "invalid_yaml",
                            "no_consensus_sha_provided",
                            "validate_post_findings_nonzero",
                            "audit_write_failed",
                        ],
                    },
                    "detail": {"type": ["string", "null"]},
                    "validate_disposition_index_findings_post": {"type": ["integer", "null"]},
                    "findings": {"type": ["array", "null"]},
                    "ledger_canonical_sha256_post_write": {"type": ["string", "null"]},
                    "audit_error": {"type": ["string", "null"]},
                },
                "required": ["error"],
            },
        ],
    },
}


def _canonical_sha256_from_text(yaml_text: str) -> str:
    """Return canonical SHA-256 (yaml.safe_dump(safe_load(text), sort_keys=True))."""
    loaded = yaml.safe_load(yaml_text)
    return hashlib.sha256(
        yaml.safe_dump(loaded, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _build_redirected_spec(spec_text: str, staged_ledger_relpath: str) -> str:
    """Return spec_text with the frontmatter `disposition_ledger:` field repointed.

    The validator's _build_provenance hashes the file at REPO_ROOT/<disposition_ledger>.
    By rewriting that field to a repo-relative path that resolves to the staged ledger,
    the validator sees staged content without the canonical file ever changing.

    Returns spec_text unchanged if no disposition_ledger line is found.
    """
    out_lines = []
    in_frontmatter = False
    saw_close = False
    replaced = False
    for line in spec_text.splitlines(keepends=True):
        if not in_frontmatter and line.startswith("---"):
            in_frontmatter = True
            out_lines.append(line)
            continue
        if in_frontmatter and not saw_close and line.startswith("---"):
            saw_close = True
            out_lines.append(line)
            continue
        if in_frontmatter and not saw_close and not replaced:
            stripped = line.lstrip()
            if stripped.startswith("disposition_ledger:"):
                # Preserve indentation; replace value.
                indent = line[: len(line) - len(stripped)]
                out_lines.append(f'{indent}disposition_ledger: "{staged_ledger_relpath}"\n')
                replaced = True
                continue
        out_lines.append(line)
    return "".join(out_lines)


def _run_validator_with_findings(staged_ledger_path: Path) -> tuple[int, list]:
    """Validate the spec with disposition_ledger frontmatter repointed to staged_ledger_path.

    Materializes the staged ledger at a path under REPO_ROOT (so the relative-path
    resolution in the validator works) without touching the canonical
    consensus-state/state/disposition-ledger.yaml. Uses a sibling temp file in the
    same state dir and a temp spec under wiki/.

    Returns (total_findings, findings_list).
    """
    from consensus_mcp.validators.validate_disposition_index import validate_disposition_index

    # Place the redirect targets under REPO_ROOT so the validator's relative-path
    # resolution (REPO_ROOT / disposition_ledger) finds the staged file. The state
    # dir under REPO_ROOT is used as the staging sibling location so the staged
    # ledger is reachable via repo-relative path. We deliberately do NOT use
    # LEDGER_PATH.parent because LEDGER_PATH may be monkeypatched in tests to a
    # path outside REPO_ROOT.
    state_dir = REPO_ROOT / "consensus-state" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sibling_ledger = state_dir / f".staged-ledger-{os.getpid()}.yaml"
    sibling_spec = SPEC_PATH.parent / f".staged-spec-{os.getpid()}.md"

    spec_text = SPEC_PATH.read_text(encoding="utf-8")
    staged_relpath = str(sibling_ledger.relative_to(REPO_ROOT)).replace("\\", "/")
    redirected_spec = _build_redirected_spec(spec_text, staged_relpath)

    try:
        sibling_ledger.write_bytes(staged_ledger_path.read_bytes())
        sibling_spec.write_text(redirected_spec, encoding="utf-8")
        report = validate_disposition_index(sibling_spec)
        return report["stats"]["total_findings"], report.get("findings", [])
    finally:
        for p in (sibling_ledger, sibling_spec):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


def handle(
    proposed_ledger_yaml: str,
    consensus_yaml_sha256: str,
    iteration_id: str | None = None,
) -> dict:
    """Validate-then-write the disposition ledger.

    Args:
        proposed_ledger_yaml: full YAML text to write to disposition-ledger.yaml.
        consensus_yaml_sha256: canonical sha256 of the consensus authorizing this update.
        iteration_id: optional iteration whose audit log gets the apply_step_landed event.

    Returns success or failure shape per SCHEMA.output_schema oneOf.
    """
    # ---- Step 0: input validation ----
    if not consensus_yaml_sha256 or not consensus_yaml_sha256.strip():
        return {
            "error": "no_consensus_sha_provided",
            "detail": "consensus_yaml_sha256 is required and must be non-empty.",
        }

    try:
        loaded = yaml.safe_load(proposed_ledger_yaml)
    except yaml.YAMLError as exc:
        return {
            "error": "invalid_yaml",
            "detail": f"proposed_ledger_yaml failed to parse: {exc}",
        }
    if not isinstance(loaded, dict):
        return {
            "error": "invalid_yaml",
            "detail": (
                f"proposed_ledger_yaml must parse to a YAML mapping; "
                f"got {type(loaded).__name__}"
            ),
        }

    # ---- Step 1: pre-state findings (real disk) ----
    from consensus_mcp.validators.validate_disposition_index import validate_disposition_index
    pre_report = validate_disposition_index(SPEC_PATH)
    pre_findings_count = pre_report["stats"]["total_findings"]

    # ---- Step 2: stage + validate against hypothetical post-write state ----
    staging_dir = tempfile.mkdtemp(prefix="state-update-stage-")
    staged_path = Path(staging_dir) / "disposition-ledger.yaml"
    staged_path.write_text(proposed_ledger_yaml, encoding="utf-8")

    try:
        post_count, post_findings = _run_validator_with_findings(staged_path)
    finally:
        try:
            staged_path.unlink()
            Path(staging_dir).rmdir()
        except OSError:
            pass

    # ---- Step 3: refuse on non-zero post findings ----
    if post_count > 0:
        return {
            "error": "validate_post_findings_nonzero",
            "validate_disposition_index_findings_post": post_count,
            "findings": post_findings,
            "detail": (
                f"validate_disposition_index reports {post_count} finding(s) against "
                "the hypothetical post-write state; ledger was NOT written."
            ),
        }

    # ---- Step 4: atomic write to real path ----
    tmp_path = LEDGER_PATH.with_suffix(LEDGER_PATH.suffix + ".tmp")
    tmp_path.write_text(proposed_ledger_yaml, encoding="utf-8")
    os.replace(str(tmp_path), str(LEDGER_PATH))

    post_canonical_sha = _canonical_sha256_from_text(proposed_ledger_yaml)

    # ---- Step 5: optional audit event ----
    audit_event_id: str | None = None
    if iteration_id is not None:
        audit_result = audit_handle(
            iteration_id=iteration_id,
            event_type="apply_step_landed",
            actor="orchestrator",
            artifact="consensus-state/state/disposition-ledger.yaml",
            sha256=post_canonical_sha,
            effect="state.update_decision_ledger committed",
            files_modified=["consensus-state/state/disposition-ledger.yaml"],
            extra_fields={
                "consensus_yaml_sha256": consensus_yaml_sha256,
                "validate_disposition_index_findings_pre": pre_findings_count,
                "validate_disposition_index_findings_post": 0,
            },
        )
        if "error" in audit_result:
            # Ledger already written; operator must reconcile.
            return {
                "error": "audit_write_failed",
                "ledger_canonical_sha256_post_write": post_canonical_sha,
                "audit_error": audit_result["error"],
                "detail": (
                    "Ledger was atomically written but audit.append_event failed. "
                    "Operator must inspect consensus-state/state/disposition-ledger.yaml "
                    "and the iteration's independence-audit.yaml to reconcile."
                ),
            }
        audit_event_id = audit_result.get("event_id")

    try:
        ledger_rel = str(LEDGER_PATH.relative_to(REPO_ROOT))
    except ValueError:
        # LEDGER_PATH may be monkeypatched outside REPO_ROOT in tests; fall back
        # to the absolute string. Production callers always have LEDGER_PATH inside.
        ledger_rel = str(LEDGER_PATH)

    return {
        "written": True,
        "validate_disposition_index_findings_pre": pre_findings_count,
        "validate_disposition_index_findings_post": 0,
        "ledger_path": ledger_rel,
        "ledger_canonical_sha256_post_write": post_canonical_sha,
        "audit_event_id": audit_event_id,
    }


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
