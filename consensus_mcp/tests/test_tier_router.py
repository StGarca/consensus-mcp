"""Tests for the rule-based tier router (sp-consensus-optimization B3 core)."""
from __future__ import annotations

import pytest

from consensus_mcp import _tier_router as tr


def test_governance_surface_auto_upgrades_to_deep_locked():
    r = tr.classify(intent_class="hotfix", files_touched=1,
                    touches_governance_surface=True)
    assert r["tier"] == tr.DEEP and r["locked"] is True
    assert r["workflow"] == "A" and r["panel_size"] == 4 and r["path"] == "A"


def test_security_irreversible_auto_upgrades_to_deep_locked():
    r = tr.classify(intent_class="bounded_feature", files_touched=2,
                    touches_governance_surface=False, security_or_irreversible=True)
    assert r["tier"] == tr.DEEP and r["locked"] is True


def test_hotfix_single_file_is_quick():
    r = tr.classify(intent_class="hotfix", files_touched=1,
                    touches_governance_surface=False)
    assert r["tier"] == tr.QUICK and r["locked"] is False
    assert r["workflow"] == "B" and r["panel_size"] == 1


def test_multi_file_bounded_feature_is_standard():
    r = tr.classify(intent_class="bounded_feature", files_touched=4,
                    touches_governance_surface=False)
    assert r["tier"] == tr.STANDARD
    assert r["workflow"] == "A" and r["panel_size"] == 3 and r["path"] == "B"


def test_architectural_is_deep_but_not_locked():
    r = tr.classify(intent_class="architectural", files_touched=10,
                    touches_governance_surface=False)
    assert r["tier"] == tr.DEEP and r["locked"] is False


def test_locked_classification_refuses_downgrade():
    r = tr.classify(intent_class="hotfix", files_touched=1,
                    touches_governance_surface=True)  # DEEP + locked
    assert tr.is_downgrade_allowed(r, tr.QUICK) is False
    assert tr.is_downgrade_allowed(r, tr.STANDARD) is False
    assert tr.is_downgrade_allowed(r, tr.DEEP) is True  # same rigor ok


def test_unlocked_classification_allows_downgrade():
    r = tr.classify(intent_class="architectural", files_touched=10,
                    touches_governance_surface=False)  # DEEP, not locked
    assert tr.is_downgrade_allowed(r, tr.STANDARD) is True


def test_upgrade_always_allowed():
    r = tr.classify(intent_class="hotfix", files_touched=1,
                    touches_governance_surface=False)  # QUICK
    assert tr.is_downgrade_allowed(r, tr.DEEP) is True  # heavier is fine


def test_unknown_target_tier_raises():
    r = tr.classify(intent_class="hotfix", files_touched=1,
                    touches_governance_surface=False)
    with pytest.raises(ValueError):
        tr.is_downgrade_allowed(r, "ultra")
