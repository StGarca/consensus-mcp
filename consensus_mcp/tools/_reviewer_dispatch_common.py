"""Shared helpers for the reviewer.dispatch_* MCP tool wrappers.

The per-reviewer wrappers (reviewer_dispatch_codex/gemini/grok/kimi) are thin
translations of MCP-tool kwargs into argv for the matching _dispatch_<reviewer>
helper. Everything except each reviewer's argv-flag surface is identical:

  - phase/mode resolution (--mode precedence),
  - the in-process main() invocation with stdout capture,
  - malformed-stdout + helper-exception hardening,
  - the rc-vs-stdout reconciliation (force ok=False on non-zero rc),
  - the output_schema of the returned dict.

This module is the single source of truth for that shared logic so the four
wrappers stay in lockstep.
"""
from __future__ import annotations

import contextlib
import io
import json


# Identical output shape for every reviewer.dispatch_* tool.
OUTPUT_SCHEMA = {
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
}


def resolve_mode(phase: str | None, mode: str | None) -> str | None:
    """Resolve the effective ``--mode`` argv value.

    Precedence (per iter-0043 converged plan):
      1. explicit ``mode`` wins (escape hatch for dispatcher-level control),
      2. otherwise translate ``phase`` via ``_phase_mode.phase_to_mode``,
      3. otherwise ``None`` (caller omits ``--mode``; dispatcher default applies).
    """
    if mode is not None:
        return mode
    if phase is not None:
        from consensus_mcp.contributors._phase_mode import phase_to_mode
        return phase_to_mode(phase)
    return None


def run_dispatch(dispatch_module, argv: list[str]) -> dict:
    """Invoke ``dispatch_module.main(argv)`` in-process and return a JSON dict.

    ``dispatch_module`` (not its ``main`` attribute) is passed so callers keep
    late-binding: tests monkeypatch ``<module>.main`` and this still sees it.

    Mirrors the historical per-wrapper behavior exactly:
      - argparse ``SystemExit`` -> structured ArgparseSystemExit failure,
      - any other helper exception -> structured failure dict,
      - non-JSON stdout -> WrapperJsonDecodeError with a 200-char sample,
      - non-zero rc with stdout claiming ok!=False -> force ok=False and stamp
        ``wrapper_forced_ok_false_due_to_nonzero_rc`` (iter-0028 F3).
    """
    buf = io.StringIO()
    rc: int = 0
    with contextlib.redirect_stdout(buf):
        try:
            rc = dispatch_module.main(argv) or 0
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
