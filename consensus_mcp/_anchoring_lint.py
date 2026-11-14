"""Mechanical anchoring/term-skew linter for consensus goal-packets.

Origin: iteration-orchestrator-framing-bias-2026-05-22 (4/4). The orchestrator
authors the goal_packet, so the orchestrator's framing/anchoring bias rides
through consensus uncaught - the 3 AI contributors reason WITHIN the frame and
their agreement echoes the orchestrator's prior. Across that session the
orchestrator repeatedly anchored one peer ("gemini" named many times; codex /
claude / kimi zero) and it was caught every time by the human, never by the AIs.

This linter is the DILIGENCE-INDEPENDENT safeguard: it flags lopsided references
to the configured contributor set (or any term group) MECHANICALLY, at packet-
author time, with NO reliance on anyone noticing. It does not depend on the
orchestrator's honesty - which is the compromised thing.

KEY: the contributor set is passed in (from config), never hardcoded - anchoring
on a fixed {claude,codex,gemini} list would be the very bias this guards against.

LIMITATIONS (be honest - this is a FIRST-LINE mechanical flag, not exhaustive;
the schema frame_audit + the independent QA verifier + the human catch the rest):
  - Catches literal name-spam with exclusion (peer X named >> peers Y,Z named 0).
    It does NOT catch PARAPHRASE/PRONOUN anchoring ("gemini" once, then "it / the
    reviewer / the dispatcher" repeatedly) - no alias map.
  - Single-top-term skew only: a TWO-peer in-group dominating an excluded
    out-group (Xx3 Yx3, Z,Wx0) is not flagged. Coverage/top-2 skew is a follow-up.
  - Thresholds (min_total, skew_fraction) are heuristics; tune from observed data.
A clean lint is NOT proof of no bias - it is the absence of the one pattern this
checks. (QA, 2026-05-22.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AnchoringFlag:
    group: str
    counts: dict[str, int]
    skewed_to: str
    skew_fraction: float
    never_mentioned: list[str]
    detail: str


def count_terms(text: str, terms: list[str]) -> dict[str, int]:
    """Word-boundary, case-insensitive counts for each term."""
    out: dict[str, int] = {}
    for t in terms:
        if not t:
            continue
        out[t] = len(re.findall(rf"\b{re.escape(t)}\b", text, flags=re.IGNORECASE))
    return out


def detect_anchoring(
    text: str,
    term_groups: dict[str, list[str]],
    *,
    min_total: int = 4,
    skew_fraction: float = 0.70,
) -> list[AnchoringFlag]:
    """Flag any term group whose references are lopsided.

    A group is flagged when, across its terms in `text`:
      - total references >= min_total (enough signal), AND
      - one term holds >= skew_fraction of them, AND
      - at least one OTHER term in the group is never mentioned.
    That pattern ("peer X named 9x; peers Y,Z never") is the anchoring signature.
    Members that should be balanced (e.g. the contributor set) but aren't =>
    likely orchestrator anchoring. Diligence-independent; runs at author time.
    """
    flags: list[AnchoringFlag] = []
    for group, terms in term_groups.items():
        terms = [t for t in (terms or []) if t]
        if len(terms) < 2:
            continue
        counts = count_terms(text, terms)
        total = sum(counts.values())
        if total < min_total:
            continue
        top = max(counts, key=counts.get)
        frac = counts[top] / total if total else 0.0
        never = [t for t in terms if counts[t] == 0]
        if frac >= skew_fraction and never:
            flags.append(AnchoringFlag(
                group=group, counts=counts, skewed_to=top,
                skew_fraction=round(frac, 3), never_mentioned=never,
                detail=(f"group {group!r} anchored on {top!r} "
                        f"({counts[top]}/{total} = {frac:.0%} of refs); "
                        f"never mentioned: {never}. Likely orchestrator anchoring "
                        f"-- treat all members as equal peers."),
            ))
    return flags
