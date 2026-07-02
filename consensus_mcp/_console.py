"""Shared console bootstrap helpers for consensus-mcp entry points.

M1-remediation (consult iteration-path-to-a-remediation-260caad1) Q10:
the UTF-8 stream hardening that previously lived only inside
`_init_wizard._force_utf8_streams` is extracted here so EVERY console entry
point -- the MCP server main and the four reviewer-dispatch mains -- can harden
stdout/stderr identically before the first print(). On a Windows console
defaulting to cp1252 the first non-ASCII byte otherwise raises
UnicodeEncodeError and aborts the whole run (the v1.33.4 Windows report).

`force_utf8_streams()` is signature-compatible with the wizard's private
`_force_utf8_streams()` (both take no args, return None, are best-effort and
idempotent), so a later cleanup can point the wizard at this shared helper
without behaviour change. This module deliberately imports ONLY the stdlib
`sys` so it is safe to import from any entry point without cycle risk.
"""
from __future__ import annotations

import sys


def force_utf8_streams() -> None:
    """Reconfigure sys.stdout/sys.stderr to UTF-8 with errors='replace'.

    Idempotent and best-effort. Streams that are None, that lack a
    reconfigure() method (e.g. a captured StringIO under pytest), or whose
    underlying buffer is detached are left untouched. Calling this more than
    once is safe -- io.TextIOWrapper.reconfigure() is itself idempotent, so a
    second call on an already-UTF-8 stream is a no-op.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            pass
