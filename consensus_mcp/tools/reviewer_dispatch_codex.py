"""reviewer.dispatch_codex MCP tool. Phase 4 v1.1.x - thin wrapper.

Wraps the proven _dispatch_codex helper (LANDED v1.10.0; HARDENED v1.10.4)
behind an MCP tool surface. Per project_phase_4_v1_1_x_mcp_wrapper_followup
memory and codex review #6 from v1.10.0:

  - Tool calls into _dispatch_codex helper (NOT a re-implementation).
  - Helper remains the source of truth.
  - Wrapper translates MCP-tool kwargs into argv, calls main() in-process,
    captures stdout, returns parsed JSON dict.

Precondition (now satisfied per v1.10.3 first-real-codex-smoke success
2026-05-09): the helper has produced at least one sealed codex-review.yaml
in a real iteration. Cf. project_phase_4_v1_1_auto_codex_dispatch memory.

Flag-exposure decision (claude-iter0009-001, resolved 2026-05-10):
  Three helper CLI flags are intentionally NOT exposed via the MCP input_schema:
    --prompt-template   (sensible internal default; dispatch_templates path-bound)
    --schema            (sensible internal default; codex output schema is fixed)
    --codex-bin         (default 'codex'; resolved via shutil.which + Windows .cmd
                         preference at v1.10.3)
  Rationale: keep the MCP surface minimal; expose only what an MCP caller would
  realistically need to override. No caller has yet expressed a need for any of
  the three. If/when one does (e.g., to swap codex for an alternative reviewer
  binary), the addition is one schema property + one argv branch - no logic
  change. Decision is reversible, not load-bearing.
"""
from __future__ import annotations

import contextlib
import io
import json

from consensus_mcp import _dispatch_codex


SCHEMA = {
    "name": "reviewer.dispatch_codex",
    "description": (
        "Dispatch the codex CLI as the second reviewer for an iteration. "
        "Thin MCP wrapper over the _dispatch_codex helper. Returns the "
        "helper's JSON output verbatim (success: ok=True with pass_id + "
        "sealed paths; failure: ok=False with error + error_type)."
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
                "description": "Reviewer identifier; defaults to 'codex-<iteration_id>-1'.",
            },
            "pass_id": {
                "type": ["string", "null"],
                "description": "Pass identifier; defaults to '<reviewer_id>-pass1'.",
            },
            "timeout_seconds": {
                "type": ["integer", "null"],
                "description": "Codex subprocess timeout in seconds; default 600.",
            },
            "review_target_path": {
                "type": ["string", "null"],
                "description": (
                    "Optional path to the file under review (diff/patch); helper "
                    "computes sha256 and threads it through the prompt."
                ),
            },
            "smoke": {
                "type": ["boolean", "null"],
                "description": (
                    "If true, helper's --smoke is passed; the env var "
                    "CONSENSUS_MCP_RUN_REAL_CODEX_SMOKE=1 must also be set "
                    "or the helper refuses (exit 3)."
                ),
            },
            "phase": {
                "type": ["string", "null"],
                "enum": ["propose", "review", "converge", None],
                "description": (
                    "iter-0044: dispatch phase, mapped internally to --mode "
                    "via consensus_mcp.contributors._phase_mode. 'propose' -> "
                    "--mode proposal; 'review' / 'converge' -> --mode review. "
                    "Hides the dispatcher template/schema split from MCP "
                    "callers; matches engine adapter abstraction. If both "
                    "phase and mode are set, mode wins as explicit override. "
                    "Default (when neither is set): phase='review' for "
                    "backward compat with pre-iter-0044 callers."
                ),
            },
            "mode": {
                "type": ["string", "null"],
                "enum": ["review", "proposal", None],
                "description": (
                    "iter-0044 escape hatch: explicit --mode override for "
                    "callers needing dispatcher-level control. Values match "
                    "the shell binary's --mode flag exactly. Wins over "
                    "phase if both are set."
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
    """iter-0044: resolve the effective --mode argv value.

    Precedence (per iter-0043 converged plan q2 weighted-synthesis):
      1. explicit `mode` wins (escape hatch for dispatcher-level control)
      2. otherwise translate `phase` via _phase_mode.phase_to_mode
      3. otherwise return None (caller omits --mode; dispatcher's own
         default of "review" applies, preserving pre-iter-0044 behavior)
    """
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
    # iter-0044: resolve and append --mode. Omitted entirely when both
    # phase and mode are None (preserves pre-iter-0044 behavior of relying
    # on the dispatcher's default "review" mode).
    resolved_mode = _resolve_mode(phase, mode)
    if resolved_mode is not None:
        argv += ["--mode", resolved_mode]
    # smoke is a boolean flag (no value arg), so we omit on any falsy
    # input (None or False) - asymmetric with the value-bearing args above
    # which use `is not None`. Either way `--smoke` is only added when truthy.
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
    smoke: bool | None = None,
    phase: str | None = None,
    mode: str | None = None,
) -> dict:
    """Dispatch codex via _dispatch_codex.main; return parsed JSON dict.

    iter-0044: phase + mode parameters added (per iter-0043 converged plan).
    `phase` is the engine-abstraction parameter (propose/review/converge);
    `mode` is the dispatcher-level escape hatch (review/proposal). When
    both are set, mode wins. When neither is set, the dispatcher's
    default --mode review applies (backward compat).
    """
    argv = _build_argv(
        goal_packet_path=goal_packet_path,
        iteration_dir=iteration_dir,
        reviewer_id=reviewer_id,
        pass_id=pass_id,
        timeout_seconds=timeout_seconds,
        review_target_path=review_target_path,
        smoke=smoke,
        phase=phase,
        mode=mode,
    )
    buf = io.StringIO()
    # iter-0012 F3: wrap the helper main() call itself in try/except so any
    # exception raised during pre-checks (yaml.YAMLError, ImportError,
    # RuntimeError, FileNotFoundError, etc.) becomes a structured error dict
    # for direct MCP callers instead of leaking as a JSON-RPC error.
    # loop.run_goal contains exceptions via its own try/except, but direct
    # tool callers got raw tracebacks before this fix.
    #
    # iter-0028 F3 (codex-rev-001): capture the helper's return code. The
    # prior wrapper discarded main()'s rc entirely and trusted stdout JSON's
    # `ok` field. A non-zero rc with `ok=True` in stdout would have been
    # reported as success. Defense-in-depth: when rc != 0 and stdout JSON
    # claims ok=True (or omits ok), force ok=False and stamp a marker key.
    rc: int = 0
    with contextlib.redirect_stdout(buf):
        try:
            rc = _dispatch_codex.main(argv) or 0
        except SystemExit as exc:
            # iter-0011 codex-rev-001 cross-fix: argparse raises SystemExit
            # (BaseException, not Exception) on bad input. Without an explicit
            # catch, malformed input from an MCP caller could kill the stdio
            # MCP server. Convert to a structured failure response. (Same fix
            # applied to reviewer_dispatch_gemini.py simultaneously.)
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
    # iter-0028 F3: rc-vs-stdout reconciliation. Only force when rc indicates
    # failure AND the parsed payload is not already declaring ok=False. The
    # marker key is only added on the forcing path; honest-failure passthrough
    # (rc != 0 AND parsed["ok"] is False) leaves the dict untouched.
    if rc != 0 and isinstance(parsed, dict) and parsed.get("ok") is not False:
        parsed["ok"] = False
        parsed["wrapper_forced_ok_false_due_to_nonzero_rc"] = True
    return parsed


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
