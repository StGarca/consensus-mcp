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


class WizardError(RuntimeError):
    pass


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
    """Return contributors whose CLIs are resolvable on PATH. Always includes claude."""
    available = ["claude"]
    if shutil.which("codex"):
        available.append("codex")
    if shutil.which("gemini"):
        available.append("gemini")
    return available


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
        default = ",".join(
            base["contributors"]["enabled"] if not fresh else _detect_available_contributors(repo_root)
        )
        choice = _prompt("Enabled contributors (comma-separated)", default)
        base["contributors"]["enabled"] = [c.strip() for c in choice.split(",") if c.strip()]
    n_contributors = len(base["contributors"]["enabled"])

    # Workflow mode.
    if "workflow" not in set_flags:
        if fresh:
            default_workflow = (
                cfg.WORKFLOW_PROPOSE_CONVERGE if n_contributors >= 2 else cfg.WORKFLOW_POST_REVIEW
            )
        else:
            default_workflow = base["workflow"]["mode"]
        choice = _prompt(
            "Workflow mode", default_workflow,
            valid=[cfg.WORKFLOW_POST_REVIEW, cfg.WORKFLOW_PROPOSE_CONVERGE, cfg.WORKFLOW_ADVISORY],
        )
        base["workflow"]["mode"] = choice

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

    # Finding disposition.
    if "finding_disposition" not in set_flags:
        default_disp = base["convergence"]["finding_disposition"] if not fresh else cfg.DISPOSITION_ALL_OR_NOTHING
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
        return 0

    write_config(new_config, config_path)
    print(f"wrote {config_path}")

    if not args.no_update_gitignore:
        changed = update_gitignore(repo_root)
        if changed:
            print(f"updated .gitignore at {repo_root / '.gitignore'}")
        else:
            print(".gitignore already up to date")

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

    # iter-0040: optional install of Claude Code skill + slash command pack.
    if getattr(args, "install_claude_code", False):
        claude_home = _resolve_claude_home()
        for line in _install_claude_extensions(claude_home, force=args.force):
            print(line)

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
    parser.add_argument("--from-claude-code", action="store_true",
                        help=("the caller is a Claude Code skill / slash "
                              "command; print contextual restart guidance "
                              "after init."))

    parser.add_argument("--workflow", default=None,
                        choices=["3", "4", "post-review", "propose-converge", "advisory"],
                        help="workflow mode")
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
