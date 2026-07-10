"""consensus.run_iteration MCP tool - drives an iteration end-to-end per
`.consensus/config.yaml`.

Per iter-0021 converged plan (Section A): wraps WorkflowEngine.run_iteration
behind an MCP tool surface so operators can run workflow #3 / #4 / advisory
via the installed MCP server, not just via Python import.

Inputs:
  - iteration_dir: path; engine writes effective-config.yaml + artifacts
  - goal_packet_path: path
  - target_path: path; for #3 the patch under review, for #4 the problem
    statement, for advisory the artifact
  - config_path: optional path; defaults to <repo_root>/.consensus/config.yaml
  - claude_proposal_yaml: optional string; when the workflow dispatches
    ClaudeAdapter, the tool wrapper supplies this via the artifact_callback
    so claude-the-orchestrator-tool-caller doesn't deadlock waiting on
    claude-the-adapter to produce content.

Output: structured dict with workflow_mode, converged, contributors_responsive,
contributors_timed_out, approve_votes, block_votes, blocking_objection_ids,
final_artifact_path, rationale, error.

Sealing already flows through T6 inside the contributor adapters; this tool
adds no new audit surface.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import yaml

from consensus_mcp import config as cfg
from consensus_mcp import _engine_factory as factory
from consensus_mcp import _contributor_profiles as profiles
from consensus_mcp import _tier_router
from consensus_mcp.contributors.base import DispatchPacket


SCHEMA = {
    "name": "consensus.run_iteration",
    "description": (
        "Run one iteration end-to-end per .consensus/config.yaml. Reads the "
        "config, instantiates the enabled contributor adapters, dispatches "
        "WorkflowEngine.run_iteration(), and returns the structured outcome "
        "(workflow mode, convergence, contributor responsiveness, final "
        "artifact path). Sealing already flows through T6 inside each "
        "adapter; this tool adds no audit surface. claude_proposal_yaml "
        "supplies claude-the-orchestrator's content for workflow modes that "
        "dispatch ClaudeAdapter (propose-converge, advisory)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "iteration_dir": {
                "type": "string",
                "description": "Absolute or repo-relative path to the iteration directory.",
            },
            "goal_packet_path": {
                "type": "string",
                "description": "Path to the iteration's goal_packet.yaml.",
            },
            "target_path": {
                "type": "string",
                "description": (
                    "Path to the workflow target. For workflow #3 (post-review): "
                    "the patch/diff/file being reviewed. For workflow #4 "
                    "(propose-converge): the problem statement. For advisory: "
                    "the artifact contributors recommend on."
                ),
            },
            "config_path": {
                "type": ["string", "null"],
                "description": (
                    "Override path to .consensus/config.yaml. Defaults to "
                    "<repo_root>/.consensus/config.yaml; if absent, legacy-mode "
                    "synthesis is used per cfg.synthesize_legacy_config()."
                ),
            },
            "claude_proposal_yaml": {
                "type": ["string", "null"],
                "description": (
                    "YAML string for claude-the-orchestrator's contributor "
                    "artifact. Required when workflow.mode dispatches "
                    "ClaudeAdapter (propose-converge, advisory). Must validate "
                    "as a contributor-artifact YAML: top-level fields "
                    "findings (list), goal_satisfied (bool), blocking_objections "
                    "(list)."
                ),
            },
            "host_peer_review_yaml": {
                "type": ["string", "null"],
                "description": (
                    "YAML string for the same-family blind SWE-reviewer's "
                    "(host_peer) contributor artifact, dispatched by the host "
                    "(e.g. consensus-host-peer-reviewer subagent) and handed "
                    "back here. SUPPLEMENTARY by construction: the adapter "
                    "stamps gate_eligible=false / weight=supplementary "
                    "authoritatively, so nothing supplied here can make a "
                    "host_peer close the cross-family gate. When an enabled "
                    "host_peer profile has no host_peer_review_yaml the "
                    "iteration GRACEFULLY soft-skips it (it is not load-bearing) "
                    "and reports it under supplementary_skipped. Must validate "
                    "as a contributor-artifact YAML: top-level fields findings "
                    "(list), goal_satisfied (bool), blocking_objections (list)."
                ),
            },
            "repo_root": {
                "type": ["string", "null"],
                "description": (
                    "Override repo root. Defaults to CONSENSUS_MCP_REPO_ROOT "
                    "env var or current working directory."
                ),
            },
            "rigor_tier": {
                "type": "string",
                "enum": ["quick", "standard", "deep"],
                "description": (
                    "Operator-declared rigor tier. Deep applies the hard-problem "
                    "model/effort preset and two-round workflow to every enabled "
                    "independent reviewer (minimum two)."
                ),
            },
            "touches_governance_surface": {
                "type": ["boolean", "null"],
                "description": "Declare governance/config/hook/gate/dispatcher/engine scope; raises and locks the effective tier to deep.",
            },
            "security_or_irreversible": {
                "type": ["boolean", "null"],
                "description": "Declare security-sensitive or irreversible scope; raises and locks the effective tier to deep.",
            },
        },
        "required": ["iteration_dir", "goal_packet_path", "target_path", "rigor_tier"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "workflow_mode": {"type": "string"},
            "converged": {"type": "boolean"},
            "convergence_rule": {"type": ["string", "null"]},
            "contributors_responsive": {"type": "array"},
            "contributors_timed_out": {"type": "array"},
            "approve_votes": {"type": "array"},
            "block_votes": {"type": "array"},
            "blocking_objection_ids": {"type": "array"},
            "final_artifact_path": {"type": ["string", "null"]},
            "rationale": {"type": ["string", "null"]},
            "supplementary_skipped": {"type": "array"},
            "error": {"type": ["string", "null"]},
            "error_type": {"type": ["string", "null"]},
            "rigor_tier": {"type": ["string", "null"]},
            "compute_preset": {"type": ["string", "null"]},
            "model_settings": {"type": "object"},
            "timeout_settings": {"type": "object"},
        },
    },
}


def _resolve_repo_root(override: str | None) -> Path:
    if override:
        return Path(override).resolve()
    env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def _resolve_config_path(
    explicit: str | None,
    repo_root: Path,
) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return repo_root / ".consensus" / "config.yaml"


def _load_config(config_path: Path, repo_root: Path) -> dict:
    """Load config. Falls back to legacy synthesis if no .consensus/config.yaml.

    Legacy synthesis returns schema_version=0 as a sentinel - validate() rejects
    that on purpose, so the legacy path skips validation per converged-plan
    Section D.
    """
    if config_path.exists():
        return cfg.load(config_path)
    return cfg.synthesize_legacy_config(repo_root)


def _build_claude_callback(claude_proposal_yaml: str | None):
    """Return an artifact_callback that returns the parsed YAML on each call.

    For multi-round workflows the same content is returned each round;
    operators wanting per-round content must structure their proposal yaml
    with the contributor-artifact shape and let the engine seal it as-is.
    """
    if claude_proposal_yaml is None:
        return None

    try:
        parsed = yaml.safe_load(claude_proposal_yaml)
    except yaml.YAMLError as exc:
        raise ValueError(f"claude_proposal_yaml is not valid YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            f"claude_proposal_yaml must parse to a mapping; got {type(parsed).__name__}"
        )
    for required in ("findings", "goal_satisfied", "blocking_objections"):
        if required not in parsed:
            raise ValueError(
                f"claude_proposal_yaml missing required field {required!r}"
            )
    # codex pass-1 rev-003: validate field types, not just presence.
    if not isinstance(parsed["findings"], list):
        raise ValueError(
            f"claude_proposal_yaml.findings must be a list; got "
            f"{type(parsed['findings']).__name__}"
        )
    if not isinstance(parsed["blocking_objections"], list):
        raise ValueError(
            f"claude_proposal_yaml.blocking_objections must be a list; got "
            f"{type(parsed['blocking_objections']).__name__}"
        )
    if not isinstance(parsed["goal_satisfied"], bool):
        raise ValueError(
            f"claude_proposal_yaml.goal_satisfied must be a bool; got "
            f"{type(parsed['goal_satisfied']).__name__}"
        )

    def _callback(packet: DispatchPacket) -> dict:
        # gemini pass-1 rev-002: deepcopy so multi-round mutations to nested
        # lists/dicts don't leak across rounds.
        return copy.deepcopy(parsed)

    return _callback


def _build_host_peer_callback(host_peer_review_yaml: str | None):
    """Return a host_peer_review_callback that echoes the supplied review YAML.

    Mirrors `_build_claude_callback` exactly: the YAML is parsed once, validated
    to a mapping carrying findings (list) / goal_satisfied (bool) /
    blocking_objections (list), and a deepcopy is returned on each call so
    multi-round mutations don't leak. Returns None when no YAML is supplied so
    the engine factory gracefully omits any enabled host_peer profile (it is
    SUPPLEMENTARY, not load-bearing).

    NOTE: nothing in this YAML can make a host_peer gate-eligible - the
    HostPeerAdapter stamps the canonical gate_eligible=false / weight=
    supplementary provenance AFTER spreading the callback output, so any
    `gate_eligible: true` / `weight: independent` claim here is overwritten.
    """
    if host_peer_review_yaml is None:
        return None

    try:
        parsed = yaml.safe_load(host_peer_review_yaml)
    except yaml.YAMLError as exc:
        raise ValueError(
            f"host_peer_review_yaml is not valid YAML: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            f"host_peer_review_yaml must parse to a mapping; got "
            f"{type(parsed).__name__}"
        )
    for required in ("findings", "goal_satisfied", "blocking_objections"):
        if required not in parsed:
            raise ValueError(
                f"host_peer_review_yaml missing required field {required!r}"
            )
    if not isinstance(parsed["findings"], list):
        raise ValueError(
            f"host_peer_review_yaml.findings must be a list; got "
            f"{type(parsed['findings']).__name__}"
        )
    if not isinstance(parsed["blocking_objections"], list):
        raise ValueError(
            f"host_peer_review_yaml.blocking_objections must be a list; got "
            f"{type(parsed['blocking_objections']).__name__}"
        )
    if not isinstance(parsed["goal_satisfied"], bool):
        raise ValueError(
            f"host_peer_review_yaml.goal_satisfied must be a bool; got "
            f"{type(parsed['goal_satisfied']).__name__}"
        )

    def _callback(packet: DispatchPacket) -> dict:
        return copy.deepcopy(parsed)

    return _callback


def _enabled_host_peers(loaded_config: dict) -> list[str]:
    """Return the enabled contributor keys whose merged profile is kind:host_peer.

    These are SUPPLEMENTARY same-family reviewers. When no host_peer_review_yaml
    is supplied the tool prunes them from contributors.enabled before building
    the engine (graceful soft-skip) and reports them under supplementary_skipped.
    """
    contributors = loaded_config.get("contributors", {}) or {}
    enabled = contributors.get("enabled", []) or []
    try:
        merged = profiles.merge_profiles(
            profiles.load_builtin_profiles(),
            contributors.get("profiles") or {},
        )
    except Exception:  # noqa: BLE001 - profile IO failure must not break the run
        return []
    return [
        name for name in enabled
        if profiles.resolve_kind(name, merged) == profiles.KIND_HOST_PEER
    ]


def _resolve_path(p: str | Path, repo_root: Path) -> Path:
    """Resolve `p` against `repo_root` when relative (codex pass-1 rev-002).

    Absolute paths pass through resolve() unchanged. Relative paths get joined
    onto repo_root BEFORE resolve() so they are correct regardless of the
    server process cwd.
    """
    p = Path(p)
    if not p.is_absolute():
        p = repo_root / p
    return p.resolve()


def handle(
    iteration_dir: str,
    goal_packet_path: str,
    target_path: str,
    config_path: str | None = None,
    claude_proposal_yaml: str | None = None,
    host_peer_review_yaml: str | None = None,
    repo_root: str | None = None,
    rigor_tier: str | None = None,
    touches_governance_surface: bool = False,
    security_or_irreversible: bool = False,
) -> dict:
    """Run one iteration end-to-end. Returns structured outcome dict."""
    supplementary_skipped: list[str] = []
    tier_decision: dict | None = None
    try:
        if rigor_tier is None:
            return {
                "ok": False,
                "error": (
                    "rigor_tier must be explicitly declared as quick, standard, "
                    "or deep; conversational hosts should map the operator's "
                    "plain-language declaration to this field"
                ),
                "error_type": "MissingRigorTierError",
            }
        rr = _resolve_repo_root(repo_root)
        iter_dir = _resolve_path(iteration_dir, rr)
        gp_path = _resolve_path(goal_packet_path, rr)
        tgt_path = _resolve_path(target_path, rr)
        cfg_path = (
            _resolve_path(config_path, rr) if config_path
            else _resolve_config_path(None, rr)
        )

        loaded_config = _load_config(cfg_path, rr)
        tier_decision = _tier_router.effective_tier(
            rigor_tier,
            touches_governance_surface=touches_governance_surface,
            security_or_irreversible=security_or_irreversible,
        )
        loaded_config = _tier_router.apply_tier_config(
            loaded_config, tier_decision,
        )

        # codex pass-3 rev-001: when claude is enabled AND the workflow mode
        # dispatches ClaudeAdapter (propose-converge, advisory), the operator
        # MUST supply claude_proposal_yaml. Workflow #3 post-review skips
        # claude entirely so the parameter is optional there.
        workflow_mode = loaded_config.get("workflow", {}).get("mode", "")
        enabled = loaded_config.get("contributors", {}).get("enabled", [])
        modes_dispatching_claude = {
            cfg.WORKFLOW_PROPOSE_CONVERGE,
            cfg.WORKFLOW_ADVISORY,
        }
        if (
            "claude" in enabled
            and workflow_mode in modes_dispatching_claude
            and claude_proposal_yaml is None
        ):
            return {
                "ok": False,
                "error": (
                    f"claude_proposal_yaml is required for workflow mode "
                    f"{workflow_mode!r} when 'claude' is in contributors.enabled. "
                    f"Without it, ClaudeAdapter has no artifact_callback and "
                    f"will raise DispatchError on first invocation."
                ),
                "error_type": "MissingClaudeProposalError",
            }

        if (
            tier_decision is not None
            and tier_decision["path"] == "A"
            and "claude" in enabled
        ):
            return {
                "ok": False,
                "error": (
                    "deep tier with an enabled Claude contributor requires the "
                    "orchestrator-driven Path A so Claude can genuinely reconverge "
                    "between rounds; consensus.run_iteration uses a static callback"
                ),
                "error_type": "OrchestratorPathRequiredError",
                "rigor_tier": tier_decision["tier"],
                "compute_preset": tier_decision["compute_preset"],
                "model_settings": tier_decision["model_settings"],
                "timeout_settings": tier_decision["timeout_settings"],
            }

        claude_callback = _build_claude_callback(claude_proposal_yaml)
        # Validates host_peer_review_yaml (malformed -> ValueError -> ok:false).
        host_peer_callback = _build_host_peer_callback(host_peer_review_yaml)

        # GRACEFUL SOFT-SKIP (converged-plan B): when a host_peer profile is
        # enabled but no host_peer_review_yaml was supplied, the factory would
        # build NO adapter for it and the engine would then fail-closed on the
        # missing adapter. host_peer is SUPPLEMENTARY, so instead prune those
        # enabled keys before building the engine and surface them in the result
        # as an informational note. (When the yaml IS supplied, the callback is
        # wired, the adapter is built, and nothing is pruned.)
        if host_peer_callback is None:
            host_peers = _enabled_host_peers(loaded_config)
            if host_peers:
                supplementary_skipped = list(host_peers)
                pruned = copy.deepcopy(loaded_config)
                contributors = pruned.setdefault("contributors", {})
                contributors["enabled"] = [
                    c for c in contributors.get("enabled", [])
                    if c not in host_peers
                ]
                loaded_config = pruned

        engine = factory.build_engine(
            loaded_config,
            repo_root=rr,
            claude_artifact_callback=claude_callback,
            host_peer_review_callback=host_peer_callback,
        )

        outcome = engine.run_iteration(iter_dir, gp_path, tgt_path)
    except (factory.EngineFactoryError, ValueError) as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    except cfg.ConfigValidationError as exc:
        return {
            "ok": False,
            "error": f"invalid config: {exc}",
            "error_type": "ConfigValidationError",
        }
    except Exception as exc:  # noqa: BLE001 - boundary translation
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }

    conv = outcome.convergence
    return {
        "ok": True,
        "workflow_mode": outcome.workflow_mode,
        "converged": (conv.converged if conv else False),
        "convergence_rule": (conv.rule if conv else None),
        "contributors_responsive": (conv.contributors_responsive if conv else []),
        "contributors_timed_out": (conv.contributors_timed_out if conv else []),
        "approve_votes": (conv.approve_votes if conv else []),
        "block_votes": (conv.block_votes if conv else []),
        "blocking_objection_ids": (conv.blocking_objection_ids if conv else []),
        "final_artifact_path": (
            str(outcome.final_artifact_path) if outcome.final_artifact_path else None
        ),
        "rationale": (conv.rationale if conv else None),
        "supplementary_skipped": supplementary_skipped,
        "error": outcome.error,
        "error_type": None,
        "rigor_tier": tier_decision["tier"] if tier_decision else None,
        "compute_preset": tier_decision["compute_preset"] if tier_decision else None,
        "model_settings": tier_decision["model_settings"] if tier_decision else {},
        "timeout_settings": tier_decision["timeout_settings"] if tier_decision else {},
    }


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
