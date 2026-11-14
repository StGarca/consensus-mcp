"""Unit tests for codified stop rules in _self_drive.py:

Task #11 rules:
  - claude_codex_goal_satisfaction_disagreement
  - patch_size_exceeds_max
  - any_acceptance_gate_returns_undefined

Task #12 rules:
  - repeated_finding_class_unresolved
  - cross_document_drift_detected
  - validator_reviewer_disagreement

Tests are TDD-style (red-first) and use monkeypatch to mock subprocess for
deterministic behavior independent of repo state.
"""
from __future__ import annotations
import hashlib as _hashlib
import json as _json
import sys
import unittest.mock as _mock
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

from consensus_mcp import _self_drive  # noqa: E402


def _canonical_sha_of_file(p: Path) -> str:
    """Helper: canonical-full sha256 of the given YAML file."""
    loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    canonical = yaml.safe_dump(loaded, sort_keys=True)
    return _hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---- helpers ----------------------------------------------------------------


def _write_goal_packet(tmp_path: Path, **overrides) -> Path:
    """Minimal goal_packet that passes cmd_check_stop_rules's required-fields path."""
    packet = {
        "schema_version": 1,
        "pilot_id": "test-pilot",
        "goal": {"summary": "x"},
        "allowed_files": ["scripts/foo.py"],
        "forbidden_files": [],
        "max_iterations": 10,
        "max_patch_size": None,
        "validators_required": [],
        "acceptance_gates": [],
        "stop_conditions": [],
        "authorization": {"authorized_by": "operator"},
    }
    packet.update(overrides)
    p = tmp_path / "goal_packet.yaml"
    p.write_text(yaml.safe_dump(packet), encoding="utf-8")
    return p


def _run_check_stop_rules(packet_path: Path, iter_dir: Path, capsys):
    """Invoke cmd_check_stop_rules and return (rc, parsed_json_output)."""
    import argparse
    rc = _self_drive.cmd_check_stop_rules(
        argparse.Namespace(goal_packet=str(packet_path), iteration_dir=str(iter_dir))
    )
    out = capsys.readouterr().out
    parsed = _json.loads(out)
    return rc, parsed


def _fired_rules(parsed) -> list[str]:
    return [r["rule"] for r in parsed.get("stop_rules_fired", [])]


# ---- claude_codex_goal_satisfaction_disagreement ---------------------------


def test_disagreement_neither_review_present_does_not_fire(tmp_path, capsys):
    """No claude-review.yaml or codex-review.yaml -> rule does NOT fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "claude_codex_goal_satisfaction_disagreement" not in _fired_rules(parsed)


def test_disagreement_only_claude_review_present_does_not_fire(tmp_path, capsys):
    """claude-review present, codex-review missing -> NOT a disagreement (loop in flight)."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"overall_position": {"goal_satisfied": True}}),
        encoding="utf-8",
    )
    packet = _write_goal_packet(tmp_path)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "claude_codex_goal_satisfaction_disagreement" not in _fired_rules(parsed)


def test_disagreement_both_reviews_agree_does_not_fire(tmp_path, capsys):
    """Both reviews present + values match -> rule does NOT fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"overall_position": {"goal_satisfied": True}}),
        encoding="utf-8",
    )
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}),
        encoding="utf-8",
    )
    packet = _write_goal_packet(tmp_path)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "claude_codex_goal_satisfaction_disagreement" not in _fired_rules(parsed)


def test_disagreement_both_reviews_disagree_fires(tmp_path, capsys):
    """Both reviews present + values differ -> rule FIRES."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"overall_position": {"goal_satisfied": True}}),
        encoding="utf-8",
    )
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}),
        encoding="utf-8",
    )
    packet = _write_goal_packet(tmp_path)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "claude_codex_goal_satisfaction_disagreement" in fired
    # Reason text exposes both values
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "claude_codex_goal_satisfaction_disagreement")
    assert entry["claude_goal_satisfied"] is True
    assert entry["codex_goal_satisfied"] is False


def test_disagreement_malformed_yaml_records_breadcrumb(tmp_path, capsys):
    """Malformed claude-review.yaml -> rule does NOT fire BUT a
    'review_yaml_parse_failed' breadcrumb appears in stop_rules_fired so the
    operator gets a signal (parallel to existing git_check_failed precedent)."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    # Malformed YAML: unmatched bracket / invalid block mapping
    (iter_dir / "claude-review.yaml").write_text("not: [valid: yaml", encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}),
        encoding="utf-8",
    )
    packet = _write_goal_packet(tmp_path)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    # Disagreement rule itself does NOT fire (we have no parsed claude review)
    assert "claude_codex_goal_satisfaction_disagreement" not in fired
    # But a breadcrumb DOES appear, naming the malformed path + exception
    assert "review_yaml_parse_failed" in fired
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "review_yaml_parse_failed")
    assert "claude-review.yaml" in entry["path"]
    assert entry["exception"]


def test_disagreement_top_level_claude_goal_satisfied_also_works(tmp_path, capsys):
    """If claude-review uses top-level goal_satisfied (not overall_position), still detect."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "claude-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": False}),
        encoding="utf-8",
    )
    (iter_dir / "codex-review.yaml").write_text(
        yaml.safe_dump({"goal_satisfied": True}),
        encoding="utf-8",
    )
    packet = _write_goal_packet(tmp_path)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "claude_codex_goal_satisfaction_disagreement" in _fired_rules(parsed)


# ---- patch_size_exceeds_max -------------------------------------------------


def test_patch_size_max_unset_does_not_fire(tmp_path, capsys, monkeypatch):
    """max_patch_size unset (None) -> rule never fires regardless of diff size."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path, max_patch_size=None)

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        # Big diff but should not fire because cap is None
        if "--numstat" in cmd:
            result.stdout = "500\t500\tscripts/foo.py\n"
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "patch_size_exceeds_max" not in _fired_rules(parsed)


def test_patch_size_below_cap_does_not_fire(tmp_path, capsys, monkeypatch):
    """Patch size below max_patch_size -> rule does not fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path, max_patch_size=100)

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        if "--numstat" in cmd:
            # 30 added + 20 deleted = 50; cap=100 -> ok
            result.stdout = "30\t20\tscripts/foo.py\n"
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "patch_size_exceeds_max" not in _fired_rules(parsed)


def test_patch_size_exceeds_cap_fires(tmp_path, capsys, monkeypatch):
    """Total added+deleted exceeds max_patch_size -> rule fires."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path, max_patch_size=50)

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        if "--numstat" in cmd:
            # 100 + 10 = 110 vs cap 50 -> fire
            result.stdout = "100\t10\tscripts/foo.py\n5\t5\tscripts/bar.py\n"
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "patch_size_exceeds_max" in fired
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "patch_size_exceeds_max")
    assert entry["patch_size"] == 120  # 100+10+5+5
    assert entry["max"] == 50


def test_patch_size_git_failure_does_not_fire(tmp_path, capsys, monkeypatch):
    """git fails -> treat as missing data; rule does not fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path, max_patch_size=10)

    def fake_run(cmd, **kwargs):
        # Existing forbidden_files check uses --name-only; numstat is the new path.
        # Make numstat raise to simulate git failure for size calc.
        if "--numstat" in cmd:
            raise OSError("git missing")
        result = _mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "patch_size_exceeds_max" not in _fired_rules(parsed)


# ---- iter-0035 patch_size_exceeds_max - union counting (codex-rev-001/002) -


def _make_size_fake_run(numstat_lines: str = "", untracked_files: list[str] | None = None):
    """Build a fake subprocess.run that returns numstat output for `git diff
    --numstat HEAD` and a list of untracked-file relpaths for
    `git ls-files --others --exclude-standard`. Other cmds get empty stdout.
    """
    untracked = untracked_files or []

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        if "--numstat" in cmd:
            result.stdout = numstat_lines
        elif "--others" in cmd:
            result.stdout = "\n".join(untracked) + ("\n" if untracked else "")
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    return fake_run


def test_patch_size_counts_untracked_text_file(tmp_path, capsys, monkeypatch):
    """iter-0035 codex-rev-001 regression: untracked text files contribute
    their full line count. Previously untracked never counted.
    """
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path, max_patch_size=100)

    # Create an untracked text file with 200 newline-terminated lines.
    untracked_file = tmp_path / "new_module.py"
    untracked_file.write_text("line\n" * 200, encoding="utf-8")

    monkeypatch.setattr(_self_drive, "_resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(_self_drive.subprocess, "run",
                        _make_size_fake_run(numstat_lines="",
                                            untracked_files=["new_module.py"]))
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "patch_size_exceeds_max" in fired, (
        f"200-line untracked file should exceed cap=100; fired={fired}"
    )
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "patch_size_exceeds_max")
    assert entry["patch_size"] == 200, f"expected 200; got {entry}"


def test_patch_size_untracked_symlink_does_not_dereference(tmp_path, capsys, monkeypatch):
    """iter-0035 codex-rev-001 (pre-review refinement): untracked symlinks
    are counted as 1 line (the link itself), not dereferenced. Following
    the symlink could count an arbitrary out-of-repo target or crash on
    broken links - neither matches `git add -A && git commit` semantics.
    """
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path, max_patch_size=10)

    # Create a HUGE target outside the repo, then a symlink inside the repo
    # pointing at it. If the patch dereferences, total >> cap; correct
    # behavior counts the symlink as 1.
    huge_target = tmp_path.parent / "huge_target_for_symlink.txt"
    huge_target.write_text("X\n" * 100000, encoding="utf-8")
    link_path = tmp_path / "link_to_outside.txt"
    try:
        link_path.symlink_to(huge_target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")

    monkeypatch.setattr(_self_drive, "_resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(_self_drive.subprocess, "run",
                        _make_size_fake_run(numstat_lines="",
                                            untracked_files=["link_to_outside.txt"]))
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    # cap=10; symlink counted as 1 -> no fire. Pre-fix: dereferenced to
    # 100k lines, fires (false-positive based on out-of-repo content).
    assert "patch_size_exceeds_max" not in fired, (
        f"symlink should be counted as 1 (not dereferenced); fired={fired}"
    )


def test_patch_size_counts_no_trailing_newline_multi_line(tmp_path, capsys, monkeypatch):
    """iter-0035 codex-rev-002 (pre-review refinement): a multi-line file
    without trailing newline counts the final line. Pre-fix text.count("\\n")
    gave N-1 for an N-line file; post-fix gives N.
    """
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path, max_patch_size=2)  # discriminating cap

    # 3 lines, no trailing newline. Pre-fix: count("\n") = 2 -> 2 > 2 False -> no fire.
    # Post-fix: 2 + 1 = 3 -> 3 > 2 True -> fire.
    untracked_file = tmp_path / "three_lines_no_newline.txt"
    untracked_file.write_text("a\nb\nc", encoding="utf-8")

    monkeypatch.setattr(_self_drive, "_resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(_self_drive.subprocess, "run",
                        _make_size_fake_run(numstat_lines="",
                                            untracked_files=["three_lines_no_newline.txt"]))
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "patch_size_exceeds_max" in fired, (
        f"3-line no-newline file should count as 3 (post-fix), cap=2 -> fire; got {fired}"
    )
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "patch_size_exceeds_max")
    assert entry["patch_size"] == 3, f"expected 3; got {entry}"


def test_patch_size_treats_untracked_binary_as_zero(tmp_path, capsys, monkeypatch):
    """iter-0035: binary untracked files contribute 0 lines (UnicodeDecodeError
    silently skips, mirroring numstat's '-\\t-' -> 0 policy).
    """
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path, max_patch_size=10)

    binary_file = tmp_path / "image.bin"
    binary_file.write_bytes(b"\xff\xfe\x00\x01\x02" * 1000)

    monkeypatch.setattr(_self_drive, "_resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(_self_drive.subprocess, "run",
                        _make_size_fake_run(numstat_lines="",
                                            untracked_files=["image.bin"]))
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "patch_size_exceeds_max" not in fired, (
        f"binary untracked should count as 0; fired={fired}"
    )


# ---- iter-0035 reviewer_clean blocker handling (codex-rev-002) -------------


def test_reviewer_with_blockers_missing_goal_satisfied_is_not_clean(tmp_path, capsys, monkeypatch):
    """iter-0035 codex-rev-002 + claude-rev-002 regression: a reviewer that
    emits non-empty blocking_objections but omits goal_satisfied must NOT
    be treated as clean. Pre-fix _reviewer_clean returned None and the
    caller's `clean is False` identity check missed the blocker.
    """
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path)

    # Synthetic claude-review with blocker but no goal_satisfied.
    (iter_dir / "claude-review.yaml").write_text(
        "blocking_objections:\n  - id: claude-rev-X\n    severity: blocking\n",
        encoding="utf-8",
    )
    # Codex review says clean, so the disagreement check runs with one
    # blocker + one clean reviewer.
    (iter_dir / "codex-review.yaml").write_text(
        "goal_satisfied: true\nblocking_objections: []\n",
        encoding="utf-8",
    )
    # Synthesize a passing validator status.
    (iter_dir / "review-packet.yaml").write_text(
        "verification_checks:\n  acceptance_gates_evaluated:\n    all_passed: true\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(_self_drive, "_resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(_self_drive.subprocess, "run", _make_size_fake_run())
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "validator_reviewer_disagreement" in fired, (
        f"blocker without goal_satisfied must trigger disagreement; fired={fired}"
    )
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "validator_reviewer_disagreement")
    assert "claude" in entry["disagreeing_reviewers"]


def test_reviewer_with_no_signal_is_treated_as_in_flight(tmp_path, capsys, monkeypatch):
    """iter-0035: reviewer with no blockers AND no goal_satisfied stays
    in-flight (returns None from _reviewer_clean -> not in disagreers).
    """
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path)

    # Both reviewers exist but have NEITHER blockers nor goal_satisfied.
    (iter_dir / "claude-review.yaml").write_text("findings: []\n", encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text("findings: []\n", encoding="utf-8")
    (iter_dir / "review-packet.yaml").write_text(
        "verification_checks:\n  acceptance_gates_evaluated:\n    all_passed: true\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(_self_drive, "_resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(_self_drive.subprocess, "run", _make_size_fake_run())
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "validator_reviewer_disagreement" not in fired, (
        f"in-flight reviewers (no signal) should NOT trigger disagreement; fired={fired}"
    )


# ---- iter-0035 closure_invariant import failure (codex-rev-003) ------------


def test_closure_invariant_import_failure_emits_stop_rule(tmp_path, capsys, monkeypatch):
    """iter-0035 codex-rev-003 + claude-rev-003 regression: when
    `from consensus_mcp._closure_invariant import ...` raises
    ImportError, cmd_check_stop_rules emits a closure_invariant_module_
    unavailable stop_rule. Pre-fix this failed silently (set _check_inv=None
    and dropped 9/9 runtime coverage to 8/9 with no operator signal).
    """
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path)

    # Force ImportError on the closure_invariant import by setting the
    # module entry to None in sys.modules (Python raises ImportError on
    # `from None.x import y`).
    monkeypatch.setitem(sys.modules, "consensus_mcp._closure_invariant", None)
    monkeypatch.setattr(_self_drive, "_resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(_self_drive.subprocess, "run", _make_size_fake_run())
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "closure_invariant_module_unavailable" in fired, (
        f"import failure should emit closure_invariant_module_unavailable; fired={fired}"
    )
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "closure_invariant_module_unavailable")
    assert "exception" in entry, f"entry should carry exception field; got {entry}"


# ---- iter-0035 _resolve_referenced_path basename fallback (codex-rev-004) --


def test_resolve_referenced_path_basename_only_falls_back_to_iter_dir(tmp_path, monkeypatch):
    """iter-0035: bare basename (no directory components) MISSING under
    repo_root still falls back to iter_dir/<basename> - preserving the
    legitimate iter-relative-fixture pattern.
    """
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    monkeypatch.setattr(_self_drive, "_resolve_repo_root", lambda: tmp_path)
    resolved = _self_drive._resolve_referenced_path("foo.yaml", iter_dir)
    assert resolved == iter_dir / "foo.yaml", (
        f"bare basename missing under repo_root should fall back to iter_dir; got {resolved}"
    )


def test_resolve_referenced_path_with_dirs_does_not_fall_back_to_iter_dir(tmp_path, monkeypatch):
    """iter-0035 codex-rev-004 + claude-rev-004 regression: a missing path
    WITH directory components (e.g., 'wiki/notes/foo.md') does NOT fall
    back to iter_dir/foo.md. Pre-fix this allowed a same-name file in
    iter_dir to satisfy a drift hash check intended for a different
    repo-root location.
    """
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    monkeypatch.setattr(_self_drive, "_resolve_repo_root", lambda: tmp_path)
    resolved = _self_drive._resolve_referenced_path("wiki/notes/foo.md", iter_dir)
    # Post-fix: returns the (missing) repo_root/wiki/notes/foo.md, NOT
    # iter_dir/foo.md.
    assert resolved == tmp_path / "wiki" / "notes" / "foo.md", (
        f"path with directory components should resolve under repo_root; got {resolved}"
    )
    assert resolved != iter_dir / "foo.md", (
        f"must NOT collapse to basename-in-iter_dir; got {resolved}"
    )


# ---- any_acceptance_gate_returns_undefined ---------------------------------


def test_undefined_gate_all_clean_does_not_fire(tmp_path, capsys, monkeypatch):
    """All gates return clean rc=0 or rc=1 -> rule does not fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(
        tmp_path,
        acceptance_gates=[
            {"id": "g1", "check": "echo hi"},
            {"id": "g2", "check": "echo bye"},
        ],
    )

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        result.stdout = "hi\n"
        result.stderr = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "any_acceptance_gate_returns_undefined" not in _fired_rules(parsed)


def test_undefined_gate_exception_fires(tmp_path, capsys, monkeypatch):
    """Gate whose check raises (subprocess error) -> rule fires."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(
        tmp_path,
        acceptance_gates=[{"id": "g1", "check": "bogus"}],
    )

    def fake_run(cmd, **kwargs):
        # Raise only on the gate's shell check (shell=True). git numstat etc still work.
        if kwargs.get("shell"):
            raise OSError("boom")
        result = _mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "any_acceptance_gate_returns_undefined" in fired
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "any_acceptance_gate_returns_undefined")
    # Must surface which gate(s) and why
    assert "g1" in str(entry)


def test_undefined_gate_empty_check_fires(tmp_path, capsys, monkeypatch):
    """Gate with empty check field -> 'no_check_command' undefined; rule fires."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(
        tmp_path,
        acceptance_gates=[{"id": "g1", "check": ""}],
    )

    # No subprocess mock needed for this path; gate-eval short-circuits on empty check.
    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "any_acceptance_gate_returns_undefined" in _fired_rules(parsed)


# ---- coverage tuple update --------------------------------------------------


def test_stop_rules_implemented_includes_three_new_rules():
    """STOP_RULES_IMPLEMENTED tuple should now list the 3 newly-codified rules."""
    for name in (
        "claude_codex_goal_satisfaction_disagreement",
        "patch_size_exceeds_max",
        "any_acceptance_gate_returns_undefined",
    ):
        assert name in _self_drive.STOP_RULES_IMPLEMENTED, \
            f"{name} should be listed in STOP_RULES_IMPLEMENTED"


# ---- regression: existing 2 rules still work --------------------------------


def test_regression_max_iteration_count_still_fires(tmp_path, capsys, monkeypatch):
    """Existing max_iteration_count_reached rule still fires when count >= max."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    audit = {"audit_log": [
        {"event": "patch_applied"}, {"event": "patch_applied"},
        {"event": "patch_applied"},
    ]}
    (iter_dir / "independence-audit.yaml").write_text(yaml.safe_dump(audit), encoding="utf-8")
    packet = _write_goal_packet(tmp_path, max_iterations=2)

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "max_iteration_count_reached" in _fired_rules(parsed)


def test_regression_forbidden_files_still_fires(tmp_path, capsys, monkeypatch):
    """Existing patch_would_touch_forbidden_files still fires."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(
        tmp_path,
        forbidden_files=["scripts/forbidden.py"],
    )

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        if "--name-only" in cmd:
            result.stdout = "scripts/forbidden.py\n"
        elif "--numstat" in cmd:
            result.stdout = ""
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "patch_would_touch_forbidden_files" in _fired_rules(parsed)


# ---- repeated_finding_class_unresolved -------------------------------------


def _make_iter_pair(tmp_path: Path):
    """Create two sibling iteration dirs (prior + current) under a parent.

    Returns (parent_dir, prior_dir, current_dir). 'iteration-0001' sorts before
    'iteration-0002' lexicographically, so 'iteration-0001' is the prior.
    """
    parent = tmp_path / "active"
    parent.mkdir()
    prior = parent / "iteration-0001"
    prior.mkdir()
    current = parent / "iteration-0002"
    current.mkdir()
    return parent, prior, current


def test_repeated_finding_no_prior_iteration_does_not_fire(tmp_path, capsys, monkeypatch):
    """Only one iteration in parent dir -> no prior -> rule does NOT fire."""
    parent = tmp_path / "active"
    parent.mkdir()
    current = parent / "iteration-0001"
    current.mkdir()
    (current / "claude-review.yaml").write_text(
        yaml.safe_dump({
            "goal_satisfied": True,
            "non_blocking_suggestions": [{"id": "claude-001", "text": "x"}],
        }),
        encoding="utf-8",
    )
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, current, capsys)
    assert "repeated_finding_class_unresolved" not in _fired_rules(parsed)


def test_repeated_finding_no_overlap_does_not_fire(tmp_path, capsys, monkeypatch):
    """Prior + current both have findings, but ids do not overlap -> no fire."""
    _, prior, current = _make_iter_pair(tmp_path)
    (prior / "claude-review.yaml").write_text(
        yaml.safe_dump({
            "goal_satisfied": True,
            "non_blocking_suggestions": [{"id": "claude-001", "text": "x"}],
        }),
        encoding="utf-8",
    )
    (current / "claude-review.yaml").write_text(
        yaml.safe_dump({
            "goal_satisfied": True,
            "non_blocking_suggestions": [{"id": "claude-099", "text": "y"}],
        }),
        encoding="utf-8",
    )
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, current, capsys)
    assert "repeated_finding_class_unresolved" not in _fired_rules(parsed)


def test_repeated_finding_overlap_fires(tmp_path, capsys, monkeypatch):
    """Same finding id in claude-review of BOTH prior + current -> rule FIRES."""
    _, prior, current = _make_iter_pair(tmp_path)
    (prior / "claude-review.yaml").write_text(
        yaml.safe_dump({
            "goal_satisfied": True,
            "non_blocking_suggestions": [{"id": "claude-iter0001-001", "text": "x"}],
        }),
        encoding="utf-8",
    )
    (current / "claude-review.yaml").write_text(
        yaml.safe_dump({
            "goal_satisfied": True,
            "non_blocking_suggestions": [{"id": "claude-iter0001-001", "text": "still x"}],
        }),
        encoding="utf-8",
    )
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, current, capsys)
    fired = _fired_rules(parsed)
    assert "repeated_finding_class_unresolved" in fired
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "repeated_finding_class_unresolved")
    assert "claude-iter0001-001" in str(entry)
    assert "iteration-0001" in str(entry)


def test_repeated_finding_codex_overlap_fires(tmp_path, capsys, monkeypatch):
    """Same finding id in codex-review.findings of BOTH prior + current -> FIRES."""
    _, prior, current = _make_iter_pair(tmp_path)
    (prior / "codex-review.yaml").write_text(
        yaml.safe_dump({
            "goal_satisfied": True,
            "findings": [{"id": "codex-iter0001-007", "severity": "low"}],
        }),
        encoding="utf-8",
    )
    (current / "codex-review.yaml").write_text(
        yaml.safe_dump({
            "goal_satisfied": True,
            "findings": [{"id": "codex-iter0001-007", "severity": "low"}],
        }),
        encoding="utf-8",
    )
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, current, capsys)
    assert "repeated_finding_class_unresolved" in _fired_rules(parsed)


def test_repeated_finding_cross_reviewer_id_does_not_fire(tmp_path, capsys, monkeypatch):
    """Same id string but different reviewer (claude-prior vs codex-current)
    -> different id space -> rule does NOT fire."""
    _, prior, current = _make_iter_pair(tmp_path)
    (prior / "claude-review.yaml").write_text(
        yaml.safe_dump({
            "non_blocking_suggestions": [{"id": "shared-id-007", "text": "x"}],
        }),
        encoding="utf-8",
    )
    (current / "codex-review.yaml").write_text(
        yaml.safe_dump({
            "findings": [{"id": "shared-id-007", "severity": "low"}],
        }),
        encoding="utf-8",
    )
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, current, capsys)
    assert "repeated_finding_class_unresolved" not in _fired_rules(parsed)


def test_repeated_finding_prior_has_no_review_yamls_does_not_fire(tmp_path, capsys, monkeypatch):
    """Prior dir exists but has no review yamls -> no comparison -> no fire."""
    _, prior, current = _make_iter_pair(tmp_path)
    # prior is empty (no review files)
    (current / "claude-review.yaml").write_text(
        yaml.safe_dump({
            "non_blocking_suggestions": [{"id": "claude-001"}],
        }),
        encoding="utf-8",
    )
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, current, capsys)
    assert "repeated_finding_class_unresolved" not in _fired_rules(parsed)


# ---- cross_document_drift_detected -----------------------------------------


def test_drift_no_documents_present_does_not_fire(tmp_path, capsys, monkeypatch):
    """No iteration documents at all -> nothing to compare -> no fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "cross_document_drift_detected" not in _fired_rules(parsed)


def test_drift_review_packet_artifact_sha_correct_does_not_fire(tmp_path, capsys, monkeypatch):
    """review-packet.iteration_artifacts[].canonical_sha256 matches actual -> no fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    # Create a referenced artifact with a known canonical sha
    art = iter_dir / "input.yaml"
    art.write_text(yaml.safe_dump({"some": "thing"}), encoding="utf-8")
    correct_sha = _canonical_sha_of_file(art)
    rp = iter_dir / "review-packet.yaml"
    rp.write_text(yaml.safe_dump({
        "iteration_artifacts": [
            {"path": str(art).replace("\\", "/"), "canonical_sha256": correct_sha},
        ],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "cross_document_drift_detected" not in _fired_rules(parsed)


def test_drift_review_packet_artifact_sha_mismatch_fires(tmp_path, capsys, monkeypatch):
    """review-packet artifact canonical_sha256 stale vs file -> rule FIRES."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    art = iter_dir / "input.yaml"
    art.write_text(yaml.safe_dump({"some": "thing"}), encoding="utf-8")
    rp = iter_dir / "review-packet.yaml"
    rp.write_text(yaml.safe_dump({
        "iteration_artifacts": [
            {"path": str(art).replace("\\", "/"), "canonical_sha256": "0" * 64},
        ],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "cross_document_drift_detected" in fired
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "cross_document_drift_detected")
    assert entry["claimed_sha"] == "0" * 64
    assert entry["actual_sha"] != "0" * 64


def test_drift_referenced_file_missing_fires(tmp_path, capsys, monkeypatch):
    """review-packet references a file that does not exist -> FIRES."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    rp = iter_dir / "review-packet.yaml"
    rp.write_text(yaml.safe_dump({
        "iteration_artifacts": [
            {"path": "consensus-state/active/iter-x/missing.yaml",
             "canonical_sha256": "f" * 64},
        ],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "cross_document_drift_detected" in fired
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "cross_document_drift_detected")
    assert entry.get("reason") == "referenced_file_missing"


def test_drift_claude_reviewed_packet_sha_mismatch_fires(tmp_path, capsys, monkeypatch):
    """claude-review.reviewed_packet_sha256 stale vs review-packet.yaml -> FIRES."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    rp = iter_dir / "review-packet.yaml"
    rp.write_text(yaml.safe_dump({"some": "content"}), encoding="utf-8")
    cr = iter_dir / "claude-review.yaml"
    cr.write_text(yaml.safe_dump({
        "reviewed_packet_sha256": "deadbeef" * 8,  # 64-char fake
        "goal_satisfied": True,
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "cross_document_drift_detected" in fired


def test_drift_consensus_artifact_sha_mismatch_fires(tmp_path, capsys, monkeypatch):
    """consensus.reviewed_artifacts.*.canonical_sha256 stale -> FIRES."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    rp = iter_dir / "review-packet.yaml"
    rp.write_text(yaml.safe_dump({"some": "content"}), encoding="utf-8")
    cons = iter_dir / "consensus.yaml"
    cons.write_text(yaml.safe_dump({
        "reviewed_artifacts": {
            "review_packet": {
                "path": str(rp).replace("\\", "/"),
                "canonical_sha256": "0" * 64,
            },
        },
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "cross_document_drift_detected" in _fired_rules(parsed)


def test_drift_missing_sha_field_does_not_fire(tmp_path, capsys, monkeypatch):
    """File present but referenced sha is null/missing -> no fire (nothing to check)."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    rp = iter_dir / "review-packet.yaml"
    rp.write_text(yaml.safe_dump({
        "iteration_artifacts": [
            {"path": str((iter_dir / "input.yaml")).replace("\\", "/")},  # no sha
        ],
    }), encoding="utf-8")
    (iter_dir / "input.yaml").write_text(yaml.safe_dump({"x": 1}), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "cross_document_drift_detected" not in _fired_rules(parsed)


# ---- validator_reviewer_disagreement ---------------------------------------


def test_validator_reviewer_no_review_packet_does_not_fire(tmp_path, capsys, monkeypatch):
    """No review-packet.yaml -> no validator data -> no fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "validator_reviewer_disagreement" not in _fired_rules(parsed)


def test_validator_pass_reviewer_pass_does_not_fire(tmp_path, capsys, monkeypatch):
    """Validator pass + reviewers goal_satisfied=true + zero blockers -> no fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump({
        "verification_checks": {
            "acceptance_gates_evaluated": {"all_passed": True, "status": "pass"},
        },
    }), encoding="utf-8")
    (iter_dir / "claude-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "validator_reviewer_disagreement" not in _fired_rules(parsed)


def test_validator_pass_reviewer_blocker_fires(tmp_path, capsys, monkeypatch):
    """Validator pass + a reviewer has blocking_objections -> FIRES."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump({
        "verification_checks": {
            "acceptance_gates_evaluated": {"all_passed": True, "status": "pass"},
        },
    }), encoding="utf-8")
    (iter_dir / "claude-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": False,
        "blocking_objections": [{"id": "claude-block-001"}],
    }), encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "validator_reviewer_disagreement" in fired
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "validator_reviewer_disagreement")
    assert "pass" in str(entry).lower() or "true" in str(entry).lower()


def test_validator_fail_both_reviewers_clean_fires(tmp_path, capsys, monkeypatch):
    """Validator fail + both reviewers say goal_satisfied=true & no blockers -> FIRES."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump({
        "verification_checks": {
            "acceptance_gates_evaluated": {"all_passed": False, "status": "fail"},
        },
    }), encoding="utf-8")
    (iter_dir / "claude-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "validator_reviewer_disagreement" in _fired_rules(parsed)


def test_validator_status_missing_does_not_fire(tmp_path, capsys, monkeypatch):
    """review-packet has no acceptance_gates_evaluated -> nothing to compare -> no fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump({
        "verification_checks": {},
    }), encoding="utf-8")
    (iter_dir / "claude-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": False, "blocking_objections": [{"id": "x"}],
    }), encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "validator_reviewer_disagreement" not in _fired_rules(parsed)


# ---- validator_reviewer_disagreement: real-world key-path aliases ----------
# Real iter-0009 review-packet uses verification_results.acceptance_gates.* and
# a separate iter_dir/verification.yaml file. The rule must read both.


def test_validator_alias_verification_results_pass_does_not_fire(tmp_path, capsys, monkeypatch):
    """review-packet uses verification_results.acceptance_gates.all_passed=true,
    both reviewers clean -> no fire (aliased PATH 2)."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump({
        "verification_results": {
            "acceptance_gates": {"all_passed": True},
        },
    }), encoding="utf-8")
    (iter_dir / "claude-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "validator_reviewer_disagreement" not in _fired_rules(parsed)


def test_validator_alias_verification_results_pass_with_blocker_fires(tmp_path, capsys, monkeypatch):
    """review-packet verification_results.acceptance_gates.all_passed=true but a
    reviewer has a blocking_objection -> rule FIRES via aliased PATH 2."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump({
        "verification_results": {
            "acceptance_gates": {"all_passed": True},
        },
    }), encoding="utf-8")
    (iter_dir / "claude-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": False,
        "blocking_objections": [{"id": "claude-block-001"}],
    }), encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "validator_reviewer_disagreement" in _fired_rules(parsed)


def test_validator_alias_verification_results_status_string_fail_fires(tmp_path, capsys, monkeypatch):
    """review-packet verification_results.acceptance_gates.status='fail' but both
    reviewers clean -> rule FIRES via PATH 2 with status-string coercion."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump({
        "verification_results": {
            "acceptance_gates": {"status": "fail"},
        },
    }), encoding="utf-8")
    (iter_dir / "claude-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "validator_reviewer_disagreement" in _fired_rules(parsed)


def test_validator_alias_verification_yaml_file_pass_does_not_fire(tmp_path, capsys, monkeypatch):
    """review-packet has no validator block; iter_dir/verification.yaml carries
    verification_checks.acceptance_gates_evaluated.all_passed=true; both
    reviewers clean -> no fire via tertiary PATH 3."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    # review-packet exists but with no validator paths
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump({
        "iteration_artifacts": [],
    }), encoding="utf-8")
    # Tertiary verification.yaml file
    (iter_dir / "verification.yaml").write_text(yaml.safe_dump({
        "verification_checks": {
            "acceptance_gates_evaluated": {"all_passed": True, "status": "pass"},
        },
    }), encoding="utf-8")
    (iter_dir / "claude-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "validator_reviewer_disagreement" not in _fired_rules(parsed)


def test_validator_alias_verification_yaml_file_fail_with_clean_reviewers_fires(tmp_path, capsys, monkeypatch):
    """review-packet has no validator block; iter_dir/verification.yaml says
    all_passed=false but both reviewers say clean -> rule FIRES via PATH 3."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump({
        "iteration_artifacts": [],
    }), encoding="utf-8")
    (iter_dir / "verification.yaml").write_text(yaml.safe_dump({
        "verification_checks": {
            "acceptance_gates_evaluated": {"all_passed": False, "status": "fail"},
        },
    }), encoding="utf-8")
    (iter_dir / "claude-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "validator_reviewer_disagreement" in _fired_rules(parsed)


def test_validator_alias_priority_path1_wins_over_path2(tmp_path, capsys, monkeypatch):
    """If review-packet has BOTH paths set, the original PATH 1 wins.
    Set PATH 1 = pass and PATH 2 = fail, with reviewers clean: PATH 1 wins
    (pass + clean reviewers -> no fire). If PATH 2 had won we'd see a fire."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    (iter_dir / "review-packet.yaml").write_text(yaml.safe_dump({
        "verification_checks": {
            "acceptance_gates_evaluated": {"all_passed": True},
        },
        "verification_results": {
            "acceptance_gates": {"all_passed": False},
        },
    }), encoding="utf-8")
    (iter_dir / "claude-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    (iter_dir / "codex-review.yaml").write_text(yaml.safe_dump({
        "goal_satisfied": True, "blocking_objections": [],
    }), encoding="utf-8")
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    assert "validator_reviewer_disagreement" not in _fired_rules(parsed)


# ---- coverage tuple update for task #12 -------------------------------------


def test_stop_rules_implemented_includes_task12_rules():
    """STOP_RULES_IMPLEMENTED should now list all 9 contract rules.

    Updated to 9/9 by Task #28 (added closure_cross_verification_failed).
    """
    for name in (
        "repeated_finding_class_unresolved",
        "cross_document_drift_detected",
        "validator_reviewer_disagreement",
    ):
        assert name in _self_drive.STOP_RULES_IMPLEMENTED, \
            f"{name} should be listed in STOP_RULES_IMPLEMENTED"
    # Full coverage now: 9/9
    assert set(_self_drive.STOP_RULES_IMPLEMENTED) == set(
        _self_drive.STOP_RULES_REQUIRED_BY_CONTRACT
    )


# ---- iter-0012 F2: _collect_changed_files unifies staged/unstaged/untracked --


def test_collect_changed_files_returns_union_of_three_git_views(tmp_path, monkeypatch):
    """_collect_changed_files must call git three times (staged, unstaged, untracked)
    and return the de-duplicated union, preserving every distinct path."""
    seen_cmds = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(list(cmd))
        result = _mock.MagicMock()
        result.returncode = 0
        result.stderr = ""
        # 1: staged
        if cmd[:3] == ["git", "diff", "--cached"]:
            result.stdout = "scripts/staged.py\nshared.py\n"
        # 2: unstaged tracked
        elif cmd[:3] == ["git", "diff", "--name-only"]:
            result.stdout = "scripts/unstaged.py\nshared.py\n"
        # 3: untracked
        elif cmd[:2] == ["git", "ls-files"]:
            result.stdout = "scripts/untracked.py\n"
        else:
            result.stdout = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    files = _self_drive._collect_changed_files(tmp_path)

    # Verify all three git views were invoked.
    cmd_strs = [" ".join(c) for c in seen_cmds]
    assert any("diff --cached --name-only" in s for s in cmd_strs), cmd_strs
    assert any(s.startswith("git diff --name-only") and "--cached" not in s
               for s in cmd_strs), cmd_strs
    assert any("ls-files --others --exclude-standard" in s for s in cmd_strs), cmd_strs

    # De-duplicated union: 4 distinct paths total.
    assert sorted(files) == [
        "scripts/staged.py",
        "scripts/unstaged.py",
        "scripts/untracked.py",
        "shared.py",
    ]


def test_collect_changed_files_handles_subprocess_error_gracefully(tmp_path, monkeypatch):
    """If a git invocation raises, _collect_changed_files should not raise; it
    returns whatever subset it could collect (empty if all three failed)."""
    def fake_run(cmd, **kwargs):
        raise OSError("git missing")

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    files = _self_drive._collect_changed_files(tmp_path)
    assert files == []


def test_collect_changed_files_empty_repo_no_crash(tmp_path, monkeypatch):
    """Empty repo / no commits / nothing changed -> [] without crash."""
    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    files = _self_drive._collect_changed_files(tmp_path)
    assert files == []


def test_forbidden_files_check_catches_unstaged_edit(tmp_path, capsys, monkeypatch):
    """Codex's repro scenario: STAGED clean file + UNSTAGED forbidden-file edit.
    Old behavior (cached-only) missed; new behavior fires the rule."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(
        tmp_path,
        forbidden_files=["consensus_mcp/_self_drive.py"],
    )

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        result.stderr = ""
        # Staged: an unrelated clean file.
        if cmd[:3] == ["git", "diff", "--cached"] and "--name-only" in cmd:
            result.stdout = "scripts/clean.py\n"
        # Unstaged tracked: the forbidden file is being modified.
        elif cmd[:3] == ["git", "diff", "--name-only"]:
            result.stdout = "consensus_mcp/_self_drive.py\n"
        # Untracked
        elif cmd[:2] == ["git", "ls-files"]:
            result.stdout = ""
        elif "--numstat" in cmd:
            result.stdout = ""
        else:
            result.stdout = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "patch_would_touch_forbidden_files" in fired, (
        f"unstaged forbidden-file edit must fire rule; fired={fired}"
    )
    entry = next(r for r in parsed["stop_rules_fired"]
                 if r["rule"] == "patch_would_touch_forbidden_files")
    assert entry["forbidden"] == "consensus_mcp/_self_drive.py"
    assert entry["changed"] == "consensus_mcp/_self_drive.py"


def test_forbidden_files_check_catches_untracked_file(tmp_path, capsys, monkeypatch):
    """Untracked file matching a forbidden pattern must fire the rule."""
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(
        tmp_path,
        forbidden_files=["run/"],
    )

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        result.stderr = ""
        if cmd[:3] == ["git", "diff", "--cached"] and "--name-only" in cmd:
            result.stdout = ""
        elif cmd[:3] == ["git", "diff", "--name-only"]:
            result.stdout = ""
        elif cmd[:2] == ["git", "ls-files"]:
            # Untracked file in forbidden-prefix dir.
            result.stdout = "run/some_new_script.py\n"
        else:
            result.stdout = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc, parsed = _run_check_stop_rules(packet, iter_dir, capsys)
    fired = _fired_rules(parsed)
    assert "patch_would_touch_forbidden_files" in fired


def test_verify_scope_catches_unstaged_out_of_scope_edit(tmp_path, monkeypatch, capsys):
    """cmd_verify_scope must see unstaged edits, not just `git diff --cached`."""
    import argparse
    packet = _write_goal_packet(
        tmp_path,
        allowed_files=["scripts/foo.py"],
    )

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        result.stderr = ""
        if cmd[:3] == ["git", "diff", "--cached"]:
            # Staged file IS in scope.
            result.stdout = "scripts/foo.py\n"
        elif cmd[:3] == ["git", "diff", "--name-only"]:
            # Unstaged tracked: out-of-scope file.
            result.stdout = "scripts/bar.py\n"
        elif cmd[:2] == ["git", "ls-files"]:
            result.stdout = ""
        else:
            result.stdout = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc = _self_drive.cmd_verify_scope(argparse.Namespace(goal_packet=str(packet)))
    out = _json.loads(capsys.readouterr().out)
    assert out["in_scope"] is False, "unstaged out-of-scope edit must fail scope check"
    assert "scripts/bar.py" in out["out_of_scope"]
    assert rc != 0


def test_verify_scope_catches_untracked_out_of_scope_file(tmp_path, monkeypatch, capsys):
    """Untracked files honoring .gitignore must be included in scope check."""
    import argparse
    packet = _write_goal_packet(
        tmp_path,
        allowed_files=["scripts/foo.py"],
    )

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        result.stderr = ""
        if cmd[:3] == ["git", "diff", "--cached"]:
            result.stdout = ""
        elif cmd[:3] == ["git", "diff", "--name-only"]:
            result.stdout = ""
        elif cmd[:2] == ["git", "ls-files"]:
            # Untracked out-of-scope file.
            result.stdout = "scripts/new_evil.py\n"
        else:
            result.stdout = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc = _self_drive.cmd_verify_scope(argparse.Namespace(goal_packet=str(packet)))
    out = _json.loads(capsys.readouterr().out)
    assert out["in_scope"] is False
    assert "scripts/new_evil.py" in out["out_of_scope"]


def test_verify_scope_clean_when_all_views_in_scope(tmp_path, monkeypatch, capsys):
    """All three views report only in-scope paths -> in_scope=True."""
    import argparse
    packet = _write_goal_packet(
        tmp_path,
        allowed_files=["scripts/foo.py", "scripts/bar.py"],
    )

    def fake_run(cmd, **kwargs):
        result = _mock.MagicMock()
        result.returncode = 0
        result.stderr = ""
        if cmd[:3] == ["git", "diff", "--cached"]:
            result.stdout = "scripts/foo.py\n"
        elif cmd[:3] == ["git", "diff", "--name-only"]:
            result.stdout = "scripts/bar.py\n"
        elif cmd[:2] == ["git", "ls-files"]:
            result.stdout = ""
        else:
            result.stdout = ""
        return result

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc = _self_drive.cmd_verify_scope(argparse.Namespace(goal_packet=str(packet)))
    out = _json.loads(capsys.readouterr().out)
    assert out["in_scope"] is True
    assert out["out_of_scope"] == []
    assert rc == 0


# ---- H-3: cmd_close emits exactly ONE JSON blob ----------------------------


def test_close_emits_single_json_blob(tmp_path, capsys, monkeypatch):
    """cmd_close must print exactly ONE JSON object on stdout.

    Pre-fix it called four sub-commands (validate, check_stop_rules,
    evaluate_gates, verify_scope) that each `print(json.dumps(...))`, then
    printed its own blob -> 5 JSON objects -> ``json.loads(stdout)`` raised
    "Extra data". The sub-command prints must be suppressed so the parsed
    output is a single object carrying can_close + a components dict.
    """
    import argparse
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    packet = _write_goal_packet(tmp_path)

    def fake_run(cmd, **kwargs):
        r = _mock.MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    monkeypatch.setattr(_self_drive.subprocess, "run", fake_run)
    rc = _self_drive.cmd_close(
        argparse.Namespace(goal_packet=str(packet), iteration_dir=str(iter_dir))
    )
    out = capsys.readouterr().out
    # A single JSON object -> json.loads must succeed (pre-fix: "Extra data").
    parsed = _json.loads(out)
    assert "can_close" in parsed
    assert isinstance(parsed["can_close"], bool)
    assert "components" in parsed
    for key in ("validate", "stop_rules", "gates", "scope"):
        assert key in parsed["components"], f"components missing {key}: {parsed}"
        assert isinstance(parsed["components"][key], bool)
    # rc mirrors can_close (0 iff can_close).
    assert (rc == 0) == parsed["can_close"]


# ---- M-11: cmd_transition is stateless (validates + reports only) -----------


def test_transition_is_stateless(tmp_path, capsys):
    """cmd_transition validates new_state and reports terminality but persists
    NOTHING. The real behavioral contract: invoking it writes no files.

    M-11 was a contract-honesty fix - the docstring claimed transitions are
    "recorded" while the function only printed. This asserts the true (no
    side-effect) behavior so the corrected contract cannot silently regress
    into an actual writer without failing this test.
    """
    import argparse
    work = tmp_path / "work"
    work.mkdir()
    packet = _write_goal_packet(work)
    before = sorted(p.name for p in work.iterdir())

    rc = _self_drive.cmd_transition(
        argparse.Namespace(
            goal_packet=str(packet),
            new_state="patch_planned",
            note="",
        )
    )
    out = _json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["state"] == "patch_planned"
    # Behavioral contract: NO new files written as a side effect.
    after = sorted(p.name for p in work.iterdir())
    assert after == before, f"cmd_transition must persist nothing; new files: {set(after) - set(before)}"
