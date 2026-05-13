"""Factory for assembling a WorkflowEngine from a `.consensus/config.yaml`.

Single source of truth for "given a validated config + repo root, return a
ready-to-run WorkflowEngine with all enabled contributors instantiated".

Reusable from Python and from the `consensus_run_iteration` MCP tool wrapper
so both entry points construct identical adapter pools.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from consensus_mcp import config as cfg
from consensus_mcp.contributors import ContributorAdapter
from consensus_mcp.contributors.base import DispatchPacket
from consensus_mcp.contributors.claude import ClaudeAdapter
from consensus_mcp.contributors.codex import CodexAdapter
from consensus_mcp.contributors.gemini import GeminiAdapter
from consensus_mcp.workflow_engine import WorkflowEngine, WorkflowError


class EngineFactoryError(RuntimeError):
    """Raised when a config requests a contributor we can't construct."""


# Registry of built-in contributor keys → adapter constructors.
# Each adapter takes `adapter_config: dict | None` at construction time and
# receives per-dispatch context (repo_root, iteration_dir, paths) via the
# DispatchPacket passed to dispatch().
_BUILTIN_ADAPTERS: dict[str, type[ContributorAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiAdapter,
}


def build_adapters(
    config: dict,
    *,
    claude_artifact_callback: Callable[[DispatchPacket], dict] | None = None,
) -> dict[str, ContributorAdapter]:
    """Build a {contributor_key: ContributorAdapter} dict from a validated config.

    For each key in `contributors.enabled`, instantiate the matching adapter
    using the per-contributor adapter_config block. Unknown keys raise
    EngineFactoryError (caller should validate config first via cfg.validate()
    to catch these earlier, but we fail-closed here too).

    `claude_artifact_callback`: required for workflow modes where ClaudeAdapter
    is dispatched (workflow #4 propose-converge; advisory). For workflow #3
    post-review the ClaudeAdapter is registered but not invoked, so the
    callback may be None.
    """
    enabled = config.get("contributors", {}).get("enabled", [])
    if not enabled:
        raise EngineFactoryError("config.contributors.enabled is empty")

    per_contributor = config.get("contributors", {}).get("config", {}) or {}

    adapters: dict[str, ContributorAdapter] = {}
    for key in enabled:
        ctor = _BUILTIN_ADAPTERS.get(key)
        if ctor is None:
            raise EngineFactoryError(
                f"unknown contributor key {key!r}; "
                f"built-in keys: {sorted(_BUILTIN_ADAPTERS.keys())}"
            )
        adapter_config = per_contributor.get(key) or {}
        if key == "claude":
            adapters[key] = ctor(
                adapter_config=adapter_config,
                artifact_callback=claude_artifact_callback,
            )
        else:
            adapters[key] = ctor(adapter_config=adapter_config)
    return adapters


def build_engine(
    config: dict,
    repo_root: Path,
    *,
    claude_artifact_callback: Callable[[DispatchPacket], dict] | None = None,
) -> WorkflowEngine:
    """Convenience: validate config, build adapters, return a WorkflowEngine.

    schema_version=0 is a legacy-synthesis sentinel per config.synthesize_
    legacy_config(); skip validation for legacy-mode dicts so the engine can
    still run against pre-iter-0015 repos without a .consensus/config.yaml.
    """
    if config.get("schema_version") != 0:
        cfg.validate(config)
    adapters = build_adapters(
        config,
        claude_artifact_callback=claude_artifact_callback,
    )
    try:
        return WorkflowEngine(config, adapters, repo_root)
    except WorkflowError as exc:
        raise EngineFactoryError(str(exc)) from exc
