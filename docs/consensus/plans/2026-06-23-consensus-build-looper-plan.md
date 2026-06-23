# Consensus Build - Looper Plan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use consensus:subagent-driven-development (recommended) or consensus:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, vendored Looper design-coach front-door to Consensus Build that coaches a goal + typed verification + caps, then seeds the Build goal (problem.md + suggestions) - with ZERO diff to the Build supervisor/invariants/gates/schemas.

**Architecture:** A self-contained `consensus_mcp/looper_plan/` package (verbatim rubrics + schemas, a trimmed `compile.py` derivative, a new `seed.py` adapter) plus a vendored `consensus-looper-plan` wizard skill and a goal-setup launch toggle. The without-looper path never imports `looper_plan` (lazy boundary). Looper artifacts live inside the goal dir, written pre-baseline and write-once-immutable; the existing architect-tree recheck protects them and a module-level re-coach refusal prevents post-baseline rewrites.

**Tech Stack:** Python 3.13 stdlib + PyYAML (already a dep), pytest, Claude Code skill markdown. Source to port: upstream `ksimback/looper` `scripts/looper.py` (staged at `consensus-state/active/iteration-looper-plan-design-2026-06-23/evidence/looper/`).

**Spec:** `docs/superpowers/specs/2026-06-23-consensus-build-looper-plan-design.md`. **Ratified by:** consult `iteration-looper-plan-design-2026-06-23`.

---

## File Structure

**Create:**
- `consensus_mcp/looper_plan/__init__.py` - package marker + public exports (`compile_plan`, `synthesize_stub_fields`, `seed_build_inputs`).
- `consensus_mcp/looper_plan/rubrics/{goal,verification,council,control}-rubric.md` - verbatim from upstream `references/`.
- `consensus_mcp/looper_plan/schemas/{loop.v1,loop.resolved.v1}.schema.json` - verbatim from upstream `schemas/`.
- `consensus_mcp/looper_plan/compile.py` - trimmed port of upstream `looper.py` + `synthesize_stub_fields`.
- `consensus_mcp/looper_plan/seed.py` - new Build adapter (problem.md / suggestions / manifest / taxonomy mapping / re-coach guard).
- `consensus_mcp/looper_plan/VENDORED.md` - provenance (URL, pinned commit, kept/trimmed list).
- `consensus_mcp/looper_plan/NOTICE` - MIT notice.
- `consensus_mcp/claude_extensions/skills/consensus-looper-plan/SKILL.md` - the vendored wizard.
- `consensus_mcp/tests/test_looper_plan_compile.py`
- `consensus_mcp/tests/test_looper_plan_seed.py`
- `consensus_mcp/tests/test_looper_plan_snapshot.py` - decisive baseline-inclusion guard.
- `consensus_mcp/tests/test_looper_plan_without_identity.py` - lazy-import boundary.
- `consensus_mcp/tests/test_looper_plan_vendoring.py` - reference-integrity + attribution.

**Modify:**
- `pyproject.toml` - package-data ships `consensus_mcp/looper_plan/**/*`.
- `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md` - document the launch toggle.
- `docs/workflows/architect-build.md` - document the with/without-looper option.

**Zero diff (must not appear in any task):** `consensus_mcp/_architect_lane.py`, `_architect_handoff.py`, `_architect_paths.py`, `_dispatch_builder.py`; the `architect.loop_step` / `approve_spec` / `cleanup` / `loop.run_goal` handlers; the goal-packet / spec schema; seal formats; stop-rule set.

---

## Task 1: Vendor the verbatim slice (rubrics, schemas, attribution)

**Files:**
- Create: `consensus_mcp/looper_plan/__init__.py`, `rubrics/*.md` (4), `schemas/*.json` (2), `VENDORED.md`, `NOTICE`
- Test: `consensus_mcp/tests/test_looper_plan_vendoring.py`

- [ ] **Step 1: Copy the verbatim files from the staged evidence**

```bash
SRC=consensus-state/active/iteration-looper-plan-design-2026-06-23/evidence/looper
DST=consensus_mcp/looper_plan
mkdir -p "$DST/rubrics" "$DST/schemas"
cp "$SRC/references/goal-rubric.md"         "$DST/rubrics/goal-rubric.md"
cp "$SRC/references/verification-rubric.md" "$DST/rubrics/verification-rubric.md"
cp "$SRC/references/council-rubric.md"      "$DST/rubrics/council-rubric.md"
cp "$SRC/references/control-rubric.md"      "$DST/rubrics/control-rubric.md"
cp "$SRC/schemas/loop.v1.schema.json"           "$DST/schemas/loop.v1.schema.json"
cp "$SRC/schemas/loop.resolved.v1.schema.json"  "$DST/schemas/loop.resolved.v1.schema.json"
```

- [ ] **Step 2: Record the pinned upstream commit**

Run: `git -C /tmp/claude-1000/-home-steve-projects-consensus-mcp/d68f1be9-1556-40b3-823a-be5cd8dd8fba/scratchpad/looper rev-parse HEAD` (or note "shallow clone; pin by clone date 2026-06-23" if the clone was `--depth 1`). Use the value in `VENDORED.md`.

- [ ] **Step 3: Write `NOTICE`**

```
Looper (design-coach slice) vendored into consensus-mcp.
Upstream: https://github.com/ksimback/looper
Copyright (c) Kevin Simback. Licensed under the MIT License.

This product includes a verbatim copy of Looper's rubrics and JSON schemas,
and a trimmed derivative of scripts/looper.py (see consensus_mcp/looper_plan/VENDORED.md).
```

- [ ] **Step 4: Write `VENDORED.md`**

```markdown
# Vendored: ksimback/looper (design-coach slice)

- Upstream: https://github.com/ksimback/looper
- License: MIT (c) Kevin Simback
- Pinned commit: <COMMIT-FROM-STEP-2>
- Vendored: 2026-06-23 (consult iteration-looper-plan-design-2026-06-23)

## Kept verbatim
- references/goal-rubric.md       -> rubrics/goal-rubric.md
- references/verification-rubric.md -> rubrics/verification-rubric.md
- references/council-rubric.md     -> rubrics/council-rubric.md
- references/control-rubric.md     -> rubrics/control-rubric.md
- schemas/loop.v1.schema.json      -> schemas/loop.v1.schema.json
- schemas/loop.resolved.v1.schema.json -> schemas/loop.resolved.v1.schema.json

## Trimmed derivative
- scripts/looper.py -> compile.py: kept load_yaml, to_jsonable, normalize_argv,
  criteria_by_id, validate_member, validate_gate, normalize_spec, clip, ascii_box,
  render_ascii_diagram, render_loop. DROPPED: detect-models / register-model /
  MODEL_PROBES / registry I/O, render_session_prompt, the argparse CLI, and the
  external run-loop.py runner.

## Not vendored (Build supersedes)
- run-loop.py external runner; RUN_IN_SESSION.md handoff; model registry;
  privacy/egress machinery; single-judge council runtime.
```

- [ ] **Step 5: Write `__init__.py`**

```python
"""Vendored Looper design-coach slice (MIT, (c) Kevin Simback). See VENDORED.md.

This package coaches a Build goal (goal + typed verification + caps) and seeds
Consensus Build. It is imported ONLY on the with-looper-plan goal-setup path.
"""
from consensus_mcp.looper_plan.compile import compile_plan, synthesize_stub_fields
from consensus_mcp.looper_plan.seed import seed_build_inputs

__all__ = ["compile_plan", "synthesize_stub_fields", "seed_build_inputs"]
```

- [ ] **Step 6: Write the reference-integrity + attribution test (failing first)**

`consensus_mcp/tests/test_looper_plan_vendoring.py`:

```python
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "looper_plan"

def test_rubrics_and_schemas_present():
    for name in ("goal", "verification", "council", "control"):
        assert (PKG / "rubrics" / f"{name}-rubric.md").is_file()
    for name in ("loop.v1.schema.json", "loop.resolved.v1.schema.json"):
        assert (PKG / "schemas" / name).is_file()

def test_vendored_md_lists_every_shipped_rubric_and_schema():
    vend = (PKG / "VENDORED.md").read_text(encoding="utf-8")
    for name in ("goal-rubric.md", "verification-rubric.md",
                 "council-rubric.md", "control-rubric.md",
                 "loop.v1.schema.json", "loop.resolved.v1.schema.json"):
        assert name in vend, f"{name} not recorded in VENDORED.md"

def test_notice_carries_mit_and_upstream():
    notice = (PKG / "NOTICE").read_text(encoding="utf-8")
    assert "MIT" in notice and "ksimback/looper" in notice
```

- [ ] **Step 7: Run the test**

Run: `.venv/bin/python -m pytest consensus_mcp/tests/test_looper_plan_vendoring.py -v`
Expected: PASS (the `__init__` import will fail until Task 2/4 land; if so, mark these tests with the import deferred - run again after Task 4). To keep this task green standalone, the test file does NOT import the package, only reads files.

- [ ] **Step 8: Commit**

```bash
git add consensus_mcp/looper_plan/rubrics consensus_mcp/looper_plan/schemas \
        consensus_mcp/looper_plan/VENDORED.md consensus_mcp/looper_plan/NOTICE \
        consensus_mcp/looper_plan/__init__.py consensus_mcp/tests/test_looper_plan_vendoring.py
git commit -m "feat(looper-plan): vendor looper design-coach slice (rubrics, schemas, attribution)"
```

---

## Task 2: `compile.py` - port the validator/renderer

**Files:**
- Create: `consensus_mcp/looper_plan/compile.py`
- Test: `consensus_mcp/tests/test_looper_plan_compile.py`

- [ ] **Step 1: Write failing tests (ported from upstream `tests/test_looper.py`)**

```python
import json, textwrap
from pathlib import Path
import pytest
from consensus_mcp.looper_plan import compile as lc

def _write(tmp_path, body):
    p = tmp_path / "loop.yaml"; p.write_text(textwrap.dedent(body), encoding="utf-8"); return p

VALID = """
version: 1
meta: {name: t}
goal:
  statement: do x
  definition_of_done: x done
  verification:
    - {id: build, type: programmatic, check: ["true"], expect: exit_zero}
host: {cli: codex, model: m, invoke: ["codex","exec"]}
council:
  - {id: r1, role: judge, cli: claude, model: o, invoke: ["claude","-p"]}
gates:
  plan_gate: {when: after_plan, members: [r1], verdict_policy: revise_until_clean, verdict_source: r1, criteria: [build], max_revisions: 3}
  delivery_gate: {when: after_each_delivery, members: [r1], verdict_policy: revise_until_clean, verdict_source: r1, criteria: [build], max_revisions: 3}
loop_control: {max_iterations: 8}
workspace: {dir: ./loop-workspace}
"""

def test_compile_plan_returns_resolved_and_markdown(tmp_path):
    resolved, md = lc.compile_plan(_write(tmp_path, VALID))
    assert resolved["criteria_by_id"]["build"]["type"] == "programmatic"
    assert "## Flow Preview" in md

def test_reviewer_only_revise_until_clean_rejected(tmp_path):
    bad = VALID.replace("role: judge", "role: reviewer")
    with pytest.raises(lc.LooperError):
        lc.compile_plan(_write(tmp_path, bad))

def test_duplicate_criteria_id_rejected(tmp_path):
    bad = VALID.replace(
        "    - {id: build, type: programmatic, check: [\"true\"], expect: exit_zero}",
        "    - {id: build, type: programmatic, check: [\"true\"], expect: exit_zero}\n"
        "    - {id: build, type: human, prompt: again}")
    with pytest.raises(lc.LooperError):
        lc.compile_plan(_write(tmp_path, bad))
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/python -m pytest consensus_mcp/tests/test_looper_plan_compile.py -v`
Expected: FAIL with `ModuleNotFoundError`/`AttributeError`.

- [ ] **Step 3: Port the implementation into `compile.py`**

Copy these symbols VERBATIM from the staged upstream `scripts/looper.py` (evidence path in header), keeping their bodies byte-identical except removing the `from __future__` duplication and dropping unused imports:

`DEFAULT_REDACTIONS`, `LooperError`, `load_yaml`, `to_jsonable`, `normalize_argv`, `criteria_by_id`, `validate_member`, `validate_gate`, `normalize_spec`, `clip`, `ascii_box`, `render_ascii_diagram`, `render_loop`.

Do NOT copy: `REGISTRY_PATH`, `MODEL_PROBES`, `read_registry`, `write_registry`, `run_probe`, `detect_models`, `cmd_*`, `build_parser`, `main`, `render_session_prompt`, `load_json`, `write_json`.

Then add the public entry at the bottom:

```python
def compile_plan(loop_yaml_path) -> tuple[dict, str]:
    """Validate+normalize a loop.yaml and render LOOP.md. Returns (resolved, md).

    Trimmed derivative of ksimback/looper scripts/looper.py (MIT). See VENDORED.md.
    """
    from pathlib import Path
    source = Path(loop_yaml_path).resolve()
    spec = load_yaml(source)
    resolved = normalize_spec(spec, source)
    return resolved, render_loop(resolved)
```

File header:

```python
"""Trimmed derivative of ksimback/looper scripts/looper.py (MIT, (c) Kevin
Simback). Validator + renderer only; model detection, the external runner, and
session-prompt emission are intentionally omitted. See looper_plan/VENDORED.md."""
from __future__ import annotations
import datetime as _dt
import json
import os
import shlex
from pathlib import Path
from typing import Any
```

- [ ] **Step 4: Run, verify pass**

Run: `.venv/bin/python -m pytest consensus_mcp/tests/test_looper_plan_compile.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/looper_plan/compile.py consensus_mcp/tests/test_looper_plan_compile.py
git commit -m "feat(looper-plan): trimmed compile.py (validate+render) ported from upstream"
```

---

## Task 3: `compile.py` - `synthesize_stub_fields` (closes the schema-required-fields gap)

**Files:**
- Modify: `consensus_mcp/looper_plan/compile.py`
- Test: `consensus_mcp/tests/test_looper_plan_compile.py`

- [ ] **Step 1: Write the failing test**

```python
def test_synthesize_stub_fields_makes_coached_only_spec_compile(tmp_path):
    coached = {
        "version": 1,
        "meta": {"name": "g1"},
        "goal": {"statement": "do x", "definition_of_done": "x done",
                 "verification": [{"id": "build", "type": "programmatic",
                                   "check": ["pytest", "-q"], "expect": "exit_zero"}]},
        "loop_control": {"max_iterations": 8},
    }
    roles = {"architect": "claude", "builder": "codex", "reviewer": "codex"}
    full = lc.synthesize_stub_fields(coached, roles)
    p = tmp_path / "loop.yaml"
    import yaml; p.write_text(yaml.safe_dump(full), encoding="utf-8")
    resolved, _ = lc.compile_plan(p)             # must not raise
    assert resolved["execution"]["mode"] == "orchestrated"
    assert resolved["execution"]["isolation"] == "worktree"
    assert resolved["council"], "council stub-seeded from roles"
    assert "plan_gate" in resolved["gates"] and "delivery_gate" in resolved["gates"]
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/python -m pytest consensus_mcp/tests/test_looper_plan_compile.py::test_synthesize_stub_fields_makes_coached_only_spec_compile -v`
Expected: FAIL (`AttributeError: synthesize_stub_fields`).

- [ ] **Step 3: Implement `synthesize_stub_fields`**

```python
# A non-network sentinel invoke for stub council/host; Build never executes these.
_STUB_INVOKE = ["consensus", "build", "executes-this-not-looper"]

def synthesize_stub_fields(coached: dict, roles: dict) -> dict:
    """Fill the loop.v1 schema's required-but-unused-by-us fields (host, council,
    gates, workspace, execution) from Build's roles, so a goal/verification/caps
    -only coached spec validates. These stubs are NEVER executed - Build is the
    runner; they exist solely to keep normalize_spec verbatim."""
    spec = {**coached}
    crit_ids = [c["id"] for c in spec.get("goal", {}).get("verification", [])]
    first = crit_ids[:1] or []
    spec.setdefault("host", {"cli": roles.get("builder", "codex"),
                             "model": "build-resolved",
                             "invoke": list(_STUB_INVOKE)})
    spec.setdefault("council", [{"id": "reviewer", "role": "judge",
                                 "cli": roles.get("reviewer", "codex"),
                                 "model": "build-resolved",
                                 "invoke": list(_STUB_INVOKE)}])
    spec.setdefault("gates", {
        "plan_gate": {"when": "after_plan", "members": ["reviewer"],
                      "verdict_policy": "revise_until_clean",
                      "verdict_source": "reviewer", "criteria": list(first),
                      "max_revisions": 3},
        "delivery_gate": {"when": "after_each_delivery", "members": ["reviewer"],
                          "verdict_policy": "revise_until_clean",
                          "verdict_source": "reviewer", "criteria": list(crit_ids),
                          "max_revisions": 3},
    })
    spec.setdefault("execution", {"mode": "orchestrated", "isolation": "worktree",
                                  "side_effects": {"requires_approval": True,
                                                   "duplicate_action_check": True}})
    spec.setdefault("workspace", {"dir": "./looper-plan"})
    return spec
```

NOTE: if every coached verification criterion is `judge`/`human`, `first` is the
first id regardless of type - the stub gate just needs a valid criterion id; the
real verification mapping is seed.py's job (Task 5).

- [ ] **Step 4: Run, verify pass**

Run: `.venv/bin/python -m pytest consensus_mcp/tests/test_looper_plan_compile.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/looper_plan/compile.py consensus_mcp/tests/test_looper_plan_compile.py
git commit -m "feat(looper-plan): synthesize_stub_fields closes schema-required-fields gap"
```

---

## Task 4: `seed.py` - render `problem.md`

**Files:**
- Create: `consensus_mcp/looper_plan/seed.py`
- Test: `consensus_mcp/tests/test_looper_plan_seed.py`

- [ ] **Step 1: Write the failing test**

```python
from consensus_mcp.looper_plan import seed

RESOLVED = {
    "goal": {"statement": "Produce X", "definition_of_done": "X is done",
             "verification": [
                 {"id": "build", "type": "programmatic", "check": ["pytest","-q"], "expect": "exit_zero"},
                 {"id": "covers", "type": "judge", "rubric": "every step has an owner"},
                 {"id": "signoff", "type": "human", "prompt": "client confirms"},
             ]},
}

def test_problem_md_has_goal_dod_and_nonautomation_banner():
    md = seed.render_problem_md(RESOLVED)
    assert "Produce X" in md and "X is done" in md
    assert "pytest -q" in md or "['pytest', '-q']" in md or "pytest" in md
    assert "NON-AUTOMATION" in md          # judge/human under the design-criteria banner
    assert "every step has an owner" in md and "client confirms" in md
```

- [ ] **Step 2: Run, verify failure** -> `ModuleNotFoundError`.

- [ ] **Step 3: Implement `render_problem_md` in `seed.py`**

```python
"""Build adapter for the vendored Looper slice (ours, not upstream). Maps a
validated loop.resolved.json into Consensus Build inputs: problem.md, a
suggestions file (never auto-applied), and a write-once manifest."""
from __future__ import annotations
import hashlib, json, os, shlex, subprocess
from pathlib import Path

def _criteria(resolved): return resolved.get("goal", {}).get("verification", [])

def render_problem_md(resolved: dict) -> str:
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
        lines.append(f"- `{c['id']}`: run `{json.dumps(c['check'])}` expect `{c['expect']}`"
                     + (f" contains `{c.get('contains')}`" if c.get("expect") == "stdout_contains" else ""))
    if not prog:
        lines.append("- (none - no programmatic criteria coached)")
    lines += ["", "## Design criteria (NON-AUTOMATION - architect/reviewer/human judgment, NOT executable gates)", ""]
    for c in design:
        if c["type"] == "judge":
            lines.append(f"- `{c['id']}` (judge rubric): {c['rubric']}")
        else:
            lines.append(f"- `{c['id']}` (human signoff): {c['prompt']}")
    if not design:
        lines.append("- (none)")
    lines += ["", "_Coached via the Looper design front-door; see `looper-plan/LOOP.md`._", ""]
    return "\n".join(lines)
```

- [ ] **Step 4: Run, verify pass.**

Run: `.venv/bin/python -m pytest consensus_mcp/tests/test_looper_plan_seed.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/looper_plan/seed.py consensus_mcp/tests/test_looper_plan_seed.py
git commit -m "feat(looper-plan): seed.py render_problem_md (programmatic vs non-automation design criteria)"
```

---

## Task 5: `seed.py` - verification taxonomy mapping + cross-platform command rendering

**Files:**
- Modify: `consensus_mcp/looper_plan/seed.py`
- Test: `consensus_mcp/tests/test_looper_plan_seed.py`

- [ ] **Step 1: Write failing tests**

```python
def test_render_verification_command_uses_platform_quoting():
    cmd = seed.render_verification_command(["pytest", "-q", "tests/a b.py"])
    assert "pytest" in cmd and ("a b.py" in cmd or "a b.py" in cmd.replace('"', ''))

def test_map_verification_exit_zero_to_frozen_and_acceptance():
    m = seed.map_verification(RESOLVED)
    assert m["frozen_verification"]            # first programmatic -> frozen gate
    assert any(g["id"] == "build" for g in m["acceptance_gates"])
    # judge/human never become deterministic gates:
    assert all(g["id"] != "covers" and g["id"] != "signoff" for g in m["acceptance_gates"])
    assert any(d["id"] == "covers" for d in m["design_criteria"])

def test_map_verification_non_exit_zero_flagged_for_operator():
    r = {"goal": {"verification": [
        {"id": "has", "type": "programmatic", "check": ["grep","x","f"], "expect": "stdout_contains", "contains": "x"}]}}
    m = seed.map_verification(r)
    g = [x for x in m["acceptance_gates"] if x["id"] == "has"][0]
    assert g["needs_operator_edit"] is True
```

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Implement the mapping**

```python
def render_verification_command(check: list[str]) -> str:
    """argv -> shell string for Build's shell=True verification gate.
    POSIX: shlex.join; Windows: subprocess.list2cmdline (Build runs shell=True)."""
    if os.name == "nt":
        return subprocess.list2cmdline(check)
    return shlex.join(check)

def map_verification(resolved: dict) -> dict:
    """Taxonomy -> Build shapes. programmatic+exit_zero maps cleanly; other
    expects are flagged needs_operator_edit (never silently remapped); judge/human
    become design_criteria, NOT deterministic gates. Multi-programmatic: the first
    is the frozen gate, the rest are acceptance_gates."""
    prog = [c for c in _criteria(resolved) if c["type"] == "programmatic"]
    design = [c for c in _criteria(resolved) if c["type"] in ("judge", "human")]
    acceptance, frozen = [], ""
    for i, c in enumerate(prog):
        clean = c.get("expect") == "exit_zero"
        cmd = render_verification_command(c["check"])
        if i == 0 and clean:
            frozen = cmd
        acceptance.append({"id": c["id"], "description": f"{c['id']} ({c['expect']})",
                           "check": cmd, "needs_operator_edit": not clean})
    return {"frozen_verification": frozen,
            "acceptance_gates": acceptance,
            "design_criteria": [{"id": c["id"], "type": c["type"]} for c in design]}
```

- [ ] **Step 4: Run, verify pass.** Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/looper_plan/seed.py consensus_mcp/tests/test_looper_plan_seed.py
git commit -m "feat(looper-plan): seed.py verification taxonomy mapping + cross-platform command quoting"
```

---

## Task 6: `seed.py` - manifest seal + re-coach refusal

**Files:**
- Modify: `consensus_mcp/looper_plan/seed.py`
- Test: `consensus_mcp/tests/test_looper_plan_seed.py`

- [ ] **Step 1: Write failing tests**

```python
def test_seal_manifest_records_sha256(tmp_path):
    (tmp_path / "looper-plan").mkdir()
    (tmp_path / "looper-plan" / "loop.yaml").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "problem.md").write_text("# x\n", encoding="utf-8")
    man = seed.seal_manifest(tmp_path)
    assert "looper-plan/loop.yaml" in man["files"] and len(man["files"]["looper-plan/loop.yaml"]) == 64

def test_assert_safe_to_coach_refuses_when_supervisor_artifact_exists(tmp_path):
    seed.assert_safe_to_coach(tmp_path)                  # clean goal dir -> ok
    (tmp_path / "spec-approval.yaml").write_text("x", encoding="utf-8")
    import pytest
    with pytest.raises(seed.ReCoachRefused):
        seed.assert_safe_to_coach(tmp_path)
```

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Implement**

```python
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
    goal_dir = Path(goal_dir)
    for marker in _BUILD_PROGRESS_MARKERS:
        if (goal_dir / marker).exists():
            raise ReCoachRefused(
                f"{goal_dir} already has {marker}: Build has begun. Re-coaching "
                f"would mutate baseline-covered inputs and block delivery. Start a "
                f"new goal id instead.")
    # also refuse if any cycle dir exists
    if any(p.name.startswith("cycle-") for p in goal_dir.glob("cycle-*")):
        raise ReCoachRefused(f"{goal_dir} has cycle dirs: Build has begun.")
```

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/looper_plan/seed.py consensus_mcp/tests/test_looper_plan_seed.py
git commit -m "feat(looper-plan): manifest seal + re-coach refusal (module-level write-once guard)"
```

---

## Task 7: `seed.py` - `seed_build_inputs` orchestration

**Files:**
- Modify: `consensus_mcp/looper_plan/seed.py`
- Test: `consensus_mcp/tests/test_looper_plan_seed.py`

- [ ] **Step 1: Write the failing test**

```python
import yaml
def test_seed_build_inputs_writes_all_artifacts(tmp_path):
    out = seed.seed_build_inputs(RESOLVED, tmp_path)
    assert (tmp_path / "problem.md").is_file()
    sug = yaml.safe_load((tmp_path / "looper-suggestions.yaml").read_text())
    assert "frozen_verification" in sug and "acceptance_gates" in sug
    man = yaml.safe_load((tmp_path / "looper-plan-manifest.yaml").read_text())
    assert "problem.md" in man["files"]
    assert out["problem_md"].endswith("problem.md")
```

- [ ] **Step 2: Run, verify failure.**

- [ ] **Step 3: Implement**

```python
def seed_build_inputs(resolved: dict, goal_dir: Path) -> dict:
    """Write problem.md + looper-suggestions.yaml + looper-plan-manifest.yaml into
    the goal dir. Refuses if Build has already begun (assert_safe_to_coach)."""
    import yaml
    goal_dir = Path(goal_dir); goal_dir.mkdir(parents=True, exist_ok=True)
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
```

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/looper_plan/seed.py consensus_mcp/tests/test_looper_plan_seed.py
git commit -m "feat(looper-plan): seed_build_inputs orchestration (problem.md + suggestions + manifest)"
```

---

## Task 8: Decisive guard - architect-tree snapshot covers looper artifacts

**Files:**
- Test: `consensus_mcp/tests/test_looper_plan_snapshot.py`

This is the consult's decisive experiment: prove Build's EXISTING snapshot covers
inside-goal-dir looper files (so zero supervisor change is needed) AND that a
post-baseline mutation is caught.

- [ ] **Step 1: Write the test**

```python
from pathlib import Path
from consensus_mcp import _architect_lane as lane
from consensus_mcp import _architect_paths as ap

def _goal(tmp_path):
    g = tmp_path / ".consensus" / "architect" / "g1"
    (g / "looper-plan").mkdir(parents=True)
    (g / "looper-plan" / "loop.yaml").write_text("version: 1\n", encoding="utf-8")
    (g / "looper-plan" / "LOOP.md").write_text("# preview\n", encoding="utf-8")
    (g / "problem.md").write_text("# coached\n", encoding="utf-8")
    return g

def test_architect_tree_snapshot_includes_looper_files(tmp_path):
    g = _goal(tmp_path)
    snap = lane.snapshot_architect_tree(tmp_path, exclude_lane=g / ap.LANE_DIRNAME)
    assert any("looper-plan/loop.yaml" in k for k in snap)
    assert any("problem.md" in k for k in snap)

def test_post_baseline_mutation_is_a_violation(tmp_path):
    g = _goal(tmp_path)
    before = lane.snapshot_architect_tree(tmp_path, exclude_lane=g / ap.LANE_DIRNAME)
    (g / "problem.md").write_text("# coached EDITED\n", encoding="utf-8")
    violations = lane.check_architect_tree(tmp_path, before, exclude_lane=g / ap.LANE_DIRNAME)
    assert any("problem.md" in v and "modified" in v for v in violations)
```

- [ ] **Step 2: Run, verify it PASSES against the unmodified lane code**

Run: `.venv/bin/python -m pytest consensus_mcp/tests/test_looper_plan_snapshot.py -v`
Expected: PASS - confirming zero supervisor change is needed. If it FAILS, the zero-diff claim is refuted (per the consult); STOP and escalate to the operator before any supervisor edit.

- [ ] **Step 3: Commit**

```bash
git add consensus_mcp/tests/test_looper_plan_snapshot.py
git commit -m "test(looper-plan): decisive guard - architect-tree snapshot covers looper artifacts, catches mutation"
```

---

## Task 9: Without-looper byte-identity + lazy-import boundary

**Files:**
- Test: `consensus_mcp/tests/test_looper_plan_without_identity.py`

- [ ] **Step 1: Write the test**

```python
import importlib, sys
from pathlib import Path

def test_loop_step_does_not_import_looper_plan():
    # Importing the supervisor must NOT pull in looper_plan (lazy boundary).
    for m in list(sys.modules):
        if m.startswith("consensus_mcp.looper_plan"):
            del sys.modules[m]
    importlib.import_module("consensus_mcp.tools.architect_loop_step")
    assert not any(m.startswith("consensus_mcp.looper_plan") for m in sys.modules), \
        "architect.loop_step must not import looper_plan"

def test_architect_lane_source_has_no_looper_reference():
    src = Path("consensus_mcp/_architect_lane.py").read_text(encoding="utf-8")
    assert "looper" not in src.lower(), "Build lane must have zero looper references (zero-diff)"
```

- [ ] **Step 2: Run, verify pass.**

Run: `.venv/bin/python -m pytest consensus_mcp/tests/test_looper_plan_without_identity.py -v`
Expected: PASS. (If the loop_step module name differs, adjust the import path to the actual module under `consensus_mcp/tools/`.)

- [ ] **Step 3: Commit**

```bash
git add consensus_mcp/tests/test_looper_plan_without_identity.py
git commit -m "test(looper-plan): zero-diff guards - no looper import on the Build path"
```

---

## Task 10: Packaging - ship `looper_plan/**/*`

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Inspect the current package-data**

Run: `.venv/bin/python - <<'PY'\nimport tomllib;d=tomllib.load(open('pyproject.toml','rb'));print(d.get('tool',{}).get('setuptools',{}).get('package-data'))\nPY`
Expected: a dict mapping `consensus_mcp` to globs (e.g. dispatch_templates, skills).

- [ ] **Step 2: Add the looper_plan globs**

Add `consensus_mcp/looper_plan/**/*.md`, `consensus_mcp/looper_plan/**/*.json`, and the skill `consensus_mcp/claude_extensions/skills/consensus-looper-plan/**/*` to the existing `[tool.setuptools.package-data]` (or `MANIFEST.in`/`[tool.setuptools.packages.find]` as the file already does it - match the existing pattern; do NOT introduce a new mechanism).

- [ ] **Step 3: Verify the build includes them**

Run: `.venv/bin/python -m build --wheel 2>/dev/null && unzip -l dist/*.whl | grep -E 'looper_plan|consensus-looper-plan' | head`
Expected: rubrics, schemas, and the skill appear in the wheel.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build(looper-plan): ship looper_plan rubrics/schemas + skill in package-data"
```

---

## Task 11: The `consensus-looper-plan` wizard skill

**Files:**
- Create: `consensus_mcp/claude_extensions/skills/consensus-looper-plan/SKILL.md`

- [ ] **Step 1: Author the SKILL.md** (no TDD - it is a host-followed procedure)

Adapt upstream `SKILL.md` (staged evidence) to:
- Frontmatter `name: consensus-looper-plan`, `disable-model-invocation: true`, `argument-hint: "<goal-id>"`, description framing it as the Build design coach.
- Workflow: (1) resolve `.consensus/architect/<goal-id>/`; (2) call `seed.assert_safe_to_coach(goal_dir)` first and STOP on `ReCoachRefused`; (3) re-read `.consensus/config.yaml` for `roles:`; (4) interview goal -> verification -> gates/control, loading `looper_plan/rubrics/<stage>-rubric.md` per stage; (5) build the coached dict, call `compile.synthesize_stub_fields(coached, roles)`, write `looper-plan/loop.yaml`; (6) `compile.compile_plan()` -> write `looper-plan/loop.resolved.json` + `looper-plan/LOOP.md`; show the ASCII preview labeled "design preview - NOT the Build supervisor loop"; (7) `seed.seed_build_inputs(resolved, goal_dir)`; (8) present `looper-suggestions.yaml` via the two-tier `apply-looper-suggestions` confirm (AskUserQuestion: execution contract explicit; caps pre-filled but confirmed; "accept all" option); (9) on confirm, write the chosen values into `.consensus/config.yaml` `architect_loop:`; (10) hand off: "Run `consensus-mcp-architect step --goal-dir <goal-id>`."
- File Rules: argv arrays not shell strings; never re-litigate model choice; never write config without the confirm step.

- [ ] **Step 2: Sanity-check the helper invocations resolve**

Run: `.venv/bin/python -c "from consensus_mcp.looper_plan import compile_plan, synthesize_stub_fields, seed_build_inputs; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add consensus_mcp/claude_extensions/skills/consensus-looper-plan/SKILL.md
git commit -m "feat(looper-plan): consensus-looper-plan wizard skill (coach -> seed Build)"
```

---

## Task 12: Launch toggle + docs

**Files:**
- Modify: `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`
- Modify: `docs/workflows/architect-build.md`

- [ ] **Step 1: Add the toggle to the consensus-workflow skill**

Under the Build launch guidance, add: when launching Consensus Build, present an AskUserQuestion - "Design with a Looper plan first (design coach), or go direct (Build execution loop)?" With -> invoke `consensus-looper-plan <goal-id>` then `step`. Without -> today's flow. State the toggle lives only at goal-setup, never in `loop_step`.

- [ ] **Step 2: Document the option in architect-build.md**

In "### 2. Set up a goal", add a subsection "Optional: design with a Looper plan" describing the with/without choice, the coach->seed flow, the suggest+confirm seam, the write-once-immutable artifacts inside the goal dir, and the zero-diff guarantee. Cross-link the spec.

- [ ] **Step 3: Full suite**

Run: `.venv/bin/python -m pytest consensus_mcp/tests -q`
Expected: all green (the new looper_plan tests + the existing architect suite unchanged). Document any pre-existing flake.

- [ ] **Step 4: Commit**

```bash
git add consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md docs/workflows/architect-build.md
git commit -m "docs(looper-plan): launch toggle in consensus-workflow + architect-build option"
```

---

## Final verification

- [ ] Full suite green: `.venv/bin/python -m pytest consensus_mcp/tests -q`
- [ ] `git grep -i looper consensus_mcp/_architect_lane.py consensus_mcp/tools/architect_loop_step.py` returns NOTHING (zero diff).
- [ ] The decisive snapshot guard (Task 8) is green against unmodified lane code.
- [ ] No version action - operator controls the release cut.
