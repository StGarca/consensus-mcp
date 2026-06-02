"""reviewer.dispatch_grok MCP tool. v1.33.0 wrapper-symmetry.

Mirrors reviewer_dispatch_codex.py and reviewer_dispatch_gemini.py to close
the MCP-surface asymmetry: kimi (and grok) only had shell-binary entry
points (`consensus-mcp-dispatch-kimi/grok`) without corresponding MCP tool
wrappers, so they were invisible in Claude Code's active-tools UI while
codex/gemini dispatches were visible. v1.33.0 adds both wrappers.

Wraps the proven _dispatch_grok helper behind an MCP tool surface.
Helper remains the source of truth; wrapper translates MCP-tool kwargs
into argv, calls main() in-process, captures stdout, returns parsed JSON.

An earlier dispatch-stall (a `--cwd <iteration_dir>` plus dispatcher-only
flag combination that could hang grok indefinitely) is resolved in
`_dispatch_grok.py`: the helper now uses the minimal verified invocation
shape - inline `-p` prompt, `--cwd /tmp`, and only `--no-memory
--disable-web-search` (no `--max-turns`, `--prompt-file`, `--no-plan`,
`--no-subagents`, or `--permission-mode`). This wrapper inherits that
shape unchanged.
"""
from __future__ import annotations

import contextlib
import io
import json

from consensus_mcp import _dispatch_grok


SCHEMA = {
    "name": "reviewer.dispatch_grok",
    "description": (
        "Dispatch the grok CLI as a reviewer for an iteration. Thin MCP "
        "wrapper over the _dispatch_grok helper. Returns the helper's JSON "
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
                "description": "Reviewer identifier; defaults to 'grok-<iteration_id>-1'.",
            },
            "pass_id": {
                "type": ["string", "null"],
                "description": "Pass identifier; defaults to '<reviewer_id>-pass1'.",
            },
            "model": {
                "type": ["string", "null"],
                "description": "Grok model identifier; default per helper.",
            },
            "timeout_seconds": {
                "type": ["integer", "null"],
                "description": "Grok subprocess timeout in seconds; default 1800.",
            },
            "review_target_path": {
                "type": ["string", "null"],
                "description": (
                    "Optional path to the file under review (diff/patch or "
                    "review-packet.yaml); helper computes sha256 and threads "
                    "it through the prompt."
                ),
            },
            "smoke": {
                "type": ["boolean", "null"],
                "description": (
                    "If true, helper's --smoke is passed; the env var "
                    "CONSENSUS_MCP_RUN_REAL_GROK_SMOKE=1 must also be set "
                    "or the helper refuses (exit 3)."
                ),
            },
            "phase": {
                "type": ["string", "null"],
                "enum": ["propose", "review", "converge", None],
                "description": (
                    "Dispatch phase, mapped internally to --mode via "
                    "consensus_mcp.contributors._phase_mode. 'propose' -> "
                    "--mode proposal; 'review' / 'converge' -> --mode review. "
                    "Hides the dispatcher template/schema split from MCP "
                    "callers; matches engine adapter abstraction. If both "
                    "phase and mode are set, mode wins as explicit override."
                ),
            },
            "mode": {
                "type": ["string", "null"],
                "enum": ["review", "proposal", None],
                "description": (
                    "Explicit --mode override for callers needing dispatcher-"
                    "level control. Values match the shell binary's --mode "
                    "flag exactly. Wins over phase if both are set."
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
    """Resolve effective --mode argv value (mirrors codex/gemini)."""
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
    model: str | None,
    timeout_seconds: int | None,
    review_target_path: str | None,
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
    if model is not None:
        argv += ["--model", model]
    if timeout_seconds is not None:
        argv += ["--timeout-seconds", str(timeout_seconds)]
    if review_target_path is not None:
        argv += ["--review-target", review_target_path]
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
    model: str | None = None,
    timeout_seconds: int | None = None,
    review_target_path: str | None = None,
    smoke: bool | None = None,
    phase: str | None = None,
    mode: str | None = None,
) -> dict:
    """Dispatch grok via _dispatch_grok.main; return parsed JSON dict."""
    argv = _build_argv(
        goal_packet_path=goal_packet_path,
        iteration_dir=iteration_dir,
        reviewer_id=reviewer_id,
        pass_id=pass_id,
        model=model,
        timeout_seconds=timeout_seconds,
        review_target_path=review_target_path,
        smoke=smoke,
        phase=phase,
        mode=mode,
    )
    buf = io.StringIO()
    rc: int = 0
    with contextlib.redirect_stdout(buf):
        try:
            rc = _dispatch_grok.main(argv) or 0
        except SystemExit as exc:
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
