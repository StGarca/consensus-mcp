"""consensus-mcp-deliver - mint a delivery token for a modified file (P0.3).

The Stop gate nags "claim done -> mint a delivery token" but no CLI existed to do
it (it named a phantom `consensus-verify`, and the real mint was only reachable
via a hand-written `python -c`). This is that command: a thin wrapper over
`_delivery_readiness.mint_delivery_token`. The trust model is unchanged - it
REFUSES unless the design ref is a sealed consult vetted by >=2 non-claude
reviewers (no self-judging).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus-mcp-deliver",
        description=(
            "Mint a delivery token for a modified file, proving it was vetted by a "
            "sealed cross-AI consult. Refuses unless --design-consensus-ref is a "
            "sealed iteration vetted by >=2 non-claude reviewers (no self-judging)."
        ),
    )
    p.add_argument("--file", required=True, help="the modified file to mint a token for")
    p.add_argument("--design-consensus-ref", required=True,
                   help="the sealed iteration name that vetted this change")
    p.add_argument("--vetted-by", required=True,
                   help="comma-separated reviewer families that vetted it (>=2 non-claude)")
    p.add_argument("--known-flaws", default="",
                   help="comma-separated known residual flaws (requires --operator-ack)")
    p.add_argument("--operator-ack", action="store_true",
                   help="operator acknowledges the known flaws (required if any)")
    p.add_argument("--action-classes", default="",
                   help="comma-separated action classes (e.g. release) for follow-up completeness")
    p.add_argument("--repo-root", default=None, help="repo root override")
    args = p.parse_args(argv)

    from consensus_mcp._delivery_readiness import (
        DeliveryReadinessError,
        mint_delivery_token,
    )

    def _split(s: str) -> list[str]:
        return [x.strip() for x in s.split(",") if x.strip()]

    try:
        token = mint_delivery_token(
            Path(args.file),
            design_consensus_ref=args.design_consensus_ref,
            vetted_by=_split(args.vetted_by),
            known_flaws=_split(args.known_flaws),
            operator_ack=args.operator_ack,
            action_classes=_split(args.action_classes),
            repo_root=Path(args.repo_root) if args.repo_root else None,
        )
    except DeliveryReadinessError as exc:
        print(json.dumps({"ok": False, "error_type": "DeliveryReadinessError",
                          "error": str(exc)}, indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "error": str(exc)}, indent=2))
        return 2
    print(json.dumps({"ok": True, "token": token}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
