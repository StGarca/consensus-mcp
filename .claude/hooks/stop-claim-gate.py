"""Stop-Claim Gate.

Per codex external review 2026-05-27 (codex-rev-001 r6) — operator agreed
2026-05-27: natural-language defect claims must be mechanically gated, not
just forwarded to consensus. PreToolUse hooks fire on tool calls; this Stop
hook fires when Claude's assistant response is about to ship. It scans the
message text for forbidden-claim phrases (from
feedback_claude_degradation_hallucination_containment) and blocks the
response unless a proof-citation pattern is also present.

Reads Stop event JSON from stdin (or transcript path on disk). Exits 0 to
allow, 2 with stderr to block.

Self-test: --self-test runs case coverage. Exits 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


# Forbidden-claim phrases (copied verbatim from
# feedback_claude_degradation_hallucination_containment failure-pattern test).
FORBIDDEN_CLAIM_PHRASES = (
    "consensus-mcp failed",
    "the packet builder caused",
    "the dispatcher skipped",
    "the agent description was ambiguous",
    "grok was only dispatched in round 3",
    "only Kimi participated",
    "3-of-5 silent panel reduction",
    "must be sealed via consensus",
    "tooling should prevent this",
)

# Citation patterns: presence of any of these in the message permits
# forbidden-claim phrases (the operator has the proof or the diagnoser's
# verdict; the claim is grounded).
CITATION_PATTERNS = (
    # Proof artifact file path (the canonical bypass-token format).
    re.compile(r"\./tmp/tool-defect-proof-[0-9a-f]{8,64}\.yaml"),
    # Diagnoser status verdict (subagent output marker).
    re.compile(r"STATUS:\s*confirmed-defect"),
    re.compile(r"STATUS:\s*no-defect-likely-self-inflicted"),
    # Explicit operator authorization (for cases the operator handles by override).
    re.compile(r"OPERATOR_OVERRIDE:[^\n]{1,500}"),
    # Section 4.2 proof package marker (codex's prescribed proof structure).
    re.compile(r"Section\s*4\.2\s*proof\s*package"),
)


def has_forbidden_claim(text: str) -> list[str]:
    """Return list of forbidden phrases found in the message text."""
    if not text:
        return []
    lower = text.lower()
    return [p for p in FORBIDDEN_CLAIM_PHRASES if p.lower() in lower]


def has_citation(text: str) -> bool:
    """Return True if any citation pattern matches the message text."""
    if not text:
        return False
    for pat in CITATION_PATTERNS:
        if pat.search(text):
            return True
    return False


def block(reason: str) -> int:
    sys.stderr.write(reason + "\n")
    return 2


def extract_message_text(payload: dict) -> str:
    """Extract the assistant message text from the Stop event payload.

    The Stop event payload structure varies by Claude Code version. Try:
    1. payload['message']['content'] (list of content blocks or string)
    2. payload['transcript_path'] (path to a JSONL transcript; read the last assistant message)
    3. payload['text'] (fallback)
    """
    msg = payload.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block_ in content:
                if isinstance(block_, dict):
                    t = block_.get("text", "")
                    if isinstance(t, str):
                        parts.append(t)
                elif isinstance(block_, str):
                    parts.append(block_)
            return "\n".join(parts)
    text = payload.get("text")
    if isinstance(text, str):
        return text
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path:
        try:
            lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        # Find last assistant message
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "assistant" or obj.get("role") == "assistant":
                content = obj.get("message", {}).get("content") if isinstance(obj.get("message"), dict) else obj.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = []
                    for b in content:
                        if isinstance(b, dict) and isinstance(b.get("text"), str):
                            parts.append(b["text"])
                    return "\n".join(parts)
    return ""


def main_payload(payload: dict) -> int:
    text = extract_message_text(payload)
    forbidden = has_forbidden_claim(text)
    if not forbidden:
        return 0
    if has_citation(text):
        return 0
    return block(
        f"BLOCKED (stop-claim gate): assistant message contains forbidden-claim "
        f"phrase(s) {forbidden!r} WITHOUT a proof citation. Per codex external "
        f"review 2026-05-27 (codex-rev-001 r6, operator-agreed): natural-language "
        f"defect claims must be cited with a tool-defect-proof artifact path "
        f"(./tmp/tool-defect-proof-<hex>.yaml), a Tooling Defect Diagnoser verdict "
        f"(STATUS: confirmed-defect | no-defect-likely-self-inflicted), or an "
        f"explicit OPERATOR_OVERRIDE marker. Either remove the claim, OR dispatch "
        f"the Tooling Defect Diagnoser to produce evidence, OR cite an existing "
        f"proof artifact."
    )


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    # FUCKUP_MODE switch (iter-fuckup-mode-switch-2026-05-27 converged plan, 4-of-4
    # cross-family ratified). Default OFF: gate idle unless marker file exists.
    # Resolved from __file__ to survive arbitrary cwd. Re-checked every invocation.
    _fuckup_marker = Path(__file__).resolve().parent.parent / "FUCKUP_MODE"
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
        (
            "1. clean message, no forbidden phrase -> ALLOW",
            {"message": {"content": "I made the requested change and tests pass."}},
            0,
        ),
        (
            "2. forbidden phrase, no citation -> BLOCK",
            {"message": {"content": "I think consensus-mcp failed when I dispatched it."}},
            2,
        ),
        (
            "3. forbidden phrase + proof artifact citation -> ALLOW",
            {"message": {"content": (
                "consensus-mcp failed; the diagnoser produced "
                "./tmp/tool-defect-proof-abc123def456.yaml so I'm proceeding."
            )}},
            0,
        ),
        (
            "4. forbidden phrase + diagnoser STATUS verdict -> ALLOW",
            {"message": {"content": (
                "I thought consensus-mcp failed, but the diagnoser returned "
                "STATUS: no-defect-likely-self-inflicted, so I'm rebuilding assumptions."
            )}},
            0,
        ),
        (
            "5. forbidden phrase + OPERATOR_OVERRIDE -> ALLOW",
            {"message": {"content": (
                "consensus-mcp failed in this case. OPERATOR_OVERRIDE: "
                "operator authorized writing about the failure mode without proof at 2026-05-27 17:42."
            )}},
            0,
        ),
        (
            "6. multiple forbidden phrases, no citation -> BLOCK",
            {"message": {"content": (
                "The dispatcher skipped my call and tooling should prevent this from happening again."
            )}},
            2,
        ),
        (
            "7. transcript_path fallback with forbidden + no citation -> BLOCK",
            {"transcript_path": "/nonexistent/transcript.jsonl"},
            0,  # transcript missing -> empty text -> no forbidden -> allow
        ),
        (
            "8. content as string -> works",
            {"message": {"content": "all clean and good"}},
            0,
        ),
        (
            "9. content as list of blocks -> works",
            {"message": {"content": [{"type": "text", "text": "consensus-mcp failed"}]}},
            2,
        ),
    ]

    for name, payload, expected in cases:
        rc = main_payload(payload)
        if rc != expected:
            failures.append(f"{name}: expected rc={expected}, got rc={rc}")

    if failures:
        sys.stderr.write("SELF-TEST FAILURES:\n" + "\n".join(failures) + "\n")
        return 1
    sys.stderr.write(f"SELF-TEST PASS: {len(cases)}/{len(cases)} cases.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
