"""FU-28: reviewers that echo envelope identity keys must not be rejected.

A real Gemini plan review was discarded because it repeated `iteration_id`
and `reviewer_id` at the top level. The sealed envelope records both from the
dispatcher, so scalar echoes are dropped; anything else stays strict.
"""
from __future__ import annotations

import json

import pytest

from consensus_mcp import _dispatch_codex, _dispatch_gemini, _dispatch_grok, _dispatch_kimi

PARSERS = [
    (_dispatch_codex._parse_codex_output, _dispatch_codex.CodexOutputParseError),
    (_dispatch_gemini._parse_gemini_output, _dispatch_gemini.GeminiOutputParseError),
    (_dispatch_grok._parse_grok_output, _dispatch_grok.GrokOutputParseError),
    (_dispatch_kimi._parse_kimi_output, _dispatch_kimi.KimiOutputParseError),
]
IDS = ["codex", "gemini", "grok", "kimi"]


def _review(**extra):
    body = {
        "findings": [],
        "goal_satisfied": True,
        "blocking_objections": [],
        "goal_satisfied_rationale": "Plan is complete for the stated goal.",
    }
    body.update(extra)
    return json.dumps(body)


@pytest.mark.parametrize("parse,err", PARSERS, ids=IDS)
def test_scalar_echoed_identity_keys_are_dropped(parse, err):
    parsed = parse(_review(iteration_id="iteration-x-plan-1", reviewer_id="gemini"))
    assert "iteration_id" not in parsed
    assert "reviewer_id" not in parsed
    assert parsed["goal_satisfied"] is True


@pytest.mark.parametrize("parse,err", PARSERS, ids=IDS)
def test_non_scalar_identity_echo_still_rejected(parse, err):
    with pytest.raises(err, match="unexpected top-level keys"):
        parse(_review(reviewer_id={"name": "gemini"}))


@pytest.mark.parametrize("parse,err", PARSERS, ids=IDS)
def test_other_unknown_top_level_keys_still_rejected(parse, err):
    with pytest.raises(err, match=r"unexpected top-level keys: \['proposal'\]"):
        parse(_review(iteration_id="iteration-x", proposal={"title": "x"}))
