"""MCP server for the consensus pipeline. Phase 1 G1+G2 hybrid skeleton.

Per spec docs/architecture/phase-1-completion.md
section 3 (architecture) + section 5 (permissions).

Registers tool framework, performs boot-time validate_disposition_index
check, listens on stdio. Tools registered: T2..T11 + Phase 4 supervisor +
codex-fix-author surface. Phase 1 MCP G1+G2 implementation complete:
state.read_decision_ledger (T2), audit.append_event (T3),
patch.stage_and_dry_run (T4), patch.apply_consensus_patch (T5),
review.write_and_seal (T6), review.read_post_seal (T7).
Phase 2 G5: state.update_decision_ledger (T8).
Phase 2 G3: repo.get_section (T9), repo.set_section (T10).
Phase 2 G4: gate.evaluate_production_with_scope_match (T11).
Phase 4 supervisor: reviewer.dispatch_codex, loop.run_goal,
loop.verify_codex_patch, apply.codex_patch (Task #26 / iter-0016).

Implementation note: the Anthropic MCP SDK (pip install mcp) was not
present in python_env at implementation time. This server uses hand-rolled
stdio JSON-RPC 2.0, which is the raw wire format the MCP SDK wraps anyway.
If the SDK is later installed, this file can be swapped for the SDK-style
server; the ToolRegistry contract stays the same.

Boot sequence:
  1. Run validate_disposition_index against the canonical spec md
  2. If findings != 0: REFUSE TO START; print findings to stderr; exit 2
  3. If findings == 0: register tool surface (empty for now); listen on stdio
  4. Append mcp_server_started event to consensus-state/state/mcp-server-audit.jsonl

Shutdown:
  - Append mcp_server_stopped event before exiting

CLI flags:
  --boot-and-exit   Boot, write audit event, then exit cleanly. Used by
                    the smoke test to verify the boot/shutdown cycle without
                    keeping a long-running stdio server open.
"""
from __future__ import annotations
import argparse
import importlib.metadata
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _package_version() -> str:
    """iter-0001 codex-rev-002 fix: derive serverInfo.version from installed
    package metadata so it stays in sync with pyproject.toml. Falls back to
    "unknown" if the package isn't installed (e.g., running from a fresh
    source checkout without `pip install -e .`)."""
    try:
        return importlib.metadata.version("consensus-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"

def _resolve_repo_root() -> Path:
    """Legacy REPO_ROOT resolver. Kept for back-compat.

    v1.13.0: no longer load-bearing for spec/state/project-root. Use
    _resolve_spec_path / _resolve_state_root / _resolve_project_root instead.
    """
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent


def _resolve_spec_path() -> Path:
    """Spec path: env override > legacy REPO_ROOT > walked-up checkout > packaged template."""
    override = os.environ.get("CONSENSUS_MCP_SPEC_PATH")
    if override:
        return Path(override).resolve()
    repo_root_env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if repo_root_env:
        legacy = Path(repo_root_env).resolve() / "docs" / "architecture" / "orchestration-spec.md"
        if legacy.exists():
            return legacy
    walked = Path(__file__).resolve().parent.parent / "docs" / "architecture" / "orchestration-spec.md"
    if walked.exists():
        return walked
    return Path(__file__).resolve().parent / "spec_template.md"


def _resolve_state_root() -> Path:
    """State root: env override > legacy REPO_ROOT > CWD."""
    override = os.environ.get("CONSENSUS_MCP_STATE_ROOT")
    if override:
        return Path(override).resolve()
    repo_root_env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if repo_root_env:
        return Path(repo_root_env).resolve() / "consensus-state"
    return Path.cwd() / "consensus-state"


def _resolve_project_root() -> Path:
    """Project root (reviewable-file root): env override > legacy REPO_ROOT > CWD.

    This is what consumers of goal_packet.allowed_files resolve against.
    """
    override = os.environ.get("CONSENSUS_MCP_PROJECT_ROOT")
    if override:
        return Path(override).resolve()
    repo_root_env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if repo_root_env:
        return Path(repo_root_env).resolve()
    return Path.cwd()


REPO_ROOT = _resolve_repo_root()
SPEC_PATH = _resolve_spec_path()
STATE_ROOT = _resolve_state_root()
PROJECT_ROOT = _resolve_project_root()


def _resolve_audit_log_path() -> Path:
    """Allow smoke tests to point AUDIT_LOG at a temp sink via env var; default to state root."""
    override = os.environ.get("CONSENSUS_MCP_AUDIT_LOG")
    if override:
        return Path(override)
    return STATE_ROOT / "state" / "mcp-server-audit.jsonl"


AUDIT_LOG = _resolve_audit_log_path()


from consensus_mcp.tool_registry import ToolRegistry  # noqa: E402

# Global registry -- T2-T11 modules import this and call registry.register().
registry = ToolRegistry()

from consensus_mcp.tools import state_read_decision_ledger  # noqa: E402
state_read_decision_ledger.register(registry)

from consensus_mcp.tools import audit_append_event  # noqa: E402
audit_append_event.register(registry)

from consensus_mcp.tools import patch_stage_and_dry_run  # noqa: E402
patch_stage_and_dry_run.register(registry)

from consensus_mcp.tools import patch_apply_consensus_patch  # noqa: E402
patch_apply_consensus_patch.register(registry)

from consensus_mcp.tools import review_write_and_seal  # noqa: E402
review_write_and_seal.register(registry)

from consensus_mcp.tools import review_read_post_seal  # noqa: E402
review_read_post_seal.register(registry)

from consensus_mcp.tools import state_update_decision_ledger  # noqa: E402
state_update_decision_ledger.register(registry)

from consensus_mcp.tools import repo_get_section  # noqa: E402
repo_get_section.register(registry)

from consensus_mcp.tools import repo_set_section  # noqa: E402
repo_set_section.register(registry)

from consensus_mcp.tools import gate_evaluate_production_with_scope_match  # noqa: E402
gate_evaluate_production_with_scope_match.register(registry)

from consensus_mcp.tools import reviewer_dispatch_codex  # noqa: E402
reviewer_dispatch_codex.register(registry)

from consensus_mcp.tools import loop_run_goal  # noqa: E402
loop_run_goal.register(registry)

from consensus_mcp.tools import loop_verify_codex_patch  # noqa: E402
loop_verify_codex_patch.register(registry)

from consensus_mcp.tools import apply_codex_patch  # noqa: E402
apply_codex_patch.register(registry)

from consensus_mcp.tools import resume  # noqa: E402
resume.register(registry)


# ---------------------------------------------------------------------------
# Boot-time disposition check
# ---------------------------------------------------------------------------

def _run_disposition_check() -> int:
    """Run validate_disposition_index against the spec; return findings count.

    Returns 0 if clean. Non-zero means server should refuse to start.
    Raises on import/IO error (caller handles).
    """
    from consensus_mcp.validators.validate_disposition_index import validate_disposition_index
    report = validate_disposition_index(SPEC_PATH)
    return report["stats"]["total_findings"]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _append_audit_event(event: str, extra: dict | None = None) -> None:
    """Append one JSON-lines record to mcp-server-audit.jsonl (create if missing).

    True JSONL (one JSON object per line). Atomic-safe for concurrent writers:
    each write is a single open-append-close, so two racing processes interleave
    lines rather than silently dropping each other's events. (Note: this is
    atomic-append for the SERVER boot/stop log; per-iteration audit_append_event
    has different semantics -- see audit_append_event.py docstring.)
    """
    record: dict = {"event": event, "timestamp_utc": _now_utc()}
    if extra:
        record.update(extra)
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Stdio JSON-RPC 2.0 server (MCP wire format)
# ---------------------------------------------------------------------------

def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _handle_request(req: dict) -> dict | None:
    """Dispatch one JSON-RPC request; return response dict or None (for notifications)."""
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    # MCP initialize handshake
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "consensus-mcp", "version": _package_version()},
                "capabilities": {"tools": {}},
            },
        }

    # MCP tools/list
    if method == "tools/list":
        tools = registry.list_tools()
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tools},
        }

    # MCP tools/call
    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            handler = registry.get_handler(name)
        except KeyError:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"tool not found: {name}"},
            }
        try:
            result = handler(**arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    # Notification (no id) -- no response required
    if req_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _serve_stdio() -> None:
    """Read JSON-RPC lines from stdin; write responses to stdout."""
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            req = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            _send({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc}"},
            })
            continue
        resp = _handle_request(req)
        if resp is not None:
            _send(resp)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="consensus-mcp MCP server (Phase 1 skeleton)")
    parser.add_argument(
        "--boot-and-exit",
        action="store_true",
        help="boot, write audit event, then exit (used by smoke test)",
    )
    args = parser.parse_args(argv)

    # Boot gate: refuse to start if spec has findings.
    try:
        findings = _run_disposition_check()
    except Exception as exc:
        print(f"ERROR: disposition check failed: {exc}", file=sys.stderr)
        return 2

    if findings != 0:
        print(
            f"ERROR: validate_disposition_index reports {findings} finding(s); "
            "server refuses to start.",
            file=sys.stderr,
        )
        return 2

    _append_audit_event("mcp_server_started", {"findings_at_boot": findings})

    if args.boot_and_exit:
        _append_audit_event("mcp_server_stopped", {"reason": "boot-and-exit"})
        return 0

    try:
        _serve_stdio()
    finally:
        _append_audit_event("mcp_server_stopped", {"reason": "stdin_eof"})

    return 0


if __name__ == "__main__":
    sys.exit(main())
