"""Unit tests for tools/loop_run_goal.py — Phase 4 supervisor MCP tool.

The supervisor is a state-machine coordinator: it reads filesystem state of
an iteration_dir, recognizes which phase the iteration is in, and returns
the next-action hint for the orchestrator. Optionally auto-dispatches the
codex reviewer when state == ready_for_codex.

Anti-scope (kept thin per Task #13):
  - Does NOT auto-dispatch claude reviewer.
  - Does NOT synthesize consensus.yaml.
  - Does NOT close the iteration (no iteration-outcome.yaml authoring).
  - Does NOT call Anthropic API; this is the SUPERVISOR pattern.

Tests use tmp_path for iteration_dir isolation, build goal_packets with a
matching scope_signature so cmd_validate accepts them, and monkeypatch
reviewer_dispatch_codex.handle to avoid real codex calls.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _self_drive  # noqa: E402
from consensus_mcp.tool_registry import ToolRegistry  # noqa: E402
from consensus_mcp.tools import loop_run_goal  # noqa: E402
from consensus_mcp.tools import reviewer_dispatch_codex  # noqa: E402


# ---- fixture helpers --------------------------------------------------------


def _write_valid_goal_packet(tmp_path: Path, **overrides) -> Path:
    """Write a goal_packet with matching scope_signature so cmd_validate exits 0.

    Mirrors the minimal-required-fields pattern from test_self_drive_stop_rules.py
    (_write_goal_packet) but additionally seals the canonical scope_signature so
    the supervisor's validate-then-proceed branch is exercised.
    """
    packet = {
        "schema_version": 1,
        "pilot_id": "test-supervisor",
        "goal": {"summary": "x"},
        "allowed_files": ["scripts/foo.py"],
        "allowed_sections": [],
        "forbidden_files": [],
        "max_iterations": 10,
        "max_patch_size": None,
        "validators_required": [],
        "acceptance_gates": [],
        "stop_conditions": [],
        "operator_escalation_triggers": [],
        "authorization": {"authorized_by": "operator"},
    }
    packet.update(overrides)
    sig = _self_drive._scope_signature(packet)
    packet["authorization"]["scope_signature"] = sig
    p = tmp_path / "goal_packet.yaml"
    p.write_text(yaml.safe_dump(packet), encoding="utf-8")
    return p


def _write_invalid_goal_packet_missing_field(tmp_path: Path) -> Path:
    """Goal packet missing a required field — cmd_validate returns 1."""
    packet = {
        "schema_version": 1,
        "pilot_id": "test-invalid",
        # missing 'goal', 'allowed_files', etc.
    }
    p = tmp_path / "goal_packet.yaml"
    p.write_text(yaml.safe_dump(packet), encoding="utf-8")
    return p


def _make_iter_dir(tmp_path: Path) -> Path:
    iter_dir = tmp_path / "iteration-0001"
    iter_dir.mkdir()
    return iter_dir


# ---- SCHEMA shape -----------------------------------------------------------


def test_schema_name_is_loop_run_goal():
    assert loop_run_goal.SCHEMA["name"] == "loop.run_goal"


def test_schema_required_fields():
    required = set(loop_run_goal.SCHEMA["input_schema"]["required"])
    assert required == {"goal_packet_path", "iteration_dir"}


def test_schema_optional_auto_dispatch_codex_present():
    props = loop_run_goal.SCHEMA["input_schema"]["properties"]
    assert "auto_dispatch_codex" in props


def test_schema_disallows_additional_properties():
    assert loop_run_goal.SCHEMA["input_schema"]["additionalProperties"] is False


def test_schema_output_disallows_additional_properties():
    """iter-0030 F1 (claude-rev-001): output_schema must mirror input_schema's
    additionalProperties:False so a future handle() refactor returning a stray
    debug/etc. field is caught at MCP contract level instead of silently
    leaking through to callers."""
    assert loop_run_goal.SCHEMA["output_schema"]["additionalProperties"] is False


def test_schema_output_properties_cover_all_required_fields():
    """iter-0030 F1: every key in output_schema['required'] must be declared
    in output_schema['properties']; otherwise additionalProperties:False
    would reject the handler's own return shape."""
    props = set(loop_run_goal.SCHEMA["output_schema"]["properties"].keys())
    required = set(loop_run_goal.SCHEMA["output_schema"]["required"])
    assert required.issubset(props)


def test_schema_output_required_fields():
    required = set(loop_run_goal.SCHEMA["output_schema"]["required"])
    assert required == {"ok", "state", "next_action"}


# ---- register() integration -------------------------------------------------


def test_register_adds_tool_to_registry():
    registry = ToolRegistry()
    loop_run_goal.register(registry)
    names = [t["name"] for t in registry.list_tools()]
    assert "loop.run_goal" in names


def test_register_handler_is_callable():
    registry = ToolRegistry()
    loop_run_goal.register(registry)
    handler = registry.get_handler("loop.run_goal")
    assert callable(handler)


# ---- state: goal_packet_invalid ---------------------------------------------


def test_state_goal_packet_invalid_missing_field(tmp_path):
    """Invalid goal_packet (missing required field) -> state=goal_packet_invalid, ok=False."""
    iter_dir = _make_iter_dir(tmp_path)
    bad = _write_invalid_goal_packet_missing_field(tmp_path)
    result = loop_run_goal.handle(
        goal_packet_path=str(bad),
        iteration_dir=str(iter_dir),
    )
    assert result["state"] == "goal_packet_invalid"
    assert result["ok"] is False
    assert result["error"]


def test_state_goal_packet_invalid_authorization_missing(tmp_path):
    """Goal packet missing authorization.authorized_by -> goal_packet_invalid."""
    iter_dir = _make_iter_dir(tmp_path)
    packet = {
        "schema_version": 1,
        "pilot_id": "no-auth",
        "goal": {"summary": "x"},
        "allowed_files": [],
        "max_iterations": 1,
        "validators_required": [],
        "acceptance_gates": [],
        "stop_conditions": [],
        "authorization": {},  # missing authorized_by
    }
    p = tmp_path / "goal_packet.yaml"
    p.write_text(yaml.safe_dump(packet), encoding="utf-8")
    result = loop_run_goal.handle(
        goal_packet_path=str(p),
        iteration_dir=str(iter_dir),
    )
    assert result["state"] == "goal_packet_invalid"
    assert result["ok"] is False


# ---- state: needs_implementation --------------------------------------------


def test_state_needs_implementation_empty_iter_dir(tmp_path):
    """Valid goal_packet + empty iter_dir -> needs_implementation."""
    iter_dir = _make_iter_dir(tmp_path)
    pkt = _write_valid_goal_packet(tmp_path)
    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] == "needs_implementation"
    assert "implement" in result["next_action"].lower()
    assert result["actions_taken"] == []


# ---- state: needs_claude_review ---------------------------------------------


def test_state_needs_claude_review(tmp_path):
    """review-packet.yaml present, no claude-review -> needs_claude_review."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)
    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] == "needs_claude_review"
    assert "claude" in result["next_action"].lower()


# ---- state: ready_for_codex (no auto-dispatch) ------------------------------


def test_state_ready_for_codex_no_auto_dispatch(tmp_path, monkeypatch):
    """claude-review present, no codex-review, auto_dispatch_codex=False ->
    ready_for_codex; reviewer.dispatch_codex.handle NOT called."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    called = []

    def fake_handle(**kw):
        called.append(kw)
        return {"ok": True}

    monkeypatch.setattr(reviewer_dispatch_codex, "handle", fake_handle)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
        auto_dispatch_codex=False,
    )
    assert result["ok"] is True
    assert result["state"] == "ready_for_codex"
    assert called == [], "reviewer.dispatch_codex must NOT be called when auto_dispatch_codex=False"
    assert result["actions_taken"] == []


# ---- state: ready_for_codex (auto-dispatch) ---------------------------------


def test_state_ready_for_codex_auto_dispatch_calls_handle(tmp_path, monkeypatch):
    """claude-review present, no codex-review, auto_dispatch_codex=True ->
    reviewer.dispatch_codex.handle is called; state advances afterward."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    called = []

    def fake_handle(**kw):
        called.append(kw)
        # Simulate a successful seal: write the codex-review.yaml so the
        # post-action state-recheck transitions out of ready_for_codex.
        (iter_dir / "codex-review.yaml").write_text(
            yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
        )
        return {"ok": True, "pass_id": "codex-test-pass1"}

    monkeypatch.setattr(reviewer_dispatch_codex, "handle", fake_handle)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
        auto_dispatch_codex=True,
    )
    assert result["ok"] is True
    assert len(called) == 1
    assert called[0]["goal_packet_path"] == str(pkt)
    assert called[0]["iteration_dir"] == str(iter_dir)
    # actions_taken must record the dispatch
    assert len(result["actions_taken"]) == 1
    assert result["actions_taken"][0].get("action") == "reviewer.dispatch_codex"
    # State must advance after dispatch (codex-review.yaml now exists)
    assert result["state"] == "needs_consensus"


def test_state_ready_for_codex_auto_dispatch_default_is_true(tmp_path, monkeypatch):
    """auto_dispatch_codex omitted -> defaults to True."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    called = []

    def fake_handle(**kw):
        called.append(kw)
        (iter_dir / "codex-review.yaml").write_text(
            yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
        )
        return {"ok": True}

    monkeypatch.setattr(reviewer_dispatch_codex, "handle", fake_handle)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert len(called) == 1, "default auto_dispatch_codex must be True"
    assert result["state"] == "needs_consensus"


def test_state_ready_for_codex_auto_dispatch_failure_propagates(tmp_path, monkeypatch):
    """If reviewer.dispatch_codex.handle returns ok=False, the supervisor
    surfaces the failure and remains in ready_for_codex (codex-review not sealed)."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    def fake_handle(**kw):
        return {"ok": False, "error": "codex CLI not found", "error_type": "FileNotFoundError"}

    monkeypatch.setattr(reviewer_dispatch_codex, "handle", fake_handle)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
        auto_dispatch_codex=True,
    )
    # state stays ready_for_codex (codex-review.yaml not produced)
    assert result["state"] == "ready_for_codex"
    # the dispatch attempt is recorded with ok=False
    assert len(result["actions_taken"]) == 1
    assert result["actions_taken"][0]["result"]["ok"] is False


# ---- state: needs_consensus -------------------------------------------------


def test_state_needs_consensus_both_reviews_agree(tmp_path):
    """Both reviews exist with goal_satisfied=true and no consensus yet ->
    needs_consensus."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] == "needs_consensus"
    assert "consensus" in result["next_action"].lower()


# ---- state: blocked_reviewer_disagreement -----------------------------------


def test_state_blocked_reviewer_disagreement(tmp_path):
    """Both reviews exist but goal_satisfied differs -> blocked_reviewer_disagreement."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["state"] == "blocked_reviewer_disagreement"
    # The disagreement stop rule must also surface in stop_rules_fired
    fired_rules = [r["rule"] for r in result["stop_rules_fired"]]
    assert "claude_codex_goal_satisfaction_disagreement" in fired_rules


# ---- state: ready_to_close --------------------------------------------------


def test_state_ready_to_close(tmp_path):
    """Consensus exists, no outcome, both reviewers satisfied, gates green ->
    ready_to_close."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    (iter_dir / "consensus.yaml").write_text(
        yaml.safe_dump({"reviewed_artifacts": {}}), encoding="utf-8"
    )
    # No acceptance_gates in the goal packet -> trivially green.
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] == "ready_to_close"
    assert "outcome" in result["next_action"].lower() or "close" in result["next_action"].lower()


# ---- state: closed ----------------------------------------------------------


def test_state_closed_iteration_outcome_present(tmp_path):
    """iteration-outcome.yaml present with closing_state -> closed."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "iteration-outcome.yaml").write_text(
        yaml.safe_dump({"closing_state": "quorum_close_passed"}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] == "closed"
    assert "already" in result["next_action"].lower() or "closed" in result["next_action"].lower()


# ---- state: blocked_stop_rule_fired -----------------------------------------


def test_state_blocked_stop_rule_fired_max_iterations(tmp_path):
    """max_iterations exceeded via independence-audit.yaml -> blocked_stop_rule_fired."""
    iter_dir = _make_iter_dir(tmp_path)
    # No review-packet.yaml present yet — but max iterations already hit.
    audit_log = {
        "audit_log": [
            {"event": "patch_applied"},
            {"event": "patch_applied"},
        ]
    }
    (iter_dir / "independence-audit.yaml").write_text(
        yaml.safe_dump(audit_log), encoding="utf-8"
    )
    # max_iterations=1 so two patch_applied entries trip the rule.
    pkt = _write_valid_goal_packet(tmp_path, max_iterations=1)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["state"] == "blocked_stop_rule_fired"
    fired_rules = [r["rule"] for r in result["stop_rules_fired"]]
    assert "max_iteration_count_reached" in fired_rules


# ---- Critical 1: ready_to_close requires both reviewers goal_satisfied=True --


def test_ready_to_close_blocked_when_codex_goal_satisfied_null(tmp_path):
    """consensus.yaml exists, claude=True, codex goal_satisfied missing/null
    -> NOT ready_to_close; falls through to needs_consensus."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    # codex review file present but goal_satisfied missing (None / null).
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"some_other_field": "x"}), encoding="utf-8"
    )
    (iter_dir / "consensus.yaml").write_text(
        yaml.safe_dump({"reviewed_artifacts": {}}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] != "ready_to_close"
    assert result["state"] == "needs_consensus"


def test_ready_to_close_blocked_when_claude_goal_satisfied_missing(tmp_path):
    """consensus.yaml exists, codex=True, claude goal_satisfied missing
    -> NOT ready_to_close; falls through to needs_consensus."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"some_other_field": "x"}), encoding="utf-8"
    )
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    (iter_dir / "consensus.yaml").write_text(
        yaml.safe_dump({"reviewed_artifacts": {}}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] == "needs_consensus"


def test_ready_to_close_blocked_when_both_false(tmp_path):
    """consensus.yaml exists, both reviewers goal_satisfied=False
    -> NOT ready_to_close. (Disagreement rule does not fire because they agree.)"""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}), encoding="utf-8"
    )
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}), encoding="utf-8"
    )
    (iter_dir / "consensus.yaml").write_text(
        yaml.safe_dump({"reviewed_artifacts": {}}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] != "ready_to_close"


# ---- Critical 2: missing goal_packet raises -> handled, returns ok=False -----


def test_handle_missing_goal_packet_returns_invalid_state(tmp_path):
    """goal_packet_path points to nonexistent file -> state=goal_packet_invalid,
    ok=False, no exception."""
    iter_dir = _make_iter_dir(tmp_path)
    missing = tmp_path / "does_not_exist.yaml"
    # Must NOT raise FileNotFoundError.
    result = loop_run_goal.handle(
        goal_packet_path=str(missing),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is False
    assert result["state"] == "goal_packet_invalid"
    assert result["error"]
    # Surface the read-failure shape so orchestrator can distinguish from a
    # parse failure.
    assert "read_failed" in result["error"] or "not found" in result["error"].lower() \
        or "no such file" in result["error"].lower() or "does not exist" in result["error"].lower() \
        or "errno 2" in result["error"].lower()


# ---- Important 3: reviewer_dispatch_codex.handle exception is contained -----


def test_dispatch_codex_exception_is_contained_no_propagation(tmp_path, monkeypatch):
    """If reviewer_dispatch_codex.handle raises (non-JSONDecode error like
    yaml.YAMLError, ImportError, ValueError), the supervisor must:
      - not let the exception propagate
      - record the synthetic action result with ok=False + error_type
      - leave state at ready_for_codex (no codex-review sealed)."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    def boom(**kw):
        raise ValueError("test exception")

    monkeypatch.setattr(reviewer_dispatch_codex, "handle", boom)

    # Must not raise.
    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
        auto_dispatch_codex=True,
    )
    assert result["ok"] is True
    assert result["state"] == "ready_for_codex"
    assert len(result["actions_taken"]) == 1
    rec = result["actions_taken"][0]
    assert rec["action"] == "reviewer.dispatch_codex"
    assert rec["result"]["ok"] is False
    assert rec["result"]["error_type"] == "ValueError"
    assert "test exception" in rec["result"]["error"]


def test_auto_dispatch_passes_review_target_path(tmp_path, monkeypatch):
    """codex-rev-001: auto-dispatch must pass review_target_path so codex
    receives explicit review-target-binding instead of fallback-scope review."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    called = []

    def fake_handle(**kw):
        called.append(kw)
        (iter_dir / "codex-review.yaml").write_text(
            yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
        )
        return {"ok": True, "pass_id": "codex-test-pass1"}

    monkeypatch.setattr(reviewer_dispatch_codex, "handle", fake_handle)

    loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
        auto_dispatch_codex=True,
    )

    assert len(called) == 1
    assert "review_target_path" in called[0], (
        "auto-dispatch must thread review_target_path to reviewer.dispatch_codex; "
        "without it codex falls back to scope-only review (codex-rev-001)"
    )
    expected = str(iter_dir / "review-packet.yaml")
    assert called[0]["review_target_path"] == expected, (
        f"review_target_path should point to the iteration's review-packet.yaml; "
        f"expected {expected!r}, got {called[0]['review_target_path']!r}"
    )


def test_no_orphan_yaml_import(tmp_path):
    """codex-rev-002: tools/loop_run_goal.py must not have a bare `import yaml`
    after iter-0010's _read_yaml_or_empty dedup removed the only consumer."""
    src = Path(loop_run_goal.__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        assert stripped != "import yaml", (
            "tools/loop_run_goal.py should not have a bare `import yaml` after "
            "iter-0010 dedup; the imported helper from _self_drive owns yaml usage"
        )


# ---- Task #25 (iter-0015): patch-verification state-machine extensions -----
#
# The supervisor learns to detect three new states:
#   * codex_patch_proposed                                      — codex-review.yaml has
#     at least one finding with patch_proposal AND no corresponding entry in
#     codex-patch-verifications/.
#   * patch_verified_ready_for_codex_resubmit                   — verification yaml
#     shows verdict=approved (still needs codex's post-correction re-review
#     before close).
#   * patch_corrected_by_claude_ready_for_codex_resubmit        — verification yaml
#     shows verdict=corrected_resubmit; claude's corrections need to be applied
#     and codex must re-review.
#
# The forbidden transition `claude_verifying_patch.corrected_resubmit ->
# ready_to_close` is enforced: the supervisor MUST route through
# `codex_re_reviewing_after_claude_correction` first.


def _write_codex_review_with_patch(iter_dir: Path, patch_id: str = "patch-aaaaaaaaaaaa-bbbbbbbbbbbb") -> Path:
    """Write a codex-review.yaml with one finding carrying a patch_proposal."""
    review = {
        "iteration_id": iter_dir.name,
        "reviewer_id": "codex-test-1",
        "pass_id": "codex-test-1-pass1",
        "findings": [
            {
                "id": "codex-rev-001",
                "severity": "medium",
                "summary": "Test finding",
                "citation": "scripts/foo.py:1",
                "risk": "low",
                "recommendation": "apply patch",
                "patch_proposal": {
                    "patch_id": patch_id,
                    "applies_to_findings": ["codex-rev-001"],
                    "base_sha": "abc123def456",
                    "unified_diff": "--- a\n+++ b\n@@ -1 +1,2 @@\n h\n+w\n",
                    "files_touched": ["scripts/foo.py"],
                },
            },
        ],
        "goal_satisfied": False,
        "blocking_objections": [],
        "goal_satisfied_rationale": "x",
    }
    p = iter_dir / "codex-review.yaml"
    p.write_text(yaml.safe_dump(review), encoding="utf-8")
    return p


def _write_verification_yaml(iter_dir: Path, patch_id: str, verdict: str, corrections: str | None = None) -> Path:
    out = iter_dir / "codex-patch-verifications"
    out.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "verifier": "claude",
        "verdict": verdict,
        "review_scope_hash": "f" * 64,
        "rationale": "test",
        "approved_patch_id": patch_id,
    }
    if corrections is not None:
        record["corrections"] = corrections
    p = out / f"{patch_id}.yaml"
    p.write_text(yaml.safe_dump(record), encoding="utf-8")
    return p


def test_state_codex_patch_proposed_detected_when_no_verification_yet(tmp_path):
    """codex-review.yaml has a patch_proposal; no verification yaml exists yet
    -> state = codex_patch_proposed."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}), encoding="utf-8"
    )
    _write_codex_review_with_patch(iter_dir, patch_id="patch-aaaaaaaaaaaa-bbbbbbbbbbbb")
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] == "codex_patch_proposed"


def test_state_patch_verified_ready_for_codex_resubmit_detected(tmp_path):
    """Verification yaml shows verdict=approved
    -> state = patch_verified_ready_for_codex_resubmit (NOT ready_to_close)."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}), encoding="utf-8"
    )
    patch_id = "patch-aaaaaaaaaaaa-bbbbbbbbbbbb"
    _write_codex_review_with_patch(iter_dir, patch_id=patch_id)
    _write_verification_yaml(iter_dir, patch_id=patch_id, verdict="approved")
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] == "patch_verified_ready_for_codex_resubmit"


def test_state_patch_corrected_by_claude_ready_for_codex_resubmit_detected(tmp_path):
    """Verification yaml verdict=corrected_resubmit
    -> state = patch_corrected_by_claude_ready_for_codex_resubmit."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}), encoding="utf-8"
    )
    patch_id = "patch-aaaaaaaaaaaa-bbbbbbbbbbbb"
    _write_codex_review_with_patch(iter_dir, patch_id=patch_id)
    _write_verification_yaml(
        iter_dir,
        patch_id=patch_id,
        verdict="corrected_resubmit",
        corrections="--- a\n+++ b\n",
    )
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] == "patch_corrected_by_claude_ready_for_codex_resubmit"


# ---- iter-0030 F4 (claude-rev-004): closed-vs-corrected_resubmit breadcrumb --
#
# Operator-chosen option (b): preserve existing closed-wins precedence (operator
# may legitimately close iterations bypassing the auto-pipeline) BUT emit a
# stop_rules_fired breadcrumb when iteration-outcome.yaml has closing_state AND
# a corrected_resubmit verification is still pending. This makes the
# irregularity visible in the audit signal without blocking the close.


def test_closed_state_emits_breadcrumb_when_corrected_resubmit_pending(tmp_path):
    """iter-0030 F4: when iteration-outcome.yaml closes the iter BUT a
    corrected_resubmit verification yaml exists, the supervisor returns
    state=closed (operator close honored) AND emits a synthetic
    'iteration_closed_despite_corrected_resubmit_pending' breadcrumb in
    stop_rules_fired so the audit trail records the irregularity."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}), encoding="utf-8"
    )
    patch_id = "patch-aaaaaaaaaaaa-bbbbbbbbbbbb"
    _write_codex_review_with_patch(iter_dir, patch_id=patch_id)
    _write_verification_yaml(
        iter_dir,
        patch_id=patch_id,
        verdict="corrected_resubmit",
        corrections="diff",
    )
    # Operator hand-authored close while corrected_resubmit was pending.
    (iter_dir / "iteration-outcome.yaml").write_text(
        yaml.safe_dump({"closing_state": "manual_close_landed"}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    # Closed-wins precedence preserved.
    assert result["state"] == "closed"
    # AND the irregularity is recorded as a breadcrumb (not a blocking rule).
    fired_rules = [r.get("rule") for r in result["stop_rules_fired"] if isinstance(r, dict)]
    assert "iteration_closed_despite_corrected_resubmit_pending" in fired_rules


def test_closed_state_no_breadcrumb_when_no_pending_resubmit(tmp_path):
    """iter-0030 F4 negative: ordinary closed iter without any pending
    corrected_resubmit verification must NOT emit the F4 breadcrumb."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "iteration-outcome.yaml").write_text(
        yaml.safe_dump({"closing_state": "quorum_close_passed"}), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["state"] == "closed"
    fired_rules = [r.get("rule") for r in result["stop_rules_fired"] if isinstance(r, dict)]
    assert "iteration_closed_despite_corrected_resubmit_pending" not in fired_rules


# ---- iter-0030 F2 (codex-rev-001 + claude-rev-003): closure-invariant tests --


def test_blocked_closure_invariant_failed_positive(tmp_path):
    """iter-0030 F2: independence-audit.yaml carrying an apply_step_landed
    event WITH last_mutation.actor.model_family AND a stale same-family
    closing review (so cross_family fails) must produce
    state=blocked_closure_invariant_failed.

    The supervisor's _closure_invariant_blocks_close gate selects the most
    recent post-mutation review and runs check_closure_invariant. A
    same-family closer fails cross_family -> invariant fails -> state
    blocked_closure_invariant_failed (per _NEXT_ACTION key wiring at line
    122-124)."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )

    apply_ts = "2026-05-10T22:00:00+00:00"
    closer_ts = "2026-05-10T22:30:00+00:00"

    # Closer is SAME family as last_mutation -> cross_family fails.
    closer_actor = {"id": "claude-bad", "model_family": "claude"}
    mutation_actor = {"id": "claude-mutator", "model_family": "claude"}
    post_sha = "deadbeef" * 8  # 64-char hex

    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({
            "goal_satisfied": True,
            "actor": closer_actor,
            "review_target_hash": post_sha,
            "created_at_utc": closer_ts,
        }),
        encoding="utf-8",
    )
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({
            "goal_satisfied": True,
            "actor": {"id": "codex-stale", "model_family": "codex"},
            "review_target_hash": post_sha,
            # codex closer is OLDER than apply -> not a post-mutation review.
            "created_at_utc": "2026-05-10T21:00:00+00:00",
        }),
        encoding="utf-8",
    )
    (iter_dir / "consensus.yaml").write_text(
        yaml.safe_dump({"reviewed_artifacts": {}}), encoding="utf-8"
    )

    # Audit log carries the apply_step_landed event with structured
    # last_mutation per iter-0017 capstone nesting.
    audit = {
        "audit_log": [
            {
                "event": "apply_step_landed",
                "timestamp_utc": apply_ts,
                "last_mutation": {
                    "actor": mutation_actor,
                    "post_sha": post_sha,
                    "timestamp_utc": apply_ts,
                },
            }
        ]
    }
    (iter_dir / "independence-audit.yaml").write_text(
        yaml.safe_dump(audit), encoding="utf-8"
    )
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] == "blocked_closure_invariant_failed"


def test_blocked_closure_invariant_failed_negative_clean_state_advances(tmp_path):
    """iter-0030 F2 negative: clean state (no apply_step_landed events in
    the audit log) -> last_mutation is None -> invariant trivially passes ->
    state advances normally to ready_to_close."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}), encoding="utf-8"
    )
    (iter_dir / "consensus.yaml").write_text(
        yaml.safe_dump({"reviewed_artifacts": {}}), encoding="utf-8"
    )
    # No independence-audit.yaml -> no last_mutation -> invariant passes.
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["ok"] is True
    assert result["state"] == "ready_to_close"


# ---- iter-0030 F3 (claude-rev-002): _check_stop_rules try/except --------------


def test_check_stop_rules_swallows_exception_returns_breadcrumb(tmp_path, monkeypatch):
    """iter-0030 F3: if _self_drive.cmd_check_stop_rules raises (e.g.,
    yaml.YAMLError on a malformed independence-audit.yaml), the exception
    must NOT escape handle() and must surface as a synthetic
    check_stop_rules_failed breadcrumb. Asymmetric defensive depth versus
    _validate_goal_packet's existing try/except was the documented gap."""
    iter_dir = _make_iter_dir(tmp_path)
    pkt = _write_valid_goal_packet(tmp_path)

    def boom(*args, **kwargs):
        raise ValueError("malformed independence-audit.yaml: bad mapping")

    monkeypatch.setattr(_self_drive, "cmd_check_stop_rules", boom)

    # Must not raise.
    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    fired_rules = [r.get("rule") for r in result["stop_rules_fired"] if isinstance(r, dict)]
    assert "check_stop_rules_failed" in fired_rules
    # The breadcrumb must carry the exception detail.
    detail_blob = next(
        r.get("detail", "")
        for r in result["stop_rules_fired"]
        if isinstance(r, dict) and r.get("rule") == "check_stop_rules_failed"
    )
    assert "malformed independence-audit.yaml" in detail_blob
    # check_stop_rules_failed is a BREADCRUMB (mirrors review_yaml_parse_failed
    # / git_check_failed at line 212); it must NOT trip blocked_stop_rule_fired
    # by itself — the supervisor should advance to needs_implementation when
    # the iter_dir is empty.
    assert result["state"] == "needs_implementation"


def test_check_stop_rules_normal_path_still_works(tmp_path):
    """iter-0030 F3 negative: the try/except wrapper must not regress the
    happy path — _self_drive.cmd_check_stop_rules running cleanly returns
    the parsed stop_rules_fired list unchanged."""
    iter_dir = _make_iter_dir(tmp_path)
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    # Empty iter_dir + clean goal_packet -> no real stop rules.
    fired_rules = [r.get("rule") for r in result["stop_rules_fired"] if isinstance(r, dict)]
    assert "check_stop_rules_failed" not in fired_rules
    assert result["state"] == "needs_implementation"


def test_corrected_resubmit_does_NOT_advance_to_ready_to_close(tmp_path):
    """A corrected_resubmit verification verdict blocks the close path
    (must route through codex_re_reviewing_after_claude_correction first
    per spec). Even with consensus.yaml present and both reviewers
    goal_satisfied=False (no disagreement), the patch state takes
    precedence over needs_consensus / ready_to_close."""
    iter_dir = _make_iter_dir(tmp_path)
    (iter_dir / "review-packet.yaml").write_text(
        yaml.safe_dump({"iteration_artifacts": []}), encoding="utf-8"
    )
    # Both reviewers explicitly goal_satisfied=False so no disagreement
    # stop-rule fires; the test is about precedence between patch state
    # and consensus/ready_to_close, not about stop rules.
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}), encoding="utf-8"
    )
    patch_id = "patch-aaaaaaaaaaaa-bbbbbbbbbbbb"
    _write_codex_review_with_patch(iter_dir, patch_id=patch_id)
    # Even include a consensus.yaml — should NOT advance to ready_to_close.
    (iter_dir / "consensus.yaml").write_text(
        yaml.safe_dump({"reviewed_artifacts": {}}), encoding="utf-8"
    )
    _write_verification_yaml(
        iter_dir,
        patch_id=patch_id,
        verdict="corrected_resubmit",
        corrections="diff",
    )
    pkt = _write_valid_goal_packet(tmp_path)

    result = loop_run_goal.handle(
        goal_packet_path=str(pkt),
        iteration_dir=str(iter_dir),
    )
    assert result["state"] != "ready_to_close"
    assert result["state"] == "patch_corrected_by_claude_ready_for_codex_resubmit"
