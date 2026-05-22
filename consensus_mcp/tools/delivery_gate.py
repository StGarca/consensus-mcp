"""delivery.* MCP tools — fail-closed delivery-readiness gate (1.16.0).

Exposes the anti-self-judge delivery gate (consensus_mcp._delivery_readiness)
on the MCP tool surface, the portable enforcement path (works across any
harness). Two tools:
  - delivery.request : verify an artifact is consensus-vetted before delivery.
  - delivery.mint    : mint a readiness token (refuses unless a SEALED consensus
                       iteration vetted the design — self-judging is impossible).

See docs/delivery-gate.md.
"""
from __future__ import annotations

from pathlib import Path

from consensus_mcp import _delivery_readiness as _dr

REQUEST_SCHEMA = {
    "name": "delivery.request",
    "description": (
        "Fail-closed delivery gate: returns {ok, reason, token_id?}. ok=True "
        "only if the artifact is consensus-vetted (sealed design_consensus_ref, "
        "current sha256, no unacked known_flaws, >=2 non-claude reviewers)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"artifact_path": {"type": "string"}},
        "required": ["artifact_path"],
    },
}

MINT_SCHEMA = {
    "name": "delivery.mint",
    "description": (
        "Mint a delivery-readiness token. REFUSES unless design_consensus_ref "
        "resolves to a SEALED/closed consensus iteration and >=2 non-claude "
        "reviewers vetted it — an agent cannot self-judge readiness."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "artifact_path": {"type": "string"},
            "design_consensus_ref": {"type": "string"},
            "vetted_by": {"type": "array", "items": {"type": "string"}},
            "known_flaws": {"type": ["array", "null"], "items": {"type": "string"}},
            "operator_ack": {"type": "boolean"},
        },
        "required": ["artifact_path", "design_consensus_ref", "vetted_by"],
    },
}


def handle_request(artifact_path: str) -> dict:
    return _dr.verify_delivery_token(Path(artifact_path))


def handle_mint(artifact_path: str, design_consensus_ref: str, vetted_by: list,
                known_flaws=None, operator_ack: bool = False) -> dict:
    try:
        tok = _dr.mint_delivery_token(
            Path(artifact_path), design_consensus_ref=design_consensus_ref,
            vetted_by=vetted_by, known_flaws=known_flaws, operator_ack=operator_ack)
        return {"ok": True, "token_id": tok["token_id"]}
    except _dr.DeliveryReadinessError as exc:
        return {"ok": False, "error": str(exc)}


def register(registry) -> None:
    registry.register(REQUEST_SCHEMA["name"], REQUEST_SCHEMA, handle_request)
    registry.register(MINT_SCHEMA["name"], MINT_SCHEMA, handle_mint)
