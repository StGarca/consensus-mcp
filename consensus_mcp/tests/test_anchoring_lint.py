"""Tests for the mechanical anchoring/term-skew linter.

Decisive test: the linter flags the orchestrator's real failure mode — anchoring
one contributor (named repeatedly) while others are never mentioned — WITHOUT
relying on anyone noticing.
"""
from consensus_mcp import _anchoring_lint as al

# Contributor set is passed in (NOT hardcoded) — anchoring on a fixed list would
# be the very bias under guard.
CONTRIBUTORS = {"contributors": ["claude", "codex", "gemini", "kimi"]}


def test_flags_one_peer_anchoring():
    # The exact failure shape: gemini named many times; codex/claude/kimi zero.
    text = ("Mirror gemini. The kimi adapter wraps _dispatch_gemini and "
            "impersonates gemini; model it on gemini, the gemini dispatcher, "
            "the gemini path, gemini's seal, gemini timeouts.")
    flags = al.detect_anchoring(text, CONTRIBUTORS)
    assert len(flags) == 1, flags
    f = flags[0]
    assert f.skewed_to == "gemini"
    assert set(f.never_mentioned) >= {"codex", "claude"}
    assert f.skew_fraction >= 0.70


def test_balanced_references_no_flag():
    text = ("claude proposes, codex audits, gemini reviews, kimi reviews. "
            "claude codex gemini kimi each weigh in equally here.")
    assert al.detect_anchoring(text, CONTRIBUTORS) == []


def test_below_min_total_no_flag():
    text = "We dispatch gemini once."  # total refs < min_total
    assert al.detect_anchoring(text, CONTRIBUTORS) == []


def test_contributor_set_is_configurable():
    # A NON-default set (proves no hardcoding — works for any AI combination).
    custom = {"contributors": ["alpha", "beta", "gamma"]}
    # alpha dominates; beta/gamma genuinely absent (don't name them, or they count).
    text = "alpha alpha alpha alpha alpha drives the whole design; nothing else."
    flags = al.detect_anchoring(text, custom)
    assert len(flags) == 1 and flags[0].skewed_to == "alpha"


def test_no_never_mentioned_means_no_flag():
    # One peer dominates but ALL are mentioned at least once -> not the anchoring
    # signature (skew without exclusion is weaker; we require an excluded peer).
    text = ("gemini gemini gemini gemini gemini gemini gemini gemini "
            "claude codex kimi")
    flags = al.detect_anchoring(text, CONTRIBUTORS)
    assert flags == [], flags


def test_counts_are_word_boundary_case_insensitive():
    c = al.count_terms("Gemini GEMINI gemini codexes codex", ["gemini", "codex"])
    assert c["gemini"] == 3
    assert c["codex"] == 1  # 'codexes' must NOT match 'codex'


# --- Integration: exercise the REAL fallback (NOT hardcoded input) ---
# These guard the QA-found defect: the prior tests hardcoded kimi into the
# linter input, so they never proved the real _configured_contributors fallback
# actually includes kimi. It didn't (it used the enabled-3 default). Now fixed.

def test_real_fallback_contributor_set_includes_kimi():
    from consensus_mcp import _author_review_packet as arp
    contributors = arp._configured_contributors(None)  # no project config -> fallback
    assert "kimi" in contributors, (
        f"fallback contributor set excludes kimi -> linter would be blind to "
        f"kimi-anchoring (the exact bias it guards): {contributors}")


def test_kimi_anchoring_caught_via_real_fallback(tmp_path):
    from consensus_mcp import _author_review_packet as arp
    gp = tmp_path / "goal_packet.yaml"
    # kimi-anchored, others excluded — must be caught WITHOUT hardcoding the set.
    gp.write_text(
        "goal:\n  summary: route everything to kimi; the kimi adapter, kimi seal, "
        "kimi path, kimi timeout, kimi kimi kimi.\n", encoding="utf-8")
    audit = arp._anchoring_audit(tmp_path, None)  # real fallback set
    assert any(a["skewed_to"] == "kimi" for a in audit), (
        f"kimi-anchoring not caught via the real fallback: {audit}")
