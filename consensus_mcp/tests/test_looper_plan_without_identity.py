"""Zero-diff / byte-identity guards (Task 9): the Build path must never reference
or import the looper_plan package. The looper front-door lives only at
goal-setup; the supervisor is untouched."""
import importlib
import sys
from pathlib import Path

import pytest

_SUPERVISOR_SOURCES = [
    "consensus_mcp/_architect_lane.py",
    "consensus_mcp/_architect_handoff.py",
    "consensus_mcp/_architect_paths.py",
    "consensus_mcp/_dispatch_builder.py",
    "consensus_mcp/tools/architect_loop_step.py",
]


@pytest.mark.parametrize("rel", _SUPERVISOR_SOURCES)
def test_supervisor_source_has_no_looper_reference(rel):
    src = Path(rel).read_text(encoding="utf-8")
    assert "looper" not in src.lower(), f"{rel} must have zero looper references (zero-diff)"


def test_fresh_loop_step_import_pulls_no_looper_plan():
    # Force a fresh import of the supervisor and assert its import graph never
    # pulls in looper_plan (lazy boundary).
    for m in [m for m in sys.modules if m.startswith("consensus_mcp.looper_plan")
              or m == "consensus_mcp.tools.architect_loop_step"]:
        del sys.modules[m]
    importlib.import_module("consensus_mcp.tools.architect_loop_step")
    assert not any(m.startswith("consensus_mcp.looper_plan") for m in sys.modules), \
        "architect.loop_step import must not pull in looper_plan"
