"""Codex contributor adapter - wraps consensus_mcp._dispatch_codex.

Per iter-0015 converged-plan Section C: codex's existing dispatch helper is
the underlying CLI invoker; this adapter normalizes its packet -> argv
translation and result extraction into the ContributorAdapter interface.

Phase semantics for v1: all three phases (propose, review, converge) reuse
the SAME prompt template (codex_review_template.md) - phase intent is
conveyed via the goal_packet's `desired_end_state` text that the workflow
engine writes appropriately per phase. This was empirically validated in
iter-0015's design consult (same template, different goal yielded design
proposals + convergence syntheses). Phase-specific templates can be added
in a later iteration if observed quality drops.

The generic dispatch/capture/parse/seal flow lives in
_subprocess_adapter.SubprocessContributorAdapter; this class supplies only
codex's reviewer-specific argv flags.
"""
from __future__ import annotations

from consensus_mcp.contributors._subprocess_adapter import (
    SubprocessContributorAdapter,
)


class CodexAdapter(SubprocessContributorAdapter):
    """Codex contributor - subprocess via _dispatch_codex.main."""

    name = "codex"
    dispatch_module = "consensus_mcp._dispatch_codex"

    def _extra_argv(self, merged_options: dict) -> list[str]:
        argv: list[str] = []
        command = merged_options.get("command") or merged_options.get("codex_bin")
        if command:
            argv += ["--codex-bin", command]
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
