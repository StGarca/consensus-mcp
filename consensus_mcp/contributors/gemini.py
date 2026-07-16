"""Gemini contributor adapter - wraps consensus_mcp._dispatch_gemini.

Mirrors CodexAdapter. Phase semantics same as codex: all phases reuse the
existing gemini_review_template.md; phase intent conveyed via goal_packet.

The generic dispatch/capture/parse/seal flow lives in
_subprocess_adapter.SubprocessContributorAdapter; this class supplies only
gemini's reviewer-specific argv flags.
"""
from __future__ import annotations

from consensus_mcp.contributors._subprocess_adapter import (
    SubprocessContributorAdapter,
)


class GeminiAdapter(SubprocessContributorAdapter):
    """Gemini contributor - subprocess via _dispatch_gemini.main."""

    name = "gemini"
    dispatch_module = "consensus_mcp._dispatch_gemini"

    def _extra_argv(self, merged_options: dict) -> list[str]:
        argv: list[str] = []
        command = merged_options.get("command") or merged_options.get("gemini_bin")
        if command:
            argv += ["--gemini-bin", command]
        model = merged_options.get("model")
        if model:
            argv += ["--model", model]
        stall = merged_options.get("stall_silence_seconds")
        if stall is not None:
            argv += ["--stall-silence-seconds", str(stall)]
        return argv
