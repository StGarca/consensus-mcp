from consensus_mcp import _init_wizard as wizard
from consensus_mcp import _results_rollup as results


def test_consensus_results_forwards_remaining_arguments(monkeypatch):
    seen = []
    monkeypatch.setattr(results, "main", lambda argv: seen.append(argv) or 17)

    assert wizard.main(["results", "--json", "--record-gold", "iter", "row"]) == 17
    assert seen == [["--json", "--record-gold", "iter", "row"]]


def test_consensus_init_and_bare_command_still_run_wizard(monkeypatch):
    seen = []
    monkeypatch.setattr(wizard, "cmd_init", lambda args: seen.append(args) or 23)

    assert wizard.main(["init", "--non-interactive"]) == 23
    assert wizard.main([]) == 23
    assert len(seen) == 2
