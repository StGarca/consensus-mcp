"""Host cherry-pick decisions cover sealed ideas without reviewer voting."""
import json
from pathlib import Path

import pytest
import yaml

from consensus_mcp import _approve_consult as ac
from consensus_mcp import _design_approval as da
from consensus_mcp import _dispositions as disp
from consensus_mcp.tests.test_approve_consult import _make_consult
from consensus_mcp.validators.validate_converged_plan import validate_dispositions


def _write(path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _review(iter_dir, reviewer="codex", **extra):
    data = {
        "reviewer_id": reviewer,
        "findings": [{"id": "F1", "summary": "Add a guard", "severity": "blocking"}],
        "blocking_objections": ["F1", "Unlinked objection"],
        "goal_satisfied": False,
        **extra,
    }
    _write(iter_dir / f"{reviewer}-review.yaml", data)


def _resolve(iter_dir, disposition="accepted", evidence=""):
    path = disp.init_dispositions(iter_dir)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for row in data["dispositions"]:
        row.update(disposition=disposition, evidence=evidence)
    _write(path, data)
    return data


@pytest.mark.parametrize("nested", [False, True])
def test_extract_proposal_ideas_with_provenance(tmp_path, nested):
    proposal = {
        "selected_target": "Guard approval",
        "rationale_vs_alternatives": "File presence alone is insufficient",
        "deliverable_scope": {"key_design_decisions": ["Check every idea", "Bind hash"]},
        "risks": ["Lost decisions", "Stale sources"],
        "recommendations": ["Keep host approval"],
        "alternatives": ["Count votes"],
    }
    _write(tmp_path / "codex-review-pass.yaml", {
        "reviewer_id": "codex-1",
        "dispatch_provenance": {"model": "gpt-test"},
        **({"proposal": proposal, "findings": []} if nested else proposal),
    })
    rows = disp.extract_rows(tmp_path)
    assert len(rows) == 8
    assert {r["summary"] for r in rows} == {
        "Guard approval", "File presence alone is insufficient", "Check every idea",
        "Bind hash", "Lost decisions", "Stale sources", "Keep host approval", "Count votes",
    }
    assert all(r["reviewer"] == "codex-1" and r["model"] == "gpt-test" for r in rows)
    assert all(r["disposition"] == "pending" and r["evidence"] == "" for r in rows)
    assert all(r["blocking"] is False for r in rows)
    assert rows == disp.extract_rows(tmp_path)


def test_extract_findings_and_blocking_objections_without_duplicate_refs(tmp_path):
    _review(tmp_path)
    rows = disp.extract_rows(tmp_path)
    assert len(rows) == 2
    assert all(row["blocking"] for row in rows)
    assert all("model" not in row for row in rows)
    assert "Add a guard" in rows[0]["summary"]
    assert rows[1]["summary"] == "Unlinked objection"


def test_init_cli_requires_no_scope_or_plan_and_preserves_host_work(tmp_path, capsys):
    iter_dir = _make_consult(tmp_path, with_plan=False)
    _review(iter_dir)
    args = ["--iteration", "iter-test", "--init-dispositions", "--repo-root", str(tmp_path)]
    assert ac.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dispositions_path"] == str(iter_dir / disp.FILENAME)
    path = iter_dir / disp.FILENAME
    original = path.read_bytes()
    assert ac.main(args) == 0
    assert path.read_bytes() == original
    _resolve(iter_dir, "rejected", "Guard already present: src/check.py:42")
    original = path.read_bytes()
    assert ac.main(args) == 0
    assert path.read_bytes() == original
    assert not (iter_dir / "converged-plan.yaml").exists()
    assert not (iter_dir / "iteration-outcome.yaml").exists()


def test_approve_auto_initializes_and_lists_pending_ids(tmp_path):
    iter_dir = _make_consult(tmp_path)
    _review(iter_dir)
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert not result["ok"] and result["error_type"] == "invalid_dispositions"
    for row in disp.extract_rows(iter_dir):
        assert row["id"] in result["error"]
    assert "pending" in result["error"]
    assert not (iter_dir / "iteration-outcome.yaml").exists()
    assert not (tmp_path / ".consensus" / "design-approved").exists()


@pytest.mark.parametrize("disposition", ["rejected", "deferred"])
def test_rejected_or_deferred_require_evidence_but_do_not_veto(tmp_path, disposition):
    iter_dir = _make_consult(tmp_path)
    _review(iter_dir)
    data = _resolve(iter_dir, disposition, " \n ")
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert not result["ok"]
    assert "requires non-empty evidence" in result["error"]
    assert all(r["id"] in result["error"] for r in data["dispositions"])
    _resolve(iter_dir, disposition, "Reproduced with fixture: guard is already present")
    assert ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)["ok"]


@pytest.mark.parametrize("change", ["omit", "duplicate", "source", "extra", "schema", "enum"])
def test_schema_and_exact_source_coverage_enforced(tmp_path, change):
    iter_dir = _make_consult(tmp_path)
    _review(iter_dir)
    data = _resolve(iter_dir)
    rows = data["dispositions"]
    if change == "omit":
        rows.pop()
    elif change == "duplicate":
        rows.append(dict(rows[0]))
    elif change == "source":
        rows[0]["blocking"] = False
    elif change == "extra":
        rows[0]["id"] = "invented"
    elif change == "schema":
        rows[0]["evidence"] = []
    else:
        rows[0]["disposition"] = "ratified"
    _write(iter_dir / disp.FILENAME, data)
    assert validate_dispositions(data, disp.extract_rows(iter_dir))
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert not result["ok"] and result["error_type"] == "invalid_dispositions"


@pytest.mark.parametrize("continuous", [False, True])
def test_approval_hash_binding_and_revalidation(tmp_path, continuous):
    iter_dir = _make_consult(tmp_path, governance_mode="continuous" if continuous else "on-demand")
    _review(iter_dir)
    _resolve(iter_dir)
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert result["ok"], result
    sha = disp.dispositions_hash(iter_dir)
    assert result["dispositions_sha256"] == sha
    outcome = yaml.safe_load((iter_dir / "iteration-outcome.yaml").read_text(encoding="utf-8"))
    assert outcome["dispositions_sha256"] == sha
    if continuous:
        marker_path = tmp_path / ".consensus" / "design-approved"
        marker = yaml.safe_load(marker_path.read_text(encoding="utf-8"))
        assert marker["dispositions_sha256"] == sha
        assert da.verify_design_approval("src/x.py", tmp_path).ok
        del marker["dispositions_sha256"]
        _write(marker_path, marker)
        assert not da.verify_design_approval("src/x.py", tmp_path).ok
        marker["dispositions_sha256"] = sha
        _write(marker_path, marker)
        path = iter_dir / disp.FILENAME
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "\n# changed\n", encoding="utf-8")
        check = da.verify_design_approval("src/x.py", tmp_path)
        assert not check.ok and "dispositions_sha256 mismatch" in check.reason
        path.unlink()
        assert not da.verify_design_approval("src/x.py", tmp_path).ok


def test_model_identity_prevents_alias_family_inflation(tmp_path):
    iter_dir = _make_consult(tmp_path)
    _review(iter_dir, dispatch_provenance={"model": "shared-model"}, findings=[], blocking_objections=[])
    _review(iter_dir, "gemini", dispatch_provenance={"model": "SHARED-MODEL"}, findings=[], blocking_objections=[])
    assert da._count_non_claude_reviewers(iter_dir) == 1
    result = ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)
    assert result["error_type"] == "insufficient_reviewers"
    _review(iter_dir, "gemini", dispatch_provenance={"model": "shared-model", "actual_model": "other-model"},
            findings=[], blocking_objections=[])
    assert da._count_non_claude_reviewers(iter_dir) == 2
    assert ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)["ok"]
    outcome = yaml.safe_load((iter_dir / "iteration-outcome.yaml").read_text(encoding="utf-8"))
    assert outcome["panel"] == ["claude", "other-model", "shared-model"]


def test_missing_or_failed_dispatches_do_not_add_pending_rows(tmp_path):
    iter_dir = _make_consult(tmp_path)
    _write(iter_dir / "dispatch-log.yaml", {"kimi": {"status": "failed"}})
    _write(iter_dir / "kimi-review.yaml.tmp", {"findings": ["unfinished"]})
    assert disp.extract_rows(iter_dir) == []
    assert ac.approve_consult("iter-test", "src/**", repo_root=tmp_path)["ok"]


def test_scaffold_explains_host_cherry_pick_and_no_material_findings(tmp_path):
    from consensus_mcp._start_consult import start_consult
    from consensus_mcp.tests.test_approve_consult import _init_project

    _init_project(tmp_path)
    result = start_consult("Inspect guards", "src/**", repo_root=tmp_path)
    assert result["ok"]
    text = result["next_steps"]["2_synthesize"]
    for required in ("cherry-picking", "host is the approver", "no majority voting",
                     "converged-plan.yaml", "dispositions.yaml", "--init-dispositions",
                     "accepted, rejected or deferred", "evidence"):
        assert required in text
    goal = yaml.safe_load(Path(result["goal_packet"]).read_text(encoding="utf-8"))
    desired = goal["goal"]["desired_end_state"]
    assert "If nothing material is found, say so explicitly and state what was checked." in desired
    assert "Empty findings is not acceptable" not in desired
