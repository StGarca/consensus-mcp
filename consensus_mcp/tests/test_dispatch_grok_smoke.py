"""Env-gated integration smoke for the grok dispatcher (v1.31.0 G8 gate).

Runs the REAL grok CLI end-to-end against a minimal goal_packet +
review-packet and asserts the dispatcher seals a parseable verdict
YAML. Active only when `CONSENSUS_MCP_RUN_REAL_GROK_SMOKE=1` is set in
the environment — same pattern as the gemini/codex smokes.

Failure modes this smoke catches:
  - grok auth-flow regression (pre-flight passes but CLI errors on
    expired token) — codex Q1 refuting observation
  - grok flag rename across versions (R1)
  - prompt-file write-then-read FS race
  - grok respects --no-subagents / --disable-web-search etc (interlock
    integrity — codex D8 / independent_safeguard)

The pure-code unit tests are in test_dispatch_grok.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


pytestmark = pytest.mark.skipif(
    os.environ.get("CONSENSUS_MCP_RUN_REAL_GROK_SMOKE") != "1",
    reason="Env-gated: set CONSENSUS_MCP_RUN_REAL_GROK_SMOKE=1 to run.",
)


def _minimal_goal_packet(tmp_path: Path) -> Path:
    p = tmp_path / "goal_packet.yaml"
    p.write_text(
        """schema_version: 1
pilot_id: grok-smoke
goal:
  summary: "Smoke test — confirm the grok dispatcher seals a parseable verdict."
  desired_end_state: |
    Emit a minimal valid review JSON with findings:[] and
    goal_satisfied:true.
  non_goals: []
allowed_files: []
acceptance_gates:
  - id: smoke
    description: smoke
    check: 'true'
stop_conditions: []
operator_escalation_triggers: []
authorization:
  authorized_by: smoke
  authorized_at_utc: '2026-05-26T00:00:00Z'
""",
        encoding="utf-8",
    )
    return p


def test_grok_smoke_seals_verdict(tmp_path):
    """End-to-end smoke: real grok CLI must produce a parseable verdict YAML.

    Acceptance: dispatcher rc=0 AND iteration_dir/grok-review.yaml exists
    AND contains goal_satisfied + findings keys AND provenance has
    grok_version + prompt_sha256 + disabled_tools.
    """
    iter_dir = tmp_path / "iteration-grok-smoke"
    iter_dir.mkdir()
    gp = _minimal_goal_packet(tmp_path)

    # Pre-flight: caller must already have ~/.grok/auth.json (smoke env
    # opt-in implies the operator authenticated grok).
    auth_path = Path.home() / ".grok" / "auth.json"
    if not auth_path.exists():
        pytest.skip(f"grok not authenticated ({auth_path} absent); run `grok login`")

    env = dict(os.environ)
    env["CONSENSUS_MCP_REPO_ROOT"] = str(tmp_path)
    (tmp_path / ".git").mkdir()  # repo marker so _resolve_repo_root passes

    result = subprocess.run(
        [
            sys.executable, "-m", "consensus_mcp._dispatch_grok",
            "--goal-packet", str(gp),
            "--iteration-dir", str(iter_dir),
            "--smoke",
            "--timeout-seconds", "300",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=360,
    )

    assert result.returncode == 0, (
        f"dispatcher rc={result.returncode}; stderr_tail={result.stderr[-2000:]!r}; "
        f"stdout_tail={result.stdout[-500:]!r}"
    )
    parsed = json.loads(result.stdout.strip())
    assert parsed["ok"] is True, parsed

    sealed_path = Path(parsed["sealed_path"])
    assert sealed_path.exists()
    import yaml
    sealed = yaml.safe_load(sealed_path.read_text(encoding="utf-8"))
    assert "goal_satisfied" in sealed
    assert "findings" in sealed
    assert "blocking_objections" in sealed

    # Provenance acceptance gate (G6).
    prov = sealed.get("dispatch_provenance", {})
    assert prov.get("grok_version") and prov["grok_version"] != "unknown", prov
    assert prov.get("prompt_sha256"), prov
    assert prov.get("adapter") == "grok"
    assert isinstance(prov.get("disabled_tools"), list), prov
    assert "--no-subagents" in prov["disabled_tools"]
    assert "--disable-web-search" in prov["disabled_tools"]
