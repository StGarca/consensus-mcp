"""repo.set_section MCP tool. Phase 2 G3 / claude-rev-048 / canonical-001 (T10).

Section-aware WRITE of a spec md region. Closes the honor-system gap that
scope_check.py file-path globs left open: a patch can declare it touches only
section_24 but the actual file edit can rewrite any section. This tool refuses
to land such a write.

Per design spec (docs/architecture/phase-1-completion.md
lines 304-320):

  inputs:
    file: string
    section_id: string
    new_section_text: string
    consensus_yaml_sha256: string  (binds write to specific implementation_scope)
  outputs:
    written_sha256: string
    sections_unchanged_verified: list of section IDs (technical attestation)
  permissions:
    callable_by: implementer (only via patch.apply_consensus_patch in production)
  technical_enforcement:
    - "Refuses if requested section is NOT in consensus.implementation_scope (per-section)"
    - "Reads ALL sections pre-write + post-write; refuses if any other section changed"

INTRA-FILE SCOPE ENFORCEMENT
----------------------------
  1. Refuse if consensus_yaml_path is not provided (the implementation_scope
     check is the load-bearing security gate; running blind would defeat the
     point of T10 vs. naked file edit).
  2. Load consensus.yaml; compute its canonical_yaml_sha256; refuse if mismatch.
  3. Refuse if (file, section_id) is not in consensus.implementation_scope.
     Match shape:
       - allowed_sections: list of strings like "<file_marker>/<section_id>"
         where file_marker is the file's repo-relative path (forward slashes).
       - allowed_sections: OR list of {file, section_id} dicts.
       - If allowed_sections is absent: fall back to allowed_files. The fall-
         back PERMITS the write if the file matches AND no per-section list
         is encoded for that file. Documented honestly: this fallback is the
         honor-system path and is here only because legacy consensus files
         predate the per-section field. New consensus files SHOULD encode
         allowed_sections explicitly.
  4. Build new_text via parse + replace + reconstruct.
  5. Re-parse new_text and diff against pre-parse. If any non-target section's
     section_text changed (or section_id set differs), refuse with
     "unintended_section_change" + the unintended-changes list. This catches
     the "new_section_text contains a '## N.' line that the parser interprets
     as a new heading" attack/bug.
  6. Atomic write (tempfile + os.replace).
  7. Optional audit event (apply_step_landed) when iteration_id is supplied.

FAILURE MODES
-------------
  - file_not_found
  - path_outside_repo
  - section_not_found
  - consensus_yaml_path_required        (set_section without consensus is forbidden)
  - invalid_consensus_yaml              (file does not parse as YAML mapping)
  - consensus_sha_mismatch              (consensus_yaml_sha256 doesn't match the file)
  - section_not_in_implementation_scope
  - unintended_section_change           (round-trip safety check fired)
  - audit_write_failed                  (write succeeded but audit emit failed; manual reconcile)

CONCURRENCY (v1.0)
------------------
Single-writer. Concurrent invocations on the same spec md file can race the
read-validate-write window. The consensus pipeline is single-writer by design.
**Do not invoke concurrently for the same file.** Per-iteration filelock
deferred to Phase 1.x.

MISSING-REQUIRED-FIELD CONTRACT (Round 6 F9 v1.9.2 disclosure): missing
required positional arguments raise Python TypeError, NOT a structured
{"error": "missing_*_field"} return. file="" / file=None and consensus_yaml_path
="" / consensus_yaml_path=None ARE caught upfront by _resolve_under_repo and
returned as {"error": "file_required"} (Round 6 F8 fix). A missing 'file' or
'consensus_yaml_sha256' kwarg entirely will TypeError at the function signature
level. Same contract applies to T8/T9/T11.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import yaml

from consensus_mcp._paths import project_root
from consensus_mcp.tools._md_sections import parse, reconstruct  # noqa: E402
from consensus_mcp.tools.audit_append_event import handle as audit_handle  # noqa: E402

# iter-0035 (Phase B step 3 per iter-0024 plan): migrated from module-level
# REPO_ROOT capture to lazy `_paths.project_root()` resolution.


def __getattr__(name: str):
    """PEP 562 backward compat for external callers that referenced
    `REPO_ROOT` as a module attribute (codex-rev-002 pass-1)."""
    if name == "REPO_ROOT":
        return project_root()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


SCHEMA = {
    "name": "repo.set_section",
    "description": (
        "Section-aware write of a spec md region. Refuses if the section is not "
        "in consensus.implementation_scope, or if any other section would change "
        "as a side effect of the write (round-trip safety check)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "Spec md path (absolute or repo-relative).",
            },
            "section_id": {
                "type": "string",
                "description": "Section identifier ('frontmatter' or 'section_N').",
            },
            "new_section_text": {
                "type": "string",
                "description": "Full new text for the section (replaces existing).",
            },
            "consensus_yaml_sha256": {
                "type": "string",
                "description": (
                    "Canonical SHA-256 of consensus.yaml authorizing this write. "
                    "Recorded in audit; checked against consensus_yaml_path if "
                    "provided."
                ),
            },
            "consensus_yaml_path": {
                "type": "string",
                "description": (
                    "Repo-relative or absolute path to consensus.yaml. Required "
                    "in v1.0 (no force-bypass). Used to enforce per-section scope."
                ),
            },
            "iteration_id": {
                "type": ["string", "null"],
                "description": (
                    "Optional iteration whose audit log gets the apply_step_landed "
                    "event. If None, no audit event is emitted."
                ),
            },
        },
        "required": [
            "file",
            "section_id",
            "new_section_text",
            "consensus_yaml_sha256",
            "consensus_yaml_path",
        ],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "description": (
            "Success: {written, written_sha256, sections_unchanged_verified, "
            "file, audit_event_id}. Failure: {error, ...}."
        ),
        "oneOf": [
            {
                "title": "success",
                "type": "object",
                "properties": {
                    "written": {"type": "boolean", "enum": [True]},
                    "written_sha256": {"type": "string"},
                    "sections_unchanged_verified": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "file": {"type": "string"},
                    "audit_event_id": {"type": ["string", "null"]},
                },
                "required": [
                    "written",
                    "written_sha256",
                    "sections_unchanged_verified",
                    "file",
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
                            "file_not_found",
                            "path_outside_repo",
                            "section_not_found",
                            "consensus_yaml_path_required",
                            "invalid_consensus_yaml",
                            "consensus_sha_mismatch",
                            "section_not_in_implementation_scope",
                            "unintended_section_change",
                            "audit_write_failed",
                        ],
                    },
                    "detail": {"type": ["string", "null"]},
                    "available_section_ids": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                    },
                    "unintended_changed_section_ids": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                    },
                    "consensus_yaml_sha256_actual": {"type": ["string", "null"]},
                },
                "required": ["error"],
            },
        ],
    },
}


def _resolve_under_repo(path_str: str) -> Path | dict:
    """Resolve to absolute path under project_root(). Returns Path or {error: ...}.

    Guards against path_str="" / path_str=None: refuse upfront with a structured
    file_required error rather than letting Path("").resolve() pass through to a
    read_text PermissionError on a directory. (Round 6 F8 fix; symmetric with
    repo_get_section._resolve_under_repo.)
    """
    if not path_str:
        return {"error": "file_required", "detail": "path argument is empty or None"}
    p = Path(path_str)
    if not p.is_absolute():
        p = project_root() / p
    try:
        resolved = p.resolve()
    except OSError as exc:
        return {"error": "file_not_found", "detail": str(exc)}
    try:
        resolved.relative_to(project_root().resolve())
    except ValueError:
        return {"error": "path_outside_repo", "detail": str(resolved)}
    if not resolved.exists():
        return {"error": "file_not_found", "detail": str(resolved)}
    if not resolved.is_file():
        return {"error": "file_required", "detail": f"path resolves to a directory, not a file: {resolved}"}
    return resolved


def _canonical_yaml_sha256_from_text(text: str) -> str:
    """Canonical SHA-256: hash of yaml.safe_dump(safe_load(text), sort_keys=True)."""
    loaded = yaml.safe_load(text)
    return hashlib.sha256(
        yaml.safe_dump(loaded, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _file_marker(file_path: Path) -> str:
    """Return repo-relative forward-slash marker for scope matching.

    Examples:
      .../docs/architecture/orchestration-spec.md
        -> "docs/architecture/orchestration-spec.md"

    The shorthand "spec_md" is also accepted at scope-match time as an alias
    for the canonical spec path (see _is_scope_allowed).
    """
    try:
        rel = file_path.relative_to(project_root().resolve())
    except ValueError:
        # File outside project_root() shouldn't reach here (caller refused).
        return str(file_path).replace("\\", "/")
    return str(rel).replace("\\", "/")


_SPEC_MD_REL = (
    "docs/architecture/"
    "orchestration-spec.md"
)


def _is_scope_allowed(
    consensus: dict,
    file_marker: str,
    section_id: str,
) -> tuple[bool, str]:
    """Return (allowed, reason) per implementation_scope.

    Match order:
      1. allowed_sections (per-section, preferred). Each entry is one of:
          - string "<marker>/<section_id>"  (e.g., "spec_md/section_24" or
            "wiki/.../<file>.md/section_24")
          - dict {"file": "<marker>", "section_id": "<id>"}
         "spec_md" is an alias for the canonical spec md path.
      2. If allowed_sections is absent or empty, fall back to allowed_files
         (per-file). If file is in allowed_files: allow. This is the legacy
         honor-system path; new consensus files SHOULD use allowed_sections.
      3. Otherwise refuse.
    """
    scope = consensus.get("implementation_scope") or {}
    if not isinstance(scope, dict):
        return False, "implementation_scope is not a mapping"

    allowed_sections = scope.get("allowed_sections")
    file_aliases = {file_marker, "spec_md"} if file_marker == _SPEC_MD_REL else {file_marker}

    if isinstance(allowed_sections, list) and allowed_sections:
        for entry in allowed_sections:
            if isinstance(entry, str):
                # "<marker>/<section_id>" — split on the last "/"; the section_id
                # part is the literal "frontmatter" or "section_N" trailing token.
                # We accept any of the file aliases as the marker prefix.
                for alias in file_aliases:
                    expected_prefix = alias + "/"
                    if entry.startswith(expected_prefix):
                        sec_part = entry[len(expected_prefix):]
                        if sec_part == section_id:
                            return True, "matched allowed_sections string entry"
            elif isinstance(entry, dict):
                e_file = entry.get("file", "")
                e_sec = entry.get("section_id", "")
                if e_file in file_aliases and e_sec == section_id:
                    return True, "matched allowed_sections dict entry"
        return False, (
            "allowed_sections present but did not match "
            f"{file_marker}/{section_id}"
        )

    # Legacy fallback: allowed_files.
    allowed_files = scope.get("allowed_files") or []
    if isinstance(allowed_files, list):
        for af in allowed_files:
            if isinstance(af, str) and af in file_aliases:
                return True, "matched allowed_files (legacy per-file path)"

    return False, (
        "neither allowed_sections nor allowed_files matched "
        f"{file_marker}/{section_id}"
    )


def handle(
    file: str,
    section_id: str,
    new_section_text: str,
    consensus_yaml_sha256: str,
    consensus_yaml_path: str,
    iteration_id: str | None = None,
) -> dict:
    """Write one section of a spec md file under intra-file scope enforcement.

    Returns success-shape or failure-shape per SCHEMA.output_schema oneOf.
    See module docstring for full failure mode list.
    """
    # ---- Step 1: resolve target file ----
    resolved_or_err = _resolve_under_repo(file)
    if isinstance(resolved_or_err, dict):
        return resolved_or_err
    target_path = resolved_or_err

    # ---- Step 2: pre-parse ----
    try:
        current_text = target_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return {"error": "invalid_utf8", "detail": str(exc)}
    pre_smap = parse(current_text)

    if section_id not in pre_smap.sections:
        return {
            "error": "section_not_found",
            "detail": f"section_id {section_id!r} not in file",
            "available_section_ids": pre_smap.section_ids(),
        }

    # ---- Step 3: consensus path required (no force-bypass in v1.0) ----
    if not consensus_yaml_path or not consensus_yaml_path.strip():
        return {
            "error": "consensus_yaml_path_required",
            "detail": (
                "v1.0 requires consensus_yaml_path; force-bypass deferred to "
                "Phase 1.x."
            ),
        }

    consensus_resolved_or_err = _resolve_under_repo(consensus_yaml_path)
    if isinstance(consensus_resolved_or_err, dict):
        # Re-key as invalid_consensus_yaml so the caller distinguishes "the
        # consensus file is missing/outside-repo" from "the spec md is missing".
        return {
            "error": "invalid_consensus_yaml",
            "detail": (
                f"consensus_yaml_path could not be resolved under project_root(): "
                f"{consensus_resolved_or_err}"
            ),
        }
    consensus_path = consensus_resolved_or_err

    try:
        consensus_text = consensus_path.read_text(encoding="utf-8")
        consensus = yaml.safe_load(consensus_text)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        return {"error": "invalid_consensus_yaml", "detail": str(exc)}
    if not isinstance(consensus, dict):
        return {
            "error": "invalid_consensus_yaml",
            "detail": (
                f"consensus.yaml must be a YAML mapping; got "
                f"{type(consensus).__name__}"
            ),
        }

    # ---- Step 4: consensus sha match ----
    actual_sha = _canonical_yaml_sha256_from_text(consensus_text)
    if actual_sha != consensus_yaml_sha256:
        return {
            "error": "consensus_sha_mismatch",
            "detail": (
                "consensus_yaml_sha256 does not match the canonical sha of "
                "consensus_yaml_path"
            ),
            "consensus_yaml_sha256_actual": actual_sha,
        }

    # ---- Step 5: scope check ----
    file_marker = _file_marker(target_path)
    allowed, reason = _is_scope_allowed(consensus, file_marker, section_id)
    if not allowed:
        return {
            "error": "section_not_in_implementation_scope",
            "detail": reason,
        }

    # ---- Step 6: stage new_text via reconstruct ----
    new_smap = pre_smap.replace(section_id, new_section_text)
    new_text = reconstruct(new_smap)

    # ---- Step 7: round-trip safety check ----
    post_smap = parse(new_text)

    pre_ids = set(pre_smap.sections.keys())
    post_ids = set(post_smap.sections.keys())

    unintended: list[str] = []
    # New or vanished section IDs are unintended.
    for sid in (post_ids - pre_ids) | (pre_ids - post_ids):
        unintended.append(sid)
    # Same-id sections whose text changed (other than the requested target).
    for sid in pre_ids & post_ids:
        if sid == section_id:
            # Target section MUST differ (the write); but if new text reparses
            # to a different target text, that's a parser/round-trip violation
            # too — flag it. Acceptable target text is whatever the parser
            # extracted from the staged file at this section_id.
            continue
        if pre_smap.sections[sid] != post_smap.sections[sid]:
            unintended.append(sid)

    if unintended:
        return {
            "error": "unintended_section_change",
            "detail": (
                "Reconstructed file would alter sections other than the target "
                "(round-trip safety violated; new_section_text likely contains "
                "a heading the parser interprets as a new section)."
            ),
            "unintended_changed_section_ids": sorted(set(unintended)),
        }

    # ---- Step 8: atomic write ----
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    tmp_path.write_text(new_text, encoding="utf-8")
    os.replace(str(tmp_path), str(target_path))

    written_sha = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
    sections_unchanged = sorted(
        sid for sid in pre_ids if sid != section_id
    )

    # ---- Step 9: optional audit event ----
    audit_event_id: str | None = None
    if iteration_id is not None:
        try:
            file_rel = str(target_path.relative_to(project_root().resolve())).replace("\\", "/")
        except ValueError:
            file_rel = str(target_path)
        audit_result = audit_handle(
            iteration_id=iteration_id,
            event_type="apply_step_landed",
            actor="implementer",
            artifact=file_rel,
            sha256=written_sha,
            effect=f"repo.set_section committed {file_rel}#{section_id}",
            files_modified=[file_rel],
            extra_fields={
                "consensus_yaml_sha256": consensus_yaml_sha256,
                "section_id": section_id,
                "sections_unchanged_verified_count": len(sections_unchanged),
            },
        )
        if "error" in audit_result:
            return {
                "error": "audit_write_failed",
                "detail": (
                    "spec md was atomically written but audit.append_event failed. "
                    "Operator must reconcile manually."
                ),
                "audit_error": audit_result["error"],
            }
        audit_event_id = audit_result.get("event_id")

    return {
        "written": True,
        "written_sha256": written_sha,
        "sections_unchanged_verified": sections_unchanged,
        "file": str(target_path),
        "audit_event_id": audit_event_id,
    }


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
