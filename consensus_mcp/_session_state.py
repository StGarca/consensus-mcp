"""Per-invocation gate activation state.

Converged consult iteration-v133-gate-scope-shift-2026-05-26 (5-AI
unanimous): the gate is DORMANT by default; ACTIVATES when an AI
explicitly invokes a consensus tool; DEACTIVATES on delivery-token
completion or explicit close.

This module owns the SESSION-STATE marker file
(`.consensus/session-active`). It is INTENTIONALLY thin:
  - Pure file operations (read / write / clear / probe).
  - Minimal validation (file shape + iteration_id resolves to a real
    unsealed iteration dir).
  - NO trust-root logic - that stays in `_design_approval`.

The session marker is NOT a trust artifact: it carries no SHA, no
signature, and its only role is to flip the gate's mode from dormant
to active. Trust-root validation continues to run via
`verify_design_approval` (re-validates the design_consensus_ref's
live seal, the cross-family count, and the canonical hash).

Why a separate file from `.consensus/design-approved`:
  - `design-approved` is the TRUST POINTER (long-lived; survives
    across sessions; re-validates the seal on every check).
  - `session-active` is the SESSION FLAG (ephemeral; created on
    consensus tool invocation; removed on close/delivery). Two
    different lifecycles -> two different files.

Schema (consult D2):
    schema_version: 1
    iteration_id: '<iter-id>'                  # must resolve in consensus-state/active/
    scope_glob: '<fnmatch glob>'                # for operator readability + provenance
    activated_by: '<reviewer_id or script>'    # who flipped the switch
    activated_at_utc: '<iso8601>'              # when
    activation_source: console_script | mcp_tool | env_override

Activation triggers (D3 - written as a side effect of):
  - `consensus-mcp-seal-iteration mint` (the primary trigger)
  - `consensus-mcp-dispatch-*` console-script invocations in proposal mode
  - consensus-mcp MCP server tool invocations

Deactivation triggers (D4):
  - `consensus-mcp-seal-iteration close` - explicit, with delivery-token check
  - `mint_delivery_token` - implicit (defense-in-depth) when the LAST in-scope
    file gets its token

Legacy mode (D6):
  - `.consensus/legacy-always-on` file OR `CONSENSUS_MCP_LEGACY_ALWAYS_ON=1`
    env var restores per-project always-active behavior. The
    `session_active()` probe consults legacy_mode_active() first.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path

import yaml

from consensus_mcp._atomic_io import atomic_write_text


SCHEMA_VERSION = 1
MARKER_RELPATH = Path(".consensus") / "session-active"
LEGACY_MARKER_RELPATH = Path(".consensus") / "legacy-always-on"
MIGRATION_WARNED_RELPATH = Path(".consensus") / ".migration-warned"
LEGACY_ENV_VAR = "CONSENSUS_MCP_LEGACY_ALWAYS_ON"

VALID_SOURCES = frozenset({
    "console_script",
    "mcp_tool",
    "env_override",
    "test_fixture",   # tests construct markers directly
})


def _marker_path(repo_root: Path) -> Path:
    return Path(repo_root) / MARKER_RELPATH


def _legacy_marker_path(repo_root: Path) -> Path:
    return Path(repo_root) / LEGACY_MARKER_RELPATH


def write_session_marker(
    repo_root: Path,
    iteration_id: str,
    scope_glob: str,
    activated_by: str,
    activation_source: str = "console_script",
) -> Path:
    """Atomically write the session-active marker.

    Refuses on invalid `activation_source`. Caller is responsible
    for ensuring the iteration_id resolves to a real iteration dir
    (the `session_active` probe re-validates on every read; a stale
    marker pointing at a non-existent iteration is treated as
    inactive, NOT as an error).
    """
    repo_root = Path(repo_root)
    if activation_source not in VALID_SOURCES:
        raise ValueError(
            f"activation_source {activation_source!r} not in {sorted(VALID_SOURCES)}"
        )
    if not iteration_id or "/" in iteration_id or "\\" in iteration_id or ".." in iteration_id:
        raise ValueError(f"iteration_id {iteration_id!r} is missing or unsafe")
    if not isinstance(scope_glob, str) or not scope_glob.strip():
        raise ValueError("scope_glob must be a non-empty string")

    marker = {
        "schema_version": SCHEMA_VERSION,
        "iteration_id": iteration_id,
        "scope_glob": scope_glob,
        "activated_by": activated_by,
        "activated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "activation_source": activation_source,
    }
    path = _marker_path(repo_root)
    # kimi-rev-003 / gemini-rev-001: write through the SINGLE blessed symlink-safe
    # atomic writer (O_EXCL + unpredictable temp name + fsync + os.replace) rather
    # than a bespoke/predictable temp file - the same primitive the init wizard and
    # the design-approved marker use, so the guarantees can never diverge.
    atomic_write_text(path, yaml.safe_dump(marker, sort_keys=True))
    return path


def read_session_marker(repo_root: Path) -> dict | None:
    """Load + minimally validate the session marker. Returns None on
    missing or unparseable (NOT an error - dormant mode)."""
    path = _marker_path(repo_root)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    if "iteration_id" not in data or not isinstance(data.get("iteration_id"), str):
        return None
    return data


def clear_session_marker(repo_root: Path) -> bool:
    """Remove the session marker. Returns True if a marker existed
    and was removed; False otherwise. Safe to call when absent."""
    path = _marker_path(repo_root)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def legacy_mode_active(repo_root: Path) -> bool:
    """True iff the operator opted into legacy per-project gating
    via the marker file OR the env var."""
    if os.environ.get(LEGACY_ENV_VAR) == "1":
        return True
    return _legacy_marker_path(repo_root).exists()


def session_active(repo_root: Path) -> bool:
    """Authoritative dormant<->active probe used by the gate.

    Returns True iff:
      (a) Legacy mode opt-in is active (env var or marker file), OR
      (b) The session marker exists, parses, AND its iteration_id
          resolves to a real (UNSEALED) iteration directory in
          `consensus-state/active/<iter-id>/`.

    A session marker pointing at an iteration dir that doesn't
    exist is treated as DORMANT (the marker is effectively garbage
    and ignored - R4 risk mitigation). Operator can clear stale
    markers via `consensus-mcp-seal-iteration close --abandon`.

    Fail-safe to False (= dormant) on any error.
    """
    try:
        repo_root = Path(repo_root)
        if legacy_mode_active(repo_root):
            return True
        data = read_session_marker(repo_root)
        if data is None:
            return False
        iter_id = data["iteration_id"]
        iter_dir = repo_root / "consensus-state" / "active" / iter_id
        return iter_dir.is_dir()
    except Exception:
        return False


def gate_should_enforce(repo_root: Path) -> bool:
    """Single activation predicate shared by ALL consensus hooks.

    The v1.32.1 consult (iteration-v133-gate-scope-shift, 5/5 unanimous) made
    the gate DORMANT-by-default - armed only when a consensus consult is in
    flight - but that model was wired into ONLY the PreToolUse edit/Bash gate.
    The Stop gate and the SessionStart/UserPromptSubmit injector kept firing in
    EVERY repo (gated solely on `consensus-init` being on PATH), nagging
    everyday work that had nothing to do with consensus. This predicate is the
    one source of truth all three hooks now consult so they cannot drift again
    ("gate-consistency hard rule").

    Returns True iff a hook should ENFORCE/inject. Precedence:
      - CONSENSUS_MCP_GATE_DISABLE set -> False (operator escape hatch; the
        human trust-root can never be deadlocked by any hook, ever).
      - CONSENSUS_MCP_FORCE_OPTED_IN set -> True (test / automation override).
      - else -> session_active(repo_root) (legacy opt-in OR a live session
        marker pointing at a real unsealed iteration).

    Fail-safe to False (= dormant) on any error, via session_active.
    """
    if os.environ.get("CONSENSUS_MCP_GATE_DISABLE"):
        return False
    if os.environ.get("CONSENSUS_MCP_FORCE_OPTED_IN"):
        return True
    return session_active(repo_root)


def emit_migration_warning_once(repo_root: Path) -> bool:
    """Emit a one-time stderr warning when a legacy `.consensus/`
    project is loaded under the new dormant-by-default model.

    Returns True if a warning was emitted (this call), False if
    already-warned or not applicable. Idempotent via the
    `.migration-warned` flag file.
    """
    import sys
    repo_root = Path(repo_root)
    consensus_dir = repo_root / ".consensus"
    if not consensus_dir.is_dir():
        return False  # no legacy mode to migrate from
    if legacy_mode_active(repo_root):
        return False  # operator explicitly kept old behavior
    if read_session_marker(repo_root) is not None:
        return False  # session is already active under new model
    flag = repo_root / MIGRATION_WARNED_RELPATH
    if flag.exists():
        return False
    msg = (
        "[consensus-mcp v1.32.1] gate activation is now PER-INVOCATION by "
        "default. This project's `.consensus/` directory used to opt in to "
        "per-project lockdown - under the new default, the gate is DORMANT "
        "unless an active consensus consult is in flight. To restore the old "
        "per-project always-on behavior, either:\n"
        "  - touch .consensus/legacy-always-on  (persistent marker)\n"
        "  - or export CONSENSUS_MCP_LEGACY_ALWAYS_ON=1  (per-session)\n"
        "See docs/operations/first-consult-quickstart.md for the new lifecycle.\n"
    )
    try:
        print(msg, file=sys.stderr, flush=True)
        consensus_dir.mkdir(parents=True, exist_ok=True)
        flag.write_text(
            datetime.datetime.now(datetime.timezone.utc).isoformat() + "\n",
            encoding="utf-8",
        )
    except OSError:
        # If we can't write the flag, we'll re-warn next time. Not fatal.
        pass
    return True
