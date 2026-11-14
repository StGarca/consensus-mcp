"""Dispatch prompt inputs use local Git packets, without calling any reviewer."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from consensus_mcp._dispatch_base import _build_prompt
from consensus_mcp._dispatch_openai_compat import prepare_template
from consensus_mcp._dispatch_runner import _ACTIVE_PROFILE


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=Packet Test", "-c", "user.email=packet@test.invalid",
                           *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    (tmp_path / "module.py").write_text("def changed():\n    return 0\n" + "# context\n" * 260, encoding="utf-8")
    (tmp_path / "related.py").write_text("from module import changed\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    (tmp_path / "iteration").mkdir()
    return tmp_path


@contextmanager
def active(repo, **options):
    token = _ACTIVE_PROFILE.set({"name": "test-seat", "kind": "cli_reviewer", "_abort_root": repo, **options})
    try:
        yield
    finally:
        _ACTIVE_PROFILE.reset(token)


def prompt(repo, target, template="{diff_packet_block}"):
    return _build_prompt({"goal": {"summary": "Review module"}, "allowed_files": ["module.py"]}, template,
                         iteration_dir=str(repo / "iteration"), review_target_path="target.yaml",
                         review_packet=target, review_target_content=yaml.safe_dump(target))


def change(repo, value):
    path = repo / "module.py"
    path.write_text(path.read_text(encoding="utf-8").replace("return 0", f"return {value}"), encoding="utf-8")


def seal(repo, text):
    import hashlib
    (repo / "iteration" / "test-seat-review.yaml").write_text(yaml.safe_dump({
        "dispatch_provenance": {"prompt_sha256": hashlib.sha256(text.encode()).hexdigest()}}), encoding="utf-8")


def test_diff_prompt_replaces_file_bodies_and_history(repo):
    change(repo, 1)
    target = {"question": "Specific design question", "instructions": "Inspect the call boundary",
              "objective": "Fix behavior", "check_results_if_any": [{"result": "passed"}],
              "defect_target": {"base_sha": "b" * 64, "touched_files_contents": {"module.py": "OLD WHOLE FILE"}},
              "prior_reviews": "OLD WHOLE REVIEW"}
    with active(repo):
        text = prompt(repo, target)
    loaded = yaml.safe_load(text)
    packet = loaded["review_packet"]
    assert "+    return 1" in packet["unified_diff"]
    assert packet["full_files"] == {}
    assert "OLD WHOLE FILE" not in text and "OLD WHOLE REVIEW" not in text
    assert loaded["review_context"]["question"] == "Specific design question"
    assert loaded["review_context"]["check_results_if_any"] == [{"result": "passed"}]
    assert any(item["section"] == "base_ref" for item in packet["omitted"])


def test_legacy_open_blockers_have_explicit_unresolved_status(repo):
    with active(repo):
        text = prompt(repo, {"open_items": [{"id": "other-item"}],
            "open_blockers": ["string-blocker", {"id": "dict-blocker", "summary": "Still open"},
            {"id": "resolved-blocker", "status": "resolved"}, {"id": "deferred-blocker", "disposition": "deferred"}]})
    items = yaml.safe_load(text)["review_packet"]["open_items"]
    assert {item["id"]: item["status"] for item in items} == {
        "other-item": "unresolved", "string-blocker": "unresolved", "dict-blocker": "unresolved", "deferred-blocker": "deferred"}
    assert next(item for item in items if item["id"] == "string-blocker")["summary"] == "string-blocker"


def test_sequential_convergence_rounds_use_previous_head_and_open_items(repo):
    base = git(repo, "rev-parse", "HEAD")
    change(repo, 1)
    git(repo, "add", "module.py")
    git(repo, "commit", "-m", "first change")
    with active(repo):
        initial_text = prompt(repo, {"base_ref": base})
        initial = yaml.safe_load(initial_text)["review_packet"]
        seal(repo, initial_text)
        path = repo / "module.py"
        path.write_text(path.read_text(encoding="utf-8") + "# second delta\n", encoding="utf-8")
        git(repo, "add", "module.py")
        git(repo, "commit", "-m", "second change")
        (repo / "iteration" / "dispositions.yaml").write_text(yaml.safe_dump({"dispositions": [
            {"id": "open", "summary": "Still unresolved", "disposition": "pending"},
            {"id": "closed", "summary": "Already resolved", "disposition": "accepted"},
            {"id": "later", "summary": "Deferred detail", "disposition": "deferred"},
        ]}), encoding="utf-8")
        first_text = prompt(repo, {"convergence_round": 1})
        first = yaml.safe_load(first_text)["review_packet"]
        seal(repo, first_text)
        again = yaml.safe_load(prompt(repo, {"convergence_round": 1}))["review_packet"]
        second = yaml.safe_load(prompt(repo, {"convergence_round": 2}))["review_packet"]
    assert first["base_sha"] == initial["head_sha"]
    assert "+# second delta" in first["unified_diff"]
    assert "+    return 1" not in first["unified_diff"]
    assert first == again
    assert second["base_sha"] == first["head_sha"] and second["unified_diff"] == ""
    assert {item["id"] for item in first["open_items"]} == {"open", "later"}


def test_later_round_without_previous_snapshot_fails(repo):
    with active(repo), pytest.raises(ValueError, match="previous reviewed packet/head_sha"):
        prompt(repo, {"convergence_round": 2})


def test_failed_dispatch_snapshot_is_not_a_reviewed_baseline(repo):
    with active(repo):
        prompt(repo, {})
        with pytest.raises(ValueError, match="previous reviewed packet/head_sha"):
            prompt(repo, {"convergence_round": 1})


def test_failed_retry_preserves_successful_snapshot(repo):
    with active(repo):
        reviewed = prompt(repo, {"open_items": [{"id": "reviewed", "status": "unresolved"}]})
        seal(repo, reviewed)
        prompt(repo, {"open_items": [{"id": "failed retry", "status": "unresolved"}]})
        later = yaml.safe_load(prompt(repo, {"convergence_round": 1}))["review_packet"]
    assert [item["id"] for item in later["open_items"]] == ["reviewed"]


def test_legacy_convergence_carries_findings_without_review_bodies(repo):
    prior_review = {"findings": [{"id": "old-1", "summary": "Open finding", "severity": "high",
                                  "patch_proposal": {"unified_diff": "OLD PATCH BODY"}}],
                    "goal_satisfied_rationale": "OLD FULL RATIONALE"}
    with active(repo):
        text = prompt(repo, {"defect_target": {"touched_files_contents": {
            "consensus-state/iteration/prior-review.yaml": yaml.safe_dump(prior_review)}}})
    packet = yaml.safe_load(text)["review_packet"]
    assert packet["open_items"][0]["summary"] == "Open finding"
    assert "OLD PATCH BODY" not in text and "OLD FULL RATIONALE" not in text


def test_resolved_source_disposition_removes_legacy_finding(repo):
    prior_review = {"findings": [{"id": "old-1", "summary": "Resolved finding"}]}
    (repo / "iteration" / "dispositions.yaml").write_text(yaml.safe_dump({"dispositions": [{
        "id": "prior-review.yaml#/findings/0", "disposition": "accepted",
        "summary": json.dumps(prior_review["findings"][0])
    }]}), encoding="utf-8")
    with active(repo):
        text = prompt(repo, {"open_items": [{"id": "old-1", "status": "unresolved"}],
                             "defect_target": {"touched_files_contents": {
            "consensus-state/iteration/prior-review.yaml": yaml.safe_dump(prior_review)}}})
    assert yaml.safe_load(text)["review_packet"]["open_items"] == []
    assert "Resolved finding" not in text


def test_http_prompt_uses_packet_and_bounded_related_context(repo):
    change(repo, 1)
    templates = Path(__file__).parents[1] / "dispatch_templates"
    profile = {"name": "deepseek", "kind": "openai_compatible"}
    ns = SimpleNamespace(profile="deepseek", profile_data=profile)
    template = prepare_template((templates / "gemini_review_template.md").read_text(encoding="utf-8"), ns)
    with active(repo, **profile):
        text = prompt(repo, {"defect_target": {"touched_files_contents": {"module.py": "NO RAW BODY"}}}, template)
    assert "No filesystem tools are available" in text
    assert "related.py" in text and "from module import changed" in text
    assert "+    return 1" in text and "NO RAW BODY" not in text
    assert text.count("# context") == 3  # Only the unified hunk's three context lines.
    assert text.count("packet_sha256:") == 1
    assert "Do NOT attempt to read files" not in text
    assert "Use repository reads" not in text


def test_full_profile_is_forwarded_and_custom_template_still_gets_packet(repo):
    change(repo, 1)
    with active(repo, packet="full"):
        text = prompt(repo, {}, "Custom template {touched_files_contents_block}")
    assert "packet_mode: full" in text
    assert text.count("packet_sha256:") == 1
    assert "{diff_packet_block}" not in text


def test_http_proposal_template_explains_no_tools():
    ns = SimpleNamespace(profile="deepseek", profile_data={"name": "deepseek", "kind": "openai_compatible"})
    assert "No filesystem tools are available" in prepare_template("Proposal {diff_packet_block}", ns)
