"""Dispatch Canon Validator.

Per codex external review 2026-05-27, suggestion 4: pre-dispatch validator
that blocks prohibited dispatch patterns and validates Bash dispatch
invocations against dispatch-invocation-canon.md.

Specifically catches (from codex's enumeration):
- MCP wrapper dispatches (mcp__consensus-mcp__reviewer_dispatch_*)
- missing run_in_background=true on consensus-mcp-dispatch-* Bash calls
- forbidden grok flags: --prompt-file, --max-turns, --cwd <iteration_dir>,
  --no-plan, --no-subagents, --permission-mode
- batched multi-dispatch commands collapsing multiple contributors into
  one invisible process

Reads PreToolUse JSON from stdin. Exits 0 to allow, 2 with stderr to block.

Self-test: --self-test runs case coverage. Exits 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import json
import re
import shlex
import sys


PROHIBITED_MCP_DISPATCH_TOOLS = {
    "mcp__consensus-mcp__reviewer_dispatch_codex",
    "mcp__consensus-mcp__reviewer_dispatch_gemini",
    "mcp__consensus-mcp__reviewer_dispatch_kimi",
    "mcp__consensus-mcp__reviewer_dispatch_grok",
}

CONSENSUS_DISPATCH_BIN_PATTERN = re.compile(
    r"\bconsensus-mcp-dispatch-(codex|gemini|kimi|grok)\b"
)

# Forbidden grok flags when grok CLI is invoked directly (not via dispatcher).
GROK_BIN_PATTERN = re.compile(r"(?:^|[\s/])grok\b")
GROK_FORBIDDEN_FLAGS = (
    "--prompt-file",
    "--max-turns",
    "--no-plan",
    "--no-subagents",
    "--permission-mode",
)

# Iteration-dir --cwd is forbidden for grok; only /tmp is allowed.
# (Round-5 kimi-rev-007 cleanup: removed GROK_CWD_PATTERN compiled regex -
# replaced by shlex token-based --cwd extraction in check_grok_direct_invocation.)
ALLOWED_GROK_CWDS = ("/tmp", "/tmp/")

# Batched multi-dispatch patterns: chained dispatcher calls in one Bash invocation.
# Detects `consensus-mcp-dispatch-` appearing more than once in the same command
# (joined by &&, ||, ;, |, &).
BATCHED_MULTI_DISPATCH_THRESHOLD = 2


def block(reason: str) -> int:
    sys.stderr.write(reason + "\n")
    return 2


def check_mcp_dispatch_wrapper(tool_name: str) -> int | None:
    if tool_name in PROHIBITED_MCP_DISPATCH_TOOLS:
        return block(
            "BLOCKED (dispatch canon): MCP dispatcher wrapper "
            f"{tool_name!r} is EXPLICITLY PROHIBITED per "
            "consensus-state/active/iteration-claude-screwup-prevention-meta-2026-05-27/"
            "dispatch-invocation-canon.md. MCP wrappers run inside the MCP "
            "server process and are invisible to the operator's "
            "background-process surface. Use the shell binary "
            ".local/share/pipx/venvs/consensus-mcp/bin/"
            f"consensus-mcp-dispatch-{tool_name.split('_')[-1]} via Bash with "
            "run_in_background=true."
        )
    return None


def check_consensus_dispatch_bash(tool_input: dict) -> int | None:
    cmd = tool_input.get("command", "")
    if not isinstance(cmd, str) or not cmd:
        return None
    # Token-based detection (round-5 fix): only count occurrences in actual
    # argv tokens, not substring of quoted prompt content. The prior
    # findall(cmd) version false-positived when the bash command embedded
    # a review-packet whose documentation text mentioned
    # `consensus-mcp-dispatch-*` strings - a 2+ findall triggered the
    # batched-multi-dispatch block on what was actually a single dispatch.
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        # Couldn't tokenize cleanly; be permissive rather than false-positive.
        return None
    matches = [t for t in tokens if CONSENSUS_DISPATCH_BIN_PATTERN.search(t)]
    if not matches:
        return None
    # Rule 1: run_in_background must be true (PreToolUse passes the param).
    if not tool_input.get("run_in_background", False):
        return block(
            "BLOCKED (dispatch canon): consensus-mcp-dispatch-* in Bash "
            "requires run_in_background=true so the operator sees the "
            "background task in their UI. Without it the dispatch is "
            "invisible (same failure surface as MCP wrappers). Re-issue "
            "with run_in_background=true."
        )
    # Rule 2: no batched multi-dispatch (one token-form invocation per Bash call).
    if len(matches) >= BATCHED_MULTI_DISPATCH_THRESHOLD:
        return block(
            "BLOCKED (dispatch canon): a single Bash invocation references "
            f"{len(matches)} consensus-mcp-dispatch-* binaries as argv "
            "tokens. Each contributor must be its own Bash run_in_background "
            "call so each gets a distinct task ID and is independently "
            "visible to the operator. Split into separate Bash tool calls."
        )
    return None


def check_grok_direct_invocation(tool_input: dict) -> int | None:
    cmd = tool_input.get("command", "")
    if not isinstance(cmd, str) or not cmd:
        return None
    if not GROK_BIN_PATTERN.search(cmd):
        return None
    # Don't double-check consensus-mcp-dispatch-grok here; that's handled above.
    if CONSENSUS_DISPATCH_BIN_PATTERN.search(cmd):
        return None
    # Token-based forbidden-flag check (NOT substring) so flag names appearing
    # inside quoted prompt content (e.g., when the prompt embeds the canon doc
    # that lists forbidden flags as text) do NOT trigger false positives.
    # Round-4 grok dispatch hit this exact false positive: the review-packet's
    # embedded dispatch-invocation-canon.md contains `--max-turns`, `--prompt-file`,
    # etc. as documentation text, which the prior `if f in cmd` substring check
    # incorrectly flagged.
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    forbidden_hits = [f for f in GROK_FORBIDDEN_FLAGS if f in tokens]
    if forbidden_hits:
        return block(
            "BLOCKED (dispatch canon): direct grok invocation contains "
            f"forbidden flag(s) {forbidden_hits!r} as actual command-line tokens. "
            "Per dispatch-invocation-canon.md, the proven simple shape is: "
            "`grok -p \"<inline prompt>\" --output-format plain --no-memory "
            "--disable-web-search --cwd /tmp`. NO --max-turns, NO "
            "--prompt-file, NO --no-plan, NO --no-subagents, NO "
            "--permission-mode."
        )
    # --cwd check uses the same token-based scan to find the cwd argument
    # in the actual argv, not just any --cwd substring in quoted content.
    if "--cwd" in tokens:
        try:
            idx = tokens.index("--cwd")
            if idx + 1 < len(tokens):
                cwd_val = tokens[idx + 1]
                if cwd_val not in ALLOWED_GROK_CWDS:
                    return block(
                        "BLOCKED (dispatch canon): direct grok invocation uses "
                        f"--cwd {cwd_val!r}. The only allowed grok --cwd is /tmp "
                        "(per canon: project subdirs trigger the .mcp.json stall). "
                        "Change --cwd to /tmp."
                    )
        except ValueError:
            pass
    return None


def main_payload(payload: dict) -> int:
    tool_name = payload.get("tool_name", "") or ""
    tool_input = payload.get("tool_input", {}) or {}

    rc = check_mcp_dispatch_wrapper(tool_name)
    if rc is not None:
        return rc

    if tool_name == "Bash":
        rc = check_consensus_dispatch_bash(tool_input)
        if rc is not None:
            return rc
        rc = check_grok_direct_invocation(tool_input)
        if rc is not None:
            return rc

    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    # FUCKUP_MODE switch (iter-fuckup-mode-switch-2026-05-27 converged plan, 4-of-4
    # cross-family ratified). Default OFF: gate idle unless marker file exists.
    # Resolved from __file__ to survive arbitrary cwd. Re-checked every invocation.
    from pathlib import Path as _Path
    _fuckup_marker = _Path(__file__).resolve().parent.parent / "FUCKUP_MODE"
    if not _fuckup_marker.exists():
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    return main_payload(payload)


def self_test() -> int:
    failures: list[str] = []

    cases = [
        # name, payload, expected_rc
        (
            "1. MCP wrapper codex -> BLOCK",
            {
                "tool_name": "mcp__consensus-mcp__reviewer_dispatch_codex",
                "tool_input": {"goal_packet_path": "x", "iteration_dir": "x"},
            },
            2,
        ),
        (
            "2. MCP wrapper kimi -> BLOCK",
            {
                "tool_name": "mcp__consensus-mcp__reviewer_dispatch_kimi",
                "tool_input": {"goal_packet_path": "x", "iteration_dir": "x"},
            },
            2,
        ),
        (
            "3. Bash consensus-mcp-dispatch-codex with run_in_background=true -> ALLOW",
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "consensus-mcp-dispatch-codex --goal-packet x",
                    "run_in_background": True,
                },
            },
            0,
        ),
        (
            "4. Bash consensus-mcp-dispatch-codex WITHOUT run_in_background -> BLOCK",
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "consensus-mcp-dispatch-codex --goal-packet x",
                },
            },
            2,
        ),
        (
            "5. Bash batched 2 dispatchers -> BLOCK",
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "consensus-mcp-dispatch-codex x && consensus-mcp-dispatch-gemini y",
                    "run_in_background": True,
                },
            },
            2,
        ),
        (
            "6. Bash grok with --max-turns -> BLOCK",
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "grok -p 'hi' --output-format plain --max-turns 100 --cwd /tmp",
                    "run_in_background": True,
                },
            },
            2,
        ),
        (
            "7. Bash grok with --prompt-file -> BLOCK",
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "grok --prompt-file /tmp/p.txt --output-format plain",
                    "run_in_background": True,
                },
            },
            2,
        ),
        (
            "8. Bash grok with --cwd <not /tmp> -> BLOCK",
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "grok -p hi --output-format plain --cwd /path/to/project",
                    "run_in_background": True,
                },
            },
            2,
        ),
        (
            "9. Bash grok canonical shape -> ALLOW",
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "grok -p 'hi' --output-format plain --no-memory --disable-web-search --cwd /tmp",
                    "run_in_background": True,
                },
            },
            0,
        ),
        (
            "10. Bash unrelated command -> ALLOW",
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "ls -la /tmp",
                    "run_in_background": False,
                },
            },
            0,
        ),
        (
            "10b. Bash grok with forbidden flag INSIDE quoted prompt content -> ALLOW (token-based check)",
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "grok -p 'the canon mentions --max-turns and --prompt-file as forbidden' --output-format plain --no-memory --disable-web-search --cwd /tmp",
                    "run_in_background": True,
                },
            },
            0,
        ),
        (
            "10c. Bash grok with consensus-mcp-dispatch-* names INSIDE quoted prompt content -> ALLOW (token-based check)",
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "grok -p 'documentation mentions consensus-mcp-dispatch-codex and consensus-mcp-dispatch-gemini and consensus-mcp-dispatch-kimi as the proven shape' --output-format plain --no-memory --disable-web-search --cwd /tmp",
                    "run_in_background": True,
                },
            },
            0,
        ),
        (
            "11. Non-Bash, non-MCP-dispatch tool -> ALLOW (out of scope)",
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/x"},
            },
            0,
        ),
    ]

    for name, payload, expected in cases:
        rc = main_payload(payload)
        if rc != expected:
            failures.append(f"{name}: expected rc={expected}, got rc={rc}")

    if failures:
        sys.stderr.write("SELF-TEST FAILURES:\n" + "\n".join(failures) + "\n")
        return 1
    sys.stderr.write(
        f"SELF-TEST PASS: {len(cases)}/{len(cases)} cases.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
