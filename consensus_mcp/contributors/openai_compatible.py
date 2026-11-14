"""Contributor bridge for a configured OpenAI-compatible HTTP seat."""
import json

from consensus_mcp.contributors._subprocess_adapter import SubprocessContributorAdapter


class OpenAICompatibleAdapter(SubprocessContributorAdapter):
    dispatch_module = "consensus_mcp._dispatch_openai_compat"

    def __init__(self, profile, adapter_config=None):
        super().__init__(adapter_config)
        self.profile = profile
        self.name = profile["name"]

    def _extra_argv(self, merged_options):
        profile = dict(self.profile)
        profile.update(merged_options)
        argv = ["--profile", self.name, "--profile-json", json.dumps(profile)]
        if merged_options.get("model"):
            argv += ["--model", str(merged_options["model"])]
        return argv
