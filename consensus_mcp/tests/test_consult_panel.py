"""Panel orchestration uses real local fake dispatchers, never model providers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tomllib

import pytest
import yaml

from consensus_mcp import _consult_panel as cp


@pytest.fixture
def project(tmp_path):
    config = tmp_path / ".consensus" / "config.yaml"
    config.parent.mkdir()
    config.write_text("schema_version: 1\n", encoding="utf-8")
    iteration = tmp_path / "consensus-state" / "active" / "iteration-test"
    iteration.mkdir(parents=True)
    for filename in ("goal_packet.yaml", "review-packet.yaml"):
        (iteration / filename).write_text("{}", encoding="utf-8")
    return tmp_path


@pytest.fixture
def dispatchers(project, monkeypatch):
    script = project / "fake_dispatch.py"
    script.write_text('''import hashlib, json, pathlib, sys, time, yaml
from consensus_mcp.tools.review_write_and_seal import _canonical_yaml_sha256, _file_lock
from consensus_mcp._atomic_io import atomic_write_text
args = sys.argv[1:]
def arg(name):
    return args[args.index(name) + 1]
seat = arg("--profile")
options = json.loads(arg("--fake-options")).get(seat, {})
directory = pathlib.Path(arg("--iteration-dir"))
started = time.monotonic()
print("fake stderr tail", file=sys.stderr, flush=True)
time.sleep(options.get("delay", .01))
if options.get("failure"):
    print(json.dumps({"ok": False, "error": "fake failure", "failure_class": options["failure"]}))
    sys.exit(1)
if options.get("empty"):
    print("not a dispatcher result")
    sys.exit(0)
pass_id = "old-pass" if options.get("stale") else arg("--pass-id")
path = directory / (seat + "-review.yaml")
packet = {"iteration_id": directory.name, "pass_id": pass_id, "reviewer_id": seat,
          "dispatch_provenance": {"model_requested": "requested-model", "model_used": "fallback-model",
                                  "fallback_reason": "quota", "usage": {"input_tokens": 12, "output_tokens": 7},
                                  "review_target_hash": hashlib.sha256((directory / "review-packet.yaml").read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
                                  "goal_packet_sha256": hashlib.sha256((directory / "goal_packet.yaml").read_text(encoding="utf-8").encode("utf-8")).hexdigest()},
          "started": started, "finished": time.monotonic()}
packet.update(options.get("packet", {}))
packet["packet_sha256"] = _canonical_yaml_sha256(packet)
archive = directory.parents[1] / "archive" / "review-passes"
archive.mkdir(parents=True, exist_ok=True)
archived = archive / (pass_id + ".yaml")
atomic_write_text(archived, yaml.safe_dump(packet))
path.write_text(json.dumps(packet), encoding="utf-8")
with _file_lock(archive / "index.lock"):
    index_path = archive / "index.yaml"
    index = yaml.safe_load(index_path.read_text()) if index_path.exists() else {"passes": []}
    index["passes"].append({"id": pass_id, "iteration_id": directory.name, "reviewer_id": seat,
                          "packet_sha256": packet["packet_sha256"], "path": str(archived)})
    atomic_write_text(index_path, yaml.safe_dump(index))
result = {"ok": True, "pass_id": arg("--pass-id"), "sealed_path": str(path),
          "archive_sealed_path": str(archived), "packet_sha256": packet["packet_sha256"]}
result.update(options.get("result", {}))
print(json.dumps(result))
''', encoding="utf-8")
    original = cp._command
    options, launches, processes = {}, [], []
    popen = subprocess.Popen

    def command(*args):
        argv = original(*args)
        return [sys.executable, str(script), *argv[4:], "--fake-options", json.dumps(options)]

    def launch(argv, **kwargs):
        launches.append(argv[argv.index("--profile") + 1])
        assert kwargs["env"]["CONSENSUS_MCP_REPO_ROOT"] == str(project)
        assert kwargs["env"]["CONSENSUS_MCP_PROJECT_ROOT"] == str(project)
        proc = popen(argv, **kwargs)
        processes.append(proc)
        return proc

    def kill(proc):
        proc.kill()
        proc.communicate(timeout=5)

    monkeypatch.setattr(cp, "_command", command)
    monkeypatch.setattr(cp.subprocess, "Popen", launch)
    monkeypatch.setattr("consensus_mcp._jobs._kill_tree", kill)
    return options, launches, processes


def run(project, **kwargs):
    # Tests written before the sensitive-data gate keep their behaviour by
    # answering "no"; the gate itself is covered in test_consult_sensitivity.py.
    kwargs.setdefault("sensitive_data", "no")
    return cp.consult_panel("iteration-test", repo_root=project, **kwargs)


def configure(project, config):
    (project / ".consensus" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_concurrent_processes_deepseek_first_and_kimi_not_serialized(project, dispatchers):
    options, launches, _ = dispatchers
    seats = ["claude", "kimi", "deepseek", "codex"]
    options.update({seat: {"delay": .5} for seat in seats})
    result = run(project, seats=seats)
    assert result["ok"] is True
    assert launches == ["deepseek", "claude", "kimi", "codex"]
    assert result["sealed_count"] == 4 and result["failed_count"] == 0
    packets = [yaml.safe_load(Path(row["review_path"]).read_text()) for row in result["seats"]]
    assert max(packet["started"] for packet in packets) < min(packet["finished"] for packet in packets)
    assert result["total_wall_seconds"] < sum(row["wall_seconds"] for row in result["seats"]) / 2


def test_failure_isolated_and_summary_records_fallback_tokens_and_schema(project, dispatchers):
    options, _, _ = dispatchers
    options["codex"] = {"failure": "quota"}
    result = run(project, seats=["codex", "claude"])
    assert result["ok"] is True and result["status"] == "success"
    assert result["sealed_count"] == result["failed_count"] == 1
    failed, sealed = result["seats"]
    assert failed["status"] == "failed" and failed["failure_class"] == "quota"
    assert failed["model_used"] is None and failed["review_path"] is None
    assert sealed["model_requested"] == "requested-model"
    assert sealed["model_used"] == "fallback-model" and sealed["fallback_reason"] == "quota"
    assert sealed["token_counts"] == {"input_tokens": 12, "output_tokens": 7}
    assert sealed["status"] == "sealed" and sealed["failure_class"] is None
    assert sealed["wall_seconds"] > 0 and Path(sealed["review_path"]).is_file()
    summary = yaml.safe_load(Path(result["summary_path"]).read_text())
    assert summary == {key: value for key, value in result.items() if key != "summary_path"}
    assert summary["schema_version"] == 1 and summary["iteration"] == "iteration-test"
    assert summary["total_wall_seconds"] >= sealed["wall_seconds"]


@pytest.mark.parametrize("mode", ["failure", "empty", "stale"])
def test_zero_valid_current_reviews_is_no_reviews(project, dispatchers, mode):
    options, _, _ = dispatchers
    options["codex"] = {mode: "auth" if mode == "failure" else True}
    result = run(project, seats=["codex"])
    assert result["ok"] is False and result["status"] == "no_reviews"
    assert result["sealed_count"] == 0 and result["failed_count"] == 1
    assert "no_reviews" in result["error"]
    assert Path(result["summary_path"]).is_file()


@pytest.mark.parametrize("limit", ["timeout_seconds", "deadline_seconds"])
def test_timeout_and_total_deadline_reap_children_without_cancelling_success(project, dispatchers, limit):
    options, _, processes = dispatchers
    options["codex"] = {"delay": 10}
    started = time.monotonic()
    result = run(project, seats=["codex", "claude"], **{limit: 1.2})
    assert time.monotonic() - started < 3
    assert result["ok"] is True and result["sealed_count"] == 1
    assert result["seats"][0]["failure_class"] == "timeout"
    assert "deadline" in result["seats"][0]["error"] if limit == "deadline_seconds" else "seat timeout" in result["seats"][0]["error"]
    assert all(proc.poll() is not None for proc in processes)


def test_default_panel_and_optional_free_seat(project, dispatchers, monkeypatch):
    _, launches, _ = dispatchers
    run(project)
    assert launches == ["claude", "codex", "kimi", "grok", "gemini"]
    launches.clear()
    monkeypatch.setenv("CONSENSUS_MCP_DEEPSEEK_BASE_URL", "http://unused.invalid")
    run(project)
    assert launches == ["deepseek", "claude", "codex", "kimi", "grok", "gemini"]
    launches.clear()
    run(project, seats=["kimi"])
    assert launches == ["kimi"]


def test_configured_standard_quick_and_deep_tier_sets(project, dispatchers, monkeypatch):
    _, launches, _ = dispatchers
    configure(project, {"contributors": {"enabled": ["claude", "codex", "kimi"]},
                        "consult": {"quick_seats": ["kimi", "codex"]}})
    monkeypatch.setattr(cp.shutil, "which", lambda command: None)
    run(project, tier="standard")
    assert launches == ["claude", "codex", "kimi"]
    launches.clear()
    run(project, tier="quick")
    assert launches == ["kimi", "codex"]
    launches.clear()
    run(project, tier="deep")
    assert launches == ["claude", "codex", "kimi"]
    launches.clear()
    monkeypatch.setattr(cp.shutil, "which", lambda command: "fake-opencode")
    run(project, tier="deep")
    assert launches == ["claude", "codex", "kimi", "opencode-deepseek"]


def test_quick_default_has_two_seats_and_rejects_bad_configuration(project, dispatchers):
    _, launches, _ = dispatchers
    run(project, tier="quick")
    assert launches == ["claude", "codex"]
    configure(project, {"consult": {"quick_seats": ["codex"]}})
    launches.clear()
    result = run(project, tier="quick")
    assert result["ok"] is False and "exactly two" in result["error"]
    assert launches == []
    for seats in (["codex"], ["codex", "codex"], ["codex", "kimi", "claude"]):
        assert run(project, tier="quick", seats=seats)["ok"] is False
    assert launches == []


def test_spawn_failure_is_isolated_and_requested_adapter_model_retained(project, dispatchers, monkeypatch):
    configure(project, {"contributors": {"adapters": {"codex": {"model": "adapter-model"}}}})
    popen = cp.subprocess.Popen

    def launch(argv, **kwargs):
        if argv[argv.index("--profile") + 1] == "codex":
            raise FileNotFoundError("fake missing dispatcher")
        return popen(argv, **kwargs)

    monkeypatch.setattr(cp.subprocess, "Popen", launch)
    result = run(project, seats=["codex", "claude"])
    assert result["ok"] is True and result["sealed_count"] == 1
    assert result["seats"][0]["model_requested"] == "adapter-model"
    assert result["seats"][0]["failure_class"] == "crash"


def test_cleanup_failure_is_isolated_with_conflicting_ambient_roots(project, dispatchers, monkeypatch):
    from consensus_mcp import _jobs

    options, _, processes = dispatchers
    options["codex"] = {"delay": 10}
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(project / "wrong-repo"))
    monkeypatch.setenv("CONSENSUS_MCP_PROJECT_ROOT", str(project / "wrong-project"))
    kill = _jobs._kill_tree

    def cleanup(proc):
        kill(proc)
        raise OSError("fake cleanup failure")

    monkeypatch.setattr(_jobs, "_kill_tree", cleanup)
    result = run(project, seats=["codex", "claude"], timeout_seconds=1.2)
    assert result["ok"] is True and result["sealed_count"] == result["failed_count"] == 1
    assert result["seats"][0]["cleanup_error"] == "fake cleanup failure"
    assert result["seats"][0]["failure_class"] == "timeout"
    assert result["seats"][0]["error"] == "seat timeout exceeded"
    assert result["seats"][1]["status"] == "sealed"
    assert Path(result["summary_path"]).is_file()
    assert all(proc.poll() is not None for proc in processes)


def test_public_dispatcher_entrypoints_and_proposal_arguments(project):
    profiles = cp._project_profiles(project)
    for seat, module in (("codex", "consensus_mcp._dispatch_codex"),
                         ("deepseek", "consensus_mcp._dispatch_openai_compat"),
                         ("opencode-deepseek", "consensus_mcp.contributors.opencode")):
        command = cp._command(seat, profiles[seat], project, "fresh-pass", 1.2)
        assert command[:4] == [sys.executable, "-I", "-m", module]
        assert command[command.index("--profile") + 1] == seat
        assert command[command.index("--pass-id") + 1] == "fresh-pass"
        assert command[command.index("--mode") + 1] == "proposal"
        assert command[command.index("--timeout-seconds") + 1] == "2"


def test_cli_exit_code_and_seat_parsing(monkeypatch, capsys):
    calls = []

    def consult(**kwargs):
        calls.append(kwargs)
        return {"ok": len(calls) == 1, "status": "success" if len(calls) == 1 else "no_reviews"}

    monkeypatch.setattr(cp, "consult_panel", consult)
    assert cp.main(["--iteration", "i", "--seats", "codex,kimi", "claude", "--tier", "quick"]) == 0
    assert calls[0]["seats"] == ["codex", "kimi", "claude"]
    assert calls[0]["tier"] == "quick"
    assert calls[0]["pass_kind"] == "ideas"
    assert cp.main(["--iteration", "i"]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "no_reviews"
    assert cp.main(["--iteration", "i", "--plan-pass"]) == 1
    assert calls[-1]["pass_kind"] == "plan"
    assert cp.main(["--iteration", "i", "--confirm-pass"]) == 1
    assert calls[-1]["pass_kind"] == "confirm"
    assert "plan_pass" not in calls[-1] and "confirm_pass" not in calls[-1]
    with pytest.raises(SystemExit):
        cp.main(["--iteration", "i", "--plan-pass", "--confirm-pass"])


@pytest.mark.parametrize("kwargs", [{"iteration": "../escape"}, {"seats": ["unknown"]},
                                    {"timeout_seconds": 0}, {"deadline_seconds": float("nan")},
                                    {"tier": "bad"}, {"pass_kind": "unknown"}])
def test_invalid_inputs_start_nothing(project, dispatchers, kwargs):
    _, launches, _ = dispatchers
    result = cp.consult_panel(**{"iteration": "iteration-test", "repo_root": project, **kwargs})
    assert result["ok"] is False and result["status"] == "no_reviews"
    assert launches == []


def test_integration_registration_job_kind_hook_and_console_script():
    from consensus_mcp import server
    from consensus_mcp._jobs import ALLOWED_TOOLS
    from consensus_mcp.claude_extensions.hooks import consensus_pretooluse_gate as gate

    assert "consensus.consult_panel" in ALLOWED_TOOLS
    assert server._consult_panel is cp.consult_panel
    assert server.registry.get_handler("consensus.consult_panel") is cp.consult_panel
    descriptor = next(tool for tool in server.registry.list_tools() if tool["name"] == "consensus.consult_panel")
    assert descriptor["inputSchema"]["required"] == ["iteration"]
    assert descriptor["inputSchema"]["properties"]["tier"]["enum"] == ["quick", "standard", "deep"]
    assert descriptor["inputSchema"]["properties"]["pass_kind"] == {
        "type": "string", "enum": ["ideas", "plan", "confirm"], "default": "ideas"}
    assert gate._bash_is_read_only("consensus-mcp-consult --iteration iteration-test")
    assert not gate._bash_is_read_only("consensus-mcp-consult --iteration iteration-test && rm file")
    config = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["scripts"]["consensus-mcp-consult"] == "consensus_mcp._consult_panel:main"


@pytest.mark.parametrize("pass_kind", ["plan", "confirm"])
def test_plan_packet_uses_existing_review_mode(project, pass_kind):
    directory = project / "consensus-state" / "active" / "iteration-test"
    (directory / "review-packet.yaml").write_text(f"pass_kind: {pass_kind}\n", encoding="utf-8")
    if pass_kind == "confirm":
        template = Path(cp.__file__).parent / "dispatch_templates" / "confirm_review.md"
        (directory / "confirm-pass.md").write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    profiles = cp._project_profiles(project)
    for seat in ("claude", "codex", "kimi", "grok", "gemini", "deepseek", "opencode-deepseek"):
        command = cp._command(seat, profiles[seat], directory, "plan-review", 600)
        assert command[command.index("--mode") + 1] == "review"
        assert command[command.index("--review-target") + 1] == str(directory / "review-packet.yaml")
        if pass_kind == "confirm":
            template = Path(command[command.index("--prompt-template") + 1])
            assert template.is_relative_to(project)
            rendered = template.read_text(encoding="utf-8")
            schema_text = rendered.split("Return only JSON matching this review output schema:\n", 1)[1]
            schema, _ = json.JSONDecoder().raw_decode(schema_text)
            assert schema["required"] == [
                "findings", "goal_satisfied", "goal_satisfied_rationale", "blocking_objections"]
            expected_prefix = ({"codex": "codex-rev", "kimi": "gemini-rev", "grok": "gemini-rev",
                                "gemini": "gemini-rev"}.get(seat, profiles[seat].get("id_prefix") or f"{seat}-rev"))
            assert schema["properties"]["findings"]["items"]["properties"]["id"]["pattern"] == f"^{expected_prefix}-\\d+$"
            if seat == "codex":
                assert schema["properties"]["blocking_objections"]["items"] == {"type": "string"}
            else:
                assert schema["properties"]["blocking_objections"]["items"]["pattern"] == f"^{expected_prefix}-\\d+$"
            assert f"Finding IDs must match ^{expected_prefix}-\\d+$" in rendered
            required = ["id", "severity", "summary", "citation", "risk", "recommendation"]
            if seat == "codex":
                required.extend(["patch_proposal", "patch_not_proposed_reason"])
            assert schema["properties"]["findings"]["items"]["required"] == required


def test_plan_child_reuses_parent_panel_and_preserves_ideas_summary(project, dispatchers, monkeypatch):
    import hashlib
    from consensus_mcp import _dispatch_base as base

    options, launches, _ = dispatchers
    assert run(project, tier="deep", seats=["codex", "kimi", "gemini"])["ok"]
    parent = project / "consensus-state" / "active" / "iteration-test"
    original_summary = (parent / "consult-summary.yaml").read_bytes()
    plan = "steps:\n- Preserve the owner's exact plan.\n"
    (parent / "converged-plan.yaml").write_text(plan, encoding="utf-8")
    (parent / "dispositions.yaml").write_text(yaml.safe_dump({"dispositions": [{
        "id": "idea-1", "disposition": "rejected", "evidence": "The goal excludes it."}]}), encoding="utf-8")
    launches.clear()
    options["codex"] = {"failure": "quota"}
    for seat in ("kimi", "gemini"):
        options[seat] = {"packet": {"findings": [], "goal_satisfied": True}}
    result = run(project, pass_kind="plan", tier="quick")
    assert result["ok"] is True and result["tier"] == "deep"
    assert launches == ["codex", "kimi", "gemini"]
    assert result["sealed_count"] == 2 and result["failed_count"] == 1
    assert result["seats"][0]["status"] == "failed"
    assert result["pass_kind"] == "plan" and result["parent_iteration"] == "iteration-test"
    assert result["converged_plan_sha256"] == hashlib.sha256((parent / "converged-plan.yaml").read_bytes()).hexdigest()
    assert (parent / "consult-summary.yaml").read_bytes() == original_summary
    child = parent.parent / "iteration-test-plan-1"
    summary = yaml.safe_load((child / "consult-summary.yaml").read_text(encoding="utf-8"))
    assert summary["converged_plan_sha256"] == result["converged_plan_sha256"]
    assert yaml.safe_load(Path(result["summary_path"]).read_text(encoding="utf-8")) == summary
    packet = yaml.safe_load((child / "review-packet.yaml").read_text(encoding="utf-8"))
    assert packet["rejected_or_deferred_ideas"][0]["evidence"] == "The goal excludes it."
    assert yaml.safe_load((parent / "plan-objections.yaml").read_text(encoding="utf-8"))["objections"] == []
    goal = yaml.safe_load((child / "goal_packet.yaml").read_text(encoding="utf-8"))
    template = (Path(cp.__file__).parent / "dispatch_templates" / "codex_review_template.md").read_text(encoding="utf-8")
    monkeypatch.setattr(base, "_diff_review_target", lambda *args: ("bounded git diff packet", None))
    prompt = base._build_prompt(goal, template, iteration_dir=str(child), review_packet=packet)
    assert "Preserve the owner's exact plan." in prompt and "[wrong_rejection]" in prompt
    assert "The goal excludes it." in prompt and result["converged_plan_sha256"] in prompt
    launches.clear()
    refused = run(project, pass_kind="plan", seats=["codex", "gemini"])
    assert not refused["ok"] and "same seats" in refused["error"] and launches == []


def _confirmation_parent(project):
    from consensus_mcp.tests.test_plan_passes import respond, seal_round, write

    directory = project / "consensus-state" / "active" / "iteration-test"
    write(directory / "consult-summary.yaml", {
        "tier": "standard", "seats": [{"seat": seat} for seat in ("codex", "kimi", "gemini")]})
    write(directory / "converged-plan.yaml", {"plan": "Small plan"})
    write(directory / "dispositions.yaml", {"dispositions": []})
    seal_round(directory, {"codex": ["[gap] Add rollback"]})
    respond(directory, "accepted", host_response="Added rollback")
    write(directory / "converged-plan.yaml", {"plan": "Small plan with rollback"})
    return directory


def test_confirm_child_uses_same_seats_and_seals_with_confirm_kind(project, dispatchers):
    options, launches, _ = dispatchers
    parent = _confirmation_parent(project)
    original_summary = (parent / "consult-summary.yaml").read_bytes()
    for seat in ("codex", "kimi", "gemini"):
        options[seat] = {"packet": {"findings": [], "goal_satisfied": True}}
    refused = run(project, pass_kind="confirm", seats=["codex", "gemini"])
    assert not refused["ok"] and "same seats" in refused["error"] and launches == []
    result = run(project, pass_kind="confirm", tier="deep")
    assert result["ok"] and result["tier"] == "standard"
    assert result["pass_kind"] == "confirm" and result["parent_iteration"] == parent.name
    assert launches == ["codex", "kimi", "gemini"]
    child = parent.parent / "iteration-test-plan-2"
    packet = yaml.safe_load((child / "review-packet.yaml").read_text(encoding="utf-8"))
    summary = yaml.safe_load((child / "consult-summary.yaml").read_text(encoding="utf-8"))
    assert packet["pass_kind"] == summary["pass_kind"] == "confirm"
    assert summary["converged_plan_sha256"] == result["converged_plan_sha256"]
    assert (parent / "consult-summary.yaml").read_bytes() == original_summary
    assert Path(result["seats"][0]["archive_sealed_path"]).is_file()
    run_log = (project / "consensus-state" / "archive" / "consult-runs.jsonl").read_text(encoding="utf-8")
    assert "iteration-test-plan-2" in run_log


def test_confirm_sensitive_gate_asks_before_scaffolding_or_launch(project, dispatchers):
    parent = _confirmation_parent(project)
    result = cp.consult_panel("iteration-test", repo_root=project, pass_kind="confirm")
    assert result["status"] == "needs_sensitivity_answer" and not result["ok"]
    assert result["restricted_seats"] == ["kimi"] and dispatchers[1] == []
    assert not (parent.parent / "iteration-test-plan-2").exists()


@pytest.mark.parametrize("answer,expected", [("yes", ["codex", "gemini"]), ("no", ["codex", "kimi", "gemini"])])
def test_confirm_sensitive_answer_applies_to_original_panel(project, dispatchers, answer, expected):
    _confirmation_parent(project)
    options, launches, _ = dispatchers
    for seat in ("codex", "kimi", "gemini"):
        options[seat] = {"packet": {"findings": [], "goal_satisfied": True}}
    result = run(project, pass_kind="confirm", sensitive_data=answer)
    assert result["ok"] and launches == expected
    assert result["sensitive_data"] == answer
    assert result["excluded_seats"] == (["kimi"] if answer == "yes" else [])


@pytest.mark.parametrize("config", [["bad"], {"contributors": {"adapters": {"kimi": "fast"}}}])
def test_config_errors_before_any_launch(project, dispatchers, config):
    configure(project, config)
    result = run(project, seats=["claude", "kimi"])
    assert not result["ok"] and dispatchers[1] == []


def test_second_launch_valueerror_isolated(project, dispatchers, monkeypatch):
    popen = cp.subprocess.Popen
    def launch(argv, **kwargs):
        if argv[argv.index("--profile") + 1] == "kimi":
            raise ValueError("invalid launch")
        return popen(argv, **kwargs)
    monkeypatch.setattr(cp.subprocess, "Popen", launch)
    result = run(project, seats=["claude", "kimi", "codex"])
    assert result["sealed_count"] == 2 and result["failed_count"] == 1
    assert result["seats"][1]["error"] == "invalid launch"
    assert Path(result["summary_path"]).is_file()


def test_submit_failure_reaps_owned_process(project, dispatchers, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("submit failed")
    monkeypatch.setattr(cp.ThreadPoolExecutor, "submit", fail)
    result = run(project, seats=["codex"])
    assert result["seats"][0]["error"] == "submit failed"
    assert all(proc.poll() is not None for proc in dispatchers[2])


def test_interrupt_kills_all_started_processes_and_propagates(project, dispatchers, monkeypatch):
    from concurrent.futures import Future
    dispatchers[0].update({seat: {"delay": 10} for seat in ("claude", "codex")})
    original = Future.result
    def interrupt(future, *args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            raise KeyboardInterrupt
        return original(future, *args, **kwargs)
    monkeypatch.setattr(Future, "result", interrupt)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run(project, seats=["claude", "codex"])
    assert time.monotonic() - started < 5
    assert len(dispatchers[2]) == 2 and all(proc.poll() is not None for proc in dispatchers[2])


@pytest.mark.parametrize("timeout,deadline,maximum", [(100, 900, 70), (600, 50, 25)])
def test_child_watchdog_has_margin_and_remaining_deadline(project, dispatchers, monkeypatch, timeout, deadline, maximum):
    original = cp.subprocess.Popen
    budgets = []
    def launch(argv, **kwargs):
        budgets.append(int(argv[argv.index("--timeout-seconds") + 1]))
        return original(argv, **kwargs)
    monkeypatch.setattr(cp.subprocess, "Popen", launch)
    assert run(project, seats=["claude", "codex"], timeout_seconds=timeout, deadline_seconds=deadline)["ok"]
    assert all(0 < value <= maximum for value in budgets)


def test_insufficient_watchdog_budget_launches_nothing(project, dispatchers):
    result = run(project, seats=["codex"], deadline_seconds=.5)
    assert result["seats"][0]["failure_class"] == "timeout" and dispatchers[1] == []


@pytest.mark.parametrize("packet", [{"dispatch_provenance": ["bad"]}, {"usage": ["bad"]}])
def test_bad_postprocessing_never_commits_sealed_status(project, dispatchers, packet):
    dispatchers[0]["codex"] = {"packet": packet}
    result = run(project, seats=["codex"])
    assert result["sealed_count"] == 0
    assert result["seats"][0]["status"] == "failed" and result["seats"][0]["failure_class"]
    assert result["seats"][0]["review_path"] is None


def test_empty_usage_preserves_previous_tokens(project, dispatchers):
    dispatchers[0]["codex"] = {"packet": {"token_counts": {"total_tokens": 19}, "dispatch_provenance": {"usage": {}}}}
    result = run(project, seats=["codex"])
    assert result["seats"][0]["token_counts"] == {"total_tokens": 19}


def test_panel_lock_is_nonblocking_and_summaries_keep_each_run(project, dispatchers, monkeypatch):
    directory = project / "consensus-state" / "active" / "iteration-test"
    launched, release = threading.Event(), threading.Event()
    original = cp.subprocess.Popen
    def launch(*args, **kwargs):
        proc = original(*args, **kwargs)
        launched.set()
        assert release.wait(5)
        return proc
    monkeypatch.setattr(cp.subprocess, "Popen", launch)
    with cp.ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(run, project, seats=["codex"])
        assert launched.wait(5)
        assert run(project, seats=["codex"])["status"] == "busy"
        assert dispatchers[1] == ["codex"]
        release.set()
        first = pending.result(timeout=5)
    monkeypatch.setattr(cp.subprocess, "Popen", original)
    saved = Path(first["summary_path"]).read_bytes()
    second = run(project, seats=["codex"])
    assert first["run_id"] != second["run_id"] and first["summary_path"] != second["summary_path"]
    assert Path(first["summary_path"]).read_bytes() == saved
    assert yaml.safe_load((directory / "consult-summary.yaml").read_text())["run_id"] == second["run_id"]
    assert first["seats"][0]["review_path"] != second["seats"][0]["review_path"]


def test_atomic_summary_readers_only_see_complete_runs(project, dispatchers, monkeypatch):
    directory = project / "consensus-state" / "active" / "iteration-test"
    run(project, seats=["codex"])
    failures, observed, stop = [], [], threading.Event()
    def reader():
        while not stop.is_set():
            try:
                summary = yaml.safe_load((directory / "consult-summary.yaml").read_text())
                assert summary["run_id"] and len(summary["seats"]) == 1
                observed.append(summary["run_id"])
            except PermissionError:
                pass  # Windows replacement may briefly deny the open itself.
            except Exception as exc:
                failures.append(exc)
    thread = threading.Thread(target=reader)
    thread.start()
    original = cp.atomic_write_text
    writes = []
    def atomic(path, text):
        writes.append(path.name)
        original(path, text)
    monkeypatch.setattr(cp, "atomic_write_text", atomic)
    try:
        result = run(project, seats=["codex"])
    finally:
        stop.set()
        thread.join(timeout=2)
    assert not failures and observed
    assert writes == [f"consult-summary-{result['run_id']}.yaml", "consult-summary.yaml"]


@pytest.mark.parametrize("mutation", [None, "identity_only", "archive_hash", "result_hash", "mirror_hash", "mirror_path", "archive_path", "index_missing", "index_hash", "index_path"])
def test_real_t6_archive_and_index_integrity(project, monkeypatch, mutation):
    from consensus_mcp.tools.review_write_and_seal import handle
    monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(project))
    packet = {"iteration_id": "iteration-test", "reviewer_id": "codex", "pass_id": "test-pass",
              "findings": [], "goal_satisfied": True, "blocking_objections": [], "independence_attestation": {}}
    seal = handle(iteration_id="iteration-test", reviewer_id="codex", pass_id="test-pass", packet=packet)
    assert "error" not in seal, seal
    archive = Path(seal["sealed_path"])
    mirror = project / "consensus-state" / "active" / "iteration-test" / "codex-review.yaml"
    mirror.write_bytes(archive.read_bytes())
    result = {"pass_id": "test-pass", "sealed_path": str(mirror), "archive_sealed_path": str(archive), "packet_sha256": seal["packet_sha256"]}
    if mutation in {"identity_only", "archive_hash", "mirror_hash"}:
        path = mirror if mutation == "mirror_hash" else archive
        value = yaml.safe_load(path.read_text())
        if mutation == "identity_only":
            value.pop("packet_sha256")
        else:
            value["goal_satisfied"] = False
        path.write_text(yaml.safe_dump(value))
    elif mutation in {"mirror_path", "archive_path"}:
        result["sealed_path" if mutation == "mirror_path" else "archive_sealed_path"] = str(project / "outside.yaml")
    elif mutation == "result_hash":
        result["packet_sha256"] = "wrong"
    elif mutation and mutation.startswith("index_"):
        path = archive.parent / "index.yaml"
        index = yaml.safe_load(path.read_text())
        if mutation == "index_missing":
            index["passes"] = []
        else:
            index["passes"][0]["packet_sha256" if mutation == "index_hash" else "path"] = "wrong"
        path.write_text(yaml.safe_dump(index))
    if mutation is None:
        verified, path, _ = cp._sealed_packet(result, "test-pass", project, "iteration-test", "codex")
        assert verified["packet_sha256"] == seal["packet_sha256"] and path == archive.resolve()
    else:
        with pytest.raises(RuntimeError, match="integrity"):
            cp._sealed_packet(result, "test-pass", project, "iteration-test", "codex")


def test_bad_seal_row_is_integrity_failure(project, dispatchers):
    dispatchers[0]["codex"] = {"result": {"packet_sha256": "wrong"}}
    result = run(project, seats=["codex"])
    assert result["sealed_count"] == 0 and result["seats"][0]["failure_class"] == "integrity"
    assert result["seats"][0]["status"] == "failed" and result["seats"][0]["review_path"] is None


def test_exited_parent_pipe_child_is_killed_and_stderr_drained(project, monkeypatch):
    if os.name != "nt":
        import posix
        monkeypatch.setattr(os, "killpg", posix.killpg)
    script = project / "pipe_parent.py"
    pidfile = project / "child.pid"
    script.write_text("import subprocess, sys\n"
                      "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                      f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
                      "print('inherited stderr', file=sys.stderr, flush=True)\n")
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {"start_new_session": True})
    proc = subprocess.Popen([sys._base_executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, **options)
    started = time.monotonic()
    try:
        proc.wait(timeout=5)
        assert pidfile.is_file() and proc.poll() is not None
        row = {"seat": "codex", "status": "failed", "failure_class": "crash", "review_path": None}
        cp._finish(proc, row, time.monotonic(), time.monotonic() + 1, .2, "pass", project, "iteration-test")
        assert row["failure_class"] == "timeout" and "inherited stderr" in row["error_tail"]
        assert "cleanup_error" not in row
        assert time.monotonic() - started < 5
        # communicate completed: the long-sleeping descendant released both
        # inherited pipes as a result of the real tree/group kill.
        assert proc.stdout.closed and proc.stderr.closed
    finally:
        from consensus_mcp._jobs import _kill_tree
        _kill_tree(proc)
        proc.communicate(timeout=2)


def test_kill_wait_and_process_enumeration_are_bounded(monkeypatch):
    from consensus_mcp import _jobs
    calls = []
    class Proc:
        pid = 99999999
        def wait(self, *, timeout):
            calls.append(timeout)
            raise subprocess.TimeoutExpired("wait", timeout)
    if os.name == "nt":
        monkeypatch.setattr(_jobs, "_windows_kill_tree", lambda pid: None)
    else:
        def enumerate_processes(*args, **kwargs):
            assert kwargs["timeout"] == 2
            return ""
        monkeypatch.setattr(_jobs.subprocess, "check_output", enumerate_processes)
        monkeypatch.setattr(_jobs.os, "kill", lambda *args: None)
        monkeypatch.setattr(_jobs.os, "killpg", lambda *args: None)
    with pytest.raises(subprocess.TimeoutExpired):
        _jobs._kill_tree(Proc())
    assert calls == [2]


def test_every_panel_run_is_recorded_in_the_archive_run_log(project, dispatchers):
    from consensus_mcp import _plan_passes as pp

    result = run(project, seats=["codex"], tier="standard")
    runs = [r for r in pp._logged_runs(project) if r["iteration"] == "iteration-test"]
    assert runs and runs[-1]["run_id"] == result["run_id"] and runs[-1]["tier"] == "standard"
    assert runs[-1]["sealed_count"] == 1
