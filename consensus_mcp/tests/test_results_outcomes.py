"""Outcome measurement from sealed host decisions and external gold, offline."""
import copy
import json

import pytest
import yaml

from consensus_mcp import _approve_consult as approve
from consensus_mcp import _contributor_weights as weights
from consensus_mcp import _design_approval as approval_gate
from consensus_mcp import _dispositions as dispositions
from consensus_mcp import _outcome_ledger as ledger
from consensus_mcp import _results_log as log
from consensus_mcp import _results_rollup as results
from consensus_mcp import _weight_eval as evaluation
from consensus_mcp.tests.test_approve_consult import _make_consult


def _write(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _consult(tmp_path, mode="on-demand"):
    directory = _make_consult(tmp_path, governance_mode=mode)
    _write(directory / "codex-review.yaml", {
        "reviewer_id": "codex", "goal_satisfied": False,
        "dispatch_provenance": {"model": "requested", "actual_model": "model-a"},
        "findings": [{"id": "shared-id", "severity": "blocking"},
                     {"id": "wrong", "severity": "high"}],
        "recommendations": ["Later"], "blocking_objections": ["shared-id"],
    })
    # The same finding ID in another review is a separately attributable idea.
    _write(directory / "gemini-review-pass-2.yaml", {
        "reviewer_id": "gemini", "dispatch_provenance": {"model": "model-b"},
        "findings": [{"id": "shared-id", "severity": "blocking"}],
    })
    (directory / "gemini-review.yaml").unlink()
    data = {"schema_version": 1, "dispositions": dispositions.extract_rows(directory)}
    for row in data["dispositions"]:
        row["disposition"] = "accepted"
        if row["id"].endswith("findings/1"):
            row["disposition"] = "rejected"
        if "recommendations" in row["id"]:
            row["disposition"] = "deferred"
        row["evidence"] = "fixture://decision"
    _write(directory / "dispositions.yaml", data)
    (directory / "run-outcome.json").write_text(json.dumps({
        "contributors_failed": {"kimi": {"class": "quota", "message": "exhausted"}},
        "model_settings": {"kimi": {"model": "model-c"}},
    }), encoding="utf-8")
    return directory


def _records(tmp_path):
    path = tmp_path / "consensus-state" / "state" / "results-v1.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("mode", ["on-demand", "continuous"])
def test_approval_records_sealed_ideas_once_and_rolls_up(tmp_path, mode):
    directory = _consult(tmp_path, mode)
    for _ in range(2):
        result = approve.approve_consult(directory.name, "src/**", repo_root=tmp_path)
        assert result["ok"], result
    records = _records(tmp_path)
    assert len(records) == 1
    record = records[0]
    host = record["convergence"]["host_disposition"]
    assert host["dispositions_sha256"] == dispositions.dispositions_hash(directory)
    assert len(host["ideas"]) == 4
    assert host["reviewers"][0] == {
        "reviewer": "codex", "model": "model-a", "ideas_raised": 3,
        "accepted": 1, "rejected": 1, "deferred": 1,
        "blocking_raised": 1, "blocking_accepted": 1,
    }
    assert host["reviewers"][1]["model"] == "model-b"
    assert host["reviewers"][1]["ideas_raised"] == 1
    card = results.build_scorecard(tmp_path / "consensus-state")
    a = card["models"]["model-a"]
    assert a["iterations"] == 1
    assert a["ideas_raised"] == 3 and a["deferred"] == 1
    assert a["acceptance_rate"] == pytest.approx(1 / 3)
    assert a["rejected_as_wrong_rate"] == pytest.approx(1 / 3)
    assert a["blocking_idea_acceptance"] == 1
    assert a["gold_precision"] is None and a["gold_total"] == 0
    assert card["models"]["model-c"]["failures_by_class"] == {"quota": 1}
    assert "gold precision n/a" in results.render_table(card)
    # Legacy schema accepts the extension without modifying the shared schema.
    log.upsert_jsonl(record, tmp_path / "validated.jsonl")


def test_failed_approve_writes_no_results(tmp_path):
    directory = _consult(tmp_path)
    data = yaml.safe_load((directory / "dispositions.yaml").read_text())
    data["dispositions"][0]["disposition"] = "pending"
    _write(directory / "dispositions.yaml", data)
    assert not approve.approve_consult(directory.name, "src/**", repo_root=tmp_path)["ok"]
    assert not (tmp_path / "consensus-state" / "state" / "results-v1.jsonl").exists()


def test_only_hash_bound_dispositions_are_counted(tmp_path):
    directory = _consult(tmp_path)
    assert "host_disposition" not in log.build_results_record(directory)["convergence"]
    assert approve.approve_consult(directory.name, "src/**", repo_root=tmp_path)["ok"]
    with (directory / "dispositions.yaml").open("a") as stream:
        stream.write("\n# changed bytes\n")
    with pytest.raises(ValueError, match="differs from sealed"):
        log.build_results_record(directory)


def test_zero_ideas_and_aliases_of_one_model_count_one_iteration(tmp_path):
    directory = _consult(tmp_path)
    _write(directory / "alias-review-pass-3.yaml", {
        "reviewer_id": "alias", "dispatch_provenance": {"model": "model-a"},
        "findings": [],
    })
    assert approve.approve_consult(directory.name, "src/**", repo_root=tmp_path)["ok"]
    host = _records(tmp_path)[0]["convergence"]["host_disposition"]
    assert any(r["reviewer"] == "alias" and r["ideas_raised"] == 0 for r in host["reviewers"])
    card = results.build_scorecard(tmp_path / "consensus-state")
    assert card["models"]["model-a"]["iterations"] == 1


def test_unapproved_runs_include_failure_classes_and_unknown_models(tmp_path):
    directory = tmp_path / "active" / "failed"
    directory.mkdir(parents=True)
    (directory / "run-outcome.json").write_text(json.dumps({
        "contributors_failed": {
            "codex": {"class": "timeout", "message": "late"},
            "kimi": {"class": "auth", "message": "no login"},
        }, "model_settings": {"codex": {"model": "model-a"}},
    }))
    card = results.build_scorecard(tmp_path)
    assert card["models"]["model-a"]["failures_by_class"] == {"timeout": 1}
    assert card["models"]["unknown (kimi)"]["model"] is None
    assert card["models"]["unknown (kimi)"]["failures_by_class"] == {"auth": 1}


def _write_consult_summary(directory, run_id, seats):
    _write(directory / f"consult-summary-{run_id}.yaml", {
        "run_id": run_id, "seats": seats,
    })


def test_consult_summary_failures_roll_up_once_across_runs_and_latest_copy(tmp_path):
    directory = tmp_path / "active" / "panel-run"
    directory.mkdir(parents=True)
    seats = [{"seat": f"seat-{i}", "model_requested": f"model-{i}",
              "model_used": None, "status": "failed", "failure_class": "timeout"}
             for i in range(5)]
    _write_consult_summary(directory, "run-one", seats)
    _write(directory / "consult-summary.yaml", {"run_id": "run-one", "seats": seats})
    _write_consult_summary(directory, "run-two", [
        {"seat": "codex", "model_requested": "requested", "model_used": "actual",
         "status": "failed", "failure_class": "auth"},
        {"seat": "claude", "model_requested": "legacy-model", "model_used": None,
         "status": "failed", "failure_class": "quota"},
    ])

    card = results.build_scorecard(tmp_path)
    assert sum(sum(m["failures_by_class"].values()) for m in card["models"].values()) == 7
    for i in range(5):
        assert card["models"][f"model-{i}"]["failures_by_class"] == {"timeout": 1}
    assert card["models"]["actual"]["failures_by_class"] == {"auth": 1}
    assert card["models"]["legacy-model"]["failures_by_class"] == {"quota": 1}


def test_consult_summary_failures_added_after_snapshot_and_keep_iteration_once(tmp_path):
    directory = _consult(tmp_path)
    # The snapshot already contains the legacy workflow failure and a reviewer
    # measurement for model-a. Later consult runs must be included alongside it.
    assert approve.approve_consult(directory.name, "src/**", repo_root=tmp_path)["ok"]
    _write_consult_summary(directory, "run-one", [
        {"seat": "codex", "model_requested": "model-a", "status": "failed",
         "failure_class": "timeout"},
        {"seat": "kimi", "model_requested": "model-c", "status": "failed",
         "failure_class": "quota"},
    ])
    _write(directory / "consult-summary.yaml", {"run_id": "run-one", "seats": [
        {"seat": "codex", "model_requested": "model-a", "status": "failed",
         "failure_class": "timeout"},
        {"seat": "kimi", "model_requested": "model-c", "status": "failed",
         "failure_class": "quota"},
    ]})
    _write_consult_summary(directory, "run-two", [
        {"seat": "codex", "model_requested": "model-a", "status": "failed",
         "failure_class": "timeout"},
    ])

    card = results.build_scorecard(tmp_path / "consensus-state")
    assert card["models"]["model-c"]["failures_by_class"] == {"quota": 2}
    assert card["models"]["model-a"]["failures_by_class"] == {"timeout": 2}
    assert card["models"]["model-a"]["iterations"] == 1


def test_consult_summary_failures_are_not_recounted_after_snapshot(tmp_path):
    directory = _consult(tmp_path)
    _write_consult_summary(directory, "same-run", [
        {"seat": "kimi", "model_requested": "model-c", "status": "failed",
         "failure_class": "quota"},
    ])
    # This scorecard snapshot fixture uses the ideas-only approval tier.
    _write(directory / "consult-summary.yaml", {"run_id": "same-run", "tier": "quick", "seats": [
        {"seat": "kimi", "model_requested": "model-c", "status": "failed",
         "failure_class": "quota"},
    ]})
    assert approve.approve_consult(directory.name, "src/**", repo_root=tmp_path)["ok"]

    card = results.build_scorecard(tmp_path / "consensus-state")
    assert card["models"]["model-c"]["failures_by_class"] == {"quota": 2}


def test_legacy_latest_only_consult_summary_is_counted(tmp_path):
    directory = tmp_path / "active" / "legacy-panel"
    directory.mkdir(parents=True)
    _write(directory / "consult-summary.yaml", {"seats": [
        {"seat": "gemini", "model_requested": "requested-model",
         "status": "failed", "failure_class": "crash"},
    ]})

    card = results.build_scorecard(tmp_path)
    assert card["models"]["requested-model"]["failures_by_class"] == {"crash": 1}


@pytest.mark.parametrize("useful", ["true", "false"])
def test_gold_cli_and_json_keep_gold_separate(tmp_path, monkeypatch, capsys, useful):
    directory = _consult(tmp_path)
    assert approve.approve_consult(directory.name, "src/**", repo_root=tmp_path)["ok"]
    root = tmp_path / "consensus-state"
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(root))
    row_id = "codex-review.yaml#/findings/0"
    args = ["results", "--json", "--record-gold", directory.name, row_id,
            "--useful", useful, "--evidence", "test://red-green", "--adjudicator", "external-test"]
    assert results.main(args) == 0
    card = json.loads(capsys.readouterr().out)
    assert card["models"]["model-a"]["gold_precision"] == (useful == "true")
    assert card["models"]["model-a"]["acceptance_rate"] == pytest.approx(1 / 3)
    rows = ledger.read_outcomes(root / "state" / "outcome-ledger.jsonl")
    assert rows[0]["adjudicator"] == "external-test"
    assert rows[0]["tier"] == "gold" and rows[0]["finding_id"] == row_id
    assert rows[0]["dispositions_sha256"] == dispositions.dispositions_hash(directory)
    with pytest.raises(SystemExit):
        results.main(args)
    assert len(ledger.read_outcomes(root / "state" / "outcome-ledger.jsonl")) == 1


@pytest.mark.parametrize("overrides", [
    {"adjudicator": "codex"}, {"adjudicator": "claude"},
    {"adjudicator": "gpt-6"}, {"adjudicator": "grok"}, {"adjudicator": None},
    {"evidence_ref": " "}, {"useful": "true"}, {"row_id": "invented"},
])
def test_gold_rejects_ai_or_invalid_evidence_and_rows(tmp_path, overrides):
    directory = _consult(tmp_path)
    assert approve.approve_consult(directory.name, "src/**", repo_root=tmp_path)["ok"]
    kwargs = dict(row_id="codex-review.yaml#/findings/0", useful=True,
                  evidence_ref="test://external", adjudicator="operator", iteration_index=0)
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        ledger.record_gold(tmp_path / "gold.jsonl", _records(tmp_path)[0], **kwargs)
    assert not (tmp_path / "gold.jsonl").exists()


def test_gold_cli_requires_explicit_external_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))
    with pytest.raises(SystemExit):
        results.main(["--record-gold", "iteration", "row", "--useful", "true", "--evidence", "ref"])
    assert not (tmp_path / "state" / "outcome-ledger.jsonl").exists()


def _external(index=0, model="model-a", useful=False, **overrides):
    return dict(finding_id=f"{model}-{index}", contributor=model, domain="general",
                tier="gold", useful=useful, iteration_index=index, model_version=model,
                adjudicator="external-test", evidence_ref="test://external", **overrides)


def test_learner_distinguishes_host_from_gold_and_ignores_deferred():
    external = [_external(index=i) for i in range(6)]
    host = [{**row, "disposition": "rejected"} for row in external]
    arguments = dict(current_iteration=6, current_model_version="model-a")
    gold_weight = weights.learned_weight_for(external, "model-a", "general", **arguments)
    host_weight = weights.learned_weight_for([], "model-a", "general",
                                             host_dispositions=host, **arguments)
    assert gold_weight < host_weight < 1
    assert weights.learned_weight_for([], "model-a", "general", host_dispositions=[
        {**row, "disposition": "deferred"} for row in host], **arguments) == 1
    # Host labels cannot impersonate external credit, even if fed to the wrong input.
    forged = [{**row, "tier": "host_disposition"} for row in external]
    assert weights.learned_weight_for(forged, "model-a", "general", **arguments) == 1


def test_unknown_model_cannot_adjudicate_its_own_gold(tmp_path):
    record = _external(model="nebula-v7")
    record["adjudicator"] = "nebula-v7"
    with pytest.raises(ValueError, match="no-self-grade"):
        ledger.append_outcome(tmp_path / "gold.jsonl", record)


def test_history_evaluation_uses_only_prior_iterations():
    # Training rejects a, accepts b. The later iteration has a before b.
    prior = [_external(i, "model-a", False) for i in range(6)]
    current = [_external(6, "model-a", False), _external(6, "model-b", True)]
    comparison = evaluation.evaluate_history(prior + current, [])
    assert comparison["advisory"] is True
    assert comparison["gold"]["beats"] is True
    assert comparison["gold"]["weighted_mean_rank"] == 0
    assert comparison["gold"]["uniform_mean_rank"] == 1
    # With only the current labels, no training data exists to improve order.
    assert evaluation.evaluate_history(current, [])["gold"]["beats"] is False
    assert evaluation.evaluate_history([], [])["gold"]["beats"] is None


def test_scorecard_and_gold_leave_approval_and_family_count_unchanged(tmp_path):
    from types import SimpleNamespace
    from consensus_mcp.contributors.base import FakeAlwaysApprove
    from consensus_mcp.workflow_engine import IterationOutcome, WorkflowEngine
    from consensus_mcp.tests.test_workflow_engine import _three_contributor_config

    directory = _consult(tmp_path, "continuous")
    assert approve.approve_consult(directory.name, "src/**", repo_root=tmp_path)["ok"]
    paths = [directory / "iteration-outcome.yaml", directory / "dispositions.yaml",
             tmp_path / ".consensus" / "design-approved", tmp_path / ".consensus" / "session-active"]
    before = {p: p.read_bytes() for p in paths}
    family_count = approval_gate._count_non_claude_reviewers(directory)
    engine = WorkflowEngine(_three_contributor_config(),
                            {n: FakeAlwaysApprove() for n in ("claude", "codex", "gemini")}, tmp_path)
    artifacts = [SimpleNamespace(contributor=n, parsed={"goal_satisfied": n != "codex",
                                                       "blocking_objections": []})
                 for n in ("claude", "codex", "gemini")]
    outcome = IterationOutcome(directory.name, "propose-converge", tmp_path / "config.yaml")
    votes_before = engine.evaluate_plan_convergence(artifacts, outcome)
    ledger.record_gold(tmp_path / "consensus-state" / "state" / "outcome-ledger.jsonl",
                       _records(tmp_path)[0], "codex-review.yaml#/findings/0", useful=False,
                       evidence_ref="test://wrong", adjudicator="operator", iteration_index=0)
    card = results.build_scorecard(tmp_path / "consensus-state")
    assert card["models"]["model-a"]["advisory_weight"] < 1
    assert {p: p.read_bytes() for p in paths} == before
    assert approval_gate._count_non_claude_reviewers(directory) == family_count == 2
    assert engine.evaluate_plan_convergence(artifacts, outcome) == votes_before
    assert votes_before.approve_votes == ["claude", "gemini"]
    assert votes_before.block_votes == ["codex"]
    assert approval_gate.verify_design_approval("src/x.py", tmp_path).ok
    assert approve.approve_consult(directory.name, "src/**", repo_root=tmp_path)["ok"]


def test_old_records_render_without_new_signals(tmp_path):
    from consensus_mcp.tests.test_results_rollup import _FORWARD_RECORDS

    path = tmp_path / "state" / "results-v1.jsonl"
    path.parent.mkdir()
    records = copy.deepcopy(_FORWARD_RECORDS)
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    card = results.build_scorecard(tmp_path)
    assert card["authoritative"]["iterations"] == 3
    assert card["models"] == {}
    assert "total findings:" in results.render_table(card)
