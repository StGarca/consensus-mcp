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
import shutil
import sys
from pathlib import Path

import yaml

from consensus_mcp import config as cfg


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
    """Walk upward looking for `.git`. Falls back to start (CWD) if no marker found."""
    cwd = (start or Path.cwd()).resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


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
        return 0

    write_config(new_config, config_path)
    print(f"wrote {config_path}")

    if not args.no_update_gitignore:
        changed = update_gitignore(repo_root)
        if changed:
            print(f"updated .gitignore at {repo_root / '.gitignore'}")
        else:
            print(".gitignore already up to date")
    return 0


def main(argv: list[str] | None = None) -> int:
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
