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

from consensus_mcp._paths import project_root, archive_dir, index_path, active_dir

# iter-0037 (Phase B step 10 per iter-0024 plan, HIGHEST-impact seal-pipeline
# tool): migrated from cached REPO_ROOT/ARCHIVE_DIR/INDEX_PATH module-level
# constants to lazy `_paths` resolvers. Tests redirect paths via
# `monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", ...)` /
# `CONSENSUS_MCP_REPO_ROOT`, NOT `monkeypatch.setattr` on this module -
# the latter is unsafe against __getattr__-only attributes (pytest captures
# the lazy-synthesized value at setattr time and restores it into __dict__
# at teardown, permanently shadowing the resolver for subsequent tests).
# PEP 562 `__getattr__` retained for external `module.REPO_ROOT` etc.
# reads.


def __getattr__(name: str):
    """PEP 562 backward compat for external `module.REPO_ROOT` /
    `module.ARCHIVE_DIR` / `module.INDEX_PATH` reads. Internal code should
    call the `_paths` resolvers directly."""
    if name == "REPO_ROOT":
        return project_root()
    if name == "ARCHIVE_DIR":
        return archive_dir()
    if name == "INDEX_PATH":
        return index_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# canonical_yaml_sha256 formula (see also: state_read_decision_ledger.py, audit_append_event.py)
# Double round-trip: yaml.safe_dump -> yaml.safe_load -> yaml.safe_dump ensures
# any Python-object quirks (ordered vs unordered dicts, aliases) are normalized
# before hashing. sort_keys=True makes hash order-independent.
def _canonical_yaml_sha256(obj) -> str:
    """Canonical SHA-256 of a Python object per spec section 7."""
    canonical = yaml.safe_dump(yaml.safe_load(yaml.safe_dump(obj)), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# iteration-seal-archive-collision-fix (codex-sealfix-audit-4 HIGH
# finding): idempotency must NOT depend on packet_sha256, because that
# hash includes the volatile `sealed_at_utc` stamp. A re-dispatch
# builds a fresh packet with no sealed_at_utc; Step 3 then stamps a
# NEW timestamp, so the recomputed packet_sha256 differs from the
# originally-sealed one even though the substantive content is
# identical - which used to be misclassified as `index_collision`,
# defeating the whole idempotency feature for the primary
# (dispatch-retry) use case. Idempotency is therefore judged on
# CONTENT IDENTITY: the canonical hash of the packet with the volatile
# seal-provenance fields removed.
_VOLATILE_SEAL_FIELDS = ("sealed_at_utc", "packet_sha256")


def _content_identity_sha256(obj) -> str:
    """Canonical hash of a packet mapping with volatile seal-provenance
    fields (`sealed_at_utc`, `packet_sha256`) removed, so two seals of
    the same substantive content compare equal regardless of seal time
    or the timestamped self-hash. Non-mapping input returns a sentinel
    that cannot collide with any real packet's identity (used by the
    integrity guard to treat a tampered non-mapping archive as a
    mismatch rather than crashing)."""
    if not isinstance(obj, dict):
        return "non-mapping:" + hashlib.sha256(
            repr(obj).encode("utf-8", "replace")
        ).hexdigest()
    stripped = copy.deepcopy(obj)
    for f in _VOLATILE_SEAL_FIELDS:
        stripped.pop(f, None)
    return _canonical_yaml_sha256(stripped)


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


def _sanitize_for_filename(value: str) -> str:
    """iteration-seal-archive-collision-fix: make a pass_id safe to embed
    in an archive filename without changing the raw pass_id used in the
    index entry or packet body.

    Replaces every character outside [A-Za-z0-9._-] with '-' and
    collapses runs of '-'. Empty / all-hostile input falls back to
    'pass' so the filename never degenerates to just the suffix.
    pass_id values are conventionally already filesystem-safe
    (e.g. 'codex-iter0044-2-pass1'); this guard only matters for
    pathological ids containing slashes, colons, spaces, etc.
    """
    import re
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value or "")
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned or "pass"


SCHEMA = {
    "name": "review.write_and_seal",
    "description": (
        "Seal a review packet and register it in the archive index. "
        "Computes packet_sha256 (self-hash exception: hashes packet sans that field), "
        "writes atomically to "
        "consensus-state/archive/review-passes/<date>-<iter>-<reviewer>-<pass_id>-pass.yaml "
        "(iteration-seal-archive-collision-fix: pass_id added so multi-pass "
        "same-reviewer seals do not collide; pass_id sanitized for the "
        "filename, raw value preserved in index/body), "
        "updates index.yaml, and appends a review_returned_and_sealed audit event. "
        "An exact re-seal (same pass_id + same packet_sha256) is an idempotent "
        "success ({idempotent: true, index_updated: false}) with an on-disk "
        "integrity guard. Only authorized writer for review packets "
        "post-Phase-0 sealing."
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
            "Success: {sealed_path, packet_sha256, index_updated, "
            "audit_event_id, [idempotent]}. On an exact idempotent re-seal, "
            "index_updated is false, audit_event_id is "
            "'skipped_idempotent_reseal', and idempotent is true. "
            "Failure: {error, ...} where error is one of: "
            "packet_path_collision | index_collision | missing_required_field | "
            "invalid_yaml | audit_write_failed | idempotent_target_missing | "
            "idempotent_target_unreadable | idempotent_target_integrity_mismatch."
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
                    # iteration-seal-archive-collision-fix: present and true
                    # only on an exact idempotent re-seal.
                    "idempotent": {"type": "boolean"},
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
                            # iteration-seal-archive-collision-fix:
                            # idempotent-path integrity guard errors.
                            "idempotent_target_missing",
                            "idempotent_target_unreadable",
                            "idempotent_target_integrity_mismatch",
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
        plus, on an exact idempotent re-seal (same pass_id + same
        packet_sha256): index_updated=False,
        audit_event_id='skipped_idempotent_reseal', idempotent=True,
        and sealed_path is the EXISTING recorded archive path (which
        may use the pre-iteration-seal-archive-collision-fix 3-token
        scheme - the index is the resolution source of truth).
      Failure: {error: <code>, ...}

    Failure codes:
      missing_required_field: packet lacks a required pre-seal field; field: <name>
      packet_path_collision: deterministic target path already exists with
        no matching pass_id in the index (defense-in-depth backstop;
        unreachable in normal flow now that the filename carries pass_id)
      index_collision: pass_id already exists in index with a different packet_sha256
      invalid_yaml: packet is not YAML-serializable
      audit_write_failed: packet+index written but audit event failed; requires operator
      idempotent_target_missing: pass_id+sha match the index but the
        recorded archive file is gone (iteration-seal-archive-collision-fix)
      idempotent_target_unreadable: recorded archive file exists but
        cannot be parsed (iteration-seal-archive-collision-fix)
      idempotent_target_integrity_mismatch: index sha matches the new
        packet but the on-disk archive hashes differently - the sealed
        file may be tampered (iteration-seal-archive-collision-fix)
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

    # packet.pass_id (if present) must match the parameter
    # (iteration-seal-archive-collision-fix, codex-sealfix-audit-3
    # finding): symmetric with the iteration_id / reviewer_id guards
    # above. Now that pass_id determines the archive FILENAME, the
    # index `id`, AND is recorded in the sealed body, a divergence
    # between the embedded pass_id and the parameter would make the
    # sealed artifact's self-described identity disagree with its
    # filename + index entry - a provenance inconsistency. The packet
    # may legitimately carry only `pass_label` (no `pass_id`); only
    # enforce when an explicit packet.pass_id is present.
    if "pass_id" in packet and packet.get("pass_id") != pass_id:
        return {
            "error": "missing_required_field",
            "field": "pass_id",
            "detail": (
                f"packet.pass_id={packet.get('pass_id')!r} "
                f"does not match parameter pass_id={pass_id!r}"
            ),
        }

    # --- Step 3: stamp seal provenance ---
    # Note: pre_canonical_pin_marker (spec sec ~1517) is for HISTORICAL pre-v1.7.4
    # packets only -- it documents that an older hash predates the canonical-yaml
    # convention. Modern T6 packets use sealed_at_utc instead. If a caller passed
    # in a historical packet that already has pre_canonical_pin_marker, preserve it;
    # we never add it to new packets.
    packet = dict(packet)  # shallow copy
    # iteration-seal-archive-collision-fix (codex-sealfix-audit-4 medium
    # finding): a pass-label-only packet carried no pass_id in its body,
    # so the sealed artifact did not self-record the canonical pass
    # identity that now determines its filename + index entry. Stamp the
    # pass_id parameter into the body so the sealed packet always
    # self-describes its pass identity. The Step-2 guard above already
    # rejected a body pass_id that DISAGREES with the parameter, so this
    # only fills an absent value (never overwrites a conflicting one).
    packet.setdefault("pass_id", pass_id)
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
    # iteration-seal-archive-collision-fix (workflow A converged plan,
    # weighted-synthesis; codex+gemini majority on the 4-token scheme):
    # the archive filename now includes pass_id, the SAME canonical
    # uniqueness key the index keys on (Step 6 below). Before this fix
    # the filename keyed on reviewer_id only, so re-using a reviewer_id
    # across passes produced a hard packet_path_collision BEFORE the
    # pass_id-aware index logic ever ran. test_contributors.py:135's
    # docstring ("filename must contain iteration_id + reviewer_id +
    # pass_id tokens") documents this 4-token scheme as the intended
    # contract; the implementation had silently regressed to 3 tokens
    # at/before extraction. pass_id is sanitized for filesystem-hostile
    # characters in the FILENAME only; the raw pass_id is preserved
    # verbatim in the index entry and packet body.
    date_str = _now_utc_date()
    safe_pass_id = _sanitize_for_filename(pass_id)
    filename = f"{date_str}-{iteration_id}-{reviewer_id}-{safe_pass_id}-pass.yaml"
    _archive = archive_dir()
    _index = index_path()
    sealed_path = _archive / filename
    _repo = project_root()

    # --- Step 6: index lookup FIRST (reordered ahead of the path guard) ---
    # Per the converged plan: the pass_id-aware idempotency check must
    # run BEFORE the path-exists guard so an exact re-seal (same pass_id,
    # same content hash) is an idempotent SUCCESS rather than a hard
    # packet_path_collision. This is what the prior line-272 comment
    # always intended ("Same hash -- idempotent") but the old ordering
    # defeated it.
    _archive.mkdir(parents=True, exist_ok=True)
    if _index.exists():
        index_raw = _index.read_bytes()
        index_data = yaml.safe_load(index_raw) or {}
    else:
        index_data = {}

    # iteration-seal-archive-collision-fix (codex-sealfix-audit-4 HIGH
    # finding): idempotency is judged on CONTENT IDENTITY, not on the
    # timestamped packet_sha256. A re-dispatch builds a fresh packet
    # with no sealed_at_utc; Step 3 stamps a new timestamp, so its
    # packet_sha256 necessarily differs from the originally-sealed one
    # even when the substantive content is identical. Comparing
    # content-identity (volatile seal-provenance stripped) of the
    # INCOMING packet against the actual ON-DISK archived packet makes
    # an exact re-seal an idempotent success regardless of seal time,
    # and a genuine different-content reuse of the pass_id a real
    # index_collision. We read the on-disk file (not the possibly-stale
    # index sha) so the decision reflects the true archived artifact.
    incoming_identity = _content_identity_sha256(packet)
    passes_list: list = index_data.get("passes", [])
    for entry in passes_list:
        if entry.get("id") == pass_id:
            existing_rel = entry.get("path")
            existing_abs = (_repo / existing_rel) if existing_rel else None
            if existing_abs is None or not existing_abs.exists():
                return {
                    "error": "idempotent_target_missing",
                    "detail": (
                        f"pass_id={pass_id!r} is in the index but its recorded "
                        f"archive file is missing: {existing_rel!r}"
                    ),
                }
            try:
                on_disk = yaml.safe_load(existing_abs.read_text(encoding="utf-8"))
            except Exception as exc:
                return {
                    "error": "idempotent_target_unreadable",
                    "detail": f"{existing_rel!r}: {type(exc).__name__}: {exc}",
                }
            # _content_identity_sha256 handles a non-mapping on_disk
            # (tampered archive that still parses as valid YAML) via a
            # sentinel that cannot equal a real packet's identity -
            # so this neither crashes (codex-sealfix-audit-4 medium
            # finding) nor falsely reports idempotent success.
            on_disk_identity = _content_identity_sha256(on_disk)
            if on_disk_identity != incoming_identity:
                # Same pass_id, substantively different content. If the
                # on-disk archive is a non-mapping it is corrupt/tampered
                # rather than a legitimate prior pass - report that
                # distinctly so an operator can tell a re-use conflict
                # from a damaged archive.
                if not isinstance(on_disk, dict):
                    return {
                        "error": "idempotent_target_integrity_mismatch",
                        "detail": (
                            f"pass_id={pass_id!r}: the recorded archive "
                            f"{existing_rel!r} is not a YAML mapping "
                            f"({type(on_disk).__name__}) - the sealed file "
                            f"may be tampered or truncated."
                        ),
                    }
                return {
                    "error": "index_collision",
                    "detail": (
                        f"pass_id={pass_id!r} already sealed with substantively "
                        f"different content (content-identity "
                        f"{on_disk_identity[:12]}... vs incoming "
                        f"{incoming_identity[:12]}...) at {existing_rel!r}"
                    ),
                }
            # Content-identical -> idempotent re-seal. Return SUCCESS
            # describing the ACTUAL archived artifact. codex-sealfix-
            # audit-5 medium finding: do NOT trust the index entry's
            # packet_sha256 verbatim (it can be absent or stale relative
            # to the file). Derive the authoritative hash from the
            # on-disk artifact itself: prefer its own recorded
            # packet_sha256 field, else reconstruct it the same way
            # Step 4 does (canonical hash sans the self-hash field).
            # This guarantees sealed_path + packet_sha256 always
            # describe the same real bytes and packet_sha256 is never
            # empty/stale on the idempotent path.
            recorded_sha = ""
            if isinstance(on_disk, dict):
                recorded_sha = on_disk.get("packet_sha256") or ""
            if not recorded_sha:
                _od_for_hash = copy.deepcopy(on_disk)
                if isinstance(_od_for_hash, dict):
                    _od_for_hash.pop("packet_sha256", None)
                recorded_sha = _canonical_yaml_sha256(_od_for_hash)
            return {
                "sealed_path": str(existing_abs),
                "packet_sha256": recorded_sha,
                "index_updated": False,
                "audit_event_id": "skipped_idempotent_reseal",
                "idempotent": True,
            }

    # --- Step 7: path collision guard (defense-in-depth backstop) ---
    # With the pass_id now in the filename AND the index check above,
    # this should be unreachable for legitimate flows. It remains as a
    # last-resort guard against a genuine filename collision (e.g. a
    # hand-placed file or a pass_id that sanitizes to a name already
    # present without a matching index entry). Distinct detail string
    # so this case is diagnosable separately from the old behavior.
    if sealed_path.exists():
        return {
            "error": "packet_path_collision",
            "detail": (
                f"{sealed_path} exists but no matching pass_id in the index - "
                f"likely a hand-placed file or a sanitized-pass_id name clash."
            ),
        }

    # --- Step 8: atomic write of the sealed packet ---
    sealed_yaml = yaml.safe_dump(packet, sort_keys=False)
    tmp_packet = sealed_path.with_suffix(".yaml.tmp")
    tmp_packet.write_text(sealed_yaml, encoding="utf-8")
    os.replace(str(tmp_packet), str(sealed_path))

    # --- Step 9: atomic update of index.yaml ---
    sealed_at = _now_utc()
    # _repo already resolved in Step 6.
    new_entry = {
        "id": pass_id,
        "path": str(sealed_path.relative_to(_repo)).replace("\\", "/"),
        "sealed_at": sealed_at,
        "packet_sha256": packet_sha256,
        "iteration_id": iteration_id,
        "reviewer_id": reviewer_id,
    }
    passes_list.append(new_entry)
    index_data["passes"] = passes_list
    index_data["last_updated_utc"] = sealed_at

    index_yaml = yaml.safe_dump(index_data, sort_keys=False)
    tmp_index = _index.with_suffix(".yaml.tmp")
    tmp_index.write_text(index_yaml, encoding="utf-8")
    os.replace(str(tmp_index), str(_index))

    # --- Step 10: audit event ---
    # Use review_returned_and_sealed (closest semantic match in CANONICAL_EVENT_TYPES).
    # Required fields for that type: actor, artifact, sha256, independence_attestation.
    from consensus_mcp.tools.audit_append_event import handle as audit_handle

    # Only write audit event if the iteration dir exists; otherwise skip silently
    # (review packets may be sealed for iterations whose active dir was cleaned up).
    iteration_dir = active_dir() / iteration_id
    if iteration_dir.is_dir():
        audit_result = audit_handle(
            iteration_id=iteration_id,
            event_type="review_returned_and_sealed",
            actor=reviewer_id,
            artifact=str(sealed_path.relative_to(_repo)).replace("\\", "/"),
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
