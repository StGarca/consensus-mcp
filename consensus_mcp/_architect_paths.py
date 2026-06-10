"""Single source of truth for architect-build (workflow D) artifact layout.

Mirrors the _iteration_paths doctrine: every module that touches a workflow-D
artifact name imports it from here; inline f-strings of artifact names are
forbidden. Dependency-light by design (stdlib + yaml + _atomic_io only).

Goal directory layout (per ratified spec section 7):

  <repo>/.consensus/architect/<goal-id>/
    problem.md            operator-authored problem statement
    spec.yaml             architect-authored spec (sealed)
    spec-rev-N.yaml       pushback-driven revisions (sealed)
    spec-approval.yaml    human spec gate seal
    dispatch-in-flight.yaml  atomic in-flight lock (consult Q3)
    HANDOFF.md            rolling-window digest the architect reads
    outcome.yaml          closing_state terminal seal
    integrity-before.yaml main-repo snapshot (latest builder dispatch)
    lane/                 git worktree (builder writes here, ONLY here)
    cycle-<N>/
      build-result.yaml   sealed builder output
      verification.yaml   frozen-gate record
      review.yaml         sealed reviewer output
      ruling.yaml         sealed architect ruling (or mechanical RED revise)
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from pathlib import Path

import yaml

from consensus_mcp._atomic_io import atomic_write_text

GOAL_ROOT_PARTS = (".consensus", "architect")
PROBLEM_FILENAME = "problem.md"
SPEC_FILENAME = "spec.yaml"
SPEC_REV_RE = re.compile(r"^spec-rev-(\d+)\.yaml$")
SPEC_APPROVAL_FILENAME = "spec-approval.yaml"
IN_FLIGHT_FILENAME = "dispatch-in-flight.yaml"
HANDOFF_FILENAME = "HANDOFF.md"
OUTCOME_FILENAME = "outcome.yaml"
INTEGRITY_BEFORE_FILENAME = "integrity-before.yaml"
LANE_DIRNAME = "lane"
CYCLE_DIR_RE = re.compile(r"^cycle-(\d+)$")
BUILD_RESULT_FILENAME = "build-result.yaml"
VERIFICATION_FILENAME = "verification.yaml"
REVIEW_FILENAME = "review.yaml"
RULING_FILENAME = "ruling.yaml"

_GOAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ArchitectPathError(ValueError):
    """Raised on an illegal goal id or malformed goal-dir layout."""


def goal_dir(repo_root: Path, goal_id: str) -> Path:
    # fullmatch, not match: a $ anchor accepts a trailing newline, which
    # would embed a newline in the directory name (mkdir-fatal on Windows).
    if not isinstance(goal_id, str) or not _GOAL_ID_RE.fullmatch(goal_id or ""):
        raise ArchitectPathError(
            f"illegal goal_id {goal_id!r}: must match {_GOAL_ID_RE.pattern} "
            f"(no path separators, no leading dot)"
        )
    return Path(repo_root).joinpath(*GOAL_ROOT_PARTS, goal_id)


def lane_dir(goal: Path) -> Path:
    return Path(goal) / LANE_DIRNAME


def cycle_dir(goal: Path, n: int) -> Path:
    return Path(goal) / f"cycle-{int(n)}"


def spec_path(goal: Path) -> Path:
    return Path(goal) / SPEC_FILENAME


def latest_spec_path(goal: Path) -> Path:
    """spec-rev-N.yaml with the highest N, else spec.yaml."""
    goal = Path(goal)
    best_n, best = -1, goal / SPEC_FILENAME
    try:
        names = [p.name for p in goal.iterdir()]
    except OSError:
        names = []
    for name in names:
        m = SPEC_REV_RE.match(name)
        if m and int(m.group(1)) > best_n:
            best_n, best = int(m.group(1)), goal / name
    return best


def current_cycle(goal: Path) -> int:
    """Highest cycle-N whose ruling is sealed, plus one; else that N.

    A cycle is CLOSED when its ruling.yaml exists with disposition=revise
    (advance) - accept/kill terminate the loop elsewhere, so they do not
    advance the counter. No cycle dirs at all -> cycle 1.
    """
    goal = Path(goal)
    highest = 0
    closed_highest = False
    try:
        entries = list(goal.iterdir())
    except OSError:
        entries = []
    for p in entries:
        m = CYCLE_DIR_RE.match(p.name)
        if not m:
            continue
        n = int(m.group(1))
        if n > highest:
            highest = n
            ruling = _read_yaml_or_empty(p / RULING_FILENAME)
            closed_highest = ruling.get("disposition") == "revise"
    if highest == 0:
        return 1
    return highest + 1 if closed_highest else highest


def _read_yaml_or_empty(path: Path) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Volatile seal-provenance stamps stripped before hashing, mirroring
# _VOLATILE_SEAL_FIELDS in tools/review_write_and_seal.py (which uses
# packet_sha256 for review packets; workflow-D seals use payload_sha256).
_VOLATILE_SEAL_FIELDS = ("sealed_at_utc", "payload_sha256")


# canonical_yaml_sha256 formula (see also: tools/review_write_and_seal.py,
# tools/state_read_decision_ledger.py, tools/audit_append_event.py).
# Double round-trip: yaml.safe_dump -> yaml.safe_load -> yaml.safe_dump ensures
# any Python-object quirks (ordered vs unordered dicts, aliases) are normalized
# before hashing. sort_keys=True makes hash order-independent. Keep the
# spelling byte-identical across the four sites so later architect-gate
# verification that reuses the established formula hashes identically.
def _canonical_yaml_sha256(obj) -> str:
    """Canonical SHA-256 of a Python object per spec section 7."""
    canonical = yaml.safe_dump(yaml.safe_load(yaml.safe_dump(obj)), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def seal_artifact(path: Path, payload: dict) -> dict:
    """Stamp sealed_at_utc + payload_sha256 onto payload, atomic-write YAML.

    Any pre-existing seal stamps on the input (the normal load-sealed-file ->
    revise -> re-seal flow for spec-rev-N.yaml) are stripped first; the sha is
    then computed over the canonical (sorted-keys) YAML of the payload BEFORE
    stamping, so re-reading and re-hashing the payload fields (minus the two
    stamps) reproduces it. Returns the stamped dict.
    """
    body = dict(payload)
    for f in _VOLATILE_SEAL_FIELDS:
        body.pop(f, None)
    stamped = dict(
        body,
        sealed_at_utc=_utcnow(),
        payload_sha256=_canonical_yaml_sha256(body),
    )
    atomic_write_text(
        Path(path),
        yaml.safe_dump(stamped, sort_keys=False, default_flow_style=False),
    )
    return stamped
