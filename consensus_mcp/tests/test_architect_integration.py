"""End-to-end: needs_spec -> awaiting_delivery_approval with a stub builder
process. Only the process boundary inside _dispatch_builder is faked.

Plan deviation (as-landed Task 8): _dispatch_builder spawns the builder via
subprocess.Popen in its own process group + communicate (timeout paths kill
the whole tree); subprocess.run is never called there. The plan's
db.subprocess.run stub therefore lands on db.subprocess.Popen - the same
boundary, nothing higher. argv construction, canon validation, out-file
handling, and output parsing all run for real.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from consensus_mcp import _architect_paths as ap
from consensus_mcp import _dispatch_builder as db
from consensus_mcp.tools import architect_gates as gates
from consensus_mcp.tools import architect_loop_step as als


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text(".consensus/\n", encoding="utf-8")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True,
                   capture_output=True)
    (repo / ".consensus").mkdir()
    (repo / ".consensus" / "config.yaml").write_text(yaml.safe_dump({
        "workflow": {"mode": "architect-build"},
        "contributors": {"enabled": ["claude", "codex"]},
        "roles": {"architect": "claude", "builder": "codex", "reviewer": "codex"},
        "architect_loop": {"max_cycles": 3, "verification": "",
                           "lane_branch_prefix": "arch-lane/",
                           "max_wall_clock_minutes": 0},
    }), encoding="utf-8")
    return repo


def test_full_goal_lifecycle_with_stub_builder(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path)
    goal = ap.goal_dir(repo, "g1")
    goal.mkdir(parents=True)
    (goal / ap.PROBLEM_FILENAME).write_text("add module m\n", encoding="utf-8")

    real_popen = subprocess.Popen

    def stub_codex_popen(argv, **kwargs):
        # db.subprocess IS the shared subprocess module, and subprocess.run
        # spawns through Popen internally - the supervisor's real git
        # processes (lane worktree, commits, rev-parse) must pass through
        # untouched. Only the codex builder spawn is faked.
        if Path(argv[0]).stem != "codex":
            return real_popen(argv, **kwargs)
        # The stub IS the builder: write a file into the --cd lane, then
        # emit canonical JSON to the -o path.
        lane = Path(argv[argv.index("--cd") + 1])
        (lane / "m.py").write_text("def m():\n    return 42\n", encoding="utf-8")
        out = Path(argv[argv.index("-o") + 1])
        out.write_text(json.dumps(
            {"summary": "added m.py", "pushback": None, "notes": ""}
        ), encoding="utf-8")

        class R:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                return b"", b""

        return R()

    monkeypatch.setattr(db.subprocess, "Popen", stub_codex_popen)

    cfg_path = str(repo / ".consensus" / "config.yaml")
    step = lambda: als.handle(goal_dir=str(goal), config_path=cfg_path)

    assert step()["state"] == "needs_spec"
    ap.seal_artifact(ap.spec_path(goal), {"kind": "spec", "body": "create m.py"})
    assert step()["state"] == "awaiting_spec_approval"
    assert gates.handle_approve_spec(
        goal_dir=str(goal), approver="op", repo_root=str(repo)
    )["ok"]
    assert step()["state"] == "built"
    build = yaml.safe_load(
        (ap.cycle_dir(goal, 1) / ap.BUILD_RESULT_FILENAME).read_text(encoding="utf-8")
    )
    assert (ap.lane_dir(goal) / "m.py").exists()
    assert step()["state"] == "needs_review"
    ap.seal_artifact(ap.cycle_dir(goal, 1) / ap.REVIEW_FILENAME,
                     {"verdict": "lgtm", "lane_head_sha": build["lane_head_sha"]})
    assert step()["state"] == "needs_ruling"
    ap.seal_artifact(ap.cycle_dir(goal, 1) / ap.RULING_FILENAME,
                     {"disposition": "accept",
                      "lane_head_sha": build["lane_head_sha"]})
    final = step()
    assert final["state"] == "awaiting_delivery_approval"
    handoff = (goal / ap.HANDOFF_FILENAME).read_text(encoding="utf-8")
    assert "added m.py" in handoff
