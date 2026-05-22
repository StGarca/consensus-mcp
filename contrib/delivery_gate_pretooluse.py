#!/usr/bin/env python3
"""PreToolUse hook template (Claude Code) — fail-closed delivery gate.

CONTRIB CONVENIENCE ONLY. The portable enforcement is the MCP tool
(`request_delivery`) in server.py — it works across Kimi CLI / Cursor / Claude
Code. This hook is harness-specific to Claude Code (.claude/settings.json) and
is invisible to other harnesses; ship it as an optional belt-and-suspenders.

Wire it (example .claude/settings.json):
    {
      "hooks": {
        "PreToolUse": [
          { "matcher": "SendUserFile",
            "hooks": [ { "type": "command",
                         "command": "python3 hooks/delivery_gate_pretooluse.py" } ] }
        ]
      }
    }

Reads the tool-call JSON on stdin; if the artifact path(s) lack a valid
delivery-readiness token, exits 2 (blocks the tool), mirroring this project's
block_direct_render precedent. Fail-closed: any error blocks.
"""
import json
import sys
from pathlib import Path

try:
    from consensus_mcp._delivery_readiness import verify_delivery_token
except Exception as exc:  # fail-closed: if the gate can't load, block
    print(f"[delivery-gate] cannot import gate ({exc}); blocking (fail-closed)", file=sys.stderr)
    sys.exit(2)


def _artifact_paths(payload: dict) -> list[str]:
    ti = payload.get("tool_input") or payload.get("toolInput") or {}
    files = ti.get("files") or ti.get("paths") or []
    if isinstance(files, str):
        files = [files]
    return [str(f) for f in files]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print("[delivery-gate] unreadable hook payload; blocking (fail-closed)", file=sys.stderr)
        return 2
    paths = _artifact_paths(payload)
    if not paths:
        return 0  # nothing to gate
    for p in paths:
        if not Path(p).exists():
            continue
        res = verify_delivery_token(p)
        if not res.get("ok"):
            print(f"[delivery-gate] BLOCKED: {p} is not consensus-vetted: {res.get('reason')}\n"
                  f"Mint a token after a SEALED consensus iteration vetted it "
                  f"(python -m consensus_mcp._delivery_readiness mint ...).", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
