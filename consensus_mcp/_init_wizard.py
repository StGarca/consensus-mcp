"""`consensus init` wizard - fourth and final sub-component of iter-0016.

Creates `.consensus/config.yaml` per the iter-0015 converged design. Supports:
  - Interactive prompts (default; one question per configurability dimension)
  - Non-interactive (`--non-interactive`) with explicit flags
  - `--accept-defaults` (non-interactive using all defaults; overridable per-flag)
  - `--reconfigure` (load existing config; use as defaults; show diff)
  - `--check` (validate existing config; print normalized form; exit 0/2/3)
  - `--print-defaults` (emit default YAML to stdout; no write)
  - `--dry-run` (show intended config + .gitignore changes; no write)

Exit codes per converged plan Section A:
  0  ok
  1  user-abort (interactive cancel via Ctrl+C / EOF)
  2  config missing (--check only) or new iteration in repo without config
  3  invalid config / illegal combination
  4  config exists without --reconfigure or --force
  8  looks like a workspace umbrella (refused; pass --here to override)

`.gitignore` updates use bracketed markers per converged design F9c. Malformed
markers (open without close) are detected and preserved untouched rather than
risking data loss.
"""
from __future__ import annotations

import argparse
import dataclasses
import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from consensus_mcp import config as cfg
from consensus_mcp import _contributor_profiles as profiles_mod
from consensus_mcp._atomic_io import atomic_write_bytes as _shared_atomic_write_bytes


# iter-0031 (per iter-0030 converged plan Q3): marker-based project-root
# detection beats .git-only. Order: git rev-parse > strong-marker walk > cwd.
_STRONG_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "CLAUDE.md",
    ".mcp.json",
    "consensus-state",
)


GITIGNORE_OPEN_MARKER = "# >>> consensus-mcp managed <<<"
GITIGNORE_CLOSE_MARKER = "# <<< consensus-mcp managed >>>"
GITIGNORE_MANAGED_PATHS = (
    ".consensus/tmp/",
    ".consensus/cache/",
    ".consensus/logs/",
)


def _detect_repo_root(start: Path | None = None) -> Path:
    """Resolve project root for the iteration.

    v1.24 (fix 10): this is the wizard's single, reusable repo-root detector -
    `cmd_init` calls it instead of inlining its own walk, so a future shared
    util is a trivial lift. NOTE: the PreToolUse gate currently has its OWN copy
    of root detection in
    consensus_mcp/claude_extensions/hooks/consensus_pretooluse_gate.py:_repo_root
    (which defers to consensus_mcp._self_drive._resolve_repo_root). Unifying the
    two onto one shared helper is pending; this function is the intended landing
    spot for that. The gate is intentionally NOT edited here.

    iter-0031 (per iter-0030 converged plan Q3): use marker-based detection
    instead of .git-only walking. Precedence:
      0. An ancestor carrying `.consensus/config.yaml` - the SAME consuming-project
         marker `_dispatch_base._resolve_repo_root` keys on. Checking it FIRST
         makes a re-init resolve to exactly the root the dispatch/approve resolver
         will later pick, eliminating the divergence hazard the panel flagged
         (gemini-rev-002 / grok-rev-003): init must not set up at root X while the
         runtime resolver resolves to root Y.
      1. `git rev-parse --show-toplevel` if git is available and start is
         inside a git working tree.
      2. Walk up from start (or cwd) looking for any strong marker
         (.git, pyproject.toml, package.json, CLAUDE.md, .mcp.json,
         consensus-state).
      3. Fall back to start / cwd.
    """
    cwd = (start or Path.cwd()).resolve()

    # 0. Already-initialized project: defer to the resolver's marker so init and
    # the runtime resolver agree. (First match walking up from cwd.)
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".consensus" / "config.yaml").is_file():
            return candidate

    # 1. git rev-parse - most authoritative when git is on PATH.
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            top = (result.stdout or "").strip()
            if top:
                return Path(top).resolve()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # git not available / not in a worktree / timeout - fall through.
        pass

    # 2. Walk up looking for strong markers.
    for candidate in (cwd, *cwd.parents):
        for marker in _STRONG_MARKERS:
            if (candidate / marker).exists():
                return candidate

    # 3. Fallback.
    return cwd


# --- iter-0040: claude-code bootstrap pack helpers per iter-0039 converged plan ---


def _resolve_claude_home() -> Path:
    """Resolve ~/.claude (or override) for installing the bootstrap skill+command.

    Precedence (per iter-0039 converged plan Q2):
      1. `CLAUDE_HOME` env var (operator override; supports non-standard installs).
      2. `~/.claude` resolved via Path.home() (POSIX + Windows USERPROFILE/HOME).
    """
    import os as _os
    override = _os.environ.get("CLAUDE_HOME")
    if override:
        # v1.24 (fix 2): a relative CLAUDE_HOME override must be normalized so
        # later path comparisons / os.replace operate on an absolute path
        # (otherwise the install destination is resolved against the *current*
        # cwd at each call, which can drift). expanduser handles a leading "~".
        return Path(override).expanduser().resolve()
    return Path.home() / ".claude"


def _claude_extensions_source_root() -> Path:
    """Return the in-package source directory for claude_extensions assets."""
    return Path(__file__).resolve().parent / "claude_extensions"


_CLAUDE_EXTENSION_FILES = (
    ("skills/consensus/SKILL.md", "skills/consensus/SKILL.md"),
    ("commands/consensus-init.md", "commands/consensus-init.md"),
    # iter-0041: ship the operating-procedure skill (workflow #3 vs #4,
    # round-1 dispatch order, gemini 429 handling, dispatcher hazards,
    # snapshot/restore safety, peer-citation verification). Carries the
    # transferable consensus-mcp memories forward into any project that
    # runs `consensus-init --install-claude-code`.
    ("skills/consensus-workflow/SKILL.md", "skills/consensus-workflow/SKILL.md"),
    # v2.1.0: the Looper plan design-coach wizard (opt-in Build front-door).
    ("skills/consensus-looper-plan/SKILL.md", "skills/consensus-looper-plan/SKILL.md"),
    # v1.21: vendored Superpowers skills (MIT, obra/superpowers v5.1.0 @ f2cbfbe),
    # adapted to hand off to consensus. Ship the attribution alongside. The 4 spine
    # skills (brainstorming, requesting/receiving-code-review,
    # verification-before-completion) carry consensus hand-offs; the rest are
    # near-verbatim. See claude_extensions/VENDORED.md + NOTICE.
    ("NOTICE", "NOTICE"),
    ("VENDORED.md", "VENDORED.md"),
    ("skills/consensus-brainstorming/SKILL.md", "skills/consensus-brainstorming/SKILL.md"),
    ("skills/consensus-writing-plans/SKILL.md", "skills/consensus-writing-plans/SKILL.md"),
    ("skills/consensus-executing-plans/SKILL.md", "skills/consensus-executing-plans/SKILL.md"),
    ("skills/consensus-subagent-driven-development/SKILL.md", "skills/consensus-subagent-driven-development/SKILL.md"),
    # v1.22: companion prompt files the skill references - the 3-family review
    # (2026-05-23) found these dangling. Vendored from upstream, MIT-attributed.
    ("skills/consensus-subagent-driven-development/implementer-prompt.md", "skills/consensus-subagent-driven-development/implementer-prompt.md"),
    ("skills/consensus-subagent-driven-development/spec-reviewer-prompt.md", "skills/consensus-subagent-driven-development/spec-reviewer-prompt.md"),
    ("skills/consensus-subagent-driven-development/code-quality-reviewer-prompt.md", "skills/consensus-subagent-driven-development/code-quality-reviewer-prompt.md"),
    ("skills/consensus-test-driven-development/SKILL.md", "skills/consensus-test-driven-development/SKILL.md"),
    ("skills/consensus-test-driven-development/testing-anti-patterns.md", "skills/consensus-test-driven-development/testing-anti-patterns.md"),
    ("skills/consensus-requesting-code-review/SKILL.md", "skills/consensus-requesting-code-review/SKILL.md"),
    ("skills/consensus-receiving-code-review/SKILL.md", "skills/consensus-receiving-code-review/SKILL.md"),
    ("skills/consensus-verification-before-completion/SKILL.md", "skills/consensus-verification-before-completion/SKILL.md"),
    ("skills/consensus-finishing-a-development-branch/SKILL.md", "skills/consensus-finishing-a-development-branch/SKILL.md"),
    ("skills/consensus-using-git-worktrees/SKILL.md", "skills/consensus-using-git-worktrees/SKILL.md"),
    # v1.21 packaging fix: copy the enforcement hook scripts into <claude_home>/hooks/
    # so the settings.json activation (_install_claude_settings_json) points at REAL
    # files. Without this, _installed_hook_script_path fell back to the in-package
    # source dir, which is absent in a wheel -> hooks activated but scripts missing
    # -> enforcement silently dead. (Names mirror _CONSENSUS_HOOK_SPECS.)
    ("hooks/consensus_sessionstart.py", "hooks/consensus_sessionstart.py"),
    ("hooks/consensus_pretooluse_gate.py", "hooks/consensus_pretooluse_gate.py"),
    ("hooks/consensus_stop_gate.py", "hooks/consensus_stop_gate.py"),
)

# v1.23 (finding 5): HARDCODED floor for the vendored consensus-* skill pack. The
# --install-claude-code path warns when fewer than this many vendored SKILL.md
# sources are actually present/installable - surfacing a STALE or partial package
# (e.g. an old pipx entrypoint, or a packaging gap) instead of silently deploying
# an old/incomplete asset set. (A self-COMPUTED count couldn't catch this, since a
# stale package would also under-count its own expectation.)
_EXPECTED_VENDORED_SKILLS = 10


def _install_claude_extensions(claude_home: Path, force: bool) -> list[str]:
    """Copy the shipped skill + command files into the user's CLAUDE_HOME.

    Idempotency contract (per iter-0039 converged plan Q2):
      - If the destination file is byte-identical to the source, no-op.
      - If the destination file diverges from the source AND force=False,
        skip with a warning printed to stderr (user-edited content preserved).
      - If force=True, overwrite divergent destinations.
      - Missing destination -> write fresh.

    Returns a list of human-readable status lines (also printed by the caller).
    """
    source_root = _claude_extensions_source_root()
    if not source_root.is_dir():
        return [f"WARN: claude_extensions source missing at {source_root}; "
                f"reinstall consensus-mcp to repair"]

    statuses: list[str] = []
    for rel_src, rel_dst in _CLAUDE_EXTENSION_FILES:
        src = source_root / rel_src
        dst = claude_home / rel_dst
        if not src.exists():
            statuses.append(f"WARN: source asset {src} missing; skipped")
            continue

        # v1.24 (fix 1): wrap every per-file read/write in try/except OSError and
        # CONTINUE rather than crash mid-install - a single bad file (permission
        # denied, ENOSPC, ...) must not abort the rest of the install.
        try:
            src_text = src.read_text(encoding="utf-8")
            if dst.exists():
                existing_text = dst.read_text(encoding="utf-8")
                if existing_text == src_text:
                    statuses.append(f"unchanged: {dst}")
                    continue
                if not force:
                    statuses.append(
                        f"SKIP: {dst} diverges from shipped content; pass --force to overwrite"
                    )
                    continue
                # force=True: fall through to write.

            # v1.25 (gemini BLOCKING / kimi): write ATOMICALLY. os.replace replaces a
            # destination symlink (the link itself, never its target) in ONE atomic
            # step - closing the TOCTOU window the prior is_symlink()->unlink()->write
            # left open, and never exposing a partial file to a concurrent reader.
            _atomic_write_text(dst, src_text)
            statuses.append(f"wrote: {dst}")
        except OSError as exc:
            statuses.append(f"WARN: {dst} failed: {exc}")
            continue

    return statuses


# --- v1.21 (converged-plan E): per-project Claude Code subagent install ---
#
# Two subagent definitions are written into the PROJECT's .claude/agents/ during
# per-project bootstrap (alongside .mcp.json / config), so they version with the
# repo and drive the per-project consensus config:
#
#   - consensus-orchestrator.md       (Agent + consensus MCP tools + Read/Bash/Grep/Glob)
#   - consensus-host-peer-reviewer.md (read-only: Read/Grep/Glob/Bash - NO Agent)
#
# Mechanics mirror _install_claude_extensions: byte-identical => unchanged;
# divergent => SKIP unless force; missing => write. Honored by --dry-run and
# opt-out via --no-agents. PROJECT-ONLY - not part of the global
# --install-claude-code path.

_PROJECT_AGENT_FILES = (
    "consensus-orchestrator.md",
    "consensus-host-peer-reviewer.md",
)


def _agents_source_root() -> Path:
    """In-package source directory for the shipped subagent definitions."""
    return _claude_extensions_source_root() / "agents"


def _install_project_agents(repo_root: Path, force: bool) -> list[str]:
    """Copy the shipped subagent definitions into <repo_root>/.claude/agents/.

    Non-destructive (mirrors _install_claude_extensions force semantics):
      - destination byte-identical to source => no-op ("unchanged")
      - destination diverges AND force=False => skip ("SKIP ... --force")
      - force=True => overwrite divergent destinations
      - missing destination => write fresh

    Returns human-readable status lines (also printed by the caller).
    """
    source_root = _agents_source_root()
    if not source_root.is_dir():
        return [f"WARN: agents source missing at {source_root}; "
                f"reinstall consensus-mcp to repair"]

    dest_dir = repo_root / ".claude" / "agents"
    statuses: list[str] = []
    for fname in _PROJECT_AGENT_FILES:
        src = source_root / fname
        dst = dest_dir / fname
        if not src.exists():
            statuses.append(f"WARN: source agent {src} missing; skipped")
            continue

        # v1.24 (fix 1): wrap every per-file read/write in try/except OSError and
        # CONTINUE rather than crash mid-install (mirrors _install_claude_extensions).
        try:
            src_text = src.read_text(encoding="utf-8")
            if dst.exists():
                existing_text = dst.read_text(encoding="utf-8")
                if existing_text == src_text:
                    statuses.append(f"unchanged: {dst}")
                    continue
                if not force:
                    statuses.append(
                        f"SKIP: {dst} diverges from shipped content; pass --force to overwrite"
                    )
                    continue
                # force=True: fall through to write.

            # v1.25 (gemini BLOCKING / kimi, parity): atomic write - os.replace
            # replaces a dst symlink without following it, no TOCTOU window.
            _atomic_write_text(dst, src_text)
            statuses.append(f"wrote: {dst}")
        except OSError as exc:
            statuses.append(f"WARN: {dst} failed: {exc}")
            continue

    return statuses


# --- v1.21 (converged-plan B5): settings.json hook ACTIVATION merge ---
#
# Background (the bug this fixes): the installer copied a `hooks.json` manifest
# into ~/.claude, but Claude Code does NOT read a bare hooks.json there - hooks
# are activated only via the `hooks` key of ~/.claude/settings.json. So copying
# hooks.json was inert and enforcement never fired.
#
# Verified settings.json hooks schema (read from a live ~/.claude/settings.json
# AND superpowers v5.1.0 hooks/hooks.json before writing):
#
#   {
#     "hooks": {
#       "<Event>": [                       # list of GROUPS
#         {
#           "matcher": "Edit|Write|...",  # OPTIONAL (absent => matches all)
#           "hooks": [
#             {"type": "command",
#              "command": "<absolute hook command>",
#              "async": false}
#           ]
#         }
#       ]
#     }
#   }
#
# Events we register: SessionStart, UserPromptSubmit, PreToolUse, Stop.
# Commands point at the installed hook scripts under <claude_home>/hooks/ via
# ABSOLUTE paths (no ${CLAUDE_PLUGIN_ROOT}; that placeholder is only resolved
# for plugin-bundled hooks, not user settings.json entries).
#
# Each consensus-owned hook entry carries a stable marker key so re-running is
# idempotent (we delete-then-reinsert our own entries) and uninstall removes
# ONLY our entries while preserving every unrelated user hook.

# Marker key stamped on every consensus-owned hook command dict. Claude Code
# ignores unknown keys, so this is a safe, machine-detectable tag.
CONSENSUS_HOOK_MARKER = "_consensus_mcp_managed"

# (event_name, matcher, hook_script_filename). matcher=None => omit the key
# (matches all events of that type, matching the live settings.json shape where
# the SessionStart group had no matcher).
_CONSENSUS_HOOK_SPECS = (
    ("SessionStart", "startup|clear|compact", "consensus_sessionstart.py"),
    ("UserPromptSubmit", None, "consensus_sessionstart.py"),
    ("PreToolUse", "Edit|Write|MultiEdit|NotebookEdit|Bash", "consensus_pretooluse_gate.py"),
    ("Stop", None, "consensus_stop_gate.py"),
)


def _resolve_settings_json_path(claude_home: Path) -> Path:
    return claude_home / "settings.json"


def _installed_hook_script_path(claude_home: Path, script: str) -> Path:
    """Absolute path to a hook script.

    Prefer the copy installed under <claude_home>/hooks/ (written by the install
    path); fall back to the in-package source hooks dir when the installed copy
    is absent (e.g. dev installs that did not copy the scripts).
    """
    installed = claude_home / "hooks" / script
    if installed.exists():
        return installed
    return _claude_extensions_source_root() / "hooks" / script


def _build_consensus_hook_command(script_path: Path) -> str:
    """Hook command string that runs the script with the active interpreter.

    Uses sys.executable so the hook runs under the same Python the wizard ran
    under (which has consensus_mcp importable).

    POSIX-quoted via shlex.join on every platform (v1.30.7, converged consult
    iteration-v1307-quoting-design-2026-05-26). Claude Code's hook executor on
    Windows is Git Bash (`/usr/bin/bash`, MSYS), evidenced by the v1.30.6 field
    error: `/usr/bin/bash: line 1: <backslash-eaten Windows path>: command not
    found`. shlex.join single-quotes any path with shell-unsafe characters
    (incl. backslash); bash treats characters inside single quotes literally,
    so a Windows path `C:\\Users\\...\\python.exe` survives bash unquoting and is
    accepted by Windows Python via MSYS's exec layer.

    The pre-v1.30.7 `os.name == "nt"` -> `subprocess.list2cmdline` branch was
    pinned to Windows CI's PowerShell, not Claude Code's runtime hook shell -
    a CI-vs-runtime shell mismatch that this commit reverses.
    """
    parts = [sys.executable, str(script_path)]
    return shlex.join(parts)


def _build_consensus_hook_groups(claude_home: Path) -> dict[str, list[dict]]:
    """Construct the per-event consensus hook GROUPS (settings.json shape).

    Each group's inner command dict carries CONSENSUS_HOOK_MARKER=True so it can
    be cleanly identified for idempotent re-merge + uninstall.
    """
    groups: dict[str, list[dict]] = {}
    for event, matcher, script in _CONSENSUS_HOOK_SPECS:
        script_path = _installed_hook_script_path(claude_home, script)
        cmd_entry: dict = {
            "type": "command",
            "command": _build_consensus_hook_command(script_path),
            "async": False,
            CONSENSUS_HOOK_MARKER: True,
        }
        group: dict = {}
        if matcher is not None:
            group["matcher"] = matcher
        group["hooks"] = [cmd_entry]
        groups.setdefault(event, []).append(group)
    return groups


def _hook_entry_is_consensus(entry: dict) -> bool:
    return isinstance(entry, dict) and entry.get(CONSENSUS_HOOK_MARKER) is True


def _group_is_consensus(group: dict) -> bool:
    """True if a group contains ONLY consensus-owned hook command entries.

    A group is consensus-owned iff every inner hook carries the marker. (Groups
    we author always contain exactly one marked entry; a user could in principle
    have hand-merged, so we only treat all-marked groups as ours to avoid
    dropping a user hook that shares a group.)
    """
    hooks = group.get("hooks") if isinstance(group, dict) else None
    if not isinstance(hooks, list) or not hooks:
        return False
    return all(_hook_entry_is_consensus(h) for h in hooks)


def _strip_consensus_hooks(hooks_map: dict) -> dict:
    """Return a copy of an event->groups map with all consensus entries removed.

    - Drops whole groups that are entirely consensus-owned.
    - From mixed groups (user + consensus hand-merged), drops only the marked
      inner entries, preserving the user's.
    - Removes an event key entirely if it ends up with no groups.
    Unrelated events / groups / hooks are preserved verbatim.
    """
    cleaned: dict = {}
    for event, groups in hooks_map.items():
        if not isinstance(groups, list):
            cleaned[event] = groups
            continue
        new_groups: list = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            if _group_is_consensus(group):
                continue  # drop our whole group
            hooks = group.get("hooks")
            if isinstance(hooks, list) and any(_hook_entry_is_consensus(h) for h in hooks):
                kept = [h for h in hooks if not _hook_entry_is_consensus(h)]
                if kept:
                    preserved = dict(group)
                    preserved["hooks"] = kept
                    new_groups.append(preserved)
                # else: every inner hook was ours -> drop the group entirely
                continue
            new_groups.append(group)
        if new_groups:
            cleaned[event] = new_groups
    return cleaned


def _load_existing_settings_json(path: Path) -> tuple[dict, str | None]:
    """Load settings.json into a dict.

    Returns (data, error). Missing file => ({}, None). Parse/IO failure =>
    ({}, "<msg>") - the caller fails soft and does not clobber the file.
    """
    if not path.exists():
        return {}, None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"read error: {exc}"
    if not text.strip():
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"parse error: {exc}"
    if not isinstance(parsed, dict):
        return {}, "root is not a JSON object"
    return parsed, None


def _merge_consensus_hooks_into_settings(
    settings: dict, claude_home: Path
) -> dict:
    """Merge consensus hook groups into a settings dict (idempotent).

    Strategy: remove any previously-installed consensus entries first (so a
    re-run produces NO duplicates), then append fresh consensus groups under
    each event. ALL unrelated hooks/settings are preserved.
    """
    result = dict(settings)
    existing_hooks = result.get("hooks")
    if not isinstance(existing_hooks, dict):
        existing_hooks = {}
    # Drop our own prior entries for true idempotency.
    merged_hooks = _strip_consensus_hooks(existing_hooks)
    # Append fresh consensus groups.
    for event, groups in _build_consensus_hook_groups(claude_home).items():
        merged_hooks.setdefault(event, [])
        if not isinstance(merged_hooks[event], list):
            # Foreign non-list value under this event - preserve it by nesting
            # it is not safe; instead leave the user's value and skip ours.
            continue
        merged_hooks[event].extend(groups)
    result["hooks"] = merged_hooks
    return result


def _install_claude_settings_json(claude_home: Path, force: bool = False) -> list[str]:
    """Activate the consensus hooks by merging them into <claude_home>/settings.json.

    MERGE-SAFE + IDEMPOTENT: preserves all unrelated user hooks/settings, and a
    re-run produces no duplicate consensus entries (we strip our prior entries
    before re-adding). `force` is accepted for signature symmetry with the other
    installers; the merge is non-destructive so it always proceeds.

    Fails soft: an unparseable existing settings.json is left untouched and a
    WARN status line is returned (enforcement install is skipped, never crashes
    the wider install).

    Returns human-readable status lines (printed by the caller).
    """
    path = _resolve_settings_json_path(claude_home)
    existing, error = _load_existing_settings_json(path)
    if error is not None:
        return [
            f"WARN: {path} could not be parsed ({error}); "
            f"skipping hook activation. Fix the file and re-run, or merge the "
            f"consensus hooks manually."
        ]

    merged = _merge_consensus_hooks_into_settings(existing, claude_home)
    if merged == existing:
        return [f"settings.json hooks already current: {path}"]

    # v1.25 (kimi): an IO failure writing settings.json must NOT crash the install -
    # fail soft with a WARN so the handler surfaces an incomplete install (rc 6),
    # mirroring the extension/agent installers.
    try:
        _atomic_write_json(path, merged)
    except OSError as exc:
        return [f"WARN: {path} could not be written ({exc}); hook activation skipped."]
    events = ", ".join(sorted({e for e, _, _ in _CONSENSUS_HOOK_SPECS}))
    return [f"activated consensus hooks in {path} (events: {events})"]


def _uninstall_claude_settings_json(claude_home: Path) -> list[str]:
    """Remove ONLY consensus-tagged hook entries from <claude_home>/settings.json.

    Preserves all unrelated user hooks/settings. No-op (with a status line) when
    the file is absent or contains no consensus entries.
    """
    path = _resolve_settings_json_path(claude_home)
    existing, error = _load_existing_settings_json(path)
    if error is not None:
        return [f"WARN: {path} could not be parsed ({error}); leaving untouched"]
    if not existing:
        return [f"settings.json absent or empty; nothing to uninstall: {path}"]

    hooks_map = existing.get("hooks")
    if not isinstance(hooks_map, dict):
        return [f"no consensus hooks present in {path}"]

    cleaned_hooks = _strip_consensus_hooks(hooks_map)
    new_settings = dict(existing)
    if cleaned_hooks:
        new_settings["hooks"] = cleaned_hooks
    else:
        new_settings.pop("hooks", None)

    if new_settings == existing:
        return [f"no consensus hooks present in {path}"]

    # v1.26 (kimi): fail soft on an IO error (parity with the install path) rather
    # than crashing the uninstall.
    try:
        _atomic_write_json(path, new_settings)
    except OSError as exc:
        return [f"WARN: {path} could not be written ({exc}); hooks left in place."]
    return [f"removed consensus hooks from {path}"]


# --- iter-0031: .mcp.json bootstrap helpers per iter-0030 converged plan ---


def _resolve_mcp_command(
    explicit: str | None = None,
) -> tuple[str, list[str], bool]:
    """Resolve the consensus-mcp command to write into `.mcp.json`.

    Precedence (per iter-0030 converged plan Q4):
      1. Explicit `--mcp-command` override (string; split on whitespace).
      2. `shutil.which("consensus-mcp")` -> use bare name "consensus-mcp"
         for PATH-portability so committed `.mcp.json` works on other
         machines that have consensus-mcp installed.
      3. Fallback: `sys.executable -m consensus_mcp.server` (dev install
         where the console script wasn't generated).

    Returns (command, args_list, is_portable). `is_portable=True` means the
    written config does not include absolute paths; the command is a name
    Claude Code resolves via PATH at spawn time.
    """
    if explicit:
        parts = explicit.split()
        if not parts:
            raise ValueError("--mcp-command override is empty")
        return parts[0], parts[1:], False

    found = shutil.which("consensus-mcp")
    if found:
        return "consensus-mcp", [], True

    return sys.executable, ["-m", "consensus_mcp.server"], False


def _mcp_command_resolves(command: str) -> bool:
    """True if the MCP server `command` can be spawned (recon #5).

    Absolute / relative paths are checked for existence; bare names are looked
    up on PATH via shutil.which. sys.executable (the dev-fallback command) is
    always considered resolvable. A False result means Claude Code may register
    a silently-dead server, so the caller warns (but never fails).
    """
    if not command:
        return False
    if command == sys.executable:
        return True
    if "/" in command or "\\" in command:
        return Path(command).exists()
    return shutil.which(command) is not None


def _resolve_mcp_json_path(repo_root: Path) -> Path:
    return repo_root / ".mcp.json"


def _build_consensus_mcp_entry(
    command: str,
    args: list[str],
    state_root: Path,
    project_root: Path,
) -> dict:
    """Construct the mcpServers["consensus-mcp"] entry."""
    entry: dict = {"command": command}
    if args:
        entry["args"] = list(args)
    entry["env"] = {
        "CONSENSUS_MCP_STATE_ROOT": str(state_root),
        "CONSENSUS_MCP_PROJECT_ROOT": str(project_root),
    }
    return entry


def _load_existing_mcp_json(path: Path) -> tuple[dict | None, str | None]:
    """Load `.mcp.json` if present.

    Returns (parsed_data, error_message):
      - (None, None) if file doesn't exist
      - (dict, None) on successful parse
      - (None, "parse error: ...") on JSON failure / IO failure
    """
    if not path.exists():
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
        parsed = json.loads(text)
        return parsed, None
    except json.JSONDecodeError as exc:
        return None, f"parse error: {exc}"
    except OSError as exc:
        return None, f"read error: {exc}"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """The SECURE, symlink-safe atomic writer (v1.26 root-fix for the tmp-symlink
    class). Now a thin alias over the single shared primitive in
    `consensus_mcp._atomic_io` (gemini-rev-001 / kimi-rev-001: the same writer is
    reused by the session-active marker and the design-approved trust pointer so
    the guarantees can never diverge). text/json/config/gitignore route through it.
    """
    _shared_atomic_write_bytes(path, data)


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic UTF-8 text write via the hardened _atomic_write_bytes primitive."""
    _atomic_write_bytes(path, text.encode("utf-8"))


def _path_is_within(repo_root: Path, target: Path) -> bool:
    """True if `target` resolves to repo_root itself or a path strictly inside it.

    v1.24 (fix 5a): guards instruction-file provisioning against path traversal -
    a profile-supplied filename like '../../etc/evil' must not let us write
    outside the repo. Both sides are fully resolved before comparison.
    """
    rr = repo_root.resolve()
    p = target.resolve()
    return rr == p or rr in p.parents


def _json_semantically_equal(a, b) -> bool:
    """True if two JSON-able values are equal ignoring object key ORDER.

    v1.24 (fix 3): used to compare an existing `.mcp.json` server entry against the
    desired one so a difference that is ONLY key ordering is not flagged as a
    conflict. json.dumps(..., sort_keys=True) canonicalizes key order at every
    nesting level. None compares unequal to a real entry (so a missing entry is
    not "already current").
    """
    if a is None or b is None:
        return a is b
    try:
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    except (TypeError, ValueError):
        return a == b


def _atomic_write_json(path: Path, data: dict) -> None:
    """Pretty-print JSON, write atomically via tmp+rename.

    v1.24 (fix 11): the tmp-file + os.replace pattern makes config writes atomic -
    a concurrent writer races to last-writer-wins rather than corrupting a
    half-written file (os.replace is an atomic rename on POSIX and Windows).
    Full cross-process file locking is overkill here; atomic replace is
    sufficient. Reused by .mcp.json AND settings.json writers.
    """
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    _atomic_write_bytes(path, text.encode("utf-8"))


def _write_mcp_json(
    repo_root: Path,
    state_root: Path,
    project_root: Path,
    command: str,
    args: list[str],
    *,
    force: bool = False,
) -> tuple[str, Path]:
    """Read existing `.mcp.json`, merge or write consensus-mcp entry,
    write back. Returns (status, path) where status is one of:

      - "wrote"             - fresh file created
      - "merged"            - added consensus-mcp into existing config,
                              other servers preserved
      - "already-current"   - entry exists and matches; no write
      - "blocked-conflict"  - entry exists but differs; not overwritten
                              (use force=True to replace just this entry)
      - "parse-error:<msg>" - existing file failed to parse; not modified

    Per iter-0030 converged plan: merge mode is default; conflict mode is
    skip+warn unless force=True; malformed JSON is skip+warn unconditionally.
    """
    path = _resolve_mcp_json_path(repo_root)
    existing, error = _load_existing_mcp_json(path)

    desired = _build_consensus_mcp_entry(command, args, state_root, project_root)

    if error is not None:
        return (f"parse-error:{error}", path)

    if existing is None:
        payload = {"mcpServers": {"consensus-mcp": desired}}
        _atomic_write_json(path, payload)
        return ("wrote", path)

    if not isinstance(existing, dict):
        return ("parse-error:root is not a JSON object", path)

    mcp_servers = existing.get("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        return ("parse-error:mcpServers is not a JSON object", path)

    existing_entry = mcp_servers.get("consensus-mcp")
    # v1.24 (fix 3): compare for SEMANTIC equality (key-order-insensitive) so an
    # existing entry that differs only in JSON key ordering is NOT reported as a
    # conflict. Python dict == is already order-independent, but normalizing via
    # json.dumps(..., sort_keys=True) makes the intent explicit and is robust even
    # if an entry was hand-written as a differently-ordered object.
    if _json_semantically_equal(existing_entry, desired):
        return ("already-current", path)

    if existing_entry is not None and not force:
        return ("blocked-conflict", path)

    # Add or replace consensus-mcp entry; preserve all other servers.
    mcp_servers["consensus-mcp"] = desired
    existing["mcpServers"] = mcp_servers
    _atomic_write_json(path, existing)
    return ("merged", path)


def _resolve_config_path(args, repo_root: Path) -> Path:
    """Honor --config override; otherwise default to repo_root/.consensus/config.yaml."""
    if getattr(args, "config", None):
        return Path(args.config).resolve()
    return repo_root / ".consensus" / "config.yaml"


def _detect_available_contributors(repo_root: Path) -> list[str]:
    """Installed INDEPENDENT contributors, derived dynamically from the merged
    profile set (no hardcoded AI list - decision 7). host is always available;
    cli_reviewers iff their detect.command resolves on PATH; host_peer excluded
    (it is offered via the conditional follow-up, never auto-enabled)."""
    profiles = _load_merged_profiles(None)
    out = []
    for name in _independent_ordered_names(profiles):
        if _profile_installed(profiles[name]):
            out.append(name)
    return out


# --- v1.18.0: contributor selection + detect-guide + instruction provisioning ---
# (per converged-plan iteration-v1180-contributor-design: decision.wizard_ux,
#  decision.detect_guide, the instruction_files block.)


# Managed-block sentinels for per-AI instruction files. The em-dash matches the
# converged plan verbatim; the block between these markers is owned by
# consensus-mcp and refreshed in place - everything outside is the user's.
INSTRUCTION_BEGIN_MARKER = "<!-- consensus-mcp:begin (managed - do not edit inside) -->"
INSTRUCTION_END_MARKER = "<!-- consensus-mcp:end -->"

# A static reminder appended to each detect+guide block: the wizard NEVER shells
# out install/auth commands (operator chose detect+guide, not auto-exec).
_DETECT_GUIDE_REMINDER = "  (these commands are NOT run for you)"


def _load_merged_profiles(config_profiles: dict | None) -> dict:
    """Merge the packaged built-in profiles with any operator overlay.

    `config_profiles` is the `contributors.profiles` map from an existing
    config (or None). Built-ins come from the package; the overlay overrides
    by name and may add new contributors.
    """
    builtin = profiles_mod.load_builtin_profiles()
    return profiles_mod.merge_profiles(builtin, config_profiles or {})



def _profile_installed(profile: dict) -> bool:
    """True if the contributor is usable on this host.

    host (claude) is the running environment - always available. host_peer
    (v1.20.0: a same-family blind SWE-reviewer run via the host callback) is
    likewise always usable on the host - it has no CLI to detect. cli_reviewers
    are available iff their detect.command resolves on PATH.
    """
    if profile.get("kind") in (profiles_mod.KIND_HOST, profiles_mod.KIND_HOST_PEER):
        return True
    command = (profile.get("detect") or {}).get("command")
    if not command:
        return False
    return shutil.which(command) is not None


def _independent_ordered_names(profiles: dict) -> list[str]:
    """Display/selection order over INDEPENDENT profiles only (host first, then
    sorted). host_peer is excluded - it is offered via the conditional follow-up."""
    indep = {
        n: p for n, p in profiles.items()
        if isinstance(p, dict) and p.get("kind") != profiles_mod.KIND_HOST_PEER
    }
    host = sorted(n for n in indep if indep[n].get("kind") == profiles_mod.KIND_HOST)
    rest = sorted(n for n in indep if n not in host)
    return host + rest


def _select_contributors_interactive(
    profiles: dict, preselected: list[str] | None = None
) -> list[str]:
    """Numbered multi-select over independent profiles only (no TUI dep).

    host_peer profiles are excluded from this list - they are offered via a
    conditional follow-up prompt (separate task). Shows '[ok] installed' /
    '[x] missing' per entry, pre-checks installed ones as the default (accepted
    on empty input), and re-prompts until >=2 are picked.

    preselected: if supplied, those names (filtered to the display list) are
    pre-checked regardless of install status. Useful for --reconfigure defaults.

    Raises KeyboardInterrupt on Ctrl+C / EOF (caller maps to exit 1).
    """
    names = _independent_ordered_names(profiles)
    installed = {n: _profile_installed(profiles[n]) for n in names}
    if preselected is not None:
        prechecked = [n for n in names if n in preselected]
    else:
        prechecked = [n for n in names if installed[n]]

    print("Select the AI reviewers to use (>=2 required):")
    for idx, name in enumerate(names, start=1):
        status = "[ok] installed" if installed[name] else "[x] missing"
        mark = "x" if name in prechecked else " "
        print(f"  [{mark}] {idx}. {name} ({status})")
    default_hint = ",".join(str(names.index(n) + 1) for n in prechecked) or "none"

    while True:
        try:
            raw = input(f"Enter comma-separated numbers [default: {default_hint}]: ").strip()
        except EOFError as exc:
            raise KeyboardInterrupt from exc

        if not raw:
            chosen = list(prechecked)
        else:
            chosen, bad = [], False
            for tok in (t.strip() for t in raw.split(",") if t.strip()):
                if not tok.isdigit() or not (1 <= int(tok) <= len(names)):
                    print(f"  invalid selection {tok!r}", file=sys.stderr)
                    bad = True
                    break
                cand = names[int(tok) - 1]
                if cand not in chosen:
                    chosen.append(cand)
            if bad:
                continue

        if len(chosen) < 2:
            print("  please select at least 2 independent reviewers", file=sys.stderr)
            continue
        return chosen


def _print_panel_summary(enabled: list[str], profiles: dict) -> None:
    """Print a one-line panel summary after init, showing weighted reviewer count."""
    indep = [n for n in enabled if profiles_mod.resolve_kind(n, profiles) != profiles_mod.KIND_HOST_PEER]
    peers = [n for n in enabled if profiles_mod.resolve_kind(n, profiles) == profiles_mod.KIND_HOST_PEER]
    if peers:
        total = f"{len(indep)}.5"
        print(
            f"Panel: {total} reviewers - {len(indep)} independent "
            f"({', '.join(indep)}) + 0.5 supplemental same-model ({', '.join(peers)})."
        )
    else:
        print(f"Panel: {len(indep)} independent reviewers ({', '.join(indep)}).")


def _warn_degenerate_panel(enabled: list[str], profiles: dict) -> None:
    """recon #6: warn (non-interactive path) when <2 independent contributors.

    Consensus needs >=2 independent (non-host_peer) reviewers for cross-family
    cross-review; a single-reviewer config is degraded. The interactive path
    re-prompts for >=2, so this guard is only wired into the non-interactive
    bootstrap. Warn-only - never fails the init.
    """
    independent = [
        n for n in enabled
        if profiles_mod.resolve_kind(n, profiles) != profiles_mod.KIND_HOST_PEER
    ]
    if len(independent) >= 2:
        return
    print(
        f"WARNING: only {len(independent)} independent contributor enabled "
        f"({', '.join(independent) or 'none'}). Consensus needs >=2 contributors "
        f"for cross-family review; this config is single-reviewer (degraded). "
        f"Re-run `consensus init --reconfigure` to add another reviewer.",
        file=sys.stderr,
    )


def _print_status_summary(
    config_path: Path,
    enabled: list[str],
    profiles: dict,
    *,
    mcp_command: str | None,
    mcp_resolves: bool | None,
    from_claude_code: bool,
) -> None:
    """recon #4: print a concise friendly post-init next-steps / status block.

    Only called from the fresh / --reconfigure per-project bootstrap path (never
    --check / --print-defaults / --install-claude-code). Surfaces: config path,
    panel composition, present/missing contributor CLIs, MCP-command
    resolvability (recon #5), and the FIRST concrete next step.
    """
    independent = [
        n for n in enabled
        if profiles_mod.resolve_kind(n, profiles) != profiles_mod.KIND_HOST_PEER
    ]
    present, missing = [], []
    for name in enabled:
        profile = profiles.get(name)
        if profile is None:
            continue
        if _profile_installed(profile):
            present.append(name)
        else:
            missing.append(name)

    print()
    print("Next steps:")
    print(f"  config:  {config_path}")
    print(f"  Panel:   {len(independent)} independent reviewers "
          f"({', '.join(independent) or 'none'}).")
    print(f"  CLIs present: {', '.join(present) or 'none'}")
    if missing:
        print(f"  CLIs missing: {', '.join(missing)} "
              f"(install them or they will be skipped)")
    # goal item 6: the exact cold-start trap - pipx installed the package but
    # `pipx ensurepath` was never run, so the console scripts are not on PATH and
    # every later command fails with "command not found". Surface it loudly here.
    missing_scripts = _missing_console_scripts()
    if missing_scripts:
        print(f"  CONSOLE SCRIPTS: WARNING - {', '.join(missing_scripts)} not on "
              f"PATH; fix before first use: {_path_export_hint()}")
    else:
        print("  Console scripts: on PATH.")
    if mcp_command is not None:
        if mcp_resolves:
            print(f"  MCP server: '{mcp_command}' resolves on PATH.")
        else:
            print(f"  MCP server: WARNING - '{mcp_command}' does NOT resolve on "
                  f"PATH; the registered consensus-mcp server may not start.")
    if from_claude_code:
        print("  -> Restart Claude Code in this project (or run `/mcp` to reload "
              "MCP servers) to activate consensus-mcp.")
    else:
        print("  -> Run a consult with the consensus-workflow skill (or restart "
              "Claude Code to load the consensus-mcp server), then start a "
              "consult.")
    # P1.3 / Q4 / grok's "every cold surface": tell the user+AI the EXACT first
    # move and the real enforcement status - never leave them guessing.
    print()
    print("Your first review (after restart):")
    print('  - In Claude Code, say: "run a consensus review on <your question>"')
    print("  - Or scaffold it directly:")
    print('      consensus-mcp-start-consult --question "<what to review>" '
          '--scope-glob "<files>"')
    print("    (it prints the exact dispatch + approve commands).")
    # Report the ACTUAL enforcement state - do NOT unconditionally tell the user to
    # run the global step (that produced the "run the thing you just ran" loop when
    # they had already installed it). _repair_check_enforcement is the read-only
    # detector: 'ok' iff the consensus hooks are in ~/.claude/settings.json AND the
    # referenced hook scripts exist.
    try:
        _enf_comp, _ = _repair_check_enforcement(_resolve_claude_home())
        _enforced = _enf_comp.state == "ok"
    except Exception:
        _enforced = False
    print("Enforcement status:")
    if _enforced:
        print("  - ENABLED: edit-gating + precedence injection are active "
              "machine-wide (the global hooks are installed). Nothing to do.")
    else:
        print("  - ADVISORY here: consults run, but edits are NOT gated. To enable "
              "edit-gating + precedence injection, run ONCE per machine: "
              "consensus-init --install-claude-code")


def _prompt_host_peer_followup(selection: list[str], profiles: dict, default_yes: bool) -> str | None:
    """If a host is selected and a same-family host_peer profile exists (and is
    not already enabled), offer it as a 0.5 supplemental. Returns the host_peer
    profile name to append, or None. Multiple same-family host_peers -> mini-
    select defaulting to none (never silently pick the first)."""
    candidates = []
    for host in (n for n in selection if profiles_mod.resolve_kind(n, profiles) == profiles_mod.KIND_HOST):
        for hp in profiles_mod.matching_host_peers(host, profiles):
            if hp not in selection and hp not in candidates:
                candidates.append(hp)
    if not candidates:
        return None

    print("\nYou're using claude as the host. Add a same-model claude review agent?")
    print(
        "This is a SUPPLEMENTAL review (shown as +0.5 in the init summary only - NOT a\n"
        "fully independent reviewer; it shares the host model's blind spots). It gets no\n"
        "vote at the consensus gate and can't close consensus (claude already votes as\n"
        "host) - but every good idea it raises is still applied on merit. A useful extra\n"
        "pass if you have the tokens to spare."
    )
    if len(candidates) == 1:
        default = "y" if default_yes else "n"
        try:
            ans = (input(f"Add it? [{'Y/n' if default_yes else 'y/N'}]: ").strip().lower() or default)
        except EOFError as exc:
            raise KeyboardInterrupt from exc
        return candidates[0] if ans.startswith("y") else None

    print("Multiple same-model reviewers available (choose one or none):")
    for i, hp in enumerate(candidates, start=1):
        print(f"  {i}. {hp}")
    try:
        raw = input("Number to add [default: none]: ").strip()
    except EOFError as exc:
        raise KeyboardInterrupt from exc
    if raw.isdigit() and 1 <= int(raw) <= len(candidates):
        return candidates[int(raw) - 1]
    return None


def _reconfigure_contributors(base: dict, profiles: dict) -> None:
    """Reconfigure path: pre-seed the multi-select with the existing INDEPENDENT
    selection (the >=2 loop guides the user to fix an invalid legacy config), then
    offer the supplemental follow-up defaulting to its CURRENT state (preserve a
    legacy host_peer)."""
    existing = list((base.get("contributors") or {}).get("enabled") or [])
    existing_independent = [n for n in existing if profiles_mod.resolve_kind(n, profiles) != profiles_mod.KIND_HOST_PEER]
    had_host_peer = any(profiles_mod.resolve_kind(n, profiles) == profiles_mod.KIND_HOST_PEER for n in existing)
    selection = _select_contributors_interactive(profiles, preselected=existing_independent)
    hp = _prompt_host_peer_followup(selection, profiles, default_yes=had_host_peer)
    if hp:
        selection.append(hp)
    base["contributors"]["enabled"] = selection


def _install_os_key() -> str:
    """Map sys.platform to a profile install[] OS key."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _detect_and_guide(selection: list[str], profiles: dict) -> None:
    """Print OS-appropriate install/auth guidance for each MISSING cli_reviewer.

    Nothing is executed. Present CLIs and the host (claude) print nothing.
    """
    os_key = _install_os_key()
    for name in selection:
        profile = profiles.get(name)
        if profile is None:
            continue
        if profile.get("kind") == profiles_mod.KIND_HOST:
            continue
        command = (profile.get("detect") or {}).get("command")
        if not command or shutil.which(command) is not None:
            continue

        print(f"{name}: not found on PATH - to enable it:")
        install = profile.get("install") or {}
        cmd = install.get(os_key)
        if cmd is None and os_key == "darwin":
            cmd = install.get("linux")
        if cmd:
            print(f"  install: {cmd}")
        else:
            print(f"  install: unavailable for {os_key} - see {name} vendor docs")

        auth = profile.get("auth") or {}
        if auth.get("command"):
            print(f"  auth:    {auth['command']}")
        for var in auth.get("env_vars") or []:
            print(f"  auth env (optional): {var}")
        if auth.get("note"):
            print(f"  note:    {auth['note']}")
        print(_DETECT_GUIDE_REMINDER)


# --- cold-start goal: prerequisite + verification preflight (goal items 1/5/6) ---

# The console scripts a working install must expose on PATH. If pipx installed the
# package but `pipx ensurepath` was never run, these are missing and EVERY later
# step fails with "command not found" - the exact friction a cold user hits. We
# surface it explicitly rather than letting it fail opaquely later.
_CORE_CONSOLE_SCRIPTS = ("consensus-mcp", "consensus-init")


def _missing_console_scripts() -> list[str]:
    """Core consensus console scripts NOT resolvable on PATH (goal item 6: 'no
    missing scripts'). Empty list == all present."""
    return [s for s in _CORE_CONSOLE_SCRIPTS if shutil.which(s) is None]


def _path_export_hint() -> str:
    """The exact, OS-appropriate command to put pipx's bin dir on PATH."""
    if sys.platform == "win32":
        return ("pipx ensurepath   (then reopen the terminal; or add "
                "%USERPROFILE%\\.local\\bin to PATH)")
    return ("pipx ensurepath   (then reopen the shell; or add ~/.local/bin to "
            "your PATH in ~/.profile / ~/.zshrc)")


def _reviewer_auth_state(profile: dict) -> tuple[str, str | None]:
    """Best-effort auth signal for an installed reviewer CLI, WITHOUT a model call.

    Returns (state, hint) where state is 'authed' (an auth env var is set or a
    declared auth state file exists), 'unknown' (installed but no positive auth
    signal), or 'n/a' (no auth contract). hint is the auth command to run when not
    yet authed. Deliberately conservative: a real auth check would cost a request,
    so 'unknown' means 'we could not confirm', not 'definitely unauthenticated'."""
    auth = profile.get("auth") or {}
    if not auth:
        return ("n/a", None)
    for var in auth.get("env_vars") or []:
        if os.environ.get(var):
            return ("authed", None)
    state_path = auth.get("state_path")
    if state_path and Path(state_path).expanduser().exists():
        return ("authed", None)
    return ("unknown", auth.get("command"))


def _verify_reviewer(name: str, profile: dict) -> dict:
    """Preflight ONE reviewer: is its CLI installed, does it run, is it authed?

    No model call (cheap + offline-friendly). Returns a structured result the
    caller renders. `version_ok` is True only if `<bin> --version` exits 0; some
    CLIs lack --version, so installed-but-not-responsive is reported, not failed."""
    command = (profile.get("detect") or {}).get("command")
    result = {"name": name, "command": command, "installed": False,
              "version_ok": False, "auth_state": "n/a", "auth_hint": None,
              "version": None}
    if not command:
        return result
    resolved = shutil.which(command)
    result["installed"] = resolved is not None
    if not resolved:
        return result
    try:
        proc = subprocess.run([resolved, "--version"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=15, check=False)
        result["version_ok"] = proc.returncode == 0
        result["version"] = (proc.stdout or proc.stderr or "").strip().splitlines()[0:1]
        result["version"] = result["version"][0] if result["version"] else None
    except (OSError, subprocess.TimeoutExpired):
        result["version_ok"] = False
    state, hint = _reviewer_auth_state(profile)
    result["auth_state"] = state
    result["auth_hint"] = hint
    return result


def _run_verification_pass(enabled: list[str], profiles: dict) -> int:
    """Optional cold-start verification (goal item 5): confirm there are NO errors
    or missing scripts/CLIs before the user's first real question - WITHOUT a paid
    model call. Checks: core console scripts on PATH, then per independent reviewer
    installed + responsive + best-effort auth. Prints a readable PASS/CHECK report.

    Returns the number of HARD problems (missing console scripts, or a selected
    reviewer whose CLI is absent) so a caller can exit non-zero."""
    print("Verification pass (preflight - no model calls):")
    problems = 0

    missing_scripts = _missing_console_scripts()
    if missing_scripts:
        problems += len(missing_scripts)
        print(f"  [X] console scripts MISSING on PATH: {', '.join(missing_scripts)}")
        print(f"      fix: {_path_export_hint()}")
    else:
        print(f"  [OK] console scripts on PATH: {', '.join(_CORE_CONSOLE_SCRIPTS)}")

    independent = [
        n for n in enabled
        if profiles_mod.resolve_kind(n, profiles) != profiles_mod.KIND_HOST
        and profiles_mod.resolve_kind(n, profiles) != profiles_mod.KIND_HOST_PEER
    ]
    installed_independent = 0
    auth_unconfirmed: list[str] = []
    for name in independent:
        profile = profiles.get(name)
        if profile is None:
            continue
        r = _verify_reviewer(name, profile)
        if not r["installed"]:
            problems += 1
            print(f"  [X] {name}: CLI '{r['command']}' NOT installed (panel will "
                  f"skip it). Install it (see the guidance above), then re-verify.")
            continue
        installed_independent += 1
        ver = f" ({r['version']})" if r["version"] else ""
        run_note = "responds" if r["version_ok"] else "installed (no --version)"
        if r["auth_state"] == "authed":
            auth_note = "authenticated"
        elif r["auth_state"] == "unknown":
            auth_unconfirmed.append(name)
            auth_note = "auth UNCONFIRMED"
            if r["auth_hint"]:
                auth_note += f" - to authenticate: {r['auth_hint']}"
        else:
            auth_note = "no auth required"
        print(f"  [OK] {name}: {run_note}{ver}; {auth_note}")

    # kimi-rev-002: a panel needs >=2 INSTALLED independent reviewers or the very
    # first approve will fail at the cross-family gate. Reporting 'ready' with
    # fewer is a false green - count it as a hard problem.
    if installed_independent < 2:
        problems += 1
        print(f"  [X] degenerate panel: {installed_independent} installed "
              f"independent reviewer(s); consensus needs >=2 (approval is refused "
              f"below that). Install/select another reviewer, then re-verify.")

    if problems:
        print(f"  -> {problems} hard problem(s) to resolve before consensus can "
              f"run a full panel.")
    elif auth_unconfirmed:
        # codex-rev-002: do NOT claim 'ready' while auth is unconfirmed - the first
        # real dispatch would fail at the CLI's own auth pre-flight. Qualify it.
        print(f"  -> scripts + CLIs present, but authenticate first: "
              f"{', '.join(auth_unconfirmed)} (run each auth command above). Then "
              f"you are ready to ask consensus your first question.")
    else:
        print("  -> ready: scripts present, >=2 reviewer CLIs installed and "
              "authenticated. Ask consensus your first question.")
    return problems


# P1.2: a cold AI reading CLAUDE.md must learn how consensus OPERATES here, not
# just generic coding guidelines. This short preamble is the guaranteed-seen
# pointer; the detailed runbook is single-sourced in the consensus-workflow skill.
_ON_DEMAND_OPERATING_PREAMBLE = """\
## consensus-mcp is available on demand

Do NOT invoke consensus-mcp unless the user explicitly requests a consensus
consult in this project. Ordinary design, implementation, review, and editing
do not require consensus approval. Global installation and this instruction
block provide the capability only; they are not consent to continuous guidance.

When explicitly requested, run the consult, return the sealed result, and stop.
On-demand consults create no edit gate or delivery-token obligation. Continuous
governance is enabled only when `.consensus/config.yaml` explicitly contains
`governance.mode: continuous`.

The full operating procedure lives in the `consensus-workflow` skill.

---

"""

_CONTINUOUS_OPERATING_PREAMBLE = """\
## consensus-mcp continuous governance is active in this project

This project explicitly opts into continuous consensus governance. Design
decisions are approved by a CROSS-AI
consult (a panel of different AIs reviewing independently), not by one model's
say-so. As the host AI:

- To run a review/consult: scaffold it with `consensus-mcp-start-consult
  --question "<what to review>" --scope-glob "<files>"` (or the
  `consensus.start_consult` MCP tool). It prints the EXACT next commands.
- Dispatch each reviewer in its OWN shell via `consensus-mcp-dispatch-<reviewer>`
  (omit `--pass-id`; it auto-derives a unique one). Do NOT use the MCP
  `reviewer_dispatch_*` wrappers (45s timeout). Run kimi LAST/alone.
- Synthesize the sealed `*-review.yaml` (read them via
  `consensus.get_iteration_outcome`) into one `converged-plan.yaml`, then approve:
  `consensus-mcp-approve --iteration <name> --scope-glob "<files>"`. Approval ARMS
  the edit gate and authorizes in-scope edits.
- The full operating procedure lives in the `consensus-workflow` skill - load it
  for anything beyond this summary.

---

"""


def _vendored_instructions_text(
    governance_mode: str = cfg.GOVERNANCE_ON_DEMAND,
) -> str:
    """Return the consensus operating preamble (P1.2) + the vendored Karpathy
    guidelines (contributor_instructions/base.md)."""
    base = Path(__file__).resolve().parent / "contributor_instructions" / "base.md"
    preamble = (
        _CONTINUOUS_OPERATING_PREAMBLE
        if governance_mode == cfg.GOVERNANCE_CONTINUOUS
        else _ON_DEMAND_OPERATING_PREAMBLE
    )
    return preamble + base.read_text(encoding="utf-8")


def _upsert_managed_block(existing: str, block_body: str) -> str:
    """Insert or refresh the managed block in `existing` text. Idempotent.

    If a sentinel pair is present, its contents are replaced and surrounding
    user content is preserved. If absent, the block is appended. Running twice
    yields exactly one block.
    """
    managed = f"{INSTRUCTION_BEGIN_MARKER}\n{block_body.rstrip(chr(10))}\n{INSTRUCTION_END_MARKER}\n"

    begin = existing.find(INSTRUCTION_BEGIN_MARKER)
    end = existing.find(INSTRUCTION_END_MARKER)
    if begin != -1 and end != -1 and end > begin:
        end_full = end + len(INSTRUCTION_END_MARKER)
        # Consume a single trailing newline after the end marker if present so
        # we don't accumulate blank lines on refresh.
        if end_full < len(existing) and existing[end_full] == "\n":
            end_full += 1
        return existing[:begin] + managed + existing[end_full:]

    # No existing block: append, separating from prior content with one blank line.
    if existing and not existing.endswith("\n"):
        existing += "\n"
    if existing.strip():
        existing += "\n"
    return existing + managed


def _provision_instruction_files(
    selection: list[str], profiles: dict, repo_root: Path,
    governance_mode: str = cfg.GOVERNANCE_ON_DEMAND,
) -> list[Path]:
    """Seed/refresh per-AI instruction files for the selected contributors.

    Targets profile.instructions.filename, deduped by filename (codex+kimi both
    -> AGENTS.md -> written once). Non-destructive: a managed block is inserted or
    refreshed between sentinels, leaving any user content intact. Idempotent.

    Returns the list of written file paths (deduped).
    """
    block_body = _vendored_instructions_text(governance_mode)
    targets: list[str] = []
    for name in selection:
        profile = profiles.get(name)
        if profile is None:
            continue
        filename = (profile.get("instructions") or {}).get("filename")
        if filename and filename not in targets:
            targets.append(filename)

    written: list[Path] = []
    for filename in targets:
        path = repo_root / filename
        # v1.24 (fix 5a): refuse to write outside repo_root. A malicious/buggy
        # profile filename ('../../x', or an absolute path) must not let us escape
        # the project tree.
        if not _path_is_within(repo_root, path):
            print(
                f"WARN: refusing to write instruction file outside repo: "
                f"{filename!r} resolves to {path.resolve()}",
                file=sys.stderr,
            )
            continue
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        new_text = _upsert_managed_block(existing, block_body)
        if new_text != existing:
            # fix 5b: write atomically (tmp + os.replace) so a crash mid-write
            # never leaves a truncated instruction file.
            _atomic_write_text(path, new_text)
        written.append(path)
    return written


# Interactive workflow picker's valid set (consult Q3, 2026-06-11): a module
# constant consumed by the call site so tests assert the REAL offering - the
# v1.14.4 defect class was 'alias in WORKFLOW_ALIASES but not in the picker'.
WORKFLOW_PROMPT_VALID = (
    "post-review", "propose-converge", "advisory", "autonomous-execute",
    "architect-build",
    "A", "B", "C", "D", "a", "b", "c", "d",
)


def _prompt_architect_roles(base: dict) -> None:
    """architect-build (workflow D, Consensus Build - preview) requires a
    roles: block (config validation); prompt the mapping when D is chosen
    interactively."""
    roles = dict(base.get("roles") or {})
    roles["architect"] = _prompt(
        "Architect role contributor (expensive model: authors the spec, "
        "rules accept/revise/kill)", roles.get("architect", "claude"))
    roles["builder"] = _prompt(
        "Builder role contributor (cheap model: writes code in the lane)",
        roles.get("builder", "codex"))
    roles["reviewer"] = _prompt(
        "Reviewer role contributor (pre-checks the builder's diff)",
        roles.get("reviewer", "codex"))
    base["roles"] = roles


def _prompt(label: str, default: str, valid: list[str] | None = None) -> str:
    """Single-line prompt with default. Raises KeyboardInterrupt on Ctrl+C/EOF."""
    suffix = f" [{default}]"
    if valid:
        suffix = f" ({'/'.join(valid)})" + suffix
    while True:
        try:
            raw = input(f"{label}{suffix}: ").strip()
        except EOFError as exc:
            raise KeyboardInterrupt from exc
        choice = raw or default
        if valid and choice not in valid:
            print(f"  invalid; expected one of {valid}", file=sys.stderr)
            continue
        return choice


def _prompt_int(label: str, default: int) -> int:
    while True:
        try:
            raw = input(f"{label} [{default}]: ").strip()
        except EOFError as exc:
            raise KeyboardInterrupt from exc
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print("  invalid; expected an integer", file=sys.stderr)


def _apply_cli_overrides(args, base: dict) -> None:
    """Apply explicit CLI flags to `base` config (mutates in place).

    Run before prompting so dependent defaults derive from the operator's
    expressed intent, not from stale base values (codex-rev-003).
    """
    if args.contributors is not None:
        base["contributors"]["enabled"] = [c.strip() for c in args.contributors.split(",") if c.strip()]
    if args.workflow is not None:
        base["workflow"]["mode"] = cfg.WORKFLOW_ALIASES.get(args.workflow, args.workflow)
    if args.convergence is not None:
        base["convergence"]["rule"] = args.convergence
    if args.independence is not None:
        base["workflow"]["independence"] = args.independence
    if args.finding_disposition is not None:
        base["convergence"]["finding_disposition"] = args.finding_disposition
    if args.snapshot_trigger is not None:
        base["snapshots"]["trigger"] = args.snapshot_trigger
    if args.snapshot_every_iterations is not None:
        base["snapshots"]["periodic"]["every_iterations"] = args.snapshot_every_iterations
    if args.patch_authoring is not None:
        base["patches"]["authoring"] = args.patch_authoring
    if args.timeout_policy is not None:
        base["workflow"]["timeout_policy"] = args.timeout_policy
    if getattr(args, "governance_mode", None) is not None:
        base["governance"]["mode"] = args.governance_mode


def _which_flags_set(args) -> set[str]:
    """Return the names of dimension flags the user passed explicitly."""
    keys = (
        "contributors", "workflow", "convergence", "independence",
        "finding_disposition", "snapshot_trigger", "snapshot_every_iterations",
        "patch_authoring", "timeout_policy", "governance_mode",
    )
    return {k for k in keys if getattr(args, k, None) is not None}


def interactive_overrides(args, repo_root: Path, base: dict, fresh: bool) -> None:
    """Prompt the user for each configurability dimension. Mutates `base` in place.

    Flags already supplied on the command line take precedence (no prompt).
    When `fresh` is True (no existing config / not reconfigure), missing
    contributors are auto-suggested from PATH. When False, current base values
    are the prompt defaults (preserving existing config).

    Raises KeyboardInterrupt on Ctrl+C / EOF - caller maps to exit code 1.
    """
    set_flags = _which_flags_set(args)

    # Contributors.
    if "contributors" not in set_flags:
        if fresh:
            # FRESH path uses the v1.18.0 numbered multi-select over merged
            # profiles ([ok] installed / [x] missing, min-2 re-prompt, claude
            # optional) - wired here per codex-rev-001 (the helper existed but
            # was never reached by the wizard flow).
            profiles = _load_merged_profiles(
                (base.get("contributors") or {}).get("profiles")
            )
            selection = _select_contributors_interactive(profiles)
            hp = _prompt_host_peer_followup(selection, profiles, default_yes=False)
            if hp:
                selection.append(hp)
            base["contributors"]["enabled"] = selection
        else:
            profiles = _load_merged_profiles((base.get("contributors") or {}).get("profiles"))
            _reconfigure_contributors(base, profiles)
    n_contributors = len(base["contributors"]["enabled"])

    # Workflow mode.
    if "workflow" not in set_flags:
        if fresh:
            default_workflow = (
                cfg.WORKFLOW_PROPOSE_CONVERGE if n_contributors >= 2 else cfg.WORKFLOW_POST_REVIEW
            )
        else:
            default_workflow = base["workflow"]["mode"]
        # iter-workflow-abc-introduce: Workflow C (autonomous-execute) added.
        # Letter aliases A/B/C accepted by WORKFLOW_ALIASES; semantic strings
        # remain canonical for the underlying value.
        choice = _prompt(
            "Workflow mode (A=propose-converge, B=post-review, "
            "C=autonomous-execute, D=architect-build [Consensus Build, "
            "preview], advisory)",
            default_workflow,
            # codex-rev-002: the prompt advertises the letter aliases, so
            # `valid` MUST accept them too - otherwise _prompt rejects the very
            # input it told the user to type. The alias -> semantic resolution
            # below (WORKFLOW_ALIASES.get) then normalizes the stored value.
            valid=list(WORKFLOW_PROMPT_VALID),
        )
        # Resolve letter alias (A/B/C/D) to semantic string before storing.
        base["workflow"]["mode"] = cfg.WORKFLOW_ALIASES.get(choice, choice)
        if base["workflow"]["mode"] == cfg.WORKFLOW_ARCHITECT_BUILD:
            # Consult Q3: D needs the roles mapping; prompt it here so the
            # interactive path round-trips to a VALID architect-build config.
            _prompt_architect_roles(base)

    # Convergence rule.
    if "convergence" not in set_flags:
        if fresh:
            if base["workflow"]["mode"] == cfg.WORKFLOW_ADVISORY:
                default_rule = cfg.CONVERGE_ADVISORY
            elif n_contributors >= 3:
                default_rule = cfg.CONVERGE_STRICT_MAJ
            else:
                default_rule = cfg.CONVERGE_UNANIMOUS
        else:
            default_rule = base["convergence"]["rule"]
        base["convergence"]["rule"] = _prompt(
            "Convergence rule", default_rule, valid=list(cfg.VALID_CONVERGENCE)
        )

    # Independence model.
    if "independence" not in set_flags:
        if fresh:
            default_ind = (
                cfg.INDEPENDENCE_BLIND if base["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE
                else cfg.INDEPENDENCE_VISIBLE
            )
        else:
            default_ind = base["workflow"]["independence"]
        base["workflow"]["independence"] = _prompt(
            "Independence model", default_ind, valid=list(cfg.VALID_INDEPENDENCE)
        )

    # Finding disposition. iter-three-gaps: workflow #4 defaults to
    # weighted-synthesis (per doctrine); workflow #3 keeps the existing
    # all-or-nothing default.
    if "finding_disposition" not in set_flags:
        if fresh:
            if base["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE:
                default_disp = cfg.DISPOSITION_WEIGHTED_SYNTHESIS
            else:
                default_disp = cfg.DISPOSITION_ALL_OR_NOTHING
        else:
            default_disp = base["convergence"]["finding_disposition"]
        base["convergence"]["finding_disposition"] = _prompt(
            "Finding disposition", default_disp, valid=list(cfg.VALID_DISPOSITION)
        )

    # Snapshot trigger + cadence.
    if "snapshot_trigger" not in set_flags:
        default_trig = base["snapshots"]["trigger"]
        base["snapshots"]["trigger"] = _prompt(
            "Snapshot trigger", default_trig, valid=list(cfg.VALID_SNAPSHOT_TRIGGER)
        )
    if "snapshot_every_iterations" not in set_flags:
        default_every = base["snapshots"]["periodic"]["every_iterations"]
        base["snapshots"]["periodic"]["every_iterations"] = _prompt_int(
            "Periodic snapshot cadence (iterations)", default_every
        )

    # Patch authoring.
    if "patch_authoring" not in set_flags:
        default_auth = base["patches"]["authoring"]
        base["patches"]["authoring"] = _prompt(
            "Patch authoring policy", default_auth, valid=list(cfg.VALID_PATCH_AUTHORING)
        )

    # Timeout policy.
    if "timeout_policy" not in set_flags:
        default_to = base["workflow"]["timeout_policy"]
        base["workflow"]["timeout_policy"] = _prompt(
            "Timeout policy", default_to, valid=list(cfg.VALID_TIMEOUT_POLICY)
        )


def build_config_from_flags(args, repo_root: Path, interactive: bool = False) -> dict:
    """Construct a config dict from CLI args + defaults. Validates before return.

    When `--reconfigure` and an existing config is present, the existing config
    is the starting point; auto-detection and derived defaults are SKIPPED for
    any dimension that already has a value (codex-rev-001). Explicit CLI flags
    always override.
    """
    config_path = _resolve_config_path(args, repo_root)
    existing_loaded = False
    if args.reconfigure and config_path.exists():
        try:
            base = cfg.load(config_path)
            existing_loaded = True
        except cfg.ConfigValidationError:
            base = cfg.default_config()
    else:
        base = cfg.default_config()

    # Apply explicit CLI flags FIRST so prompts / derived defaults see them.
    _apply_cli_overrides(args, base)
    set_flags = _which_flags_set(args)

    if interactive:
        interactive_overrides(args, repo_root, base, fresh=not existing_loaded)
    else:
        # Non-interactive: only apply derived defaults when we are NOT
        # preserving an existing config; in reconfigure mode the existing
        # values must survive any unspecified flag (codex-rev-001).
        if not existing_loaded:
            if "contributors" not in set_flags:
                base["contributors"]["enabled"] = _detect_available_contributors(repo_root)
            n_contributors = len(base["contributors"]["enabled"])
            if "workflow" not in set_flags:
                base["workflow"]["mode"] = (
                    cfg.WORKFLOW_PROPOSE_CONVERGE if n_contributors >= 2 else cfg.WORKFLOW_POST_REVIEW
                )
            if "convergence" not in set_flags:
                if base["workflow"]["mode"] == cfg.WORKFLOW_ADVISORY:
                    base["convergence"]["rule"] = cfg.CONVERGE_ADVISORY
                elif n_contributors >= 3:
                    base["convergence"]["rule"] = cfg.CONVERGE_STRICT_MAJ
                else:
                    base["convergence"]["rule"] = cfg.CONVERGE_UNANIMOUS
            if "independence" not in set_flags:
                base["workflow"]["independence"] = (
                    cfg.INDEPENDENCE_BLIND if base["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE
                    else cfg.INDEPENDENCE_VISIBLE
                )
            if "finding_disposition" not in set_flags:
                # iter-three-gaps doctrine: workflow #4 defaults to
                # weighted-synthesis; workflow #3 / advisory keep
                # all-or-nothing as the default.
                if base["workflow"]["mode"] == cfg.WORKFLOW_PROPOSE_CONVERGE:
                    base["convergence"]["finding_disposition"] = cfg.DISPOSITION_WEIGHTED_SYNTHESIS
                else:
                    base["convergence"]["finding_disposition"] = cfg.DISPOSITION_ALL_OR_NOTHING

    base["project"]["name"] = repo_root.name
    cfg.validate(base)
    return base


def write_config(config: dict, path: Path) -> None:
    """Atomically write config YAML to path.

    v1.24 (fix 11): confirmed atomic - tmp.replace() is an atomic rename, so a
    concurrent writer is last-writer-wins rather than corrupting config.yaml. Full
    cross-process locking is overkill; atomic replace suffices. (Same contract as
    _atomic_write_json / _atomic_write_text.)
    """
    # v1.26 (kimi): route through the hardened atomic writer (secure tmp + O_EXCL)
    # instead of an inlined predictable-tmp pattern that could follow a symlink.
    _atomic_write_text(
        path, yaml.safe_dump(config, sort_keys=False, default_flow_style=False)
    )


def _config_diff(old: dict, new: dict) -> str:
    """Return a well-formed unified diff of two configs serialized as YAML."""
    a = yaml.safe_dump(old, sort_keys=False, default_flow_style=False).splitlines()
    b = yaml.safe_dump(new, sort_keys=False, default_flow_style=False).splitlines()
    # Use lineterm='' + join with '\n' so control records (---, +++, @@) and
    # body lines all end with one newline (codex-rev-004).
    diff_lines = list(difflib.unified_diff(a, b, fromfile="existing", tofile="proposed", lineterm=""))
    return "\n".join(diff_lines)


def update_gitignore(repo_root: Path) -> bool:
    """Add managed paths to .gitignore between bracketed markers. Returns True if changed.

    Idempotent: complete marker pairs are stripped, then a single fresh managed
    block is appended. If the file contains an OPEN marker without a CLOSE
    marker (or vice versa), the file is treated as foreign and left untouched
    - a fresh block is appended but no existing lines are deleted
    (codex-rev-005).
    """
    gi = repo_root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""

    # Validate that markers are well-ordered: each open is followed by a close
    # before another open, and there are no orphans. Counts must match AND
    # ordering must be valid (codex pass-2 rev-005 + codex pass-3 rev-001).
    markers_well_formed = True
    depth = 0
    for line in existing.splitlines():
        if line == GITIGNORE_OPEN_MARKER:
            depth += 1
            if depth != 1:
                markers_well_formed = False
                break
        elif line == GITIGNORE_CLOSE_MARKER:
            depth -= 1
            if depth != 0:
                markers_well_formed = False
                break
    if depth != 0:
        markers_well_formed = False

    stripped_lines: list[str] = []
    if markers_well_formed:
        in_block = False
        for line in existing.splitlines():
            if line == GITIGNORE_OPEN_MARKER:
                in_block = True
                continue
            if line == GITIGNORE_CLOSE_MARKER:
                in_block = False
                continue
            if not in_block:
                stripped_lines.append(line)
    else:
        # Malformed (orphan markers, reversed order, nested, etc.):
        # preserve every existing line; never silently drop content.
        # BUT we still strip OUR own previously appended exact managed block at
        # the tail so reruns remain idempotent (codex pass-4 rev-001).
        raw_lines = existing.splitlines()
        block_size = len(GITIGNORE_MANAGED_PATHS) + 2  # open + paths + close
        if (
            len(raw_lines) >= block_size
            and raw_lines[-block_size] == GITIGNORE_OPEN_MARKER
            and raw_lines[-1] == GITIGNORE_CLOSE_MARKER
            and raw_lines[-(block_size - 1):-1] == list(GITIGNORE_MANAGED_PATHS)
        ):
            # Drop the trailing managed block + any blank separator immediately
            # before it so the next append doesn't accumulate blank lines.
            raw_lines = raw_lines[:-block_size]
            while raw_lines and raw_lines[-1].strip() == "":
                raw_lines.pop()
        stripped_lines = raw_lines

    new_block = [GITIGNORE_OPEN_MARKER, *GITIGNORE_MANAGED_PATHS, GITIGNORE_CLOSE_MARKER]
    new_content_lines = list(stripped_lines)
    if new_content_lines and new_content_lines[-1].strip() != "":
        new_content_lines.append("")
    new_content_lines.extend(new_block)
    new_content = "\n".join(new_content_lines) + "\n"

    if new_content == existing:
        return False
    # v1.26 (kimi): atomic + symlink-safe write (was a bare gi.write_text()).
    _atomic_write_text(gi, new_content)
    return True


# v1.29.0 (init-already-installed-ux consult): the binary<->skill contract for
# "this project is already bootstrapped". Emitted as the FIRST line of stdout
# (no variable data) so the consensus skill can anchor on it without parsing
# prose. Paired with exit code 4. Keep this string in sync with the matcher in
# claude_extensions/skills/consensus/SKILL.md and commands/consensus-init.md
# (a contract regression test enforces this).
ALREADY_CONFIGURED_TOKEN = "STATUS: already-configured"

# v1.29.x (umbrella-guard consult): stdout-line-1 contract token for "this looks
# like a workspace folder, not a project". DISTINCT from ALREADY_CONFIGURED_TOKEN.
# The consensus skill keys on it (+ exit 8) to present an AskUserQuestion.
WORKSPACE_UMBRELLA_TOKEN = "STATUS: looks-like-workspace-umbrella"


def _looks_like_workspace_umbrella(root: Path) -> list[Path]:
    """Return `root`'s immediate child directories that are git repos (contain a
    `.git` DIRECTORY), IFF `root` itself is NOT a git repo. Empty list = not an
    umbrella.

    A `.git` *file* (submodule / linked-worktree gitlink) does NOT count - so a
    project that vendors submodules is never misflagged. Per-entry errors
    (PermissionError/OSError) are swallowed; symlinked children are not followed.
    """
    if (root / ".git").exists():
        # root is itself a repo (dir) or a linked worktree (file) -> a project.
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


# Marker env vars set by agent harnesses (Claude Code) and CI. When any is
# present there is no human to answer a prompt, so the wizard MUST take the
# non-interactive path regardless of isatty(). This is the reliable signal on
# Windows, where a ConPTY-backed subprocess can report isatty()=True even though
# it is launched by an agent and unanswerable (the v1.33.4 Windows report:
# `consensus-init` "went interactive asking for reviewer selection" under a
# Claude Code skill). CLAUDECODE / AI_AGENT are set by Claude Code; CI is the
# de-facto standard set by every major CI runner.
_NON_INTERACTIVE_ENV_MARKERS = ("CLAUDECODE", "AI_AGENT", "CI")


def _running_under_agent_or_ci() -> bool:
    """True when an agent/CI marker env var is set (no human to prompt)."""
    return any(os.environ.get(var) for var in _NON_INTERACTIVE_ENV_MARKERS)


def _stdin_is_interactive() -> bool:
    """True only when stdin is a real interactive TTY with a human to answer.

    Claude Code's Bash tool, CI runners, and pipes all present a non-TTY stdin;
    there, the wizard's input() prompts hit EOF on the first read (an uncaught
    EOFError crash, or a premature "aborted by user"). Callers downgrade to the
    non-interactive path when this is False. Defensive: a closed / detached
    stdin can make isatty() raise (ValueError/OSError) or stdin can be None.

    An agent/CI marker env var (CLAUDECODE / AI_AGENT / CI) forces False BEFORE
    consulting isatty(): on Windows a ConPTY subprocess reports isatty()=True
    even when launched by an agent that cannot answer the prompt.
    """
    if _running_under_agent_or_ci():
        return False
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (ValueError, OSError):
        return False


# v1.29.1 (verify/repair consult): version-STABLE summary prefixes. The consensus
# skill parses these to relay repair results - treat as a contract (a regression
# test pins them), like ALREADY_CONFIGURED_TOKEN.
REPAIR_OK = "OK:"            # present and healthy
REPAIR_FIXED = "REPAIRED:"   # was missing, recreated
REPAIR_SKIP = "SKIP:"        # exists but diverged from shipped; left intact
REPAIR_GLOBAL = "REPORT-GLOBAL:"  # global enforcement issue, not repaired here


@dataclasses.dataclass
class RepairComponent:
    """One health component's outcome. state is one of:
    'ok' | 'repaired' | 'skipped_diverged' | 'missing_config' |
    'invalid_config' | 'report_global'."""
    name: str
    state: str
    detail: str = ""


def _repair_exit_code(components: list["RepairComponent"]) -> int:
    """Aggregate component states into the repair exit code.

    0 = healthy or fully repaired; 2 = config missing; 3 = config invalid;
    7 = repair incomplete (diverged left for --force, global enforcement dead, OR
    any component that did not end healthy). Config prerequisite (2/3) outranks 7
    (you can't repair #2-#5 without a valid config). ONLY 'ok' and 'repaired' are
    success states.

    gemini-rev-001: this is FAIL-SAFE. Previously it returned 0 unless a specific
    bad state was present, so a component that failed to repair (a raised write,
    or any state outside the known-bad set) could still report success. Now
    success (0) requires EVERY component to be 'ok' or 'repaired'; any other state
    - including an unrecognized/failed one - maps to 7.
    """
    states = {c.state for c in components}
    if "missing_config" in states:
        return 2
    if "invalid_config" in states:
        return 3
    _SUCCESS_STATES = {"ok", "repaired"}
    if states - _SUCCESS_STATES:
        # skipped_diverged / report_global / repair_failed / anything unexpected.
        return 7
    return 0


def _repair_check_config(config_path: Path) -> tuple[RepairComponent, str]:
    """#1 config.yaml - NOT repairable (can't synthesize panel choices)."""
    if not config_path.exists():
        return (RepairComponent("config.yaml", "missing_config"),
                f"{REPAIR_SKIP} config.yaml missing - run `consensus init` "
                f"(cannot synthesize your panel choices)")
    try:
        cfg.load(config_path)
    except cfg.ConfigValidationError as exc:
        return (RepairComponent("config.yaml", "invalid_config"),
                f"{REPAIR_SKIP} config.yaml invalid ({exc}) - run "
                f"`consensus init --reconfigure`")
    return (RepairComponent("config.yaml", "ok"), f"{REPAIR_OK} config.yaml")


def _repair_check_mcp(repo_root: Path, *, dry_run: bool) -> tuple[RepairComponent, str]:
    """#2 .mcp.json - repair when the consensus-mcp entry is missing; report when
    present-but-diverged; ok when present-and-matching."""
    mcp_path = _resolve_mcp_json_path(repo_root)
    existing, load_err = _load_existing_mcp_json(mcp_path)
    if load_err is not None:
        # codex-rev-001: the file is PRESENT but unparseable. The old code discarded
        # this error and fell through to the "missing -> write + report repaired"
        # branch, claiming success even when nothing could be safely written (we
        # must not clobber a corrupt user file blindly). Report it instead so the
        # exit code is non-zero and the user fixes/removes it (or passes --force).
        return (RepairComponent(".mcp.json", "skipped_diverged"),
                f"{REPAIR_SKIP} .mcp.json is present but unparseable ({load_err}); "
                f"NOT auto-repaired - fix or remove it, or pass --force to overwrite")
    command, mcp_args, _ = _resolve_mcp_command(None)
    state_root = repo_root / "consensus-state"
    expected = _build_consensus_mcp_entry(command, mcp_args, state_root, repo_root)
    servers = (existing or {}).get("mcpServers", {})
    have = servers.get("consensus-mcp")
    if have is None:
        if not dry_run:
            _write_mcp_json(repo_root, state_root, repo_root, command, mcp_args)
        return (RepairComponent(".mcp.json", "repaired"),
                f"{REPAIR_FIXED} .mcp.json (consensus-mcp server entry)")
    if not _json_semantically_equal(have, expected):
        return (RepairComponent(".mcp.json", "skipped_diverged"),
                f"{REPAIR_SKIP} .mcp.json consensus-mcp entry diverges from "
                f"shipped; pass --force to overwrite")
    return (RepairComponent(".mcp.json", "ok"), f"{REPAIR_OK} .mcp.json")


def _repair_check_gitignore(repo_root: Path, *, dry_run: bool) -> tuple[RepairComponent, str]:
    """#3 .gitignore managed block - re-add when absent."""
    gi = repo_root / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if GITIGNORE_OPEN_MARKER in text:
        return (RepairComponent(".gitignore", "ok"), f"{REPAIR_OK} .gitignore managed block")
    if not dry_run:
        update_gitignore(repo_root)
    return (RepairComponent(".gitignore", "repaired"), f"{REPAIR_FIXED} .gitignore managed block")


def _repair_check_agents(repo_root: Path, *, dry_run: bool) -> tuple[RepairComponent, str]:
    """#4 .claude/agents/ - re-copy missing subagent files; report diverged (installer SKIPs them)."""
    agents_dir = repo_root / ".claude" / "agents"
    source_root = _agents_source_root()

    missing: list[str] = []
    diverged: list[str] = []
    for fname in _PROJECT_AGENT_FILES:
        dst = agents_dir / fname
        if not dst.exists():
            missing.append(fname)
        else:
            src = source_root / fname
            try:
                if src.read_text(encoding="utf-8") != dst.read_text(encoding="utf-8"):
                    diverged.append(fname)
            except OSError:
                # If we can't read source or dest, treat as diverged (safe default).
                diverged.append(fname)

    if missing:
        # Install writes the missing ones; already-diverged ones are left alone by installer.
        if not dry_run:
            _install_project_agents(repo_root, force=False)
        note = f" ({', '.join(missing)})"
        if diverged:
            note += f"; diverged (pass --force): {', '.join(diverged)}"
        return (RepairComponent(".claude/agents", "repaired"),
                f"{REPAIR_FIXED} .claude/agents{note}")

    if diverged:
        return (RepairComponent(".claude/agents", "skipped_diverged"),
                f"{REPAIR_SKIP} .claude/agents - diverged from shipped: "
                f"{', '.join(diverged)}; pass --force to overwrite")

    return (RepairComponent(".claude/agents", "ok"), f"{REPAIR_OK} .claude/agents")


def _enabled_contributors_and_profiles(loaded: dict) -> tuple[list[str], dict]:
    """Extract (enabled, merged_profiles) from a loaded config dict.

    Mirrors the derivation in cmd_init before it calls _provision_instruction_files.
    """
    existing_profiles = (loaded.get("contributors") or {}).get("profiles")
    merged_profiles = _load_merged_profiles(existing_profiles)
    enabled = (loaded.get("contributors") or {}).get("enabled") or []
    return enabled, merged_profiles


def _instruction_files_missing_block(
    enabled: list[str], profiles: dict, repo_root: Path,
    governance_mode: str = cfg.GOVERNANCE_ON_DEMAND,
) -> list[Path]:
    """Return instruction files that are absent or have stale mode guidance."""
    targets: list[str] = []
    for name in enabled:
        profile = profiles.get(name)
        if profile is None:
            continue
        filename = (profile.get("instructions") or {}).get("filename")
        if filename and filename not in targets:
            targets.append(filename)
    missing: list[Path] = []
    for filename in targets:
        path = repo_root / filename
        if not path.exists():
            missing.append(path)
        else:
            text = path.read_text(encoding="utf-8")
            expected_heading = (
                "consensus-mcp continuous governance is active"
                if governance_mode == cfg.GOVERNANCE_CONTINUOUS
                else "consensus-mcp is available on demand"
            )
            if (INSTRUCTION_BEGIN_MARKER not in text
                    or expected_heading not in text):
                missing.append(path)
    return missing


def _repair_check_instructions(repo_root: Path, *, dry_run: bool) -> tuple[RepairComponent, str]:
    """#5 per-AI instruction managed blocks - re-seed when absent. Reads enabled
    contributors from the existing config."""
    config_path = repo_root / ".consensus" / "config.yaml"
    loaded = cfg.load(config_path)
    enabled, profiles = _enabled_contributors_and_profiles(loaded)
    mode = (loaded.get("governance") or {}).get(
        "mode", cfg.GOVERNANCE_ON_DEMAND)
    needs = _instruction_files_missing_block(enabled, profiles, repo_root, mode)
    if not needs:
        return (RepairComponent("instructions", "ok"), f"{REPAIR_OK} instruction files")
    if not dry_run:
        _provision_instruction_files(enabled, profiles, repo_root, mode)
    return (RepairComponent("instructions", "repaired"),
            f"{REPAIR_FIXED} instruction files ({', '.join(str(p) for p in needs)})")


def _repair_check_enforcement(claude_home: Path) -> tuple[RepairComponent, str]:
    """#6 global enforcement - READ-ONLY. Healthy iff settings.json carries the
    consensus hooks for every expected event AND every referenced hook script
    exists on disk. Never writes ~/.claude (that's --install-claude-code)."""
    settings_path = _resolve_settings_json_path(claude_home)
    settings, _ = _load_existing_settings_json(settings_path)
    expected = _build_consensus_hook_groups(claude_home)
    hooks = (settings or {}).get("hooks", {})
    for event in expected:
        if not hooks.get(event):
            return (RepairComponent("enforcement", "report_global"),
                    f"{REPAIR_GLOBAL} enforcement: settings.json missing consensus "
                    f"{event} hook - run `consensus-init --install-claude-code`")
    # Check that every unique hook script referenced in _CONSENSUS_HOOK_SPECS
    # is actually installed under claude_home/hooks/.  We use script-name lookup
    # (via _installed_hook_script_path) rather than string-sniffing the command
    # string, which avoids any quoting/platform sensitivity.
    seen: set[str] = set()
    for _event, _matcher, script in _CONSENSUS_HOOK_SPECS:
        if script in seen:
            continue
        seen.add(script)
        installed = claude_home / "hooks" / script
        if not installed.exists():
            return (RepairComponent("enforcement", "report_global"),
                    f"{REPAIR_GLOBAL} enforcement: hook script missing ({installed}) "
                    f"- run `consensus-init --install-claude-code`")
    return (RepairComponent("enforcement", "ok"), f"{REPAIR_OK} enforcement")


def _verify_repair_install(repo_root: Path, *, dry_run: bool,
                           claude_home: Path) -> tuple[list[str], int]:
    """Comprehensive verify + non-destructive repair. Returns (summary_lines, exit_code)."""
    config_path = repo_root / ".consensus" / "config.yaml"
    comps: list[RepairComponent] = []
    lines: list[str] = []

    c1, l1 = _repair_check_config(config_path)
    comps.append(c1); lines.append(l1)
    if c1.state in ("missing_config", "invalid_config"):
        # #2-#5 cannot be derived without a valid config; report #6 then stop.
        c6, l6 = _repair_check_enforcement(claude_home)
        comps.append(c6); lines.append(l6)
        return lines, _repair_exit_code(comps)

    for check in (_repair_check_mcp, _repair_check_gitignore,
                  _repair_check_agents, _repair_check_instructions):
        # gemini-rev-001: a repair attempt that RAISES (e.g. an OSError writing
        # .mcp.json) must NOT crash the command or be silently dropped - record it
        # as a failed component so the exit code reflects the incomplete repair.
        try:
            c, l = check(repo_root, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - any failure is a repair failure
            name = getattr(check, "__name__", "repair_check").replace("_repair_check_", "")
            c = RepairComponent(name, "repair_failed", str(exc))
            l = f"{REPAIR_SKIP} {name}: repair FAILED ({type(exc).__name__}: {exc})"
        comps.append(c); lines.append(l)
    # kimi-rev-002: enforcement check gets the same exception-safe treatment as
    # #2-#5 so a raised read can't crash --repair or yield a false success.
    try:
        c6, l6 = _repair_check_enforcement(claude_home)
    except Exception as exc:  # noqa: BLE001
        c6 = RepairComponent("enforcement", "repair_failed", str(exc))
        l6 = f"{REPAIR_SKIP} enforcement: check FAILED ({type(exc).__name__}: {exc})"
    comps.append(c6); lines.append(l6)
    return lines, _repair_exit_code(comps)


def _prompt_existing_config_action(config_path: Path) -> str:
    """Interactive menu shown when config already exists (TTY callers only).

    Returns "leave" | "repair" | "reconfigure" | "force". Empty input and
    EOF/Ctrl-D both default to "leave" (the safe no-op). Ctrl-C propagates as
    KeyboardInterrupt (caller maps it to exit 1), matching the rest of the
    wizard.
    """
    print(f"consensus-mcp is already configured here: {config_path}")
    print("  [1] Leave as-is - no changes            (default)")
    print("  [2] Verify / repair - re-create missing pieces, report diverged")
    print("  [3] Reconfigure - re-prompt, keep current settings as defaults, show diff")
    print("  [4] Force overwrite - discard local config edits, write fresh")
    while True:
        try:
            raw = input("Choose [1/2/3/4, default 1]: ").strip()
        except EOFError:
            # Deliberate divergence from the other prompts (which map EOF ->
            # KeyboardInterrupt -> exit 1): this menu has a safe no-op default,
            # so an unattended/EOF caller should accept the default, not abort.
            return "leave"
        if raw in ("", "1"):
            return "leave"
        if raw == "2":
            return "repair"
        if raw == "3":
            return "reconfigure"
        if raw == "4":
            return "force"
        print("Please enter 1, 2, 3, or 4.", file=sys.stderr)


def cmd_init(args) -> int:
    """Implement the `consensus init` command."""
    repo_root = _detect_repo_root()
    config_path = _resolve_config_path(args, repo_root)

    if args.check:
        if not config_path.exists():
            print(f"error: {config_path} does not exist", file=sys.stderr)
            return 2
        try:
            loaded = cfg.load(config_path)
        except cfg.ConfigValidationError as exc:
            print(f"error: invalid config: {exc}", file=sys.stderr)
            return 3
        print(yaml.safe_dump(loaded, sort_keys=False, default_flow_style=False))
        return 0

    # goal item 5: optional verification pass. Runs against the EXISTING config
    # (offline, no model calls) and exits non-zero if there are hard problems
    # (missing scripts / uninstalled selected CLIs) so a cold user can confirm
    # "no errors, no missing scripts" before their first real question.
    if getattr(args, "verify", False):
        if not config_path.exists():
            print(f"error: {config_path} does not exist - run `consensus init` "
                  f"first, then --verify", file=sys.stderr)
            return 2
        try:
            loaded = cfg.load(config_path)
        except cfg.ConfigValidationError as exc:
            print(f"error: invalid config: {exc}", file=sys.stderr)
            return 3
        contributors = loaded.get("contributors") or {}
        v_profiles = _load_merged_profiles(contributors.get("profiles"))
        v_enabled = contributors.get("enabled") or []
        problems = _run_verification_pass(v_enabled, v_profiles)
        return 0 if problems == 0 else 2

    if args.print_defaults:
        print(yaml.safe_dump(cfg.default_config(), sort_keys=False, default_flow_style=False))
        return 0

    # Auto-init support: the Claude Code skill calls this BEFORE writing config to
    # offer the user a panel of the AIs actually present (the "which AIs" question).
    # Prints JSON listing every INDEPENDENT contributor with its install status +
    # whether it is the host, derived dynamically from the merged profiles (so a
    # custom-profile AI shows up too). Read-only; never writes anything.
    if getattr(args, "detect_contributors", False):
        profiles = _load_merged_profiles(None)
        contributors = []
        for name in _independent_ordered_names(profiles):
            profile = profiles[name]
            contributors.append({
                "name": name,
                "installed": _profile_installed(profile),
                "host": profile.get("kind") == profiles_mod.KIND_HOST,
            })
        print(json.dumps({"contributors": contributors}, indent=2))
        return 0

    # iter-0040 hot-fix: `--install-claude-code` is a one-time GLOBAL operation
    # (copy skill + slash command into ~/.claude). It is NOT a per-project
    # bootstrap, and should not trigger the interactive wizard or write
    # .consensus/config.yaml / .mcp.json. Short-circuit here, run the install,
    # and exit. Users who want BOTH the global pack install AND a per-project
    # bootstrap should run the two commands separately.
    if getattr(args, "uninstall_claude_code", False):
        claude_home = _resolve_claude_home()
        for line in _uninstall_claude_settings_json(claude_home):
            print(line)
        return 0

    if getattr(args, "install_claude_code", False):
        # --- return-code convention for the global --install-claude-code path ---
        #   0  ok
        #   5  managed-file SKIP (a divergent installed file was KEPT, not updated)
        #   6  INCOMPLETE install: freshness-stale ABORT (fix 8) OR settings.json
        #      hook-activation failure (fix 7)
        # Precedence (more severe / earlier-detected wins): the freshness ABORT
        # (fix 8) happens BEFORE any asset is copied, so it returns 6 first. After
        # copying, an activation failure (6, incomplete) outranks a managed-file
        # SKIP (5, stale-but-functional) because a dead hook set leaves enforcement
        # OFF, which is the worse state. (.consensus per-project agent SKIP -> 7 is
        # a separate, project-only path; it never co-occurs here.)
        claude_home = _resolve_claude_home()
        # Finding 5 / fix 8 (freshness self-check): a STALE or partial package
        # must NOT silently deploy an old/incomplete asset set. ABORT before
        # copying anything (return 6, incomplete) unless --force overrides.
        src_root = _claude_extensions_source_root()
        present_vendored = sum(
            1 for rel, _ in _CLAUDE_EXTENSION_FILES
            if rel.startswith("skills/consensus-") and rel.endswith("/SKILL.md")
            and (src_root / rel).exists()
        )
        if present_vendored < _EXPECTED_VENDORED_SKILLS:
            print(
                f"WARNING: this consensus-mcp install ships only {present_vendored} of "
                f"the expected {_EXPECTED_VENDORED_SKILLS} vendored skills - it looks "
                f"STALE or partial. Upgrade the package (e.g. "
                f"`pipx install --force consensus-mcp`) before installing, or you will "
                f"deploy an old/incomplete skill set.", file=sys.stderr)
            if not args.force:
                # fix 8: abort BEFORE copying any asset; --force allows proceeding
                # (with the warning already printed above).
                print(
                    "ABORTING: refusing to deploy a stale/partial skill pack. "
                    "Upgrade consensus-mcp, or pass --force to install anyway.",
                    file=sys.stderr)
                return 6
        ext_lines = list(_install_claude_extensions(claude_home, force=args.force))
        for line in ext_lines:
            print(line)
        # v1.21 (B5): copying the skill/command files does NOT activate the
        # enforcement hooks - Claude Code reads hooks only from settings.json.
        # Merge them in (idempotent, preserves unrelated user hooks).
        settings_lines = list(_install_claude_settings_json(claude_home, force=args.force))
        for line in settings_lines:
            print(line)
        # fix 7: if hooks could NOT be activated (e.g. a malformed existing
        # settings.json that _install_claude_settings_json refused to clobber),
        # the install is INCOMPLETE - enforcement is OFF. Return a distinct
        # nonzero (6) instead of a misleading 0. The function signals failure via
        # a WARN: status line (it never raises; it fails soft to avoid clobbering).
        settings_failed = any(ln.startswith("WARN:") for ln in settings_lines)
        # Finding 4 (stale-skill SKIP): a divergent managed file was KEPT (not
        # updated). Surface it LOUDLY so an incomplete upgrade is detectable;
        # silent staleness is the failure mode here.
        skipped = [ln for ln in ext_lines if ln.startswith("SKIP:")]
        if skipped:
            print(
                f"\nWARNING: {len(skipped)} managed file(s) were SKIPPED because the "
                f"installed copy diverges from this version - they are now STALE. "
                f"Re-run with --force to update them (this overwrites local edits):",
                file=sys.stderr)
            for ln in skipped:
                print(f"  - {ln[len('SKIP: '):]}", file=sys.stderr)
        if settings_failed:
            print(
                "\nERROR: consensus hooks were NOT activated (settings.json could "
                "not be updated) - enforcement is OFF. Fix the reported settings.json "
                "problem and re-run.", file=sys.stderr)
            return 6  # activation failure outranks a managed-file SKIP (see above)
        if skipped:
            return 5
        return 0

    # gemini pass-3 rev-001: --dry-run no longer forces non-interactive. Users
    # can preview an interactive session via `consensus init --dry-run`.
    # Interactivity is suppressed only by --non-interactive or --accept-defaults.
    # codex pass-4 rev-002: check existing-config guard BEFORE we build / prompt
    # so exit 4 wins over exit 1 (user abort during interactive) and exit 3
    # (invalid construction).
    # --force supersedes --reconfigure: a full overwrite has nothing to diff.
    if args.force and args.reconfigure:
        args.reconfigure = False

    if getattr(args, "repair", False):
        lines, code = _verify_repair_install(
            repo_root, dry_run=args.dry_run, claude_home=_resolve_claude_home())
        for line in lines:
            print(line)
        return code

    # v1.29.x (umbrella-guard consult): don't silently bootstrap a workspace
    # umbrella (a non-repo dir containing git repos). Fresh-init only - skip when
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
            # confirmed -> fall through to normal init

    # v1.29.0 (init-already-installed-ux consult): the existing-config guard is
    # install-AWARE. It still runs BEFORE we build/prompt so its disposition
    # wins over exit 1 (user abort) and exit 3 (invalid build). TTY -> menu;
    # non-TTY (Claude Code / CI / pipe / explicit non-interactive) -> the stable
    # machine-detectable contract (token on stdout line 1 + guidance on stderr)
    # + exit 4, which the consensus skill keys on. The token is emitted ONLY
    # here (never when --reconfigure/--force is set), so the skill's one-shot
    # re-invoke cannot loop.
    if config_path.exists() and not (args.reconfigure or args.force or args.repair):
        non_interactive_run = (
            args.non_interactive or args.accept_defaults
            or not _stdin_is_interactive()
        )
        if non_interactive_run:
            print(ALREADY_CONFIGURED_TOKEN)  # stdout, first line, exact, no variable data
            print(
                f"consensus-mcp is already configured at {config_path}. "
                f"Re-run with --reconfigure to update (keeps current settings as "
                f"defaults) or --force to overwrite - or run "
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
        if action == "repair":
            lines, code = _verify_repair_install(
                repo_root, dry_run=args.dry_run, claude_home=_resolve_claude_home())
            for line in lines:
                print(line)
            return code
        if action == "reconfigure":
            # Reconfigure re-prompts interactively (keeping the existing config
            # as defaults) and shows a diff - do NOT force non-interactive.
            args.reconfigure = True
        else:  # "force"
            # The menu WAS the interaction; overwrite with auto-detected
            # defaults rather than re-prompting every wizard dimension.
            args.force = True
            args.accept_defaults = True

    interactive = not (args.non_interactive or args.accept_defaults)
    if interactive and not _stdin_is_interactive():
        # No interactive terminal (Claude Code's Bash tool, CI, a pipe): the
        # input() prompts would hit EOF on the first read and crash / abort.
        # Fall back to the non-interactive path, which auto-detects available
        # reviewers and applies defaults; any explicit flags still take effect.
        interactive = False
        if getattr(args, "from_claude_code", False):
            print(
                "note: no interactive terminal detected (running under Claude "
                "Code); initializing with auto-detected reviewers + defaults. To "
                "customize, re-run with flags (e.g. --contributors, --workflow) "
                "or run `consensus init --reconfigure` in a terminal.",
                file=sys.stderr,
            )
        else:
            print(
                "note: stdin is not a TTY; initializing non-interactively with "
                "auto-detected reviewers + defaults. Pass --accept-defaults to "
                "silence this note, or explicit flags (--contributors, "
                "--workflow, ...) to customize.",
                file=sys.stderr,
            )

    try:
        new_config = build_config_from_flags(args, repo_root, interactive=interactive)
    except cfg.ConfigValidationError as exc:
        print(f"error: invalid config: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\naborted by user", file=sys.stderr)
        return 1

    if args.reconfigure and config_path.exists():
        try:
            existing = cfg.load(config_path)
            diff = _config_diff(existing, new_config)
            if diff:
                print("--- reconfigure diff ---")
                print(diff)
                print("--- end diff ---")
            else:
                print("reconfigure: no changes")
        except cfg.ConfigValidationError:
            print(f"note: existing config at {config_path} is invalid; overwriting")

    if args.dry_run:
        print(f"DRY RUN: would write {config_path}")
        print(yaml.safe_dump(new_config, sort_keys=False, default_flow_style=False))
        if not args.no_update_gitignore:
            print(f"would update .gitignore at {repo_root / '.gitignore'} (managed block)")
        else:
            print(".gitignore update skipped (--no-update-gitignore)")
        if not args.no_mcp_json:
            mcp_path = _resolve_mcp_json_path(repo_root)
            print(f"would write .mcp.json at {mcp_path} (consensus-mcp registered)")
        else:
            print(".mcp.json write skipped (--no-mcp-json)")
        if not args.no_agents:
            agents_dir = repo_root / ".claude" / "agents"
            for fname in _PROJECT_AGENT_FILES:
                print(f"would write subagent {agents_dir / fname}")
        else:
            print("subagent install skipped (--no-agents)")
        if not args.no_instructions:
            print("would seed per-AI instruction files (managed block)")
        else:
            print("instruction-file seeding skipped (--no-instructions)")
        return 0

    write_config(new_config, config_path)
    print(f"wrote {config_path}")

    if not args.no_update_gitignore:
        changed = update_gitignore(repo_root)
        if changed:
            print(f"updated .gitignore at {repo_root / '.gitignore'}")
        else:
            print(".gitignore already up to date")

    # v1.18.0: detect+guide for missing CLIs and seed per-AI instruction files
    # for the contributors that ended up enabled. Best-effort: profile load
    # failures must not abort a successful config write.
    try:
        existing_profiles = (new_config.get("contributors") or {}).get("profiles")
        merged_profiles = _load_merged_profiles(existing_profiles)
        enabled = (new_config.get("contributors") or {}).get("enabled") or []
        _detect_and_guide(enabled, merged_profiles)
        _print_panel_summary(enabled, merged_profiles)
        # goal item 5: offer the optional verification pass right after setup, so a
        # first-time user can confirm there are no errors / missing scripts before
        # their first question. Interactive only (a non-interactive run can call
        # `consensus init --verify` explicitly); default No to stay unobtrusive.
        if interactive and not args.non_interactive:
            try:
                ans = input("\nRun a quick verification pass now (no model calls)? "
                            "[y/N]: ").strip().lower()
            except EOFError:
                ans = ""
            if ans.startswith("y"):
                _run_verification_pass(enabled, merged_profiles)
        if not args.no_instructions:
            mode = (new_config.get("governance") or {}).get(
                "mode", cfg.GOVERNANCE_ON_DEMAND)
            for path in _provision_instruction_files(
                enabled, merged_profiles, repo_root, mode
            ):
                print(f"seeded instruction file {path}")
        else:
            print("instruction-file seeding skipped (--no-instructions)")
    except (OSError, ValueError) as exc:
        print(f"WARN: instruction/detect-guide step skipped: {exc}", file=sys.stderr)

    # recon #6: degenerate-panel guard for the NON-interactive bootstrap path.
    # The interactive path already re-prompts for >=2 reviewers; this warns when
    # a non-interactive run lands a single-reviewer (degraded) config.
    if not interactive:
        try:
            guard_profiles = _load_merged_profiles(
                (new_config.get("contributors") or {}).get("profiles")
            )
            guard_enabled = (new_config.get("contributors") or {}).get("enabled") or []
            _warn_degenerate_panel(guard_enabled, guard_profiles)
        except (OSError, ValueError):
            pass  # best-effort; never abort a successful write on a guard.

    # iter-0031: write .mcp.json by default (per iter-0030 converged plan).
    # recon #5: also record whether the resolved command resolves on PATH so the
    # status summary can warn about a silently-dead server.
    mcp_command_for_summary: str | None = None
    mcp_resolves_for_summary: bool | None = None
    if not args.no_mcp_json:
        try:
            command, mcp_args, is_portable = _resolve_mcp_command(args.mcp_command)
        except ValueError as exc:
            print(f"WARN: --mcp-command invalid: {exc}; skipping .mcp.json", file=sys.stderr)
            return 0
        mcp_command_for_summary = command
        mcp_resolves_for_summary = _mcp_command_resolves(command)
        if not mcp_resolves_for_summary:
            print(
                f"WARNING: the resolved MCP server command '{command}' does not "
                f"resolve on PATH; the registered consensus-mcp server may not "
                f"start. Install consensus-mcp so it is on PATH, or pass "
                f"--mcp-command with a working command.",
                file=sys.stderr,
            )
        state_root = repo_root / "consensus-state"
        project_root = repo_root
        status, mcp_path = _write_mcp_json(
            repo_root,
            state_root,
            project_root,
            command,
            mcp_args,
            force=args.mcp_force,
        )
        if status == "wrote":
            print(f"wrote {mcp_path} (consensus-mcp registered)")
        elif status == "merged":
            print(f"merged consensus-mcp into existing {mcp_path}")
        elif status == "already-current":
            print(f"consensus-mcp entry in {mcp_path} already current")
        elif status == "blocked-conflict":
            print(
                f"BLOCKED: existing consensus-mcp entry in {mcp_path} differs "
                f"from desired config. Use --mcp-force to replace, or edit "
                f"manually.",
                file=sys.stderr,
            )
        elif status.startswith("parse-error:"):
            err_detail = status[len("parse-error:"):]
            print(
                f"WARN: {mcp_path} failed to parse as JSON ({err_detail}); "
                f"not modifying. Fix the file manually or delete it and re-run.",
                file=sys.stderr,
            )
        else:
            print(f"WARN: unknown mcp-json status {status!r}", file=sys.stderr)
    else:
        print(".mcp.json write skipped (--no-mcp-json)")

    # v1.21 (converged-plan E): write the per-project Claude Code subagents into
    # .claude/agents/. Non-destructive (skip-if-exists unless --force); opt-out
    # via --no-agents. Best-effort: a copy failure must not abort a successful
    # config write.
    #
    # v1.24 (fix 9): a divergent agent is SKIPPED (kept), but the per-project init
    # used to report plain success - masking a stale agent. Collect SKIP lines and
    # mark the init INCOMPLETE (rc 7) unless --force, mirroring the global
    # managed-file SKIP -> rc 5 behavior. (rc 7 is per-project-agent-specific so it
    # does not collide with the global path's 5/6.)
    agent_skipped: list[str] = []
    if not args.no_agents:
        try:
            for line in _install_project_agents(repo_root, force=args.force):
                print(line)
                if line.startswith("SKIP:"):
                    agent_skipped.append(line)
        except OSError as exc:
            print(f"WARN: subagent install skipped: {exc}", file=sys.stderr)
    else:
        print("subagent install skipped (--no-agents)")

    # recon #4: concise post-init next-steps / status summary. Only on the
    # fresh/--reconfigure per-project bootstrap path (we already short-circuited
    # --check / --print-defaults / --install-claude-code above). Folds in the
    # MCP-command resolvability result (recon #5) and the --from-claude-code
    # restart guidance (formerly a standalone iter-0040 block).
    try:
        summary_profiles = _load_merged_profiles(
            (new_config.get("contributors") or {}).get("profiles")
        )
        summary_enabled = (new_config.get("contributors") or {}).get("enabled") or []
        _print_status_summary(
            config_path,
            summary_enabled,
            summary_profiles,
            mcp_command=mcp_command_for_summary,
            mcp_resolves=mcp_resolves_for_summary,
            from_claude_code=getattr(args, "from_claude_code", False),
        )
    except (OSError, ValueError) as exc:
        print(f"WARN: status summary skipped: {exc}", file=sys.stderr)

    # v1.24 (fix 9): surface any divergent (SKIPPED) subagent loudly and mark the
    # per-project init INCOMPLETE (rc 7) unless --force. The config + .mcp.json
    # were still written (so the bootstrap is usable), but a stale agent is a
    # detectable, non-silent partial - mirrors the global managed-file SKIP path.
    if agent_skipped and not args.force:
        print(
            f"\nWARNING: {len(agent_skipped)} subagent file(s) were SKIPPED because "
            f"the on-disk copy diverges from this version - they are now STALE. "
            f"Re-run with --force to update them (this overwrites local edits):",
            file=sys.stderr,
        )
        for ln in agent_skipped:
            print(f"  - {ln[len('SKIP: '):]}", file=sys.stderr)
        return 7

    return 0


def _force_utf8_streams() -> None:
    """Make stdout/stderr tolerate non-ASCII glyphs regardless of the console
    code page. The wizard prints status glyphs ('[ok]'/'[x]') and '->' arrows; on a
    Windows console defaulting to cp1252 the first such print() raises
    UnicodeEncodeError and aborts the whole run (the v1.33.4 Windows report).
    Reconfiguring to UTF-8 with errors='replace' removes the dependency on the
    ambient code page / PYTHONUTF8. Best-effort: streams that are None, lack
    reconfigure() (e.g. a captured StringIO under pytest), or whose buffer is
    detached are left untouched.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            pass


def main(argv: list[str] | None = None) -> int:
    # Windows-portability (v1.33.4): make output encoding-safe before any print()
    # so a cp1252 console can't crash the wizard on the first '[ok]' status line.
    _force_utf8_streams()
    # iter-0040 (per iter-0039 converged plan Q6 bonus): the package also ships
    # a `consensus` console-script alias. When invoked as `consensus init ...`,
    # argv[0] is the literal subcommand "init". Strip it so argparse sees the
    # same arguments as `consensus-init ...`. Any other leading token is left
    # in place (treated as an unrecognized argument by argparse, which
    # produces the expected usage error).
    if argv is None:
        raw = sys.argv[1:]
    else:
        raw = list(argv)
    if raw and raw[0] == "init":
        raw = raw[1:]
    argv = raw

    parser = argparse.ArgumentParser(
        prog="consensus init",
        description="Initialize .consensus/config.yaml for cross-AI workflow",
    )
    parser.add_argument("--check", action="store_true", help="validate existing config and exit")
    parser.add_argument("--verify", action="store_true",
                        help="run an offline verification pass (console scripts on PATH + "
                             "selected reviewer CLIs installed/authed) and exit 0/2")
    parser.add_argument("--detect-contributors", action="store_true",
                        help="print JSON of independent contributors + install status "
                             "(used by the Claude Code auto-init panel question); read-only")
    parser.add_argument("--print-defaults", action="store_true", help="print default YAML to stdout")
    parser.add_argument("--dry-run", action="store_true", help="show intended config; do not write")
    parser.add_argument("--non-interactive", action="store_true", help="no prompts")
    parser.add_argument("--accept-defaults", action="store_true", help="non-interactive with defaults")
    parser.add_argument("--reconfigure", action="store_true", help="re-prompt with existing as defaults; show diff")
    parser.add_argument("--force", action="store_true", help="overwrite without prompt")
    parser.add_argument("--here", action="store_true",
                        help="initialize the current directory even if it looks "
                             "like a workspace folder containing other git projects")
    parser.add_argument("--repair", action="store_true",
                        help="verify the install and non-destructively repair "
                             "missing pieces (report diverged); does not re-prompt")
    parser.add_argument("--no-update-gitignore", action="store_true", help="skip .gitignore marker write")
    parser.add_argument("--no-instructions", action="store_true",
                        help=("skip seeding per-AI instruction files "
                              "(CLAUDE.md/AGENTS.md/GEMINI.md managed block)"))
    parser.add_argument("--config", default=None, help="override config path")

    # iter-0031: .mcp.json bootstrap flags (per iter-0030 converged plan).
    parser.add_argument("--no-mcp-json", action="store_true",
                        help="skip writing .mcp.json (default: auto-write)")
    # v1.21 (converged-plan E): per-project Claude Code subagent install.
    parser.add_argument("--no-agents", action="store_true",
                        help=("skip writing the consensus-orchestrator + "
                              "consensus-host-peer-reviewer subagent files into "
                              ".claude/agents/ (default: auto-write; "
                              "non-destructive skip-if-exists unless --force)"))
    parser.add_argument("--mcp-command", default=None,
                        help=("override the command written into .mcp.json. "
                              "Whitespace-split; first token = command, rest = args. "
                              "Default: discover via PATH (consensus-mcp), else "
                              "fall back to sys.executable -m consensus_mcp.server."))
    parser.add_argument("--mcp-force", action="store_true",
                        help=("replace existing consensus-mcp entry in .mcp.json "
                              "even when it differs from the desired config. "
                              "Does NOT overwrite other servers in mcpServers."))

    # iter-0040: Claude Code bootstrap pack flags (per iter-0039 converged plan).
    parser.add_argument("--install-claude-code", action="store_true",
                        help=("copy shipped skill + slash-command into "
                              "$CLAUDE_HOME or ~/.claude so Claude Code can "
                              "trigger consensus-init from chat. Idempotent; "
                              "use --force to overwrite user-edited content."))
    parser.add_argument("--uninstall-claude-code", action="store_true",
                        help=("remove the consensus-tagged hook entries from "
                              "$CLAUDE_HOME/settings.json (deactivates "
                              "enforcement). Preserves all unrelated user "
                              "hooks/settings. Does not remove skill/command "
                              "files."))
    parser.add_argument("--from-claude-code", action="store_true",
                        help=("the caller is a Claude Code skill / slash "
                              "command; print contextual restart guidance "
                              "after init."))

    parser.add_argument("--workflow", default=None,
                        choices=[
                            # Letter aliases (canonical operator vocabulary as of v1.14.4)
                            "A", "B", "C", "D", "a", "b", "c", "d",
                            # Numeric aliases (deprecated; emit DeprecationWarning)
                            "3", "4",
                            # Semantic strings (canonical internal values)
                            "post-review", "propose-converge", "advisory",
                            "autonomous-execute", "architect-build",
                        ],
                        help=("workflow mode. A=propose-converge (default; "
                              "all contributors propose blindly then converge), "
                              "B=post-review (lightweight; one AI implements, "
                              "others review), C=autonomous-execute (overnight; "
                              "v1.14.4 contract, v1.15.0 engine), "
                              "D=architect-build (Consensus Build, preview; "
                              "requires a roles: block - architect/builder/"
                              "reviewer). Numeric aliases 3/4 deprecated."))
    parser.add_argument("--contributors", default=None,
                        help="comma-separated list of enabled contributors")
    parser.add_argument("--independence", default=None,
                        choices=list(cfg.VALID_INDEPENDENCE),
                        help="independence model")
    parser.add_argument("--convergence", default=None,
                        choices=list(cfg.VALID_CONVERGENCE),
                        help="convergence rule")
    parser.add_argument("--finding-disposition", default=None,
                        choices=list(cfg.VALID_DISPOSITION),
                        help="finding disposition granularity")
    parser.add_argument("--snapshot-trigger", default=None,
                        choices=list(cfg.VALID_SNAPSHOT_TRIGGER),
                        help="snapshot trigger policy")
    parser.add_argument("--snapshot-every-iterations", type=int, default=None,
                        help="periodic snapshot iteration cadence")
    parser.add_argument("--patch-authoring", default=None,
                        choices=list(cfg.VALID_PATCH_AUTHORING),
                        help="patch authoring permission")
    parser.add_argument("--timeout-policy", default=None,
                        choices=list(cfg.VALID_TIMEOUT_POLICY),
                        help="contributor timeout policy")
    parser.add_argument(
        "--governance-mode",
        default=None,
        choices=sorted(cfg.VALID_GOVERNANCE_MODES),
        help=("per-project governance: on-demand (default; explicit user calls "
              "only, no edit/delivery gates) or continuous (proactive guidance "
              "and enforced approval/delivery lifecycle)"),
    )

    args = parser.parse_args(argv)
    return cmd_init(args)


if __name__ == "__main__":
    sys.exit(main())
