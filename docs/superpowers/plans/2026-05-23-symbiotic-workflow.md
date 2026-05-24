# Symbiotic superpowers + consensus pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire kimi into the engine (so the 4-AI panel runs through `consensus.run_iteration`) and codify the end-to-end superpowers↔consensus iteration pipeline (dual-path, release-currency, host_peer dispatch) so it's session-independently repeatable.

**Architecture:** (A) Add `KimiAdapter` (a near-verbatim mirror of `CodexAdapter`, wrapping `_dispatch_kimi.main`) and register it as a built-in so `enabled:[…,kimi]` works. (B/C) Capture the dual-path consult doctrine, release-currency steps, and host_peer dispatch procedure in the existing `consensus-workflow` skill + a `docs/workflows/iteration-pipeline.md` runbook, guarded by a contract test. No new runtime beyond KimiAdapter; the static-echo fix is an explicit out-of-scope follow-up.

**Tech Stack:** Python 3.11+ (stdlib), pytest.

**Spec:** `docs/superpowers/specs/2026-05-23-symbiotic-workflow-design.md` (authoritative).

**Test runner:** `VPY=/home/user/.local/share/pipx/venvs/consensus-mcp/bin/python` → `$VPY -m pytest …` from repo root. **Do NOT edit `build/lib/`.**

**Key verified facts:**
- `phase_to_mode` (`consensus_mcp/contributors/_phase_mode.py`) maps `PROPOSE→"proposal"`, `REVIEW→"review"`, `CONVERGE→"review"`. kimi's `--mode` accepts `{review, proposal}` (same as codex) — so `phase_to_mode` never emits a kimi-invalid mode.
- `_dispatch_kimi.main(argv)` accepts the same flags as `_dispatch_codex.main` (`--goal-packet --iteration-dir --reviewer-id --pass-id --timeout-seconds --mode --review-target`) and emits the same JSON envelope (`ok/pass_id/sealed_path/archive_sealed_path/packet_sha256`).

---

## File structure
- **Create** `consensus_mcp/contributors/kimi.py` — `KimiAdapter` (mirror of `codex.py`).
- **Modify** `consensus_mcp/_engine_factory.py` — import + register `"kimi": KimiAdapter`.
- **Create** `consensus_mcp/tests/test_kimi_adapter.py` — adapter-level tests.
- **Modify** `consensus_mcp/tests/test_engine_factory.py` — `enabled:[…,kimi]` builds `KimiAdapter`.
- **Modify** `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md` — pipeline section + dual-path + host_peer procedure + `.consensus` caveat + release-currency steps.
- **Create** `docs/workflows/iteration-pipeline.md` — the runbook.
- **Create** `consensus_mcp/tests/test_workflow_skill_currency.py` — contract test on the SKILL.md additions.
- **Modify** `CHANGELOG.md`.

---

### Task 1: `KimiAdapter`

**Files:** Create `consensus_mcp/contributors/kimi.py`; Test: `consensus_mcp/tests/test_kimi_adapter.py`.

- [ ] **Step 1: Write the failing tests** — create `consensus_mcp/tests/test_kimi_adapter.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from consensus_mcp.contributors.base import (
    DispatchError,
    DispatchPacket,
    PHASE_CONVERGE,
    PHASE_PROPOSE,
    PHASE_REVIEW,
)


def _packet(phase, tmp_path):
    return DispatchPacket(
        phase=phase, contributor="kimi", iteration_dir=tmp_path,
        goal_packet_path=tmp_path / "goal.yaml", review_target_path=None,
        reviewer_id="kimi-test-1", pass_id="kimi-test-1-pass1", timeout_seconds=600,
    )


def _fake_main(captured, tmp_path, *, ok=True, rc=0, non_json=False):
    sealed = tmp_path / "kimi-sealed.yaml"
    def main(argv):
        captured.extend(argv)
        sealed.write_text("findings: []\ngoal_satisfied: true\nblocking_objections: []\n", encoding="utf-8")
        if non_json:
            print("not json at all")
        else:
            print(json.dumps({"ok": ok, "pass_id": "kimi-test-1-pass1",
                              "sealed_path": str(sealed), "archive_sealed_path": None,
                              "packet_sha256": "0" * 64}))
        return rc
    return main


def test_kimi_adapter_propose_forwards_mode_proposal(monkeypatch, tmp_path):
    cap = []
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main(cap, tmp_path))
    from consensus_mcp.contributors.kimi import KimiAdapter
    art = KimiAdapter().dispatch(_packet(PHASE_PROPOSE, tmp_path))
    assert cap[cap.index("--mode") + 1] == "proposal"
    assert art.contributor == "kimi"
    assert art.parsed["goal_satisfied"] is True


def test_kimi_adapter_review_forwards_mode_review(monkeypatch, tmp_path):
    cap = []
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main(cap, tmp_path))
    from consensus_mcp.contributors.kimi import KimiAdapter
    KimiAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))
    assert cap[cap.index("--mode") + 1] == "review"


def test_kimi_adapter_converge_forwards_mode_review(monkeypatch, tmp_path):
    """CONVERGE -> 'review' (kimi-valid). Guards the iter-0044 bug class:
    a kimi-INVALID mode like 'converge' would SystemExit -> DispatchError."""
    cap = []
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main(cap, tmp_path))
    from consensus_mcp.contributors.kimi import KimiAdapter
    KimiAdapter().dispatch(_packet(PHASE_CONVERGE, tmp_path))
    assert cap[cap.index("--mode") + 1] == "review"


def test_kimi_adapter_non_json_stdout_raises_dispatcherror(monkeypatch, tmp_path):
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main([], tmp_path, non_json=True))
    from consensus_mcp.contributors.kimi import KimiAdapter
    with pytest.raises(DispatchError, match="non-JSON"):
        KimiAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))


def test_kimi_adapter_rc_nonzero_raises_dispatcherror(monkeypatch, tmp_path):
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main([], tmp_path, ok=False, rc=1))
    from consensus_mcp.contributors.kimi import KimiAdapter
    with pytest.raises(DispatchError, match="kimi dispatch failed"):
        KimiAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))


def test_kimi_adapter_returns_sealed_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr("consensus_mcp._dispatch_kimi.main", _fake_main([], tmp_path))
    from consensus_mcp.contributors.kimi import KimiAdapter
    art = KimiAdapter().dispatch(_packet(PHASE_REVIEW, tmp_path))
    assert art.pass_id == "kimi-test-1-pass1"
    assert art.sealed_path.name == "kimi-sealed.yaml"
    assert art.packet_sha256 == "0" * 64
```

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_kimi_adapter.py -q`
Expected: FAIL — `ModuleNotFoundError`/`ImportError` for `consensus_mcp.contributors.kimi`.

- [ ] **Step 3: Implement** — create `consensus_mcp/contributors/kimi.py` (verbatim mirror of `codex.py`, swapping `codex`→`kimi`):

```python
"""Kimi contributor adapter — wraps consensus_mcp._dispatch_kimi.

Mirror of contributors/codex.py: normalizes _dispatch_kimi's packet→argv
translation + result extraction into the ContributorAdapter interface, so kimi
is a first-class built-in (not a generic ProfileAdapter cli_reviewer) and keeps
_dispatch_kimi's hardened behavior (env-scrub, exit-75 retry, disposable
workdir, integrity check). Phase→mode via the shared _phase_mode.phase_to_mode
(CONVERGE→"review", which kimi's --mode {review,proposal} accepts).
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from consensus_mcp.contributors.base import (
    ContributorAdapter,
    DispatchError,
    DispatchPacket,
    SealedArtifact,
)


class KimiAdapter(ContributorAdapter):
    """Kimi contributor — subprocess via _dispatch_kimi.main."""

    name = "kimi"

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        reviewer_id = packet.reviewer_id or f"kimi-{packet.iteration_dir.name}-{packet.phase}-1"
        pass_id = packet.pass_id or f"{reviewer_id}-pass1"
        from consensus_mcp.contributors._phase_mode import phase_to_mode
        mode = phase_to_mode(packet.phase)
        argv = [
            "--goal-packet", str(packet.goal_packet_path),
            "--iteration-dir", str(packet.iteration_dir),
            "--reviewer-id", reviewer_id,
            "--pass-id", pass_id,
            "--timeout-seconds", str(packet.timeout_seconds),
            "--mode", mode,
        ]
        if packet.review_target_path is not None:
            argv += ["--review-target", str(packet.review_target_path)]

        from consensus_mcp import _dispatch_kimi

        buf = io.StringIO()
        rc = 0
        with contextlib.redirect_stdout(buf):
            try:
                rc = _dispatch_kimi.main(argv) or 0
            except SystemExit as exc:
                raise DispatchError(f"kimi argparse SystemExit: {exc.code!r}") from exc
            except Exception as exc:
                raise DispatchError(
                    f"kimi dispatch failed ({type(exc).__name__}): {exc}"
                ) from exc

        output = buf.getvalue().strip()
        try:
            parsed_result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise DispatchError(
                f"kimi dispatch returned non-JSON stdout: {exc}; sample: {output[:200]!r}"
            ) from exc

        if rc != 0 or not parsed_result.get("ok"):
            raise DispatchError(
                f"kimi dispatch failed: rc={rc}, "
                f"error={parsed_result.get('error')!r}, "
                f"error_type={parsed_result.get('error_type')!r}"
            )

        try:
            sealed_path_str = parsed_result["sealed_path"]
        except KeyError as exc:
            raise DispatchError(
                f"kimi dispatch returned no sealed_path: {parsed_result!r}"
            ) from exc
        sealed_path = Path(sealed_path_str)
        try:
            import yaml
            sealed = yaml.safe_load(sealed_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise DispatchError(
                f"kimi sealed artifact unreadable at {sealed_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(sealed, dict):
            raise DispatchError(
                f"kimi sealed artifact at {sealed_path} is not a YAML mapping; "
                f"got {type(sealed).__name__}"
            )

        return SealedArtifact(
            contributor=self.name,
            phase=packet.phase,
            pass_id=parsed_result["pass_id"],
            sealed_path=sealed_path,
            archive_sealed_path=Path(parsed_result["archive_sealed_path"])
                if parsed_result.get("archive_sealed_path") else None,
            packet_sha256=parsed_result.get("packet_sha256", ""),
            parsed=sealed,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_kimi_adapter.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/contributors/kimi.py consensus_mcp/tests/test_kimi_adapter.py
git commit -m "feat(engine): KimiAdapter wrapping _dispatch_kimi (mirrors CodexAdapter)"
```

---

### Task 2: Register kimi as a built-in adapter

**Files:** Modify `consensus_mcp/_engine_factory.py`; Test: `consensus_mcp/tests/test_engine_factory.py`.

- [ ] **Step 1: Write the failing test** — append to `consensus_mcp/tests/test_engine_factory.py`:

```python
def test_build_adapters_kimi_is_builtin_kimiadapter():
    """Default panel enables kimi with no profile; it must build a KimiAdapter
    (the hardened built-in), NOT a ProfileAdapter and NOT a build failure."""
    from consensus_mcp import _engine_factory
    from consensus_mcp.contributors.kimi import KimiAdapter
    adapters = _engine_factory.build_adapters(
        enabled=["claude", "codex", "gemini", "kimi"],
        profiles={},
    )
    assert isinstance(adapters["kimi"], KimiAdapter)
```

NOTE: match `build_adapters`'s ACTUAL signature/kwargs by copying an existing call in `test_engine_factory.py` (it may take `claude_artifact_callback`/`host_peer_review_callback`/etc.). If `claude` requires a callback to build, pass the same stub the existing tests use, or restrict `enabled` to `["codex","gemini","kimi"]` and assert only `adapters["kimi"]`. Keep the assertion (kimi → `KimiAdapter`) intact.

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_engine_factory.py -k kimi -q`
Expected: FAIL — `EngineFactoryError` (no adapter for "kimi") or KeyError.

- [ ] **Step 3: Implement** — in `consensus_mcp/_engine_factory.py`:

Add the import alongside the others (after the `gemini` import):
```python
from consensus_mcp.contributors.kimi import KimiAdapter
```
Add to `_BUILTIN_ADAPTERS`:
```python
_BUILTIN_ADAPTERS: dict[str, type[ContributorAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiAdapter,
    "kimi": KimiAdapter,
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_engine_factory.py -q && $VPY -m pytest consensus_mcp/tests/test_kimi_adapter.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_engine_factory.py consensus_mcp/tests/test_engine_factory.py
git commit -m "feat(engine): register kimi as a built-in adapter (4-AI panel works in-engine)"
```

---

### Task 3: Codify the pipeline + release-currency + host_peer procedure in `consensus-workflow`

**Files:** Modify `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`; Test: create `consensus_mcp/tests/test_workflow_skill_currency.py`. **(Do NOT edit `build/lib/`.)**

- [ ] **Step 1: Write the failing contract test** — create `consensus_mcp/tests/test_workflow_skill_currency.py`:

```python
from pathlib import Path

import consensus_mcp._init_wizard as wiz


def _skill_text():
    p = (Path(wiz.__file__).parent / "claude_extensions" / "skills"
         / "consensus-workflow" / "SKILL.md")
    return p.read_text(encoding="utf-8")


def test_release_currency_steps_documented():
    """The release cut-sequence MUST codify the install-currency steps that
    otherwise live only in operator memory (stale-pipx failure mode)."""
    t = _skill_text().lower()
    assert "pipx install --force" in t
    assert "--install-claude-code --force" in t
    # version-asserting smoke (not just 'binary runs')
    assert "version" in t and "smoke" in t


def test_dual_path_and_host_peer_documented():
    t = _skill_text()
    assert "Path A" in t and "Path B" in t
    assert "host_peer_review_yaml" in t
    assert "run_iteration" in t


def test_consensus_gate_caveat_documented():
    """The runbook must flag that the .consensus enforcement gate is project-
    scoped (inactive where there's no .consensus/config.yaml)."""
    t = _skill_text()
    assert ".consensus" in t
```

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_workflow_skill_currency.py -q`
Expected: FAIL — the strings aren't in SKILL.md yet.

- [ ] **Step 3: Implement** — edit `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`:

(a) Add a new section (place it near the top, after the workflow-selection section). Insert verbatim:

```markdown
## End-to-end iteration pipeline (superpowers ↔ consensus)

One repeatable pipeline. Each superpowers stage maps to a consensus role:

1. `superpowers:brainstorming` → intent + design exploration.
2. **Consensus consult = the design approval gate** (consensus is the approver):
   author goal_packet + review-packet; run the panel; converge (weighted-synthesis).
3. `superpowers:writing-plans` → TDD implementation plan from the converged design.
4. `superpowers:subagent-driven-development` → implement task-by-task (implementer
   + spec-compliance review + code-quality review per task).
5. `superpowers:finishing-a-development-branch` → release cut (see "Release cadence").

### Dual-path consult selection
- **Path B — `consensus.run_iteration`** (the built engine dispatches
  codex/gemini/kimi via adapters + claude/host_peer via callbacks, runs
  blind-first-reveal, seals): use for **post-review / execution / clear-design /
  hot-patches**. claude + host_peer are supplied as `claude_proposal_yaml` /
  `host_peer_review_yaml` (static across rounds — acceptable here).
- **Path A — orchestrator-driven** (dispatch shell binaries + host-supplied blind
  subagents + manual weighted-synthesis): use for **propose-converge with real
  design surface**, where claude/host_peer must genuinely re-converge across
  rounds. KEPT as the documented advanced path; its sole justification is genuine
  multi-round host convergence. (Known follow-up: per-round host re-convergence in
  Path B needs a `run_iteration` pause/resume API — not yet built.)
- **host_peer is first-class in BOTH paths.**

### host_peer dispatch procedure (repeatable, not improvised)
1. Dispatch a **blind** Claude subagent (fresh context, NO peer artifacts) as the
   host_peer reviewer, using `dispatch_templates/host_peer_review_template.md`.
2. Capture its output as `host_peer_review_yaml` with this schema:
   ```yaml
   findings: []            # list
   goal_satisfied: true    # bool
   blocking_objections: [] # list
   ```
3. Path B: pass it as `consensus.run_iteration(..., host_peer_review_yaml=<yaml>)`.
   Path A: seal it via the host_peer path. One-shot; never loop.

### `.consensus` gate caveat
The PreToolUse enforcement gate (seal `.consensus/design-approved`, mint a
delivery token) is **project-scoped**: it only fires where a `.consensus/config.yaml`
exists. In a repo without one (e.g. consensus-mcp itself dogfooding via shell
binaries), those seal/mint steps are **aspirational** — follow the discipline by
convention, or activate the gate via `consensus init` to make it enforced.

### Concurrency warning (4-AI engine runs)
Do NOT mutate the repo (edits, or concurrent subagent writes) while a
`run_iteration` engine run is dispatching kimi — kimi's integrity check shells
`git status` and false-positives on concurrent changes, spuriously rejecting the
review.
```

(b) In the **"Release cadence"** cut-sequence, after the step that publishes the
GitHub Release / fast-forwards main, add these steps (the install-currency steps —
the global pipx install is a non-editable COPY, so without them the operator's CLI
stays on the old tag):

```markdown
- **Make the release LIVE locally (install-currency — REQUIRED):**
  1. `pipx install --force git+https://github.com/StGarca/consensus-mcp.git@vX.Y.Z`
     (the global install is a non-editable COPY pinned to a tag; `pipx upgrade`
     will NOT move a tag pin).
  2. `consensus-init --install-claude-code --force` (refresh `~/.claude`
     skills/commands to the new version).
  3. **Smoke the INSTALLED binary AND assert its version == `vX.Y.Z`** — the
     stale-pipx failure is a binary that *runs* but reports the OLD version, so a
     "binary runs" check is insufficient.
```

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_workflow_skill_currency.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md consensus_mcp/tests/test_workflow_skill_currency.py
git commit -m "docs(workflow): codify iteration pipeline + dual-path + host_peer + release-currency"
```

---

### Task 4: The `docs/workflows/iteration-pipeline.md` runbook

**Files:** Create `docs/workflows/iteration-pipeline.md`.

- [ ] **Step 1: Create the runbook** — write `docs/workflows/iteration-pipeline.md`:

```markdown
# Iteration pipeline: superpowers ↔ consensus (repeatable runbook)

A session-independent walkthrough of one iteration. The load-bearing detail lives
in the `consensus-workflow` skill; this is the linear checklist.

## Stages
1. **Brainstorm** (`superpowers:brainstorming`) — explore intent → design.
2. **Consult = approval gate** — consensus is the approver. Author goal_packet +
   review-packet; run the panel (offer panel size 2/3/4 + framing anchored/open);
   converge (weighted-synthesis).
3. **Plan** (`superpowers:writing-plans`) — TDD plan from the converged design.
4. **Implement** (`superpowers:subagent-driven-development`) — implementer +
   spec-compliance review + code-quality review per task.
5. **Finish** (`superpowers:finishing-a-development-branch`) — release cut.

## Choosing the consult path
- **Path B (`consensus.run_iteration`)** — execution / post-review / clear-design /
  hot-patches. Engine dispatches codex/gemini/kimi + claude/host_peer callbacks.
- **Path A (orchestrator-driven)** — high-design-surface propose-converge needing
  genuine multi-round host re-convergence. host_peer first-class in both.

## host_peer
Dispatch a blind Claude subagent (fresh context, no peer artifacts) with
`host_peer_review_template.md`; capture `findings/goal_satisfied/blocking_objections`
YAML; feed `run_iteration` (Path B) or seal it (Path A). One-shot.

## Release-currency (after tag + GitHub Release + main FF)
1. `pipx install --force git+…@vX.Y.Z` (global install is a non-editable COPY).
2. `consensus-init --install-claude-code --force` (refresh ~/.claude).
3. Smoke the INSTALLED binary and assert version == vX.Y.Z.

## Caveats
- `.consensus` enforcement gate is project-scoped (inactive without
  `.consensus/config.yaml`).
- Don't mutate the repo during a 4-AI `run_iteration` run (kimi integrity check).
- Known follow-up: per-round host re-convergence in Path B needs a `run_iteration`
  pause/resume API (static-echo limitation).
```

- [ ] **Step 2: Commit**

```bash
git add docs/workflows/iteration-pipeline.md
git commit -m "docs(workflow): add iteration-pipeline runbook"
```

---

### Task 5: CHANGELOG

**Files:** Modify `CHANGELOG.md`.

- [ ] **Step 1: Replace the `## 1.29.2 - unreleased` stub body** with:

```markdown
## 1.29.2 - unreleased

**4-AI consensus runs in-engine + the iteration pipeline is codified.**

### Added
- `KimiAdapter` (`contributors/kimi.py`), registered as a built-in, so
  `consensus.run_iteration` dispatches all four AIs (claude/codex/gemini/kimi)
  out of the box — kimi keeps its hardened `_dispatch_kimi` behavior instead of
  the generic `ProfileAdapter` path. (`ProfileAdapter` remains for user-defined
  `cli_reviewer`s.)
- `consensus-workflow` skill now codifies the end-to-end **iteration pipeline**
  (superpowers ↔ consensus), the **dual-path** consult-selection rule (Path B
  `run_iteration` for execution; Path A orchestrator-driven for high-design-surface),
  the **host_peer dispatch procedure**, the `.consensus` gate caveat, and the
  **release install-currency** steps (`pipx install --force` + `--install-claude-code
  --force` + version-asserting smoke). Mirrored in `docs/workflows/iteration-pipeline.md`.

### Known follow-up
- Per-round host re-convergence in Path B (the claude/host_peer callbacks echo one
  YAML across convergence rounds) needs a `run_iteration` pause/resume API; until
  then use Path A for design-surface convergence.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): KimiAdapter + codified iteration pipeline (1.29.2)"
```

---

### Task 6: Full-suite verification

- [ ] **Step 1: Full suite** — Run: `$VPY -m pytest consensus_mcp/tests/ -q` — expect all pass (baseline 1456 + new kimi/factory/skill tests).
- [ ] **Step 2: Engine smoke** — confirm `enabled:[claude,codex,gemini,kimi]` builds without a kimi profile:
  `$VPY -c "from consensus_mcp import _engine_factory as f; a=f.build_adapters(enabled=['codex','gemini','kimi'], profiles={}); from consensus_mcp.contributors.kimi import KimiAdapter; print('kimi ->', type(a['kimi']).__name__)"` → `kimi -> KimiAdapter`. (Match the real `build_adapters` kwargs.)
- [ ] **Step 3: `build/lib` untouched** — `git diff --stat <base> HEAD | grep -c build/lib` → 0.
- [ ] **Step 4: Report** — artifact-scoped (branch + that no tag is cut yet).

---

## Self-review (against the spec)

- **A — KimiAdapter (T1a)** → Task 1 (mirror of codex.py, `phase_to_mode`); converge→review tested (iter-0044 guard). ✓
- **A — register built-in (T3a)** → Task 2; `build_adapters` test asserts `KimiAdapter` not `ProfileAdapter`. ✓
- **ProfileAdapter retained for user cli_reviewers** → not touched; noted in CHANGELOG. ✓
- **B — dual-path (T2)** → Task 3 SKILL.md section + Task 4 runbook; static-echo recorded as follow-up (CHANGELOG + runbook). ✓
- **C — extend consensus-workflow (T4b)** → Task 3; runbook Task 4. ✓
- **C — release-currency (T5)** incl. version-asserting smoke → Task 3 (b) + contract test `test_release_currency_steps_documented`. ✓
- **C — host_peer procedure (T6a)** → Task 3 section + contract test `test_dual_path_and_host_peer_documented`. ✓
- **`.consensus` caveat** → Task 3 section + `test_consensus_gate_caveat_documented`. ✓
- **kimi concurrency warning** → Task 3 section. ✓
- **Phase→mode (iter-0044 guard)** → Task 1 `test_kimi_adapter_converge_forwards_mode_review`. ✓
- No placeholders; names consistent (`KimiAdapter`, `_BUILTIN_ADAPTERS`, the test names). The Task 2 build_adapters call + Task 6 smoke carry an explicit "match the real `build_adapters` kwargs" note (signature confirmation, not unresolved design).
- **Out of scope:** static-echo fix, host_peer helper, gate dogfooding, ProfileAdapter deprecation — all named follow-ups, no task. ✓
```
