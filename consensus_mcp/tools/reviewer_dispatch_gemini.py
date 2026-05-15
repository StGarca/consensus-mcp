"""reviewer.dispatch_gemini MCP tool. v1.14.0 — thin wrapper over _dispatch_gemini.

Mirrors reviewer_dispatch_codex.py: translates MCP tool kwargs into argv,
calls _dispatch_gemini.main() in-process, captures stdout, returns parsed
JSON. The helper remains the source of truth.

Scope (iter-0011): review-only. Gemini does NOT emit patch_proposal in
v1.14.0; the schema and parser enforce this. Patch authoring parity is
deferred to iter-0013 capability metadata.

The MCP surface exposes `--model` (gemini's CLI flag for model selection)
because operators may reasonably want to pick between gemini-2.5-pro (default)
and lighter/cheaper variants per dispatch.
"""
from __future__ import annotations

import contextlib
import io
import json

from consensus_mcp import _dispatch_gemini


SCHEMA = {
    "name": "reviewer.dispatch_gemini",
    "description": (
        "Dispatch the gemini CLI as a reviewer for an iteration. Thin MCP "
        "wrapper over the _dispatch_gemini helper. Returns the helper's JSON "
        "output verbatim (success: ok=True with pass_id + sealed paths; "
        "failure: ok=False with error + error_type)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "goal_packet_path": {
                "type": "string",
                "description": "Repo-relative or absolute path to the iteration's goal_packet.yaml.",
            },
            "iteration_dir": {
                "type": "string",
                "description": "Repo-relative or absolute path to the iteration directory.",
            },
            "reviewer_id": {
                "type": ["string", "null"],
                "description": "Reviewer identifier; defaults to 'gemini-<iteration_id>-1'.",
            },
            "pass_id": {
                "type": ["string", "null"],
                "description": "Pass identifier; defaults to '<reviewer_id>-pass1'.",
            },
            "timeout_seconds": {
                "type": ["integer", "null"],
                "description": "Gemini subprocess timeout in seconds; default 600.",
            },
            "review_target_path": {
                "type": ["string", "null"],
                "description": (
                    "Optional path to the file under review (diff/patch or "
                    "review-packet.yaml); helper computes sha256 and threads "
                    "it through the prompt."
                ),
            },
            "model": {
                "type": ["string", "null"],
                "description": "Gemini model identifier; defaults to gemini-2.5-pro.",
            },
            "smoke": {
                "type": ["boolean", "null"],
                "description": (
                    "If true, helper's --smoke is passed; the env var "
                    "CONSENSUS_MCP_RUN_REAL_GEMINI_SMOKE=1 must also be set "
                    "or the helper refuses (exit 3)."
                ),
            },
            "phase": {
                "type": ["string", "null"],
                "enum": ["propose", "review", "converge", None],
                "description": (
                    "iter-0044: dispatch phase, mapped internally to --mode "
                    "via consensus_mcp.contributors._phase_mode. 'propose' → "
                    "--mode proposal; 'review' / 'converge' → --mode review. "
                    "Hides dispatcher template/schema split from MCP callers; "
                    "matches engine adapter abstraction. If both phase and "
                    "mode are set, mode wins."
                ),
            },
            "mode": {
                "type": ["string", "null"],
                "enum": ["review", "proposal", None],
                "description": (
                    "iter-0044 escape hatch: explicit --mode override for "
                    "callers needing dispatcher-level control. Wins over "
                    "phase when both are set."
                ),
            },
        },
        "required": ["goal_packet_path", "iteration_dir"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "pass_id": {"type": ["string", "null"]},
            "packet_sha256": {"type": ["string", "null"]},
            "sealed_path": {"type": ["string", "null"]},
            "archive_sealed_path": {"type": ["string", "null"]},
            "audit_event_id": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
            "error_type": {"type": ["string", "null"]},
            "raw_stdout_sample": {"type": ["string", "null"]},
        },
        "required": ["ok"],
    },
}


def _resolve_mode(phase: str | None, mode: str | None) -> str | None:
    """iter-0044: resolve effective --mode argv value (mirror of codex wrapper)."""
    if mode is not None:
        return mode
    if phase is not None:
        from consensus_mcp.contributors._phase_mode import phase_to_mode
        return phase_to_mode(phase)
    return None


def _build_argv(
    goal_packet_path: str,
    iteration_dir: str,
    reviewer_id: str | None,
    pass_id: str | None,
    timeout_seconds: int | None,
    review_target_path: str | None,
    model: str | None,
    smoke: bool | None,
    phase: str | None = None,
    mode: str | None = None,
) -> list[str]:
    argv: list[str] = [
        "--goal-packet", goal_packet_path,
        "--iteration-dir", iteration_dir,
    ]
    if reviewer_id is not None:
        argv += ["--reviewer-id", reviewer_id]
    if pass_id is not None:
        argv += ["--pass-id", pass_id]
    if timeout_seconds is not None:
        argv += ["--timeout-seconds", str(timeout_seconds)]
    if review_target_path is not None:
        argv += ["--review-target", review_target_path]
    if model is not None:
        argv += ["--model", model]
    # iter-0044: append --mode based on phase/mode (omitted entirely if neither set).
    resolved_mode = _resolve_mode(phase, mode)
    if resolved_mode is not None:
        argv += ["--mode", resolved_mode]
    if smoke:
        argv += ["--smoke"]
    return argv


def handle(
    goal_packet_path: str,
    iteration_dir: str,
    reviewer_id: str | None = None,
    pass_id: str | None = None,
    timeout_seconds: int | None = None,
    review_target_path: str | None = None,
    model: str | None = None,
    smoke: bool | None = None,
    phase: str | None = None,
    mode: str | None = None,
) -> dict:
    """Dispatch gemini via _dispatch_gemini.main; return parsed JSON dict.

    iter-0044: phase + mode parameters added (per iter-0043 converged plan).
    See reviewer_dispatch_codex.handle docstring for semantics; identical.

    Same rc-vs-stdout reconciliation as reviewer_dispatch_codex (iter-0028 F3):
    if main() returns a non-zero exit code but stdout JSON claims ok=True,
    force ok=False and stamp the marker key. Defense-in-depth.
    """
    argv = _build_argv(
        goal_packet_path=goal_packet_path,
        iteration_dir=iteration_dir,
        reviewer_id=reviewer_id,
        pass_id=pass_id,
        timeout_seconds=timeout_seconds,
        review_target_path=review_target_path,
        model=model,
        smoke=smoke,
        phase=phase,
        mode=mode,
    )
    buf = io.StringIO()
    rc: int = 0
    with contextlib.redirect_stdout(buf):
        try:
            rc = _dispatch_gemini.main(argv) or 0
        except SystemExit as exc:
            # codex-rev-001 round-2 fix: argparse raises SystemExit on bad
            # input. SystemExit subclasses BaseException (NOT Exception), so
            # the bare `except Exception` below would let it escape and kill
            # the stdio MCP server. Catch it explicitly and convert to a
            # structured failure response.
            return {
                "ok": False,
                "error_type": "ArgparseSystemExit",
                "error": f"argparse rejected input: {exc.code!r}",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
    output = buf.getvalue().strip()
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error_type": "WrapperJsonDecodeError",
            "error": str(exc),
            "raw_stdout_sample": output[:200],
        }
    if rc != 0 and isinstance(parsed, dict) and parsed.get("ok") is not False:
        parsed["ok"] = False
        parsed["wrapper_forced_ok_false_due_to_nonzero_rc"] = True
    return parsed


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
