# Design spec: `consensus init` workspace-umbrella guard

- **Date:** 2026-05-23
- **Status:** Approved (4-AI consensus consult - codex + gemini + kimi + host-peer, **unanimous** Q1a-Q5a)
- **Iteration:** `consensus-state/active/iteration-umbrella-guard-design-2026-05-23`
- **Consult provenance:** codex `codex-ug-1-pass1` (`37aef3c6...`), gemini
  `gemini-ug-1-pass1` (`ed346fc1...`), kimi `kimi-ug-1-pass1` (`f9e84320...`),
  host-peer (blind Claude subagent).

## Problem (the incident)

`consensus-init --from-claude-code` run from `/home/user/projects` - a
workspace **umbrella** folder (not a git repo, containing `consensus-mcp` +
`external-project`) - bootstrapped the umbrella: wrote `.consensus/config.yaml`
(`project.name: projects`), `.mcp.json`, `.claude/agents/`, and seeded
`CLAUDE.md`/`AGENTS.md`/`GEMINI.md` at the umbrella. Since `CLAUDE.md` is read
hierarchically by Claude Code, those consensus instructions now blanket every
sub-project. Almost certainly unintended.

**Root cause (verified):** `_detect_repo_root` precedence is (1) `git rev-parse`
(fails - umbrella not a repo); (2) walk up for a STRONG marker
(`.git`/`pyproject.toml`/`package.json`/`CLAUDE.md`/`.mcp.json`/`consensus-state`);
(3) **fall back to cwd**. A non-repo workspace folder containing git repos
resolves to itself and gets bootstrapped - with no "this is a parent workspace"
guard.

## Approach (converged, unanimous - a deliberate twin of the v1.29.0 already-configured gate)

Add a guard so `consensus init` does not silently bootstrap a workspace umbrella.

### Q1a - Detection signal
A directory is a "workspace umbrella" iff: the resolved root is **NOT itself a
git repo** (i.e. `git rev-parse --show-toplevel` did not return it) **AND** >=1 of
its **immediate child directories contains a `.git` *directory*** (a real repo).
- A `.git` **file** (submodule / linked-worktree gitlink) does NOT count - so a
  project that has submodules is never misflagged.
- A normal repo-root (git rev-parse returns it) and a monorepo (root IS a repo)
  never trigger. Zero qualifying children -> never triggers.

### Q2a - Action + non-TTY UX
- **TTY:** interactive confirm - "This looks like a workspace folder containing N
  git projects, not a project itself. Initialize here anyway? [y/N]" (default No).
- **Non-TTY** (`--from-claude-code` / CI / pipe): do NOT bootstrap. Print a
  stable token as the first stdout line - `STATUS: looks-like-workspace-umbrella`
  (DISTINCT from `ALREADY_CONFIGURED_TOKEN`) - plus human guidance on stderr
  (naming the child projects found, capped at ~10) - and **exit 8**. The
  `consensus` skill detects the token and presents an `AskUserQuestion`: pick one
  of the enumerated child projects to init, or re-run with `--here` to init the
  umbrella anyway. One-shot (the override flag suppresses the token, so no loop).

### Q3a - Override
A **dedicated flag** `--here` (bypasses the umbrella guard; deliberately init the
current directory even though it looks like a workspace). Do NOT overload
`--force` (means "overwrite existing config") or `--reconfigure`.

### Q4a - Scan + safety
- **Immediate child directories only** (a workspace has repos as direct
  children). Cheap, deterministic; no recursion (avoids monorepo-subpackage false
  positives + cost).
- For the boolean decision, short-circuit on the first qualifying child.
- For the enumerated guidance, list qualifying children separately, **capped at
  ~10**.
- **Robust iteration:** catch `PermissionError`/`OSError` per entry (treat an
  unreadable child as "no `.git`"); do NOT follow symlinks when testing for the
  `.git` directory.

### Q5a - Placement
A separate guard in `cmd_init`, AFTER `_detect_repo_root` resolves the root and
BEFORE any write. Keep `_detect_repo_root` a pure detector (it's the intended
landing spot for the pending hook/self-drive unification - do not add policy
there).

## Exit-code & gate interaction (Q6 - unanimous)

- **Exit 8** (codes 0-7 are all in use: 0 ok / 1 abort / 2 missing / 3 invalid /
  4 already-configured / 5 managed-skip / 6 incomplete-install / 7
  repair-incomplete). 8 = workspace-umbrella refused.
- **The guard SKIPS when a config already exists, and for `--reconfigure` /
  `--repair` / `--check`.** Those operate on an EXISTING config - and crucially,
  re-running `consensus init` to *clean up* a contaminated umbrella must remain
  possible. So:
  - config exists -> the v1.29.0 already-configured path (token/menu), guard skipped.
  - `--reconfigure`/`--repair`/`--check` -> guard skipped.
  - fresh init (no config) + umbrella detected + no `--here` -> THIS guard (exit 8).
- **`--config <path>`:** the guard keys on the auto-detected ROOT, not the config
  path. `--config` redirects only the write target; it does NOT bypass the guard.
  The single escape hatch is `--here`. (Documented decision.)
- **Nested case:** an umbrella that is itself inside a parent git repo -> `git
  rev-parse` returns the parent repo root, so the guard does not fire. Acceptable
  (that's a root-policy question, not umbrella detection).

## Components & files in scope

- **Modify** `consensus_mcp/_init_wizard.py`:
  - Add `WORKSPACE_UMBRELLA_TOKEN = "STATUS: looks-like-workspace-umbrella"`.
  - Add `_looks_like_workspace_umbrella(root) -> list[Path]` (returns qualifying
    child repo dirs; empty = not an umbrella) - immediate-children, `.git`-dir,
    symlink/permission-safe.
  - Add the TTY confirm helper (or reuse the existing prompt style).
  - Add `--here` to the arg parser.
  - Add the guard branch in `cmd_init` (post-detect, pre-write, fresh-init only).
- **Modify** `consensus_mcp/claude_extensions/skills/consensus/SKILL.md` +
  `commands/consensus-init.md`: a carve-out - on `STATUS:
  looks-like-workspace-umbrella` + exit 8, present an AskUserQuestion enumerating
  the child projects (cap ~10) + a "init here anyway (`--here`)" option; re-invoke
  one-shot.
- **Create** `consensus_mcp/tests/test_init_wizard_umbrella.py` - detection unit
  tests + the gate integration + contract regression test.
- **Modify** `consensus_mcp/_init_wizard.py` module docstring exit-code legend
  (add `8  workspace umbrella (refused; pass --here to override)`).
- **Modify** `CHANGELOG.md`.

## Testing plan (TDD)

1. **Detection** (`_looks_like_workspace_umbrella`): umbrella (non-repo dir with
   2 child `.git`-dirs) -> returns the children; a child with a `.git` **file**
   (submodule) -> NOT counted; zero child repos -> empty; an unreadable child
   (PermissionError) -> treated as no-`.git`, no crash; a symlinked child -> not
   followed.
2. **Non-TTY gate:** fresh init in an umbrella (mock `_stdin_is_interactive`->False)
   -> stdout line 1 == `WORKSPACE_UMBRELLA_TOKEN`, child projects listed on stderr,
   exit 8, NOTHING written.
3. **`--here` bypass:** umbrella + `--here` -> no token, proceeds to bootstrap
   (exit 0); token NOT emitted.
4. **TTY confirm:** umbrella + TTY, mock input "n"/"" -> exit 8/abort no-write;
   input "y" -> proceeds.
5. **Skip paths:** umbrella + existing config -> already-configured path (NOT the
   umbrella token); `--reconfigure`/`--repair`/`--check` in an umbrella -> guard
   skipped.
6. **Non-umbrella unaffected:** a normal git repo + a non-repo single project (no
   child repos) -> guard never fires; existing init tests stay green.
7. **Contract test:** the exact `WORKSPACE_UMBRELLA_TOKEN` appears in SKILL.md +
   `consensus-init.md`; exit-8 documented; distinct from `ALREADY_CONFIGURED_TOKEN`.
8. Full suite green.

## Out of scope (follow-ups)

- Recursive/deep umbrella detection (repos nested >1 level) - Q4a accepts the
  immediate-children trade-off.
- Auto-cleanup of an already-contaminated umbrella - separate (the operator runs
  the cleanup; `--reconfigure`/manual removal).
- Unifying `_detect_repo_root` with the gate/self-drive copy - noted in its
  docstring, not this scope.

## Risks (from the panel)

1. **Token/exit-code drift** between CLI and skill matcher - mitigated by the
   contract regression test + a shared token constant.
2. **False positives** - mitigated by Q1a's tight conjunction (non-repo AND child
   `.git`-dir) + the `--here` escape + TTY confirm.
3. **Placement assumption** - if any path writes before `_detect_repo_root`
   returns, the guard must move earlier (codex); verified `cmd_init` resolves root
   before writes.
4. **Symlink/permission edge** - per-entry try/except, no symlink-follow (Q4a).
