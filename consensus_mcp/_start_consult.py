"""consensus-mcp-start-consult / consensus.start_consult - the ONE cold-start
entrypoint (consult Q3, unanimous).

A cold AI asked to "run a consensus review on X" should not have to hand-author a
goal_packet, guess the iteration layout, or know which tool to call. This command
does the scaffolding deterministically:
  - creates consensus-state/active/<iteration>/
  - writes a schema-valid goal_packet.yaml + a review-packet.yaml embedding the
    question (the thing reviewers read)
  - ARMS the gate (writes the session marker), so edits stay blocked until approval
  - returns the EXACT next commands: how to fan out the reviewers (own shell, auto
    pass_id) and how to approve.

It does NOT dispatch or synthesize - those are the host's job. It removes the
"how do I even start" guess.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

from consensus_mcp._design_approval import _marker_path as _design_marker_path
from consensus_mcp._dispatch_base import (
    _resolve_repo_root,
    validate_explicit_repo_root,
)
from consensus_mcp._session_state import write_session_marker


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "consult").lower()).strip("-")
    return (s[:48] or "consult")


def _resolve_repo(repo_root):
    """Explicit repo_root is VALIDATED against the marker contract (codex finding:
    never accept an explicit --repo-root verbatim); else strict auto-discovery."""
    return validate_explicit_repo_root(repo_root) if repo_root else _resolve_repo_root()


_DEFAULT_REVIEWERS = ["codex", "gemini", "grok", "kimi"]


def _configured_reviewers(rr: Path) -> list[str] | None:
    """The project's CONFIGURED independent panel from `.consensus/config.yaml`
    (`contributors.enabled`, minus host/host_peer kinds), or None if there is no
    config / no usable list. codex-rev-002 / kimi-rev-006: a consult must dispatch
    the panel the operator actually chose at init, not a hardcoded set that may
    name CLIs they never installed (or omit ones they did)."""
    cfg_path = rr / ".consensus" / "config.yaml"
    if not cfg_path.is_file():
        return None
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    contributors = cfg.get("contributors") or {}
    enabled = [n for n in (contributors.get("enabled") or [])
               if isinstance(n, str) and n.strip()]
    profiles = contributors.get("profiles") or {}

    def _is_host(name: str) -> bool:
        kind = ((profiles.get(name) or {}).get("kind") or "")
        return kind in ("host", "host_peer") or name == "claude"

    indep = [n for n in enabled if not _is_host(n)]
    return indep or None


def start_consult(question: str, scope_glob: str, reviewers=None,
                  repo_root=None, iteration_slug=None) -> dict:
    if not question or not question.strip():
        return {"ok": False, "error_type": "missing_question",
                "error": "a non-empty consult question is required"}
    if not scope_glob or not scope_glob.strip():
        return {"ok": False, "error_type": "missing_scope",
                "error": "scope_glob is required (the files the eventual approval will cover)"}
    try:
        rr = _resolve_repo(repo_root)
    except Exception as exc:
        return {"ok": False, "error_type": "repo_root_unresolved", "error": str(exc)}

    # Panel precedence: explicit arg > project config > built-in default.
    reviewers = reviewers or _configured_reviewers(rr) or _DEFAULT_REVIEWERS
    slug = iteration_slug or _slugify(question)
    # short content hash keeps the iteration id globally unique (avoids collisions
    # with a prior same-slug consult).
    h = hashlib.sha256(f"{slug}\x1f{question}\x1f{scope_glob}".encode()).hexdigest()[:8]
    iter_id = f"iteration-{slug}-{h}"
    iter_dir = rr / "consensus-state" / "active" / iter_id
    if iter_dir.exists():
        return {"ok": False, "error_type": "iteration_exists",
                "error": f"{iter_dir} already exists"}
    iter_dir.mkdir(parents=True)

    goal_packet = {
        "schema_version": 1,
        "pilot_id": iter_id,
        "goal": {
            "summary": question.strip()[:280],
            "desired_end_state": (
                "Each reviewer returns a structured proposal answering the question. "
                "The host synthesizes a converged-plan.yaml (weighted-synthesis), "
                "then approves. Empty findings is not acceptable."
            ),
            "non_goals": ["implement before approval", "audit unrelated code"],
        },
        "allowed_files": [scope_glob],
        "allowed_sections": [],
        "forbidden_files": ["consensus-state/"],
        "max_iterations": 1,
        "max_patch_size": 0,
        "fix_author_policy": "permissive",
        "validators_required": [],
        "acceptance_gates": [
            {"id": "A1", "description": "each reviewer returns a non-empty proposal",
             "check": "true"}],
        "stop_conditions": ["max_iteration_count_reached"],
        "operator_escalation_triggers": ["touched_forbidden_files"],
        "authorization": {
            "authorized_by": "operator",
            "codex_patch_apply_authorized": False,
            "workflow": "propose-converge",
            "panel": list(reviewers),
        },
    }
    (iter_dir / "goal_packet.yaml").write_text(
        yaml.safe_dump(goal_packet, sort_keys=False), encoding="utf-8")

    review_packet = {
        "schema_version": 1,
        "iteration_id": iter_id,
        "question": question.strip(),
        "scope_glob": scope_glob,
        "instructions": (
            "Answer the question with a structured proposal. State the prior you "
            "reasoned from. Do not propose edits before approval."),
    }
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump(review_packet, sort_keys=False), encoding="utf-8")

    # Stale-marker hygiene (kimi-rev-004): a brand-new consult is UNAPPROVED, so
    # any pre-existing `.consensus/design-approved` belongs to a PRIOR consult and
    # must not carry over - otherwise the gate would arm for THIS iteration while a
    # stale design-approved still authorizes an OLD scope (marker poisoning). Clear
    # it so the gate is correctly "armed but unapproved" until this consult's own
    # approve runs. (The design-approved is a re-validated trust pointer, not data
    # we lose anything by clearing - a still-valid prior approval can be re-minted.)
    # grok-rev-002: this must FAIL CLOSED. If a stale design-approved exists and we
    # cannot remove it, arming the session would activate the gate while that stale
    # marker still re-validates an OLD scope (verify_design_approval runs whenever
    # the session is active) - i.e. marker poisoning. Rather than silently pass and
    # leave a poisoned state, refuse to scaffold so the operator fixes it first.
    stale_marker = _design_marker_path(rr)
    if stale_marker.exists():
        try:
            stale_marker.unlink()
        except OSError as exc:
            return {"ok": False, "error_type": "stale_marker_unclearable",
                    "error": (f"a prior design-approved marker at {stale_marker} "
                              f"could not be removed ({exc}); it would authorize an "
                              f"OLD scope for this new consult. Remove it manually "
                              f"(or run `consensus-mcp-seal-iteration close --abandon`)"
                              f", then re-run start-consult."),
                    "iteration": iter_id}

    # ARM the gate: edits stay blocked until consensus-mcp-approve runs.
    try:
        write_session_marker(rr, iteration_id=iter_id, scope_glob=scope_glob,
                             activated_by="consensus-mcp-start-consult",
                             activation_source="console_script")
    except Exception as exc:
        return {"ok": False, "error_type": "gate_arm_failed", "error": str(exc),
                "iteration": iter_id}

    gp = f"consensus-state/active/{iter_id}/goal_packet.yaml"
    rp = f"consensus-state/active/{iter_id}/review-packet.yaml"
    dispatch_cmds = [
        (f"consensus-mcp-dispatch-{r} --goal-packet {gp} --iteration-dir "
         f"consensus-state/active/{iter_id} --reviewer-id {r} --mode proposal "
         f"--review-target {rp} --timeout-seconds 600   # own shell; omit --pass-id")
        for r in reviewers
    ]
    return {
        "ok": True,
        "iteration": iter_id,
        "iteration_dir": str(iter_dir),
        "goal_packet": str(iter_dir / "goal_packet.yaml"),
        "gate_armed": True,
        "next_steps": {
            "1_dispatch_each_reviewer_in_its_OWN_shell": dispatch_cmds,
            "2_synthesize": (
                f"Read the sealed *-review.yaml in {iter_dir} (or via "
                f"consensus.get_iteration_outcome), then author "
                f"consensus-state/active/{iter_id}/converged-plan.yaml (weighted-synthesis)."),
            "3_approve_to_unblock_edits": (
                f"consensus-mcp-approve --iteration {iter_id} --scope-glob {scope_glob!r}"),
            "4_disarm_when_done": (
                "consensus-mcp-seal-iteration close --iteration-dir "
                f"consensus-state/active/{iter_id}   # after edits + delivery "
                "tokens: clears the gate markers, returns it to dormant. The gate "
                "is armed NOW, so finish with this even if you abandon the consult "
                "(add --abandon to force-clear)."),
        },
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus-mcp-start-consult",
        description=("Scaffold a new consensus consult: create the iteration dir + a "
                     "valid goal_packet, arm the gate, and print the exact next "
                     "commands. The one-call cold-start entrypoint."))
    p.add_argument("--question", required=True, help="the design question / what to review")
    p.add_argument("--scope-glob", required=True,
                   help="files the eventual approval will cover (e.g. 'consensus_mcp/_x.py')")
    p.add_argument("--reviewers", default=None,
                   help="comma-separated reviewer families (default: the project's "
                        "configured panel from .consensus/config.yaml, else the "
                        "built-in default)")
    p.add_argument("--repo-root", default=None)
    args = p.parse_args(argv)
    # codex-rev-001: leave reviewers=None when --reviewers is omitted so the
    # CONFIGURED panel is used. A hardcoded argparse default would shadow the
    # config every time and re-introduce the very bypass we just fixed.
    explicit_reviewers = (
        [r.strip() for r in args.reviewers.split(",") if r.strip()]
        if args.reviewers else None
    )
    res = start_consult(
        question=args.question, scope_glob=args.scope_glob,
        reviewers=explicit_reviewers,
        repo_root=args.repo_root)
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
