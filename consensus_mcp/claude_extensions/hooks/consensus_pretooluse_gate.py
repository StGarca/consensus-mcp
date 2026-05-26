#!/usr/bin/env python3
"""PreToolUse design gate (Claude Code) — THE HARD BACKSTOP.

Modelled on `contrib/delivery_gate_pretooluse.py` (verified exit-2 block
pattern): reads the PreToolUse event JSON on stdin; blocks a tool call by
exiting 2 with a reason on stderr, allows by exiting 0.

Contract enforced (see `consensus_mcp/_design_approval.py`):
  Implementation tools (Edit/Write/MultiEdit/NotebookEdit) are DENIED until a
  VALIDATED `.consensus/design-approved` marker covers the scope being touched
  (`verify_design_approval` re-validates the marker pointer against the live T6
  seal — a hand-written marker cannot self-approve).

  Bash is DEFAULT-DENY (decision B2): the old leaky blocklist (`_classify_bash`)
  is GONE. Bash is allowed ONLY if (a) the command is on a conservative
  READ-ONLY ALLOWLIST (matched on the leading command token; pipelines/`&&`
  require EVERY segment allowlisted), OR (b) a tight-scope sealed marker is in
  force (`marker_is_sealed`). An unknown command is DENIED (fail-safe — the
  inverse of the unfixable blocklist).

  THREAT MODEL: this enforces a COOPERATING agent's discipline, not a malicious
  shell. It is not a sandbox.

Graceful degradation (converged-plan invariant): if the consensus runtime is
absent (`shutil.which("consensus-init") is None`) the gate FAILS OPEN (exit 0)
so the integrated workflow is never worse than the plain workflow.

stdin event shape (subset used):
  {"tool_name": "Edit", "tool_input": {"file_path": "src/x.py"}, "cwd": "..."}
  {"tool_name": "Bash", "tool_input": {"command": "sed -i ..."}, "cwd": "..."}

Operator override (env):
  CONSENSUS_MCP_GATE_DISABLE=1           -> OPERATOR escape hatch: fully disable the
                                            design gate for this session (fail open).
                                            The human trust-root can never be deadlocked
                                            by the gate. Set in the launch env, not by an
                                            in-session agent.

Test/runtime overrides (env):
  CONSENSUS_MCP_FORCE_RUNTIME_ABSENT=1  -> force fail-open (simulate no runtime)
  CONSENSUS_MCP_FORCE_RUNTIME_PRESENT=1 -> force runtime present (simulate
                                            install without consensus-init on PATH)
  CONSENSUS_MCP_REPO_ROOT=<path>        -> repo root for marker lookup
                                            (else the event `cwd`, else _self_drive)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

# Ensure the consensus_mcp package that ships ALONGSIDE this hook is importable,
# regardless of the cwd Claude Code invokes us from (this file lives at
# consensus_mcp/claude_extensions/hooks/, so the repo root is three parents up).
_PKG_ROOT = Path(__file__).resolve().parents[3]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# Tools whose every invocation modifies a file at `tool_input.file_path`.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# A command line is split into segments (pipeline / sequence) that are each
# allowlisted independently — see _split_segments. Splitting is QUOTE-AWARE: a
# `|`/`;`/`&&` INSIDE a quoted string (e.g. `grep -E 'a|b'`) is part of the
# argument, not a separator (re-audit 2026-05-23: the old regex split mis-denied
# such read-only commands). Conservative — anything unrecognised is denied.

# Conservative READ-ONLY ALLOWLIST (decision B2 + claude's usability fold-in).
# A single command segment is allowed iff its LEADING token (or a recognised
# `git <subcommand>`) is on this list. Default-deny: an unknown leading token is
# DENIED (fail-safe).
# v1.24 (codex finding): `pytest` / `python -m pytest` are REMOVED — running tests
# executes arbitrary test/conftest/plugin code, so they are not "read-only" and
# must not be allowed pre-approval. Run tests behind a sealed marker (or in a
# non-opted-in repo, where the gate fails open anyway).
_READ_ONLY_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "rg",
    "echo", "pwd", "which",
})
# NOTE: `find` is deliberately NOT allowlisted — `find -exec`/`-delete`/`-fprintf`
# mutate the filesystem (re-audit codex-rev-001). A leading-token allowlist cannot
# safely admit a command whose own primaries can write/exec.
# git subcommands that are read-only.
_READ_ONLY_GIT_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "branch", "rev-parse",
})
# consensus's OWN console scripts (pyproject [project.scripts]) — EXEMPT from the
# gate. You cannot require a sealed design to bootstrap/repair/validate/run the
# consensus setup itself: it's chicken-and-egg, `--repair` is the remediation
# command, `--check` is read-only, and the dispatchers ARE the consult. An
# EXPLICIT set (not a `consensus-` prefix) so an unknown `consensus-`-named
# binary on PATH is NOT trusted. Leading-token only; the redirection / subshell /
# command-substitution rejection + the per-segment split (caller) still apply, so
# a chained writer riding on an allowed token is denied.
_CONSENSUS_TOOLING = frozenset({
    "consensus", "consensus-init", "consensus-mcp", "consensus-results",
    "consensus-mcp-dispatch-codex", "consensus-mcp-dispatch-gemini",
    "consensus-mcp-dispatch-grok", "consensus-mcp-dispatch-kimi",
    "consensus-mcp-seal-iteration",
})


def _runtime_present() -> bool:
    """Probe whether the consensus runtime is installed. Env overrides win so
    tests can deterministically simulate either branch."""
    if os.environ.get("CONSENSUS_MCP_FORCE_RUNTIME_ABSENT"):
        return False
    if os.environ.get("CONSENSUS_MCP_FORCE_RUNTIME_PRESENT"):
        return True
    return shutil.which("consensus-init") is not None


def _repo_root(event: dict) -> Path:
    # Test/operator override always wins.
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        return Path(override)
    # Decision H2: resolve the repo root via `git rev-parse --show-toplevel`
    # (anchored at the event cwd), not the raw event cwd, so the marker lookup is
    # stable regardless of which subdirectory the tool was invoked from.
    cwd = event.get("cwd")
    try:
        import subprocess
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd or None, capture_output=True, text=True, timeout=10,
        )
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip())
    except Exception:
        pass
    if cwd:
        return Path(cwd)  # fallback to event cwd
    # Last resort: the package's own repo root.
    from consensus_mcp._self_drive import _resolve_repo_root
    return _resolve_repo_root()


def _segment_is_read_only(segment: str) -> bool:
    """True iff a single command segment's leading token is on the read-only
    allowlist. Recognises `git <read-only-subcommand>` and `python[3] -m pytest`.
    Anything else (incl. redirections, unknown tokens) -> False (default-deny)."""
    seg = segment.strip()
    if not seg:
        return False
    # A redirection OR a command substitution makes a segment a potential writer
    # / arbitrary-exec, even if the leading token looks read-only
    # (e.g. `echo $(rm x)`). Re-audit (gemini-rev-001): the allowlist must reject
    # these before trusting the leading token.
    if any(marker in seg for marker in (">", "<", "$(", "`", "${")):
        return False
    try:
        import shlex
        tokens = shlex.split(seg)
    except ValueError:
        return False
    if not tokens:
        return False
    head = tokens[0]
    # v1.25 (gemini): reject a segment whose COMMAND token opens/closes a subshell
    # — `(rm x)`, `cmd)` etc. Parens inside a QUOTED arg (`grep '(x)' f`) stay in a
    # LATER token, so head is clean — no false positive on legitimate regex args.
    if "(" in head or ")" in head:
        return False
    if head in _READ_ONLY_COMMANDS:
        return True
    # consensus's own tooling is allowed pre-approval (not read-only, but exempt —
    # you can't gate the tooling that bootstraps/repairs the gate). The redirect /
    # subshell / command-substitution rejection above already ran, so a writer
    # chained onto an allowed token (`consensus-init && rm x`, `consensus-init
    # $(rm x)`) is still denied (the rm rides a separate segment / the `$(`/`>`
    # markers fail the check).
    if head in _CONSENSUS_TOOLING:
        return True
    if head == "git":
        if len(tokens) < 2 or tokens[1] not in _READ_ONLY_GIT_SUBCOMMANDS:
            return False
        # Reject exec / file-write injection on ANY read-only subcommand: `-c`
        # (config -> pager/alias exec), `--output`/`--exec-path` (write / exec path),
        # and v1.26 (codex BLOCKING) `--ext-diff`/`--textconv` (run a repo-configured
        # external command). Match the flag BASE so `--output=f`, `--ext-diff=…` etc.
        # are all caught.
        for t in tokens[1:]:
            base_flag = t.split("=", 1)[0]
            if base_flag in ("-c", "--output", "--exec-path", "--ext-diff", "--textconv"):
                return False
        # `git branch` MUTATES when a write flag is present (-d/-D/-m/-M/-c/-C/-u/…)
        # OR a bare positional appears (a NEW branch name = create). Allow read-only
        # forms — including a positional that is the VALUE of a filter flag such as
        # `--contains <sha>` / `--merged <branch>` (v1.26 kimi: do not over-deny those).
        if tokens[1] == "branch":
            _WRITE_BRANCH = {
                "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy",
                "--edit-description", "--set-upstream-to", "-u", "--unset-upstream",
                "-f", "--force", "--create-reflog",
            }
            # v1.27 (codex+kimi): `--list <pattern>` is a read-only listing form whose
            # positional is a glob, not a new branch name — its positional is safe.
            _FILTER_FLAGS = {"--contains", "--no-contains", "--merged",
                             "--no-merged", "--points-at", "--list"}
            args = tokens[2:]
            for t in args:
                if t.split("=", 1)[0] in _WRITE_BRANCH:
                    return False
            positionals = [t for t in args if not t.startswith("-")]
            has_filter = any(t.split("=", 1)[0] in _FILTER_FLAGS for t in args)
            if positionals and not has_filter:
                return False  # bare positional == branch create
        return True
    # v1.24 (codex finding): `python -m pytest` is NO LONGER allowed — pytest runs
    # arbitrary test/conftest/plugin code, so it is not read-only. python is denied.
    return False


def _split_segments(command: str) -> list[str]:
    """Split on |, ||, &&, ;, newline — but ONLY outside quotes. A `|` inside
    `grep -E 'a|b'` is part of the regex, not a pipeline separator. Returns the
    non-empty segments."""
    segs: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote is not None:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                cur.append(command[i + 1]); i += 2; continue
            if c == quote:
                quote = None
            i += 1; continue
        if c in ("'", '"'):
            quote = c; cur.append(c); i += 1; continue
        if c in (";", "\n"):
            segs.append("".join(cur)); cur = []; i += 1; continue
        if c == "|":
            segs.append("".join(cur)); cur = []
            i += 2 if (i + 1 < n and command[i + 1] == "|") else 1
            continue
        if c == "&":
            # v1.24 (kimi BLOCKING): a SINGLE `&` (background) is also a separator —
            # otherwise `cat x & rm -rf y` is one allowlisted segment and the writer
            # after `&` slips through. Split on both `&` and `&&`.
            segs.append("".join(cur)); cur = []
            i += 2 if (i + 1 < n and command[i + 1] == "&") else 1
            continue
        cur.append(c); i += 1
    segs.append("".join(cur))
    return [s for s in segs if s.strip()]


def _bash_is_read_only(command: str) -> bool:
    """DEFAULT-DENY allowlist check for a whole command line. A pipeline /
    sequence (split QUOTE-AWARE on | || && ; newline) is read-only iff EVERY
    non-empty segment is read-only. Empty / unknown -> False (denied)."""
    if not command or not command.strip():
        return False
    segments = _split_segments(command)
    if not segments:
        return False
    return all(_segment_is_read_only(s) for s in segments)


def _deny(reason: str) -> int:
    print(f"[consensus-design-gate] BLOCKED: {reason}\n"
          f"Seal a Workflow A converged plan (>=2 non-claude reviewers) covering "
          f"this scope, then mint `.consensus/design-approved` pointing at that "
          f"sealed iteration. (The gate re-validates the pointer against the live "
          f"seal — a hand-written marker cannot self-approve.)",
          file=sys.stderr)
    return 2


def _is_governance_path(file_path: Path, repo_root: Path) -> bool:
    """True iff the edit target is consensus GOVERNANCE state (under `.consensus/`
    or `consensus-state/`), not gated code. These must stay writable so the
    consensus workflow can bootstrap its own approval — the design-approved marker
    is re-validated against the live seal on use, so permitting its WRITE is safe.
    """
    try:
        rr = repo_root.resolve()
        p = file_path if file_path.is_absolute() else (repo_root / file_path)
        p = p.resolve()
        for gov in (".consensus", "consensus-state"):
            gov_dir = repo_root / gov
            # v1.26 (kimi): a SINGLE lstat (no is_symlink()+resolve() double-stat
            # TOCTOU). The governance dir must be a REAL directory, never a symlink
            # (a symlinked `.consensus` — even to an in-repo dir like `src/` — would
            # let code paths be treated as governance). `base` is then the LITERAL
            # `rr/gov` (rr already resolved, gov has no traversal) — strictly inside
            # repo_root by construction, with no symlink followed at the gov component.
            try:
                st = os.lstat(gov_dir)
            except FileNotFoundError:
                st = None  # absent -> no symlink to follow; bootstrap write is fine
            except OSError:
                continue
            # If it EXISTS, it must be a real directory, never a symlink (a symlinked
            # .consensus — even to an in-repo dir — would smuggle code paths through).
            if st is not None and (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)):
                continue
            base = rr / gov
            if p == base or base in p.parents:
                return True
    except Exception:
        return False
    return False


def _is_protected_install_path(file_path: Path, repo_root: Path) -> bool:
    """True iff the target is part of the consensus ENFORCEMENT surface an in-session agent
    could write to in order to DISABLE the gate globally:
      - ``~/.claude/settings.json``         (registers the hooks)
      - ``~/.claude/hooks/consensus_*.py``  (the hook scripts themselves)

    MINIMAL set per consult iteration-gate-scope-design-2026-05-24 (codex+gemini+kimi):
    the pipx venv is EXCLUDED (tampering it yields an import/exec failure, which the gate
    already treats as fail-OPEN and is `pipx reinstall`-recoverable — not a silent
    self-disable) and skills are instructions, not enforcement. EXPAND this set only if a
    new ENFORCEMENT file is ever added.

    Resolves the TARGET (and the protected refs) so an in-repo symlink pointing into the set
    is caught (symlink-escape). Fail-SAFE to False on error — the caller runs under the
    global fail-OPEN wrapper, and the gate must never brick editing on a resolution error."""
    try:
        claude = Path.home() / ".claude"
        raw = file_path if file_path.is_absolute() else (repo_root / file_path)
        p = raw.resolve()
        settings = claude / "settings.json"
        hooks_dir_raw = claude / "hooks"
        # --- pathname checks: canonical-path writes (incl. a target that does not exist
        #     yet) and symlink escapes (resolve() follows symlinks) ---
        try:
            if p == settings.resolve():
                return True
        except OSError:
            pass
        try:
            hooks_dir = hooks_dir_raw.resolve()
        except OSError:
            hooks_dir = hooks_dir_raw
        if p.parent == hooks_dir and p.name.startswith("consensus_") and p.suffix == ".py":
            return True
        # --- IDENTITY (st_dev/st_ino) check (codex-rev-001 / gemini-rev-001, v1.30.4):
        #     a HARDLINK alias to an existing protected file resolves to its OWN path, not
        #     the protected pathname, so the pathname guard above misses it. Compare inodes
        #     for EXISTING targets. (A non-existent target has no inode to alias -> the
        #     pathname check already covers canonical-path creation.) ---
        try:
            tstat = os.stat(p)
        except OSError:
            return False
        protected = [settings]
        try:
            protected += sorted(hooks_dir_raw.glob("consensus_*.py"))
        except OSError:
            pass
        for prot in protected:
            try:
                ps = os.stat(prot)
            except OSError:
                continue
            if tstat.st_ino == ps.st_ino and tstat.st_dev == ps.st_dev:
                return True
    except Exception:
        return False
    return False


def _resolves_inside_repo(file_path: Path, repo_root: Path) -> bool:
    """True iff the target resolves to repo_root or a path under it. False on an out-of-repo
    target OR any resolution error — both lead the EDIT_TOOLS branch to ALLOW (out-of-repo
    writes are not the gate's concern; erroring -> fail-OPEN, per the gate's prime directive)."""
    try:
        rr = repo_root.resolve()
        p = (file_path if file_path.is_absolute() else (repo_root / file_path)).resolve()
        return p == rr or rr in p.parents
    except Exception:
        return False


def main(argv=None) -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        # Unreadable payload: FAIL OPEN. The design gate must never brick a
        # session on a malformed/foreign event (UX-parity invariant); the
        # delivery gate is the fail-closed backstop for finished artifacts.
        return 0

    # OPERATOR ESCAPE HATCH (v1.30.5): the human trust-root can deliberately disable the
    # gate for a session via CONSENSUS_MCP_GATE_DISABLE=1 in the environment. A safety gate
    # must NEVER be able to deadlock its own operator ("can't mint a seal" must never mean
    # "can't work"). This is read from the Claude Code PROCESS env, set by the operator
    # before launch — an in-session agent cannot self-enable it (a Bash `export` mutates only
    # a subshell, not the hook's inherited env). Full override: bypasses every check below,
    # including the protected-install guard, because the operator has chosen to lift the gate.
    if os.environ.get("CONSENSUS_MCP_GATE_DISABLE"):
        return 0

    if not _runtime_present():
        return 0  # FAIL OPEN — plain workflow, never worse.

    tool = event.get("tool_name") or event.get("toolName") or ""
    tool_input = event.get("tool_input") or event.get("toolInput") or {}

    # Lazy import so a missing module also fails OPEN here (runtime "present"
    # via PATH but package import broken should not brick editing).
    try:
        from consensus_mcp import _design_approval as da
    except Exception:
        return 0

    repo_root = _repo_root(event)

    # PROTECTED-INSTALL tamper guard (ALWAYS-ON — fires BEFORE the opt-in check).
    # consult iteration-gate-scope-design-2026-05-24 (codex+gemini+kimi): this hook is
    # GLOBAL, so an agent in ANY project — even one that never opted in — could write the
    # enforcement surface (~/.claude/settings.json or ~/.claude/hooks/consensus_*.py) and
    # disable the gate for every project. That self-disable threat is opt-in-INDEPENDENT,
    # so it must be checked before the opt-in early-return. (The "never govern a
    # non-opted-in project" promise is about DESIGN-APPROVAL gating, not self-integrity.)
    if tool in EDIT_TOOLS:
        _fp = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if _fp and _is_protected_install_path(Path(_fp), repo_root):
            print("[consensus-design-gate] BLOCKED: writing the consensus enforcement "
                  "surface (~/.claude/settings.json or ~/.claude/hooks/consensus_*.py) is "
                  "refused — it would disable the design gate globally. (To legitimately "
                  "update the install, run `consensus-init` or reinstall.)", file=sys.stderr)
            return 2

    # Finding 1 (opt-in): enforce ONLY in repos that opted into consensus (a
    # `.consensus/` dir present). A repo that never enabled consensus — including
    # the consensus-mcp repo itself, and any unrelated project — fails OPEN so the
    # gate never bricks development it was never meant to govern.
    # CONSENSUS_MCP_FORCE_OPTED_IN forces enforcement deterministically for tests.
    opted_in = (repo_root / ".consensus").is_dir()
    if not (opted_in or os.environ.get("CONSENSUS_MCP_FORCE_OPTED_IN")):
        return 0

    if tool in EDIT_TOOLS:
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not file_path:
            return 0  # nothing to gate
        # 3-class scope (consult iteration-gate-scope-design-2026-05-24): an out-of-repo
        # target is NOT a repo modification and is NOT the gate's concern -> ALLOW. The
        # enforcement surface was already protected above; everything else outside the repo
        # (the agent's ~/.claude memory dir, /tmp scratch, ...) is free. (Resolution errors
        # also land here -> fail-OPEN, per the gate's prime directive.)
        if not _resolves_inside_repo(Path(file_path), repo_root):
            return 0
        # Finding 2 (bootstrap): governance-state writes (.consensus/ incl. the
        # marker, and consensus-state/) are always permitted — otherwise the gate
        # cannot mint its own approval marker (a circular lock). The marker is
        # re-validated against the live seal on use, so allowing its write is safe.
        if _is_governance_path(Path(file_path), repo_root):
            return 0
        res = da.verify_design_approval(Path(file_path), repo_root=repo_root)
        return 0 if res.ok else _deny(res.reason)

    if tool == "Bash":
        command = tool_input.get("command") or ""
        # DEFAULT-DENY (decision B2). Allow ONLY if the command is read-only
        # (every segment on the conservative allowlist) OR a tight-scope sealed
        # marker is in force. An unknown command is denied (fail-safe).
        if _bash_is_read_only(command):
            return 0  # read-only allowlist (ls, cat, grep, git status, pytest, …)
        res = da.marker_is_sealed(repo_root)
        if res.ok:
            return 0  # a tight-scope sealed plan authorises this Bash command
        return _deny(f"Bash command DENIED by default (not on the read-only "
                     f"allowlist and no tight-scope sealed marker in force): "
                     f"{res.reason}")

    return 0  # other tools (Read, Grep, Glob, Task, ...) -> allow.


def _main_fail_open() -> int:
    """Run main(), but FAIL OPEN (exit 0) on ANY unexpected exception. A PreToolUse
    hook that crashes would otherwise block the tool — and since this gate is a GLOBAL
    hook, a crash bricks Bash/Edit in EVERY project at once. A hook bug must never be
    able to do that; the delivery/Stop gate is the fail-closed backstop for finished
    work. (A deliberate DENY returns/raises exit 2 and is preserved.)"""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:
        return 0


if __name__ == "__main__":
    raise SystemExit(_main_fail_open())
