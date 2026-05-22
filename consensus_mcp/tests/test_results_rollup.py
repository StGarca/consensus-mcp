"""Tests for consensus_mcp._results_rollup — the read-only project scorecard.

The rollup reads the project ledger consensus-state/state/results-v1.jsonl
(one JSON object per line, each conforming to schemas/results-v1.schema.json)
and aggregates it into a PROJECT SCORECARD.

Design contract (docs/design-consults/v1.19.0-result-logging.md):
  - findings by severity, validated vs dismissed_refuted vs deferred vs open,
    fixes_applied, iteration count, convergence rate, date span.
  - SKIP records whose consensus_results_schema_version != 1 with a warning.
  - SEGREGATE backfilled records (confidence == "best-effort" / backfilled
    == true): reported in a SEPARATE section, never folded into authoritative
    (forward) totals.
  - Human-readable table (string) + machine-readable dict (for --json).
  - Ledger path resolves via CONSENSUS_MCP_STATE_ROOT or cwd/consensus-state.
  - main(argv): `results` prints the table; `results --json` prints JSON.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from consensus_mcp import _results_rollup


# ---------------------------------------------------------------------------
# Fixture ledger
# ---------------------------------------------------------------------------

# Forward (authoritative) records: 3 iterations.
#   iter-A: converged. 2 findings — 1 high validated_fixed (1 fix, 2 files),
#           1 medium dismissed_refuted.
#   iter-B: NOT converged. 2 findings — 1 critical deferred, 1 low open.
#   iter-C: converged. 1 finding — 1 blocking validated_fixed (1 fix, 1 file).
# Backfilled (best-effort) record: 1 iteration, 1 high finding — MUST be
#   segregated, never folded into the forward totals.
# Unknown-schema record: schema_version == 2 — MUST be skipped with a warning.

_FORWARD_RECORDS = [
    {
        "consensus_results_schema_version": 1,
        "iteration_id": "iter-A",
        "run_id": "run-A",
        "record_updated_utc": "2026-05-01T00:00:00Z",
        "sealed_at_utc": "2026-05-01T00:00:00Z",
        "convergence": {"rule": "unanimous", "converged": True},
        "findings": [
            {
                "id": "A-1",
                "severity": "high",
                "disposition": "validated_fixed",
                "fix": {"patch_id": "p1", "files": ["a.py", "b.py"]},
            },
            {
                "id": "A-2",
                "severity": "medium",
                "disposition": "dismissed_refuted",
                "evidence_ref": "deadbeef",
            },
        ],
        "counts": {
            "by_severity": {"high": 1, "medium": 1},
            "validated": 1,
            "dismissed": 1,
            "deferred": 0,
            "open": 0,
            "fixes_applied": 1,
        },
    },
    {
        "consensus_results_schema_version": 1,
        "iteration_id": "iter-B",
        "run_id": "run-B",
        "record_updated_utc": "2026-05-10T00:00:00Z",
        "sealed_at_utc": "2026-05-10T00:00:00Z",
        "convergence": {"rule": "unanimous", "converged": False},
        "findings": [
            {"id": "B-1", "severity": "critical", "disposition": "deferred"},
            {"id": "B-2", "severity": "low", "disposition": "open"},
        ],
        "counts": {
            "by_severity": {"critical": 1, "low": 1},
            "validated": 0,
            "dismissed": 0,
            "deferred": 1,
            "open": 1,
            "fixes_applied": 0,
        },
    },
    {
        "consensus_results_schema_version": 1,
        "iteration_id": "iter-C",
        "run_id": "run-C",
        "record_updated_utc": "2026-05-20T00:00:00Z",
        "sealed_at_utc": "2026-05-20T00:00:00Z",
        "convergence": {"rule": "unanimous", "converged": True},
        "findings": [
            {
                "id": "C-1",
                "severity": "blocking",
                "disposition": "validated_fixed",
                "fix": {"patch_id": "p2", "files": ["c.py"]},
            },
        ],
        "counts": {
            "by_severity": {"blocking": 1},
            "validated": 1,
            "dismissed": 0,
            "deferred": 0,
            "open": 0,
            "fixes_applied": 1,
        },
    },
]

_BACKFILLED_RECORD = {
    "consensus_results_schema_version": 1,
    "iteration_id": "iter-OLD",
    "run_id": None,
    "record_updated_utc": "2025-01-01T00:00:00Z",
    "sealed_at_utc": "2025-01-01T00:00:00Z",
    "convergence": {"rule": None, "converged": None},
    "findings": [
        {"id": "OLD-1", "severity": "high", "disposition": "open"},
    ],
    "counts": {
        "by_severity": {"high": 1},
        "validated": 0,
        "dismissed": 0,
        "deferred": 0,
        "open": 1,
        "fixes_applied": 0,
    },
    "backfilled": True,
    "confidence": "best-effort",
}

_UNKNOWN_SCHEMA_RECORD = {
    "consensus_results_schema_version": 2,
    "iteration_id": "iter-FUTURE",
    "findings": [],
    "counts": {},
}


def _write_ledger(state_root: Path, records: list[dict]) -> Path:
    ledger = state_root / "state" / "results-v1.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return ledger


@pytest.fixture()
def ledger_root(tmp_path: Path) -> Path:
    """A state root whose ledger contains forward + backfilled + unknown lines."""
    all_records = list(_FORWARD_RECORDS) + [_BACKFILLED_RECORD, _UNKNOWN_SCHEMA_RECORD]
    _write_ledger(tmp_path, all_records)
    return tmp_path


# ---------------------------------------------------------------------------
# Scorecard aggregation
# ---------------------------------------------------------------------------

def test_authoritative_totals_match_hand_counts(ledger_root: Path):
    card = _results_rollup.build_scorecard(state_root=ledger_root)
    auth = card["authoritative"]

    # 3 forward iterations.
    assert auth["iterations"] == 3

    # by_severity over forward records only:
    #   high 1 (iter-A), medium 1 (iter-A), critical 1 (iter-B),
    #   low 1 (iter-B), blocking 1 (iter-C).
    assert auth["by_severity"] == {
        "high": 1,
        "medium": 1,
        "critical": 1,
        "low": 1,
        "blocking": 1,
    }
    assert auth["total_findings"] == 5

    # dispositions: validated 2 (A-1, C-1), dismissed 1 (A-2),
    # deferred 1 (B-1), open 1 (B-2).
    assert auth["validated"] == 2
    assert auth["dismissed_refuted"] == 1
    assert auth["deferred"] == 1
    assert auth["open"] == 1

    # fixes applied: iter-A 1 + iter-C 1 = 2.
    assert auth["fixes_applied"] == 2

    # convergence rate: 2 of 3 converged.
    assert auth["converged"] == 2
    assert auth["convergence_rate"] == pytest.approx(2 / 3)

    # date span over forward records' record_updated_utc.
    assert auth["date_span"]["first"] == "2026-05-01T00:00:00Z"
    assert auth["date_span"]["last"] == "2026-05-20T00:00:00Z"


def test_backfilled_record_is_segregated(ledger_root: Path):
    card = _results_rollup.build_scorecard(state_root=ledger_root)
    auth = card["authoritative"]
    backfilled = card["backfilled"]

    # The backfilled iter-OLD must NOT inflate the forward totals.
    assert auth["iterations"] == 3
    assert "high" in auth["by_severity"]
    assert auth["by_severity"]["high"] == 1  # only iter-A, NOT iter-OLD
    assert auth["open"] == 1  # only B-2, NOT OLD-1

    # The backfilled section reports it separately.
    assert backfilled["iterations"] == 1
    assert backfilled["total_findings"] == 1
    assert backfilled["by_severity"] == {"high": 1}
    assert backfilled["open"] == 1


def test_unknown_schema_version_skipped_with_warning(ledger_root: Path, capsys):
    card = _results_rollup.build_scorecard(state_root=ledger_root)

    # iter-FUTURE (schema_version 2) must not appear anywhere in totals.
    assert card["authoritative"]["iterations"] == 3
    assert card["backfilled"]["iterations"] == 1
    assert card["skipped_unknown_schema"] == 1

    # A warning is surfaced (stderr), not silently swallowed.
    captured = capsys.readouterr()
    assert "schema" in captured.err.lower()
    assert "iter-FUTURE" in captured.err or "2" in captured.err


def test_empty_or_missing_ledger(tmp_path: Path):
    # No ledger file at all → empty scorecard, no crash.
    card = _results_rollup.build_scorecard(state_root=tmp_path)
    assert card["authoritative"]["iterations"] == 0
    assert card["authoritative"]["total_findings"] == 0
    assert card["backfilled"]["iterations"] == 0


# ---------------------------------------------------------------------------
# Human-readable table
# ---------------------------------------------------------------------------

def test_human_table_is_string_and_mentions_key_figures(ledger_root: Path):
    card = _results_rollup.build_scorecard(state_root=ledger_root)
    table = _results_rollup.render_table(card)
    assert isinstance(table, str)
    assert table  # non-empty
    # Mentions the authoritative iteration count and the backfilled section.
    assert "3" in table
    assert "backfill" in table.lower()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def test_loader_honors_state_root_env(ledger_root: Path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(ledger_root))
    # No explicit state_root arg → must fall back to the env var.
    card = _results_rollup.build_scorecard()
    assert card["authoritative"]["iterations"] == 3


# ---------------------------------------------------------------------------
# main(argv) / console-script
# ---------------------------------------------------------------------------

def test_main_human_table(ledger_root: Path, monkeypatch, capsys):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(ledger_root))
    rc = _results_rollup.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "3" in out
    assert "backfill" in out.lower()


def test_main_json_is_valid_machine_readable(ledger_root: Path, monkeypatch, capsys):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(ledger_root))
    rc = _results_rollup.main(["--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)  # must be valid JSON
    assert parsed["authoritative"]["iterations"] == 3
    assert parsed["authoritative"]["fixes_applied"] == 2
    assert parsed["backfilled"]["iterations"] == 1


def test_main_strips_leading_results_subcommand(ledger_root: Path, monkeypatch, capsys):
    """`consensus results --json` routes here with argv == ['results', '--json'];
    a leading 'results' token must be stripped (mirrors _init_wizard's 'init')."""
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(ledger_root))
    rc = _results_rollup.main(["results", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["authoritative"]["iterations"] == 3


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------

def test_mcp_tool_returns_scorecard_dict(ledger_root: Path, monkeypatch):
    monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(ledger_root))
    from consensus_mcp.tools import results as results_tool

    result = results_tool.handle()
    assert result["authoritative"]["iterations"] == 3
    assert result["backfilled"]["iterations"] == 1
    assert result["skipped_unknown_schema"] == 1


def test_server_registers_results_tool():
    """server.py must register the read-only results tool."""
    import importlib

    import consensus_mcp.server as server
    importlib.reload(server)
    names = {t["name"] for t in server.registry.list_tools()}
    assert "consensus.results" in names


def test_console_script_entry_point_importable():
    """The console-script target consensus_mcp._results_rollup:main must exist."""
    assert callable(_results_rollup.main)
