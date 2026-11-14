"""Dispatchable Claude reviewer without changing the in-process host adapter."""
import json

from consensus_mcp.contributors._subprocess_adapter import SubprocessContributorAdapter


class ClaudeCliAdapter(SubprocessContributorAdapter):
    name = "claude"
    dispatch_module = "consensus_mcp._dispatch_claude"

    def __init__(self, adapter_config=None, profile=None):
        super().__init__(adapter_config)
        self.profile = profile

    def _extra_argv(self, merged_options):
        argv = []
        if self.profile:
            argv += ["--profile-json", json.dumps(self.profile)]
        command = merged_options.get("command") or merged_options.get("claude_bin")
        if command:
            argv += ["--claude-bin", str(command)]
        for key, flag in (("model", "--model"),
                          ("stall_silence_seconds", "--stall-silence-seconds")):
            if merged_options.get(key) is not None:
                argv += [flag, str(merged_options[key])]
        return argv
