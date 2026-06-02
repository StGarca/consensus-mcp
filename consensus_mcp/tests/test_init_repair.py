import yaml
import consensus_mcp._init_wizard as wiz
import consensus_mcp.config as cfg


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


# ---------------------------------------------------------------------------
# Task 2: config (#1) + .mcp.json (#2) component checks
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Task 3: .gitignore (#3) + agents (#4) + instructions (#5) component checks
# ---------------------------------------------------------------------------

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
    assert comp.state == "repaired"


def test_check_agents_diverged_reports_skip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    wiz._install_project_agents(tmp_path, force=False)  # install clean
    # corrupt one agent file so it diverges from shipped
    agents_dir = tmp_path / ".claude" / "agents"
    target = agents_dir / wiz._PROJECT_AGENT_FILES[0]
    target.write_text(target.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")
    comp, line = wiz._repair_check_agents(tmp_path, dry_run=False)
    assert comp.state == "skipped_diverged"
    assert line.startswith(wiz.REPAIR_SKIP)
    # diverged file left intact (not clobbered)
    assert "# local edit" in target.read_text(encoding="utf-8")


def test_check_agents_missing_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    comp, _ = wiz._repair_check_agents(tmp_path, dry_run=True)
    assert comp.state == "repaired"
    assert not (tmp_path / ".claude" / "agents").exists() or not any((tmp_path / ".claude" / "agents").iterdir())


# ---------------------------------------------------------------------------
# Task 4: enforcement (#6) detection + _verify_repair_install engine
# ---------------------------------------------------------------------------

def test_check_enforcement_dead_reports_global(tmp_path):
    # empty claude_home -> no settings.json hooks -> dead
    comp, line = wiz._repair_check_enforcement(tmp_path / "fake_claude_home")
    assert comp.state == "report_global"
    assert line.startswith(wiz.REPAIR_GLOBAL)
    assert "install-claude-code" in line


def test_engine_happy_path_repairs_and_exits_0(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_config(tmp_path)
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


def test_check_enforcement_healthy_after_install_is_ok(tmp_path):
    """The 'ok' branch must hold against a REAL installed claude_home - guards
    detector/installer drift (a mismatch would make every --repair exit 7)."""
    claude_home = tmp_path / "claude_home"
    claude_home.mkdir()
    wiz._install_claude_extensions(claude_home, force=True)   # copies hook scripts
    wiz._install_claude_settings_json(claude_home, force=True)  # activates hooks in settings.json
    comp, line = wiz._repair_check_enforcement(claude_home)
    assert comp.state == "ok", f"expected healthy enforcement, got: {line}"
    assert line.startswith(wiz.REPAIR_OK)
