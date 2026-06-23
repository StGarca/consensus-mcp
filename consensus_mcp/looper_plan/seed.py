"""Build adapter for the vendored Looper slice (ours, NOT upstream).

Maps a validated loop.resolved.json into Consensus Build inputs:
  - problem.md             (the seam: a Build input the architect already reads)
  - looper-suggestions.yaml (frozen gate + acceptance_gates + caps; never auto-applied)
  - looper-plan-manifest.yaml (file sha256s; powers the write-once re-coach refusal)

Verification taxonomy mapping (consult Q4): programmatic+exit_zero maps cleanly
to Build's frozen gate / acceptance_gates; other `expect` values are FLAGGED for
operator edit (never silently remapped); judge/human criteria become design
context, NOT deterministic gates. Multi-programmatic: first is the frozen gate,
the rest are acceptance_gates.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path


def resolve_goal_dir(repo_root, goal_id: str):
    """Validated goal-dir resolver (post-review rev-001). Delegates to Build's
    OWN _architect_paths.goal_dir so the looper front-door uses the EXACT goal-id
    rule (no path separators / traversal / Windows-reserved name / leading dot /
    trailing dot) and can only resolve under <repo>/.consensus/architect/.
    Raises _architect_paths.ArchitectPathError on a bad id. The wizard MUST call
    this before any mkdir/write so a malformed goal id can never escape the goal
    tree. Importing _architect_paths here is read-only reuse (single source of
    truth), not a Build modification - the Build path still never imports
    looper_plan."""
    from consensus_mcp import _architect_paths as ap
    return ap.goal_dir(repo_root, goal_id)


def _criteria(resolved: dict) -> list:
    return resolved.get("goal", {}).get("verification", [])


def render_problem_md(resolved: dict) -> str:
    """Render the coached goal + DoD + verification into a Build problem.md.
    judge/human criteria go under an explicit NON-AUTOMATION banner so the
    architect treats them as judgment, not executable gates."""
    g = resolved.get("goal", {})
    prog = [c for c in _criteria(resolved) if c["type"] == "programmatic"]
    design = [c for c in _criteria(resolved) if c["type"] in ("judge", "human")]
    lines = [
        f"# {resolved.get('meta', {}).get('name', 'Looper-coached goal')}",
        "", "## Goal", "", g.get("statement", "").strip(),
        "", "## Definition of Done", "", g.get("definition_of_done", "").strip(),
        "", "## Verification (automatable)", "",
    ]
    for c in prog:
        suffix = f" contains `{c.get('contains')}`" if c.get("expect") == "stdout_contains" else ""
        lines.append(f"- `{c['id']}`: run `{json.dumps(c['check'])}` expect `{c['expect']}`{suffix}")
    if not prog:
        lines.append("- (none - no programmatic criteria coached)")
    lines += ["",
              "## Design criteria (NON-AUTOMATION - architect/reviewer/human "
              "judgment, NOT executable gates)", ""]
    for c in design:
        if c["type"] == "judge":
            lines.append(f"- `{c['id']}` (judge rubric): {c['rubric']}")
        else:
            lines.append(f"- `{c['id']}` (human signoff): {c['prompt']}")
    if not design:
        lines.append("- (none)")
    lines += ["", "_Coached via the Looper design front-door; see `looper-plan/LOOP.md`._", ""]
    return "\n".join(lines)


def render_verification_command(check: list[str]) -> str:
    """argv -> shell string for Build's shell=True verification gate.
    POSIX: shlex.join; Windows: subprocess.list2cmdline."""
    if os.name == "nt":
        return subprocess.list2cmdline(check)
    return shlex.join(check)


def map_verification(resolved: dict) -> dict:
    """Taxonomy -> Build shapes. See module docstring for the rules."""
    prog = [c for c in _criteria(resolved) if c["type"] == "programmatic"]
    design = [c for c in _criteria(resolved) if c["type"] in ("judge", "human")]
    acceptance: list[dict] = []
    frozen = ""
    for i, c in enumerate(prog):
        clean = c.get("expect") == "exit_zero"
        cmd = render_verification_command(c["check"])
        if i == 0 and clean:
            frozen = cmd
        acceptance.append({"id": c["id"], "description": f"{c['id']} ({c.get('expect')})",
                           "check": cmd, "needs_operator_edit": not clean})
    return {"frozen_verification": frozen,
            "acceptance_gates": acceptance,
            "design_criteria": [{"id": c["id"], "type": c["type"]} for c in design]}


class ReCoachRefused(RuntimeError):
    """Raised when a looper plan would mutate a goal that Build has already begun
    sealing - the existing architect-tree recheck would later block delivery, so
    refuse early instead (root-cause-independent safeguard)."""


# Build artifacts whose presence means the goal has progressed past goal-setup.
_BUILD_PROGRESS_MARKERS = (
    "spec.yaml", "spec-approval.yaml", "architect-tree-baseline.yaml", "outcome.yaml",
)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def seal_manifest(goal_dir: Path) -> dict:
    """Record sha256 of every looper-authored file under the goal dir."""
    goal_dir = Path(goal_dir)
    targets = ["problem.md", "looper-suggestions.yaml"]
    lp = goal_dir / "looper-plan"
    if lp.is_dir():
        targets += [f"looper-plan/{p.name}" for p in sorted(lp.iterdir()) if p.is_file()]
    files = {rel: _sha256(goal_dir / rel) for rel in targets if (goal_dir / rel).is_file()}
    return {"version": 1, "files": files}


def assert_safe_to_coach(goal_dir: Path) -> None:
    """Refuse to (re-)coach once Build has begun: any supervisor seal or cycle
    dir means re-writing the looper inputs would mutate baseline-covered files
    and block delivery. Fail early with a clear message instead."""
    goal_dir = Path(goal_dir)
    for marker in _BUILD_PROGRESS_MARKERS:
        if (goal_dir / marker).exists():
            raise ReCoachRefused(
                f"{goal_dir} already has {marker}: Build has begun. Re-coaching "
                f"would mutate baseline-covered inputs and block delivery. Start a "
                f"new goal id instead.")
    if any(p.is_dir() for p in goal_dir.glob("cycle-*")):
        raise ReCoachRefused(f"{goal_dir} has cycle dirs: Build has begun.")


def seed_build_inputs(resolved: dict, goal_dir: Path) -> dict:
    """Write problem.md + looper-suggestions.yaml + looper-plan-manifest.yaml into
    the goal dir. Refuses if Build has already begun (assert_safe_to_coach)."""
    import yaml
    goal_dir = Path(goal_dir)
    goal_dir.mkdir(parents=True, exist_ok=True)
    assert_safe_to_coach(goal_dir)
    (goal_dir / "problem.md").write_text(render_problem_md(resolved), encoding="utf-8")
    suggestions = map_verification(resolved)
    ctrl = resolved.get("loop_control", {})
    suggestions["architect_loop"] = {
        "max_cycles": ctrl.get("max_iterations"),
        "max_wall_clock_minutes": (ctrl.get("budget") or {}).get("wall_clock_min", 0),
    }
    (goal_dir / "looper-suggestions.yaml").write_text(
        yaml.safe_dump(suggestions, sort_keys=True), encoding="utf-8")
    man = seal_manifest(goal_dir)
    (goal_dir / "looper-plan-manifest.yaml").write_text(
        yaml.safe_dump(man, sort_keys=True), encoding="utf-8")
    return {"problem_md": str(goal_dir / "problem.md"),
            "suggestions": str(goal_dir / "looper-suggestions.yaml"),
            "manifest": str(goal_dir / "looper-plan-manifest.yaml")}
