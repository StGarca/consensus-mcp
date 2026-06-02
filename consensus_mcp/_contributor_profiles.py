"""Contributor profile data foundation (v1.18.0).

Per converged-plan.yaml (iteration-v1180-contributor-design-2026-05-22):
**B-ROUTING + UNIVERSAL PROFILES.** Every built-in AI (claude/codex/gemini/kimi)
has a YAML *profile* - a pure-data description of how to detect, invoke, parse,
install, and authenticate one contributor. Profiles supply:

  * the wizard's selectable list,
  * detect+guide install/auth strings (printed, never executed),
  * model/provenance labels sealed into the T6 review (fixes the parent kimi
    wrapper's ``gemini-2.5-pro`` mislabel), and
  * forward-compat docs for a future option-A dispatcher refactor.

This module is the **data layer ONLY**: it loads the packaged built-ins, merges
operator overrides from ``contributors.profiles``, and validates the schema. It
does NOT dispatch (``ProfileAdapter`` + ``_engine_factory`` own routing), does
NOT run the wizard, and does NOT touch the network.

Dispatch routing note (B-routing): the codex/gemini built-in *adapters* stay
unchanged; their profiles here are METADATA-ONLY (wizard/detect/provenance).
Only ``kimi`` (and future user-added cli_reviewers) is consumed by
``ProfileAdapter`` in v1.18.0.

Schema (converged-plan ``decision.profile_schema``)::

    name: str                 # contributor id; matches the yaml stem / dict key
    kind: host | cli_reviewer # claude=host (in-process); others=cli_reviewer
    model: str                # label sealed into dispatch_provenance.model
    detect: {command: str}    # binary resolved via shutil.which (cli_reviewer)
    invoke:
      transport: stdin | flag
      base_args: [str]
      prompt_flag: str|null   # transport=flag -> the flag; stdin -> null
      workdir_flag: str       # optional, e.g. -w
      model_flag: str         # optional, e.g. --model
    env: {KEY: str}           # injected into the subprocess
    output:
      strip_patterns: [regex] # chrome removed before JSON parse
      schema_enforced: bool   # codex enforces JSON natively; gemini/kimi do not
    sealed_filename: str      # default <name>-review.yaml
    id_prefix: str            # rev-id prefix, e.g. codex-rev / kimi-rev
    install: {windows, linux, darwin}   # detect+guide ONLY, never executed
    auth: {command, env_vars: [str], note}
    instructions: {filename}  # per-AI convention file (CLAUDE.md/AGENTS.md/...)
    timeout_seconds: int      # default 1800
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

# === Schema vocabulary ===
KIND_HOST = "host"
KIND_CLI_REVIEWER = "cli_reviewer"
# v1.20.0: a same-family blind SWE-reviewer run via a dedicated host review
# callback (no CLI). Like KIND_HOST it has no detect/invoke/output; it
# additionally REQUIRES `family` + `role` and is supplementary +
# gate-ineligible by contract.
KIND_HOST_PEER = "host_peer"
VALID_KINDS = {KIND_HOST, KIND_CLI_REVIEWER, KIND_HOST_PEER}

TRANSPORT_STDIN = "stdin"
TRANSPORT_FLAG = "flag"
VALID_TRANSPORTS = {TRANSPORT_STDIN, TRANSPORT_FLAG}

# Keys allowed in an install map. Mirrors the os names sys.platform maps onto
# (win32->windows, linux->linux, darwin->darwin) per detect+guide.
VALID_INSTALL_OS_KEYS = {"windows", "linux", "darwin"}

# Directory holding the packaged built-in profile YAMLs. Resolved relative to
# THIS module's __file__ so it works both source-tree and pip-installed
# (mirrors the dispatch_templates resolution in _dispatch_codex/_dispatch_gemini).
_PROFILE_DIR = Path(__file__).parent / "contributor_profiles"


def load_builtin_profiles() -> dict:
    """Load every ``consensus_mcp/contributor_profiles/*.yaml`` built-in profile.

    Returns a dict keyed by the profile's ``name`` field (which equals the yaml
    file stem for the built-ins). Resolves the directory relative to this
    module's ``__file__`` so it works when pip-installed.

    Raises:
      ValueError if a profile lacks a ``name`` or two profiles collide on name.
    """
    profiles: dict = {}
    for path in sorted(_PROFILE_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(
                f"built-in profile {path.name} must be a YAML mapping, "
                f"got {type(data).__name__}"
            )
        name = data.get("name")
        if not name:
            raise ValueError(f"built-in profile {path.name} is missing 'name'")
        if name in profiles:
            raise ValueError(
                f"duplicate built-in profile name {name!r} (file {path.name})"
            )
        profiles[name] = data
    return profiles


def merge_profiles(builtin: dict, config_profiles: dict) -> dict:
    """Overlay ``config_profiles`` onto ``builtin``.

    Config overrides a built-in by the same name (whole-profile replacement,
    consistent with config's list-replace semantics) and may add new names.
    Neither input is mutated.

    Args:
      builtin: the packaged built-in profiles (from ``load_builtin_profiles``).
      config_profiles: the operator's ``contributors.profiles`` map (may be
        empty or None).

    Returns:
      A new merged dict.
    """
    merged = deepcopy(builtin)
    if not config_profiles:
        return merged
    if not isinstance(config_profiles, dict):
        raise ValueError(
            f"contributors.profiles must be a mapping, "
            f"got {type(config_profiles).__name__}"
        )
    for name, prof in config_profiles.items():
        merged[name] = deepcopy(prof)
    return merged


def validate_profile(name: str, d: dict) -> None:
    """Validate a single profile dict. Raises ``ValueError`` on any violation.

    Checks (per converged-plan ``decision.profile_schema``):
      * ``d`` is a mapping with a non-empty ``name`` and a valid ``kind``.
      * ``kind == cli_reviewer`` additionally requires ``detect.command``,
        ``invoke.transport`` (valid enum), and ``output``.
      * ``invoke.transport == flag`` requires a non-null ``invoke.prompt_flag``.
      * ``invoke.transport == stdin`` requires ``prompt_flag`` null/absent.
      * any ``install`` map's keys must be a subset of {windows, linux, darwin}.
      * ``kind == host`` (claude) need not carry detect/invoke/output.
    """
    if not isinstance(d, dict):
        raise ValueError(
            f"profile {name!r} must be a mapping, got {type(d).__name__}"
        )

    # --- name ---
    pname = d.get("name")
    if not pname or not isinstance(pname, str):
        raise ValueError(f"profile {name!r} missing required field 'name'")

    # --- kind ---
    kind = d.get("kind")
    if kind not in VALID_KINDS:
        raise ValueError(
            f"profile {name!r} has invalid kind {kind!r}; "
            f"must be one of {sorted(VALID_KINDS)}"
        )

    # --- install map (any kind may declare it; detect+guide only) ---
    install = d.get("install")
    if install is not None:
        if not isinstance(install, dict):
            raise ValueError(
                f"profile {name!r} 'install' must be a mapping, "
                f"got {type(install).__name__}"
            )
        bad_keys = set(install) - VALID_INSTALL_OS_KEYS
        if bad_keys:
            raise ValueError(
                f"profile {name!r} 'install' has invalid OS key(s) "
                f"{sorted(bad_keys)}; allowed: {sorted(VALID_INSTALL_OS_KEYS)}"
            )

    # host (claude) needs no detect/invoke/output - it is the in-process env.
    if kind == KIND_HOST:
        return

    # host_peer (v1.20.0): a same-family blind SWE-reviewer run via a dedicated
    # host review callback. Like host it has NO CLI detect/invoke/output, but it
    # REQUIRES `family` + `role`. `weight` and `gate_eligible` are allowed
    # (optional) - the adapter stamps the canonical supplementary /
    # gate_eligible=False provenance regardless, so the profile fields are
    # advisory metadata only.
    if kind == KIND_HOST_PEER:
        family = d.get("family")
        if not family or not isinstance(family, str):
            raise ValueError(
                f"profile {name!r} (kind=host_peer) missing required field 'family'"
            )
        role = d.get("role")
        if not role or not isinstance(role, str):
            raise ValueError(
                f"profile {name!r} (kind=host_peer) missing required field 'role'"
            )
        gate_eligible = d.get("gate_eligible")
        if gate_eligible is not None and not isinstance(gate_eligible, bool):
            raise ValueError(
                f"profile {name!r} (kind=host_peer) 'gate_eligible' must be a "
                f"boolean when present, got {type(gate_eligible).__name__}"
            )
        return

    # === cli_reviewer-specific required fields ===
    detect = d.get("detect")
    if not isinstance(detect, dict) or not detect.get("command"):
        raise ValueError(
            f"profile {name!r} (kind=cli_reviewer) missing required "
            f"'detect.command'"
        )

    invoke = d.get("invoke")
    if not isinstance(invoke, dict):
        raise ValueError(
            f"profile {name!r} (kind=cli_reviewer) missing required 'invoke'"
        )
    transport = invoke.get("transport")
    if transport not in VALID_TRANSPORTS:
        raise ValueError(
            f"profile {name!r} has invalid invoke.transport {transport!r}; "
            f"must be one of {sorted(VALID_TRANSPORTS)}"
        )

    if "output" not in d or not isinstance(d.get("output"), dict):
        raise ValueError(
            f"profile {name!r} (kind=cli_reviewer) missing required 'output'"
        )

    # transport / prompt_flag consistency
    prompt_flag = invoke.get("prompt_flag")
    if transport == TRANSPORT_FLAG and not prompt_flag:
        raise ValueError(
            f"profile {name!r} invoke.transport=flag requires a non-null "
            f"invoke.prompt_flag"
        )
    if transport == TRANSPORT_STDIN and prompt_flag is not None:
        raise ValueError(
            f"profile {name!r} invoke.transport=stdin requires "
            f"invoke.prompt_flag to be null/absent; got {prompt_flag!r}"
        )


# === v1.20.1 profile-kind helpers ===

def resolve_kind(name: str, profiles: dict) -> str | None:
    """Return the kind of an enabled contributor name, or None if it has no
    profile (an unknown/open contributor). Never raises."""
    p = profiles.get(name)
    return p.get("kind") if isinstance(p, dict) else None


def independent_count(enabled: list[str], profiles: dict) -> int:
    """Count INDEPENDENT contributors: everything except a known host_peer.
    Unknown names (no profile) count as independent - config.py keeps its open
    model; constructibility stays engine_factory's fail-closed job."""
    return sum(1 for n in enabled if resolve_kind(n, profiles) != KIND_HOST_PEER)


def host_family(name: str, profiles: dict) -> str | None:
    """The host-family key a name belongs to. host_peer -> its `family`; host ->
    its explicit `family` or, by convention, its own name; anything else -> None."""
    p = profiles.get(name)
    if not isinstance(p, dict):
        return None
    kind = p.get("kind")
    if kind == KIND_HOST_PEER:
        return p.get("family")
    if kind == KIND_HOST:
        return p.get("family") or p.get("name") or name
    return None


def matching_host_peers(host_name: str, profiles: dict) -> list[str]:
    """host_peer profile names whose `family` matches host_name's family.
    Sorted for determinism. Empty if host_name is not a host or has no peers."""
    fam = host_family(host_name, profiles)
    if fam is None:
        return []
    return sorted(
        n for n, p in profiles.items()
        if isinstance(p, dict)
        and p.get("kind") == KIND_HOST_PEER
        and p.get("family") == fam
    )


def orphan_host_peers(enabled: list[str], profiles: dict) -> list[str]:
    """Enabled host_peers whose host family is NOT also enabled (a host_peer
    requires its host). Order follows `enabled`."""
    enabled_host_families = {
        host_family(n, profiles)
        for n in enabled
        if resolve_kind(n, profiles) == KIND_HOST
    }
    return [
        n for n in enabled
        if resolve_kind(n, profiles) == KIND_HOST_PEER
        and (profiles.get(n) or {}).get("family") not in enabled_host_families
    ]
