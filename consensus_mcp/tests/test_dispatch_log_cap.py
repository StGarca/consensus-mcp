"""Root-fix regression: _log_dispatch must bound oversized string fields.

Field report (2026-06-04, Codex-hosted consult in a consuming project): a kimi
workdir copytree failure raised a shutil.Error whose str() embedded the entire
copied-file manifest (~188 MB). _log_dispatch wrote it verbatim, producing a
single 188 MB JSON line and a 702 MB append-only dispatch-log.jsonl across
iterations. The cap lives at the single log-writer primitive so EVERY adapter
(codex/gemini/grok/kimi) is protected, not per call-site.
"""
from __future__ import annotations

import json

from consensus_mcp import _dispatch_base


def test_log_dispatch_caps_oversized_error_field(tmp_path):
    """A multi-megabyte error string is truncated (with a marker) before write."""
    log_path = tmp_path / "dispatch-log.jsonl"
    huge = "x" * 2_000_000  # ~2 MB, as a copytree manifest str() would be

    _dispatch_base._log_dispatch(
        log_path, {"event": "dispatch_failed", "error": huge}
    )

    line = log_path.read_text(encoding="utf-8").strip()
    rec = json.loads(line)  # must remain valid JSON
    stored = rec["error"]
    assert len(stored) < 20_000, f"error field not capped: {len(stored)} chars"
    assert "truncated" in stored, "capped field must carry a truncation marker"
    assert "2000000" in stored, "marker must record the original length for debugging"


def test_log_dispatch_passes_small_fields_through_unchanged(tmp_path):
    """Normal-sized fields and non-string values are written verbatim."""
    log_path = tmp_path / "dispatch-log.jsonl"
    event = {
        "event": "dispatch_done",
        "reviewer_id": "codex-iter-1",
        "exit_code": 0,
        "timeout_seconds": 600,
        "error": "short error",
    }
    _dispatch_base._log_dispatch(log_path, event)

    rec = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert rec["reviewer_id"] == "codex-iter-1"
    assert rec["exit_code"] == 0
    assert rec["timeout_seconds"] == 600
    assert rec["error"] == "short error"
