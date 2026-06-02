# v1.30.6 Synthesis-Aware Propose-Converge - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make propose-converge refuse to silently never-converge on a plan deliverable - the autonomous engine (Path B) fails loud when `convergence.requires_synthesis` is declared, and a thin host-driven Path A helper converges on (and seals) ONE synthesized plan.

**Architecture:** Synthesis is a host judgment act; the autonomous engine has no host in the loop, so it must not fake it. (1) An operator-declared goal_packet flag `convergence.requires_synthesis`. (2) A guard in `_run_workflow_4` that raises `WorkflowError` (-> `outcome.error`) when the flag is set. (3) A max-rounds hint for the undeclared case. (4) Two thin engine methods for Path A: `evaluate_plan_convergence` (delegates to `_evaluate_convergence`) and `seal_plan_iteration` (on converge, writes `iteration-outcome.yaml` with a sealed `closing_state` and returns the host's plan path - never overwriting it).

**Tech Stack:** Python 3.13, pytest (pipx venv: `~/.local/share/pipx/venvs/consensus-mcp/bin/python -m pytest`), PyYAML.

**Spec:** `docs/superpowers/specs/2026-05-24-v1306-synthesis-step-design.md`

---

## File Structure

- **Modify** `consensus_mcp/workflow_engine.py`
  - add `_requires_synthesis(self, goal_packet_path) -> bool` (reader; mirrors `_goal_risk_class`)
  - guard at the top of `_run_workflow_4` (raises `WorkflowError`)
  - max-rounds hint in `_run_workflow_4`'s final `outcome.error`
  - add `evaluate_plan_convergence(self, review_artifacts, outcome, eligible_voters=None) -> ConvergenceOutcome`
  - add `seal_plan_iteration(self, iteration_dir, plan_path, conv, round_number) -> Path | None`
- **Modify** `consensus_mcp/goal_packet_schema.yaml` (document the field in the template)
- **Create** `consensus_mcp/tests/test_workflow_v1306_synthesis.py` (all new tests)
- **Create** `docs/workflows/path-a-plan-convergence.md` (the Path A flow)
- **Modify** `CHANGELOG.md`, `pyproject.toml` (release)

Run the suite with: `~/.local/share/pipx/venvs/consensus-mcp/bin/python -m pytest <path> -v`

---

## Task 1: Operator-declared synthesis flag reader

**Files:**
- Modify: `consensus_mcp/workflow_engine.py` (add method next to `_goal_risk_class`, ~`:561`)
- Test: `consensus_mcp/tests/test_workflow_v1306_synthesis.py`

- [ ] **Step 1: Write the failing test**

```python
"""v1.30.6 - synthesis-aware propose-converge (Path B guard + Path A helper)."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from consensus_mcp import config as cfg
from consensus_mcp.contributors.base import (
    FakeAlwaysApprove, FakeAlwaysBlock, SealedArtifact,
)
from consensus_mcp.workflow_engine import (
    ConvergenceOutcome, WorkflowEngine,
)


def _config(mode=cfg.WORKFLOW_PROPOSE_CONVERGE, rule=cfg.CONVERGE_STRICT_MAJ) -> dict:
    c = deepcopy(cfg.default_config())
    c["contributors"]["enabled"] = ["claude", "codex", "gemini"]
    c["workflow"]["mode"] = mode
    c["convergence"]["rule"] = rule
    c["convergence"]["finding_disposition"] = cfg.DISPOSITION_ALL_OR_NOTHING
    cfg.validate(c)
    return c


def _engine(tmp_path, approve=True) -> WorkflowEngine:
    fake = FakeAlwaysApprove() if approve else FakeAlwaysBlock()
    adapters = {n: (FakeAlwaysApprove() if approve else FakeAlwaysBlock())
                for n in ["claude", "codex", "gemini"]}
    return WorkflowEngine(_config(), adapters, tmp_path)


def _goal(tmp_path, body: str) -> Path:
    g = tmp_path / "goal_packet.yaml"
    g.write_text(body, encoding="utf-8")
    return g


def test_requires_synthesis_true_when_declared(tmp_path):
    eng = _engine(tmp_path)
    g = _goal(tmp_path, "convergence:\n  requires_synthesis: true\n")
    assert eng._requires_synthesis(g) is True


def test_requires_synthesis_false_when_absent(tmp_path):
    eng = _engine(tmp_path)
    g = _goal(tmp_path, "goal:\n  summary: x\n")
    assert eng._requires_synthesis(g) is False


def test_requires_synthesis_false_on_nonbool_or_unreadable(tmp_path):
    eng = _engine(tmp_path)
    g = _goal(tmp_path, "convergence:\n  requires_synthesis: maybe\n")
    assert eng._requires_synthesis(g) is False
    assert eng._requires_synthesis(tmp_path / "nope.yaml") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.local/share/pipx/venvs/consensus-mcp/bin/python -m pytest consensus_mcp/tests/test_workflow_v1306_synthesis.py -v`
Expected: FAIL - `AttributeError: 'WorkflowEngine' object has no attribute '_requires_synthesis'`

- [ ] **Step 3: Add the reader (after `_goal_risk_class`, ~line 561)**

```python
    def _requires_synthesis(self, goal_packet_path: Path) -> bool:
        """Operator-DECLARED: does convergence require ONE merged artifact (a plan)?
        No inference (heuristics are the shared-prior trap). True only for an explicit
        boolean `convergence.requires_synthesis: true`."""
        try:
            gp = yaml.safe_load(Path(goal_packet_path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return False
        if not isinstance(gp, dict):
            return False
        conv = gp.get("convergence")
        return isinstance(conv, dict) and conv.get("requires_synthesis") is True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.local/share/pipx/venvs/consensus-mcp/bin/python -m pytest consensus_mcp/tests/test_workflow_v1306_synthesis.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/workflow_engine.py consensus_mcp/tests/test_workflow_v1306_synthesis.py
git commit -m "feat(v1.30.6): operator-declared convergence.requires_synthesis reader"
```

---

## Task 2: Path B guard - fail loud, never silently never-converge

**Files:**
- Modify: `consensus_mcp/workflow_engine.py` (`_run_workflow_4`, top of the method ~`:246`; final `outcome.error` ~`:311`)
- Test: `consensus_mcp/tests/test_workflow_v1306_synthesis.py`

- [ ] **Step 1: Write the failing tests (append to the test file)**

```python
def test_path_b_fails_loud_on_requires_synthesis(tmp_path):
    eng = _engine(tmp_path)
    iter_dir = tmp_path / "iter-synth"; iter_dir.mkdir()
    g = _goal(iter_dir, "convergence:\n  requires_synthesis: true\n")
    target = iter_dir / "problem.yaml"; target.write_text("schema_version: 1\n", encoding="utf-8")
    outcome = eng.run_iteration(iter_dir, g, target)
    assert outcome.error is not None
    assert "requires_synthesis" in outcome.error
    assert "Path A" in outcome.error
    # it must NOT have entered the bundle-vote loop: no convergence-packet written
    assert not list(iter_dir.glob("convergence-packet-round-*.yaml"))


def test_path_b_unchanged_without_flag(tmp_path):
    # No flag -> normal propose-converge runs (FakeAlwaysApprove -> converges).
    eng = _engine(tmp_path, approve=True)
    iter_dir = tmp_path / "iter-normal"; iter_dir.mkdir()
    g = _goal(iter_dir, "goal:\n  summary: agree on approach\n")
    target = iter_dir / "problem.yaml"; target.write_text("schema_version: 1\n", encoding="utf-8")
    outcome = eng.run_iteration(iter_dir, g, target)
    assert outcome.error is None
    assert outcome.convergence is not None and outcome.convergence.converged
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.local/share/pipx/venvs/consensus-mcp/bin/python -m pytest consensus_mcp/tests/test_workflow_v1306_synthesis.py -v -k path_b`
Expected: `test_path_b_fails_loud_on_requires_synthesis` FAILS (no guard yet -> it runs the loop / converges, so `outcome.error` is None).

- [ ] **Step 3: Add the guard at the TOP of `_run_workflow_4`**

Immediately after the docstring / before `enabled = self.config["contributors"]["enabled"]` (~line 246), insert:

```python
        # v1.30.6: a plan/synthesis-deliverable consult cannot converge in the autonomous
        # engine - there is no host in the loop to merge proposals into ONE plan and revise
        # it. Fail LOUD here rather than bundle-vote forever. Plan consults use Path A.
        if self._requires_synthesis(goal_packet_path):
            raise WorkflowError(
                "this consult declares convergence.requires_synthesis: the deliverable is a "
                "single synthesized plan, which the autonomous engine (Path B / run_iteration) "
                "cannot author - there is no host in the loop. Converge it via Path A: author "
                "converged-plan.yaml, dispatch contributors to review THAT plan "
                "(--review-target converged-plan.yaml), then evaluate_plan_convergence + "
                "seal_plan_iteration. See docs/workflows/path-a-plan-convergence.md."
            )
```

- [ ] **Step 4: Add the undeclared safety-net hint to the final `outcome.error`**

Replace the existing max-rounds error (~`:311`):

```python
        outcome.error = (
            f"workflow #4: convergence not reached after {max_rounds} rounds"
        )
```

with:

```python
        outcome.error = (
            f"workflow #4: convergence not reached after {max_rounds} rounds. "
            "If the deliverable is a single synthesized artifact (e.g. a plan), the "
            "bundle-vote cannot converge - declare convergence.requires_synthesis and use "
            "the Path A flow (docs/workflows/path-a-plan-convergence.md)."
        )
```

- [ ] **Step 5: Run to verify pass**

Run: `~/.local/share/pipx/venvs/consensus-mcp/bin/python -m pytest consensus_mcp/tests/test_workflow_v1306_synthesis.py -v -k path_b`
Expected: PASS (both)

- [ ] **Step 6: Commit**

```bash
git add consensus_mcp/workflow_engine.py consensus_mcp/tests/test_workflow_v1306_synthesis.py
git commit -m "feat(v1.30.6): Path B fails loud on requires_synthesis + max-rounds hint"
```

---

## Task 3: Path A helper - evaluate + seal the synthesized plan

**Files:**
- Modify: `consensus_mcp/workflow_engine.py` (add two methods after `_seal_converged_plan`)
- Test: `consensus_mcp/tests/test_workflow_v1306_synthesis.py`

- [ ] **Step 1: Write the failing tests (append)**

```python
def _review_artifact(contributor: str, *, satisfied: bool, blocking=None) -> SealedArtifact:
    return SealedArtifact(
        contributor=contributor, phase="converge",
        pass_id=f"{contributor}-pass1", sealed_path=Path(f"/tmp/{contributor}-review.yaml"),
        archive_sealed_path=None, packet_sha256="",
        parsed={"goal_satisfied": satisfied, "blocking_objections": blocking or []},
    )


def test_evaluate_plan_convergence_clean_converges(tmp_path):
    eng = _engine(tmp_path)
    from consensus_mcp.workflow_engine import IterationOutcome
    outcome = IterationOutcome(iteration_id="i", workflow_mode=cfg.WORKFLOW_PROPOSE_CONVERGE,
                               effective_config_path=tmp_path / "ec.yaml")
    arts = [_review_artifact(c, satisfied=True) for c in ["claude", "codex", "gemini"]]
    for a in arts:
        outcome.contributor_artifacts.setdefault(a.contributor, []).append(a)
    conv = eng.evaluate_plan_convergence(arts, outcome)
    assert isinstance(conv, ConvergenceOutcome) and conv.converged


def test_evaluate_plan_convergence_block_does_not_converge(tmp_path):
    eng = _engine(tmp_path)
    from consensus_mcp.workflow_engine import IterationOutcome
    outcome = IterationOutcome(iteration_id="i", workflow_mode=cfg.WORKFLOW_PROPOSE_CONVERGE,
                               effective_config_path=tmp_path / "ec.yaml")
    arts = [_review_artifact("claude", satisfied=True),
            _review_artifact("codex", satisfied=False, blocking=["codex-b1"]),
            _review_artifact("gemini", satisfied=False, blocking=["gemini-b1"])]
    for a in arts:
        outcome.contributor_artifacts.setdefault(a.contributor, []).append(a)
    conv = eng.evaluate_plan_convergence(arts, outcome)
    assert not conv.converged


def test_seal_plan_iteration_seals_THE_PLAN_not_a_bundle(tmp_path):
    eng = _engine(tmp_path)
    from consensus_mcp.workflow_engine import IterationOutcome
    iter_dir = tmp_path / "iter-plan"; iter_dir.mkdir()
    plan = iter_dir / "converged-plan.yaml"
    plan.write_text("decision:\n  do: ship the thing\nfeasibility: {a: ok}\n", encoding="utf-8")
    plan_before = plan.read_text(encoding="utf-8")
    outcome = IterationOutcome(iteration_id=iter_dir.name, workflow_mode=cfg.WORKFLOW_PROPOSE_CONVERGE,
                               effective_config_path=tmp_path / "ec.yaml")
    arts = [_review_artifact(c, satisfied=True) for c in ["claude", "codex", "gemini"]]
    for a in arts:
        outcome.contributor_artifacts.setdefault(a.contributor, []).append(a)
    conv = eng.evaluate_plan_convergence(arts, outcome)

    sealed = eng.seal_plan_iteration(iter_dir, plan, conv, round_number=1)
    assert sealed == plan                                  # the PLAN is the sealed artifact
    assert plan.read_text(encoding="utf-8") == plan_before  # NOT overwritten with a summary
    oc = yaml.safe_load((iter_dir / "iteration-outcome.yaml").read_text(encoding="utf-8"))
    from consensus_mcp._delivery_readiness import SEALED_CLOSING_STATES
    assert oc["closing_state"] in SEALED_CLOSING_STATES    # mintable sealed iteration


def test_seal_plan_iteration_none_when_not_converged(tmp_path):
    eng = _engine(tmp_path)
    from consensus_mcp.workflow_engine import IterationOutcome
    iter_dir = tmp_path / "iter-plan2"; iter_dir.mkdir()
    plan = iter_dir / "converged-plan.yaml"; plan.write_text("decision: {}\n", encoding="utf-8")
    outcome = IterationOutcome(iteration_id=iter_dir.name, workflow_mode=cfg.WORKFLOW_PROPOSE_CONVERGE,
                               effective_config_path=tmp_path / "ec.yaml")
    arts = [_review_artifact("claude", satisfied=False, blocking=["b1"]),
            _review_artifact("codex", satisfied=False, blocking=["b2"]),
            _review_artifact("gemini", satisfied=True)]
    for a in arts:
        outcome.contributor_artifacts.setdefault(a.contributor, []).append(a)
    conv = eng.evaluate_plan_convergence(arts, outcome)
    assert eng.seal_plan_iteration(iter_dir, plan, conv, round_number=1) is None
    assert not (iter_dir / "iteration-outcome.yaml").exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.local/share/pipx/venvs/consensus-mcp/bin/python -m pytest consensus_mcp/tests/test_workflow_v1306_synthesis.py -v -k "plan_convergence or seal_plan"`
Expected: FAIL - `AttributeError: ... 'evaluate_plan_convergence'`

- [ ] **Step 3: Add the two methods (after `_seal_converged_plan`)**

```python
    def evaluate_plan_convergence(
        self,
        review_artifacts: list[SealedArtifact],
        outcome: IterationOutcome,
        eligible_voters: list[str] | None = None,
    ) -> ConvergenceOutcome:
        """Path A convergence evaluation: did contributors approve the ONE synthesized plan?

        Vote-counting is identical to the engine's convergence rule - the only difference
        from Path B is WHAT was reviewed (the host-authored plan, not a proposal bundle).
        Thin public wrapper over _evaluate_convergence so the host orchestrator has a named,
        intention-revealing entry point."""
        return self._evaluate_convergence(review_artifacts, outcome, eligible_voters)

    def seal_plan_iteration(
        self,
        iteration_dir: Path,
        plan_path: Path,
        conv: ConvergenceOutcome,
        round_number: int,
    ) -> Path | None:
        """On convergence, seal the HOST-AUTHORED plan as the iteration's converged artifact.

        Unlike _seal_converged_plan (which writes an engine SUMMARY), Path A's plan is
        authored by the host and must NOT be overwritten. We only write iteration-outcome.yaml
        with a sealed closing_state so mint_design_approval can point at this iteration (the
        cross-family review YAMLs already live here from the review dispatch). Returns the plan
        path on convergence, else None (the host must revise the plan and re-dispatch)."""
        from consensus_mcp._delivery_readiness import SEALED_CLOSING_STATES
        if not conv.converged:
            return None
        closing_state = "quorum_close_passed"
        assert closing_state in SEALED_CLOSING_STATES  # guard against a future rename
        (iteration_dir / "iteration-outcome.yaml").write_text(
            yaml.safe_dump({
                "iteration_id": iteration_dir.name,
                "workflow_mode": self.config["workflow"]["mode"],
                "closing_state": closing_state,
                "converged_at_round": round_number,
                "convergence_rule": conv.rule,
                "approve_votes": conv.approve_votes,
                "final_artifact_path": str(plan_path),
                "convergence_path": "A-orchestrator-synthesis",
            }, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return plan_path
```

- [ ] **Step 4: Run to verify pass**

Run: `~/.local/share/pipx/venvs/consensus-mcp/bin/python -m pytest consensus_mcp/tests/test_workflow_v1306_synthesis.py -v -k "plan_convergence or seal_plan"`
Expected: PASS (4)

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/workflow_engine.py consensus_mcp/tests/test_workflow_v1306_synthesis.py
git commit -m "feat(v1.30.6): Path A evaluate_plan_convergence + seal_plan_iteration"
```

---

## Task 4: Document the field + the Path A flow

**Files:**
- Modify: `consensus_mcp/goal_packet_schema.yaml`
- Create: `docs/workflows/path-a-plan-convergence.md`

- [ ] **Step 1: Add the field to the goal_packet template**

Append to `consensus_mcp/goal_packet_schema.yaml` (after `authorization:` block):

```yaml
# v1.30.6: declare when the deliverable is ONE synthesized artifact (a plan), so the
# autonomous engine fails loud and the consult converges via the host-driven Path A flow.
convergence:
  requires_synthesis: false   # set true for plan-deliverable propose-converge consults
```

- [ ] **Step 2: Write the Path A flow doc**

Create `docs/workflows/path-a-plan-convergence.md`:

```markdown
# Path A: converging on a synthesized plan

A propose-converge consult whose deliverable is ONE merged plan cannot converge in the
autonomous engine (Path B / `run_iteration`) - there is no host in the loop to author/revise
the plan, so the engine would only bundle proposals and vote on the pile. Declare it and use
Path A:

1. In the goal_packet: `convergence: { requires_synthesis: true }`. (Path B now fails loud
   pointing here.)
2. Host authors ONE `converged-plan.yaml` in the iteration dir (the real plan: resolves each
   goal question; includes the feasibility matrix + concrete DoD).
3. Dispatch each contributor to review THAT plan (blind round 1):
   `consensus-mcp-dispatch-<codex|gemini|kimi> --goal-packet <gp> --iteration-dir <iter>
   --reviewer-id <id> --pass-id <id>-pass1 --mode review --review-target <iter>/converged-plan.yaml`
   (the v1.30.5 review_target_content embed makes the plan visible to the sandbox).
4. Load the sealed review artifacts and call
   `engine.evaluate_plan_convergence(review_artifacts, outcome)`.
5. If `conv.converged`: `engine.seal_plan_iteration(iter, plan_path, conv, round_number)` ->
   writes `iteration-outcome.yaml` (sealed closing_state). With the plan + >=2 cross-family
   review YAMLs present, `mint_design_approval` can now point at this iteration.
6. If not converged: REVISE `converged-plan.yaml` (fold in the round's findings) and
   re-dispatch - the next round's review target is the REVISED plan.
```

- [ ] **Step 3: Commit**

```bash
git add consensus_mcp/goal_packet_schema.yaml docs/workflows/path-a-plan-convergence.md
git commit -m "docs(v1.30.6): convergence.requires_synthesis field + Path A flow"
```

---

## Task 5: Release v1.30.6

**Files:**
- Modify: `CHANGELOG.md`, `pyproject.toml`

- [ ] **Step 1: Full suite (no regressions)**

Run: `~/.local/share/pipx/venvs/consensus-mcp/bin/python -m pytest consensus_mcp/tests/ -q`
Expected: all pass (prior baseline 1673 passed, 7 skipped, + the new v1306 tests)

- [ ] **Step 2: CHANGELOG entry** - prepend under `# Changelog`:

```markdown
## 1.30.6 - 2026-05-24

**Synthesis-aware propose-converge.** A plan-deliverable consult can't converge in the
autonomous engine (it bundles proposals + votes on the pile, never merging to ONE plan).

### Fixed / Added
- `_run_workflow_4` now FAILS LOUD when the goal_packet declares
  `convergence.requires_synthesis: true` - instead of silently never-converging - pointing at
  the host-driven Path A flow. The max-rounds error also hints at this for the undeclared case.
- New Path A helpers `WorkflowEngine.evaluate_plan_convergence` (delegates to the convergence
  rule) and `seal_plan_iteration` (seals the host-authored plan as the converged artifact via
  `iteration-outcome.yaml`, never overwriting it) - so a host can converge contributors on ONE
  synthesized plan and mint design-approval from it.
- Operator-declared (never inferred) `convergence.requires_synthesis` goal_packet field + the
  Path A flow doc.
```

- [ ] **Step 3: Bump version** in `pyproject.toml`: `version = "1.30.5"` -> `version = "1.30.6"`

- [ ] **Step 4: Commit + ship** (tag + GitHub Release Latest + main FF if branched + install refresh + assert from /tmp). Follow the release runbook used for v1.30.1-1.30.5.

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "release: v1.30.6 - synthesis-aware propose-converge (Path B fail-loud + Path A helper)"
```

---

## Self-Review (completed by plan author)

- **Spec coverage:** detection field (Task 1, 4) [ok]; Path B fail-loud + undeclared hint (Task 2) [ok]; Path A helper evaluate+seal-the-plan (Task 3) [ok]; acceptance tests - Path B raises (Task 2), Path A converges+seals the plan / blocks->None (Task 3) [ok]; docs (Task 4) [ok]. The "next round target is the revised plan" property is exercised by `seal_plan_iteration` sealing whatever `plan_path` the host passes (round 2 passes the revised path) + the host-driven flow in the doc.
- **Placeholders:** none - every code/test step shows complete code.
- **Type consistency:** `evaluate_plan_convergence` / `seal_plan_iteration` signatures match between Task 3 definition and its tests; `ConvergenceOutcome` fields used (`.converged`, `.rule`, `.approve_votes`) match the dataclass; `SEALED_CLOSING_STATES` imported from `_delivery_readiness`.
