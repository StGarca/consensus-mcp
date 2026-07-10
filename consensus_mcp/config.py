"""Per-project `.consensus/config.yaml` schema + loader + validator.

Per iter-0015 converged design (workflow #4, all three contributors agreed):
this module is the FIRST sub-component of the iter-0016 implementation
delivering the configurable workflow engine. It provides:

  * Schema constants (workflow modes, independence models, convergence rules,
    finding dispositions, snapshot triggers, patch authoring, timeout policies)
  * `ConfigValidationError` - raised on illegal config or invalid combinations
  * `load(path)` - parse YAML, normalize aliases + defaults, validate, return
    the effective config dict
  * `validate(config)` - pure check on an already-loaded dict
  * `normalize(config)` - apply default values + alias resolution (e.g.
    `workflow.mode: 3` -> `workflow.mode: post-review`)
  * `default_config()` - return the canonical default config dict
  * `effective_config_sha256(config)` - deterministic hash for sealed provenance

This module does NOT load files into the engine, does NOT dispatch
contributors, does NOT run the wizard. Those land in iter-0016b/c/d.

Schema version 1 lifecycle: schema_version=0 is the synthetic legacy mode
(no `.consensus/config.yaml`); schema_version=1 is what iter-0016 ships.
Future bumps add an in-place migration command.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


SCHEMA_VERSION = 1

# === Workflow modes (semantic strings; CLI accepts numeric aliases 3/4) ===
WORKFLOW_POST_REVIEW = "post-review"
WORKFLOW_PROPOSE_CONVERGE = "propose-converge"
WORKFLOW_ADVISORY = "advisory"
# iter-workflow-abc-introduce: Workflow C - autonomous-execute. v1.14.4
# ships the contract (alias, validators, scope_check helper, schema);
# multi-iteration engine path is named-blocker for v1.15.0.
WORKFLOW_AUTONOMOUS_EXECUTE = "autonomous-execute"
WORKFLOW_ARCHITECT_BUILD = "architect-build"
VALID_WORKFLOWS = {
    WORKFLOW_POST_REVIEW,
    WORKFLOW_PROPOSE_CONVERGE,
    WORKFLOW_ADVISORY,
    WORKFLOW_AUTONOMOUS_EXECUTE,
    WORKFLOW_ARCHITECT_BUILD,
}

# Operator-facing aliases. iter-workflow-abc-introduce: letter aliases
# replace numeric as the canonical operator vocabulary; numeric aliases
# (3, 4) stay accepted for one release cycle with DeprecationWarning,
# then removed in a future minor.
WORKFLOW_ALIASES = {
    # Letter aliases (canonical operator vocabulary as of v1.14.4)
    "A": WORKFLOW_PROPOSE_CONVERGE,
    "B": WORKFLOW_POST_REVIEW,
    "C": WORKFLOW_AUTONOMOUS_EXECUTE,
    "D": WORKFLOW_ARCHITECT_BUILD,
    "a": WORKFLOW_PROPOSE_CONVERGE,
    "b": WORKFLOW_POST_REVIEW,
    "c": WORKFLOW_AUTONOMOUS_EXECUTE,
    "d": WORKFLOW_ARCHITECT_BUILD,
    # Numeric aliases (deprecated; emit DeprecationWarning when resolved)
    "3": WORKFLOW_POST_REVIEW,
    "4": WORKFLOW_PROPOSE_CONVERGE,
    3: WORKFLOW_POST_REVIEW,
    4: WORKFLOW_PROPOSE_CONVERGE,
    # Semantic strings pass through unchanged in normalize.
}
_DEPRECATED_NUMERIC_WORKFLOW_ALIASES = {"3", "4", 3, 4}

# === Independence models ===
INDEPENDENCE_BLIND = "blind-first-reveal"
INDEPENDENCE_VISIBLE = "visible-from-start"
INDEPENDENCE_SEQUENTIAL = "sequential"
VALID_INDEPENDENCE = {INDEPENDENCE_BLIND, INDEPENDENCE_VISIBLE, INDEPENDENCE_SEQUENTIAL}

# === Convergence rules ===
CONVERGE_UNANIMOUS = "unanimous"
CONVERGE_STRICT_MAJ = "strict-majority"
CONVERGE_INCL_MAJ = "inclusive-majority"
CONVERGE_ADVISORY = "advisory"
VALID_CONVERGENCE = {CONVERGE_UNANIMOUS, CONVERGE_STRICT_MAJ, CONVERGE_INCL_MAJ, CONVERGE_ADVISORY}

# === Finding disposition ===
DISPOSITION_ALL_OR_NOTHING = "all-or-nothing"
DISPOSITION_PER_FINDING = "per-finding"
DISPOSITION_WEIGHTED_SYNTHESIS = "weighted-synthesis"
VALID_DISPOSITION = {
    DISPOSITION_ALL_OR_NOTHING,
    DISPOSITION_PER_FINDING,
    DISPOSITION_WEIGHTED_SYNTHESIS,
}
# iter-three-gaps: workflow #4 (propose-converge) accepts only the two
# plan-shaped dispositions. per-finding is post-review semantics.
VALID_DISPOSITION_FOR_PROPOSE_CONVERGE = {
    DISPOSITION_ALL_OR_NOTHING,
    DISPOSITION_WEIGHTED_SYNTHESIS,
}

# === Converged-plan convention enforcement (v1.15.1) ===
# Machine-enforcement level for the v1.15.0 converged-plan convention
# blocks (falsification / independent_safeguard /
# decisive_experiment_before_next_iteration). Default `graduated`:
# hard-reject ONLY (i) operator-declared safety/data-loss/bricking/
# irreversible risk class missing a conforming independent_safeguard
# and (ii) empirical_status:proven with no recorded experiment; warn +
# annotate otherwise. See consensus_mcp.validators.validate_converged_plan
# and docs/workflows/converged-plan-convention.md.
ENFORCEMENT_OFF = "off"
ENFORCEMENT_WARN = "warn"
ENFORCEMENT_GRADUATED = "graduated"
ENFORCEMENT_STRICT = "strict"
VALID_CONVERGED_PLAN_ENFORCEMENT = {
    ENFORCEMENT_OFF,
    ENFORCEMENT_WARN,
    ENFORCEMENT_GRADUATED,
    ENFORCEMENT_STRICT,
}

# === Snapshot triggers ===
SNAPSHOT_MANUAL = "manual-only"
SNAPSHOT_ON_CLOSE = "on-iteration-close"
SNAPSHOT_PERIODIC = "periodic"
VALID_SNAPSHOT_TRIGGER = {SNAPSHOT_MANUAL, SNAPSHOT_ON_CLOSE, SNAPSHOT_PERIODIC}

# === Patch authoring ===
PATCH_CLAUDE_ONLY = "claude-only"
PATCH_ANY = "any-contributor"
PATCH_NONE = "none"
VALID_PATCH_AUTHORING = {PATCH_CLAUDE_ONLY, PATCH_ANY, PATCH_NONE}

# === Timeout policy ===
TIMEOUT_NO_VOTE = "treat-as-no-vote"
TIMEOUT_BLOCKING = "treat-as-blocking"
TIMEOUT_SHRINK = "shrink-quorum"
VALID_TIMEOUT_POLICY = {TIMEOUT_NO_VOTE, TIMEOUT_BLOCKING, TIMEOUT_SHRINK}

# === Allowed contributor identities (v1 closed enum per converged plan SO-5) ===
# kimi added 2026-05-22: it is a real default contributor; excluding it from the
# allow-list (a) made `validate()` reject any project that configures kimi and
# (b) made the anchoring linter blind to kimi-anchoring - the exact bias it was
# built to catch. (Found by independent QA, not self-review.) Note: enabling
# kimi still requires a KimiAdapter in the engine (tracked separately); this
# allow-list entry just stops kimi being a second-class identity.
KNOWN_CONTRIBUTORS = ("claude", "codex", "gemini", "grok", "kimi")
CLAUDE = "claude"


class ConfigValidationError(ValueError):
    """Raised on schema violation or illegal config combination."""


def _default_independent_enabled() -> list[str]:
    """Derive the default enabled set from built-in INDEPENDENT profiles (decision 7).

    Lazy import avoids any import-cycle risk with _contributor_profiles.
    host_peer profiles (e.g. claude-swe-reviewer) are excluded - they are
    opt-in overlays, not independent contributors.
    """
    from consensus_mcp._contributor_profiles import (  # noqa: PLC0415
        KIND_HOST_PEER,
        load_builtin_profiles,
    )

    profiles = load_builtin_profiles()
    return sorted(
        name for name, p in profiles.items()
        if isinstance(p, dict) and p.get("kind") != KIND_HOST_PEER
    )


def default_config() -> dict:
    """Return the canonical default `.consensus/config.yaml` structure.

    The enabled contributor list derives dynamically from built-in independent
    profiles (decision 7: no hardcoded AI list). Adding a new built-in profile
    automatically extends this default.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "project": {
            "name": None,
            "config_created_at_utc": None,
        },
        "workflow": {
            "mode": WORKFLOW_PROPOSE_CONVERGE,
            "independence": INDEPENDENCE_BLIND,
            "max_convergence_rounds": 3,
            "timeout_policy": TIMEOUT_NO_VOTE,
        },
        "contributors": {
            "enabled": _default_independent_enabled(),  # decision 7: dynamic, not hardcoded
            "adapters": {
                "claude": {
                    "role": "orchestrator",
                    "model": "claude-fable-5",
                    "effort": "low",
                    "can_propose": True,
                    "can_review": True,
                    "can_converge": True,
                },
                "codex": {
                    "command": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "low",
                    "sandbox_mode": "read-only",
                    "can_propose": True,
                    "can_review": True,
                    "can_converge": True,
                },
                "gemini": {
                    "command": "agy",
                    "model": "Gemini 3.5 Flash (Medium)",
                    "approval_mode": "plan",
                    "can_propose": True,
                    "can_review": True,
                    "can_converge": True,
                },
                "grok": {
                    "command": "grok",
                    "model": "grok-4.5",
                    "effort": "medium",
                    "can_propose": True,
                    "can_review": True,
                    "can_converge": True,
                },
                "kimi": {
                    "command": "kimi",
                    "effort": "medium",
                    "can_propose": True,
                    "can_review": True,
                    "can_converge": True,
                },
            },
        },
        "convergence": {
            "rule": CONVERGE_STRICT_MAJ,
            "finding_disposition": DISPOSITION_WEIGHTED_SYNTHESIS,
            "converged_plan_enforcement": ENFORCEMENT_GRADUATED,
        },
        "patches": {
            "authoring": PATCH_CLAUDE_ONLY,
            "max_patch_lines": 600,
        },
        "snapshots": {
            "trigger": SNAPSHOT_ON_CLOSE,
            "periodic": {
                "every_iterations": None,
                "every_minutes": None,
            },
            "branch": "consensus-state-snapshots",
            "retention": "unbounded",
        },
        "artifacts": {
            "root": "consensus-state",
            "seal_outputs": True,
        },
        "defaults": {
            "iteration_timeout_seconds": 600,
            "stall_silence_seconds": 180,
            "pre_first_byte_silence_seconds": 600,
        },
        "architect_loop": {
            "max_cycles": 8,
            "verification": "",
            "lane_branch_prefix": "arch-lane/",
            "max_wall_clock_minutes": 0,
        },
    }


def _deep_merge_defaults(base: dict, overrides: dict) -> dict:
    """Merge overrides into base (deep, override wins for scalars; lists replaced)."""
    out = deepcopy(base)
    for k, v in overrides.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge_defaults(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def normalize(config: dict) -> dict:
    """Apply defaults + alias resolution. Returns a NEW dict; does not mutate input.

    Currently handles:
      - `workflow.mode` aliases (A -> propose-converge, B -> post-review,
        C -> autonomous-execute; numeric 3/4 deprecated but still resolved)
      - Filling unspecified keys with defaults from `default_config()`

    Does NOT validate - see `validate()` for that.
    """
    import warnings
    if not isinstance(config, dict):
        raise ConfigValidationError(
            f"config root must be a mapping, got {type(config).__name__}"
        )
    normalized = _deep_merge_defaults(default_config(), config)
    # Resolve workflow.mode aliases. Emit DeprecationWarning for numeric
    # aliases (kept for backward compat for one release cycle per
    # iter-workflow-abc-introduce convergence; will be removed in a
    # future minor).
    mode = normalized.get("workflow", {}).get("mode")
    if mode in WORKFLOW_ALIASES:
        if mode in _DEPRECATED_NUMERIC_WORKFLOW_ALIASES:
            warnings.warn(
                f"workflow.mode numeric alias {mode!r} is deprecated; "
                f"use letter alias 'A' (propose-converge), 'B' (post-review), "
                f"or 'C' (autonomous-execute) instead. Numeric aliases will "
                f"be removed in a future minor release.",
                DeprecationWarning,
                stacklevel=2,
            )
        normalized["workflow"]["mode"] = WORKFLOW_ALIASES[mode]
    return normalized


def _contributor_family(name: str, profiles: dict) -> str:
    prof = profiles.get(name) or {}
    fam = prof.get("family")
    return str(fam) if fam else str(name)


def _validate_architect_build(config: dict) -> None:
    """Mode-conditional rules for workflow.mode=architect-build (workflow D).

    Enforced at CONFIG time per the 2026-06-10 consult (Q2): a roles map with
    no cross-family signer vs the builder is rejected at start, never at
    delivery.
    """
    from consensus_mcp import _contributor_profiles as _profiles

    roles = config.get("roles")
    if not isinstance(roles, dict):
        raise ConfigValidationError(
            "workflow.mode=architect-build requires a top-level roles: block "
            "mapping architect/builder/reviewer to enabled contributors; "
            "got none. Add roles: {architect: <name>, builder: <name>, "
            "reviewer: <name>}."
        )
    required = ("architect", "builder", "reviewer")
    for key in required:
        if not isinstance(roles.get(key), str) or not roles[key].strip():
            raise ConfigValidationError(
                f"roles.{key} is required for architect-build (reviewer is "
                f"REQUIRED in v1 per the 2026-06-10 consult Q4); got "
                f"{roles.get(key)!r}."
            )
    extra = sorted(set(roles) - set(required))
    if extra:
        raise ConfigValidationError(
            f"roles: block has unknown keys {extra}; only "
            f"architect/builder/reviewer are recognized in v1."
        )
    enabled = config.get("contributors", {}).get("enabled", [])
    for key in required:
        if roles[key] not in enabled:
            raise ConfigValidationError(
                f"roles.{key}={roles[key]!r} is not in contributors.enabled "
                f"{enabled}; every role must name an enabled contributor."
            )

    merged = _profiles.merge_profiles(
        _profiles.load_builtin_profiles(),
        config.get("contributors", {}).get("profiles", {}) or {},
    )
    if not _profiles.resolve_builder_capable(roles["builder"], merged):
        raise ConfigValidationError(
            f"roles.builder={roles['builder']!r} is not builder_capable: the "
            f"profile must declare builder_capable: true (v1: only codex). "
            f"Write-enabled dispatch is never granted implicitly."
        )
    builder_fam = _contributor_family(roles["builder"], merged)
    signer_fams = {
        _contributor_family(roles["architect"], merged),
        _contributor_family(roles["reviewer"], merged),
    }
    if not (signer_fams - {builder_fam}):
        raise ConfigValidationError(
            f"architect-build requires at least one of roles.architect/"
            f"roles.reviewer to be a DIFFERENT model family than the builder "
            f"(cross-family floor, consult Q2); all three resolve to family "
            f"{builder_fam!r}. Map the architect or reviewer to another "
            f"family."
        )

    loop = config.get("architect_loop", {})
    mc = loop.get("max_cycles")
    if not isinstance(mc, int) or isinstance(mc, bool) or mc < 1:
        raise ConfigValidationError(
            f"architect_loop.max_cycles must be an integer >= 1; got {mc!r}."
        )
    wc = loop.get("max_wall_clock_minutes", 0)
    if not isinstance(wc, int) or isinstance(wc, bool) or wc < 0:
        raise ConfigValidationError(
            f"architect_loop.max_wall_clock_minutes must be an integer >= 0 "
            f"(0 disables); got {wc!r}."
        )
    if not isinstance(loop.get("verification", ""), str):
        raise ConfigValidationError(
            f"architect_loop.verification must be a string command (empty "
            f"string disables the frozen gate); got "
            f"{loop.get('verification')!r}."
        )
    # Full git check-ref-format hardening is owned by the lane layer; this
    # contract check only rejects the obvious path-traversal shapes.
    prefix = loop.get("lane_branch_prefix", "")
    if (
        not isinstance(prefix, str)
        or not prefix.strip()
        or "\\" in prefix
        or ".." in prefix
        or prefix.count("/") > 1
        or ("/" in prefix and not prefix.endswith("/"))
    ):
        raise ConfigValidationError(
            f"architect_loop.lane_branch_prefix must be a non-empty branch "
            f"prefix with at most one trailing '/' (e.g. 'arch-lane/'); got "
            f"{prefix!r}."
        )


def validate(config: dict) -> None:
    """Validate a (preferably normalized) config dict. Raises ConfigValidationError.

    Per converged-plan.yaml Section B validation rules.
    """
    if not isinstance(config, dict):
        raise ConfigValidationError(
            f"config root must be a mapping, got {type(config).__name__}"
        )

    # === schema_version ===
    sv = config.get("schema_version")
    if sv != SCHEMA_VERSION:
        raise ConfigValidationError(
            f"schema_version must be {SCHEMA_VERSION}, got {sv!r}"
        )

    # === workflow ===
    workflow = config.get("workflow", {})
    if not isinstance(workflow, dict):
        raise ConfigValidationError("'workflow' must be a mapping")
    mode = workflow.get("mode")
    if mode not in VALID_WORKFLOWS:
        raise ConfigValidationError(
            f"workflow.mode={mode!r} not in {sorted(VALID_WORKFLOWS)}"
        )
    independence = workflow.get("independence")
    if independence not in VALID_INDEPENDENCE:
        raise ConfigValidationError(
            f"workflow.independence={independence!r} not in {sorted(VALID_INDEPENDENCE)}"
        )
    timeout_policy = workflow.get("timeout_policy", TIMEOUT_NO_VOTE)
    if timeout_policy not in VALID_TIMEOUT_POLICY:
        raise ConfigValidationError(
            f"workflow.timeout_policy={timeout_policy!r} not in {sorted(VALID_TIMEOUT_POLICY)}"
        )
    max_rounds = workflow.get("max_convergence_rounds", 3)
    if not isinstance(max_rounds, int) or max_rounds < 1:
        raise ConfigValidationError(
            f"workflow.max_convergence_rounds must be positive int, got {max_rounds!r}"
        )

    # === contributors ===
    contributors = config.get("contributors", {})
    if not isinstance(contributors, dict):
        raise ConfigValidationError("'contributors' must be a mapping")
    enabled = contributors.get("enabled", [])
    if not isinstance(enabled, list) or not enabled:
        raise ConfigValidationError(
            "contributors.enabled must be a non-empty list"
        )
    if len(set(enabled)) != len(enabled):
        raise ConfigValidationError(
            f"contributors.enabled must be unique; got {enabled!r}"
        )
    # OPEN contributor set (2026-05-22, "2-or-20-or-200 AIs" acceptance):
    # validation is STRUCTURAL only - it no longer rejects names outside a closed
    # enum, so a clean install supports ANY number of contributors with ANY
    # names (min-2 / max-N). Whether a name is actually CONSTRUCTIBLE is checked
    # at build time by engine_factory (fail-closed with a register_contributor
    # hint), which is the only layer that knows the open adapter registry.
    # Names must still be non-empty strings.
    for c in enabled:
        if not isinstance(c, str) or not c.strip():
            raise ConfigValidationError(
                f"contributors.enabled entries must be non-empty, non-whitespace "
                f"strings; got {c!r}"
            )
    # NOTE: min-2 is NOT blanket-enforced here - it is MODE-SPECIFIC below
    # (propose-converge / sequential / strict-majority each require >=2), so
    # single-contributor modes (e.g. solo-claude post-review) stay valid. There
    # is NO upper cap on N anywhere.
    # v1.18.0 (open-contributor model - converged plan): claude is OPTIONAL (the
    # host orchestrates the loop regardless of whether claude is itself a
    # contributor) and a per-contributor `contributors.adapters` entry is
    # OPTIONAL. Whether a name is actually CONSTRUCTIBLE - a built-in class, a
    # registered adapter, or a kind:cli_reviewer profile - is the engine_factory's
    # fail-closed job (see the STRUCTURAL-only note above), NOT config.validate's.
    # We keep only the adapters-is-a-mapping type check.
    adapters = contributors.get("adapters", {})
    if not isinstance(adapters, dict):
        raise ConfigValidationError("contributors.adapters must be a mapping")

    # === contributors.profiles (v1.18.0 - optional operator overlay) ===
    # Validate each operator-supplied profile against the contributor-profile
    # schema (loader/validator lives in _contributor_profiles). config overrides
    # built-in profiles by same name; this block only checks the OVERLAY supplied
    # in config - built-in profiles are validated by their own test suite. Plain
    # ValueErrors from validate_profile are re-raised as ConfigValidationError to
    # keep the config-layer error type consistent.
    profiles = contributors.get("profiles")
    if profiles is not None:
        if not isinstance(profiles, dict):
            raise ConfigValidationError(
                f"contributors.profiles must be a mapping, "
                f"got {type(profiles).__name__}"
            )
        from consensus_mcp._contributor_profiles import validate_profile
        for pname, pdef in profiles.items():
            try:
                validate_profile(pname, pdef)
            except ValueError as exc:
                raise ConfigValidationError(
                    f"contributors.profiles.{pname}: {exc}"
                ) from exc

    # === convergence ===
    convergence = config.get("convergence", {})
    if not isinstance(convergence, dict):
        raise ConfigValidationError("'convergence' must be a mapping")
    rule = convergence.get("rule")
    if rule not in VALID_CONVERGENCE:
        raise ConfigValidationError(
            f"convergence.rule={rule!r} not in {sorted(VALID_CONVERGENCE)}"
        )
    disposition = convergence.get("finding_disposition")
    if disposition not in VALID_DISPOSITION:
        raise ConfigValidationError(
            f"convergence.finding_disposition={disposition!r} not in {sorted(VALID_DISPOSITION)}"
        )
    # v1.15.1: converged-plan convention machine-enforcement level.
    cpe = convergence.get("converged_plan_enforcement", ENFORCEMENT_GRADUATED)
    if cpe not in VALID_CONVERGED_PLAN_ENFORCEMENT:
        raise ConfigValidationError(
            f"convergence.converged_plan_enforcement={cpe!r} not in "
            f"{sorted(VALID_CONVERGED_PLAN_ENFORCEMENT)}"
        )

    # === Cross-validation rules per converged-plan.yaml ===
    # Floor is INDEPENDENT count (host_peer is a 0.5 supplemental, never a vote).
    # Resolve kinds via merged built-in + overlay profiles; unknown names count
    # as independent (open-contributor model). Keep the helper small - do NOT
    # import wizard code here.
    from consensus_mcp._contributor_profiles import (
        load_builtin_profiles,
        merge_profiles,
        independent_count,
        orphan_host_peers,
    )
    _merged = merge_profiles(load_builtin_profiles(), contributors.get("profiles") or {})
    n_independent = independent_count(enabled, _merged)

    # Rule: a host_peer may be enabled only if its host family is also enabled.
    _orphans = orphan_host_peers(enabled, _merged)
    if _orphans:
        raise ConfigValidationError(
            f"contributors.enabled has orphan supplemental reviewer(s) {_orphans!r}: "
            f"a host_peer (same-model supplemental) requires its host to also be "
            f"enabled. Add the host or remove the supplemental."
        )

    # Rule: workflow.mode=propose-converge requires N>=2 independent contributors
    if mode == WORKFLOW_PROPOSE_CONVERGE and n_independent < 2:
        raise ConfigValidationError(
            f"workflow.mode=propose-converge requires at least 2 independent "
            f"contributors (a same-model supplemental does not count); "
            f"got {n_independent} independent ({enabled!r}). "
            f"Use post-review or advisory for solo setups."
        )

    # Rule: workflow.mode=autonomous-execute requires N==3 independent contributors
    # (iter-workflow-abc-introduce safety floor: autonomous mode runs
    # without operator-in-the-loop, so the wide cross-AI safety net is
    # mandatory; v1.15.0+ may relax with explicit operator opt-in).
    if mode == WORKFLOW_AUTONOMOUS_EXECUTE and n_independent != 3:
        raise ConfigValidationError(
            f"workflow.mode=autonomous-execute requires exactly 3 independent "
            f"contributors (a same-model supplemental does not count) "
            f"for the wide cross-AI safety net "
            f"required by autonomous runs; got {n_independent} independent ({enabled!r}). "
            f"Use propose-converge for 2-AI setups."
        )

    if mode == WORKFLOW_ARCHITECT_BUILD:
        _validate_architect_build(config)
    elif "roles" in config:
        raise ConfigValidationError(
            f"a top-level roles: block is only legal when workflow.mode="
            f"architect-build; current mode is {mode!r}. Remove roles: or "
            f"switch the mode."
        )

    # Rule: workflow.mode=propose-converge accepts the two plan-shaped
    # dispositions only (iter-three-gaps doctrine: weighted-synthesis is the
    # default for workflow #4; all-or-nothing remains valid as explicit opt-in
    # for binary scope decisions / safety gates / compliance verdicts).
    # per-finding stays post-review-only (its semantics fit defect lists, not
    # plan synthesis).
    if mode == WORKFLOW_PROPOSE_CONVERGE and disposition not in VALID_DISPOSITION_FOR_PROPOSE_CONVERGE:
        raise ConfigValidationError(
            f"workflow.mode=propose-converge accepts "
            f"convergence.finding_disposition in "
            f"{sorted(VALID_DISPOSITION_FOR_PROPOSE_CONVERGE)!r}; "
            f"got {disposition!r}. per-finding is post-review semantics; "
            f"use weighted-synthesis (default) or all-or-nothing for workflow #4."
        )

    # Rule: advisory mode requires advisory convergence rule
    if mode == WORKFLOW_ADVISORY and rule != CONVERGE_ADVISORY:
        raise ConfigValidationError(
            f"workflow.mode=advisory requires convergence.rule=advisory; got {rule!r}"
        )
    # And vice versa: advisory rule only valid in advisory mode
    if rule == CONVERGE_ADVISORY and mode != WORKFLOW_ADVISORY:
        raise ConfigValidationError(
            f"convergence.rule=advisory valid only when workflow.mode=advisory; "
            f"got workflow.mode={mode!r}"
        )

    # Rule: strict-majority with N=1 independent is invalid
    if rule == CONVERGE_STRICT_MAJ and n_independent == 1:
        raise ConfigValidationError(
            "convergence.rule=strict-majority is invalid with only 1 independent "
            "contributor (a same-model supplemental does not count); "
            "use unanimous or advisory"
        )

    # Rule: sequential independence requires N>=2 independent contributors
    if independence == INDEPENDENCE_SEQUENTIAL and n_independent < 2:
        raise ConfigValidationError(
            f"workflow.independence=sequential requires at least 2 independent "
            f"contributors (a same-model supplemental does not count); "
            f"got {n_independent} independent"
        )

    # === patches ===
    patches = config.get("patches", {})
    if not isinstance(patches, dict):
        raise ConfigValidationError("'patches' must be a mapping")
    authoring = patches.get("authoring")
    if authoring not in VALID_PATCH_AUTHORING:
        raise ConfigValidationError(
            f"patches.authoring={authoring!r} not in {sorted(VALID_PATCH_AUTHORING)}"
        )
    max_patch_lines = patches.get("max_patch_lines", 600)
    if not isinstance(max_patch_lines, int) or max_patch_lines < 0:
        raise ConfigValidationError(
            f"patches.max_patch_lines must be non-negative int, got {max_patch_lines!r}"
        )

    # === snapshots ===
    snapshots = config.get("snapshots", {})
    if not isinstance(snapshots, dict):
        raise ConfigValidationError("'snapshots' must be a mapping")
    trigger = snapshots.get("trigger")
    if trigger not in VALID_SNAPSHOT_TRIGGER:
        raise ConfigValidationError(
            f"snapshots.trigger={trigger!r} not in {sorted(VALID_SNAPSHOT_TRIGGER)}"
        )
    periodic = snapshots.get("periodic", {})
    if not isinstance(periodic, dict):
        raise ConfigValidationError("snapshots.periodic must be a mapping")
    every_iters = periodic.get("every_iterations")
    every_mins = periodic.get("every_minutes")
    if trigger == SNAPSHOT_PERIODIC:
        if every_iters is None and every_mins is None:
            raise ConfigValidationError(
                "snapshots.trigger=periodic requires at least one of "
                "snapshots.periodic.every_iterations or every_minutes"
            )
        if every_iters is not None and (not isinstance(every_iters, int) or every_iters < 1):
            raise ConfigValidationError(
                f"snapshots.periodic.every_iterations must be positive int, got {every_iters!r}"
            )
        if every_mins is not None and (not isinstance(every_mins, int) or every_mins < 1):
            raise ConfigValidationError(
                f"snapshots.periodic.every_minutes must be positive int, got {every_mins!r}"
            )
    else:
        # Non-periodic trigger: both periodic fields must be null
        if every_iters is not None or every_mins is not None:
            raise ConfigValidationError(
                f"snapshots.trigger={trigger!r} requires "
                f"snapshots.periodic.every_iterations/every_minutes to be null; "
                f"got every_iterations={every_iters!r}, every_minutes={every_mins!r}"
            )


def load(path: Path) -> dict:
    """Load `.consensus/config.yaml` from path. Returns normalized + validated dict.

    Raises:
      ConfigValidationError on malformed YAML, missing schema_version, or
        illegal combination.
      OSError on file-read failure (caller decides legacy-mode fallback).
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigValidationError(f"malformed YAML in {path}: {exc}") from exc
    normalized = normalize(raw)
    validate(normalized)
    return normalized


def effective_config_sha256(config: dict) -> str:
    """Deterministic sha256 of a config dict (canonical YAML form).

    Used by the engine to stamp goal_packets with `config_sha256` so sealed
    artifacts can prove which config governed dispatch.
    """
    canonical = yaml.safe_dump(config, sort_keys=True, default_flow_style=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_legacy_mode_repo(repo_root: Path) -> bool:
    """True iff `.consensus/config.yaml` is absent at the repo root.

    Engine uses this to decide between loading config vs entering legacy mode.
    """
    return not (Path(repo_root) / ".consensus" / "config.yaml").is_file()


def synthesize_legacy_config(repo_root: Path) -> dict:
    """Return the synthetic schema_version=0 'legacy mode' config.

    Per converged-plan.yaml Section D: when `.consensus/config.yaml` is absent,
    the engine emulates pre-iter-0015 behavior. This helper returns the dict
    representing that behavior so the engine has a uniform config object to
    consume regardless of whether real config exists.

    Schema_version=0 is INTENTIONALLY not 1 - it's a sentinel marking legacy
    synthesis. validate() rejects it; the engine uses a separate code path.
    """
    return {
        "schema_version": 0,  # sentinel: legacy synthesis
        "project": {"name": Path(repo_root).name, "config_created_at_utc": None},
        "workflow": {
            "mode": WORKFLOW_POST_REVIEW,
            "independence": INDEPENDENCE_VISIBLE,
            "max_convergence_rounds": 1,
            "timeout_policy": TIMEOUT_NO_VOTE,
        },
        "contributors": {
            # Historical legacy list - intentional pre-iter-0015 state, NOT the
            # canonical default (which is dynamic via _default_independent_enabled).
            "enabled": ["claude", "codex"],
            "adapters": {
                "claude": {
                    "role": "orchestrator",
                    "model": "claude-fable-5",
                    "effort": "low",
                    "can_propose": True,
                    "can_review": True,
                    "can_converge": False,
                },
                "codex": {
                    "command": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "low",
                    "sandbox_mode": "read-only",
                    "can_propose": False,
                    "can_review": True,
                    "can_converge": False,
                },
            },
        },
        "convergence": {
            "rule": CONVERGE_UNANIMOUS,
            "finding_disposition": DISPOSITION_ALL_OR_NOTHING,
            "converged_plan_enforcement": ENFORCEMENT_GRADUATED,
        },
        "patches": {"authoring": PATCH_CLAUDE_ONLY, "max_patch_lines": 600},
        "snapshots": {
            "trigger": SNAPSHOT_MANUAL,
            "periodic": {"every_iterations": None, "every_minutes": None},
            "branch": "consensus-state-snapshots",
            "retention": "unbounded",
        },
        "artifacts": {"root": "consensus-state", "seal_outputs": True},
        "defaults": {
            "iteration_timeout_seconds": 600,
            "stall_silence_seconds": 180,
            "pre_first_byte_silence_seconds": 600,
        },
        "_legacy_mode_synthesis": True,
    }
