"""repo.get_section MCP tool. Phase 2 G3 / claude-rev-048 / canonical-001 (T9).

Section-aware READ of a spec md region. Returns ONLY the requested section
(zero leakage of other sections). Pairs with repo.set_section (T10) which
enforces intra-file scope on writes.

Per design spec (docs/architecture/phase-1-completion.md
lines 287-302):

  inputs:
    file: string (spec md path; must resolve under REPO_ROOT)
    section_id: string (e.g., 'frontmatter', 'section_0' .. 'section_24')
  outputs:
    section_text: string
    section_sha256: string  (plain SHA-256 of utf-8 bytes; sections are markdown,
                             not YAML, so canonical-yaml-sha256 does not apply)
  permissions:
    callable_by: orchestrator | implementer

FAILURE MODES
-------------
  - file_not_found:        file path does not exist on disk.
  - path_outside_repo:     resolved path is not under REPO_ROOT (path-traversal guard).
  - invalid_utf8:          file is not valid utf-8.
  - section_not_found:     section_id does not appear in parsed file. Returns
                           available_section_ids list to aid caller diagnosis.

PARSER
------
Section parser is shared with repo.set_section in tools/_md_sections.py. See
that module's docstring for section_id namespace + round-trip guarantees.

MISSING-REQUIRED-FIELD CONTRACT (Round 6 F9 v1.9.2 disclosure): missing
required positional arguments raise Python TypeError, NOT a structured
{"error": "missing_*_field"} return. file="" / file=None ARE caught upfront
by _resolve_under_repo and returned as {"error": "file_required"} (Round 6 F8
fix), but a missing 'file' kwarg entirely will TypeError at the function
signature level. Same contract applies to T8/T10/T11.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from consensus_mcp.tools._md_sections import parse  # noqa: E402

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


SCHEMA = {
    "name": "repo.get_section",
    "description": (
        "Section-aware read of a spec md region. Returns ONLY the requested "
        "section (frontmatter or section_N). Refuses paths outside REPO_ROOT."
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
                "description": (
                    "Section identifier: 'frontmatter' or 'section_N' where N "
                    "matches a '## N. ...' heading in the file."
                ),
            },
        },
        "required": ["file", "section_id"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "description": (
            "Success: {section_text, section_sha256, file}. "
            "Failure: {error, ...} where error is one of: "
            "file_not_found | path_outside_repo | invalid_utf8 | section_not_found."
        ),
        "oneOf": [
            {
                "title": "success",
                "type": "object",
                "properties": {
                    "section_text": {"type": "string"},
                    "section_sha256": {"type": "string"},
                    "file": {"type": "string"},
                },
                "required": ["section_text", "section_sha256", "file"],
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
                            "invalid_utf8",
                            "section_not_found",
                        ],
                    },
                    "detail": {"type": ["string", "null"]},
                    "available_section_ids": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                    },
                },
                "required": ["error"],
            },
        ],
    },
}


def _resolve_under_repo(file: str) -> Path | dict:
    """Resolve file to absolute path under REPO_ROOT. Returns Path or {error: ...}.

    Guards against file="" / file=None: Path("").resolve() returns the cwd
    (REPO_ROOT under normal invocation), which would pass the existence check
    but fail downstream on read_text with PermissionError on Windows. Refuse
    upfront with a structured `file_required` error instead. (Round 6 F8 fix.)
    """
    if not file:
        return {"error": "file_required", "detail": "file argument is empty or None"}
    p = Path(file)
    if not p.is_absolute():
        p = REPO_ROOT / p
    try:
        resolved = p.resolve()
    except OSError as exc:
        return {"error": "file_not_found", "detail": str(exc)}
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return {"error": "path_outside_repo", "detail": str(resolved)}
    if not resolved.exists():
        return {"error": "file_not_found", "detail": str(resolved)}
    if not resolved.is_file():
        return {"error": "file_required", "detail": f"path resolves to a directory, not a file: {resolved}"}
    return resolved


def handle(file: str, section_id: str) -> dict:
    """Read a single section from a spec md file.

    Args:
        file: spec md path (absolute or repo-relative).
        section_id: 'frontmatter' or 'section_N'.

    Returns success-shape or failure-shape per SCHEMA.output_schema oneOf.
    """
    resolved_or_err = _resolve_under_repo(file)
    if isinstance(resolved_or_err, dict):
        return resolved_or_err
    resolved = resolved_or_err

    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return {"error": "invalid_utf8", "detail": str(exc)}

    smap = parse(text)
    if section_id not in smap.sections:
        return {
            "error": "section_not_found",
            "detail": f"section_id {section_id!r} not in file",
            "available_section_ids": smap.section_ids(),
        }

    section_text = smap.sections[section_id]
    section_sha = hashlib.sha256(section_text.encode("utf-8")).hexdigest()
    return {
        "section_text": section_text,
        "section_sha256": section_sha,
        "file": str(resolved),
    }


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
