# `consensus init --repair` (verify/repair) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `consensus init --repair` - a deterministic, non-destructive verify+repair that makes a partially-broken install healthy (re-create missing pieces, report diverged ones), surfaced as a CLI flag, a 4th existing-config menu option, and a skill carve-out.

**Architecture:** A new verify/repair engine in `consensus_mcp/_init_wizard.py` (where all the install primitives already live - no new module, no import cycle) that *composes the existing non-destructive installers* (`_write_mcp_json`, `update_gitignore`, `_install_project_agents`, `_provision_instruction_files`) plus a read-only global-enforcement detector, classifies each of 6 components, emits version-stable summary lines, and returns a 4-value exit code (0/2/3/7). `cmd_init` gains a `--repair` flag + handler + a gate carve-out so repair doesn't trip the v1.29.0 already-configured gate.

**Tech Stack:** Python 3.11+ (stdlib only), pytest.

**Spec:** `docs/superpowers/specs/2026-05-23-init-verify-repair-design.md` (read it; the 6-component table + exit taxonomy + contract are authoritative).

**Test runner:** `VPY=python` -> `$VPY -m pytest ...` from repo root. **Do NOT edit `build/lib/`.**

**Reuse map (exact, in `consensus_mcp/_init_wizard.py`):**
- `_detect_repo_root()` L62 - `_resolve_config_path(args, repo_root)` L843 - `_resolve_claude_home()` L116 - `_resolve_mcp_json_path(repo_root)` L659
- `.mcp.json`: `_load_existing_mcp_json(path) -> (dict|None, str|None)` L680 - `_build_consensus_mcp_entry(command, args, state_root, project_root) -> dict` L663 - `_resolve_mcp_command(explicit) -> (str, list[str], bool)` L612 - `_write_mcp_json(repo_root, state_root, project_root, command, args) -> (status_str, Path)` L781
- `.gitignore`: `update_gitignore(repo_root) -> bool` L1525 - `GITIGNORE_OPEN_MARKER` L53
- agents: `_install_project_agents(repo_root, force) -> list[str]` L270 - `_PROJECT_AGENT_FILES` L259 - `_agents_source_root()` L265
- instructions: `_provision_instruction_files(selection, profiles, repo_root) -> list[Path]` L1200 - `INSTRUCTION_BEGIN_MARKER` L871
- enforcement (#6, read-only): `_resolve_settings_json_path(claude_home)` L371 - `_load_existing_settings_json(path) -> (dict, str|None)` L485 - `_build_consensus_hook_groups(claude_home) -> dict[str,list[dict]]` L408 - `_installed_hook_script_path(claude_home, script)` L375
- config: `cfg.load(path)` raises `cfg.ConfigValidationError` - `cfg.default_config()`
- gate + menu: existing-config gate in `cmd_init` (the `if config_path.exists() and not (args.reconfigure or args.force):` block) - `_prompt_existing_config_action(config_path)` L1629 - arg parser L2061+

---

## File structure

- **Modify** `consensus_mcp/_init_wizard.py`:
  - Add summary-line prefix constants + a `RepairComponent` result structure + `_repair_exit_code(...)` (Task 1).
  - Add `_verify_repair_install(repo_root, *, dry_run, claude_home)` engine + per-component helpers (Tasks 2-4).
  - Add `--repair` arg, the `--repair` handler in `cmd_init`, and the gate carve-out (Task 5).
  - Extend `_prompt_existing_config_action` to a 4th "repair" option + map it in the gate (Task 6).
- **Create** `consensus_mcp/tests/test_init_repair.py` - engine + component + exit-code tests (Tasks 1-4).
- **Modify** `consensus_mcp/tests/test_init_wizard_already_configured.py` - `--repair` CLI/gate tests + menu 4th option (Tasks 5-6).
- **Modify** `consensus_mcp/claude_extensions/skills/consensus/SKILL.md` + `commands/consensus-init.md` - verify/repair menu option (Task 7).
- **Modify** `CHANGELOG.md` - 1.29.1 entry (Task 8).

---

### Task 1: Repair result vocabulary + exit-code aggregation

**Files:** Modify `consensus_mcp/_init_wizard.py` (add near the other init constants/helpers, e.g. just above `_prompt_existing_config_action`). Test: `consensus_mcp/tests/test_init_repair.py` (new).

- [ ] **Step 1: Write the failing tests**

Create `consensus_mcp/tests/test_init_repair.py`:

```python
import consensus_mcp._init_wizard as wiz


def test_summary_prefixes_are_stable():
    # The skill parses these - they are a contract.
    assert wiz.REPAIR_OK == "OK:"
    assert wiz.REPAIR_FIXED == "REPAIRED:"
    assert wiz.REPAIR_SKIP == "SKIP:"
    assert wiz.REPAIR_GLOBAL == "REPORT-GLOBAL:"


def test_exit_code_all_healthy_is_0():
    comps = [wiz.RepairComponent("config", "ok"), wiz.RepairComponent(".mcp.json", "ok")]
    assert wiz._repair_exit_code(comps) == 0


def test_exit_code_repaired_is_0():
    comps = [wiz.RepairComponent(".mcp.json", "repaired")]
    assert wiz._repair_exit_code(comps) == 0


def test_exit_code_config_missing_is_2():
    comps = [wiz.RepairComponent("config", "missing_config")]
    assert wiz._repair_exit_code(comps) == 2


def test_exit_code_config_invalid_is_3():
    comps = [wiz.RepairComponent("config", "invalid_config")]
    assert wiz._repair_exit_code(comps) == 3


def test_exit_code_diverged_is_7():
    comps = [wiz.RepairComponent(".gitignore", "skipped_diverged")]
    assert wiz._repair_exit_code(comps) == 7


def test_exit_code_global_dead_is_7():
    comps = [wiz.RepairComponent("enforcement", "report_global")]
    assert wiz._repair_exit_code(comps) == 7


def test_exit_code_config_outranks_diverged():
    # config missing/invalid is the prerequisite failure -> wins over 7.
    comps = [wiz.RepairComponent("config", "missing_config"),
             wiz.RepairComponent(".gitignore", "skipped_diverged")]
    assert wiz._repair_exit_code(comps) == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_repair.py -q`
Expected: FAIL - `AttributeError` (`REPAIR_OK` / `RepairComponent` / `_repair_exit_code` undefined).

- [ ] **Step 3: Implement**

In `consensus_mcp/_init_wizard.py`, add (e.g. just above `def _prompt_existing_config_action`):

```python
from dataclasses import dataclass

# v1.29.1 (verify/repair consult): version-STABLE summary prefixes. The consensus
# skill parses these to relay repair results - treat as a contract (a regression
# test pins them), like ALREADY_CONFIGURED_TOKEN.
REPAIR_OK = "OK:"            # present and healthy
REPAIR_FIXED = "REPAIRED:"   # was missing, recreated
REPAIR_SKIP = "SKIP:"        # exists but diverged from shipped; left intact
REPAIR_GLOBAL = "REPORT-GLOBAL:"  # global enforcement issue, not repaired here


@dataclass
class RepairComponent:
    """One health component's outcome. state is one of:
    'ok' | 'repaired' | 'skipped_diverged' | 'missing_config' |
    'invalid_config' | 'report_global'."""
    name: str
    state: str
    detail: str = ""


def _repair_exit_code(components: list["RepairComponent"]) -> int:
    """Aggregate component states into the repair exit code.

    0 = healthy or fully repaired; 2 = config missing; 3 = config invalid;
    7 = repair incomplete (diverged left for --force, or global enforcement
    dead). Config prerequisite (2/3) outranks 7 (you can't repair #2-#5 without
    a valid config). 'ok' and 'repaired' do not raise the code.
    """
    states = {c.state for c in components}
    if "missing_config" in states:
        return 2
    if "invalid_config" in states:
        return 3
    if "skipped_diverged" in states or "report_global" in states:
        return 7
    return 0
```

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_repair.py -q`
Expected: PASS (8 cases).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_repair.py
git commit -m "feat(repair): repair result vocabulary + exit-code aggregation"
```

---

### Task 2: config (#1) + `.mcp.json` (#2) component checks

**Files:** Modify `consensus_mcp/_init_wizard.py`. Test: `consensus_mcp/tests/test_init_repair.py` (append).

Each component check returns a `RepairComponent` and appends a summary line. config (#1) is NOT repairable (report-only); `.mcp.json` (#2) repairs when missing, reports when diverged.

- [ ] **Step 1: Write the failing tests** - append to `test_init_repair.py`:

```python
import yaml
import consensus_mcp.config as cfg


def _seed_config(tmp_path):
    d = tmp_path / ".consensus"; d.mkdir(exist_ok=True)
    (d / "config.yaml").write_text(yaml.safe_dump(cfg.default_config()), encoding="utf-8")
    return d / "config.yaml"


def test_check_config_missing(tmp_path):
    comp, line = wiz._repair_check_config(tmp_path / ".consensus" / "config.yaml")
    assert comp.state == "missing_config"
    assert line.startswith(wiz.REPAIR_SKIP) or "config" in line.lower()


def test_check_config_invalid(tmp_path):
    p = tmp_path / ".consensus"; p.mkdir()
    (p / "config.yaml").write_text("not: [valid", encoding="utf-8")  # bad YAML/schema
    comp, _ = wiz._repair_check_config(p / "config.yaml")
    assert comp.state == "invalid_config"


def test_check_config_ok(tmp_path):
    cfgp = _seed_config(tmp_path)
    comp, line = wiz._repair_check_config(cfgp)
    assert comp.state == "ok"
    assert line.startswith(wiz.REPAIR_OK)


def test_check_mcp_missing_repairs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    comp, line = wiz._repair_check_mcp(tmp_path, dry_run=False)
    assert comp.state == "repaired"
    assert line.startswith(wiz.REPAIR_FIXED)
    assert (tmp_path / ".mcp.json").exists()


def test_check_mcp_missing_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    comp, line = wiz._repair_check_mcp(tmp_path, dry_run=True)
    assert comp.state == "repaired"  # would repair
    assert not (tmp_path / ".mcp.json").exists()  # but wrote nothing


def test_check_mcp_present_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    wiz._repair_check_mcp(tmp_path, dry_run=False)  # create it
    comp, line = wiz._repair_check_mcp(tmp_path, dry_run=False)  # second pass
    assert comp.state == "ok"
    assert line.startswith(wiz.REPAIR_OK)
```

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_repair.py -q`
Expected: FAIL - `_repair_check_config` / `_repair_check_mcp` undefined.

- [ ] **Step 3: Implement** - add to `consensus_mcp/_init_wizard.py`:

```python
def _repair_check_config(config_path: Path) -> tuple[RepairComponent, str]:
    """#1 config.yaml - NOT repairable (can't synthesize panel choices)."""
    if not config_path.exists():
        return (RepairComponent("config.yaml", "missing_config"),
                f"{REPAIR_SKIP} config.yaml missing - run `consensus init` "
                f"(cannot synthesize your panel choices)")
    try:
        cfg.load(config_path)
    except cfg.ConfigValidationError as exc:
        return (RepairComponent("config.yaml", "invalid_config"),
                f"{REPAIR_SKIP} config.yaml invalid ({exc}) - run "
                f"`consensus init --reconfigure`")
    return (RepairComponent("config.yaml", "ok"), f"{REPAIR_OK} config.yaml")


def _repair_check_mcp(repo_root: Path, *, dry_run: bool) -> tuple[RepairComponent, str]:
    """#2 .mcp.json - repair when the consensus-mcp entry is missing; report when
    present-but-diverged; ok when present-and-matching."""
    mcp_path = _resolve_mcp_json_path(repo_root)
    existing, _ = _load_existing_mcp_json(mcp_path)
    command, mcp_args, _ = _resolve_mcp_command(None)
    state_root = repo_root / "consensus-state"
    expected = _build_consensus_mcp_entry(command, mcp_args, state_root, repo_root)
    servers = (existing or {}).get("mcpServers", {})
    have = servers.get("consensus-mcp")
    if have is None:
        if not dry_run:
            _write_mcp_json(repo_root, state_root, repo_root, command, mcp_args)
        return (RepairComponent(".mcp.json", "repaired"),
                f"{REPAIR_FIXED} .mcp.json (consensus-mcp server entry)")
    if have != expected:
        return (RepairComponent(".mcp.json", "skipped_diverged"),
                f"{REPAIR_SKIP} .mcp.json consensus-mcp entry diverges from "
                f"shipped; pass --force to overwrite")
    return (RepairComponent(".mcp.json", "ok"), f"{REPAIR_OK} .mcp.json")
```

NOTE for implementer: confirm `_build_consensus_mcp_entry`'s exact `project_root`/`state_root` argument semantics by reading L663-L700 and mirror what `cmd_init` passes at the `_write_mcp_json` call (~L1948); the equality check `have != expected` must compare the same shape the writer produces. If `_resolve_mcp_command(None)` requires a different sentinel for "no explicit command", match `cmd_init`'s call.

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_repair.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_repair.py
git commit -m "feat(repair): config + .mcp.json component checks"
```

---

### Task 3: `.gitignore` (#3) + agents (#4) + instructions (#5) checks

**Files:** Modify `consensus_mcp/_init_wizard.py`. Test: append to `test_init_repair.py`.

These three repair-when-missing using the existing installers. agents (#4) already SKIPs diverged internally.

- [ ] **Step 1: Write the failing tests** - append:

```python
def test_check_gitignore_missing_repairs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    comp, line = wiz._repair_check_gitignore(tmp_path, dry_run=False)
    assert comp.state == "repaired"
    assert wiz.GITIGNORE_OPEN_MARKER in (tmp_path / ".gitignore").read_text()


def test_check_gitignore_present_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    wiz.update_gitignore(tmp_path)  # create block
    comp, line = wiz._repair_check_gitignore(tmp_path, dry_run=False)
    assert comp.state == "ok"
    assert line.startswith(wiz.REPAIR_OK)


def test_check_gitignore_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    comp, _ = wiz._repair_check_gitignore(tmp_path, dry_run=True)
    assert comp.state == "repaired"
    assert not (tmp_path / ".gitignore").exists()


def test_check_agents_missing_repairs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    comp, line = wiz._repair_check_agents(tmp_path, dry_run=False)
    assert comp.state == "repaired"
    agents_dir = tmp_path / ".claude" / "agents"
    assert agents_dir.exists() and any(agents_dir.iterdir())


def test_check_agents_present_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    wiz._install_project_agents(tmp_path, force=False)  # install
    comp, line = wiz._repair_check_agents(tmp_path, dry_run=False)
    assert comp.state == "ok"


def test_check_instructions_missing_repairs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfgp = _seed_config(tmp_path)
    comp, line = wiz._repair_check_instructions(tmp_path, dry_run=False)
    assert comp.state in ("repaired", "ok")  # at least one instruction file managed
```

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_repair.py -q`
Expected: FAIL - the three `_repair_check_*` undefined.

- [ ] **Step 3: Implement** - add to `consensus_mcp/_init_wizard.py`:

```python
def _repair_check_gitignore(repo_root: Path, *, dry_run: bool) -> tuple[RepairComponent, str]:
    """#3 .gitignore managed block - re-add when absent."""
    gi = repo_root / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if GITIGNORE_OPEN_MARKER in text:
        return (RepairComponent(".gitignore", "ok"), f"{REPAIR_OK} .gitignore managed block")
    if not dry_run:
        update_gitignore(repo_root)
    return (RepairComponent(".gitignore", "repaired"), f"{REPAIR_FIXED} .gitignore managed block")


def _repair_check_agents(repo_root: Path, *, dry_run: bool) -> tuple[RepairComponent, str]:
    """#4 .claude/agents/ - re-copy missing subagent files (installer SKIPs diverged)."""
    agents_dir = repo_root / ".claude" / "agents"
    missing = [f for f in _PROJECT_AGENT_FILES if not (agents_dir / f).exists()]
    if not missing:
        return (RepairComponent(".claude/agents", "ok"), f"{REPAIR_OK} .claude/agents")
    if not dry_run:
        _install_project_agents(repo_root, force=False)
    return (RepairComponent(".claude/agents", "repaired"),
            f"{REPAIR_FIXED} .claude/agents ({', '.join(missing)})")


def _repair_check_instructions(repo_root: Path, *, dry_run: bool) -> tuple[RepairComponent, str]:
    """#5 per-AI instruction managed blocks - re-seed when absent. Reads enabled
    contributors from the existing config."""
    config_path = _resolve_config_path_for_repo(repo_root)
    loaded = cfg.load(config_path)
    enabled, profiles = _enabled_contributors_and_profiles(loaded)  # see NOTE
    # detect: is the managed block present in at least the expected instruction files?
    # Reuse the same target-file logic _provision_instruction_files uses.
    needs = _instruction_files_missing_block(enabled, profiles, repo_root)
    if not needs:
        return (RepairComponent("instructions", "ok"), f"{REPAIR_OK} instruction files")
    if not dry_run:
        _provision_instruction_files(enabled, profiles, repo_root)
    return (RepairComponent("instructions", "repaired"),
            f"{REPAIR_FIXED} instruction files ({', '.join(str(p) for p in needs)})")
```

NOTE for implementer (instructions #5): `_provision_instruction_files(selection, profiles, repo_root)` takes the enabled contributor selection + merged profiles. Derive these from the loaded config the same way `cmd_init` does on the reconfigure/non-fresh path (find where `cmd_init` computes `enabled` + `merged_profiles` before calling `_provision_instruction_files` at ~L1905, and factor that derivation into the small helpers `_enabled_contributors_and_profiles(loaded)` and `_instruction_files_missing_block(enabled, profiles, repo_root)`). `_instruction_files_missing_block` checks each target file for `INSTRUCTION_BEGIN_MARKER`. If deriving profiles proves heavy, the minimal correct behavior is: detect presence of `INSTRUCTION_BEGIN_MARKER` in the existing instruction files; if absent, call `_provision_instruction_files` with the enabled set from config. Keep it non-destructive (the helper already upserts a managed block, idempotent per `_upsert_managed_block`).

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_repair.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_repair.py
git commit -m "feat(repair): .gitignore + agents + instructions component checks"
```

---

### Task 4: global enforcement (#6) read-only detection + the `_verify_repair_install` engine

**Files:** Modify `consensus_mcp/_init_wizard.py`. Test: append to `test_init_repair.py`.

#6 is REPORT-ONLY (never writes ~/.claude). The engine runs #1 first (short-circuits to 2/3 if config unusable), then #2-#5, then #6, returning (summary_lines, exit_code).

- [ ] **Step 1: Write the failing tests** - append:

```python
def test_check_enforcement_dead_reports_global(tmp_path):
    # empty claude_home -> no settings.json hooks -> dead
    comp, line = wiz._repair_check_enforcement(tmp_path / "fake_claude_home")
    assert comp.state == "report_global"
    assert line.startswith(wiz.REPAIR_GLOBAL)
    assert "install-claude-code" in line


def test_engine_happy_path_repairs_and_exits_0(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_config(tmp_path)
    # healthy enforcement: stub the detector to ok so #6 doesn't force 7
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    lines, code = wiz._verify_repair_install(tmp_path, dry_run=False, claude_home=tmp_path / "ch")
    assert code == 0
    assert any(l.startswith(wiz.REPAIR_FIXED) for l in lines)  # repaired #2-#5


def test_engine_missing_config_short_circuits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config
    lines, code = wiz._verify_repair_install(tmp_path, dry_run=False, claude_home=tmp_path / "ch")
    assert code == 2
    assert any("config.yaml missing" in l for l in lines)


def test_engine_idempotent_second_run_all_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_config(tmp_path)
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    wiz._verify_repair_install(tmp_path, dry_run=False, claude_home=tmp_path / "ch")  # first
    lines, code = wiz._verify_repair_install(tmp_path, dry_run=False, claude_home=tmp_path / "ch")  # second
    assert code == 0
    assert all(not l.startswith(wiz.REPAIR_FIXED) for l in lines)  # nothing re-written
```

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_repair.py -q`
Expected: FAIL - `_repair_check_enforcement` / `_verify_repair_install` undefined.

- [ ] **Step 3: Implement** - add to `consensus_mcp/_init_wizard.py`:

```python
def _repair_check_enforcement(claude_home: Path) -> tuple[RepairComponent, str]:
    """#6 global enforcement - READ-ONLY. Healthy iff settings.json carries the
    consensus hooks for every expected event AND every referenced hook script
    exists on disk. Never writes ~/.claude (that's --install-claude-code)."""
    settings_path = _resolve_settings_json_path(claude_home)
    settings, _ = _load_existing_settings_json(settings_path)
    expected = _build_consensus_hook_groups(claude_home)
    hooks = (settings or {}).get("hooks", {})
    for event in expected:
        if not hooks.get(event):
            return (RepairComponent("enforcement", "report_global"),
                    f"{REPAIR_GLOBAL} enforcement: settings.json missing consensus "
                    f"{event} hook - run `consensus-init --install-claude-code`")
    # referenced hook scripts present?
    for groups in expected.values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                for tok in cmd.split():
                    if tok.endswith(".py") and tok.startswith(str(claude_home)) and not Path(tok).exists():
                        return (RepairComponent("enforcement", "report_global"),
                                f"{REPAIR_GLOBAL} enforcement: hook script missing ({tok}) "
                                f"- run `consensus-init --install-claude-code`")
    return (RepairComponent("enforcement", "ok"), f"{REPAIR_OK} enforcement")


def _verify_repair_install(repo_root: Path, *, dry_run: bool,
                           claude_home: Path) -> tuple[list[str], int]:
    """Comprehensive verify + non-destructive repair. Returns (summary_lines, exit_code)."""
    config_path = _resolve_config_path_for_repo(repo_root)
    comps: list[RepairComponent] = []
    lines: list[str] = []

    c1, l1 = _repair_check_config(config_path)
    comps.append(c1); lines.append(l1)
    if c1.state in ("missing_config", "invalid_config"):
        # #2-#5 cannot be derived without a valid config; report #6 then stop.
        c6, l6 = _repair_check_enforcement(claude_home)
        comps.append(c6); lines.append(l6)
        return lines, _repair_exit_code(comps)

    for check in (_repair_check_mcp, _repair_check_gitignore,
                  _repair_check_agents, _repair_check_instructions):
        c, l = check(repo_root, dry_run=dry_run)
        comps.append(c); lines.append(l)
    c6, l6 = _repair_check_enforcement(claude_home)
    comps.append(c6); lines.append(l6)
    return lines, _repair_exit_code(comps)
```

NOTE for implementer: add the tiny helper `_resolve_config_path_for_repo(repo_root)` = `repo_root / ".consensus" / "config.yaml"` (or reuse `_resolve_config_path` with a stub args having `config=None`). The enforcement hook-script token detection above is heuristic; verify against the real `command` strings `_build_consensus_hook_groups` produces (read L408-L484) and tighten the token match to the exact script-path form they use (`_installed_hook_script_path(claude_home, name)`), iterating the known hook-script names rather than string-sniffing if that's cleaner.

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_repair.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_repair.py
git commit -m "feat(repair): enforcement detection + verify/repair engine"
```

---

### Task 5: `--repair` CLI flag + handler + gate carve-out

**Files:** Modify `consensus_mcp/_init_wizard.py`. Test: `consensus_mcp/tests/test_init_wizard_already_configured.py` (append).

- [ ] **Step 1: Write the failing tests** - append to `test_init_wizard_already_configured.py`:

```python
def test_repair_flag_runs_engine_and_exits_0(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    _write_existing_config(tmp_path)
    rc = wiz.main(["--repair"])
    assert rc == 0
    out = capsys.readouterr().out
    assert any(line.startswith(("OK:", "REPAIRED:")) for line in out.splitlines())


def test_repair_does_not_emit_already_configured_token(tmp_path, capsys, monkeypatch):
    """Gate carve-out: --repair must NOT trip the v1.29.0 existing-config gate."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    _write_existing_config(tmp_path)
    rc = wiz.main(["--repair"])
    out = capsys.readouterr().out
    assert wiz.ALREADY_CONFIGURED_TOKEN not in out
    assert rc != 4


def test_repair_missing_config_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    rc = wiz.main(["--repair"])
    assert rc == 2


def test_repair_dry_run_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    _write_existing_config(tmp_path)
    rc = wiz.main(["--repair", "--dry-run"])
    assert not (tmp_path / ".mcp.json").exists()  # previewed, not written
```

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py -q`
Expected: FAIL - `--repair` unknown arg / token still emitted.

- [ ] **Step 3: Implement**

(a) Add the arg near `--reconfigure`/`--force` (after L2067 in `build_parser`):

```python
    parser.add_argument("--repair", action="store_true",
                        help="verify the install and non-destructively repair "
                             "missing pieces (report diverged); does not re-prompt")
```

(b) In `cmd_init`, add the `--repair` handler BEFORE the existing-config gate (and after the `--check`/`--print-defaults`/`--install-claude-code` early returns). Place it right before the `if config_path.exists() and not (args.reconfigure or args.force):` gate:

```python
    if getattr(args, "repair", False):
        lines, code = _verify_repair_install(
            repo_root, dry_run=args.dry_run, claude_home=_resolve_claude_home())
        for line in lines:
            print(line)
        return code
```

(c) Gate carve-out - change the gate condition so `--repair` is exempt (it returns above, but make the exemption explicit and defensive for any future reordering):

```python
    if config_path.exists() and not (args.reconfigure or args.force or args.repair):
```

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py consensus_mcp/tests/test_init_repair.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard_already_configured.py
git commit -m "feat(repair): --repair CLI flag + handler + gate carve-out"
```

---

### Task 6: 4th menu option (verify/repair)

**Files:** Modify `consensus_mcp/_init_wizard.py`. Test: `consensus_mcp/tests/test_init_wizard_already_configured.py` (update + append).

The TTY menu becomes safety-ordered: `[1]` leave / `[2]` verify/repair / `[3]` reconfigure / `[4]` force.

- [ ] **Step 1: Update the helper unit tests + add menu-repair tests**

In `test_init_wizard_already_configured.py`, update `test_prompt_existing_config_action_choices`'s parametrization to the new numbering and add the repair case:

```python
@pytest.mark.parametrize("raw,expected", [
    ("1", "leave"),
    ("", "leave"),
    ("2", "repair"),
    ("3", "reconfigure"),
    ("4", "force"),
])
def test_prompt_existing_config_action_choices(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setattr(builtins, "input", _stub_input([raw]))
    assert wiz._prompt_existing_config_action(tmp_path / ".consensus" / "config.yaml") == expected
```

Update `test_prompt_existing_config_action_reprompts_on_invalid` to feed a now-invalid token then a valid one, e.g. `_stub_input(["x", "9", "4"])` -> `"force"`.

Append a menu->repair integration test:

```python
def test_tty_menu_repair_runs_repair(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(wiz, "_prompt_existing_config_action", lambda _p: "repair")
    monkeypatch.setattr(wiz, "_repair_check_enforcement",
                        lambda ch: (wiz.RepairComponent("enforcement", "ok"), f"{wiz.REPAIR_OK} enforcement"))
    _write_existing_config(tmp_path)
    rc = wiz.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert any(l.startswith(("OK:", "REPAIRED:")) for l in out.splitlines())
```

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py -q`
Expected: FAIL - menu returns wrong values for new numbering; `"repair"` action unhandled.

- [ ] **Step 3: Implement**

(a) In `_prompt_existing_config_action` (L1629), update the menu text + return mapping:

```python
    print(f"consensus-mcp is already configured here: {config_path}")
    print("  [1] Leave as-is - no changes            (default)")
    print("  [2] Verify / repair - re-create missing pieces, report diverged")
    print("  [3] Reconfigure - re-prompt, keep current settings as defaults, show diff")
    print("  [4] Force overwrite - discard local config edits, write fresh")
    while True:
        try:
            raw = input("Choose [1/2/3/4, default 1]: ").strip()
        except EOFError:
            # Deliberate divergence from the other prompts (which map EOF ->
            # KeyboardInterrupt -> exit 1): this menu has a safe no-op default,
            # so an unattended/EOF caller should accept the default, not abort.
            return "leave"
        if raw in ("", "1"):
            return "leave"
        if raw == "2":
            return "repair"
        if raw == "3":
            return "reconfigure"
        if raw == "4":
            return "force"
        print("Please enter 1, 2, 3, or 4.", file=sys.stderr)
```

(b) In the existing-config gate's TTY branch, handle the new action (add the `repair` case before reconfigure/force):

```python
        if action == "leave":
            print(f"Leaving existing configuration unchanged: {config_path}")
            return 0
        if action == "repair":
            lines, code = _verify_repair_install(
                repo_root, dry_run=args.dry_run, claude_home=_resolve_claude_home())
            for line in lines:
                print(line)
            return code
        if action == "reconfigure":
            args.reconfigure = True
        else:  # "force"
            args.force = True
            args.accept_defaults = True
```

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py consensus_mcp/tests/test_init_repair.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard_already_configured.py
git commit -m "feat(repair): add verify/repair as the 4th existing-config menu option"
```

---

### Task 7: Skill + command carve-out (verify/repair option) + contract test

**Files:** Modify `consensus_mcp/claude_extensions/skills/consensus/SKILL.md`, `consensus_mcp/claude_extensions/commands/consensus-init.md`. Test: `consensus_mcp/tests/test_init_wizard_already_configured.py` (append).

The skill's AskUserQuestion menu (shown on the `STATUS: already-configured` token) gains a "Verify / repair" choice that re-invokes `--repair`.

- [ ] **Step 1: Write the failing contract test** - append:

```python
def test_repair_option_documented_in_skill_and_command():
    skill = (_ext_dir() / "skills" / "consensus" / "SKILL.md").read_text(encoding="utf-8")
    command = (_ext_dir() / "commands" / "consensus-init.md").read_text(encoding="utf-8")
    assert "--repair" in skill and "Verify / repair" in skill
    assert "--repair" in command
```

(`_ext_dir()` already exists in this test file.)

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py::test_repair_option_documented_in_skill_and_command -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `SKILL.md`, update the "If the project is already configured" section's option list (step 2) to four options and add the repair re-invoke to step 3:

```markdown
2. Present these options to the user via `AskUserQuestion`:
   - **Leave as-is** - already set up; do nothing.
   - **Verify / repair** - re-create any missing pieces, report diverged ones (non-destructive).
   - **Reconfigure** - update settings, keeping current values as defaults.
   - **Force overwrite** - discard local config edits and write fresh.
3. Act on the choice **one-shot** (do NOT loop):
   - Leave -> stop; tell the user nothing changed.
   - Verify / repair -> run `consensus-init --from-claude-code --repair` once; relay its `OK:`/`REPAIRED:`/`SKIP:`/`REPORT-GLOBAL:` summary lines.
   - Reconfigure -> run `consensus-init --from-claude-code --reconfigure` once.
   - Force overwrite -> run `consensus-init --from-claude-code --force` once.
```

In `consensus-init.md`, extend the "Already configured" paragraph to mention the verify/repair option re-invoking `consensus-init --from-claude-code --repair`.

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/claude_extensions/skills/consensus/SKILL.md consensus_mcp/claude_extensions/commands/consensus-init.md consensus_mcp/tests/test_init_wizard_already_configured.py
git commit -m "feat(skill): verify/repair menu option + contract test"
```

---

### Task 8: CHANGELOG (1.29.1)

**Files:** Modify `CHANGELOG.md`.

- [ ] **Step 1: Replace the `## 1.29.1 - unreleased` stub body** with:

```markdown
## 1.29.1 - unreleased

**`consensus init --repair` - verify & repair a partially-broken install.**
Re-running init on a project whose `.mcp.json`, `.gitignore` block,
`.claude/agents/` files, or instruction blocks went missing can now restore
them non-destructively, instead of only leave/reconfigure/force.

### Added
- `consensus init --repair` (and the `consensus-init --repair` binary): a
  deterministic verify of all install components that **re-creates missing**
  project pieces and **reports diverged** ones (`pass --force`), never clobbering
  local edits. Emits version-stable summary lines (`OK:`/`REPAIRED:`/`SKIP:`/
  `REPORT-GLOBAL:`); composes with `--dry-run` (preview, no writes); idempotent.
- Exit codes: `0` healthy-or-repaired, `2` config missing, `3` config invalid,
  `7` repair incomplete (diverged left for `--force`, or global enforcement
  detected dead). Global enforcement (`~/.claude` hooks) is **reported** (run
  `consensus-init --install-claude-code`), not written from a per-project repair.
- The existing-config menu (TTY + the `consensus` skill) gains a **Verify /
  repair** option; `--repair` is exempt from the already-configured gate.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): consensus init --repair (1.29.1)"
```

---

### Task 9: Full-suite verification

- [ ] **Step 1: Full suite** - Run: `$VPY -m pytest consensus_mcp/tests/ -q` - expect all pass (baseline 1422 + new repair tests).
- [ ] **Step 2: Manual smoke (branch code)** - from a temp dir with a broken install (valid `.consensus/config.yaml` but no `.mcp.json`), run `PYTHONPATH=<repo> $VPY -m consensus_mcp._init_wizard --repair`; expect a `REPAIRED: .mcp.json` line, `.mcp.json` created, exit 0 (or 7 if enforcement dead in that env). Confirm `--repair --dry-run` writes nothing.
- [ ] **Step 3: `--check` untouched** - Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard.py -k check -q` - the 3 `--check` tests still pass.
- [ ] **Step 4: Report** - artifact-scoped: name the branch + that no tag is cut yet.

---

## Self-review (against the spec)

- **Q1a repair-missing/report-diverged** -> component checks re-create missing, report diverged (Tasks 2-3); `.mcp.json` diverged -> `skipped_diverged` -> exit 7. [ok]
- **Q2a comprehensive verify, project repair / global report** -> engine checks #1-#6; #6 read-only `_repair_check_enforcement` (Task 4). [ok]
- **Q3a flag + menu + skill** -> Task 5 (flag), Task 6 (menu), Task 7 (skill/command). [ok]
- **Q4a keep `--check`** -> untouched; Task 9 Step 3 verifies its 3 tests. [ok]
- **Q5a deterministic** -> engine has no TTY branch; same in both (Task 4). [ok]
- **Exit taxonomy 0/2/3/7** -> `_repair_exit_code` (Task 1), config outranks 7. [ok]
- **Gate carve-out** -> Task 5 (c) + `test_repair_does_not_emit_already_configured_token`. [ok]
- **Version-stable summary** -> prefixes pinned by `test_summary_prefixes_are_stable` (Task 1) + skill contract test (Task 7). [ok]
- **`--dry-run` composes** -> engine `dry_run` param + tests (Tasks 2-5). [ok]
- **Idempotency** -> `test_engine_idempotent_second_run_all_ok` (Task 4). [ok]
- **config not repairable** -> `_repair_check_config` -> 2/3, no write (Task 2); engine short-circuits (Task 4). [ok]
- **Dead global enforcement loud + exit 7** -> `REPORT-GLOBAL` line + `report_global`->7 (Tasks 1, 4). [ok]
- **Both entry points** -> `consensus`/`consensus-init` share `main`; `--repair` on the parser covers both. [ok]
- **Menu test 4th option** -> Task 6 updates parametrization + asserts. [ok]
- No placeholders; names consistent (`RepairComponent`, `_repair_check_*`, `_verify_repair_install`, `_repair_exit_code`, `REPAIR_*`). Two implementer NOTES flag where to confirm exact reuse-helper shapes (mcp entry equality; instruction selection derivation; enforcement hook-script token match) - these are "read the named helper to match its shape," not unresolved design.
