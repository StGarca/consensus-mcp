"""v1.20.0 HostPeerAdapter tests (TDD: RED -> GREEN).

The host_peer contributor is a SAME-FAMILY blind SWE-reviewer:

  * It runs the host's own AI family (e.g. claude when claude hosts) as a
    fresh-context adversarial reviewer via a DEDICATED host review callback
    (NOT the orchestrator's claude_artifact_callback).
  * Its sealed artifact provenance MUST carry: family == host family,
    role == swe_reviewer, weight == supplementary, gate_eligible == False, and
    an independence_attestation{method, fresh_context, no_peer_review_visible_at_dispatch}.
  * It is EXCLUDED from the cross-family closure invariant - see
    test_closure_invariant.py for the load-bearing regression.

Conventions mirror test_engine_factory.py / test_results_log.py: redirect the
state root via CONSENSUS_MCP_STATE_ROOT (so T6 seals land in a tmp dir), and
build a fake host review callback that returns a deterministic review dict.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from consensus_mcp import _engine_factory as factory
from consensus_mcp import config as cfg
from consensus_mcp.contributors.base import DispatchPacket, DispatchError, PHASE_REVIEW
from consensus_mcp.contributors.host_peer_adapter import HostPeerAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_t6_factory(tmp_path: Path):
    """Mock T6 (review.write_and_seal) the way the ClaudeAdapter tests do:
    write an archive file named EXACTLY as the real seal pipeline names it
    (M1 S5: via _bounded_seal_filename, which caps long names - the adapter's
    confinement check recomputes and requires an exact match), so the fixture
    mirrors the real T6 contract. Keeps the adapter unit test focused on the
    adapter, not the real seal pipeline."""
    from consensus_mcp.tools.review_write_and_seal import _bounded_seal_filename

    def fake_t6(iteration_id, reviewer_id, pass_id, packet):
        fname = _bounded_seal_filename("2026-05-22", iteration_id, reviewer_id, pass_id)
        archive_path = tmp_path / fname
        archive_path.write_text(yaml.safe_dump(packet), encoding="utf-8")
        return {"sealed_path": str(archive_path), "packet_sha256": "fakehash"}
    return fake_t6


def _make_packet(iter_dir: Path) -> DispatchPacket:
    iter_dir.mkdir(parents=True, exist_ok=True)
    goal_packet = iter_dir / "goal_packet.yaml"
    goal_packet.write_text(yaml.safe_dump({"goal": {"summary": "x"}}), encoding="utf-8")
    review_target = iter_dir / "review-target.md"
    review_target.write_text("the change under review\n", encoding="utf-8")
    return DispatchPacket(
        phase=PHASE_REVIEW,
        contributor="claude-swe-reviewer",
        iteration_dir=iter_dir,
        goal_packet_path=goal_packet,
        review_target_path=review_target,
        reviewer_id=None,
        pass_id=None,
    )


def _clean_review_callback(packet: DispatchPacket) -> dict:
    """A fake host review callback returning a clean (no-finding) review."""
    return {
        "findings": [],
        "goal_satisfied": True,
        "goal_satisfied_rationale": "no defects from fresh-context review",
        "blocking_objections": [],
    }


# ---------------------------------------------------------------------------
# Provenance + sealing
# ---------------------------------------------------------------------------


def test_host_peer_seals_artifact_with_supplementary_provenance(tmp_path):
    """HostPeerAdapter with a fake callback produces a sealed artifact whose
    provenance is family==host, role=swe_reviewer, gate_eligible=False,
    weight=supplementary."""
    iter_dir = tmp_path / "active" / "iteration-host-peer-1"
    packet = _make_packet(iter_dir)

    adapter = HostPeerAdapter(
        adapter_config={"family": "claude", "role": "swe_reviewer"},
        host_peer_review_callback=_clean_review_callback,
    )
    with patch("consensus_mcp.tools.review_write_and_seal.handle",
               side_effect=_fake_t6_factory(tmp_path)):
        sealed = adapter.dispatch(packet)

    parsed = sealed.parsed
    prov = parsed.get("dispatch_provenance") or {}
    assert prov.get("family") == "claude"
    assert prov.get("role") == "swe_reviewer"
    assert prov.get("weight") == "supplementary"
    assert prov.get("gate_eligible") is False
    # Top-level provenance the closure invariant + results-log key on.
    assert parsed.get("gate_eligible") is False
    assert parsed.get("weight") == "supplementary"
    actor = parsed.get("actor") or {}
    assert actor.get("model_family") == "claude"
    assert actor.get("role") == "swe_reviewer"

    att = prov.get("independence_attestation") or parsed.get("independence_attestation") or {}
    assert att.get("method") == "host_peer_callback"
    assert att.get("fresh_context") is True
    assert att.get("no_peer_review_visible_at_dispatch") is True

    assert sealed.sealed_path.exists()


def test_host_peer_runtime_isolation_absent_by_default(tmp_path):
    """Backward-compat: with no subagent flag, method stays host_peer_callback
    and NO runtime_isolation field is stamped (existing consumers unchanged)."""
    iter_dir = tmp_path / "active" / "iteration-host-peer-noiso"
    packet = _make_packet(iter_dir)
    adapter = HostPeerAdapter(
        adapter_config={"family": "claude", "role": "swe_reviewer"},
        host_peer_review_callback=_clean_review_callback,
    )
    with patch("consensus_mcp.tools.review_write_and_seal.handle",
               side_effect=_fake_t6_factory(tmp_path)):
        sealed = adapter.dispatch(packet)
    att = sealed.parsed["independence_attestation"]
    assert att["method"] == "host_peer_callback"
    assert "runtime_isolation" not in att


def test_host_peer_runtime_isolation_subagent_flag(tmp_path):
    """v1.21: when the review was dispatched as a Claude Code subagent, an
    ADDITIVE runtime_isolation: claude_code_subagent field is recorded in the
    independence_attestation + dispatch_provenance. method stays host_peer_callback."""
    iter_dir = tmp_path / "active" / "iteration-host-peer-iso"
    packet = _make_packet(iter_dir)
    adapter = HostPeerAdapter(
        adapter_config={
            "family": "claude",
            "role": "swe_reviewer",
            "runtime_isolation": "claude_code_subagent",
        },
        host_peer_review_callback=_clean_review_callback,
    )
    with patch("consensus_mcp.tools.review_write_and_seal.handle",
               side_effect=_fake_t6_factory(tmp_path)):
        sealed = adapter.dispatch(packet)
    parsed = sealed.parsed
    att = parsed["independence_attestation"]
    prov = parsed["dispatch_provenance"]
    # method UNCHANGED for backward-compat.
    assert att["method"] == "host_peer_callback"
    # additive isolation provenance.
    assert att["runtime_isolation"] == "claude_code_subagent"
    assert prov["independence_attestation"]["runtime_isolation"] == "claude_code_subagent"


def test_host_peer_callback_receives_only_packet_no_peer_artifacts(tmp_path):
    """Structural blindness: the callback is invoked with ONLY the DispatchPacket
    (no revealed peer artifacts)."""
    iter_dir = tmp_path / "active" / "iteration-host-peer-blind"
    packet = _make_packet(iter_dir)

    seen = {}

    def _spy_callback(p):
        seen["arg"] = p
        return _clean_review_callback(p)

    adapter = HostPeerAdapter(
        adapter_config={"family": "claude", "role": "swe_reviewer"},
        host_peer_review_callback=_spy_callback,
    )
    with patch("consensus_mcp.tools.review_write_and_seal.handle",
               side_effect=_fake_t6_factory(tmp_path)):
        adapter.dispatch(packet)

    # The single argument is the packet itself - nothing else.
    assert seen["arg"] is packet
    assert isinstance(seen["arg"], DispatchPacket)


def test_host_peer_no_callback_raises(tmp_path):
    """No callback wired -> dispatch raises DispatchError (the engine factory
    never builds it in this case, but the adapter itself must fail closed)."""
    iter_dir = tmp_path / "active" / "iteration-host-peer-nocb"
    packet = _make_packet(iter_dir)
    adapter = HostPeerAdapter(adapter_config={"family": "claude", "role": "swe_reviewer"})
    with pytest.raises(DispatchError):
        adapter.dispatch(packet)


# ---------------------------------------------------------------------------
# Engine-factory routing + graceful absence
# ---------------------------------------------------------------------------


def _config_with_host_peer():
    c = deepcopy(cfg.default_config())
    c["contributors"]["enabled"] = ["claude", "codex", "gemini", "claude-swe-reviewer"]
    c["contributors"]["profiles"] = {
        "claude-swe-reviewer": {
            "name": "claude-swe-reviewer",
            "kind": "host_peer",
            "family": "claude",
            "role": "swe_reviewer",
            "weight": "supplementary",
            "gate_eligible": False,
        }
    }
    return c


def test_engine_factory_routes_host_peer_when_callback_wired(tmp_path):
    """When host_peer_review_callback is provided, kind:host_peer -> HostPeerAdapter."""
    config = _config_with_host_peer()
    adapters = factory.build_adapters(
        config,
        claude_artifact_callback=lambda p: {
            "findings": [], "goal_satisfied": True, "blocking_objections": []},
        host_peer_review_callback=_clean_review_callback,
    )
    assert "claude-swe-reviewer" in adapters
    assert isinstance(adapters["claude-swe-reviewer"], HostPeerAdapter)


def test_engine_factory_skips_host_peer_when_no_callback(tmp_path):
    """No host_peer_review_callback -> host_peer is gracefully NOT built; the
    other (existing) contributors are unaffected."""
    config = _config_with_host_peer()
    adapters = factory.build_adapters(
        config,
        claude_artifact_callback=lambda p: {
            "findings": [], "goal_satisfied": True, "blocking_objections": []},
        # host_peer_review_callback intentionally omitted
    )
    assert "claude-swe-reviewer" not in adapters
    # Existing flows unaffected.
    assert set(adapters.keys()) == {"claude", "codex", "gemini"}


def test_existing_config_build_unchanged(tmp_path):
    """A config WITHOUT any host_peer builds exactly as before (no new key)."""
    c = deepcopy(cfg.default_config())
    c["contributors"]["enabled"] = ["claude", "codex", "gemini"]
    cfg.validate(c)
    adapters = factory.build_adapters(
        c,
        claude_artifact_callback=lambda p: {
            "findings": [], "goal_satisfied": True, "blocking_objections": []},
        host_peer_review_callback=_clean_review_callback,  # present but unused
    )
    assert set(adapters.keys()) == {"claude", "codex", "gemini"}
