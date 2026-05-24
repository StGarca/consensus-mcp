# Workspace-umbrella guard for `consensus init` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `consensus init` from silently bootstrapping a workspace *umbrella* folder (a non-repo directory containing git repos); detect it and steer the operator to a real project, with a deliberate `--here` override.

**Architecture:** A pure detector `_looks_like_workspace_umbrella(root)` + a guard in `cmd_init` (after `_detect_repo_root`, before any write) that mirrors the v1.29.0 already-configured gate: TTY → interactive confirm; non-TTY → `STATUS: looks-like-workspace-umbrella` token + exit 8, which the `consensus` skill turns into an AskUserQuestion. The guard runs on fresh init only (skipped when a config exists or for `--reconfigure`/`--repair`/`--check`).

**Tech Stack:** Python 3.11+ (stdlib), pytest.

**Spec:** `docs/superpowers/specs/2026-05-23-umbrella-guard-design.md` (authoritative).

**Test runner:** `VPY=/home/user/.local/share/pipx/venvs/consensus-mcp/bin/python` → `$VPY -m pytest …` from repo root. **Do NOT edit `build/lib/`.**

**Verified facts:** exit codes 0–7 are all used (0 ok/1 abort/2 missing/3 invalid/4 already-configured/5 skip/6 incomplete-install/7 repair-incomplete) → use **8**. `cmd_init` resolves `repo_root = _detect_repo_root()` (L1891) and `config_path` (L1892), then early-returns for `--check`/`--print-defaults`/`--install/uninstall-claude-code`/`--repair`, then `--force`>`--reconfigure` normalization, then the existing-config gate. The umbrella guard goes after the early-return cluster, before the existing-config gate.

---

## File structure
- **Modify** `consensus_mcp/_init_wizard.py`: `WORKSPACE_UMBRELLA_TOKEN` constant; `_looks_like_workspace_umbrella(root)` detector; `--here` arg; the `cmd_init` guard; module-docstring exit-code legend (+8).
- **Create** `consensus_mcp/tests/test_init_wizard_umbrella.py` — detector + gate + contract tests.
- **Modify** `consensus_mcp/claude_extensions/skills/consensus/SKILL.md` + `commands/consensus-init.md` — the umbrella carve-out.
- **Modify** `CHANGELOG.md`.

---

### Task 1: `WORKSPACE_UMBRELLA_TOKEN` + `_looks_like_workspace_umbrella` detector

**Files:** Modify `consensus_mcp/_init_wizard.py` (near the other init constants/helpers, e.g. above `_prompt_existing_config_action`). Test: `consensus_mcp/tests/test_init_wizard_umbrella.py` (new).

- [ ] **Step 1: Write the failing tests** — create `consensus_mcp/tests/test_init_wizard_umbrella.py`:

```python
from pathlib import Path

import consensus_mcp._init_wizard as wiz


def _make_repo(d: Path):
    """A directory that looks like a git repo (has a .git DIRECTORY)."""
    d.mkdir(parents=True, exist_ok=True)
    (d / ".git").mkdir()
    return d


def test_token_constant_value():
    assert wiz.WORKSPACE_UMBRELLA_TOKEN == "STATUS: looks-like-workspace-umbrella"
    assert wiz.WORKSPACE_UMBRELLA_TOKEN != wiz.ALREADY_CONFIGURED_TOKEN


def test_umbrella_with_child_repos_detected(tmp_path):
    _make_repo(tmp_path / "proj-a")
    _make_repo(tmp_path / "proj-b")
    (tmp_path / "not-a-repo").mkdir()
    children = wiz._looks_like_workspace_umbrella(tmp_path)
    names = sorted(c.name for c in children)
    assert names == ["proj-a", "proj-b"]


def test_root_is_a_repo_is_not_umbrella(tmp_path):
    (tmp_path / ".git").mkdir()          # root itself is a repo (monorepo case)
    _make_repo(tmp_path / "sub")
    assert wiz._looks_like_workspace_umbrella(tmp_path) == []


def test_child_dot_git_FILE_does_not_count(tmp_path):
    # a .git FILE = submodule/worktree gitlink, NOT a repo dir
    (tmp_path / "with-submodule").mkdir()
    (tmp_path / "with-submodule" / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    assert wiz._looks_like_workspace_umbrella(tmp_path) == []


def test_zero_child_repos_is_not_umbrella(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")
    assert wiz._looks_like_workspace_umbrella(tmp_path) == []


def test_symlinked_child_not_followed(tmp_path):
    real = _make_repo(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    children = wiz._looks_like_workspace_umbrella(tmp_path)
    # only the real dir counts; the symlink is skipped
    assert [c.name for c in children] == ["real"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_umbrella.py -q`
Expected: FAIL — `WORKSPACE_UMBRELLA_TOKEN` / `_looks_like_workspace_umbrella` undefined.

- [ ] **Step 3: Implement** — add to `consensus_mcp/_init_wizard.py` (near `ALREADY_CONFIGURED_TOKEN`):

```python
# v1.29.x (umbrella-guard consult): stdout-line-1 contract token for "this looks
# like a workspace folder, not a project". DISTINCT from ALREADY_CONFIGURED_TOKEN.
# The consensus skill keys on it (+ exit 8) to present an AskUserQuestion.
WORKSPACE_UMBRELLA_TOKEN = "STATUS: looks-like-workspace-umbrella"


def _looks_like_workspace_umbrella(root: Path) -> list[Path]:
    """Return `root`'s immediate child directories that are git repos (contain a
    `.git` DIRECTORY), IFF `root` itself is NOT a git repo. Empty list = not an
    umbrella.

    A `.git` *file* (submodule / linked-worktree gitlink) does NOT count — so a
    project that vendors submodules is never misflagged. Per-entry errors
    (PermissionError/OSError) are swallowed; symlinked children are not followed.
    """
    if (root / ".git").exists():
        # root is itself a repo (dir) or a linked worktree (file) → a project.
        return []
    try:
        entries = sorted(root.iterdir())
    except (PermissionError, OSError):
        return []
    children: list[Path] = []
    for child in entries:
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            if (child / ".git").is_dir():
                children.append(child)
        except (PermissionError, OSError):
            continue
    return children
```

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_umbrella.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard_umbrella.py
git commit -m "feat(init): workspace-umbrella token + detector (immediate-child .git dirs)"
```

---

### Task 2: `--here` flag + the `cmd_init` guard + exit-code legend

**Files:** Modify `consensus_mcp/_init_wizard.py` (arg parser, `cmd_init` body, module docstring). Test: append to `consensus_mcp/tests/test_init_wizard_umbrella.py`.

- [ ] **Step 1: Write the failing tests** — append:

```python
import builtins
import consensus_mcp.config as cfg
import yaml


def _seed_umbrella(tmp_path):
    """tmp_path becomes a workspace umbrella with 2 child git repos."""
    _make_repo(tmp_path / "alpha")
    _make_repo(tmp_path / "beta")
    return tmp_path


def test_nontty_umbrella_emits_token_exit_8_no_write(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _seed_umbrella(tmp_path)
    rc = wiz.main([])
    assert rc == 8
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == wiz.WORKSPACE_UMBRELLA_TOKEN
    assert "alpha" in captured.err and "beta" in captured.err
    assert not (tmp_path / ".consensus").exists()  # nothing written


def test_here_flag_bypasses_guard(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _seed_umbrella(tmp_path)
    rc = wiz.main(["--here", "--non-interactive", "--accept-defaults",
                   "--contributors", "claude,codex,gemini"])
    assert rc == 0
    assert wiz.WORKSPACE_UMBRELLA_TOKEN not in capsys.readouterr().out
    assert (tmp_path / ".consensus" / "config.yaml").exists()


def test_tty_umbrella_confirm_no_aborts_8(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: "n")
    _seed_umbrella(tmp_path)
    rc = wiz.main([])
    assert rc == 8
    assert not (tmp_path / ".consensus").exists()


def test_tty_umbrella_confirm_yes_proceeds(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    # "y" to the umbrella confirm, then defaults for the rest of the wizard
    answers = iter(["y"] + [""] * 12)
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: next(answers))
    _seed_umbrella(tmp_path)
    rc = wiz.main([])
    assert rc == 0
    assert (tmp_path / ".consensus" / "config.yaml").exists()


def test_guard_skipped_when_config_exists(tmp_path, capsys, monkeypatch):
    """An already-bootstrapped umbrella routes to the v1.29.0 already-configured
    path (so you can re-run to clean it up) — NOT the umbrella token."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _seed_umbrella(tmp_path)
    (tmp_path / ".consensus").mkdir()
    (tmp_path / ".consensus" / "config.yaml").write_text(
        yaml.safe_dump(cfg.default_config()), encoding="utf-8")
    rc = wiz.main([])
    out = capsys.readouterr().out
    assert wiz.WORKSPACE_UMBRELLA_TOKEN not in out
    assert out.splitlines()[0] == wiz.ALREADY_CONFIGURED_TOKEN  # already-configured wins
    assert rc == 4


def test_non_umbrella_fresh_init_unaffected(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    # no child repos → not an umbrella → normal non-TTY fresh init
    rc = wiz.main(["--non-interactive", "--accept-defaults",
                   "--contributors", "claude,codex,gemini"])
    assert rc == 0
    assert wiz.WORKSPACE_UMBRELLA_TOKEN not in capsys.readouterr().out
    assert (tmp_path / ".consensus" / "config.yaml").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_umbrella.py -q`
Expected: FAIL — `--here` unknown arg; no umbrella guard (the umbrella tests bootstrap instead of refusing).

- [ ] **Step 3: Implement**

(a) Add the `--here` arg near `--reconfigure`/`--force` in `build_parser`:

```python
    parser.add_argument("--here", action="store_true",
                        help="initialize the current directory even if it looks "
                             "like a workspace folder containing other git projects")
```

(b) In `cmd_init`, insert the guard AFTER the early-return cluster
(`--check`/`--print-defaults`/`--install/uninstall-claude-code`/`--repair`) and
BEFORE the existing-config gate (i.e. right before the `# --force supersedes
--reconfigure` normalization, or immediately before `if config_path.exists() and
not (...)`):

```python
    # v1.29.x (umbrella-guard consult): don't silently bootstrap a workspace
    # umbrella (a non-repo dir containing git repos). Fresh-init only — skip when
    # a config already exists (-> already-configured path, so re-running to clean
    # up an umbrella still works) and for --reconfigure (a maintenance op).
    # --check/--repair already returned above. --here is the deliberate override.
    if (not getattr(args, "here", False)
            and not args.reconfigure
            and not config_path.exists()):
        umbrella_children = _looks_like_workspace_umbrella(repo_root)
        if umbrella_children:
            listed = ", ".join(c.name for c in umbrella_children[:10])
            more = ("" if len(umbrella_children) <= 10
                    else f" (+{len(umbrella_children) - 10} more)")
            guidance = (
                f"{repo_root} looks like a workspace folder containing git "
                f"projects ({listed}{more}), not a project itself. Re-run "
                f"`consensus init` inside the project you want, or pass --here to "
                f"initialize this directory anyway."
            )
            non_interactive = (args.non_interactive or args.accept_defaults
                               or not _stdin_is_interactive())
            if non_interactive:
                print(WORKSPACE_UMBRELLA_TOKEN)  # stdout line 1, exact, no variable data
                print(guidance, file=sys.stderr)
                return 8
            try:
                ans = input(
                    f"{repo_root} looks like a workspace folder containing git "
                    f"projects ({listed}{more}), not a project. Initialize here "
                    f"anyway? [y/N]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\naborted by user", file=sys.stderr)
                return 1
            if ans not in ("y", "yes"):
                print("aborted: not initializing a workspace umbrella "
                      "(pass --here to override).", file=sys.stderr)
                return 8
            # confirmed → fall through to normal init
```

(c) Update the module-docstring exit-code legend (near the top, after the `4  …`
line) — add:

```python
#  8  looks like a workspace umbrella (refused; pass --here to override)
```

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_umbrella.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard_umbrella.py
git commit -m "feat(init): workspace-umbrella guard (--here override; TTY confirm / non-TTY token+exit8)"
```

## After committing
Run the FULL suite `$VPY -m pytest consensus_mcp/tests/ -q` and report total — confirm no regression (esp. existing init tests; the guard must not fire on normal repos / single projects). `git show --stat HEAD` = only 2 files.

---

### Task 3: Skill + command carve-out + contract test

**Files:** Modify `consensus_mcp/claude_extensions/skills/consensus/SKILL.md`, `consensus_mcp/claude_extensions/commands/consensus-init.md`. Test: append to `consensus_mcp/tests/test_init_wizard_umbrella.py`. **(Do NOT edit `build/lib/`.)**

- [ ] **Step 1: Write the failing contract test** — append:

```python
def _ext_dir():
    return Path(wiz.__file__).parent / "claude_extensions"


def test_umbrella_token_documented_in_skill_and_command():
    skill = (_ext_dir() / "skills" / "consensus" / "SKILL.md").read_text(encoding="utf-8")
    command = (_ext_dir() / "commands" / "consensus-init.md").read_text(encoding="utf-8")
    assert wiz.WORKSPACE_UMBRELLA_TOKEN in skill
    assert wiz.WORKSPACE_UMBRELLA_TOKEN in command
    assert "--here" in skill and "--here" in command
    # exit 8 documented in the skill, distinct from already-configured (exit 4)
    assert "exit code 8" in skill.lower() or "exits with code 8" in skill.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_umbrella.py::test_umbrella_token_documented_in_skill_and_command -q`
Expected: FAIL.

- [ ] **Step 3: Implement** — in `consensus_mcp/claude_extensions/skills/consensus/SKILL.md`, add a new section immediately AFTER the "If the project is already configured" section (and before `## What NOT to do`):

```markdown
## If it looks like a workspace folder (not a project)

If `consensus-init` exits with **code 8** AND the first line of its stdout is
exactly `STATUS: looks-like-workspace-umbrella`, the current directory is a
*workspace folder containing git projects*, not a project — bootstrapping it
would blanket every sub-project. Do NOT surface the raw error. Instead:

1. Consume (do not display) the token line; the stderr guidance names the child
   projects found.
2. Present these options via `AskUserQuestion`:
   - One option per child project found (enumerate them from the stderr list,
     **capped at ~10**) — "Initialize `<that project>`" → re-run
     `consensus-init --from-claude-code` from inside that project directory.
   - **Initialize here anyway** → re-run `consensus-init --from-claude-code --here`.
   - **Cancel** → stop; nothing was written.
3. Act on the choice one-shot (the project dir / `--here` suppresses the token, so
   no loop).
```

Then update the existing `## What NOT to do` exception bullet to mention this is a
second carve-out (the bullet that already notes the already-configured exception)
— append: "and the workspace-umbrella carve-out (exit code 8 + the
`STATUS: looks-like-workspace-umbrella` token)."

In `consensus_mcp/claude_extensions/commands/consensus-init.md`, add a concise
paragraph: if the binary exits code 8 with first stdout line
`STATUS: looks-like-workspace-umbrella`, the cwd is a workspace folder — don't
surface the raw error; offer to init one of the listed child projects, or re-run
with `--here` to init the current directory anyway (one-shot).

- [ ] **Step 4: Run to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_umbrella.py -q`
Expected: PASS. Then run `$VPY -m pytest consensus_mcp/tests/test_vendored_skill_references.py -q` (the namespace/path reference-integrity guard) — must stay green (use no `superpowers:` refs and no `dir/file.md` paths in the new section).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/claude_extensions/skills/consensus/SKILL.md consensus_mcp/claude_extensions/commands/consensus-init.md consensus_mcp/tests/test_init_wizard_umbrella.py
git commit -m "feat(skill): workspace-umbrella carve-out + contract test"
```

---

### Task 4: CHANGELOG

**Files:** Modify `CHANGELOG.md`.

- [ ] **Step 1: Replace the `## 1.29.3 - unreleased` stub body** with:

```markdown
## 1.29.3 - unreleased

**`consensus init` guards against bootstrapping a workspace folder.** Running
init in a *non-repo directory that contains git projects* (a workspace umbrella)
used to silently bootstrap the umbrella — blanketing every sub-project via the
hierarchical `CLAUDE.md`. It now detects that and steers you to a real project.

### Added
- Workspace-umbrella guard: when the resolved root is not a git repo but has ≥1
  immediate child git repo, `consensus init` refuses by default. In a terminal it
  asks for confirmation; under Claude Code / CI it exits **8** with a stable
  `STATUS: looks-like-workspace-umbrella` token (the `consensus` skill turns it
  into a pick-a-project menu). Pass **`--here`** to initialize the current
  directory anyway. The guard is skipped when a config already exists and for
  `--reconfigure`/`--repair`/`--check` (so re-running to clean up still works).
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): workspace-umbrella guard (1.29.3)"
```

---

### Task 5: Full-suite verification

- [ ] **Step 1: Full suite** — Run: `$VPY -m pytest consensus_mcp/tests/ -q` — expect all pass (baseline 1466 + new umbrella tests).
- [ ] **Step 2: Manual smoke (branch code)** — from a temp umbrella (`mkdir -p /tmp/umb/{a,b}/.git`), run `cd /tmp/umb && printf '' | PYTHONPATH=<repo> $VPY -m consensus_mcp._init_wizard`; expect first stdout line `STATUS: looks-like-workspace-umbrella`, child names on stderr, exit 8, no `.consensus/` written. Then `--here` → bootstraps.
- [ ] **Step 3: Non-umbrella regression** — same in a temp single dir (no child `.git`) → normal non-TTY init (exit 0, writes config).
- [ ] **Step 4: `build/lib` untouched + report** — `git diff --stat <base> HEAD | grep -c build/lib` → 0; artifact-scoped completion report.

---

## Self-review (against the spec)

- **Q1a detection** → Task 1 `_looks_like_workspace_umbrella` (non-repo root + child `.git` dir); submodule/zero-child/symlink/perm tested. ✓
- **Q2a action/non-TTY** → Task 2 guard: TTY confirm / non-TTY token + exit 8 + child list; skill AskUserQuestion (Task 3). ✓
- **Q3a `--here` override** → Task 2 arg + bypass; `test_here_flag_bypasses_guard`. ✓
- **Q4a immediate-children, `.git`-dir-not-file, symlink/perm-safe** → Task 1 detector + its tests. ✓
- **Q5a placement in cmd_init post-detect/pre-write** → Task 2 (b). ✓
- **Exit 8 + distinct token** → Task 1 constant (`!= ALREADY_CONFIGURED_TOKEN`), Task 2 return 8, legend updated. ✓
- **Skip when config exists / `--reconfigure`** → Task 2 guard condition + `test_guard_skipped_when_config_exists`; `--check`/`--repair` early-return above. ✓
- **child enumeration capped ~10** → Task 2 `[:10]` + `(+N more)`. ✓
- **contract test (token in both docs + exit 8)** → Task 3. ✓
- **reference-integrity stays green** → Task 3 Step 4 runs `test_vendored_skill_references.py` (no `superpowers:`/`dir/file.md` in the new section). ✓
- No placeholders; names consistent (`WORKSPACE_UMBRELLA_TOKEN`, `_looks_like_workspace_umbrella`, `--here`). Out-of-scope (recursive detection, auto-cleanup, detector unification) correctly absent.
