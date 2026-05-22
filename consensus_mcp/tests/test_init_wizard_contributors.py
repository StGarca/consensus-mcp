"""Tests for v1.18.0 contributor-selection + detect-guide + instruction
provisioning in consensus_mcp._init_wizard.

Covers the four converged-plan features (decision.wizard_ux, decision.detect_guide,
the instruction_files block):

  1. interactive multi-select over merged profiles (min-2 re-prompt, claude
     optional, installed-status, pre-checked defaults)
  2. non-interactive --contributors precedence (unknown-name + <2 errors)
  3. detect+guide (per-OS install/auth lines for missing CLIs; silence for
     present ones; nothing executed)
  4. non-destructive idempotent managed-block instruction-file provisioning
     (filename-map dedupe; --no-instructions opt-out)

Helpers are exercised directly (mirroring how test_init_wizard.py drives
_resolve_mcp_command et al.) so config-level validation does not couple these
unit tests to other agents' modules.
"""
from __future__ import annotations

import argparse
import builtins

import pytest

from consensus_mcp import _init_wizard as wiz
from consensus_mcp import config as cfg


# ---------- input stub (mirrors test_init_wizard._stub_input) ----------

def _stub_input(responses):
    queue = list(responses)
    def _fake(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)
    return _fake


def _all_none_args(**overrides):
    """argparse.Namespace with every dimension flag = None (interactive path).

    Mirrors the flags inspected by wiz._which_flags_set so the contributor
    dimension takes the interactive multi-select branch.
    """
    base = {
        "contributors": None,
        "workflow": None,
        "convergence": None,
        "independence": None,
        "finding_disposition": None,
        "snapshot_trigger": None,
        "snapshot_every_iterations": None,
        "patch_authoring": None,
        "timeout_policy": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ============================================================
# 1. interactive multi-select
# ============================================================

def test_select_contributors_interactive_min2_reprompt(monkeypatch, capsys):
    """A selection of <2 must re-prompt until >=2 are chosen."""
    profiles = wiz._load_merged_profiles(None)
    # which → nothing installed, so nothing pre-checked (claude host always avail).
    monkeypatch.setattr(wiz.shutil, "which", lambda name: None)
    # First answer picks a single number (claude only via its index) → re-prompt;
    # second answer picks two indices → accepted.
    # Use _independent_ordered_names since that's what the multiselect displays.
    names = wiz._independent_ordered_names(profiles)
    one = str(names.index("claude") + 1)
    two = f"{names.index('claude') + 1},{names.index('codex') + 1}"
    monkeypatch.setattr(builtins, "input", _stub_input([one, two]))
    chosen = wiz._select_contributors_interactive(profiles)
    assert set(chosen) == {"claude", "codex"}
    err = capsys.readouterr().err
    assert "at least 2" in err.lower()


def test_select_contributors_interactive_min2_ok_first_try(monkeypatch):
    profiles = wiz._load_merged_profiles(None)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: None)
    # Use _independent_ordered_names since that's what the multiselect displays.
    names = wiz._independent_ordered_names(profiles)
    pick = f"{names.index('codex') + 1},{names.index('gemini') + 1}"
    monkeypatch.setattr(builtins, "input", _stub_input([pick]))
    chosen = wiz._select_contributors_interactive(profiles)
    assert set(chosen) == {"codex", "gemini"}


def test_select_contributors_interactive_empty_accepts_prechecked(monkeypatch):
    """Empty input accepts the pre-checked (installed) default set when >=2."""
    profiles = wiz._load_merged_profiles(None)
    # codex + gemini installed → pre-checked; claude host always available too.
    installed = {"codex", "gemini"}
    monkeypatch.setattr(
        wiz.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in installed else None,
    )
    monkeypatch.setattr(builtins, "input", _stub_input([""]))
    chosen = wiz._select_contributors_interactive(profiles)
    # claude (host) + codex + gemini are the pre-checked defaults.
    assert "codex" in chosen and "gemini" in chosen
    assert len(chosen) >= 2


def test_select_contributors_status_line_shows_installed_and_missing(monkeypatch, capsys):
    profiles = wiz._load_merged_profiles(None)
    installed = {"codex"}
    monkeypatch.setattr(
        wiz.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in installed else None,
    )
    monkeypatch.setattr(builtins, "input", _stub_input(["1,2,3"]))
    wiz._select_contributors_interactive(profiles)
    out = capsys.readouterr().out
    assert "installed" in out
    assert "missing" in out


# ------------------------------------------------------------
# regression lock: codex-rev-001 (v1.18.0 Workflow B)
# The numbered multi-select MUST be wired into interactive_overrides'
# contributor dimension for the FRESH path — not just unit-tested in
# isolation. Drives interactive_overrides directly with deterministic
# install detection + a numeric multi-select answer.
# ------------------------------------------------------------

def test_interactive_overrides_fresh_wires_numbered_multiselect(tmp_path, monkeypatch):
    """FRESH interactive_overrides must use _select_contributors_interactive
    (numbered multi-select), NOT the old comma-separated free-text prompt.

    Selecting indices that EXCLUDE claude proves claude-optional, and proves
    the numeric selection (not raw text) drives the enabled list.
    """
    base = cfg.default_config()
    args = _all_none_args()  # contributors flag is None → interactive branch

    # Deterministic install detection regardless of host PATH.
    monkeypatch.setattr(wiz, "_profile_installed", lambda profile: True)

    # Display order is host-first then alphabetical over INDEPENDENT profiles only
    # (host_peer excluded): [claude, codex, gemini, kimi]. "2,3" picks
    # codex+gemini (claude-optional). Remaining answers ("") accept each later
    # dimension's default. interactive_overrides prompts 8 more dimensions after
    # contributors.
    monkeypatch.setattr(
        builtins, "input",
        _stub_input(["2,3", "", "", "", "", "", "", "", ""]),
    )

    wiz.interactive_overrides(args, tmp_path, base, fresh=True)

    enabled = base["contributors"]["enabled"]
    assert enabled == ["codex", "gemini"], enabled
    assert "claude" not in enabled  # claude-optional, selected out
    assert len(enabled) >= 2


# ============================================================
# 3. detect + guide
# ============================================================

def test_detect_guide_missing_windows_prints_install_and_auth(monkeypatch, capsys):
    profiles = wiz._load_merged_profiles(None)
    monkeypatch.setattr(wiz.sys, "platform", "win32")
    monkeypatch.setattr(wiz.shutil, "which", lambda name: None)  # nothing installed
    # Guard: nothing must be shelled out.
    monkeypatch.setattr(
        wiz.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess.run called")),
    )
    wiz._detect_and_guide(["codex"], profiles)
    out = capsys.readouterr().out
    assert "npm install -g @openai/codex" in out
    assert "codex login" in out
    assert "OPENAI_API_KEY" in out
    assert "NOT run for you" in out


def test_detect_guide_uses_linux_install_on_linux(monkeypatch, capsys):
    profiles = wiz._load_merged_profiles(None)
    monkeypatch.setattr(wiz.sys, "platform", "linux")
    monkeypatch.setattr(wiz.shutil, "which", lambda name: None)
    wiz._detect_and_guide(["gemini"], profiles)
    out = capsys.readouterr().out
    assert "npm install -g @google/gemini-cli" in out


def test_detect_guide_darwin_falls_back_to_linux_when_absent(monkeypatch, capsys):
    """darwin uses install.darwin if present, else install.linux."""
    profiles = wiz._load_merged_profiles(None)
    monkeypatch.setattr(wiz.sys, "platform", "darwin")
    monkeypatch.setattr(wiz.shutil, "which", lambda name: None)
    wiz._detect_and_guide(["kimi"], profiles)  # kimi has no darwin install key
    out = capsys.readouterr().out
    assert "pipx install kimi-cli" in out  # linux fallback


def test_detect_guide_present_cli_prints_nothing(monkeypatch, capsys):
    profiles = wiz._load_merged_profiles(None)
    monkeypatch.setattr(wiz.sys, "platform", "linux")
    monkeypatch.setattr(wiz.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        wiz.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess.run called")),
    )
    wiz._detect_and_guide(["codex", "gemini"], profiles)
    out = capsys.readouterr().out
    assert out.strip() == ""


def test_detect_guide_host_claude_prints_nothing(monkeypatch, capsys):
    """claude is kind=host — no CLI, never guided, never executed."""
    profiles = wiz._load_merged_profiles(None)
    monkeypatch.setattr(wiz.sys, "platform", "win32")
    monkeypatch.setattr(wiz.shutil, "which", lambda name: None)
    wiz._detect_and_guide(["claude"], profiles)
    out = capsys.readouterr().out
    assert out.strip() == ""


# ============================================================
# 4. instruction-file provisioning
# ============================================================

_SENTINEL_BEGIN = "<!-- consensus-mcp:begin (managed — do not edit inside) -->"
_SENTINEL_END = "<!-- consensus-mcp:end -->"


def test_provision_creates_per_ai_files_with_dedupe(tmp_path):
    profiles = wiz._load_merged_profiles(None)
    wiz._provision_instruction_files(
        ["claude", "codex", "gemini", "kimi"], profiles, tmp_path,
    )
    # filename map: claude→CLAUDE.md, codex/kimi→AGENTS.md, gemini→GEMINI.md
    assert (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / "GEMINI.md").exists()
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # dedupe: AGENTS.md (codex AND kimi) written exactly once.
    assert agents.count(_SENTINEL_BEGIN) == 1
    assert agents.count(_SENTINEL_END) == 1
    # managed block carries the vendored guidelines.
    assert "Behavioral guidelines to reduce common LLM coding mistakes" in agents


def test_provision_is_idempotent(tmp_path):
    profiles = wiz._load_merged_profiles(None)
    wiz._provision_instruction_files(["claude", "codex"], profiles, tmp_path)
    first = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    wiz._provision_instruction_files(["claude", "codex"], profiles, tmp_path)
    second = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert first == second
    assert second.count(_SENTINEL_BEGIN) == 1
    assert second.count(_SENTINEL_END) == 1


def test_provision_preserves_existing_user_content(tmp_path):
    profiles = wiz._load_merged_profiles(None)
    existing = "# My project\n\nUser-authored notes here.\n"
    (tmp_path / "CLAUDE.md").write_text(existing, encoding="utf-8")
    wiz._provision_instruction_files(["claude", "codex"], profiles, tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "# My project" in text
    assert "User-authored notes here." in text
    assert _SENTINEL_BEGIN in text
    assert _SENTINEL_END in text
    assert "Behavioral guidelines to reduce common LLM coding mistakes" in text


def test_provision_refreshes_block_without_duplicating_user_content(tmp_path):
    """Re-running over a file that already has user content + a stale block keeps
    one block and the user's surrounding content intact."""
    profiles = wiz._load_merged_profiles(None)
    pre = (
        "# Header\n\n"
        f"{_SENTINEL_BEGIN}\n"
        "STALE managed content that should be refreshed\n"
        f"{_SENTINEL_END}\n\n"
        "# Footer kept by user\n"
    )
    (tmp_path / "GEMINI.md").write_text(pre, encoding="utf-8")
    wiz._provision_instruction_files(["claude", "gemini"], profiles, tmp_path)
    text = (tmp_path / "GEMINI.md").read_text(encoding="utf-8")
    assert text.count(_SENTINEL_BEGIN) == 1
    assert text.count(_SENTINEL_END) == 1
    assert "STALE managed content" not in text
    assert "# Header" in text
    assert "# Footer kept by user" in text
    assert "Behavioral guidelines to reduce common LLM coding mistakes" in text


def test_no_instructions_flag_skips_provisioning(tmp_path, monkeypatch):
    """End-to-end: --no-instructions suppresses instruction-file writes."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        wiz.shutil, "which",
        lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None,
    )
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-instructions",
        "--no-mcp-json",
    ])
    assert rc == 0
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "GEMINI.md").exists()


def test_init_provisions_instruction_files_by_default(tmp_path, monkeypatch):
    """End-to-end: a normal non-interactive init writes the per-AI files."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        wiz.shutil, "which",
        lambda name: "/fake/consensus-mcp" if name == "consensus-mcp" else None,
    )
    rc = wiz.main([
        "--non-interactive", "--accept-defaults",
        "--contributors", "claude,codex,gemini",
        "--no-mcp-json",
    ])
    assert rc == 0
    assert (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / "AGENTS.md").exists()   # codex
    assert (tmp_path / "GEMINI.md").exists()



def cfg_profiles_kind_host_peer():
    from consensus_mcp import _contributor_profiles as p
    return p.KIND_HOST_PEER


# ============================================================
# Task 4: _independent_ordered_names + preselect support
# ============================================================

_T4_PROFILES = {
    "claude": {"name": "claude", "kind": "host"},
    "codex": {"name": "codex", "kind": "cli_reviewer", "detect": {"command": "codex"}},
    "kimi": {"name": "kimi", "kind": "cli_reviewer", "detect": {"command": "kimi"}},
    "claude-swe-reviewer": {"name": "claude-swe-reviewer", "kind": "host_peer", "family": "claude"},
}


def test_selectable_names_exclude_host_peer():
    names = wiz._independent_ordered_names(_T4_PROFILES)
    assert "claude-swe-reviewer" not in names
    assert names[0] == "claude"  # host first
    assert set(names) == {"claude", "codex", "kimi"}


def test_multiselect_list_has_no_host_peer(monkeypatch, capsys):
    monkeypatch.setattr(wiz.shutil, "which", lambda c: "/x/" + c)  # all installed
    monkeypatch.setattr("builtins.input", lambda *_: "1,2")  # claude, codex
    chosen = wiz._select_contributors_interactive(_T4_PROFILES)
    out = capsys.readouterr().out
    assert "claude-swe-reviewer" not in out
    assert chosen == ["claude", "codex"]


def test_multiselect_preselected_defaults(monkeypatch):
    monkeypatch.setattr(wiz.shutil, "which", lambda c: None)  # none installed
    monkeypatch.setattr("builtins.input", lambda *_: "")  # accept default
    chosen = wiz._select_contributors_interactive(_T4_PROFILES, preselected=["claude", "kimi"])
    assert chosen == ["claude", "kimi"]


# ============================================================
# Task 6: _prompt_host_peer_followup (module-level alias for test convenience)
# ============================================================

_PROFILES = _T4_PROFILES


def test_followup_offered_when_host_selected(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    add = wiz._prompt_host_peer_followup(["claude", "codex"], _PROFILES, default_yes=False)
    assert add == "claude-swe-reviewer"


def test_followup_default_no(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "")
    add = wiz._prompt_host_peer_followup(["claude", "codex"], _PROFILES, default_yes=False)
    assert add is None


def test_followup_skipped_when_no_host(monkeypatch):
    def boom(*_):
        raise AssertionError("must not prompt when no host selected")
    monkeypatch.setattr("builtins.input", boom)
    assert wiz._prompt_host_peer_followup(["codex", "kimi"], _PROFILES, default_yes=False) is None


def test_followup_default_yes_on_empty(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "")
    add = wiz._prompt_host_peer_followup(["claude", "codex"], _PROFILES, default_yes=True)
    assert add == "claude-swe-reviewer"


# ============================================================
# D8: multiple same-family host_peers -> deterministic mini-select
# (default none; NEVER silently pick the first)
# ============================================================

_TWO_PEERS = {
    "claude": {"name": "claude", "kind": "host"},
    "codex": {"name": "codex", "kind": "cli_reviewer", "detect": {"command": "codex"}},
    "claude-swe-reviewer": {"name": "claude-swe-reviewer", "kind": "host_peer", "family": "claude"},
    "claude-deep-reviewer": {"name": "claude-deep-reviewer", "kind": "host_peer", "family": "claude"},
}


def test_followup_multiple_host_peers_mini_select(monkeypatch):
    # candidates are sorted: [claude-deep-reviewer, claude-swe-reviewer]; "2" picks the second
    monkeypatch.setattr("builtins.input", lambda *_: "2")
    add = wiz._prompt_host_peer_followup(["claude", "codex"], _TWO_PEERS, default_yes=False)
    assert add == "claude-swe-reviewer"


def test_followup_multiple_host_peers_default_none_never_first(monkeypatch):
    # empty / non-numeric input must default to NONE even with default_yes=True —
    # the mini-select must never silently append the first candidate.
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert wiz._prompt_host_peer_followup(["claude", "codex"], _TWO_PEERS, default_yes=True) is None


def test_followup_multiple_host_peers_out_of_range_none(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "9")
    assert wiz._prompt_host_peer_followup(["claude", "codex"], _TWO_PEERS, default_yes=False) is None


# ============================================================
# Task 8: _reconfigure_contributors — preserves legacy host_peer
# ============================================================

def test_reconfigure_preserves_existing_host_peer(monkeypatch):
    monkeypatch.setattr(wiz, "_load_merged_profiles", lambda *_: _PROFILES)
    monkeypatch.setattr(wiz.shutil, "which", lambda c: "/x/" + c)
    # accept preselected independents (empty), then accept supplemental (empty -> default Yes)
    answers = iter(["", ""])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    base_cfg = {"contributors": {"enabled": ["claude", "codex", "claude-swe-reviewer"]},
                "workflow": {"mode": "x", "independence": "y"},
                "convergence": {"rule": "z"}}
    wiz._reconfigure_contributors(base_cfg, _PROFILES)
    assert "claude-swe-reviewer" in base_cfg["contributors"]["enabled"]
    assert set(base_cfg["contributors"]["enabled"]) >= {"claude", "codex"}


def test_reconfigure_invalid_legacy_forces_two_independents(monkeypatch):
    monkeypatch.setattr(wiz, "_load_merged_profiles", lambda *_: _PROFILES)
    monkeypatch.setattr(wiz.shutil, "which", lambda c: "/x/" + c)
    # legacy [claude, claude-swe-reviewer] -> 1 independent; multi-select must
    # re-prompt until >=2; user adds codex, then declines supplemental
    # _independent_ordered_names(_PROFILES) = [claude, codex, kimi] (host first, then sorted)
    # so "1,2" = claude+codex
    answers = iter(["1,2", "n"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    base_cfg = {"contributors": {"enabled": ["claude", "claude-swe-reviewer"]},
                "workflow": {"mode": "x", "independence": "y"},
                "convergence": {"rule": "z"}}
    wiz._reconfigure_contributors(base_cfg, _PROFILES)
    assert wiz.profiles_mod.independent_count(base_cfg["contributors"]["enabled"], _PROFILES) >= 2
