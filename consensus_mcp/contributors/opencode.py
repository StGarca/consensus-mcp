"""Opt-in opencode tool reviewer using the shared disposable runner."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from consensus_mcp._dispatch_openai_compat import parse_json_output, prepare_template, profile_from_argv
from consensus_mcp.contributors._subprocess_adapter import SubprocessContributorAdapter


def _resolve_opencode_bin(binary: str) -> str:
    """Resolve the way a shell would: Windows CreateProcess does not apply
    PATHEXT, so a bare `opencode` cannot start npm's `opencode.cmd` launcher."""
    return shutil.which(binary) or binary


def _invoke_opencode(*, prompt, model, repo_root, schema_path, ns, log_path, anchors):
    from consensus_mcp._dispatch_runner import run_process
    cwd = repo_root.resolve()
    model_arg = model if "/" in model else f"dgx/{model}"
    env = dict(os.environ, PWD=str(cwd))
    # The prompt goes over stdin: on the command line a real review packet
    # exceeds Windows' 32k-character limit (WinError 206). The read-only
    # `plan` agent reviews without editing files.
    argv = [_resolve_opencode_bin(ns.opencode_bin), "run", "--agent", "plan", "-m", model_arg]
    # The agent reads files silently and prints mostly at the end (live: 7.5
    # minutes between its first sentence and the JSON), so a silence watchdog
    # kills healthy reviews; the seat timeout bounds the run instead.
    stall = int(max(float(ns.stall_silence_seconds or 0), float(ns.timeout_seconds or 0)))
    return run_process(argv, prompt=prompt, cwd=cwd,
                       env=env, timeout_seconds=ns.timeout_seconds,
                       stall_silence_seconds=stall,
                       log_path=log_path, anchors=anchors)


def _parse(raw, goal_packet, review_packet, repo_root, ns, schema_path):
    # opencode is an agent: it may narrate ("I'll ground this...") and fence
    # the JSON, as DeepSeek did live. Recover the object like the other agent
    # seats; schema validation below is unchanged.
    from consensus_mcp._dispatch_base import _extract_json_from_text
    return parse_json_output(_extract_json_from_text(raw), schema_path,
                             None if ns.schema else ns.profile_data)


def _configure_parser(parser):
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--profile-json", help=argparse.SUPPRESS)


def main(argv=None):
    from consensus_mcp._dispatch_runner import ProviderSpec, main_for_provider
    argv = sys.argv[1:] if argv is None else argv
    return main_for_provider(ProviderSpec(name="opencode-deepseek", invoke=_invoke_opencode,
        parse=_parse, default_model="DeepSeek-V4.1-Flash-1017",
        template_name="gemini", configure_parser=_configure_parser, disposable=True,
        profile=profile_from_argv(argv), prepare_template=prepare_template), argv)


class OpencodeAdapter(SubprocessContributorAdapter):
    dispatch_module = "consensus_mcp.contributors.opencode"

    def __init__(self, profile, adapter_config=None):
        super().__init__(adapter_config)
        self.profile = profile
        self.name = profile["name"]

    def _extra_argv(self, merged_options):
        profile = dict(self.profile)
        profile.update(merged_options)
        argv = ["--profile", self.name, "--profile-json", json.dumps(profile)]
        command = merged_options.get("command") or merged_options.get("opencode_bin")
        if command:
            argv += ["--opencode-bin", str(command)]
        if merged_options.get("model"):
            argv += ["--model", str(merged_options["model"])]
        return argv


if __name__ == "__main__":
    raise SystemExit(main())
