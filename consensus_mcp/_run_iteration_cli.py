#!/usr/bin/env python3
"""Run a consensus-mcp iteration end-to-end from the command line.

The supported console script (`consensus-mcp-run-iteration`) for hosting a full
propose-converge / post-review iteration. It is a THIN wrapper around
`consensus_mcp.tools.consensus_run_iteration.handle()` - the single execution
implementation - so a non-Claude AI host stops hand-rolling its own shim that
calls handle() directly (the gap a Codex-hosted field run exposed; consult
iteration-approve-two-...-f641f060, Component 2).

Output contract (consult Q4, weighted synthesis):
  - ALWAYS prints the structured outcome JSON to stdout (a no-extra-file path for
    batch hosts - codex).
  - ALSO writes the outcome to --outcome, defaulting to
    {iteration-dir}/run-outcome.json (gemini/grok/kimi).

Exit code: 0 when the iteration result is ok, else 1.

Foreign hosts: external reviewer CLIs need writable HOME/config/session paths, so
run this OUTSIDE a restrictive sandbox. See docs/operations/hosting-a-consult.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from consensus_mcp.tools import consensus_run_iteration


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="consensus-mcp-run-iteration",
        description="Run one consensus-mcp iteration end-to-end and report the outcome.",
    )
    p.add_argument("--iteration-dir", required=True,
                   help="consensus-state/active/<iteration> directory")
    p.add_argument("--goal-packet", required=True, help="path to goal_packet.yaml")
    p.add_argument("--target", required=True,
                   help="document under review (workflow A) or the produced artifact (workflow B)")
    p.add_argument("--config", default=None, help="path to .consensus/config.yaml")
    p.add_argument("--claude-proposal", default=None,
                   help="path to claude's proposal YAML (required for propose-converge/advisory)")
    p.add_argument("--host-peer-review-yaml", default=None,
                   help="path to a host_peer review YAML")
    p.add_argument("--repo-root", default=None, help="repo root override")
    p.add_argument("--rigor-tier", choices=["quick", "standard", "deep"], required=True,
                   help="operator-declared rigor tier; use deep for hard problems")
    p.add_argument("--touches-governance-surface", action="store_true",
                   help="raise and lock the effective tier to deep")
    p.add_argument("--security-or-irreversible", action="store_true",
                   help="raise and lock the effective tier to deep")
    p.add_argument("--outcome", default=None,
                   help="where to write the outcome JSON (default: <iteration-dir>/run-outcome.json)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    claude_yaml = (
        Path(args.claude_proposal).read_text(encoding="utf-8")
        if args.claude_proposal else None
    )
    host_peer_yaml = (
        Path(args.host_peer_review_yaml).read_text(encoding="utf-8")
        if args.host_peer_review_yaml else None
    )

    result = consensus_run_iteration.handle(
        iteration_dir=args.iteration_dir,
        goal_packet_path=args.goal_packet,
        target_path=args.target,
        config_path=args.config,
        claude_proposal_yaml=claude_yaml,
        host_peer_review_yaml=host_peer_yaml,
        repo_root=args.repo_root,
        rigor_tier=args.rigor_tier,
        touches_governance_surface=args.touches_governance_surface,
        security_or_irreversible=args.security_or_irreversible,
    )

    payload = {
        "iteration_dir": args.iteration_dir,
        "goal_packet": args.goal_packet,
        "target": args.target,
        "config": args.config,
        "rigor_tier": args.rigor_tier,
        "result": result,
    }
    outcome_path = Path(args.outcome) if args.outcome else Path(args.iteration_dir) / "run-outcome.json"
    try:
        outcome_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:  # never lose the result to an unwritable outcome path
        print(f"warning: could not write outcome to {outcome_path}: {exc}", file=sys.stderr)

    # Always print the structured outcome (no-extra-file path for batch hosts).
    print(json.dumps(payload, indent=2))
    return 0 if isinstance(result, dict) and result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
