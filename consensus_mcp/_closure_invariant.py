"""Closure-cross-verification-and-freshness invariant helpers (Task #28).

Implements the supervisor-enforced invariant from codex 2026-05-10 v3+v4+v5
directive:

    closer.actor.model_family != last_mutation.actor.model_family  # PRIMARY: cross-FAMILY
    closer.review_target_hash == last_mutation.post_sha            # PRIMARY: hash binding
    closer.created_at_utc > last_mutation.timestamp                # SECONDARY: freshness

All three must pass for the closer to be permitted to record a quorum_close.

Per codex 2026-05-10 v5 Finding 1: the cross-AI check operates on
`actor.model_family`, NOT `actor.id`. Codex-A -> Codex-B with different
actor.ids is same-family-different-actor, NOT cross-AI. Real cross-AI
requires the families differ. If either model_family is missing, the check
FAILS (treat undeterminable as failed).

Hash binding is PRIMARY because clocks and generated timestamps can drift;
hashes can't. Freshness is SECONDARY defense-in-depth against re-played stale
verdicts on the right post-mutation hash.

Pure helpers — no side effects. Filesystem reads are confined to bundle_sha.
Three independent enforcement layers (T6 audit_append_event, _self_drive
stop rule, loop.run_goal transition guard) call check_closure_invariant
against the same logic; defense-in-depth requires defeating all three to bypass.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional


def _normalize_path(p: str) -> str:
    """Cross-platform forward-slash form for path-bundle hashing (iter-0024 F3-004).

    Windows callers may pass 'a\\b.py'; POSIX callers pass 'a/b.py'. Both must
    hash to the same bundle_sha when targeting the same file. PurePosixPath
    converts backslashes-as-separators only when fed through PurePath; we go
    through PurePath then re-emit as POSIX.
    """
    # Normalise separators first (handles 'a\\b.py' on POSIX too — pure string
    # replace, not path-aware, because PurePath on POSIX treats '\\' as a
    # literal character, not a separator).
    s = p.replace("\\", "/")
    # Reject embedded \0 (field separator in the canonical form) and \n
    # (inter-record separator).
    if "\0" in s or "\n" in s:
        raise ValueError(
            f"bundle_sha path contains forbidden \\0 or \\n separator chars: {p!r}"
        )
    return str(PurePosixPath(s))


def bundle_sha(repo_root: Path, files_touched: list[str]) -> str:
    """Patch target file bundle hash.

    Canonical hash for last_mutation.{base_sha, post_sha}. sha256 of sorted
    (path, sha256-of-file-content) pairs. Reproducible without commits;
    bound to exactly the touched files; doesn't drift with unrelated commits.
    Missing files contribute b"" content hash (allowed for delete patches).

    NOT git HEAD (no-auto-commit conflict) and NOT the review-packet sha
    (covers metadata not patched files).

    iter-0024 F3-004: paths are normalised to POSIX-style forward-slash form
    before hashing so a Windows caller passing 'a\\b.py' and a POSIX caller
    passing 'a/b.py' produce the same hash. Paths containing the canonical-
    form separators (\\0 or \\n) raise ValueError.

    iter-0024 F3-008: OSError on file read is silently treated as empty
    content (preserves the original semantics — missing/unreadable file ==
    empty bundle contribution). This masks permission errors and disk
    corruption; downstream drift detection still catches post_sha mismatches.
    Documented; not surfaced via side channel (out of scope for this fix).
    """
    repo_root = Path(repo_root)
    parts: list[str] = []
    # Normalise then sort so equivalent path spellings produce identical
    # canonical form.
    normalised = [_normalize_path(p) for p in files_touched]
    for p in sorted(normalised):
        full = repo_root / p
        try:
            content = full.read_bytes() if full.exists() else b""
        except OSError:
            content = b""
        content_hash = hashlib.sha256(content).hexdigest()
        parts.append(f"{p}\0{content_hash}")
    canonical = "\n".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def last_mutation_from_audit(audit_log: list[dict]) -> Optional[dict]:
    """Derive last_mutation from apply_step_landed audit events.

    Returns the MOST RECENT apply_step_landed event (event-derived from the
    audit log, not mutable state text). Cannot drift from reality.

    Returns None if no apply_step_landed events found (no mutation has
    occurred yet — close paths without mutation aren't gated by this rule).

    Backward-compat: pre-#28 audit logs may have apply_step_landed events
    without the structured actor/patch_id fields. Returns the legacy event
    as-is; the downstream invariant check fails on the missing required
    fields rather than crash.
    """
    if not audit_log:
        return None
    apply_events = [e for e in audit_log if isinstance(e, dict) and e.get("event") == "apply_step_landed"]
    if not apply_events:
        return None
    # iter-0024 F3-003: do not trust list order alone. Sort by timestamp_utc
    # when available; events without a parseable timestamp sort to "oldest"
    # so a corrupt-timestamp event cannot masquerade as most-recent. When all
    # events lack timestamps the original list order is preserved (Python
    # sort is stable).
    floor = datetime.min.replace(tzinfo=timezone.utc)
    apply_events = sorted(
        apply_events,
        key=lambda e: _parse_utc_timestamp(e.get("timestamp_utc")) or floor,
    )
    most_recent = apply_events[-1]

    # Per iter-0017 capstone finding: apply_codex_patch (Task #26) emits its
    # structured per-mutation fields nested under `last_mutation` (because T6's
    # apply_step_landed canonical event type doesn't have actor/post_sha as
    # named top-level fields — they go in extra_fields). This reader must
    # accept BOTH shapes:
    #   - top-level (legacy / unit-test shape): {actor, post_sha, ...}
    #   - nested (real apply_codex_patch shape): {extra_fields: {last_mutation: {actor, post_sha, ...}}}
    # Without this hoisting, the closure invariant would always fail
    # hash_match against a real apply_codex_patch event because the top-level
    # post_sha is absent.
    nested = most_recent.get("last_mutation")
    if not isinstance(nested, dict):
        extra_fields = most_recent.get("extra_fields")
        if isinstance(extra_fields, dict):
            nested = extra_fields.get("last_mutation")
    if isinstance(nested, dict):
        # iter-0024 F1 (claude-iter0023-001 fix): merge so the NESTED structured
        # form wins over top-level legacy fields. Rationale: audit_append_event
        # flattens extra_fields and writes top-level `actor: "<id>"` (string
        # from `actor.get("id")`) alongside the nested
        # `extra_fields.last_mutation.actor: {id, model_family, ...}` (dict).
        # The previous merge order ({**nested, **top_level}) made the legacy
        # string overwrite the structured dict; downstream cross_family then
        # saw `actor` as a string and returned None for model_family, failing
        # cross_family closed even on a perfectly valid cross-family closure.
        #
        # Inverted merge: top-level fields are seeded first, then nested
        # overlays so nested fields win on collision. Top-level-only keys (e.g.
        # event, timestamp_utc, effect, event_id) survive; nested's structured
        # actor/post_sha/etc. authoritatively override their flattened legacy
        # shadows.
        top_level = {k: v for k, v in most_recent.items() if k != "last_mutation"}
        merged = {**top_level, **nested}
        most_recent = merged

    # Surface a `timestamp` alias for downstream invariant code (which
    # expects either `timestamp` or `timestamp_utc`).
    if "timestamp" not in most_recent and "timestamp_utc" in most_recent:
        return {**most_recent, "timestamp": most_recent["timestamp_utc"]}
    return most_recent


def _actor_id(actor) -> Optional[str]:
    """Extract actor id from either the structured dict form or a legacy string."""
    if isinstance(actor, dict):
        return actor.get("id")
    if isinstance(actor, str):
        return actor
    return None


def _parse_utc_timestamp(value) -> Optional[datetime]:
    """Parse ISO 8601/RFC 3339 timestamps to aware UTC datetimes."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _actor_model_family(actor) -> Optional[str]:
    """Extract actor model_family from the structured dict form.

    Legacy string actors have no model_family — return None (which causes the
    cross-family check to FAIL as undeterminable, per v5 Finding 1).
    """
    if isinstance(actor, dict):
        return actor.get("model_family")
    return None


def check_closure_invariant(
    last_mutation: Optional[dict],
    closing_verdict: dict,
) -> dict:
    """Three-part gate: cross_family + hash_match + freshness.

    Returns:
      {
        "ok": bool,
        "checks": {
          "cross_family": bool,
          "hash_match": bool,
          "freshness": bool,
        },
        "reason": str,  # human-readable failure description; empty when ok
      }

    Hash binding PRIMARY, timestamp SECONDARY (per codex 2026-05-10 v4: clocks
    can drift, hashes can't). All three must pass for ok=True.

    Per v5 Finding 1, the family check (cross_family) replaces the old
    cross_actor check that compared only actor.id. Codex-A -> Codex-B with
    different actor.ids passes cross_actor but is NOT cross-AI; the family
    check catches that. If either side's model_family is missing, the check
    FAILS (treat undeterminable as failed) — operators MUST author actor
    objects with model_family.

    closing_verdict shape:
      {
        actor: {id, model_family, pass_id},  # or string for legacy
        review_target_hash: str,
        created_at_utc: str (ISO 8601),
        review_scope_hash: str,  # optional
      }

    If last_mutation is None (no mutation yet), invariant trivially passes.
    Close paths that don't involve mutation aren't gated by this rule.
    """
    if last_mutation is None:
        return {
            "ok": True,
            "checks": {
                "cross_family": True,
                "hash_match": True,
                "freshness": True,
            },
            "reason": "",
        }

    closer_actor_id = _actor_id(closing_verdict.get("actor"))
    lm_actor_id = _actor_id(last_mutation.get("actor"))
    closer_family = _actor_model_family(closing_verdict.get("actor"))
    lm_family = _actor_model_family(last_mutation.get("actor"))

    # v5 Finding 1: cross-family check (BOTH families must be present AND
    # different). actor.id difference is necessary (a node cannot close its
    # own work) but not sufficient — same-family-different-actor doesn't
    # buy independence.
    cross_family = (
        closer_family is not None
        and lm_family is not None
        and closer_family != lm_family
        and closer_actor_id is not None
        and lm_actor_id is not None
        and closer_actor_id != lm_actor_id
    )

    closer_target = closing_verdict.get("review_target_hash")
    lm_post = last_mutation.get("post_sha")
    hash_match = (
        closer_target is not None
        and lm_post is not None
        and closer_target == lm_post
    )

    closer_ts = closing_verdict.get("created_at_utc")
    lm_ts = last_mutation.get("timestamp") or last_mutation.get("timestamp_utc")
    closer_dt = _parse_utc_timestamp(closer_ts)
    lm_dt = _parse_utc_timestamp(lm_ts)
    freshness = (
        closer_dt is not None
        and lm_dt is not None
        and closer_dt > lm_dt
    )

    ok = cross_family and hash_match and freshness
    reasons: list[str] = []
    if not cross_family:
        reasons.append(
            f"cross_family: closer.actor.model_family={closer_family!r}/"
            f"id={closer_actor_id!r} vs last_mutation.actor.model_family={lm_family!r}/"
            f"id={lm_actor_id!r}"
        )
    if not hash_match:
        reasons.append(
            f"hash_match: closer.review_target_hash={closer_target!r} != last_mutation.post_sha={lm_post!r}"
        )
    if not freshness:
        reasons.append(
            f"freshness: closer.created_at_utc={closer_ts!r} <= last_mutation.timestamp={lm_ts!r}"
        )
    return {
        "ok": ok,
        "checks": {
            "cross_family": cross_family,
            "hash_match": hash_match,
            "freshness": freshness,
        },
        "reason": "; ".join(reasons),
    }
