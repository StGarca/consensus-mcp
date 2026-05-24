# Graceful "already installed" init UX — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-running `consensus init` on an already-bootstrapped project shows a clean choice (leave / reconfigure / force) instead of a raw exit-4 error — an interactive menu in a real terminal, a skill-presented menu under Claude Code.

**Architecture:** The existing-config guard in `_init_wizard.py` becomes install-aware: a TTY gets an interactive menu; a non-TTY (Claude Code / CI / pipe / explicit non-interactive) emits a stable, machine-detectable contract — `STATUS: already-configured` on stdout line 1 + human guidance on stderr + exit 4 — which the `consensus` skill detects to present its own `AskUserQuestion` menu and re-invoke once with the chosen flag.

**Tech Stack:** Python 3.11+ (stdlib only), pytest. Spec: `docs/superpowers/specs/2026-05-23-init-already-installed-ux-design.md`.

**Test runner (this repo):** the only interpreter with `consensus_mcp` + deps is the pipx venv:
```
VPY=/home/user/.local/share/pipx/venvs/consensus-mcp/bin/python
```
Run all init tests with: `$VPY -m pytest consensus_mcp/tests/ -k init -q`

**Do NOT hand-edit** `build/lib/consensus_mcp/...` — it is a generated build artifact.

---

## File structure

- **Modify** `consensus_mcp/_init_wizard.py`
  - Add module constant `ALREADY_CONFIGURED_TOKEN` (the contract token).
  - Add helper `_prompt_existing_config_action()` (the TTY menu).
  - Replace the existing-config guard (currently ~lines 1733–1739) with the install-aware branch.
  - Add `--force` > `--reconfigure` precedence normalization.
- **Modify** `consensus_mcp/claude_extensions/skills/consensus/SKILL.md` — the one carve-out from "surface verbatim".
- **Modify** `consensus_mcp/claude_extensions/commands/consensus-init.md` — mirror the carve-out.
- **Modify** `consensus_mcp/tests/test_init_wizard.py` — update 3 legacy tests to the new contract.
- **Create** `consensus_mcp/tests/test_init_wizard_already_configured.py` — helper unit tests, gate integration tests, and the mandatory contract regression test.
- **Modify** `CHANGELOG.md` — feature entry + verify/repair follow-up note (consult Q3c).

---

### Task 1: Contract token + interactive menu helper

**Files:**
- Modify: `consensus_mcp/_init_wizard.py` (add constant above `_stdin_is_interactive`, ~line 1602; add helper after it)
- Test: `consensus_mcp/tests/test_init_wizard_already_configured.py` (new)

- [ ] **Step 1: Write the failing unit tests for the menu helper**

Create `consensus_mcp/tests/test_init_wizard_already_configured.py`:

```python
import builtins
import pytest

import consensus_mcp._init_wizard as wiz


def _stub_input(values):
    it = iter(values)
    def _fake(prompt=""):
        return next(it)
    return _fake


def test_token_constant_value():
    # The contract token is a fixed string with no variable data.
    assert wiz.ALREADY_CONFIGURED_TOKEN == "STATUS: already-configured"


@pytest.mark.parametrize("raw,expected", [
    ("1", "leave"),
    ("", "leave"),
    ("2", "reconfigure"),
    ("3", "force"),
])
def test_prompt_existing_config_action_choices(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setattr(builtins, "input", _stub_input([raw]))
    assert wiz._prompt_existing_config_action(tmp_path / ".consensus" / "config.yaml") == expected


def test_prompt_existing_config_action_eof_defaults_to_leave(tmp_path, monkeypatch):
    def _eof(prompt=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", _eof)
    assert wiz._prompt_existing_config_action(tmp_path / "c.yaml") == "leave"


def test_prompt_existing_config_action_reprompts_on_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(builtins, "input", _stub_input(["x", "9", "3"]))
    assert wiz._prompt_existing_config_action(tmp_path / "c.yaml") == "force"


def test_prompt_existing_config_action_ctrl_c_propagates(tmp_path, monkeypatch):
    def _kbi(prompt=""):
        raise KeyboardInterrupt
    monkeypatch.setattr(builtins, "input", _kbi)
    with pytest.raises(KeyboardInterrupt):
        wiz._prompt_existing_config_action(tmp_path / "c.yaml")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'ALREADY_CONFIGURED_TOKEN'` / `_prompt_existing_config_action`.

- [ ] **Step 3: Add the constant and helper**

In `consensus_mcp/_init_wizard.py`, immediately **above** `def _stdin_is_interactive() -> bool:`:

```python
# v1.29.0 (init-already-installed-ux consult): the binary<->skill contract for
# "this project is already bootstrapped". Emitted as the FIRST line of stdout
# (no variable data) so the consensus skill can anchor on it without parsing
# prose. Paired with exit code 4. Keep this string in sync with the matcher in
# claude_extensions/skills/consensus/SKILL.md and commands/consensus-init.md
# (a contract regression test enforces this).
ALREADY_CONFIGURED_TOKEN = "STATUS: already-configured"
```

Immediately **after** the `_stdin_is_interactive` function, add:

```python
def _prompt_existing_config_action(config_path: Path) -> str:
    """Interactive menu shown when config already exists (TTY callers only).

    Returns "leave" | "reconfigure" | "force". Empty input and EOF/Ctrl-D both
    default to "leave" (the safe no-op). Ctrl-C propagates as KeyboardInterrupt
    (caller maps it to exit 1), matching the rest of the wizard.
    """
    print(f"consensus-mcp is already configured here: {config_path}")
    print("  [1] Leave as-is — no changes            (default)")
    print("  [2] Reconfigure — re-prompt, keep current settings as defaults, show diff")
    print("  [3] Force overwrite — discard local config edits, write fresh")
    while True:
        try:
            raw = input("Choose [1/2/3, default 1]: ").strip()
        except EOFError:
            return "leave"
        if raw in ("", "1"):
            return "leave"
        if raw == "2":
            return "reconfigure"
        if raw == "3":
            return "force"
        print("Please enter 1, 2, or 3.")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py -q`
Expected: PASS (all 8 cases).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard_already_configured.py
git commit -m "feat(init): add already-configured token + interactive menu helper"
```

---

### Task 2: Install-aware existing-config gate

**Files:**
- Modify: `consensus_mcp/_init_wizard.py` (the guard at ~lines 1733–1739)
- Modify: `consensus_mcp/tests/test_init_wizard.py` (legacy tests at ~lines 134, 162, 432)
- Test: `consensus_mcp/tests/test_init_wizard_already_configured.py` (append integration tests)

- [ ] **Step 1: Update the legacy tests to the new contract (they will fail)**

In `consensus_mcp/tests/test_init_wizard.py`, replace the body of `test_non_interactive_refuses_existing_without_reconfigure` (the two assert lines after `rc = wiz.main([...])`):

```python
    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == wiz.ALREADY_CONFIGURED_TOKEN
    assert "already configured" in captured.err.lower()
```

In `test_existing_config_guard_uses_config_override`, replace its two assert lines after `rc = wiz.main([...])`:

```python
    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == wiz.ALREADY_CONFIGURED_TOKEN
    assert "already configured" in captured.err.lower()
    assert str(alt) in captured.err
```

In the guard-runs-first test (~line 432, `rc = wiz.main([])` with the `_explode` input stub), add token assertions after `assert rc == 4`:

```python
    assert rc == 4
    out = capsys.readouterr().out
    assert out.splitlines()[0] == wiz.ALREADY_CONFIGURED_TOKEN
```

(That test must already import `wiz`; it does. The `_explode` input stub still must NOT be called — the non-TTY branch never prompts.)

- [ ] **Step 2: Append integration tests for the new gate**

Append to `consensus_mcp/tests/test_init_wizard_already_configured.py`:

```python
import yaml
import consensus_mcp.config as cfg


def _write_existing_config(tmp_path):
    d = tmp_path / ".consensus"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump(cfg.default_config()), encoding="utf-8")
    return d / "config.yaml"


def test_non_tty_existing_config_emits_token_and_exit_4(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _write_existing_config(tmp_path)
    rc = wiz.main([])
    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == wiz.ALREADY_CONFIGURED_TOKEN
    assert "already configured" in captured.err.lower()


def test_dry_run_existing_non_tty_emits_token(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _write_existing_config(tmp_path)
    rc = wiz.main(["--dry-run"])
    assert rc == 4
    assert capsys.readouterr().out.splitlines()[0] == wiz.ALREADY_CONFIGURED_TOKEN


def test_token_absent_when_reconfigure_flag(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _write_existing_config(tmp_path)
    rc = wiz.main(["--reconfigure", "--non-interactive", "--accept-defaults",
                   "--contributors", "claude,codex,gemini"])
    assert rc == 0
    assert wiz.ALREADY_CONFIGURED_TOKEN not in capsys.readouterr().out


def test_token_absent_when_force_flag(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    _write_existing_config(tmp_path)
    rc = wiz.main(["--force", "--non-interactive", "--accept-defaults",
                   "--contributors", "claude,codex,gemini"])
    assert rc == 0
    assert wiz.ALREADY_CONFIGURED_TOKEN not in capsys.readouterr().out


def test_tty_menu_leave_returns_0_without_writing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(wiz, "_prompt_existing_config_action", lambda _p: "leave")
    cfg_path = _write_existing_config(tmp_path)
    original = cfg_path.read_text(encoding="utf-8")
    rc = wiz.main([])
    assert rc == 0
    assert cfg_path.read_text(encoding="utf-8") == original  # untouched
    assert wiz.ALREADY_CONFIGURED_TOKEN not in capsys.readouterr().out


def test_tty_menu_force_overwrites(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(wiz, "_prompt_existing_config_action", lambda _p: "force")
    cfg_path = tmp_path / ".consensus" / "config.yaml"
    cfg_path.parent.mkdir()
    cfg_path.write_text("schema_version: 1\n# user edit\n", encoding="utf-8")
    rc = wiz.main(["--contributors", "claude,codex,gemini"])
    assert rc == 0
    assert "# user edit" not in cfg_path.read_text(encoding="utf-8")  # overwritten


def test_tty_menu_ctrl_c_returns_1(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: True)
    def _kbi(_p):
        raise KeyboardInterrupt
    monkeypatch.setattr(wiz, "_prompt_existing_config_action", _kbi)
    _write_existing_config(tmp_path)
    rc = wiz.main([])
    assert rc == 1
    assert "aborted by user" in capsys.readouterr().err
```

- [ ] **Step 3: Run the new + legacy tests to verify they fail**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py consensus_mcp/tests/test_init_wizard.py -q`
Expected: FAIL — the new integration tests and the 3 updated legacy tests fail because the gate still prints "error: ... already exists" with no token.

- [ ] **Step 4: Replace the gate with the install-aware branch**

In `consensus_mcp/_init_wizard.py`, replace this block:

```python
    if config_path.exists() and not (args.reconfigure or args.force):
        print(
            f"error: {config_path} already exists. Use --reconfigure to update "
            f"or --force to overwrite.",
            file=sys.stderr,
        )
        return 4
```

with:

```python
    # v1.29.0 (init-already-installed-ux consult): the existing-config guard is
    # install-AWARE. It still runs BEFORE we build/prompt so its disposition
    # wins over exit 1 (user abort) and exit 3 (invalid build). TTY -> menu;
    # non-TTY (Claude Code / CI / pipe / explicit non-interactive) -> the stable
    # machine-detectable contract (token on stdout line 1 + guidance on stderr)
    # + exit 4, which the consensus skill keys on. The token is emitted ONLY
    # here (never when --reconfigure/--force is set), so the skill's one-shot
    # re-invoke cannot loop.
    if config_path.exists() and not (args.reconfigure or args.force):
        non_interactive_run = (
            args.non_interactive or args.accept_defaults
            or not _stdin_is_interactive()
        )
        if non_interactive_run:
            print(ALREADY_CONFIGURED_TOKEN)  # stdout, first line, exact, no variable data
            print(
                f"consensus-mcp is already configured at {config_path}. "
                f"Re-run with --reconfigure to update (keeps current settings as "
                f"defaults) or --force to overwrite — or run "
                f"`consensus init --reconfigure` in an interactive terminal.",
                file=sys.stderr,
            )
            return 4
        try:
            action = _prompt_existing_config_action(config_path)
        except KeyboardInterrupt:
            print("\naborted by user", file=sys.stderr)
            return 1
        if action == "leave":
            print(f"Leaving existing configuration unchanged: {config_path}")
            return 0
        if action == "reconfigure":
            args.reconfigure = True
        else:  # "force"
            args.force = True
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py consensus_mcp/tests/test_init_wizard.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard.py consensus_mcp/tests/test_init_wizard_already_configured.py
git commit -m "feat(init): install-aware existing-config gate (TTY menu / non-TTY status token)"
```

---

### Task 3: `--force` beats `--reconfigure` (conflicting-flag precedence)

**Files:**
- Modify: `consensus_mcp/_init_wizard.py` (just before the gate block from Task 2)
- Test: `consensus_mcp/tests/test_init_wizard_already_configured.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `consensus_mcp/tests/test_init_wizard_already_configured.py`:

```python
def test_force_beats_reconfigure(tmp_path, capsys, monkeypatch):
    """Both flags together: --force wins (overwrite, no reconfigure diff)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wiz, "_stdin_is_interactive", lambda: False)
    cfg_path = tmp_path / ".consensus" / "config.yaml"
    cfg_path.parent.mkdir()
    cfg_path.write_text("schema_version: 1\n# user edit\n", encoding="utf-8")
    rc = wiz.main(["--force", "--reconfigure", "--non-interactive",
                   "--accept-defaults", "--contributors", "claude,codex,gemini"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# user edit" not in cfg_path.read_text(encoding="utf-8")  # overwritten
    assert "reconfigure diff" not in out  # reconfigure path suppressed
```

- [ ] **Step 2: Run it to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py::test_force_beats_reconfigure -q`
Expected: FAIL — the reconfigure diff is printed because both flags are honored.

- [ ] **Step 3: Add the precedence normalization**

In `consensus_mcp/_init_wizard.py`, immediately **before** the existing-config gate block (the `if config_path.exists() and not (...)` from Task 2), add:

```python
    # --force supersedes --reconfigure: a full overwrite has nothing to diff.
    if args.force and args.reconfigure:
        args.reconfigure = False
```

- [ ] **Step 4: Run it to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py::test_force_beats_reconfigure -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard_already_configured.py
git commit -m "feat(init): --force supersedes --reconfigure when both are passed"
```

---

### Task 4: Skill + command carve-out, with the mandatory contract test

**Files:**
- Modify: `consensus_mcp/claude_extensions/skills/consensus/SKILL.md`
- Modify: `consensus_mcp/claude_extensions/commands/consensus-init.md`
- Test: `consensus_mcp/tests/test_init_wizard_already_configured.py` (append the contract test)

- [ ] **Step 1: Write the failing contract regression test**

Append to `consensus_mcp/tests/test_init_wizard_already_configured.py`:

```python
from pathlib import Path


def _ext_dir():
    return Path(wiz.__file__).parent / "claude_extensions"


def test_contract_token_present_in_skill_and_command():
    """The skill/command matchers MUST stay in sync with the binary token.
    If the token string changes, these docs must change too — this test fails
    on drift, which is the whole point of the binary<->skill contract."""
    token = wiz.ALREADY_CONFIGURED_TOKEN
    skill = (_ext_dir() / "skills" / "consensus" / "SKILL.md").read_text(encoding="utf-8")
    command = (_ext_dir() / "commands" / "consensus-init.md").read_text(encoding="utf-8")
    assert token in skill, "SKILL.md must reference the exact already-configured token"
    assert token in command, "consensus-init.md must reference the exact token"
    # exit code 4 is the paired half of the contract — keep it documented too.
    assert "exit code 4" in skill.lower() or "exits with code 4" in skill.lower()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py::test_contract_token_present_in_skill_and_command -q`
Expected: FAIL — the token is not yet in SKILL.md / consensus-init.md.

- [ ] **Step 3: Add the carve-out to SKILL.md**

In `consensus_mcp/claude_extensions/skills/consensus/SKILL.md`, insert a new section immediately **before** `## What NOT to do`:

```markdown
## If the project is already configured

If `consensus-init` exits with **code 4** AND the first line of its stdout is
exactly `STATUS: already-configured`, the project is already bootstrapped. This
is the ONE case where you do **not** surface the output verbatim:

1. Consume (do not display) the `STATUS: already-configured` line. The stderr
   guidance after it is human-readable; you may show it.
2. Present these three options to the user via `AskUserQuestion`:
   - **Leave as-is** — already set up; do nothing.
   - **Reconfigure** — update settings, keeping current values as defaults.
   - **Force overwrite** — discard local config edits and write fresh.
3. Act on the choice **one-shot** (do NOT loop):
   - Leave → stop; tell the user nothing changed.
   - Reconfigure → run `consensus-init --from-claude-code --reconfigure` once.
   - Force overwrite → run `consensus-init --from-claude-code --force` once.

The resolving flag stops the token from re-firing, so there is no menu loop.
```

Then update the first bullet under `## What NOT to do` from:

```markdown
- Don't reimplement any of `consensus-init`'s logic. It writes
  `.consensus/config.yaml`, `.mcp.json`, and a `.gitignore` managed
  block — let the binary handle all of that.
```

to:

```markdown
- Don't reimplement any of `consensus-init`'s logic. It writes
  `.consensus/config.yaml`, `.mcp.json`, and a `.gitignore` managed
  block — let the binary handle all of that. (The ONE exception is the
  already-configured carve-out above: exit code 4 + the
  `STATUS: already-configured` token triggers the AskUserQuestion menu.)
```

- [ ] **Step 4: Add the carve-out to consensus-init.md**

In `consensus_mcp/claude_extensions/commands/consensus-init.md`, insert before the final "Do not reimplement" paragraph:

```markdown
**Already configured:** if the binary exits with code 4 and the first stdout
line is exactly `STATUS: already-configured`, the project is already set up. Do
not surface the raw error — consume that token line and present three options
via `AskUserQuestion` (leave as-is / reconfigure / force overwrite), then
re-invoke `consensus-init --from-claude-code --reconfigure` or `--force` once
(one-shot; "leave" does nothing).
```

- [ ] **Step 5: Run the contract test to verify it passes**

Run: `$VPY -m pytest consensus_mcp/tests/test_init_wizard_already_configured.py::test_contract_token_present_in_skill_and_command -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add consensus_mcp/claude_extensions/skills/consensus/SKILL.md consensus_mcp/claude_extensions/commands/consensus-init.md consensus_mcp/tests/test_init_wizard_already_configured.py
git commit -m "feat(skill): already-configured carve-out + contract regression test"
```

---

### Task 5: CHANGELOG entry + verify/repair follow-up note

**Files:**
- Modify: `CHANGELOG.md` (top of file)

- [ ] **Step 1: Add the entry**

Insert at the very top of `CHANGELOG.md`, above `## 1.28.1 - 2026-05-23` (the operator renumbers `1.29.0` vs a patch at release-cut time):

```markdown
## 1.29.0 - unreleased

**Graceful re-init when consensus is already configured.** Re-running
`consensus init` on a bootstrapped project no longer hard-errors with a raw
"Exit code 4". In a real terminal you get an interactive menu (`[1]` leave /
`[2]` reconfigure / `[3]` force); under Claude Code the `consensus` skill
presents the same three choices.

### Added
- The `_init_wizard` existing-config guard is now install-aware. TTY → an
  interactive leave/reconfigure/force menu. Non-TTY (`--from-claude-code`, CI,
  a pipe, or `--non-interactive`/`--accept-defaults`) → exit 4 plus a stable
  `STATUS: already-configured` token on the first line of stdout and
  human-readable guidance on stderr. The `consensus` skill and `consensus-init`
  command detect that contract and present an `AskUserQuestion` menu, then
  re-invoke once with `--reconfigure` or `--force`.
- `--force` now supersedes `--reconfigure` when both are passed (overwrite has
  nothing to diff).

### Known gap / follow-up
- "Leave as-is" is a pure no-op: it does NOT verify or repair a partially
  broken install (e.g. a missing `.mcp.json` or a dead enforcement hook). A
  future release will add an explicit "verify/repair" option. (4-AI consult
  Q3c — `iteration-init-already-installed-ux-2026-05-23`.)
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): graceful already-configured init UX (1.29.0) + verify/repair follow-up"
```

---

### Task 6: Full-suite verification

- [ ] **Step 1: Run the whole test suite**

Run: `$VPY -m pytest consensus_mcp/tests/ -q`
Expected: all pass (the v1.28.1 baseline was 1404 passed; this adds ~16 tests and updates 3).

- [ ] **Step 2: Manual smoke (non-TTY contract)**

Run: `printf '' | consensus-init` in a directory that already has `.consensus/config.yaml`.
Expected: first stdout line is exactly `STATUS: already-configured`; guidance on stderr; `echo $?` prints `4`.

- [ ] **Step 3: Manual smoke (resolving flag suppresses token)**

Run: `printf '' | consensus-init --force --non-interactive --accept-defaults --contributors claude,codex,gemini`
Expected: no `STATUS:` token; exit 0; config overwritten.

- [ ] **Step 4: Final state check**

Run: `git log --oneline -6` and confirm the feature commits are present, then report completion (artifact-scoped: name the branch + that no release tag is cut yet).

---

## Self-review (against the spec)

- **Q1a keep exit 4** → Task 2 gate returns 4; legacy tests updated (Task 2 Step 1). ✓
- **Q2a exit 4 + stable token, stdout/stderr split** → constant (Task 1), gate prints token to stdout line 1 + guidance to stderr (Task 2), contract test (Task 4). ✓
- **Q3c pure no-op + follow-up note** → "leave" returns 0 without writing (Task 2 `test_tty_menu_leave...`); CHANGELOG follow-up note (Task 5). ✓
- **Ordering fix** → TTY-awareness merged *into* the guard (Task 2 Step 4). ✓
- **One-shot / no-loop** → token emitted only when no resolving flag; skill carve-out re-invokes once (Task 2 comment + `test_token_absent_when_*` + Task 4 SKILL text). ✓
- **Slash-command parity** → Task 4 Step 4. ✓
- **TTY EOF→leave, Ctrl-C→exit 1** → Task 1 helper + Task 2 `test_tty_menu_ctrl_c_returns_1`. ✓
- **--dry-run defined** → Task 2 `test_dry_run_existing_non_tty_emits_token`; TTY dry-run shows menu (gate has no dry-run special-case). ✓
- **--force > --reconfigure** → Task 3. ✓
- **Contract regression test mandatory** → Task 4. ✓
- **build/lib not edited** → noted in header; no task touches it. ✓
- No placeholders; types/names consistent (`ALREADY_CONFIGURED_TOKEN`, `_prompt_existing_config_action` used identically across tasks).
