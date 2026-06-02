"""Unit tests for consensus_mcp._seal_iteration.

Focused on the four subcommands:
  - prepare:  canonicalize per-family review filenames + skeleton
  - lint:     YAML parse error reporting
  - mint:     refuses bad inputs; succeeds on valid sealed iteration
  - verify:   round-trip with a real sealed iteration

Trust-root regression gates (per converged-plan G6): mint MUST refuse
when fewer than 2 distinct non-claude reviewers, when the
iteration-outcome.yaml declares a non-sealed closing_state, when the
converged-plan.yaml hash doesn't match.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consensus_mcp import _seal_iteration as si  # noqa: E402


# Helpers ----------------------------------------------------------------

def _scaffold_iter(tmp_path, monkeypatch, iter_name="iter-test"):
    """Create a minimal repo-marker tree + iteration dir."""
    repo = tmp_path / "repo"
    (repo / "consensus_mcp" / "validators").mkdir(parents=True)
    (repo / "consensus-state").mkdir()
    iter_dir = repo / "consensus-state" / "active" / iter_name
    iter_dir.mkdir(parents=True)
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(repo))
    return repo, iter_dir


def _write_review(iter_dir: Path, family: str, suffix: str = "") -> Path:
    """Write a minimal sealed review YAML for `family`."""
    name = f"{family}-review{suffix}.yaml" if suffix else f"{family}-review.yaml"
    p = iter_dir / name
    p.write_text(
        f"iteration_id: {iter_dir.name}\n"
        f"reviewer_id: {family}-1\n"
        f"pass_id: {family}-1-pass1\n"
        "findings: []\n"
        "goal_satisfied: true\n"
        "blocking_objections: []\n",
        encoding="utf-8",
    )
    return p


def _write_converged_plan(iter_dir: Path) -> Path:
    p = iter_dir / "converged-plan.yaml"
    p.write_text(
        "schema_version: 1\n"
        f"iteration_id: {iter_dir.name}\n"
        "selected_strategy:\n  name: 'test'\n",
        encoding="utf-8",
    )
    return p


def _write_outcome(iter_dir: Path, closing_state: str = "quorum_close_passed"):
    p = iter_dir / "iteration-outcome.yaml"
    p.write_text(
        f"iteration_id: {iter_dir.name}\n"
        f"closing_state: {closing_state}\n"
        "workflow: 'test'\n"
        "goal: 'test'\n",
        encoding="utf-8",
    )
    return p


# ----- prepare ----------------------------------------------------------

def test_prepare_copies_per_pass_review_to_canonical_name(tmp_path, monkeypatch):
    """Section 3.6 friction: kimi sealed at kimi-review-kimi-debrief-1-pass1.yaml.
    `prepare` must produce a canonical kimi-review.yaml so the cross-family
    counter glob (`*-review.yaml` -> `kimi`) sees it."""
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_review(iter_dir, "kimi", suffix="-kimi-debrief-1-pass1")

    result = si._prepare(iter_dir)
    assert result["ok"]
    assert (iter_dir / "kimi-review.yaml").exists()
    assert any("kimi-review.yaml" in s for s in result["copied"])


def test_prepare_does_not_overwrite_existing_canonical(tmp_path, monkeypatch):
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    canonical = _write_review(iter_dir, "codex")
    canonical_content = canonical.read_text()
    _write_review(iter_dir, "codex", suffix="-codex-2-pass1")

    result = si._prepare(iter_dir)
    # Canonical file unchanged
    assert canonical.read_text() == canonical_content
    assert any("codex-review.yaml (canonical exists" in s for s in result["skipped"])


def test_prepare_writes_outcome_skeleton_when_absent(tmp_path, monkeypatch):
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    result = si._prepare(iter_dir)
    assert result["skeleton_written"]
    outcome = iter_dir / "iteration-outcome.yaml"
    assert outcome.exists()
    text = outcome.read_text()
    assert "EDIT_ME_TO_A_SEALED_STATE" in text  # non-authoritative placeholder


def test_prepare_does_not_overwrite_existing_outcome(tmp_path, monkeypatch):
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_outcome(iter_dir)
    sentinel = (iter_dir / "iteration-outcome.yaml").read_text()
    result = si._prepare(iter_dir)
    assert not result["skeleton_written"]
    assert (iter_dir / "iteration-outcome.yaml").read_text() == sentinel


# ----- lint -------------------------------------------------------------

def test_lint_passes_on_valid_yamls(tmp_path, monkeypatch):
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_review(iter_dir, "codex")
    _write_review(iter_dir, "gemini")
    result = si._lint(iter_dir)
    assert result["ok"]
    assert result["parsed"] == 2
    assert result["errors"] == []


def test_lint_catches_embedded_colon(tmp_path, monkeypatch):
    """Section 3.7 friction: `key: value with : embedded`. Lint must
    catch this BEFORE the verifier's hash step."""
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    bad = iter_dir / "broken.yaml"
    bad.write_text(
        "key1: ok\n"
        "key2: value with: an embedded colon that yaml hates\n",
        encoding="utf-8",
    )
    result = si._lint(iter_dir)
    assert not result["ok"]
    assert len(result["errors"]) == 1
    assert "broken.yaml" in result["errors"][0]["file"]
    # Line + col pointers present.
    assert result["errors"][0]["line"] is not None
    assert result["errors"][0]["col"] is not None


# ----- mint -------------------------------------------------------------

def test_mint_refuses_overbroad_scope(tmp_path, monkeypatch):
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_outcome(iter_dir)
    _write_converged_plan(iter_dir)
    result = si._mint(iter_dir, "quorum_close_passed", "*")
    assert not result["ok"]
    assert result["error_type"] == "overbroad_scope"


def test_mint_refuses_missing_iteration_outcome(tmp_path, monkeypatch):
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_converged_plan(iter_dir)
    result = si._mint(iter_dir, "quorum_close_passed", "consensus_mcp/**")
    assert not result["ok"]
    assert result["error_type"] == "missing_iteration_outcome"


def test_mint_refuses_closing_state_mismatch(tmp_path, monkeypatch):
    """The outcome file is authoritative - --closing-state must match."""
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_outcome(iter_dir, closing_state="quorum_close_passed")
    _write_converged_plan(iter_dir)
    result = si._mint(
        iter_dir, "implementation_ready_apply_landed", "consensus_mcp/**",
    )
    assert not result["ok"]
    assert result["error_type"] == "closing_state_mismatch"


def test_mint_refuses_when_lint_fails(tmp_path, monkeypatch):
    """Pre-flight lint blocks mint when ANY YAML in iter_dir is unparseable.
    The Section 3.7 -> 3.8 cascade is short-circuited."""
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_outcome(iter_dir)
    _write_converged_plan(iter_dir)
    (iter_dir / "broken.yaml").write_text("key: value with: bad colon", encoding="utf-8")
    result = si._mint(iter_dir, "quorum_close_passed", "consensus_mcp/**")
    assert not result["ok"]
    assert result["error_type"] == "lint_failed"
    assert result["lint_errors"]


def test_mint_succeeds_on_valid_sealed_iteration(tmp_path, monkeypatch):
    """G1 acceptance gate: end-to-end mint with 2 non-claude reviewers
    succeeds; the resulting marker passes verify_design_approval."""
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_outcome(iter_dir, closing_state="quorum_close_passed")
    _write_converged_plan(iter_dir)
    _write_review(iter_dir, "codex")
    _write_review(iter_dir, "gemini")

    result = si._mint(iter_dir, "quorum_close_passed", "consensus_mcp/**")
    assert result["ok"], result
    assert "converged_plan_sha256" in result

    # Verify the marker.
    from consensus_mcp._design_approval import verify_design_approval
    # Target a file inside the scope.
    target = repo / "consensus_mcp" / "_x.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# scope target", encoding="utf-8")
    res = verify_design_approval(target, repo_root=repo)
    assert res.ok, res.reason


def test_mint_refuses_fewer_than_2_non_claude_reviewers(tmp_path, monkeypatch):
    """G6 trust-root regression: a hand-fabricated marker for an iteration
    with only 1 non-claude review must NOT pass mint."""
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_outcome(iter_dir, closing_state="quorum_close_passed")
    _write_converged_plan(iter_dir)
    _write_review(iter_dir, "codex")  # only one non-claude - too few

    # mint itself doesn't enforce the count (mint_design_approval doesn't
    # either; the count is enforced at VERIFY time). But the marker
    # produced must fail verify.
    result = si._mint(iter_dir, "quorum_close_passed", "consensus_mcp/**")
    assert result["ok"]  # mint allows; verify rejects
    from consensus_mcp._design_approval import verify_design_approval
    target = repo / "consensus_mcp" / "_x.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# scope target", encoding="utf-8")
    res = verify_design_approval(target, repo_root=repo)
    assert not res.ok
    assert "non-claude reviewer" in res.reason


# ----- verify -----------------------------------------------------------

def test_verify_returns_ok_for_in_scope_path_with_valid_marker(tmp_path, monkeypatch):
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_outcome(iter_dir, closing_state="quorum_close_passed")
    _write_converged_plan(iter_dir)
    _write_review(iter_dir, "codex")
    _write_review(iter_dir, "gemini")
    mint_result = si._mint(iter_dir, "quorum_close_passed", "consensus_mcp/**")
    assert mint_result["ok"]

    target = repo / "consensus_mcp" / "_x.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# scope target", encoding="utf-8")

    result = si._verify(target)
    assert result["ok"], result


def test_verify_returns_not_ok_for_out_of_scope_path(tmp_path, monkeypatch):
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_outcome(iter_dir, closing_state="quorum_close_passed")
    _write_converged_plan(iter_dir)
    _write_review(iter_dir, "codex")
    _write_review(iter_dir, "gemini")
    si._mint(iter_dir, "quorum_close_passed", "consensus_mcp/**")

    out_target = repo / "docs" / "x.md"
    out_target.parent.mkdir(parents=True, exist_ok=True)
    out_target.write_text("# out of scope", encoding="utf-8")

    result = si._verify(out_target)
    assert not result["ok"]
    assert "OUT OF SCOPE" in result["reason"] or "scope_glob" in result["reason"]


# ----- main argv shape --------------------------------------------------

def test_main_help_lists_all_5_subcommands(capsys):
    """v1.32.1: `close` added per the gate-scope-shift consult."""
    with pytest.raises(SystemExit):
        si.main(["--help"])
    out = capsys.readouterr().out
    for cmd in ("prepare", "lint", "mint", "verify", "close"):
        assert cmd in out, f"subcommand {cmd!r} missing from --help"


def test_main_mint_requires_scope_glob_or_writing_plans_flag(tmp_path, monkeypatch, capsys):
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_outcome(iter_dir, closing_state="quorum_close_passed")
    rc = si.main([
        "mint",
        "--iteration-dir", str(iter_dir),
        "--closing-state", "quorum_close_passed",
    ])
    assert rc == 1
    out = capsys.readouterr().out
    result = json.loads(out.strip())
    assert result["error_type"] == "missing_scope_glob"


def test_main_mint_with_writing_plans_uses_default_scope(tmp_path, monkeypatch, capsys):
    """G5 / D1: --writing-plans-followup populates the default scope_glob
    docs/consensus/**, eliminating Section 3.9 re-mint."""
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    _write_outcome(iter_dir, closing_state="quorum_close_passed")
    _write_converged_plan(iter_dir)
    _write_review(iter_dir, "codex")
    _write_review(iter_dir, "gemini")
    rc = si.main([
        "mint",
        "--iteration-dir", str(iter_dir),
        "--closing-state", "quorum_close_passed",
        "--writing-plans-followup",
    ])
    out = capsys.readouterr().out
    result = json.loads(out.strip())
    assert rc == 0, result
    assert result["scope_glob"] == "docs/consensus/**"


def test_main_lint_returns_nonzero_on_parse_failure(tmp_path, monkeypatch, capsys):
    repo, iter_dir = _scaffold_iter(tmp_path, monkeypatch)
    (iter_dir / "bad.yaml").write_text("key: value with: bad", encoding="utf-8")
    rc = si.main(["lint", "--iteration-dir", str(iter_dir)])
    assert rc == 2
