"""v1.30.6 — synthesis-aware propose-converge (Path B guard + Path A helper)."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from consensus_mcp import config as cfg
from consensus_mcp.contributors.base import (
    FakeAlwaysApprove, FakeAlwaysBlock, SealedArtifact,
)
from consensus_mcp.workflow_engine import (
    ConvergenceOutcome, IterationOutcome, WorkflowEngine,
)


def _config(mode=cfg.WORKFLOW_PROPOSE_CONVERGE, rule=cfg.CONVERGE_STRICT_MAJ) -> dict:
    c = deepcopy(cfg.default_config())
    c["contributors"]["enabled"] = ["claude", "codex", "gemini"]
    c["workflow"]["mode"] = mode
    c["convergence"]["rule"] = rule
    c["convergence"]["finding_disposition"] = cfg.DISPOSITION_ALL_OR_NOTHING
    cfg.validate(c)
    return c


def _engine(tmp_path, approve=True) -> WorkflowEngine:
    adapters = {n: (FakeAlwaysApprove() if approve else FakeAlwaysBlock())
                for n in ["claude", "codex", "gemini"]}
    return WorkflowEngine(_config(), adapters, tmp_path)


def _goal(tmp_path, body: str) -> Path:
    g = tmp_path / "goal_packet.yaml"
    g.write_text(body, encoding="utf-8")
    return g


# ---------- Task 1: _requires_synthesis reader ----------

def test_requires_synthesis_true_when_declared(tmp_path):
    eng = _engine(tmp_path)
    g = _goal(tmp_path, "convergence:\n  requires_synthesis: true\n")
    assert eng._requires_synthesis(g) is True


def test_requires_synthesis_false_when_absent(tmp_path):
    eng = _engine(tmp_path)
    g = _goal(tmp_path, "goal:\n  summary: x\n")
    assert eng._requires_synthesis(g) is False


def test_requires_synthesis_false_on_nonbool_or_unreadable(tmp_path):
    eng = _engine(tmp_path)
    g = _goal(tmp_path, "convergence:\n  requires_synthesis: maybe\n")
    assert eng._requires_synthesis(g) is False
    assert eng._requires_synthesis(tmp_path / "nope.yaml") is False
