"""consensus.start_consult MCP tool (P2.1) - the cold-start scaffold entrypoint."""
from __future__ import annotations

from consensus_mcp._start_consult import start_consult

SCHEMA = {
    "name": "consensus.start_consult",
    "description": (
        "START HERE to run a consensus review. Scaffolds a new consult: creates "
        "the iteration dir + a valid goal_packet, ARMS the gate, and returns the "
        "EXACT next commands (dispatch each reviewer in its own shell, then "
        "consensus.approve). Does not dispatch or synthesize - that is the host's "
        "job. Use this instead of hand-authoring a goal_packet."),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "the design question / what to review"},
            "scope_glob": {"type": ["string", "array"], "items": {"type": "string"},
                           "description": "files the eventual approval will cover; pass a LIST for a multi-root consult (G3)"},
            "reviewers": {"type": ["array", "null"], "items": {"type": "string"},
                          "description": "reviewer families (default codex,gemini,grok,kimi)"},
            "repo_root": {"type": ["string", "null"]},
        },
        "required": ["question", "scope_glob"],
        "additionalProperties": False,
    },
}


def handle(question: str, scope_glob, reviewers=None, repo_root: str | None = None) -> dict:
    return start_consult(question=question, scope_glob=scope_glob,
                         reviewers=reviewers, repo_root=repo_root)


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
