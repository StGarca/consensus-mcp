"""consensus.approve MCP tool - composed consult-approval (Q6 + Finding C/#7).

Thin wrapper over consensus_mcp._approve_consult.approve_consult so the MCP tool
and the consensus-mcp-approve CLI share ONE implementation AND one strict
repo-root resolver (Finding #7: the prior MCP delivery path used a looser
cwd-fallback resolver and minted/sealed against a different root than the shell
binaries). Validates the >=2-non-claude precondition, seals the outcome
mechanically, mints the .consensus/design-approved marker, and re-validates it.
Does NOT author the converged plan (trust boundary).
"""
from __future__ import annotations

from consensus_mcp._approve_consult import approve_consult

SCHEMA = {
    "name": "consensus.approve",
    "description": (
        "Validate a converged consult and mint the .consensus/design-approved "
        "marker in one step (precondition check + mechanical seal + mint + "
        "re-validate). Replaces the manual prepare -> edit EDIT_ME -> mint slog. "
        "Does NOT author the converged plan; it must already exist as "
        "converged-plan.yaml in the iteration dir. Trust model unchanged "
        "(>=2 non-claude families, hash match, scope confinement, fail-closed)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "iteration": {
                "type": "string",
                "description": "Iteration name (-> consensus-state/active/<name>) or a path.",
            },
            "scope_glob": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "Files the approval authorizes edits to (e.g. 'consensus_mcp/_x.py'). Must not be overbroad ('*'/'**'). Pass a LIST to cover a multi-root change in one approval (G3); each glob is confined to the goal_packet's allowed_files independently (max 8, tight, deduped).",
            },
            "converged_plan": {
                "type": "string",
                "description": "Converged plan file (bare name or full path). The canonical 'converged-plan.yaml' is required.",
            },
            "repo_root": {
                "type": ["string", "null"],
                "description": "Repo root override. Default: CONSENSUS_MCP_REPO_ROOT (strict, env-first) - the SAME resolver the shell binaries use.",
            },
        },
        "required": ["iteration", "scope_glob"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "iteration": {"type": ["string", "null"]},
            "non_claude_reviewers": {"type": ["integer", "null"]},
            "converged_plan_sha256": {"type": ["string", "null"]},
            "scope_glob": {"type": ["string", "null"]},
            "scope_globs": {"type": ["array", "null"], "items": {"type": "string"}},
            "marker_path": {"type": ["string", "null"]},
            "revalidated": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
            "error_type": {"type": ["string", "null"]},
        },
    },
}


def handle(iteration: str, scope_glob,
           converged_plan: str = "converged-plan.yaml",
           repo_root: str | None = None) -> dict:
    return approve_consult(
        iteration=iteration,
        scope_glob=scope_glob,
        converged_plan=converged_plan,
        repo_root=repo_root,
    )


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
