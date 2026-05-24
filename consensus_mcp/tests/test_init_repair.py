import yaml
import consensus_mcp._init_wizard as wiz
import consensus_mcp.config as cfg


def test_summary_prefixes_are_stable():
    # The skill parses these — they are a contract.
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
    # config missing/invalid is the prerequisite failure → wins over 7.
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
