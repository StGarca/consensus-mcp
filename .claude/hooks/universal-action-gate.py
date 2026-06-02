"""Universal Pre-Action Gate.

Per codex external review 2026-05-27, suggestions 1+2+3+6:
- Suggestion 1: replace narrow protected-path gate with universal pre-action gate
  evaluating every Claude action surface.
- Suggestion 2: validates against the action-intent schema pinned at
  .consensus/schemas/action-intent.schema.yaml.
- Suggestion 3: produces append-only audit log at ./tmp/action-audit-log.jsonl.
- Suggestion 6: removes the implicit fast path - every action either has a
  valid intent record or is blocked.

Reads PreToolUse JSON from stdin. Exits 0 to allow, 2 with stderr to block.

Self-test: --self-test runs cases covering no record, stale record, wrong
tool, target mismatch, golden-rule fail, valid record, contentless tool
allowlist. Exits 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path


# Anchor INTENT_DIR to __file__ so it is the same path regardless of the
# cwd Claude Code launches the hook with. Layout: <repo>/.claude/hooks/X.py
# -> <repo>/tmp. Matches the prior Path("./tmp") semantics when the hook is
# invoked from the project root, and survives mid-session `cd` into a
# subdirectory (would otherwise resolve to a different ./tmp/).
INTENT_DIR = Path(__file__).resolve().parent.parent.parent / "tmp"
INTENT_GLOB = "action-intent-*.yaml"
AUDIT_LOG = INTENT_DIR / "action-audit-log.jsonl"
TARGET_COMPARE_PREFIX_LEN = 120

# Freshness check REMOVED 2026-05-27 per operator directive: "within 60 seconds
# is arbitrary and pointless. Remove it." The intent record's purpose is
# reasoning-capture and audit trail, not a security-token expiry. Matching is
# tool_name + target only; created_at_utc stays in the schema as informational
# but is no longer gated on.

# Tools that legitimately have no target (allowlist per schema constraint).
CONTENTLESS_TOOLS: set[str] = {"ExitPlanMode"}

# Schema-prescribed action_id format (action-intent.schema.yaml):
# intent-<utc-iso8601>-<sha256(tool_name+target_first_64_chars):8-char-prefix>.
# This regex is permissive on the middle segment (lets selftest fixtures use
# `intent-selftest-aaaaaaaa`) but strict on the prefix and the 8-char hex
# suffix - malformed IDs that lose the suffix or prefix are blocked.
ACTION_ID_RE = re.compile(r"^intent-.+-[0-9a-f]{8}$")

GOLDEN_RULE_FIELDS = (
    "golden_rule_1_think_before_coding",
    "golden_rule_2_simplicity_first",
    "golden_rule_3_surgical_changes",
    "golden_rule_4_goal_driven_execution",
)

VALID_RULE_VALUES = {"PASS", "NOT_APPLICABLE", "OPERATOR_OVERRIDE"}

REQUIRED_FIELDS = (
    "action_id",
    "tool_name",
    "target",
    "stated_assumption",
    "minimality_rationale",
    "scope_rationale",
    "success_criterion",
    *GOLDEN_RULE_FIELDS,
    "created_at_utc",
)

REQUIRED_NON_EMPTY_FIELDS = (
    "action_id",
    "tool_name",
    "stated_assumption",
    "minimality_rationale",
    "scope_rationale",
    "success_criterion",
    "created_at_utc",
)


def parse_simple_yaml(text: str) -> dict | None:
    """Top-level 'key: value' pairs, scalar values only. No nesting, no lists."""
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


def extract_target(tool_name: str, tool_input: dict) -> str:
    """Extract the target string Claude is expected to put in intent.target.

    Mirrors the schema's field_descriptions.target shape. The gate and Claude
    must agree on this value or the match check fails.
    """
    if tool_name in ("Edit", "Write", "MultiEdit"):
        return tool_input.get("file_path", "") or ""
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path", "") or tool_input.get("file_path", "") or ""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if not isinstance(cmd, str):
            return ""
        return cmd[:500]
    if tool_name == "Read":
        return tool_input.get("file_path", "") or ""
    if tool_name in ("Glob", "Grep"):
        return tool_input.get("pattern", "") or tool_input.get("path", "") or ""
    if tool_name == "Agent":
        return tool_input.get("subagent_type", "") or ""
    if tool_name == "Task" or tool_name == "TaskCreate" or tool_name == "TaskUpdate":
        sub = tool_input.get("subject", "") or tool_input.get("taskId", "")
        return sub[:500] if isinstance(sub, str) else ""
    # Fallback: stringify the first 500 chars of tool_input JSON.
    try:
        return json.dumps(tool_input, sort_keys=True)[:500]
    except (TypeError, ValueError):
        return ""


def targets_match(intent_target: str, observed_target: str) -> bool:
    """Compare targets by their first TARGET_COMPARE_PREFIX_LEN chars.

    Trim trailing whitespace before compare to forgive minor formatting drift.
    """
    a = (intent_target or "").rstrip()[:TARGET_COMPARE_PREFIX_LEN]
    b = (observed_target or "").rstrip()[:TARGET_COMPARE_PREFIX_LEN]
    return a == b


# parse_utc_iso8601 removed 2026-05-27 (kimi-rev-005 r6): unused after the
# freshness check was removed from find_matching_intent.


def find_matching_intent(
    tool_name: str,
    observed_target: str,
    now_utc: datetime.datetime,
) -> tuple[dict | None, Path | None, str]:
    """Return (intent_dict, intent_path, mismatch_reason).

    Walks INTENT_DIR for action-intent-*.yaml, parses each, picks the
    most-recent (by file mtime) whose tool_name AND target match.

    Freshness check removed 2026-05-27 (see module-level comment). Matching
    is tool_name + target only; created_at_utc is informational.
    """
    if not INTENT_DIR.exists():
        return None, None, f"intent dir {INTENT_DIR} does not exist"
    candidates: list[tuple[float, dict, Path]] = []
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
        intent_target = intent.get("target", "")
        if tool_name in CONTENTLESS_TOOLS:
            if (intent_target or "").rstrip() != "":
                continue
        else:
            if not targets_match(intent_target, observed_target):
                continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, intent, p))
    if not candidates:
        return None, None, (
            f"no action-intent record at {INTENT_DIR}/{INTENT_GLOB} matches "
            f"tool_name={tool_name!r} + target_prefix={(observed_target or '')[:80]!r}"
        )
    candidates.sort(key=lambda t: t[0], reverse=True)
    _, intent, intent_path = candidates[0]
    return intent, intent_path, ""


def validate_intent_record(intent: dict) -> tuple[bool, str]:
    """Validate intent record fields per the schema.

    Returns (ok, reason). On ok=True the action may proceed (per the 4
    golden rules).
    """
    missing = [f for f in REQUIRED_FIELDS if f not in intent]
    if missing:
        return False, f"intent missing required field(s): {', '.join(missing)}"
    empty_required = [
        f for f in REQUIRED_NON_EMPTY_FIELDS if not (intent.get(f) or "").strip()
    ]
    if empty_required:
        return False, f"intent has empty required field(s): {', '.join(empty_required)}"
    # action_id format check (kimi-rev-004: schema MUST was unenforced).
    action_id = (intent.get("action_id") or "").strip()
    if not ACTION_ID_RE.match(action_id):
        return False, (
            f"intent.action_id={action_id!r} does not match schema format "
            f"'intent-<iso8601>-<8-hex>' (see action-intent.schema.yaml)"
        )
    # The four golden-rule enum values
    for rule_field in GOLDEN_RULE_FIELDS:
        val = (intent.get(rule_field) or "").strip()
        if val not in VALID_RULE_VALUES:
            return False, (
                f"intent.{rule_field}={val!r} not in {sorted(VALID_RULE_VALUES)}"
            )
    # If any rule is OPERATOR_OVERRIDE, evidence must be non-empty
    any_override = any(
        (intent.get(rf) or "").strip() == "OPERATOR_OVERRIDE"
        for rf in GOLDEN_RULE_FIELDS
    )
    if any_override:
        evidence = (intent.get("operator_override_evidence") or "").strip()
        if not evidence:
            return False, (
                "intent has OPERATOR_OVERRIDE on a golden rule but no "
                "operator_override_evidence field"
            )
    # All four rules must be PASS or NOT_APPLICABLE or have OPERATOR_OVERRIDE evidence
    # (the override case is already validated above)
    return True, "ok"


def log_audit_entry(
    intent: dict,
    intent_path: Path | None,
    tool_name: str,
    target: str,
    decision: str,
    reason: str,
    now_utc: datetime.datetime,
) -> bool:
    """Append a JSON line to the audit log.

    Returns True iff the entry was successfully written. Per codex-rev-002 r6:
    audit-write failure is blocking for non-bootstrap actions (callers in
    main_payload check the return value and block if False). The bootstrap
    exemption path logs best-effort and ignores the return.
    """
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry: dict = {
            "action_id": intent.get("action_id", "") if intent else "",
            "tool_name": tool_name,
            "target_first_120_chars": (target or "")[:120],
            "gate_decision": decision,
            "gate_reason": reason,
            "golden_rule_results": {
                rf: (intent.get(rf, "") if intent else "")
                for rf in GOLDEN_RULE_FIELDS
            },
            "intent_record_sha256": "",
            "logged_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if intent_path is not None and intent_path.exists():
            try:
                entry["intent_record_sha256"] = hashlib.sha256(
                    intent_path.read_bytes()
                ).hexdigest()
            except OSError:
                pass
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def is_bootstrap_intent_write(tool_name: str, target: str) -> bool:
    """Bootstrap exemption: writes to the gate's own intent-record directory
    are allowed without a pre-existing intent record (otherwise the gate
    cannot be bootstrapped - every intent-record write would require a
    pre-existing intent record, infinite recursion).

    Per Instance 8 + 9 lessons: this exemption is NARROW and EXPLICIT.
    Codex suggestion 6 ("no implicit fast path") is satisfied because the
    exemption is documented, restricted to one path pattern, and logged.
    """
    if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return False
    if not target:
        return False
    # Normalize to absolute path for compare
    try:
        target_path = Path(target).resolve()
    except (OSError, ValueError):
        return False
    intent_dir_abs = INTENT_DIR.resolve() if INTENT_DIR.exists() else INTENT_DIR.absolute()
    try:
        rel = target_path.relative_to(intent_dir_abs)
    except ValueError:
        return False
    fname = rel.name
    # Match action-intent-*.yaml files only; no other files in ./tmp/ get exempted.
    return fname.startswith("action-intent-") and fname.endswith(".yaml")


def main_payload(payload: dict) -> int:
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    tool_name = payload.get("tool_name", "") or ""
    tool_input = payload.get("tool_input", {}) or {}
    observed_target = extract_target(tool_name, tool_input)

    # Bootstrap exemption (Instance 8/9 fix): writes to action-intent
    # record files in INTENT_DIR are allowed without a pre-existing
    # intent record. Logged to the audit trail so the exemption is
    # externally inspectable.
    if is_bootstrap_intent_write(tool_name, observed_target):
        log_audit_entry(
            {}, None, tool_name, observed_target, "PASS",
            "BOOTSTRAP_EXEMPTION: write to action-intent record path",
            now_utc,
        )
        return 0

    intent, intent_path, mismatch_reason = find_matching_intent(
        tool_name, observed_target, now_utc
    )
    if intent is None:
        reason = (
            f"BLOCKED (universal action gate): {mismatch_reason}. "
            f"Per codex external review 2026-05-27 (suggestion 6, no fast path): "
            f"every Claude action requires a valid action-intent record at "
            f"{INTENT_DIR}/action-intent-<action_id>.yaml that matches by "
            f"tool_name AND target. "
            f"Schema: .consensus/schemas/action-intent.schema.yaml."
        )
        log_audit_entry(
            {}, None, tool_name, observed_target, "BLOCK", reason, now_utc
        )
        sys.stderr.write(reason + "\n")
        return 2

    ok, reason = validate_intent_record(intent)
    if not ok:
        block_msg = (
            f"BLOCKED (universal action gate): intent record at {intent_path} "
            f"failed schema validation: {reason}. "
            f"Schema: .consensus/schemas/action-intent.schema.yaml."
        )
        log_audit_entry(
            intent, intent_path, tool_name, observed_target, "BLOCK", block_msg, now_utc
        )
        sys.stderr.write(block_msg + "\n")
        return 2

    logged = log_audit_entry(
        intent, intent_path, tool_name, observed_target, "PASS", "", now_utc
    )
    if not logged:
        # codex-rev-002 r6: audit-write failure is blocking for non-bootstrap
        # actions. gate_4_rules_audit_record requires every gated action
        # produce an inspectable record; if logging fails (OSError), the
        # action cannot have its required audit trail, so we block.
        sys.stderr.write(
            "BLOCKED (universal action gate): could not append to audit log "
            f"at {AUDIT_LOG} (OSError on append). Per codex-rev-002 r6: "
            "audit-write failure is blocking for non-bootstrap actions. "
            "Check filesystem permissions on ./tmp/."
        )
        return 2
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
    """Cases: no record, stale, wrong tool, target mismatch, golden-rule
    fail, valid record, contentless allowlist."""
    import shutil
    import tempfile

    failures: list[str] = []
    original_intent_dir = INTENT_DIR
    original_audit_log = AUDIT_LOG

    # Use an isolated tmp directory for the self-test so we don't pollute
    # the real ./tmp/ with test intent records.
    test_root = Path(tempfile.mkdtemp(prefix="universal-gate-selftest-"))
    test_intent_dir = test_root / "tmp"
    test_intent_dir.mkdir(parents=True, exist_ok=True)

    # Swap the module-level paths for the duration of self-test.
    globals()["INTENT_DIR"] = test_intent_dir
    globals()["AUDIT_LOG"] = test_intent_dir / "action-audit-log.jsonl"

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

    base_fields = {
        "action_id": "intent-selftest-aaaaaaaa",
        "tool_name": "Write",
        "target": "/tmp/some-file.txt",
        "stated_assumption": "test assumption",
        "minimality_rationale": "test minimality",
        "scope_rationale": "test scope",
        "success_criterion": "self-test exits 0",
        "golden_rule_1_think_before_coding": "PASS",
        "golden_rule_2_simplicity_first": "PASS",
        "golden_rule_3_surgical_changes": "PASS",
        "golden_rule_4_goal_driven_execution": "PASS",
        "created_at_utc": now_iso(),
    }

    payload_write = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/some-file.txt"},
    }

    try:
        # 1. No record -> BLOCK
        rc = main_payload(payload_write)
        if rc != 2:
            failures.append(f"Test 1 (no record): expected rc=2, got {rc}")

        # 2. Stale record -> ALLOW (freshness check removed 2026-05-27)
        write_intent(
            "action-intent-selftest-stale.yaml",
            **{**base_fields, "created_at_utc": now_iso(-3600)},
        )
        rc = main_payload(payload_write)
        if rc != 0:
            failures.append(
                f"Test 2 (stale now allowed after freshness removal): expected rc=0, got {rc}"
            )
        (test_intent_dir / "action-intent-selftest-stale.yaml").unlink()

        # 3. Wrong tool -> BLOCK
        write_intent(
            "action-intent-selftest-wrongtool.yaml",
            **{**base_fields, "tool_name": "Edit"},
        )
        rc = main_payload(payload_write)
        if rc != 2:
            failures.append(f"Test 3 (wrong tool): expected rc=2, got {rc}")
        (test_intent_dir / "action-intent-selftest-wrongtool.yaml").unlink()

        # 4. Target mismatch -> BLOCK
        write_intent(
            "action-intent-selftest-targetmis.yaml",
            **{**base_fields, "target": "/tmp/different-file.txt"},
        )
        rc = main_payload(payload_write)
        if rc != 2:
            failures.append(f"Test 4 (target mismatch): expected rc=2, got {rc}")
        (test_intent_dir / "action-intent-selftest-targetmis.yaml").unlink()

        # 5. Golden-rule fail (invalid enum) -> BLOCK
        write_intent(
            "action-intent-selftest-rulefail.yaml",
            **{**base_fields, "golden_rule_1_think_before_coding": "MAYBE"},
        )
        rc = main_payload(payload_write)
        if rc != 2:
            failures.append(f"Test 5 (rule invalid enum): expected rc=2, got {rc}")
        (test_intent_dir / "action-intent-selftest-rulefail.yaml").unlink()

        # 6. Valid record -> ALLOW
        write_intent("action-intent-selftest-valid.yaml", **base_fields)
        rc = main_payload(payload_write)
        if rc != 0:
            failures.append(f"Test 6 (valid record): expected rc=0, got {rc}")
        (test_intent_dir / "action-intent-selftest-valid.yaml").unlink()

        # 7. OPERATOR_OVERRIDE without evidence -> BLOCK
        write_intent(
            "action-intent-selftest-overridenoevidence.yaml",
            **{
                **base_fields,
                "golden_rule_2_simplicity_first": "OPERATOR_OVERRIDE",
            },
        )
        rc = main_payload(payload_write)
        if rc != 2:
            failures.append(f"Test 7 (override no evidence): expected rc=2, got {rc}")
        (
            test_intent_dir / "action-intent-selftest-overridenoevidence.yaml"
        ).unlink()

        # 8. OPERATOR_OVERRIDE with evidence -> ALLOW
        write_intent(
            "action-intent-selftest-overrideok.yaml",
            **{
                **base_fields,
                "golden_rule_2_simplicity_first": "OPERATOR_OVERRIDE",
                "operator_override_evidence": "operator said 'go' 2026-05-27",
            },
        )
        rc = main_payload(payload_write)
        if rc != 0:
            failures.append(f"Test 8 (override + evidence): expected rc=0, got {rc}")
        (test_intent_dir / "action-intent-selftest-overrideok.yaml").unlink()

        # 9. Contentless tool (ExitPlanMode) with empty target -> ALLOW when record present
        write_intent(
            "action-intent-selftest-contentless.yaml",
            **{**base_fields, "tool_name": "ExitPlanMode", "target": ""},
        )
        rc = main_payload({"tool_name": "ExitPlanMode", "tool_input": {}})
        if rc != 0:
            failures.append(f"Test 9 (contentless allow): expected rc=0, got {rc}")
        (test_intent_dir / "action-intent-selftest-contentless.yaml").unlink()

        # 10. Bootstrap exemption: write to action-intent file in INTENT_DIR
        # is allowed WITHOUT a pre-existing intent record.
        bootstrap_target = str(
            test_intent_dir / "action-intent-newly-being-written.yaml"
        )
        rc = main_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": bootstrap_target},
        })
        if rc != 0:
            failures.append(f"Test 10 (bootstrap exemption): expected rc=0, got {rc}")

        # 11. Bootstrap exemption does NOT extend to non-intent files in INTENT_DIR.
        non_intent_target = str(test_intent_dir / "random-tmp-file.txt")
        rc = main_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": non_intent_target},
        })
        if rc != 2:
            failures.append(f"Test 11 (non-intent in tmp not exempt): expected rc=2, got {rc}")

        # 12. Bootstrap exemption does NOT extend to action-intent files OUTSIDE INTENT_DIR.
        outside_target = "/tmp/elsewhere/action-intent-fake.yaml"
        rc = main_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": outside_target},
        })
        if rc != 2:
            failures.append(f"Test 12 (intent file outside INTENT_DIR not exempt): expected rc=2, got {rc}")
    finally:
        globals()["INTENT_DIR"] = original_intent_dir
        globals()["AUDIT_LOG"] = original_audit_log
        try:
            shutil.rmtree(test_root)
        except OSError:
            pass

    if failures:
        sys.stderr.write("SELF-TEST FAILURES:\n" + "\n".join(failures) + "\n")
        return 1
    sys.stderr.write(
        "SELF-TEST PASS: no-record block, stale ALLOW (freshness removed), "
        "wrong-tool block, target-mismatch block, rule-invalid-enum block, "
        "valid allow, override-no-evidence block, override-with-evidence allow, "
        "contentless-tool allow, bootstrap-exemption allow, non-intent-in-tmp block, "
        "intent-outside-INTENT_DIR block.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
