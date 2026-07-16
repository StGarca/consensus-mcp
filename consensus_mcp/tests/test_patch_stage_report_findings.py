"""Unit tests for tools/patch_stage_and_dry_run._read_report_findings.

A validator has already exited 0/1 (it ran) by the time its report is read, so
an unreadable or malformed report is an infrastructure failure - not a clean
run. The helper used to return an empty findings list on any parse error, which
let a corrupt report masquerade as "no findings" and silently pass a dry-run
that should have surfaced the problem. These tests pin the propagation contract:
a genuine parse/shape error is reported - and so is a MISSING report (the
validator was invoked with --out, so an absent artifact is an infra failure,
not a clean run; deep-audit codex follow-up, iter eb8af083). Only a present
but empty report stays quiet.
"""
from __future__ import annotations

import types

from pathlib import Path

import pytest

from consensus_mcp.tools import patch_stage_and_dry_run as psd


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "report.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_valid_report_returns_findings_no_error(tmp_path):
    report = _write(
        tmp_path,
        "findings:\n"
        "  - id: A\n"
        "    severity: high\n"
        "  - id: B\n"
        "    severity: medium\n",
    )
    findings, err = psd._read_report_findings(report)
    assert err is None
    assert [f["id"] for f in findings] == ["A", "B"]


def test_missing_report_surfaces_error(tmp_path):
    # Fail-closed: the validator exited 0/1, so a report that never appeared
    # must not masquerade as "no findings".
    findings, err = psd._read_report_findings(tmp_path / "does-not-exist.yaml")
    assert findings == []
    assert err is not None
    assert "missing" in err


def test_empty_report_is_not_an_error(tmp_path):
    findings, err = psd._read_report_findings(_write(tmp_path, ""))
    assert findings == []
    assert err is None


def test_corrupt_yaml_report_surfaces_error(tmp_path):
    findings, err = psd._read_report_findings(_write(tmp_path, "findings: [oops: :\n"))
    assert findings == []
    assert err is not None
    assert "report.yaml" in err


def test_non_mapping_report_surfaces_error(tmp_path):
    findings, err = psd._read_report_findings(_write(tmp_path, "- just\n- a\n- list\n"))
    assert findings == []
    assert err is not None
    assert "not a YAML mapping" in err


def test_non_list_findings_field_surfaces_error(tmp_path):
    findings, err = psd._read_report_findings(_write(tmp_path, "findings: not-a-list\n"))
    assert findings == []
    assert err is not None
    assert "non-list" in err


@pytest.mark.parametrize("returncode", [0, 1])
def test_run_validator_missing_report_surfaces_error(tmp_path, monkeypatch, returncode):
    """End-to-end regression: a validator that exits 0 OR 1 (both accepted
    exit codes) without writing its --out report must surface an
    infrastructure error, not read as clean (codex-postreview rev-002)."""
    monkeypatch.setattr(psd, "_validator_scripts", lambda: {"v1": "validate_v1.py"})
    monkeypatch.setattr(
        psd.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=returncode, stderr="", stdout=""),
    )
    findings, err = psd._run_validator("v1", [], tmp_path / "report.yaml")
    assert findings == []
    assert err is not None
    assert "report unreadable" in err
    assert "missing" in err
