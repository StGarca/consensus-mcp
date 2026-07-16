"""Unit tests for tools/patch_stage_and_dry_run._read_report_findings.

A validator has already exited 0/1 (it ran) by the time its report is read, so
an unreadable or malformed report is an infrastructure failure - not a clean
run. The helper used to return an empty findings list on any parse error, which
let a corrupt report masquerade as "no findings" and silently pass a dry-run
that should have surfaced the problem. These tests pin the propagation contract:
a genuine parse/shape error is reported, while the benign cases stay quiet.
"""
from __future__ import annotations

from pathlib import Path

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


def test_missing_report_is_not_an_error(tmp_path):
    findings, err = psd._read_report_findings(tmp_path / "does-not-exist.yaml")
    assert findings == []
    assert err is None


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
