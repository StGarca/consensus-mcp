"""Pre-Write Rule Consultation Validator.

Per codex external review 2026-05-27, suggestion 5: "Before any new file
write, especially memory files and CLAUDE.md edits, the gate should consult
the relevant standing rules and block contradictions unless the operator
explicitly approves."

This validator detects Write/Edit/MultiEdit/NotebookEdit operations targeting
memory files or CLAUDE.md and requires explicit rule-consultation evidence
to be present in the matching action-intent record. Directly addresses
Instance 5 (Claude wrote a memory file when standing rule
feedback_learnings_become_consensus_mcp_code.md forbade it).

Reads PreToolUse JSON from stdin. Exits 0 to allow, 2 with stderr to block.

Self-test: --self-test runs case coverage. Exits 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path


WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Path patterns that require rule consultation before write.
MEMORY_FILE_PATTERN = re.compile(
    r"/\.claude/projects/[^/]+/memory/[^/]+\.md$"
)
CLAUDE_MD_PATTERN = re.compile(r"(?:^|/)CLAUDE\.md$")

# Anchor INTENT_DIR to __file__ (matches universal-action-gate.py). Layout:
# <repo>/.claude/hooks/X.py → <repo>/tmp. Survives mid-session cwd changes.
INTENT_DIR = Path(__file__).resolve().parent.parent.parent / "tmp"
INTENT_GLOB = "action-intent-*.yaml"

# Freshness check REMOVED 2026-05-27 (round-5 codex-rev-004 / gemini-rev-001 /
# kimi-rev-001 all caught that this file retained the 60s window after
# universal-action-gate.py dropped it). Per operator directive: intent records
# do not expire; matching is tool_name + target only.

# Memory rule MEMORY.md path (lives in ~/.claude/projects/<slug>/memory/MEMORY.md)
MEMORY_INDEX_RELATIVE_HINT = ".claude/projects"
LEARNINGS_RULE_TOKEN = "feedback_learnings_become_consensus_mcp_code"


def parse_simple_yaml(text: str) -> dict | None:
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            return None
        key, _, val = line.partition(":")
        key_stripped = key.strip()
        if not key_stripped or key != key_stripped:
            return None
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        result[key_stripped] = val
    return result


# parse_utc_iso8601 removed 2026-05-27 (kimi-rev-003 / iter-0045 cleanup):
# the freshness check that consumed it was removed per operator directive
# (intent records do not expire). Mirrors the same removal already applied
# to universal-action-gate.py.


def find_memory_index_for(file_path: str) -> Path | None:
    """Given a memory file path, locate the MEMORY.md index for that project."""
    p = Path(file_path)
    parts = p.parts
    try:
        memory_idx = parts.index("memory")
    except ValueError:
        return None
    memory_dir = Path(*parts[: memory_idx + 1])
    memory_index = memory_dir / "MEMORY.md"
    return memory_index if memory_index.exists() else None


def find_matching_intent(
    tool_name: str, target: str
) -> dict | None:
    """Look for an intent record that matches this Write.

    Matching: tool_name + target prefix (first 120 chars). NO freshness check
    (removed 2026-05-27 per operator directive — intent records do not expire).
    When multiple match, picks the most-recent by file mtime.
    """
    if not INTENT_DIR.exists():
        return None
    candidates: list[tuple[float, dict]] = []
    for p in INTENT_DIR.glob(INTENT_GLOB):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        intent = parse_simple_yaml(text)
        if intent is None:
            continue
        if intent.get("tool_name", "") != tool_name:
            continue
        intent_target = (intent.get("target", "") or "").rstrip()
        if intent_target[:120] != (target or "").rstrip()[:120]:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, intent))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def write_target(tool_name: str, tool_input: dict) -> str:
    if tool_name in ("Write", "Edit", "MultiEdit"):
        return tool_input.get("file_path", "") or ""
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path", "") or tool_input.get("file_path", "") or ""
    return ""


def is_memory_file_path(p: str) -> bool:
    return bool(MEMORY_FILE_PATTERN.search(p or ""))


def is_claude_md_path(p: str) -> bool:
    return bool(CLAUDE_MD_PATTERN.search(p or ""))


def block(reason: str) -> int:
    sys.stderr.write(reason + "\n")
    return 2


def consult_memory_index_for_learnings_rule(file_path: str) -> bool:
    """Return True iff MEMORY.md contains feedback_learnings_become_consensus_mcp_code."""
    idx = find_memory_index_for(file_path)
    if idx is None:
        return False
    try:
        text = idx.read_text(encoding="utf-8")
    except OSError:
        return False
    return LEARNINGS_RULE_TOKEN in text


def check_memory_file_write(file_path: str, intent: dict | None) -> int | None:
    """Block memory-file writes that don't explicitly consult standing rules.

    Rule: if MEMORY.md contains the 'learnings_become_consensus_mcp_code'
    rule, then writing a new memory file requires either:
      - an explicit standing_rule_consultation field in the intent that
        names the rule and justifies why the write doesn't violate it; OR
      - operator override (any golden_rule = OPERATOR_OVERRIDE with
        operator_override_evidence naming this specific write).
    """
    learnings_rule_present = consult_memory_index_for_learnings_rule(file_path)
    if not learnings_rule_present:
        # No specific contradicting rule found; allow.
        return None
    if intent is None:
        return block(
            "BLOCKED (pre-write rule consultation): write to memory file "
            f"{file_path!r} requires rule consultation. Standing rule "
            "feedback_learnings_become_consensus_mcp_code is present in "
            "the project MEMORY.md and forbids storing learnings as memory "
            "files (they must become consensus-mcp code). No matching "
            "action-intent record found; cannot verify consultation. "
            "Either move the learning into consensus-mcp code OR provide "
            "operator override evidence in the intent record."
        )
    consultation = (intent.get("standing_rule_consultation") or "").strip()
    if consultation:
        # Claude explicitly consulted; allow even without override.
        return None
    # No consultation field; check operator override.
    has_override = any(
        (intent.get(rf) or "").strip() == "OPERATOR_OVERRIDE"
        for rf in (
            "golden_rule_1_think_before_coding",
            "golden_rule_2_simplicity_first",
            "golden_rule_3_surgical_changes",
            "golden_rule_4_goal_driven_execution",
        )
    )
    if has_override:
        evidence = (intent.get("operator_override_evidence") or "").strip()
        if evidence:
            return None
    return block(
        f"BLOCKED (pre-write rule consultation): memory file write to "
        f"{file_path!r} is forbidden by standing rule "
        "feedback_learnings_become_consensus_mcp_code (present in "
        "MEMORY.md). The matching intent record contains neither a "
        "standing_rule_consultation field nor an OPERATOR_OVERRIDE with "
        "evidence. Either move the learning into consensus-mcp code, OR "
        "add standing_rule_consultation: <one-line citation of why this "
        "write doesn't violate the rule> to the intent record, OR set a "
        "golden_rule_*=OPERATOR_OVERRIDE with operator_override_evidence."
    )


def check_claude_md_write(file_path: str, intent: dict | None) -> int | None:
    """CLAUDE.md edits require explicit consultation evidence in the intent."""
    if intent is None:
        return block(
            f"BLOCKED (pre-write rule consultation): write to CLAUDE.md "
            f"({file_path!r}) requires rule consultation. CLAUDE.md edits "
            "are project-level rule changes that affect every Claude "
            "session. No matching action-intent record found."
        )
    consultation = (intent.get("standing_rule_consultation") or "").strip()
    if consultation:
        return None
    has_override = any(
        (intent.get(rf) or "").strip() == "OPERATOR_OVERRIDE"
        for rf in (
            "golden_rule_1_think_before_coding",
            "golden_rule_2_simplicity_first",
            "golden_rule_3_surgical_changes",
            "golden_rule_4_goal_driven_execution",
        )
    )
    if has_override and (intent.get("operator_override_evidence") or "").strip():
        return None
    return block(
        f"BLOCKED (pre-write rule consultation): CLAUDE.md write to "
        f"{file_path!r} requires either a standing_rule_consultation "
        "field naming which standing rules were considered, OR a "
        "golden_rule_*=OPERATOR_OVERRIDE with operator_override_evidence. "
        "CLAUDE.md edits are project-level rule changes; the gate "
        "enforces explicit consultation."
    )


def main_payload(payload: dict) -> int:
    tool_name = payload.get("tool_name", "") or ""
    if tool_name not in WRITE_TOOLS:
        return 0
    tool_input = payload.get("tool_input", {}) or {}
    target = write_target(tool_name, tool_input)
    if not target:
        return 0

    is_memory = is_memory_file_path(target)
    is_claudemd = is_claude_md_path(target)
    if not (is_memory or is_claudemd):
        return 0

    intent = find_matching_intent(tool_name, target)

    if is_memory:
        rc = check_memory_file_write(target, intent)
        if rc is not None:
            return rc
    if is_claudemd:
        rc = check_claude_md_write(target, intent)
        if rc is not None:
            return rc
    return 0


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
    import shutil
    import tempfile

    failures: list[str] = []

    original_intent_dir = INTENT_DIR
    test_root = Path(tempfile.mkdtemp(prefix="prewriterule-selftest-"))
    test_intent_dir = test_root / "tmp"
    test_intent_dir.mkdir(parents=True, exist_ok=True)
    globals()["INTENT_DIR"] = test_intent_dir

    # Stand up a synthetic MEMORY.md with the learnings rule.
    fake_memory_root = test_root / "claude_projects" / "fake-project" / "memory"
    fake_memory_root.mkdir(parents=True, exist_ok=True)
    (fake_memory_root / "MEMORY.md").write_text(
        "# Memory index\n\n## Feedback\n"
        "- [Learnings become consensus-mcp code](feedback_learnings_become_consensus_mcp_code.md)"
        " — every session learning must ship as PERMANENT consensus-mcp code\n",
        encoding="utf-8",
    )
    # The target memory file path mirrors the production ~/.claude/projects/.../memory/...
    # The MEMORY_FILE_PATTERN matches any /.claude/projects/<slug>/memory/<file>.md.
    # So we need a synthetic path that hits the regex.
    fake_target_memory = (
        Path("/tmp/fake-home/.claude/projects/fake-project/memory/feedback_new_thing.md")
    )

    # Symlink so find_memory_index_for can locate MEMORY.md.
    # Actually find_memory_index_for resolves Path(file_path); we'll need MEMORY.md
    # at /tmp/fake-home/.claude/projects/fake-project/memory/MEMORY.md.
    real_memory_dir = Path("/tmp/fake-home/.claude/projects/fake-project/memory")
    real_memory_dir.mkdir(parents=True, exist_ok=True)
    (real_memory_dir / "MEMORY.md").write_text(
        "# Memory index\n\n## Feedback\n"
        "- [Learnings become consensus-mcp code](feedback_learnings_become_consensus_mcp_code.md)"
        " — every session learning must ship as PERMANENT consensus-mcp code\n",
        encoding="utf-8",
    )

    def now_iso(offset_seconds: float = 0.0) -> str:
        t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=offset_seconds
        )
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")

    def write_intent(filename: str, **fields) -> Path:
        p = test_intent_dir / filename
        body = "\n".join(f"{k}: {v}" for k, v in fields.items()) + "\n"
        p.write_text(body, encoding="utf-8")
        return p

    base_intent = {
        "action_id": "intent-test-aaaaaaaa",
        "tool_name": "Write",
        "target": str(fake_target_memory),
        "stated_assumption": "test",
        "minimality_rationale": "test",
        "scope_rationale": "test",
        "success_criterion": "test exits 0",
        "golden_rule_1_think_before_coding": "PASS",
        "golden_rule_2_simplicity_first": "PASS",
        "golden_rule_3_surgical_changes": "PASS",
        "golden_rule_4_goal_driven_execution": "PASS",
        "created_at_utc": now_iso(),
    }

    cases = []

    # 1. Memory write, no intent at all -> BLOCK
    cases.append(
        (
            "1. memory write no intent -> BLOCK",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(fake_target_memory)},
            },
            2,
            None,
        )
    )

    # 2. Memory write, intent without standing_rule_consultation, all rules PASS -> BLOCK
    cases.append(
        (
            "2. memory write, intent without consultation, all PASS -> BLOCK",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(fake_target_memory)},
            },
            2,
            base_intent,
        )
    )

    # 3. Memory write, intent WITH standing_rule_consultation -> ALLOW
    cases.append(
        (
            "3. memory write, intent with consultation -> ALLOW",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(fake_target_memory)},
            },
            0,
            {
                **base_intent,
                "standing_rule_consultation": (
                    "feedback_learnings_become_consensus_mcp_code reviewed; this "
                    "write is the index entry for a code change, not a learning"
                ),
            },
        )
    )

    # 4. Memory write, intent with OPERATOR_OVERRIDE + evidence -> ALLOW
    cases.append(
        (
            "4. memory write, OPERATOR_OVERRIDE+evidence -> ALLOW",
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(fake_target_memory)},
            },
            0,
            {
                **base_intent,
                "golden_rule_2_simplicity_first": "OPERATOR_OVERRIDE",
                "operator_override_evidence": "operator said 'just write it'",
            },
        )
    )

    # 5. CLAUDE.md write, no intent -> BLOCK
    claudemd_target = "/tmp/fake-project/CLAUDE.md"
    cases.append(
        (
            "5. CLAUDE.md write no intent -> BLOCK",
            {"tool_name": "Edit", "tool_input": {"file_path": claudemd_target}},
            2,
            None,
        )
    )

    # 6. CLAUDE.md write, intent without consultation -> BLOCK
    cases.append(
        (
            "6. CLAUDE.md write no consultation -> BLOCK",
            {"tool_name": "Edit", "tool_input": {"file_path": claudemd_target}},
            2,
            {**base_intent, "tool_name": "Edit", "target": claudemd_target},
        )
    )

    # 7. CLAUDE.md write, intent WITH consultation -> ALLOW
    cases.append(
        (
            "7. CLAUDE.md write with consultation -> ALLOW",
            {"tool_name": "Edit", "tool_input": {"file_path": claudemd_target}},
            0,
            {
                **base_intent,
                "tool_name": "Edit",
                "target": claudemd_target,
                "standing_rule_consultation": "checked CLAUDE.md sections 1-4; this edit extends section 5 per converged plan",
            },
        )
    )

    # 8. Unrelated write (not memory, not CLAUDE.md) -> ALLOW (no intent needed for this validator)
    cases.append(
        (
            "8. unrelated write -> ALLOW",
            {"tool_name": "Write", "tool_input": {"file_path": "/tmp/random.txt"}},
            0,
            None,
        )
    )

    try:
        for name, payload, expected, intent in cases:
            # Clear previous intent files
            for p in test_intent_dir.glob(INTENT_GLOB):
                p.unlink()
            if intent is not None:
                p = test_intent_dir / "action-intent-current.yaml"
                body = "\n".join(f"{k}: {v}" for k, v in intent.items()) + "\n"
                p.write_text(body, encoding="utf-8")
            rc = main_payload(payload)
            if rc != expected:
                failures.append(f"{name}: expected rc={expected}, got rc={rc}")
    finally:
        globals()["INTENT_DIR"] = original_intent_dir
        try:
            shutil.rmtree(test_root)
        except OSError:
            pass
        try:
            shutil.rmtree("/tmp/fake-home")
        except OSError:
            pass

    if failures:
        sys.stderr.write("SELF-TEST FAILURES:\n" + "\n".join(failures) + "\n")
        return 1
    sys.stderr.write(
        f"SELF-TEST PASS: {len(cases)}/{len(cases)} cases.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
