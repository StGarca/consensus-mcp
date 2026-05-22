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
from consensus_mcp import _contributor_profiles as profiles
from consensus_mcp.contributors import ContributorAdapter
from consensus_mcp.contributors.base import DispatchPacket
from consensus_mcp.contributors.claude import ClaudeAdapter
from consensus_mcp.contributors.codex import CodexAdapter
from consensus_mcp.contributors.gemini import GeminiAdapter
from consensus_mcp.contributors.profile_adapter import ProfileAdapter
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

# OPEN contributor registry (2026-05-22, "2-or-20-or-200 AIs" acceptance). The
# built-ins above are NOT a closed set — ANY number of contributors with ANY
# names can be registered with ZERO code changes, so a clean install supports
# min-2 / max-N / any-combination. A registered name shadows a same-named
# built-in (host swaps an impl).
_REGISTERED_ADAPTERS: dict[str, type[ContributorAdapter]] = {}


def register_contributor(name: str, adapter_cls: type[ContributorAdapter]) -> None:
    """Register a contributor adapter under `name` (any name). The no-classes,
    open-set extension point: adding the Nth AI is this one call, identical for
    the 2nd or the 200th — no special-casing, no enum edit."""
    if not name or not isinstance(name, str):
        raise EngineFactoryError(f"contributor name must be a non-empty string; got {name!r}")
    if not (isinstance(adapter_cls, type) and issubclass(adapter_cls, ContributorAdapter)):
        raise EngineFactoryError(
            f"adapter for {name!r} must subclass ContributorAdapter; got {adapter_cls!r}")
    _REGISTERED_ADAPTERS[name] = adapter_cls


def unregister_contributor(name: str) -> None:
    """Remove a registered adapter (test isolation / host reconfiguration)."""
    _REGISTERED_ADAPTERS.pop(name, None)


def known_contributor_keys() -> list[str]:
    """All currently-CONSTRUCTIBLE contributor keys (built-in + registered).

    This is the set `build_adapters()` can actually instantiate; it powers the
    fail-closed error message there and is a public API for a host to query what
    it can run. NOTE: it is NOT the anchoring linter's name set — the linter
    sources contributor NAMES from the project config (or `KNOWN_CONTRIBUTORS`
    as a static fallback), because in a CLI/author context no adapters are
    registered yet, so this set would understate the real contributor list."""
    return sorted(set(_BUILTIN_ADAPTERS) | set(_REGISTERED_ADAPTERS))


def _resolve_adapter(key: str) -> "type[ContributorAdapter] | None":
    return _REGISTERED_ADAPTERS.get(key) or _BUILTIN_ADAPTERS.get(key)


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

    # 1.17 review (codex-002): read per-contributor config from `contributors.
    # adapters` — the key default_config() + validate() actually use. The old
    # `contributors.config` key was never populated, so adapter_config (e.g.
    # model overrides) was silently always empty. Fall back to the legacy
    # `.config` key for any pre-existing config that used it.
    contributors_block = config.get("contributors", {}) or {}
    per_contributor = (contributors_block.get("adapters")
                       or contributors_block.get("config")
                       or {})

    # v1.18.0 B-routing (converged-plan decision.engine_factory): resolution
    # order per enabled key is (a) _REGISTERED_ADAPTERS, (b) _BUILTIN_ADAPTERS
    # (claude/codex/gemini → existing classes), (c) merged profiles. Only step
    # (c) needs the profile map, so it is loaded LAZILY (and at most once) — a
    # config with only built-in classes (the R8 case) never touches profile IO.
    _merged_profiles_cache: dict | None = None

    def _merged_profiles() -> dict:
        nonlocal _merged_profiles_cache
        if _merged_profiles_cache is None:
            _merged_profiles_cache = profiles.merge_profiles(
                profiles.load_builtin_profiles(),
                contributors_block.get("profiles") or {},
            )
        return _merged_profiles_cache

    adapters: dict[str, ContributorAdapter] = {}
    for key in enabled:
        adapter_config = per_contributor.get(key) or {}

        # (a) + (b): a registered or built-in adapter CLASS wins. This keeps
        # claude/codex/gemini on their existing classes (R8) even though they
        # also carry metadata-only profiles.
        ctor = _resolve_adapter(key)
        if ctor is not None:
            if key == "claude":
                adapters[key] = ctor(
                    adapter_config=adapter_config,
                    artifact_callback=claude_artifact_callback,
                )
            else:
                adapters[key] = ctor(adapter_config=adapter_config)
            continue

        # (c): no class for this key — fall back to the merged profiles.
        profile = _merged_profiles().get(key)
        if profile is not None:
            kind = profile.get("kind")
            if kind == profiles.KIND_HOST:
                # kind:host is reserved for the in-process orchestrator (claude),
                # which always resolves via _BUILTIN_ADAPTERS above. A host
                # profile under any other name must NOT be spun up as a
                # subprocess — fail closed.
                raise EngineFactoryError(
                    f"contributor {key!r} has profile kind={profiles.KIND_HOST!r} "
                    f"but no built-in host adapter. kind:host is reserved for the "
                    f"in-process orchestrator (claude); it is never dispatched as a "
                    f"subprocess. Use kind:{profiles.KIND_CLI_REVIEWER!r} for a "
                    f"CLI contributor, or remove {key!r} from contributors.enabled."
                )
            if kind == profiles.KIND_CLI_REVIEWER:
                adapters[key] = ProfileAdapter(profile, adapter_config=adapter_config)
                continue
            raise EngineFactoryError(
                f"contributor {key!r} profile has unsupported kind {kind!r}; "
                f"expected {profiles.KIND_CLI_REVIEWER!r} (or {profiles.KIND_HOST!r} "
                f"for the reserved host)."
            )

        # Nothing matched: unknown contributor. Fail closed.
        known_profile_names = sorted(_merged_profiles().keys())
        raise EngineFactoryError(
            f"unknown contributor key {key!r}; "
            f"constructible classes: {known_contributor_keys()}; "
            f"profile names: {known_profile_names}. "
            f"Register a custom contributor via "
            f"engine_factory.register_contributor(name, AdapterClass), or add a "
            f"kind:cli_reviewer profile under contributors.profiles."
        )
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
