"""review.write_and_seal MCP tool. Phase 1 G1 (sealed-review provenance).

The only authorized writer for review packets under
consensus-state/archive/review-passes/. After Phase 0 sealing, no review may land
in that directory except through this tool.

Sealing contract:
  1. Validate required pre-seal fields.
  2. Compute canonical_yaml_sha256 of the packet sans packet_sha256 field
     (self-hash exception -- the field cannot affect its own hash).
  3. Insert packet_sha256 into the packet dict.
  4. Compute deterministic file path from inputs + current UTC date.
  5. Refuse if path already exists (idempotent only at hash level, never
     overwrites).
  6. Atomic write (tmp + os.replace) of the sealed packet.
  7. Atomic read-modify-write of index.yaml (append new entry).
  8. Append review_returned_and_sealed audit event.

CONCURRENCY (v1.0 limitation): This tool is single-writer for both the packet
file and index.yaml. Concurrent invocations can race on index.yaml
read-modify-write -- the audit_log read-modify-write is also non-locked.
Do not invoke concurrently. Phase 1.x will add per-file filelock.
"""
from __future__ import annotations

import copy
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

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
ARCHIVE_DIR = REPO_ROOT / "consensus-state" / "archive" / "review-passes"
INDEX_PATH = ARCHIVE_DIR / "index.yaml"

# canonical_yaml_sha256 formula (see also: state_read_decision_ledger.py, audit_append_event.py)
# Double round-trip: yaml.safe_dump -> yaml.safe_load -> yaml.safe_dump ensures
# any Python-object quirks (ordered vs unordered dicts, aliases) are normalized
# before hashing. sort_keys=True makes hash order-independent.
def _canonical_yaml_sha256(obj) -> str:
    """Canonical SHA-256 of a Python object per spec section 7."""
    canonical = yaml.safe_dump(yaml.safe_load(yaml.safe_dump(obj)), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Required pre-seal fields (keys that must exist in the packet).
_REQUIRED_FIELDS = ("iteration_id", "reviewer_id", "findings")
# pass_id or pass_label -- at least one must be present.
_PASS_LABEL_FIELDS = ("pass_id", "pass_label")


def _now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _now_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


SCHEMA = {
    "name": "review.write_and_seal",
    "description": (
        "Seal a review packet and register it in the archive index. "
        "Computes packet_sha256 (self-hash exception: hashes packet sans that field), "
        "writes atomically to consensus-state/archive/review-passes/<date>-<iter>-<reviewer>-pass.yaml, "
        "updates index.yaml, and appends a review_returned_and_sealed audit event. "
        "Refuses if the deterministic path already exists (path_collision). "
        "Only authorized writer for review packets post-Phase-0 sealing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "iteration_id": {
                "type": "string",
                "description": "Iteration identifier (e.g. 'iteration-0006').",
            },
            "reviewer_id": {
                "type": "string",
                "description": "'codex' | 'claude' or other free string.",
            },
            "pass_id": {
                "type": "string",
                "description": "Unique pass identifier (e.g. 'iteration-0006-pass-a'); enforced via index.",
            },
            "packet": {
                "type": "object",
                "description": "Full review packet dict, YAML-serializable.",
            },
        },
        "required": ["iteration_id", "reviewer_id", "pass_id", "packet"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "description": (
            "Success: {sealed_path, packet_sha256, index_updated, audit_event_id}. "
            "Failure: {error, ...} where error is one of: "
            "packet_path_collision | index_collision | missing_required_field | "
            "invalid_yaml | audit_write_failed."
        ),
        "oneOf": [
            {
                "title": "success",
                "type": "object",
                "properties": {
                    "sealed_path": {"type": "string"},
                    "packet_sha256": {"type": "string"},
                    "index_updated": {"type": "boolean"},
                    "audit_event_id": {"type": "string"},
                },
                "required": ["sealed_path", "packet_sha256", "index_updated", "audit_event_id"],
            },
            {
                "title": "failure",
                "type": "object",
                "properties": {
                    "error": {
                        "type": "string",
                        "enum": [
                            "packet_path_collision",
                            "index_collision",
                            "missing_required_field",
                            "invalid_yaml",
                            "audit_write_failed",
                        ],
                    },
                    "field": {"type": ["string", "null"]},
                    "detail": {"type": ["string", "null"]},
                },
                "required": ["error"],
            },
        ],
    },
}


def handle(
    iteration_id: str,
    reviewer_id: str,
    pass_id: str,
    packet: dict,
) -> dict:
    """Seal a review packet, write it to the archive, update the index.

    Returns:
      Success: {sealed_path, packet_sha256, index_updated, audit_event_id}
      Failure: {error: <code>, ...}

    Failure codes:
      missing_required_field: packet lacks a required pre-seal field; field: <name>
      packet_path_collision: deterministic target path already exists; never overwrites
      index_collision: pass_id already exists in index with a different packet_sha256
      invalid_yaml: packet is not YAML-serializable
      audit_write_failed: packet+index written but audit event failed; requires operator
    """
    # --- Step 1: validate YAML serializability ---
    try:
        yaml.safe_dump(packet)
    except Exception as exc:
        return {"error": "invalid_yaml", "detail": str(exc)}

    # --- Step 2: validate required pre-seal fields ---
    for field in _REQUIRED_FIELDS:
        if field not in packet:
            return {"error": "missing_required_field", "field": field}

    # pass_id OR pass_label must be present
    if not any(f in packet for f in _PASS_LABEL_FIELDS):
        return {"error": "missing_required_field", "field": "pass_id or pass_label"}

    # packet.iteration_id must match the parameter
    if packet.get("iteration_id") != iteration_id:
        return {
            "error": "missing_required_field",
            "field": "iteration_id",
            "detail": (
                f"packet.iteration_id={packet.get('iteration_id')!r} "
                f"does not match parameter iteration_id={iteration_id!r}"
            ),
        }

    # packet.reviewer_id must match the parameter
    if packet.get("reviewer_id") != reviewer_id:
        return {
            "error": "missing_required_field",
            "field": "reviewer_id",
            "detail": (
                f"packet.reviewer_id={packet.get('reviewer_id')!r} "
                f"does not match parameter reviewer_id={reviewer_id!r}"
            ),
        }

    # --- Step 3: stamp seal provenance ---
    # Note: pre_canonical_pin_marker (spec sec ~1517) is for HISTORICAL pre-v1.7.4
    # packets only -- it documents that an older hash predates the canonical-yaml
    # convention. Modern T6 packets use sealed_at_utc instead. If a caller passed
    # in a historical packet that already has pre_canonical_pin_marker, preserve it;
    # we never add it to new packets.
    packet = dict(packet)  # shallow copy
    if "sealed_at_utc" not in packet:
        packet["sealed_at_utc"] = _now_utc()

    # --- Step 4: self-hash exception ---
    # Hash the packet WITHOUT the packet_sha256 field (circular dependency prevention).
    packet_for_hash = copy.deepcopy(packet)
    packet_for_hash.pop("packet_sha256", None)
    packet_sha256 = _canonical_yaml_sha256(packet_for_hash)

    # Insert computed hash into the working packet dict.
    packet = dict(packet)
    packet["packet_sha256"] = packet_sha256

    # --- Step 5: deterministic path ---
    date_str = _now_utc_date()
    filename = f"{date_str}-{iteration_id}-{reviewer_id}-pass.yaml"
    sealed_path = ARCHIVE_DIR / filename

    # --- Step 6: path collision guard ---
    if sealed_path.exists():
        return {"error": "packet_path_collision", "detail": str(sealed_path)}

    # --- Step 7: index collision check (read now; write after packet lands) ---
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    if INDEX_PATH.exists():
        index_raw = INDEX_PATH.read_bytes()
        index_data = yaml.safe_load(index_raw) or {}
    else:
        index_data = {}

    passes_list: list = index_data.get("passes", [])
    for entry in passes_list:
        if entry.get("id") == pass_id:
            if entry.get("packet_sha256") != packet_sha256:
                return {
                    "error": "index_collision",
                    "detail": (
                        f"pass_id={pass_id!r} already in index with different "
                        f"packet_sha256={entry.get('packet_sha256')!r}"
                    ),
                }
            # Same hash -- idempotent; still refuse to overwrite the packet file
            # (already caught above by path collision if file exists).

    # --- Step 8: atomic write of the sealed packet ---
    sealed_yaml = yaml.safe_dump(packet, sort_keys=False)
    tmp_packet = sealed_path.with_suffix(".yaml.tmp")
    tmp_packet.write_text(sealed_yaml, encoding="utf-8")
    os.replace(str(tmp_packet), str(sealed_path))

    # --- Step 9: atomic update of index.yaml ---
    sealed_at = _now_utc()
    new_entry = {
        "id": pass_id,
        "path": str(sealed_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "sealed_at": sealed_at,
        "packet_sha256": packet_sha256,
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
    }
    passes_list.append(new_entry)
    index_data["passes"] = passes_list
    index_data["last_updated_utc"] = sealed_at

    index_yaml = yaml.safe_dump(index_data, sort_keys=False)
    tmp_index = INDEX_PATH.with_suffix(".yaml.tmp")
    tmp_index.write_text(index_yaml, encoding="utf-8")
    os.replace(str(tmp_index), str(INDEX_PATH))

    # --- Step 10: audit event ---
    # Use review_returned_and_sealed (closest semantic match in CANONICAL_EVENT_TYPES).
    # Required fields for that type: actor, artifact, sha256, independence_attestation.
    from consensus_mcp.tools.audit_append_event import handle as audit_handle
    from consensus_mcp.tools.audit_append_event import ACTIVE_DIR

    # Only write audit event if the iteration dir exists; otherwise skip silently
    # (review packets may be sealed for iterations whose active dir was cleaned up).
    iteration_dir = ACTIVE_DIR / iteration_id
    if iteration_dir.is_dir():
        audit_result = audit_handle(
            iteration_id=iteration_id,
            event_type="review_returned_and_sealed",
            actor=reviewer_id,
            artifact=str(sealed_path.relative_to(REPO_ROOT)).replace("\\", "/"),
            sha256=packet_sha256,
            independence_attestation=packet.get("independence_attestation"),
        )
        if "error" in audit_result:
            return {
                "error": "audit_write_failed",
                "detail": audit_result["error"],
            }
        audit_event_id = audit_result["event_id"]
    else:
        # No active iteration dir -- skip audit (not an error; packet+index landed fine).
        audit_event_id = "skipped_no_iteration_dir"

    return {
        "sealed_path": str(sealed_path),
        "packet_sha256": packet_sha256,
        "index_updated": True,
        "audit_event_id": audit_event_id,
    }


def register(registry) -> None:
    """Register this tool with the server's ToolRegistry."""
    registry.register(SCHEMA["name"], SCHEMA, handle)
