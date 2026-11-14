"""Follow-up completeness gate (consensus-mcp 1.16.1).

Mechanically BINDS the EXISTING CLAUDE.md "Complete fulfillment" rule (do the
whole job) + Karpathy #4 "Goal-Driven Execution". This is NOT a new rule - the
rule already existed and still got skipped (an agent merged a version bump to
main but did not cut the GitHub release). Passive rules do not bind in the
moment; this is the same mechanical-enforcement pattern as the v1.16.0
delivery-readiness gate.

How it binds: a task declaring an `action_class` (e.g. version_bump) acquires
REQUIRED follow-ups from required_followups.yaml. The v1.16.0 delivery token
refuses to mint while any required follow-up is neither RESOLVED nor EXPLICITLY
DEFERRED-WITH-REASON. So "done" is impossible with an open follow-up.

Detection: the declared action_class is AUTHORITATIVE. Heuristics may only ADD
requirements (fail-toward-enforcement), never clear a declared one.

Ledger shape (.delivery-readiness/<key>.followups.json):
  {"action_classes": [...], "resolved": [...], "deferred": [{"item","reason"}]}
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Action-class -> required follow-ups. Embedded (not a data file) so it always
# ships with the package and needs no package-data wiring. Extend by editing
# this dict. Each follow-up is satisfied when RESOLVED or DEFERRED-WITH-REASON.
ACTION_FOLLOWUPS: dict[str, list[str]] = {
    "version_bump": ["tag", "github_release", "changelog_entry"],
    "merge_release_to_main": ["tag", "github_release"],
    "new_feature": ["tests", "docs", "changelog_entry"],
    "consensus_iteration_close": ["snapshot", "log_entry"],
}


def load_action_followups() -> dict[str, list[str]]:
    return dict(ACTION_FOLLOWUPS)


def required_followups(action_classes: list[str]) -> list[str]:
    """Union of required follow-ups across the declared action classes."""
    mapping = load_action_followups()
    out: list[str] = []
    for ac in action_classes or []:
        for item in mapping.get(ac, []):
            if item not in out:
                out.append(item)
    return out


def _ledger_path(key: str, repo_root: Path) -> Path:
    safe = hashlib.sha256(str(key).encode()).hexdigest()[:16]
    d = repo_root / ".delivery-readiness"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe}.followups.json"


def read_ledger(key: str, repo_root: Path) -> dict:
    p = _ledger_path(key, repo_root)
    if not p.exists():
        return {"action_classes": [], "resolved": [], "deferred": []}
    return json.loads(p.read_text(encoding="utf-8"))


def write_ledger(key: str, repo_root: Path, ledger: dict) -> None:
    _ledger_path(key, repo_root).write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def open_followups(action_classes: list[str], ledger: dict) -> list[str]:
    """Required follow-ups that are neither resolved nor deferred-with-reason."""
    req = required_followups(action_classes)
    resolved = set(ledger.get("resolved") or [])
    deferred = {d.get("item") for d in (ledger.get("deferred") or []) if d.get("item") and d.get("reason")}
    satisfied = resolved | deferred
    return [item for item in req if item not in satisfied]


def followups_complete(action_classes: list[str], ledger: dict) -> tuple[bool, list[str]]:
    """True iff every required follow-up is resolved or deferred-with-reason."""
    missing = open_followups(action_classes, ledger)
    return (not missing), missing
