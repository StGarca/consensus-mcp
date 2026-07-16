"""Grok contributor adapter - wraps consensus_mcp._dispatch_grok.

Gemini-twin (converged consult iteration-v131-grok-design-2026-05-26).
Phase semantics same as gemini/codex: all phases reuse the existing
grok templates; phase intent conveyed via goal_packet + forwarded as
`--mode` to the dispatcher.

The generic dispatch/capture/parse/seal flow lives in
_subprocess_adapter.SubprocessContributorAdapter; this class supplies only
grok's reviewer-specific argv flags.
"""
from __future__ import annotations

from consensus_mcp.contributors._subprocess_adapter import (
    SubprocessContributorAdapter,
)


class GrokAdapter(SubprocessContributorAdapter):
    """Grok contributor - subprocess via _dispatch_grok.main."""

    name = "grok"
    dispatch_module = "consensus_mcp._dispatch_grok"

    def _extra_argv(self, merged_options: dict) -> list[str]:
        argv: list[str] = []
        model = merged_options.get("model")
        if model:
            argv += ["--model", model]
        effort = merged_options.get("effort")
        if effort:
            argv += ["--effort", effort]
        stall = merged_options.get("stall_silence_seconds")
        if stall is not None:
            argv += ["--stall-silence-seconds", str(stall)]
        return argv
