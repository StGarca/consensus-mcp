"""consensus.results MCP tool - read-only project scorecard.

Returns the aggregated project scorecard built from
``consensus-state/state/results-v1.jsonl`` (see
``consensus_mcp._results_rollup``). This is the MCP surface of the same logic
behind the ``consensus results`` console-script.

Per design spec docs/design-consults/v1.19.0-result-logging.md:
  * callable_by: any (READ-ONLY - never writes the ledger)
  * inputs: {} (no parameters)
  * outputs: the scorecard dict - authoritative (forward) totals, a SEGREGATED
    backfilled (best-effort) section, and a count of skipped unknown-schema
    records.

Path resolution is lazy (honors CONSENSUS_MCP_STATE_ROOT / CONSENSUS_MCP_REPO_ROOT
at call time) via ``_results_rollup.build_scorecard``.
"""
from __future__ import annotations

from consensus_mcp import _results_rollup


SCHEMA = {
    "name": "consensus.results",
    "description": (
        "Read-only project scorecard aggregated from "
        "consensus-state/state/results-v1.jsonl: total findings by severity, "
        "dispositions (validated / dismissed_refuted / deferred / open), "
        "fixes applied, iteration count, convergence rate, and date span. "
        "Backfilled best-effort records are reported in a SEPARATE section and "
        "never folded into the authoritative totals. Records with an "
        "unsupported schema version are skipped and counted. Does NOT write."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "authoritative": {"type": "object"},
            "backfilled": {"type": "object"},
            "skipped_unknown_schema": {"type": "integer"},
            "ledger_path": {"type": "string"},
        },
        "required": ["authoritative", "backfilled", "skipped_unknown_schema"],
    },
}


def handle() -> dict:
    """MCP tool handler. No inputs per schema (inputs: {}).

    Called via handler(**{}) from server dispatch, or directly as handle().
    """
    return _results_rollup.build_scorecard()


def register(registry) -> None:
    """Register this tool with the server's ToolRegistry."""
    registry.register(SCHEMA["name"], SCHEMA, handle)
