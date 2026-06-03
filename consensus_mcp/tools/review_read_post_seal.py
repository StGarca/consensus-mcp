"""review.read_post_seal MCP tool. Phase 1 G1 (sealed-review provenance, read side).

The only authorized reader of a sealed review packet for downstream agents.
Verifies the packet's canonical_yaml_sha256 matches the stored packet_sha256
field (self-hash exception -- same convention as review.write_and_seal / T6).

Verification contract:
  1. Resolve the packet path (via pass_id index lookup OR direct path with safety check).
  2. Load the packet YAML.
  3. Deep-copy packet, pop packet_sha256 (self-hash exception).
  4. Re-compute canonical_yaml_sha256 of the remainder.
  5. Compare re-computed vs stored.
  6. Return success-shape with verified=True/False.

verified=False is NOT the same as error="verification_failed":
  - verified=False in success-shape: packet loaded and hashed fine, but digests differ.
    The caller decides what to do with a hash mismatch.
  - error="verification_failed": structural problem prevented canonical hashing
    (e.g., packet_sha256 field absent in a modern packet, YAML invalid).
  - The ONLY expected normal verified=False case is a legacy pre-T6 packet that
    never had a packet_sha256 field; these are flagged with legacy_unsealed=True.

Read-only tool: NO writes anywhere, NO audit event (mirrors state.read_decision_ledger).
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import yaml

from consensus_mcp._paths import project_root, archive_dir, index_path

# iter-0035 (Phase B step 8 per iter-0024 plan): migrated to lazy `_paths`
# resolvers. ARCHIVE_DIR and INDEX_PATH now resolve per-call; REPO_ROOT
# semantic is project_root.


def __getattr__(name: str):
    """PEP 562 backward compat for module-level constants."""
    if name == "REPO_ROOT":
        return project_root()
    if name == "ARCHIVE_DIR":
        return archive_dir()
    if name == "INDEX_PATH":
        return index_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# canonical_yaml_sha256 formula (see also: state_read_decision_ledger.py, review_write_and_seal.py, ...)
# Double round-trip: yaml.safe_dump -> yaml.safe_load -> yaml.safe_dump ensures
# any Python-object quirks (ordered vs unordered dicts, aliases) are normalized
# before hashing. sort_keys=True makes hash order-independent.
def _canonical_yaml_sha256(obj) -> str:
    """Canonical SHA-256 of a Python object per spec section 7."""
    canonical = yaml.safe_dump(yaml.safe_load(yaml.safe_dump(obj)), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finalize_verification(packet, packet_yaml: str, sealed_path, resolved_pass_id):
    """Apply self-hash exception + compare recorded vs computed; return success-shape dict."""
    if not isinstance(packet, dict):
        return {
            "error": "invalid_yaml",
            "detail": f"packet YAML is not a mapping (got {type(packet).__name__})",
        }
    packet_for_hash = copy.deepcopy(packet)
    recorded_sha = packet_for_hash.pop("packet_sha256", None)
    if recorded_sha is None:
        try:
            computed_sha = _canonical_yaml_sha256(packet_for_hash)
        except Exception as exc:
            return {"error": "verification_failed", "detail": f"cannot hash packet: {exc}"}
        return {
            "packet_yaml": packet_yaml,
            "packet": packet,
            "packet_sha256_recorded": None,
            "packet_sha256_computed": computed_sha,
            "verified": False,
            "sealed_path": str(sealed_path),
            "pass_id": resolved_pass_id,
            "legacy_unsealed": True,
        }
    try:
        computed_sha = _canonical_yaml_sha256(packet_for_hash)
    except Exception as exc:
        return {"error": "verification_failed", "detail": f"cannot hash packet: {exc}"}
    return {
        "packet_yaml": packet_yaml,
        "packet": packet,
        "packet_sha256_recorded": recorded_sha,
        "packet_sha256_computed": computed_sha,
        "verified": recorded_sha == computed_sha,
        "sealed_path": str(sealed_path),
        "pass_id": resolved_pass_id,
    }


SCHEMA = {
    "name": "review.read_post_seal",
    "description": (
        "Read and verify a sealed review packet. Accepts exactly one of: pass_id "
        "(resolved via index.yaml) or path (relative or absolute, must be under "
        "consensus-state/archive/review-passes/). Re-computes canonical_yaml_sha256 using "
        "the self-hash exception (hashes packet sans packet_sha256 field) and compares "
        "to the stored value. Returns packet content + verification result. "
        "Read-only -- no writes, no audit event. "
        "Legacy pre-T6 packets without packet_sha256 are flagged with legacy_unsealed=True "
        "and verified=False (expected normal; not a corruption indicator)."
    ),
    "input_schema": {
        # Provide EXACTLY ONE of pass_id | path. This is enforced in handle()
        # (error: must_provide_exactly_one_mode), NOT via a top-level oneOf - the
        # Anthropic tool input_schema rejects a top-level oneOf/anyOf/allOf, which
        # kills any subagent granted this tool on launch (v1.30.1 fix).
        "type": "object",
        "properties": {
            "pass_id": {
                "type": "string",
                "description": (
                    "Pass identifier matching an 'id' entry in index.yaml. "
                    "Provide EXACTLY ONE of pass_id or path."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Relative (from repo root) or absolute path to a sealed packet. "
                    "Must resolve to a file under consensus-state/archive/review-passes/. "
                    "Provide EXACTLY ONE of pass_id or path."
                ),
            },
        },
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "description": (
            "Success: {packet_yaml, packet, packet_sha256_recorded, packet_sha256_computed, "
            "verified, sealed_path, pass_id}. "
            "verified=False + legacy_unsealed=True: pre-T6 packet without hash field (expected normal). "
            "verified=False + no legacy_unsealed: modern packet with hash mismatch (corruption/tamper). "
            "Failure: {error, ...} where error is one of: "
            "must_provide_exactly_one_of_pass_id_or_path | pass_id_not_in_index | "
            "path_outside_archive | file_not_found | invalid_yaml | verification_failed."
        ),
        "oneOf": [
            {
                "title": "success",
                "type": "object",
                "properties": {
                    "packet_yaml": {
                        "type": "string",
                        "description": "Raw YAML text as read from disk.",
                    },
                    "packet": {
                        "type": "object",
                        "description": "Parsed packet dict.",
                    },
                    "packet_sha256_recorded": {
                        "type": ["string", "null"],
                        "description": "Value of packet_sha256 in the packet (null for legacy packets).",
                    },
                    "packet_sha256_computed": {
                        "type": "string",
                        "description": "Re-computed canonical_yaml_sha256 (self-hash exception applied).",
                    },
                    "verified": {
                        "type": "boolean",
                        "description": (
                            "True iff recorded == computed. "
                            "False for legacy packets (legacy_unsealed=True) and for tampered packets."
                        ),
                    },
                    "sealed_path": {
                        "type": "string",
                        "description": "Absolute path read from.",
                    },
                    "pass_id": {
                        "type": ["string", "null"],
                        "description": "Resolved pass_id (from index lookup or filename).",
                    },
                    "legacy_unsealed": {
                        "type": "boolean",
                        "description": (
                            "True iff packet had no packet_sha256 field (pre-T6 legacy packet). "
                            "Only present when verified=False for this reason."
                        ),
                    },
                },
                "required": [
                    "packet_yaml",
                    "packet",
                    "packet_sha256_recorded",
                    "packet_sha256_computed",
                    "verified",
                    "sealed_path",
                    "pass_id",
                ],
            },
            {
                "title": "failure",
                "type": "object",
                "properties": {
                    "error": {
                        "type": "string",
                        "enum": [
                            "must_provide_exactly_one_mode",
                            "pass_id_not_in_index",
                            "path_outside_archive",
                            "file_not_found",
                            "invalid_yaml",
                            "verification_failed",
                            "both_reviews_not_sealed",
                            "iteration_dir_not_found",
                            "unknown_reviewer",
                        ],
                    },
                    "detail": {"type": ["string", "null"]},
                },
                "required": ["error"],
            },
        ],
    },
}


def handle(
    pass_id: str | None = None,
    path: str | None = None,
    iteration_id: str | None = None,
    reviewer: str | None = None,
) -> dict:
    """Read and verify a sealed review packet.

    Exactly one of three modes:
      - pass_id  -> look up a sealed review-pass packet from archive/index.yaml
      - path     -> read a specific archive packet by path (must be under ARCHIVE_DIR)
      - iteration_id + reviewer -> G1 mode per Phase 1 design spec: serve the
        per-reviewer review.yaml from the iteration dir, BUT ONLY after verifying
        both reviewer_invoked AND review_returned_and_sealed events exist for BOTH
        codex AND claude in the iteration's independence-audit.yaml. Refuses with
        error="both_reviews_not_sealed" if either is missing. This is the design-
        intended G1 enforcement: synthesizer cannot read a sealed review until
        both reviewers have sealed.

    Returns success-shape:
        {
            "packet_yaml": str,            # raw YAML text as on disk
            "packet": dict,                # parsed dict
            "packet_sha256_recorded": str | None,  # value of field in packet (None if absent)
            "packet_sha256_computed": str, # re-computed hash (self-hash exception applied)
            "verified": bool,              # True iff recorded == computed
            "sealed_path": str,            # absolute path read from
            "pass_id": str | None,         # resolved (from index or filename)
        }
        Plus "legacy_unsealed": True when verified=False because the packet had no
        packet_sha256 field (pre-T6 legacy packet -- the ONLY expected normal verified=False).

    Returns failure-shape:
        {"error": <code>, ...}

    Failure codes:
        must_provide_exactly_one_of_pass_id_or_path: neither or both args given
        pass_id_not_in_index: pass_id not found in index.yaml
        path_outside_archive: resolved path is not under ARCHIVE_DIR
        file_not_found: resolved path does not exist on disk
        invalid_yaml: file exists but is not valid YAML or not a dict
        verification_failed: structural problem prevented canonical hashing
            (distinct from verified=False: use this only when hashing cannot complete)

    verified=False vs error="verification_failed":
        verified=False (success-shape): packet loaded and hashed successfully, but
            recorded != computed. The most common case is legacy_unsealed=True (pre-T6
            packet that never had a hash). Also signals tampered/corrupted modern packets.
        error="verification_failed" (failure-shape): packet could not be hashed at all
            due to structural problems (e.g., packet is not a dict after YAML parse).
    """
    # --- Step 1: exactly one mode ---
    has_pass_id = pass_id is not None
    has_path = path is not None
    has_iter = iteration_id is not None and reviewer is not None
    has_iter_partial = (iteration_id is not None) ^ (reviewer is not None)
    modes = sum([has_pass_id, has_path, has_iter])
    if modes != 1 or has_iter_partial:
        return {
            "error": "must_provide_exactly_one_mode",
            "detail": (
                "exactly one of: pass_id, path, or (iteration_id + reviewer); "
                "iteration_id + reviewer must both be provided together"
            ),
        }

    # --- iteration_id + reviewer mode: G1 both-sealed enforcement ---
    if has_iter:
        from consensus_mcp._paths import active_dir
        iter_dir = active_dir() / iteration_id
        if not iter_dir.is_dir():
            return {"error": "iteration_dir_not_found", "detail": str(iter_dir)}
        audit_path = iter_dir / "independence-audit.yaml"
        if not audit_path.exists():
            return {
                "error": "both_reviews_not_sealed",
                "detail": f"independence-audit.yaml missing at {audit_path}",
            }
        try:
            audit_data = yaml.safe_load(audit_path.read_bytes()) or {}
        except Exception as exc:
            return {"error": "invalid_yaml", "detail": f"independence-audit.yaml: {exc}"}
        audit_log = audit_data.get("audit_log", []) or []
        # P0.2 (generalized from a hardcoded codex+claude pair): cross-family
        # independence requires the NAMED reviewer to have sealed AND at least one
        # OTHER distinct family to also have sealed. Panel-agnostic: works for
        # grok, kimi, gemini, any future family - not just codex+claude (which
        # silently rejected every other reviewer with `unknown_reviewer`).
        sealed = {
            e.get("actor") for e in audit_log
            if e.get("event") == "review_returned_and_sealed" and e.get("actor")
        }
        if reviewer not in sealed:
            return {
                "error": "both_reviews_not_sealed",
                "detail": (
                    f"named reviewer {reviewer!r} has not sealed yet "
                    f"(sealed families: {sorted(sealed) or 'none'})"
                ),
            }
        if not (sealed - {reviewer}):
            return {
                "error": "both_reviews_not_sealed",
                "detail": (
                    f"no independent cross-reviewer has sealed besides {reviewer!r} "
                    f"(need >=2 distinct families for independence)"
                ),
            }
        # Both sealed -> serve the per-reviewer review file from the iteration dir.
        # NOTE: per-reviewer review.yaml files are NOT sealed packets (no
        # packet_sha256 field). The G1 "unsealed" check is on the AUDIT LOG (above);
        # the file content itself is just a dict. Return a mode-specific shape that
        # signals both-sealed=True + provides canonical sha for downstream
        # consumers that want to pin the served content.
        review_path = iter_dir / f"{reviewer}-review.yaml"
        if not review_path.exists():
            return {"error": "file_not_found", "detail": str(review_path)}
        try:
            review_yaml_text = review_path.read_text(encoding="utf-8")
            review_dict = yaml.safe_load(review_yaml_text)
        except Exception as exc:
            return {"error": "invalid_yaml", "detail": str(exc)}
        if not isinstance(review_dict, dict):
            return {
                "error": "invalid_yaml",
                "detail": f"review YAML is not a mapping (got {type(review_dict).__name__})",
            }
        try:
            review_sha = _canonical_yaml_sha256(review_dict)
        except Exception as exc:
            return {"error": "verification_failed", "detail": f"cannot hash review: {exc}"}
        return {
            "mode": "iteration_id_reviewer",
            "iteration_id": iteration_id,
            "reviewer": reviewer,
            "both_reviews_sealed": True,
            "review_yaml": review_yaml_text,
            "review": review_dict,
            "review_canonical_sha256": review_sha,
            "review_path": str(review_path),
        }

    # --- Step 2: resolve to an absolute sealed_path (pass_id or path mode) ---
    resolved_pass_id: str | None = None

    if has_pass_id:
        # Index lookup
        if not index_path().exists():
            return {"error": "pass_id_not_in_index", "detail": "index.yaml not found"}
        try:
            index_raw = index_path().read_bytes()
            index_data = yaml.safe_load(index_raw) or {}
        except Exception as exc:
            return {"error": "invalid_yaml", "detail": f"index.yaml: {exc}"}

        passes_list: list = index_data.get("passes", [])
        matched_entry = None
        for entry in passes_list:
            if entry.get("id") == pass_id:
                matched_entry = entry
                break

        if matched_entry is None:
            return {"error": "pass_id_not_in_index", "detail": f"pass_id={pass_id!r} not in index"}

        resolved_pass_id = pass_id

        # The index stores paths as repo-relative (forward slashes per T6).
        raw_path_str = matched_entry.get("path") or matched_entry.get("archived_at")
        if not raw_path_str:
            return {
                "error": "pass_id_not_in_index",
                "detail": f"index entry for {pass_id!r} has no 'path' or 'archived_at' field",
            }
        # Resolve relative to REPO_ROOT (T6 stores repo-relative paths).
        candidate = project_root() / raw_path_str
        sealed_path = candidate.resolve()

        # Safety: index path MUST resolve under ARCHIVE_DIR (defense against a
        # corrupted or tampered index that points elsewhere).
        try:
            sealed_path.relative_to(archive_dir().resolve())
        except ValueError:
            return {
                "error": "path_outside_archive",
                "detail": (
                    f"index entry for {pass_id!r} resolves to {sealed_path}, "
                    f"which is not under {archive_dir()}"
                ),
            }

    else:
        # Direct path -- resolve and safety-check
        raw_path = Path(path)
        if not raw_path.is_absolute():
            raw_path = project_root() / raw_path
        sealed_path = raw_path.resolve()

        # Safety: must be under ARCHIVE_DIR
        try:
            sealed_path.relative_to(archive_dir().resolve())
        except ValueError:
            return {
                "error": "path_outside_archive",
                "detail": f"resolved path {sealed_path} is not under {archive_dir()}",
            }

        # Derive pass_id from filename (best-effort; may be None for unusual names)
        # Convention: YYYY-MM-DD-<iter>-<reviewer>-pass.yaml
        stem = sealed_path.stem  # strips .yaml
        resolved_pass_id = stem if stem else None

    # --- Step 3: file existence ---
    if not sealed_path.exists():
        return {"error": "file_not_found", "detail": str(sealed_path)}

    # --- Step 4: load YAML + verify ---
    try:
        packet_yaml = sealed_path.read_text(encoding="utf-8")
        packet = yaml.safe_load(packet_yaml)
    except Exception as exc:
        return {"error": "invalid_yaml", "detail": str(exc)}

    return _finalize_verification(
        packet=packet,
        packet_yaml=packet_yaml,
        sealed_path=sealed_path,
        resolved_pass_id=resolved_pass_id,
    )


def register(registry) -> None:
    """Register this tool with the server's ToolRegistry."""
    registry.register(SCHEMA["name"], SCHEMA, handle)
