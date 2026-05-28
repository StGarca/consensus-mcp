"""Delivery-readiness gate — fail-closed enforcement that an operator-facing
artifact has been CONSENSUS-VETTED, not self-judged.

Origin: internal anti-stall consensus (iteration-antistall-impl-2026-05-22, 4/4).
Problem fixed: an agent kept FAILING TO INVOKE/UTILIZE consensus-mcp — building
before vetting, self-judging soundness ("I tested it myself, it's sound"), and
delivering before it was checked. Memories did not bind the agent; only a
mechanism does. This module is that mechanism.

Core invariant (unforgeable by the agent): you cannot mint a delivery token
unless `design_consensus_ref` resolves to a CLOSED/SEALED consensus iteration
(closing_state in the same accepted set used by
`_release_gate_check.gate_real_iter`). An agent that self-judged has no sealed
iteration to point at, so `mint_delivery_token` refuses and
`verify_delivery_token` fails closed. Soundness therefore *must* route through
consensus.

Enforcement surfaces (portable-first):
  - MCP tool `consensus.request_delivery` / `delivery_gate_check` (server.py) —
    works across any harness (Kimi CLI, Cursor, Claude Code).
  - CLI: `python -m consensus_mcp._delivery_readiness {mint,verify} ...`.
  - Optional PreToolUse hook template (contrib/) for Claude Code only.

Reuses (verified present in consensus_mcp 1.15.10):
  _self_drive._canonical_sha256_of_yaml_file, _self_drive._resolve_repo_root,
  and the closing_state accepted-set semantics of
  _release_gate_check.gate_real_iter.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import yaml

from consensus_mcp._self_drive import (
    _canonical_sha256_of_yaml_file,
    _resolve_repo_root,
)

# Mirrors _release_gate_check.gate_real_iter's accepted_states (a module-local
# there, so mirrored — not imported — with this citation). Keep in sync.
SEALED_CLOSING_STATES = frozenset(
    {"quorum_close_passed", "implementation_ready_apply_landed"}
)
TOKEN_SCHEMA_VERSION = 1
ISSUER = "consensus_mcp._delivery_readiness"
HASH_ALGORITHM = "sha256"


class DeliveryReadinessError(RuntimeError):
    """Raised when minting is refused (no sealed design ref, etc.)."""


@dataclasses.dataclass
class ConsensusRefStatus:
    ref: str
    sealed: bool
    closing_state: str | None
    detail: str


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_artifact_hash(path: Path) -> str:
    """sha256 of an artifact. YAML uses the canonical-YAML hash (drift-stable,
    same algorithm consensus-mcp uses for review-packet drift); everything else
    uses raw bytes."""
    path = Path(path)
    if path.suffix.lower() in (".yaml", ".yml"):
        return _canonical_sha256_of_yaml_file(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_consensus_ref(ref: str, repo_root: Path) -> ConsensusRefStatus:
    """A design_consensus_ref is VALID only if it names a real iteration dir
    whose iteration-outcome.yaml carries a sealed closing_state. Fail-closed:
    missing / unsealed / malformed all return sealed=False."""
    if not ref or "/" in ref or "\\" in ref or ".." in ref:
        return ConsensusRefStatus(ref, False, None, "missing or unsafe ref")
    iter_dir = repo_root / "consensus-state" / "active" / ref
    outcome = iter_dir / "iteration-outcome.yaml"
    if not outcome.exists():
        return ConsensusRefStatus(ref, False, None, f"no iteration-outcome.yaml at {ref}")
    try:
        data = yaml.safe_load(outcome.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return ConsensusRefStatus(ref, False, None, f"unparseable outcome: {exc}")
    raw = data.get("closing_state")
    state = ""
    if isinstance(raw, str):
        for ln in raw.splitlines():
            if ln.strip():
                state = ln.strip()
                break
    if state in SEALED_CLOSING_STATES:
        return ConsensusRefStatus(ref, True, state, "sealed")
    return ConsensusRefStatus(ref, False, state or None,
                              f"closing_state {state!r} not in {sorted(SEALED_CLOSING_STATES)}")


def _canonical_artifact_key(artifact_path: Path, repo_root: Path) -> str:
    """Stable repo-relative, forward-slashed key for an artifact, identical
    whether the caller passes a repo-relative path (as mint/verify get from the
    gate) or an absolute path (as `repo_root.rglob('*')` yields at close).

    Before this normalization the token filename was `sha256(str(path))`, so the
    same file addressed two ways hashed to two different token files -> false
    `missing_delivery_tokens` at close even with a valid token on disk
    (field bug, 2026-05-28). Canonicalizing at this one root primitive fixes the
    hash key for mint, verify, AND close together.

    An out-of-repo absolute target (or any resolution error) falls back to the
    resolved/forward-slashed absolute form — still stable across path forms."""
    p = Path(artifact_path)
    try:
        if p.is_absolute():
            rel = p.resolve().relative_to(Path(repo_root).resolve())
        else:
            # Treated as already repo-relative (the gate's contract); normalize
            # lexical noise like "./" / ".." segments.
            rel = Path(os.path.normpath(str(p)))
        return str(rel).replace("\\", "/")
    except ValueError:
        # Absolute target outside repo_root — no repo-relative form exists.
        return str(p.resolve()).replace("\\", "/")
    except Exception:
        return str(p).replace("\\", "/")


def _token_path(artifact_path: Path, repo_root: Path) -> Path:
    key = _canonical_artifact_key(artifact_path, repo_root)
    safe = hashlib.sha256(key.encode()).hexdigest()[:16]
    d = repo_root / ".delivery-readiness"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe}.token.json"


def mint_delivery_token(artifact_path: Path, *, design_consensus_ref: str,
                        vetted_by: list[str], known_flaws: list | None = None,
                        operator_ack: bool = False, action_classes: list | None = None,
                        followup_ledger_key: str | None = None,
                        repo_root: Path | None = None) -> dict:
    """Mint a token — REFUSES unless the design ref is sealed and >=2 non-claude
    reviewers vetted it. This is the anti-self-judge gate: an agent cannot mint
    readiness from its own assertion.

    1.16.1: also REFUSES if the declared `action_classes` carry required
    follow-ups (required_followups.yaml) that are neither resolved nor
    deferred-with-reason in the follow-up ledger — the mechanical binding of the
    EXISTING complete-fulfillment rule (prevents 'merge a version bump but skip
    the release')."""
    repo_root = repo_root or _resolve_repo_root()
    artifact_path = Path(artifact_path)
    if not artifact_path.exists():
        raise DeliveryReadinessError(f"artifact does not exist: {artifact_path}")
    status = resolve_consensus_ref(design_consensus_ref, repo_root)
    if not status.sealed:
        raise DeliveryReadinessError(
            f"design_consensus_ref not sealed: {status.detail}. "
            f"Soundness must be consensus-vetted — self-judging is not permitted."
        )
    non_claude = [r for r in (vetted_by or []) if r and "claude" not in r.lower()]
    if len(non_claude) < 2:
        raise DeliveryReadinessError(
            f"vetted_by needs >=2 non-claude reviewers from {design_consensus_ref}; got {non_claude}"
        )
    known_flaws = list(known_flaws or [])
    if known_flaws and not operator_ack:
        raise DeliveryReadinessError(
            f"known_flaws non-empty {known_flaws} without operator_ack=True — no caveat-and-ship"
        )
    # 1.16.1 follow-up completeness: declared action_classes acquire required
    # follow-ups; refuse to mint while any is unresolved/undeferred.
    action_classes = list(action_classes or [])
    open_fu: list[str] = []
    if action_classes:
        from consensus_mcp import _followup_completeness as _fc
        ledger = _fc.read_ledger(followup_ledger_key or str(artifact_path), repo_root)
        complete, open_fu = _fc.followups_complete(action_classes, ledger)
        if not complete:
            raise DeliveryReadinessError(
                f"action_classes {action_classes} have unresolved required follow-ups "
                f"{open_fu} — resolve them or defer-with-reason before delivery "
                f"(complete-fulfillment rule, mechanically enforced)"
            )
    token = {
        "schema_version": TOKEN_SCHEMA_VERSION,
        "token_id": uuid.uuid4().hex,
        "created_at_utc": _utcnow(),
        "issuer": ISSUER,
        "artifact_path": _canonical_artifact_key(artifact_path, repo_root),
        "artifact_hash": compute_artifact_hash(artifact_path),
        "hash_algorithm": HASH_ALGORITHM,
        "design_consensus_ref": design_consensus_ref,
        "design_closing_state": status.closing_state,
        "vetted_by": list(vetted_by),
        "known_flaws": known_flaws,
        "operator_ack": bool(operator_ack),
        "action_classes": action_classes,
        "followups_resolved": not open_fu,
        "open_followups": open_fu,
    }
    _token_path(artifact_path, repo_root).write_text(json.dumps(token, indent=2), encoding="utf-8")
    return token


def verify_delivery_token(artifact_path: Path, repo_root: Path | None = None) -> dict:
    """Fail-closed delivery check. Returns {ok, reason, token_id?}. ok=True only
    if: a token exists for this artifact, its hash matches the CURRENT file, its
    design_consensus_ref is still sealed, known_flaws==[] (or operator_ack), and
    >=2 non-claude reviewers vetted it."""
    try:
        repo_root = repo_root or _resolve_repo_root()
        artifact_path = Path(artifact_path)
        tp = _token_path(artifact_path, repo_root)
        if not tp.exists():
            return {"ok": False, "reason": "no delivery-readiness token (artifact not consensus-vetted)"}
        token = json.loads(tp.read_text(encoding="utf-8"))
        if token.get("artifact_path") != _canonical_artifact_key(artifact_path, repo_root):
            return {"ok": False, "reason": "token artifact_path mismatch"}
        current = compute_artifact_hash(artifact_path)
        if token.get("artifact_hash") != current:
            return {"ok": False, "reason": "artifact changed since vetting (hash mismatch) — re-vet"}
        status = resolve_consensus_ref(token.get("design_consensus_ref", ""), repo_root)
        if not status.sealed:
            return {"ok": False, "reason": f"design ref no longer sealed: {status.detail}"}
        flaws = token.get("known_flaws") or []
        if flaws and not token.get("operator_ack"):
            return {"ok": False, "reason": f"known_flaws present without operator_ack: {flaws}"}
        non_claude = [r for r in (token.get("vetted_by") or []) if r and "claude" not in r.lower()]
        if len(non_claude) < 2:
            return {"ok": False, "reason": f"<2 non-claude reviewers: {non_claude}"}
        # 1.16.1: re-check follow-up completeness against the live ledger.
        action_classes = token.get("action_classes") or []
        if action_classes:
            from consensus_mcp import _followup_completeness as _fc
            ledger = _fc.read_ledger(str(artifact_path), repo_root)
            complete, open_fu = _fc.followups_complete(action_classes, ledger)
            if not complete:
                return {"ok": False, "reason": f"unresolved required follow-ups: {open_fu}"}
        return {"ok": True, "reason": "consensus-vetted, hash-current, sealed, follow-ups complete", "token_id": token.get("token_id")}
    except Exception as exc:  # fail-closed
        return {"ok": False, "reason": f"verify error (fail-closed): {exc}"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="consensus_mcp._delivery_readiness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint")
    m.add_argument("artifact"); m.add_argument("--design-consensus-ref", required=True)
    m.add_argument("--vetted-by", required=True, help="comma-separated reviewer ids")
    m.add_argument("--known-flaws", default=""); m.add_argument("--operator-ack", action="store_true")
    v = sub.add_parser("verify"); v.add_argument("artifact")
    ns = ap.parse_args(argv)
    if ns.cmd == "mint":
        try:
            tok = mint_delivery_token(
                Path(ns.artifact), design_consensus_ref=ns.design_consensus_ref,
                vetted_by=[s.strip() for s in ns.vetted_by.split(",") if s.strip()],
                known_flaws=[s.strip() for s in ns.known_flaws.split(",") if s.strip()],
                operator_ack=ns.operator_ack)
            print(json.dumps({"ok": True, "token_id": tok["token_id"]})); return 0
        except DeliveryReadinessError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr); return 2
    res = verify_delivery_token(Path(ns.artifact))
    print(json.dumps(res)); return 0 if res["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
