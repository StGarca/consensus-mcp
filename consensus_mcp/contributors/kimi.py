"""Kimi contributor adapter - wraps consensus_mcp._dispatch_kimi.

Mirror of contributors/codex.py: normalizes _dispatch_kimi's packet->argv
translation + result extraction into the ContributorAdapter interface, so kimi
is a first-class built-in (not a generic ProfileAdapter cli_reviewer) and keeps
_dispatch_kimi's hardened behavior (env-scrub, exit-75 retry, disposable
workdir, integrity check). Phase->mode via the shared _phase_mode.phase_to_mode
(CONVERGE->"review", which kimi's --mode {review,proposal} accepts).

The generic dispatch/capture/parse/seal flow lives in
_subprocess_adapter.SubprocessContributorAdapter; this class supplies only
kimi's reviewer-specific argv flags.
"""
from __future__ import annotations

from consensus_mcp.contributors._subprocess_adapter import (
    SubprocessContributorAdapter,
)


class KimiAdapter(SubprocessContributorAdapter):
    """Kimi contributor - subprocess via _dispatch_kimi.main."""

    name = "kimi"
    dispatch_module = "consensus_mcp._dispatch_kimi"

    def _extra_argv(self, merged_options: dict) -> list[str]:
        argv: list[str] = []
        command = merged_options.get("command") or merged_options.get("kimi_bin")
        if command:
            argv += ["--kimi-bin", command]
        model = merged_options.get("model")
        if model:
            argv += ["--model", model]
        if "thinking" in merged_options:
            argv += ["--thinking" if merged_options["thinking"] else "--no-thinking"]
        stall = merged_options.get("stall_silence_seconds")
        if stall is not None:
            argv += ["--stall-silence-seconds", str(stall)]
        return argv
