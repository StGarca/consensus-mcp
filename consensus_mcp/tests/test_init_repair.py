import consensus_mcp._init_wizard as wiz


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
