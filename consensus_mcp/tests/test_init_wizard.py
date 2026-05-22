"""Unit tests for consensus_mcp._init_wizard (`consensus init` CLI)."""
from __future__ import annotations

import builtins
from pathlib import Path

import pytest
import yaml

from consensus_mcp import _init_wizard as wiz
from consensus_mcp import config as cfg


# ---------- print-defaults ----------

def test_print_defaults_outputs_yaml(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main(["--print-defaults"])
    out = capsys.readouterr().out
    assert rc == 0
    parsed = yaml.safe_load(out)
    assert parsed["schema_version"] == 1
    assert parsed["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE


# ---------- check ----------

def test_check_missing_config_returns_2(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main(["--check"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_check_valid_config_returns_0(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".consensus"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(cfg.default_config()), encoding="utf-8"
    )
    rc = wiz.main(["--check"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = yaml.safe_load(out)
    assert parsed["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE


def test_check_invalid_config_returns_3(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".consensus"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "schema_version: 1\nworkflow:\n  mode: wrong\n", encoding="utf-8"
    )
    rc = wiz.main(["--check"])
    assert rc == 3


def test_check_honors_config_path_override(tmp_path, capsys, monkeypatch):
    """--config flag must redirect --check to the override path (codex-rev-002)."""
    monkeypatch.chdir(tmp_path)
    alt = tmp_path / "elsewhere" / "myconfig.yaml"
    alt.parent.mkdir()
    alt.write_text(yaml.safe_dump(cfg.default_config()), encoding="utf-8")
    # Confirm default path is empty.
    assert not (tmp_path / ".consensus" / "config.yaml").exists()
    rc = wiz.main(["--check", "--config", str(alt)])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = yaml.safe_load(out)
    assert parsed["schema_version"] == 1


# ---------- dry-run ----------

def test_dry_run_interactive_previews_without_writing(tmp_path, capsys, monkeypatch):
    """gemini pass-3 rev-001: --dry-run with no --non-interactive must enter
    the interactive prompt path and preview the result without writing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(builtins, "input", _stub_input([""] * 8))
    rc = wiz.main(["--dry-run", "--contributors", "claude,codex,gemini"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert not (tmp_path / ".consensus" / "config.yaml").exists()


def test_dry_run_does_not_write(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--dry-run", "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert not (tmp_path / ".consensus" / "config.yaml").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_dry_run_with_no_update_gitignore_reports_skip(tmp_path, capsys, monkeypatch):
    """codex-rev-004: dry-run must honor --no-update-gitignore in its preview."""
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--dry-run", "--non-interactive", "--accept-defaults",
        "--no-update-gitignore",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "skipped (--no-update-gitignore)" in out
    # Must not claim ".gitignore at ..." would be updated.
    assert "would update .gitignore" not in out


# ---------- non-interactive write ----------

def test_non_interactive_writes_config(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    config_path = tmp_path / ".consensus" / "config.yaml"
    assert config_path.exists()
    loaded = cfg.load(config_path)
    assert loaded["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE


def test_non_interactive_refuses_existing_without_reconfigure(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".consensus"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 4
    err = capsys.readouterr().err
    assert "already exists" in err


def test_non_interactive_writes_to_config_override(tmp_path, monkeypatch):
    """codex-rev-002: --config must redirect the write target."""
    monkeypatch.chdir(tmp_path)
    alt = tmp_path / "custom" / "cfg.yaml"
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--config", str(alt),
    ])
    assert rc == 0
    assert alt.exists()
    assert not (tmp_path / ".consensus" / "config.yaml").exists()


def test_existing_config_guard_uses_config_override(tmp_path, capsys, monkeypatch):
    """codex-rev-002: existing-config refusal must check the --config path."""
    monkeypatch.chdir(tmp_path)
    alt = tmp_path / "elsewhere.yaml"
    alt.write_text("schema_version: 1\n", encoding="utf-8")
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--config", str(alt),
    ])
    assert rc == 4
    err = capsys.readouterr().err
    assert "already exists" in err


# ---------- reconfigure ----------

def test_reconfigure_overwrites(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".consensus"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(cfg.default_config()), encoding="utf-8"
    )
    rc = wiz.main([
        "--non-interactive", "--reconfigure", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--workflow", "3",
        "--independence", cfg.INDEPENDENCE_VISIBLE,
    ])
    assert rc == 0
    loaded = cfg.load(config_dir / "config.yaml")
    assert loaded["workflow"]["mode"] == cfg.WORKFLOW_POST_REVIEW


def test_reconfigure_preserves_existing_non_default(tmp_path, monkeypatch):
    """codex-rev-003 (pass 1) / codex-rev-001 (pass 2): --reconfigure must
    preserve existing config values for any dimension not explicitly overridden
    via CLI flag — including snapshot trigger and contributors.
    """
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".consensus"
    config_dir.mkdir()
    existing = cfg.default_config()
    existing["snapshots"]["trigger"] = cfg.SNAPSHOT_MANUAL
    existing["contributors"]["enabled"] = ["claude", "codex"]
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(existing), encoding="utf-8"
    )
    rc = wiz.main([
        "--non-interactive", "--reconfigure", "--accept-defaults",
    ])
    assert rc == 0
    loaded = cfg.load(config_dir / "config.yaml")
    # Reconfigure preserved both the non-default snapshot trigger AND the
    # existing contributor list (NOT re-detected from PATH).
    assert loaded["snapshots"]["trigger"] == cfg.SNAPSHOT_MANUAL
    assert loaded["contributors"]["enabled"] == ["claude", "codex"]


def test_reconfigure_emits_diff(tmp_path, capsys, monkeypatch):
    """codex-rev-003: --reconfigure must print a before/after diff before write."""
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".consensus"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(cfg.default_config()), encoding="utf-8"
    )
    rc = wiz.main([
        "--non-interactive", "--reconfigure", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--workflow", "3",
        "--independence", cfg.INDEPENDENCE_VISIBLE,
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "reconfigure diff" in out


# ---------- default-from-contributor-count ----------

def test_solo_claude_defaults_to_workflow_3(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude",
    ])
    assert rc == 0
    loaded = cfg.load(tmp_path / ".consensus" / "config.yaml")
    assert loaded["workflow"]["mode"] == cfg.WORKFLOW_POST_REVIEW


def test_two_plus_contributors_defaults_to_workflow_4(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex",
    ])
    assert rc == 0
    loaded = cfg.load(tmp_path / ".consensus" / "config.yaml")
    assert loaded["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE


# ---------- alias resolution ----------

def test_numeric_workflow_alias_resolves(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex",
        "--workflow", "4",
    ])
    assert rc == 0
    loaded = cfg.load(tmp_path / ".consensus" / "config.yaml")
    assert loaded["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE


# ---------- .gitignore management ----------

def test_gitignore_added_when_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert wiz.GITIGNORE_OPEN_MARKER in gi
    assert wiz.GITIGNORE_CLOSE_MARKER in gi
    assert ".consensus/tmp/" in gi


def test_gitignore_skipped_when_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-update-gitignore",
    ])
    assert rc == 0
    assert not (tmp_path / ".gitignore").exists()


def test_gitignore_deduplicated_on_rerun(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    wiz.main([
        "--non-interactive", "--reconfigure", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    gi2 = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert gi2.count(wiz.GITIGNORE_OPEN_MARKER) == 1
    assert gi2.count(wiz.GITIGNORE_CLOSE_MARKER) == 1


def test_gitignore_preserves_existing_unrelated_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gitignore").write_text("# user content\n*.pyc\nbuild/\n", encoding="utf-8")
    wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "# user content" in gi
    assert "*.pyc" in gi
    assert "build/" in gi
    assert wiz.GITIGNORE_OPEN_MARKER in gi


def test_gitignore_reversed_markers_preserve_user_lines(tmp_path, monkeypatch):
    """codex pass-3 rev-001: balanced-but-reversed markers (close-before-open)
    must NOT cause the rewriter to treat downstream lines as managed content."""
    monkeypatch.chdir(tmp_path)
    reversed_content = (
        "# user header\n"
        "*.pyc\n"
        f"{wiz.GITIGNORE_CLOSE_MARKER}\n"
        "build/\n"
        f"{wiz.GITIGNORE_OPEN_MARKER}\n"
        "secrets.env\n"
    )
    (tmp_path / ".gitignore").write_text(reversed_content, encoding="utf-8")
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    for kept in ("# user header", "*.pyc", "build/", "secrets.env"):
        assert kept in gi, f"missing preserved line {kept!r}"


def test_gitignore_malformed_open_marker_preserves_user_lines(tmp_path, monkeypatch):
    """codex-rev-005: an unmatched open marker must NOT cause downstream user
    content to be silently deleted. Wizard preserves all original lines and
    appends a clean block."""
    monkeypatch.chdir(tmp_path)
    malformed = (
        f"# user header\n*.pyc\n"
        f"{wiz.GITIGNORE_OPEN_MARKER}\n"
        f".consensus/tmp/\n"
        # ← intentionally NO close marker
        f"build/\n"
        f"secrets.env\n"
    )
    (tmp_path / ".gitignore").write_text(malformed, encoding="utf-8")
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # All original lines preserved despite malformed marker pair.
    for kept in ("# user header", "*.pyc", "build/", "secrets.env"):
        assert kept in gi, f"missing preserved line {kept!r}"


def test_gitignore_malformed_marker_idempotent_on_rerun(tmp_path, monkeypatch):
    """codex pass-4 rev-001: with malformed markers in the user's file, repeated
    `consensus init` runs must not accumulate duplicate managed blocks."""
    monkeypatch.chdir(tmp_path)
    malformed = (
        f"# user content\n*.pyc\n"
        f"{wiz.GITIGNORE_OPEN_MARKER}\n"
        f"build/\n"  # orphan open marker, no close
    )
    (tmp_path / ".gitignore").write_text(malformed, encoding="utf-8")
    # First run: appends managed block (and preserves all user content).
    wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    after_first = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # Second run: must not duplicate the managed block.
    wiz.main([
        "--non-interactive", "--reconfigure", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    after_second = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # Idempotency: rerun must not change the file (no duplicate managed block).
    assert after_first == after_second
    # The user's orphan open marker is preserved (we never silently delete it),
    # so the open-marker count is 2 (orphan + appended block), but the managed
    # PATHS appear exactly once.
    for path in wiz.GITIGNORE_MANAGED_PATHS:
        assert after_second.count(path) == 1, f"{path!r} duplicated on rerun"
    # User content preserved across both runs.
    for kept in ("# user content", "*.pyc", "build/"):
        assert kept in after_first
        assert kept in after_second


def test_existing_config_guard_precedes_interactive_prompt(tmp_path, capsys, monkeypatch):
    """codex pass-4 rev-002: exit 4 (config exists) must win over exit 1
    (interactive abort) and exit 3 (invalid args). Even with no --non-interactive,
    an existing config without --reconfigure/--force returns 4 without prompting."""
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".consensus"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(yaml.safe_dump(cfg.default_config()), encoding="utf-8")
    # If the guard were not first, input() would be called and we'd get
    # KeyboardInterrupt (exit 1). Stub input to raise so the test would fail
    # loudly if the guard ran late.
    def _explode(prompt=""):
        raise AssertionError(f"prompt invoked but guard should have returned first: {prompt!r}")
    monkeypatch.setattr(builtins, "input", _explode)
    rc = wiz.main([])
    assert rc == 4


def test_reconfigure_diff_is_well_formed(tmp_path, capsys, monkeypatch):
    """codex-rev-004 (pass 2): unified diff control records and body lines
    must each be on their own line; no run-together output."""
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".consensus"
    config_dir.mkdir()
    existing = cfg.default_config()
    existing["snapshots"]["trigger"] = cfg.SNAPSHOT_MANUAL
    (config_dir / "config.yaml").write_text(yaml.safe_dump(existing), encoding="utf-8")
    rc = wiz.main([
        "--non-interactive", "--reconfigure", "--accept-defaults",
        "--snapshot-trigger", cfg.SNAPSHOT_ON_CLOSE,
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "reconfigure diff" in out
    # The diff prelude (---) and the proposed prelude (+++) and the @@ hunk
    # marker must each appear on their own line.
    for needle in ("--- existing", "+++ proposed", "@@"):
        assert needle in out, f"missing diff control line {needle!r}"
    # No two control records glued together.
    assert "--- existing+++ proposed" not in out
    assert "+++ proposed@@" not in out


# ---------- validation ----------

def test_invalid_combo_returns_3(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude",
        "--workflow", "4",
    ])
    assert rc == 3
    err = capsys.readouterr().err
    assert "invalid config" in err.lower()


# ---------- interactive mode (gemini-rev-001 / codex-rev-001) ----------

def _stub_input(responses):
    """Build an input() stub that pops responses left-to-right."""
    queue = list(responses)
    def _fake(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)
    return _fake


def test_interactive_default_path_prompts(tmp_path, monkeypatch, capsys):
    """No mode flags → must enter interactive path; user can accept all defaults
    by pressing enter (empty input). With --contributors supplied, 8 prompts remain."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(builtins, "input", _stub_input([""] * 8))
    rc = wiz.main(["--contributors", "claude,codex,gemini"])
    assert rc == 0
    loaded = cfg.load(tmp_path / ".consensus" / "config.yaml")
    assert loaded["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE


def test_interactive_eof_returns_user_abort(tmp_path, monkeypatch, capsys):
    """codex-rev-001 / gemini-rev-001: Ctrl+D / EOF must yield exit code 1."""
    monkeypatch.chdir(tmp_path)
    def _eof(prompt=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", _eof)
    rc = wiz.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "aborted by user" in err


def test_interactive_keyboard_interrupt_returns_user_abort(tmp_path, monkeypatch, capsys):
    """Ctrl+C during prompt must yield exit code 1."""
    monkeypatch.chdir(tmp_path)
    def _kbi(prompt=""):
        raise KeyboardInterrupt
    monkeypatch.setattr(builtins, "input", _kbi)
    rc = wiz.main([])
    assert rc == 1


def test_interactive_user_overrides_workflow(tmp_path, monkeypatch):
    """User can pick non-default workflow at the prompt; remaining 6 dimensions
    accept defaults via empty input.

    The fresh contributor dimension is now a numbered multi-select (codex-rev-001
    wiring); install detection is forced deterministic so this is hermetic across
    machines/CI, and the first answer is a numeric selection (display order:
    1=claude,2=codex,3=gemini,4=kimi → "1,2,3" picks claude,codex,gemini)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_profile_installed", lambda profile: True)
    monkeypatch.setattr(builtins, "input", _stub_input([
        "1,2,3",                         # contributors (claude,codex,gemini)
        cfg.WORKFLOW_POST_REVIEW,       # workflow
        cfg.CONVERGE_STRICT_MAJ,        # convergence
        cfg.INDEPENDENCE_VISIBLE,       # independence (workflow #3 needs visible)
        "",                              # finding_disposition (default)
        "",                              # snapshot_trigger (default)
        "",                              # snapshot_every_iterations (default)
        "",                              # patch_authoring (default)
        "",                              # timeout_policy (default)
    ]))
    rc = wiz.main([])
    assert rc == 0
    loaded = cfg.load(tmp_path / ".consensus" / "config.yaml")
    assert loaded["workflow"]["mode"] == cfg.WORKFLOW_POST_REVIEW


def test_interactive_prompts_all_nine_dimensions(tmp_path, monkeypatch, capsys):
    """codex-rev-002: every configurability dimension must be prompted in
    interactive mode (not just contributors/workflow/convergence)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_profile_installed", lambda profile: True)
    monkeypatch.setattr(builtins, "input", _stub_input([
        "1,2,3",                         # contributors (claude,codex,gemini)
        cfg.WORKFLOW_PROPOSE_CONVERGE,  # workflow
        cfg.CONVERGE_STRICT_MAJ,        # convergence
        cfg.INDEPENDENCE_BLIND,         # independence
        cfg.DISPOSITION_ALL_OR_NOTHING, # finding_disposition
        cfg.SNAPSHOT_PERIODIC,          # snapshot_trigger (periodic to keep cadence valid)
        "7",                             # snapshot_every_iterations
        cfg.PATCH_CLAUDE_ONLY,          # patch_authoring
        cfg.TIMEOUT_BLOCKING,           # timeout_policy
    ]))
    rc = wiz.main([])
    assert rc == 0
    loaded = cfg.load(tmp_path / ".consensus" / "config.yaml")
    assert loaded["snapshots"]["trigger"] == cfg.SNAPSHOT_PERIODIC
    assert loaded["snapshots"]["periodic"]["every_iterations"] == 7
    assert loaded["patches"]["authoring"] == cfg.PATCH_CLAUDE_ONLY
    assert loaded["workflow"]["timeout_policy"] == cfg.TIMEOUT_BLOCKING


def test_interactive_defaults_reflect_cli_overrides(tmp_path, monkeypatch, capsys):
    """codex-rev-003: when --workflow advisory is passed, convergence prompt
    default must be 'advisory', not the count-derived default."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_profile_installed", lambda profile: True)
    captured_prompts: list[str] = []
    # First answer is a numeric multi-select (1,2,3 → claude,codex,gemini) now
    # that the fresh contributor dimension is the wired numbered multi-select.
    queue = ["1,2,3", "", "", "", "", "", "", ""]
    def _capturing_input(prompt=""):
        captured_prompts.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)
    monkeypatch.setattr(builtins, "input", _capturing_input)
    rc = wiz.main(["--workflow", "advisory"])
    assert rc == 0
    # The convergence prompt should suggest 'advisory' as default since
    # workflow is advisory; not 'strict-majority' (3-contributor default).
    convergence_prompts = [p for p in captured_prompts if p.startswith("Convergence rule")]
    assert any("advisory" in p for p in convergence_prompts), convergence_prompts


# ---------- repo root detection ----------

def test_repo_root_walks_up_to_git(tmp_path, monkeypatch):
    """gemini-rev-002: _detect_repo_root finds .git in an ancestor directory."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    # Config landed at repo root, not at sub.
    assert (repo / ".consensus" / "config.yaml").exists()
    assert not (sub / ".consensus" / "config.yaml").exists()


# ---------- iter-0031: .mcp.json bootstrap ----------


def _read_mcp_json(path: Path) -> dict:
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def test_mcp_command_resolution_no_override(monkeypatch):
    """Default resolution: shutil.which("consensus-mcp") path returns bare name."""
    monkeypatch.setattr(wiz.shutil, "which", lambda name: "/some/path/consensus-mcp")
    cmd, args, portable = wiz._resolve_mcp_command()
    assert cmd == "consensus-mcp"
    assert args == []
    assert portable is True


def test_mcp_command_resolution_fallback_when_no_path(monkeypatch):
    """When consensus-mcp not on PATH, fall back to sys.executable -m form."""
    monkeypatch.setattr(wiz.shutil, "which", lambda name: None)
    cmd, args, portable = wiz._resolve_mcp_command()
    assert cmd == wiz.sys.executable
    assert args == ["-m", "consensus_mcp.server"]
    assert portable is False


def test_mcp_command_resolution_explicit_override(monkeypatch):
    """--mcp-command override is split on whitespace; first token is command."""
    cmd, args, portable = wiz._resolve_mcp_command("py -3.11 -m consensus_mcp.server")
    assert cmd == "py"
    assert args == ["-3.11", "-m", "consensus_mcp.server"]
    assert portable is False


def test_mcp_command_empty_override_raises():
    with pytest.raises(ValueError):
        wiz._resolve_mcp_command("   ")


def test_init_writes_mcp_json_in_fresh_project(tmp_path, monkeypatch):
    """A2: fresh project, no existing .mcp.json → consensus-init writes one."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    mcp_path = tmp_path / ".mcp.json"
    assert mcp_path.exists()
    data = _read_mcp_json(mcp_path)
    assert "mcpServers" in data
    assert "consensus-mcp" in data["mcpServers"]
    entry = data["mcpServers"]["consensus-mcp"]
    assert entry["command"] == "consensus-mcp"
    assert entry["env"]["CONSENSUS_MCP_STATE_ROOT"] == str(tmp_path / "consensus-state")
    assert entry["env"]["CONSENSUS_MCP_PROJECT_ROOT"] == str(tmp_path)


def test_init_merges_existing_mcp_json_with_other_servers(tmp_path, monkeypatch):
    """A3: existing .mcp.json with another server → merge, both servers present."""
    import json
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None)
    # Pre-existing .mcp.json with a different MCP server.
    existing = {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": ["@playwright/mcp@latest"],
            }
        }
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(existing, indent=2), encoding="utf-8")

    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    data = _read_mcp_json(tmp_path / ".mcp.json")
    # Both servers must be present.
    assert "playwright" in data["mcpServers"]
    assert "consensus-mcp" in data["mcpServers"]
    assert data["mcpServers"]["playwright"]["command"] == "npx"
    assert data["mcpServers"]["consensus-mcp"]["command"] == "consensus-mcp"


def test_init_no_mcp_json_flag_skips(tmp_path, monkeypatch, capsys):
    """A4: --no-mcp-json skips .mcp.json entirely; config.yaml still written."""
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-mcp-json",
    ])
    assert rc == 0
    assert (tmp_path / ".consensus" / "config.yaml").exists()
    assert not (tmp_path / ".mcp.json").exists()


def test_init_mcp_json_is_idempotent_on_rerun(tmp_path, monkeypatch):
    """A5: re-running consensus-init produces identical .mcp.json content."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    first = (tmp_path / ".mcp.json").read_text(encoding="utf-8")
    # Reconfigure should re-write but produce identical .mcp.json since
    # nothing changed in the discoverable inputs.
    rc = wiz.main([
        "--non-interactive", "--reconfigure", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    second = (tmp_path / ".mcp.json").read_text(encoding="utf-8")
    assert first == second


def test_init_blocks_conflict_without_force(tmp_path, monkeypatch, capsys):
    """A6: existing consensus-mcp entry with different command → skip+warn,
    file unchanged, no force flag → blocked."""
    import json
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None)
    # Pre-existing consensus-mcp entry with a CUSTOM command.
    existing = {
        "mcpServers": {
            "consensus-mcp": {
                "command": "/custom/path/consensus-mcp",
                "env": {
                    "CONSENSUS_MCP_STATE_ROOT": "/custom/state",
                    "CONSENSUS_MCP_PROJECT_ROOT": "/custom/proj",
                },
            }
        }
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(existing, indent=2), encoding="utf-8")

    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0  # Init still succeeds; .mcp.json portion is just skipped.
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    # File contents preserved.
    data = _read_mcp_json(tmp_path / ".mcp.json")
    assert data["mcpServers"]["consensus-mcp"]["command"] == "/custom/path/consensus-mcp"


def test_init_force_replaces_conflict_preserving_other_servers(tmp_path, monkeypatch):
    """A7: --mcp-force replaces ONLY the consensus-mcp entry; other servers preserved."""
    import json
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None)
    existing = {
        "mcpServers": {
            "playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]},
            "consensus-mcp": {"command": "/custom/path/consensus-mcp", "env": {}},
        }
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(existing, indent=2), encoding="utf-8")

    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--mcp-force",
    ])
    assert rc == 0
    data = _read_mcp_json(tmp_path / ".mcp.json")
    # Playwright entry preserved.
    assert data["mcpServers"]["playwright"]["command"] == "npx"
    # consensus-mcp entry replaced.
    assert data["mcpServers"]["consensus-mcp"]["command"] == "consensus-mcp"


def test_init_malformed_mcp_json_skip_warn(tmp_path, monkeypatch, capsys):
    """A8: malformed existing .mcp.json → skip+warn, file unchanged."""
    monkeypatch.chdir(tmp_path)
    malformed = "{ this is not valid json, trailing comma, }"
    (tmp_path / ".mcp.json").write_text(malformed, encoding="utf-8")

    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    err = capsys.readouterr().err
    assert "failed to parse as JSON" in err
    # File untouched.
    assert (tmp_path / ".mcp.json").read_text(encoding="utf-8") == malformed


def test_init_mcp_command_override_threads_through(tmp_path, monkeypatch):
    """A10: --mcp-command override is written into .mcp.json."""
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--mcp-command", "py -3.11 -m consensus_mcp.server",
    ])
    assert rc == 0
    data = _read_mcp_json(tmp_path / ".mcp.json")
    entry = data["mcpServers"]["consensus-mcp"]
    assert entry["command"] == "py"
    assert entry["args"] == ["-3.11", "-m", "consensus_mcp.server"]


def test_init_dry_run_does_not_write_mcp_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = wiz.main([
        "--dry-run", "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would write .mcp.json" in out
    assert not (tmp_path / ".mcp.json").exists()


def test_init_nested_cwd_resolves_to_root_via_markers(tmp_path, monkeypatch):
    """A9: from nested cwd, project-root walks up via strong markers."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None)
    # Force git rev-parse failure so we exercise the strong-marker walk
    # (otherwise git might find the consensus-mcp repo above tmp_path).
    monkeypatch.setattr(wiz.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
    ])
    assert rc == 0
    # .mcp.json lands at the repo root (where pyproject.toml is), not the sub.
    assert (repo / ".mcp.json").exists()
    assert not (sub / ".mcp.json").exists()
