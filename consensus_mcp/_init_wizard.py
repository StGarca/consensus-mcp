"""`consensus init` wizard — fourth and final sub-component of iter-0016.

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

`.gitignore` updates use bracketed markers per converged design F9c. Malformed
markers (open without close) are detected and preserved untouched rather than
risking data loss.
"""
from __future__ import annotations

import argparse
import difflib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from consensus_mcp import config as cfg
from consensus_mcp import _contributor_profiles as profiles_mod


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

    iter-0031 (per iter-0030 converged plan Q3): use marker-based detection
    instead of .git-only walking. Precedence:
      1. `git rev-parse --show-toplevel` if git is available and start is
         inside a git working tree.
      2. Walk up from start (or cwd) looking for any strong marker
         (.git, pyproject.toml, package.json, CLAUDE.md, .mcp.json,
         consensus-state).
      3. Fall back to start / cwd.
    """
    cwd = (start or Path.cwd()).resolve()

    # 1. git rev-parse — most authoritative when git is on PATH.
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
        # git not available / not in a worktree / timeout — fall through.
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
        return Path(override)
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
    ("skills/consensus-test-driven-development/SKILL.md", "skills/consensus-test-driven-development/SKILL.md"),
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


def _install_claude_extensions(claude_home: Path, force: bool) -> list[str]:
    """Copy the shipped skill + command files into the user's CLAUDE_HOME.

    Idempotency contract (per iter-0039 converged plan Q2):
      - If the destination file is byte-identical to the source, no-op.
      - If the destination file diverges from the source AND force=False,
        skip with a warning printed to stderr (user-edited content preserved).
      - If force=True, overwrite divergent destinations.
      - Missing destination → write fresh.

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

        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src_text, encoding="utf-8")
        statuses.append(f"wrote: {dst}")

    return statuses


# --- v1.21 (converged-plan B5): settings.json hook ACTIVATION merge ---
#
# Background (the bug this fixes): the installer copied a `hooks.json` manifest
# into ~/.claude, but Claude Code does NOT read a bare hooks.json there — hooks
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
    under (which has consensus_mcp importable); quotes the path for spaces.
    """
    return f'"{sys.executable}" "{script_path}"'


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
    ({}, "<msg>") — the caller fails soft and does not clobber the file.
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
            # Foreign non-list value under this event — preserve it by nesting
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

    _atomic_write_json(path, merged)
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

    _atomic_write_json(path, new_settings)
    return [f"removed consensus hooks from {path}"]


# --- iter-0031: .mcp.json bootstrap helpers per iter-0030 converged plan ---


def _resolve_mcp_command(
    explicit: str | None = None,
) -> tuple[str, list[str], bool]:
    """Resolve the consensus-mcp command to write into `.mcp.json`.

    Precedence (per iter-0030 converged plan Q4):
      1. Explicit `--mcp-command` override (string; split on whitespace).
      2. `shutil.which("consensus-mcp")` → use bare name "consensus-mcp"
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


def _atomic_write_json(path: Path, data: dict) -> None:
    """Pretty-print JSON, write atomically via tmp+rename."""
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


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

      - "wrote"             — fresh file created
      - "merged"            — added consensus-mcp into existing config,
                              other servers preserved
      - "already-current"   — entry exists and matches; no write
      - "blocked-conflict"  — entry exists but differs; not overwritten
                              (use force=True to replace just this entry)
      - "parse-error:<msg>" — existing file failed to parse; not modified

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
    if existing_entry == desired:
        return ("already-current", path)

    if existing_entry is not None and existing_entry != desired and not force:
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
    profile set (no hardcoded AI list — decision 7). host is always available;
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
# consensus-mcp and refreshed in place — everything outside is the user's.
INSTRUCTION_BEGIN_MARKER = "<!-- consensus-mcp:begin (managed — do not edit inside) -->"
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

    host (claude) is the running environment — always available. host_peer
    (v1.20.0: a same-family blind SWE-reviewer run via the host callback) is
    likewise always usable on the host — it has no CLI to detect. cli_reviewers
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
    sorted). host_peer is excluded — it is offered via the conditional follow-up."""
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

    host_peer profiles are excluded from this list — they are offered via a
    conditional follow-up prompt (separate task). Shows '✓ installed' /
    '✗ missing' per entry, pre-checks installed ones as the default (accepted
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
        status = "✓ installed" if installed[name] else "✗ missing"
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
            f"Panel: {total} reviewers — {len(indep)} independent "
            f"({', '.join(indep)}) + 0.5 supplemental same-model ({', '.join(peers)})."
        )
    else:
        print(f"Panel: {len(indep)} independent reviewers ({', '.join(indep)}).")


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
        "This is a SUPPLEMENTAL review (shown as +0.5 in the init summary only — NOT a\n"
        "fully independent reviewer; it shares the host model's blind spots). It gets no\n"
        "vote at the consensus gate and can't close consensus (claude already votes as\n"
        "host) — but every good idea it raises is still applied on merit. A useful extra\n"
        "pass if you have the tokens to spare."
    )
    if len(candidates) == 1:
        default = "y" if default_yes else "n"
        ans = (input(f"Add it? [{'Y/n' if default_yes else 'y/N'}]: ").strip().lower() or default)
        return candidates[0] if ans.startswith("y") else None

    print("Multiple same-model reviewers available (choose one or none):")
    for i, hp in enumerate(candidates, start=1):
        print(f"  {i}. {hp}")
    raw = input("Number to add [default: none]: ").strip()
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

        print(f"{name}: not found on PATH — to enable it:")
        install = profile.get("install") or {}
        cmd = install.get(os_key)
        if cmd is None and os_key == "darwin":
            cmd = install.get("linux")
        if cmd:
            print(f"  install: {cmd}")
        else:
            print(f"  install: unavailable for {os_key} — see {name} vendor docs")

        auth = profile.get("auth") or {}
        if auth.get("command"):
            print(f"  auth:    {auth['command']}")
        for var in auth.get("env_vars") or []:
            print(f"  auth env (optional): {var}")
        if auth.get("note"):
            print(f"  note:    {auth['note']}")
        print(_DETECT_GUIDE_REMINDER)


def _vendored_instructions_text() -> str:
    """Return the vendored Karpathy guidelines (contributor_instructions/base.md)."""
    base = Path(__file__).resolve().parent / "contributor_instructions" / "base.md"
    return base.read_text(encoding="utf-8")


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
) -> list[Path]:
    """Seed/refresh per-AI instruction files for the selected contributors.

    Targets profile.instructions.filename, deduped by filename (codex+kimi both
    → AGENTS.md → written once). Non-destructive: a managed block is inserted or
    refreshed between sentinels, leaving any user content intact. Idempotent.

    Returns the list of written file paths (deduped).
    """
    block_body = _vendored_instructions_text()
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
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        new_text = _upsert_managed_block(existing, block_body)
        if new_text != existing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_text, encoding="utf-8")
        written.append(path)
    return written


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


def _which_flags_set(args) -> set[str]:
    """Return the names of dimension flags the user passed explicitly."""
    keys = (
        "contributors", "workflow", "convergence", "independence",
        "finding_disposition", "snapshot_trigger", "snapshot_every_iterations",
        "patch_authoring", "timeout_policy",
    )
    return {k for k in keys if getattr(args, k, None) is not None}


def interactive_overrides(args, repo_root: Path, base: dict, fresh: bool) -> None:
    """Prompt the user for each configurability dimension. Mutates `base` in place.

    Flags already supplied on the command line take precedence (no prompt).
    When `fresh` is True (no existing config / not reconfigure), missing
    contributors are auto-suggested from PATH. When False, current base values
    are the prompt defaults (preserving existing config).

    Raises KeyboardInterrupt on Ctrl+C / EOF — caller maps to exit code 1.
    """
    set_flags = _which_flags_set(args)

    # Contributors.
    if "contributors" not in set_flags:
        if fresh:
            # FRESH path uses the v1.18.0 numbered multi-select over merged
            # profiles (✓ installed / ✗ missing, min-2 re-prompt, claude
            # optional) — wired here per codex-rev-001 (the helper existed but
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
            "Workflow mode (A=propose-converge, B=post-review, C=autonomous-execute, advisory)",
            default_workflow,
            valid=[
                cfg.WORKFLOW_POST_REVIEW,
                cfg.WORKFLOW_PROPOSE_CONVERGE,
                cfg.WORKFLOW_ADVISORY,
                cfg.WORKFLOW_AUTONOMOUS_EXECUTE,
            ],
        )
        # Resolve letter alias (A/B/C) to semantic string before storing.
        base["workflow"]["mode"] = cfg.WORKFLOW_ALIASES.get(choice, choice)

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
    """Atomically write config YAML to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(config, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    tmp.replace(path)


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
    — a fresh block is appended but no existing lines are deleted
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
    gi.write_text(new_content, encoding="utf-8")
    return True


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

    if args.print_defaults:
        print(yaml.safe_dump(cfg.default_config(), sort_keys=False, default_flow_style=False))
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
        claude_home = _resolve_claude_home()
        for line in _install_claude_extensions(claude_home, force=args.force):
            print(line)
        # v1.21 (B5): copying the skill/command files does NOT activate the
        # enforcement hooks — Claude Code reads hooks only from settings.json.
        # Merge them in (idempotent, preserves unrelated user hooks).
        for line in _install_claude_settings_json(claude_home, force=args.force):
            print(line)
        return 0

    # gemini pass-3 rev-001: --dry-run no longer forces non-interactive. Users
    # can preview an interactive session via `consensus init --dry-run`.
    # Interactivity is suppressed only by --non-interactive or --accept-defaults.
    # codex pass-4 rev-002: check existing-config guard BEFORE we build / prompt
    # so exit 4 wins over exit 1 (user abort during interactive) and exit 3
    # (invalid construction).
    if config_path.exists() and not (args.reconfigure or args.force):
        print(
            f"error: {config_path} already exists. Use --reconfigure to update "
            f"or --force to overwrite.",
            file=sys.stderr,
        )
        return 4

    interactive = not (args.non_interactive or args.accept_defaults)

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
        if not args.no_instructions:
            for path in _provision_instruction_files(enabled, merged_profiles, repo_root):
                print(f"seeded instruction file {path}")
        else:
            print("instruction-file seeding skipped (--no-instructions)")
    except (OSError, ValueError) as exc:
        print(f"WARN: instruction/detect-guide step skipped: {exc}", file=sys.stderr)

    # iter-0031: write .mcp.json by default (per iter-0030 converged plan).
    if not args.no_mcp_json:
        try:
            command, mcp_args, is_portable = _resolve_mcp_command(args.mcp_command)
        except ValueError as exc:
            print(f"WARN: --mcp-command invalid: {exc}; skipping .mcp.json", file=sys.stderr)
            return 0
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

    # iter-0040: when invoked from inside a Claude Code session, print
    # contextual restart guidance so the user knows what to do next.
    if getattr(args, "from_claude_code", False):
        print()
        print(
            "Detected --from-claude-code: Claude Code must reload to "
            "activate the consensus-mcp server. Either restart Claude "
            "Code in this project (Ctrl-C, then `claude code`), or run "
            "`/mcp` to reload MCP servers if your build supports it."
        )

    return 0


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--print-defaults", action="store_true", help="print default YAML to stdout")
    parser.add_argument("--dry-run", action="store_true", help="show intended config; do not write")
    parser.add_argument("--non-interactive", action="store_true", help="no prompts")
    parser.add_argument("--accept-defaults", action="store_true", help="non-interactive with defaults")
    parser.add_argument("--reconfigure", action="store_true", help="re-prompt with existing as defaults; show diff")
    parser.add_argument("--force", action="store_true", help="overwrite without prompt")
    parser.add_argument("--no-update-gitignore", action="store_true", help="skip .gitignore marker write")
    parser.add_argument("--no-instructions", action="store_true",
                        help=("skip seeding per-AI instruction files "
                              "(CLAUDE.md/AGENTS.md/GEMINI.md managed block)"))
    parser.add_argument("--config", default=None, help="override config path")

    # iter-0031: .mcp.json bootstrap flags (per iter-0030 converged plan).
    parser.add_argument("--no-mcp-json", action="store_true",
                        help="skip writing .mcp.json (default: auto-write)")
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
                            "A", "B", "C", "a", "b", "c",
                            # Numeric aliases (deprecated; emit DeprecationWarning)
                            "3", "4",
                            # Semantic strings (canonical internal values)
                            "post-review", "propose-converge", "advisory", "autonomous-execute",
                        ],
                        help=("workflow mode. A=propose-converge (default; "
                              "all contributors propose blindly then converge), "
                              "B=post-review (lightweight; one AI implements, "
                              "others review), C=autonomous-execute (overnight; "
                              "v1.14.4 contract, v1.15.0 engine). Numeric "
                              "aliases 3/4 deprecated."))
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

    args = parser.parse_args(argv)
    return cmd_init(args)


if __name__ == "__main__":
    sys.exit(main())
