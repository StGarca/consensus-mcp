"""consensus_mcp._iteration_paths - SINGLE SOURCE OF TRUTH for iteration
artifact names.

The round-keyed review-file naming contract used to live as inline f-strings,
globs, and regexes scattered across ~58 files; v1.40.1 fixed one counting bug
caused by that scatter and review found another latent mismatch. Every module
that names, globs, or parses an iteration artifact must go through the helpers
here instead of re-spelling the convention.

Naming contract
---------------
  canonical review : ``<family>-review.yaml``        (e.g. codex-review.yaml)
  pass review      : ``<family>-review-<pass>.yaml`` (e.g. codex-review-pass-001.yaml,
                                                      gemini-review-2.yaml)
  outcome          : ``iteration-outcome.yaml``
  review packet    : ``review-packet.yaml``

This module is intentionally dependency-free (stdlib ``pathlib`` + ``re``
only) so anything in the package - including bootstrap-time code - can import
it without cycles.
"""
from __future__ import annotations

import re
from pathlib import Path

ITERATION_OUTCOME_FILENAME = "iteration-outcome.yaml"
REVIEW_PACKET_FILENAME = "review-packet.yaml"

# Glob patterns. CANONICAL matches only bare `<fam>-review.yaml`; PASS matches
# only round/pass-keyed `<fam>-review-<pass>.yaml`; ALL matches both forms.
CANONICAL_REVIEW_GLOB = "*-review.yaml"
PASS_REVIEW_GLOB = "*-review-*.yaml"
ALL_REVIEW_GLOB = "*-review*.yaml"

# Matches a sealed review artifact filename, capturing the reviewer family.
# Accepts both the bare `<fam>-review.yaml` AND the round/pass-keyed
# `<fam>-review-<N>.yaml` form an adapter writes when reviewers seal under
# distinct pass_ids (the parallel-dispatch H3 seal-collision fix). `<fam>` is
# non-greedy so `kimi-review-4.yaml` -> 'kimi', not 'kimi-review-4'.
# (Ported verbatim from _design_approval._REVIEW_FILE_RE.)
REVIEW_FILE_RE = re.compile(r"^(?P<fam>.+?)-review(?:-.+)?\.yaml$")

# Seal-canonicalization variant: PASS-form filenames ONLY, with a restricted
# family charset and the ORIGINAL case preserved (the seal `prepare` step
# derives the canonical filename from the captured family without lowercasing).
# Ported verbatim from _seal_iteration._FAMILY_SUFFIX_RE - intentionally
# narrower than REVIEW_FILE_RE; do not merge the two.
PASS_REVIEW_FILE_RE = re.compile(
    r"^(?P<family>[a-zA-Z0-9_-]+?)-review-(?P<rest>.+)\.yaml$"
)


def canonical_review_name(family: str) -> str:
    """The canonical sealed-review filename for a reviewer family."""
    return f"{family}-review.yaml"


def canonical_review_glob(iter_dir: Path) -> list[Path]:
    """Sorted canonical review files (`<fam>-review.yaml`) in an iteration dir."""
    return sorted(iter_dir.glob(CANONICAL_REVIEW_GLOB))


def pass_review_glob(iter_dir: Path) -> list[Path]:
    """Sorted pass review files (`<fam>-review-<pass>.yaml`) in an iteration dir."""
    return sorted(iter_dir.glob(PASS_REVIEW_GLOB))


def all_review_files(iter_dir: Path) -> list[Path]:
    """Sorted review files of BOTH forms (canonical + pass) in an iteration dir."""
    return sorted(iter_dir.glob(ALL_REVIEW_GLOB))


def review_family(filename: str) -> str | None:
    """Reviewer family from a sealed-review filename, or None if not a review
    artifact. Handles `<fam>-review.yaml` and `<fam>-review-<N>.yaml`; the
    family is normalized (stripped + lowercased)."""
    m = REVIEW_FILE_RE.match(filename)
    if not m:
        return None
    return m.group("fam").strip().lower() or None


def pass_review_family(filename: str) -> str | None:
    """Reviewer family from a PASS-form filename ONLY (original case kept).

    Returns None for canonical names and for families containing characters
    outside ``[a-zA-Z0-9_-]`` - the exact semantics the seal `prepare` step
    relies on when deriving `<family>-review.yaml` from a pass file."""
    m = PASS_REVIEW_FILE_RE.match(filename)
    if not m:
        return None
    return m.group("family")


def iteration_outcome_path(iter_dir: Path) -> Path:
    """Path of the authoritative iteration-outcome.yaml in an iteration dir."""
    return iter_dir / ITERATION_OUTCOME_FILENAME


def review_packet_path(iter_dir: Path) -> Path:
    """Path of the review-packet.yaml in an iteration dir."""
    return iter_dir / REVIEW_PACKET_FILENAME
