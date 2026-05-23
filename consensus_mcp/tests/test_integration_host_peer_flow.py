"""END-TO-END integration test — the v1.21 "decisive experiment".

This module proves the v1.21 "full automated superpower->host integration" works
TOGETHER, not just in isolated unit tests. Each isolated piece already has unit
coverage (test_consensus_hooks.py, test_consensus_run_iteration.py,
test_agents_init.py); this module wires the pieces into one cohesive flow and
asserts the SEAMS hold:

  1. MARKER -> GATE   the PreToolUse design gate denies an Edit with NO sealed
                      marker (exit 2), and ALLOWS the SAME Edit once a GENUINE
                      cross-family-sealed iteration + minted .consensus/
                      design-approved marker exist (exit 0). The gate is wired to
                      the real T6 seal, not a self-asserted boolean.
  2. FORGE RESISTANCE a hand-written marker pointing at a NON-sealed iteration is
                      DENIED through the full subprocess hook path — the closure
                      invariant survives the whole pipeline.
  3. host_peer ACTIVATION  consensus.run_iteration drives the (formerly dormant)
                      host_peer path: an enabled host_peer profile + a valid
                      host_peer_review_yaml seals a host-peer artifact with
                      gate_eligible=false (the gate-safety provenance holds even
                      now that the path is live).
  4. SOFT-SKIP        same as #3 but no host_peer_review_yaml -> the iteration
                      still succeeds and names the skipped profile under
                      supplementary_skipped (no hard error — host_peer is
                      supplementary by construction).
  5. AGENT FILES      the two installed subagent files parse as frontmatter and
                      grant the `Agent` tool ONLY to the orchestrator (the
                      host-driven dispatch contract: only the neutral driver may
                      sub-dispatch; the read-only reviewer cannot).

Run ONLY via the consensus-mcp pipx venv:
  .../venvs/consensus-mcp/bin/python -m pytest \
      consensus_mcp/tests/test_integration_host_peer_flow.py -q

Style mirrors the three reference modules: hook scripts are exercised as
subprocesses (the real Claude Code invocation shape); run_iteration is driven via
the real `handle()` with Fake cross-family adapters + the REAL HostPeerAdapter;
the marker pieces reuse the `_make_sealed_iteration` / `_seal_marker` patterns;
agent files are parsed with the same frontmatter splitter as test_agents_init.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from consensus_mcp import _delivery_readiness as dr
from consensus_mcp import _design_approval as da
from consensus_mcp import _engine_factory as factory
from consensus_mcp import config as cfg
from consensus_mcp.tools import consensus_run_iteration as tool

# --------------------------------------------------------------------------- #
# Shared locations (mirrors test_consensus_hooks.py / test_agents_init.py).
# --------------------------------------------------------------------------- #

_HOOKS_DIR = (
    Path(__file__).resolve().parent.parent / "claude_extensions" / "hooks"
)
PRETOOLUSE = _HOOKS_DIR / "consensus_pretooluse_gate.py"

_AGENTS_DIR = (
    Path(__file__).resolve().parent.parent / "claude_extensions" / "agents"
)
_AGENT_FILES = ("consensus-orchestrator.md", "consensus-host-peer-reviewer.md")


# --------------------------------------------------------------------------- #
# Helpers — reused verbatim in spirit from the reference test modules so the
# integration assertions exercise the SAME on-disk shapes the unit tests do.
# --------------------------------------------------------------------------- #

def _run_hook(script: Path, event: dict, *, repo_root: Path,
              runtime: str = "present") -> subprocess.CompletedProcess:
    """Invoke a hook script with `event` on stdin (real Claude Code shape).
    `runtime` in {"present","absent"} forces the runtime probe deterministically.
    Lifted from test_consensus_hooks.py so the gate is driven identically."""
    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(repo_root)
    env.pop("CONSENSUS_MCP_FORCE_RUNTIME_ABSENT", None)
    env.pop("CONSENSUS_MCP_FORCE_RUNTIME_PRESENT", None)
    if runtime == "present":
        env["CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"] = "1"
    elif runtime == "absent":
        env["CONSENSUS_MCP_FORCE_RUNTIME_ABSENT"] = "1"
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(event),
        capture_output=True, text=True, env=env, timeout=60,
    )


def _make_sealed_iteration(repo_root: Path, ref: str = "iteration-fix-impl",
                           *, closing_state: str = "quorum_close_passed",
                           reviewers=("codex", "gemini")) -> str:
    """Build a GENUINE sealed cross-family iteration on disk so the marker pointer
    re-validates against a real T6 seal. Returns the converged-plan sha256.
    Same shape as test_consensus_hooks.py's helper."""
    iter_dir = repo_root / "consensus-state" / "active" / ref
    iter_dir.mkdir(parents=True, exist_ok=True)
    (iter_dir / "iteration-outcome.yaml").write_text(
        f"closing_state: {closing_state}\n", encoding="utf-8")
    plan = iter_dir / "converged-plan.yaml"
    plan.write_text("decision:\n  do: the thing\n", encoding="utf-8")
    for fam in reviewers:
        (iter_dir / f"{fam}-review.yaml").write_text(
            f"iteration_id: {ref}\nreviewer_id: {fam}-1\n", encoding="utf-8")
    return dr.compute_artifact_hash(plan)


def _seal_marker(repo_root: Path, scope_glob: str = "src/**",
                 *, sealed: bool = True) -> None:
    """Write a design-approval marker. sealed=True mints via the REAL
    `mint_design_approval` against a genuine sealed iteration; sealed=False forges
    a hand-written pointer at a NON-sealed iteration (so re-validation rejects it).
    Same shape as test_consensus_hooks.py's helper."""
    (repo_root / ".consensus").mkdir(parents=True, exist_ok=True)
    if sealed:
        plan_sha = _make_sealed_iteration(repo_root, "iteration-fix-impl")
        da.mint_design_approval(
            repo_root=repo_root, design_consensus_ref="iteration-fix-impl",
            scope_glob=scope_glob, converged_plan_sha256=plan_sha,
        )
    else:
        plan_sha = _make_sealed_iteration(
            repo_root, "iteration-open", closing_state="in_progress")
        (repo_root / ".consensus" / "design-approved").write_text(
            yaml.safe_dump({
                "schema_version": 1,
                "design_consensus_ref": "iteration-open",
                "converged_plan_sha256": plan_sha,
                "scope_glob": scope_glob,
                "repo_root_id": "x",
            }, sort_keys=True), encoding="utf-8")


def _make_iter_dir(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Iteration scratch dir + goal/target inputs (test_consensus_run_iteration)."""
    iter_dir = tmp_path / "iteration-test"
    iter_dir.mkdir()
    goal = iter_dir / "goal_packet.yaml"
    goal.write_text("pilot: iter-test\nschema_version: 1\n", encoding="utf-8")
    target = iter_dir / "problem.yaml"
    target.write_text("question: test\n", encoding="utf-8")
    return iter_dir, goal, target


def _write_host_peer_config(tmp_path: Path, mode=cfg.WORKFLOW_POST_REVIEW) -> Path:
    """Config enabling the built-in claude-swe-reviewer host_peer profile.
    Mirrors test_consensus_run_iteration._write_host_peer_config so the
    activation/soft-skip seams are driven through the identical config shape."""
    config = deepcopy(cfg.default_config())
    config["contributors"]["enabled"] = [
        "claude", "codex", "gemini", "claude-swe-reviewer",
    ]
    config["contributors"]["profiles"] = {
        "claude-swe-reviewer": {
            "name": "claude-swe-reviewer",
            "kind": "host_peer",
            "family": "claude",
            "role": "swe_reviewer",
            "weight": "supplementary",
            "gate_eligible": False,
        }
    }
    config["workflow"]["mode"] = mode
    if mode == cfg.WORKFLOW_POST_REVIEW:
        config["workflow"]["independence"] = cfg.INDEPENDENCE_VISIBLE
    cfg.validate(config)
    cfg_dir = tmp_path / ".consensus"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return cfg_path


def _parse_frontmatter(text: str) -> dict:
    """Parse the leading YAML frontmatter block (test_agents_init.py)."""
    assert text.startswith("---"), "agent file must start with YAML frontmatter"
    parts = text.split("---", 2)
    assert len(parts) >= 3, "agent file must have a closed frontmatter block"
    return yaml.safe_load(parts[1])


def _tool_set(fm: dict) -> set[str]:
    tools = fm["tools"]
    if isinstance(tools, str):
        return {t.strip() for t in tools.split(",") if t.strip()}
    return set(tools)


# =========================================================================== #
# E2E #1 — MARKER -> GATE: the same Edit flips from DENY to ALLOW once a genuine
# cross-family seal + minted marker exist. Proves the design gate is wired to the
# real T6 seal, end-to-end through the subprocess hook.
# =========================================================================== #

def test_e2e_marker_flips_edit_from_deny_to_allow(tmp_path):
    edit_ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"},
               "cwd": str(tmp_path)}

    # (a) NO marker yet -> the gate DENIES (exit 2) with a consensus reason.
    before = _run_hook(PRETOOLUSE, edit_ev, repo_root=tmp_path, runtime="present")
    assert before.returncode == 2, before.stderr
    assert "consensus" in before.stderr.lower()

    # (b) Seal a GENUINE cross-family iteration + mint the real marker pointing at
    #     it (via mint_design_approval -> the real T6 seal, not a forged boolean).
    _seal_marker(tmp_path, scope_glob="src/**", sealed=True)

    # Sanity: the marker really exists on disk and points at the sealed iteration.
    marker = tmp_path / ".consensus" / "design-approved"
    assert marker.exists()
    parsed = yaml.safe_load(marker.read_text(encoding="utf-8"))
    assert parsed["design_consensus_ref"] == "iteration-fix-impl"

    # (c) The SAME Edit is now ALLOWED (exit 0) — the gate re-validated the pointer
    #     against the live seal and let the in-scope edit through.
    after = _run_hook(PRETOOLUSE, edit_ev, repo_root=tmp_path, runtime="present")
    assert after.returncode == 0, after.stderr


# =========================================================================== #
# E2E #2 — FORGE RESISTANCE through the full path: a hand-written marker pointing
# at a NON-sealed iteration is DENIED. The closure invariant (seal is the trust
# root, not the marker) survives the whole subprocess pipeline, for BOTH an Edit
# and a non-allowlisted Bash command.
# =========================================================================== #

def test_e2e_forged_unsealed_marker_denied_end_to_end(tmp_path):
    # A forged marker: a hand-written pointer at an in_progress (NON-sealed) iter.
    _seal_marker(tmp_path, scope_glob="src/**", sealed=False)

    # Edit branch: re-validation rejects the forged pointer -> deny (exit 2).
    edit_ev = {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"},
               "cwd": str(tmp_path)}
    edit_cp = _run_hook(PRETOOLUSE, edit_ev, repo_root=tmp_path, runtime="present")
    assert edit_cp.returncode == 2, edit_cp.stderr
    assert "consensus" in edit_cp.stderr.lower()

    # Bash branch: a forged marker does NOT enable a non-allowlisted Bash command
    # either (marker_is_sealed re-validates the same way).
    bash_ev = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
               "cwd": str(tmp_path)}
    bash_cp = _run_hook(PRETOOLUSE, bash_ev, repo_root=tmp_path, runtime="present")
    assert bash_cp.returncode == 2, bash_cp.stderr


def test_e2e_marker_in_process_verify_matches_subprocess_gate(tmp_path):
    """Cross-check the seam: the in-process verify_design_approval verdict matches
    the subprocess gate's allow/deny for the SAME marker + target. Proves the hook
    and the library agree on the trust decision (no divergent second copy)."""
    _seal_marker(tmp_path, scope_glob="src/**", sealed=True)

    # In-scope -> in-process verify says ok AND subprocess gate exits 0.
    in_scope = da.verify_design_approval(Path("src/x.py"), repo_root=tmp_path)
    assert in_scope.ok, in_scope.reason
    cp_in = _run_hook(
        PRETOOLUSE,
        {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"},
         "cwd": str(tmp_path)},
        repo_root=tmp_path, runtime="present")
    assert cp_in.returncode == 0, cp_in.stderr

    # Out-of-scope -> in-process verify says NOT ok AND subprocess gate exits 2.
    out_scope = da.verify_design_approval(Path("docs/y.md"), repo_root=tmp_path)
    assert not out_scope.ok
    cp_out = _run_hook(
        PRETOOLUSE,
        {"tool_name": "Write", "tool_input": {"file_path": "docs/y.md"},
         "cwd": str(tmp_path)},
        repo_root=tmp_path, runtime="present")
    assert cp_out.returncode == 2, cp_out.stderr


# =========================================================================== #
# E2E #3 — host_peer ACTIVATION: the (v1.20-dormant) host_peer path is now live.
# An enabled host_peer profile + a valid host_peer_review_yaml drives the REAL
# HostPeerAdapter through consensus.run_iteration and SEALS a host-peer artifact
# carrying the canonical gate_eligible=false / weight=supplementary provenance.
# =========================================================================== #

def test_e2e_host_peer_activation_seals_gate_eligible_false(tmp_path, monkeypatch):
    _write_host_peer_config(tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)

    from consensus_mcp.contributors.base import FakeAlwaysApprove
    from consensus_mcp.contributors.host_peer_adapter import HostPeerAdapter

    def _patched_build(config, *, claude_artifact_callback=None,
                       host_peer_review_callback=None, **_kw):
        # Fake cross-family reviewers (no subprocess), but the GENUINE
        # HostPeerAdapter so the canonical provenance + sealing path is real.
        adapters = {
            "claude": FakeAlwaysApprove(),
            "codex": FakeAlwaysApprove(),
            "gemini": FakeAlwaysApprove(),
        }
        if host_peer_review_callback is not None:
            adapters["claude-swe-reviewer"] = HostPeerAdapter(
                adapter_config={"family": "claude", "role": "swe_reviewer"},
                host_peer_review_callback=host_peer_review_callback,
            )
        return adapters
    monkeypatch.setattr(factory, "build_adapters", _patched_build)

    hp_yaml = yaml.safe_dump({
        "findings": [],
        "goal_satisfied": True,
        "blocking_objections": [],
    })
    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        host_peer_review_yaml=hp_yaml,
        repo_root=str(tmp_path),
    )
    assert result["ok"] is True, result.get("error")

    # The dormant path is now LIVE: a sealed host-peer artifact landed in the iter
    # dir with the gate-safety provenance intact.
    sealed = iter_dir / "host-peer-review.yaml"
    assert sealed.exists(), "host_peer activation did not seal an artifact"
    parsed = yaml.safe_load(sealed.read_text(encoding="utf-8"))
    assert parsed["gate_eligible"] is False
    assert parsed["weight"] == "supplementary"

    # Activation ran -> NOT soft-skipped.
    assert not result.get("supplementary_skipped")


# =========================================================================== #
# E2E #4 — SOFT-SKIP: the SAME enabled host_peer profile with NO
# host_peer_review_yaml is gracefully soft-skipped. The iteration still succeeds
# and surfaces the skipped profile name (supplementary, never a hard error).
# =========================================================================== #

def test_e2e_host_peer_soft_skip_when_no_review_yaml(tmp_path, monkeypatch):
    _write_host_peer_config(tmp_path)
    iter_dir, goal, target = _make_iter_dir(tmp_path)
    monkeypatch.chdir(tmp_path)

    from consensus_mcp.contributors.base import FakeAlwaysApprove

    def _patched_build(config, *, claude_artifact_callback=None,
                       host_peer_review_callback=None, **_kw):
        # No callback wired -> the factory gracefully omits the host_peer adapter.
        return {
            "claude": FakeAlwaysApprove(),
            "codex": FakeAlwaysApprove(),
            "gemini": FakeAlwaysApprove(),
        }
    monkeypatch.setattr(factory, "build_adapters", _patched_build)

    result = tool.handle(
        iteration_dir=str(iter_dir),
        goal_packet_path=str(goal),
        target_path=str(target),
        # host_peer_review_yaml deliberately omitted.
        repo_root=str(tmp_path),
    )
    assert result["ok"] is True, result.get("error")
    # Soft-skip note names the profile; no host-peer artifact was sealed.
    assert result.get("supplementary_skipped")
    assert "claude-swe-reviewer" in result["supplementary_skipped"]
    assert not (iter_dir / "host-peer-review.yaml").exists()


# =========================================================================== #
# E2E #5 — AGENT FILES present + correct: the two installed subagent definitions
# parse as frontmatter and grant the `Agent` (sub-dispatch) tool ONLY to the
# orchestrator. This is the host-driven dispatch contract: the neutral driver may
# sub-dispatch the reviewer; the read-only reviewer can neither sub-dispatch nor
# mutate.
# =========================================================================== #

def test_e2e_agent_files_dispatch_contract(tmp_path, monkeypatch):
    # Drive the REAL per-project install path so the assertion covers what `init`
    # actually writes (not just the packaged source).
    from consensus_mcp import _init_wizard as wiz
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    agents_dir = tmp_path / ".claude" / "agents"

    orch_text = (agents_dir / "consensus-orchestrator.md").read_text(encoding="utf-8")
    rev_text = (agents_dir / "consensus-host-peer-reviewer.md").read_text(encoding="utf-8")

    orch = _parse_frontmatter(orch_text)
    reviewer = _parse_frontmatter(rev_text)

    assert orch["name"] == "consensus-orchestrator"
    assert reviewer["name"] == "consensus-host-peer-reviewer"

    orch_tools = _tool_set(orch)
    reviewer_tools = _tool_set(reviewer)

    # The dispatch contract: Agent (sub-dispatch) granted ONLY to the orchestrator.
    assert "Agent" in orch_tools
    assert "Agent" not in reviewer_tools

    # The reviewer is strictly read-only (cannot mutate, cannot sub-dispatch).
    assert reviewer_tools == {"Read", "Grep", "Glob", "Bash"}

    # The orchestrator drives consensus via the MCP tools + read/bash/grep/glob,
    # and specifically holds the run_iteration tool that activates the host_peer.
    assert {"Read", "Bash", "Grep", "Glob"}.issubset(orch_tools)
    assert "mcp__consensus-mcp__consensus_run_iteration" in orch_tools
    assert any(t.startswith("mcp__consensus-mcp__") for t in orch_tools)
