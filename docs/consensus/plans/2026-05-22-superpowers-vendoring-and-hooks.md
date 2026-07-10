# Superpowers Vendoring + Consensus Hook Enforcement - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make consensus-mcp self-contained (no Superpowers prerequisite) by vendoring 10 MIT Superpowers skills (adapted to hand off to consensus) and shipping a Claude Code hook layer that deterministically makes the workflow defer to consensus instead of barreling ahead.

**Architecture:** Two independent tracks meeting at a *marker contract*. Track A copies+adapts 10 skills into `consensus_mcp/claude_extensions/skills/consensus-<name>/` and installs them via the existing `_install_claude_extensions` path. Track B ships `hooks.json` (SessionStart/UserPromptSubmit/PreToolUse/Stop) reusing the existing `consensus_mcp/_delivery_readiness.py` token system + `contrib/delivery_gate_pretooluse.py` block pattern. The contract: vendored `brainstorming` writes `.consensus/design-approved`; vendored `verification-before-completion` mints a delivery token; the PreToolUse/Stop hooks validate them.

**Tech Stack:** Python 3 (run via the project interpreter), pytest, Claude Code plugin hooks (JSON), markdown SKILL.md files. Distribution: git tags + pipx + `consensus-init --install-claude-code` (NO PyPI, NO new channel).

**Provenance:** Superpowers v5.1.0, MIT (Jesse Vincent / github.com/obra/superpowers), pinned commit `f2cbfbe...`. Source skills at `~/.claude/plugins/cache/claude-plugins-official/superpowers/5.1.0/skills/<name>/SKILL.md`.

**Source of truth:** the two converged plans -
`consensus-state/active/iteration-superpowers-vendor-skill-selection-2026-05-22/converged-plan.yaml` (skill set + adaptation) and
`consensus-state/active/iteration-superpowers-fork-consensus-integration-2026-05-22/converged-plan.yaml` (hook design).

---

## File Structure

**Track A - vendored skills (new):**
- `consensus_mcp/claude_extensions/skills/consensus-<name>/SKILL.md` x 10 - each a copied+adapted Superpowers skill; each retains the MIT header.
- `consensus_mcp/claude_extensions/NOTICE` - MIT attribution (Jesse Vincent / obra).
- `consensus_mcp/claude_extensions/VENDORED.md` - provenance ledger (skill, source v5.1.0+SHA, exact change).
- `consensus_mcp/_init_wizard.py` - extend `_CLAUDE_EXTENSION_FILES` (modify; currently ~line 124).

**Track B - hooks (new):**
- `consensus_mcp/claude_extensions/hooks/hooks.json` - the manifest.
- `consensus_mcp/claude_extensions/hooks/consensus_pretooluse_gate.py` - design gate (generalize `contrib/delivery_gate_pretooluse.py`).
- `consensus_mcp/claude_extensions/hooks/consensus_stop_gate.py` - verification soft-gate.
- `consensus_mcp/claude_extensions/hooks/consensus_sessionstart.py` - precedence injector + runtime probe.
- `consensus_mcp/_design_approval.py` - `.consensus/design-approved` mint/verify (parallel to `_delivery_readiness.py`).

**Shared/contract:**
- `.consensus/design-approved` marker schema (written by Track A brainstorming, validated by Track B PreToolUse).
- Existing `consensus_mcp/_delivery_readiness.py` token (written by Track A verification, validated by Track B Stop). Do NOT modify.

**Tests:** `consensus_mcp/tests/test_vendored_skills_install.py`, `consensus_mcp/tests/test_design_approval.py`, `consensus_mcp/tests/test_consensus_hooks.py`.

> **Parallelization:** Track A and Track B share no source files except the read-only marker/token contract. Dispatch them as two concurrent subagents. Track B's `_design_approval.py` (Task B1) defines the marker schema; Track A Task A3 (brainstorming adaptation) only needs the *path + field names* from B1 - pass those as a fixed contract string so the tracks don't block each other.

### Marker contract (fixed - both tracks code against this)
`.consensus/design-approved` (YAML): `{iteration_id, scope_glob, converged_plan_sha256, sealed_at_utc}`. Valid iff: file parses, `converged_plan_sha256` matches a sealed converged-plan, and the edited path matches `scope_glob`. A single-Claude-only marker (no cross-family seal in the referenced iteration) is ADVISORY - the PreToolUse gate treats it as NOT approved.

---

## Track B - Hook Enforcement

### Task B1: design-approval marker mint/verify

**Files:**
- Create: `consensus_mcp/_design_approval.py`
- Test: `consensus_mcp/tests/test_design_approval.py`

- [ ] **Step 1: Write failing tests**

```python
# test_design_approval.py
from pathlib import Path
import yaml
from consensus_mcp import _design_approval as da

def test_verify_rejects_missing_marker(tmp_path):
    assert da.verify_design_approval(tmp_path/"src/x.py", repo_root=tmp_path).ok is False

def test_mint_then_verify_in_scope(tmp_path):
    (tmp_path/".consensus").mkdir(parents=True)
    da.mint_design_approval(repo_root=tmp_path, iteration_id="it1",
        scope_glob="src/**", converged_plan_sha256="abc", cross_family_sealed=True)
    assert da.verify_design_approval(tmp_path/"src/x.py", repo_root=tmp_path).ok is True

def test_verify_rejects_out_of_scope(tmp_path):
    (tmp_path/".consensus").mkdir(parents=True)
    da.mint_design_approval(repo_root=tmp_path, iteration_id="it1",
        scope_glob="src/**", converged_plan_sha256="abc", cross_family_sealed=True)
    assert da.verify_design_approval(tmp_path/"docs/y.md", repo_root=tmp_path).ok is False

def test_single_claude_marker_is_advisory_only(tmp_path):
    (tmp_path/".consensus").mkdir(parents=True)
    da.mint_design_approval(repo_root=tmp_path, iteration_id="it1",
        scope_glob="src/**", converged_plan_sha256="abc", cross_family_sealed=False)
    assert da.verify_design_approval(tmp_path/"src/x.py", repo_root=tmp_path).ok is False
```

- [ ] **Step 2: Run tests, verify they fail** - `.../python -m pytest consensus_mcp/tests/test_design_approval.py -q` -> FAIL (module missing).
- [ ] **Step 3: Implement `_design_approval.py`** - a `Result(ok, reason)` dataclass; `mint_design_approval(...)` writes `.consensus/design-approved` (YAML with the marker-contract fields + `cross_family_sealed`); `verify_design_approval(target_path, repo_root)` loads it, rejects if missing/unparseable/`cross_family_sealed False`, and `fnmatch`-matches the repo-relative target against `scope_glob`. Fail-closed on any error.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit** - `git add consensus_mcp/_design_approval.py consensus_mcp/tests/test_design_approval.py && git commit -m "feat(consensus): design-approval marker mint/verify"`

### Task B2: PreToolUse design gate

**Files:**
- Create: `consensus_mcp/claude_extensions/hooks/consensus_pretooluse_gate.py` (start from `contrib/delivery_gate_pretooluse.py`)
- Test: `consensus_mcp/tests/test_consensus_hooks.py` (PreToolUse cases)

- [ ] **Step 1: Write failing tests** - feed the hook a fake PreToolUse event JSON on stdin (`{"tool_name":"Edit","tool_input":{"file_path":"src/x.py"},"cwd":"<tmp>"}`); assert: (a) no marker -> exit code 2 (deny) with a reason mentioning consensus; (b) valid in-scope marker -> exit 0 (allow); (c) read-only tool (`Read`/`Grep`) -> exit 0 always; (d) consensus runtime absent (no `consensus-init` on PATH, simulate) -> exit 0 (FAIL-OPEN); (e) file-modifying Bash (`sed -i`, `>` redirect, `git commit`) with no marker -> exit 2; read-only Bash (`ls`,`git status`) -> exit 0.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** - parse stdin event; if runtime absent -> exit 0; if tool in `{Edit,Write,MultiEdit,NotebookEdit}` -> require `verify_design_approval(file_path)` else exit 2; if tool == `Bash` -> classify command (conservative regex for `sed -i`, `tee`, `>`/`>>`, `mv`,`cp`,`rm`, `git commit|tag|push`, release/deploy) -> require marker else exit 2; read-only -> exit 0. Block via exit code 2 + stderr reason (the verified pattern).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit.**

### Task B3: Stop verification soft-gate

**Files:** Create `consensus_mcp/claude_extensions/hooks/consensus_stop_gate.py`; extend `test_consensus_hooks.py`.

- [ ] **Step 1: Failing tests** - (a) git-modified source file lacking a delivery token -> hook emits a blocking-directive context string naming the file; (b) all modified files tokenized -> empty/no directive; (c) runtime absent -> no-op (fail-open). NOTE: soft gate (context injection), since a hard Stop deny is unverified.
- [ ] **Step 2-4:** Implement: `git diff --name-only HEAD`; for each modified source file call `_delivery_readiness.verify_delivery_token`; if any fails, print the directive. Run tests.
- [ ] **Step 5: Commit.**

### Task B4: SessionStart precedence injector + UserPromptSubmit nudge

**Files:** Create `consensus_mcp/claude_extensions/hooks/consensus_sessionstart.py`; extend tests.

- [ ] **Step 1: Failing tests** - runtime present -> outputs JSON with `hookSpecificOutput.additionalContext` containing the precedence mapping (brainstorming->Workflow A; requesting/receiving-code-review->Workflow B; verification->sealed gate; Edit/Write blocked until `.consensus/design-approved`); runtime absent -> outputs a benign "consensus not installed; plain workflow" notice.
- [ ] **Step 2-4:** Implement probe (`shutil.which("consensus-init")`) + emit the two branches. Run tests.
- [ ] **Step 5: Commit.**

### Task B5: hooks.json manifest + degradation integration test

**Files:** Create `consensus_mcp/claude_extensions/hooks/hooks.json`; extend tests.

- [ ] **Step 1:** Write `hooks.json` registering SessionStart, UserPromptSubmit, PreToolUse (matcher `Edit|Write|MultiEdit|NotebookEdit|Bash`), Stop -> the four scripts.
- [ ] **Step 2:** Integration test: with runtime simulated-absent, ALL gates are no-ops (assert plain-workflow behavior). With runtime present + no marker, PreToolUse denies.
- [ ] **Step 3: Commit.**

---

## Track A - Vendoring

### Task A1: scaffold NOTICE + VENDORED.md + dir layout

**Files:** Create `consensus_mcp/claude_extensions/NOTICE`, `consensus_mcp/claude_extensions/VENDORED.md`.

- [ ] **Step 1:** Write `NOTICE` with the MIT license text + "Portions vendored from Superpowers (c) 2025 Jesse Vincent, github.com/obra/superpowers, v5.1.0 @ f2cbfbe..., used under MIT."
- [ ] **Step 2:** Write `VENDORED.md` table header: `| skill | source path | version | sha | adaptation |`.
- [ ] **Step 3: Commit.**

### Task A2: vendor the 6 near-verbatim skills

**Files:** Create `consensus_mcp/claude_extensions/skills/consensus-{writing-plans,executing-plans,subagent-driven-development,test-driven-development,finishing-a-development-branch,using-git-worktrees}/SKILL.md`.

For EACH of the 6:
- [ ] **Step 1:** Copy the source SKILL.md verbatim.
- [ ] **Step 2:** Apply the mechanical adaptation: (a) prepend the MIT header comment; (b) replace every `superpowers:<x>` reference with `consensus:<x>`; (c) repoint any `docs/superpowers/...` path to `docs/consensus/...`; (d) update the frontmatter `name:` to `consensus-<name>`; (e) add a 1-line precedence header: "> Consensus has precedence at decision gates; see the consensus bootstrap.".
- [ ] **Step 3:** Add a VENDORED.md row (adaptation = "verbatim + ref-rewrite").
- [ ] **Step 4: Commit** (one commit per skill or one batch commit).

### Task A3: adapt `consensus-brainstorming` (spine)

**Files:** Create `consensus_mcp/claude_extensions/skills/consensus-brainstorming/SKILL.md`.

- [ ] **Step 1:** Copy brainstorming SKILL.md + MIT header + frontmatter rename + ref-rewrite (as A2).
- [ ] **Step 2:** Replace the terminal approval gate: change "present the design and get **user** approval" / "invoke writing-plans" to: "Hand off to a consensus Workflow A consult (see consensus-workflow). The **converged-plan IS the approval** - consensus is the approver, not the user. On convergence, mint the `.consensus/design-approved` marker (`{iteration_id, scope_glob, converged_plan_sha256, sealed_at_utc}` - see Track B contract) and then invoke `consensus:writing-plans`."
- [ ] **Step 3:** VENDORED.md row (adaptation = "spine: approval->Workflow A + marker mint"). Commit.

### Task A4: adapt `consensus-requesting-code-review` (spine)

- [ ] **Step 1:** Copy + header + rename + ref-rewrite.
- [ ] **Step 2:** Replace the single-Claude reviewer-subagent dispatch with: "Invoke Workflow B - `reviewer_dispatch_codex` (and `_gemini`/`_kimi` per panel size); the review is the sealed cross-family audit, not a single-Claude pass." Keep the git-range diff prep.
- [ ] **Step 3:** VENDORED.md row. Commit.

### Task A5: adapt `consensus-receiving-code-review` (spine)

- [ ] **Step 1:** Copy + header + rename + ref-rewrite.
- [ ] **Step 2:** Reframe "evaluate review feedback" to "weigh the **sealed consensus panel** findings on merit; record any dismissal with empirical evidence (per the consensus 'verify peer-cited content' rule)."
- [ ] **Step 3:** VENDORED.md row. Commit.

### Task A6: adapt `consensus-verification-before-completion` (spine)

- [ ] **Step 1:** Copy + header + rename + ref-rewrite.
- [ ] **Step 2:** Add to the gate: "Before any completion claim, **mint/verify a delivery-readiness token** (`consensus_mcp/_delivery_readiness.py`) + run `gate_evaluate_production_with_scope_match`. This token is what the consensus Stop hook checks." Keep the "evidence before claims" iron law.
- [ ] **Step 3:** VENDORED.md row. Commit.

### Task A7: wire install path + test

**Files:** Modify `consensus_mcp/_init_wizard.py` (`_CLAUDE_EXTENSION_FILES`); Create `consensus_mcp/tests/test_vendored_skills_install.py`.

- [ ] **Step 1: Failing test** - assert `_CLAUDE_EXTENSION_FILES` includes all 10 `skills/consensus-*/SKILL.md` + `NOTICE` + `VENDORED.md` + the 4 `hooks/*` entries; and that `_install_claude_extensions` copies them into a temp CLAUDE_HOME.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** - extend the `_CLAUDE_EXTENSION_FILES` tuple with the 10 skills + NOTICE + VENDORED.md + hooks.
- [ ] **Step 4: Run, verify pass** + run the FULL suite (`.../python -m pytest consensus_mcp/tests/ -q`) for regressions.
- [ ] **Step 5: Commit.**

### Task A8: coexistence + skip-set documentation

**Files:** Create `docs/consensus/vendoring.md`.

- [ ] **Step 1:** Document: the 10 vendored skills + adaptations; the SKIP set (dispatching-parallel-agents, writing-skills, systematic-debugging[optional]) + rationale; coexistence (precedence hook wins when upstream Superpowers also installed); the manual re-sync procedure (VENDORED.md + pinned SHA + health-check).
- [ ] **Step 2: Commit.**

---

## Self-Review notes
- Spec coverage: Track A = converged vendor plan (10 skills, adapt spine, namespace, NOTICE/VENDORED.md, install path). Track B = converged hook plan (4 events, marker, fail-open, soft-Stop). Covered.
- Marker contract is defined once and referenced by both tracks (type consistency: `verify_design_approval`, `.consensus/design-approved` fields, `_delivery_readiness` token).
- No PyPI/new channel. MIT attribution retained. Cross-family-seal check preserves the closure invariant (single-Claude marker = advisory).

## Out of scope (do not touch)
- `consensus_mcp/_delivery_readiness.py`, `contrib/delivery_gate_pretooluse.py` (reuse, don't modify).
- Hard fork; requiring Superpowers; the SKIP-set skills.
- The uncommitted kimi dispatcher (separate change; its own Workflow B audit).
