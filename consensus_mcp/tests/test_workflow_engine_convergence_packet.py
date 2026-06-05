"""Component 1 (consult iteration-approve-two-...-f641f060): the convergence
packet must embed the TARGET DOCUMENT under review, not just the proposal YAMLs.

Field evidence: a Codex-hosted converge round returned patch_proposal:null on all
three findings because the actual document was absent from the packet. Weighted
synthesis (4/4 approve): fold the target into the existing touched_files_contents
map, list it FIRST in defect_target.files, size-guard with the shared truncation
helper + marker. target_path stays OPTIONAL so existing callers are unaffected (A4).
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from consensus_mcp import config as cfg
from consensus_mcp.contributors.base import FakeAlwaysApprove
from consensus_mcp.workflow_engine import WorkflowEngine


def _engine(tmp_path: Path) -> WorkflowEngine:
    c = deepcopy(cfg.default_config())
    c["contributors"]["enabled"] = ["claude", "codex", "gemini"]
    c["workflow"]["mode"] = cfg.WORKFLOW_PROPOSE_CONVERGE
    c["convergence"]["finding_disposition"] = cfg.DISPOSITION_ALL_OR_NOTHING
    cfg.validate(c)
    adapters = {n: FakeAlwaysApprove() for n in ["claude", "codex", "gemini"]}
    return WorkflowEngine(c, adapters, tmp_path)


def _proposal_paths(iter_dir: Path) -> list[str]:
    paths = []
    for cname in ["gemini", "codex", "claude"]:
        p = iter_dir / f"{cname}-proposal.yaml"
        p.write_text(f"contributor: {cname}\n", encoding="utf-8")
        paths.append(str(p))
    return paths


def test_convergence_packet_embeds_target_document_first(tmp_path):
    """A1: target body is embedded under its repo-relative key, listed FIRST."""
    engine = _engine(tmp_path)
    iter_dir = tmp_path / "iter-embed"
    iter_dir.mkdir()
    target = tmp_path / "handoff" / "doc.md"
    target.parent.mkdir()
    target.write_text("# Speaker SOP\nLine under review.\n", encoding="utf-8")

    pkt = engine._build_convergence_packet(
        iter_dir, _proposal_paths(iter_dir), 1, target_path=target
    )
    doc = yaml.safe_load(pkt.read_text(encoding="utf-8"))
    files = doc["defect_target"]["files"]
    contents = doc["defect_target"]["touched_files_contents"]

    assert files[0] == "handoff/doc.md", "target must be listed first for scan order"
    assert contents["handoff/doc.md"] == "# Speaker SOP\nLine under review.\n"
    # proposals are still present alongside the target
    assert any(k.endswith("codex-proposal.yaml") for k in contents)


def test_convergence_packet_truncates_oversized_target(tmp_path):
    """A2: an oversized target is truncated with the shared marker (not omitted)."""
    engine = _engine(tmp_path)
    iter_dir = tmp_path / "iter-big"
    iter_dir.mkdir()
    target = tmp_path / "big.md"
    target.write_text("y" * 40000, encoding="utf-8")

    pkt = engine._build_convergence_packet(
        iter_dir, _proposal_paths(iter_dir), 1, target_path=target
    )
    doc = yaml.safe_load(pkt.read_text(encoding="utf-8"))
    stored = doc["defect_target"]["touched_files_contents"]["big.md"]

    assert len(stored) < 17000, f"target not capped: {len(stored)}"
    assert "truncated" in stored and "40000" in stored


def test_convergence_packet_without_target_is_unchanged(tmp_path):
    """A4: omitting target_path leaves the packet behavior identical (proposals only)."""
    engine = _engine(tmp_path)
    iter_dir = tmp_path / "iter-none"
    iter_dir.mkdir()
    pkt = engine._build_convergence_packet(iter_dir, _proposal_paths(iter_dir), 1)
    doc = yaml.safe_load(pkt.read_text(encoding="utf-8"))
    contents = doc["defect_target"]["touched_files_contents"]
    assert all(k.endswith("-proposal.yaml") for k in contents)
