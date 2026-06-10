"""Builder dispatch for architect-build (workflow D): codex, workspace-write,
confined to the lane worktree. The argv MUST pass the separate write-enabled
canon (validators/validate_builder_dispatch) before Popen - fail-closed.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from consensus_mcp.validators.validate_builder_dispatch import (
    validate_builder_argv,
)

_SCRUB_KEYS = ("OPENAI_API_KEY",)
_TEMPLATE_DIR = Path(__file__).parent / "dispatch_templates"
BUILDER_TEMPLATE = _TEMPLATE_DIR / "builder_build_template.md"
BUILDER_SCHEMA = _TEMPLATE_DIR / "builder_build_schema.json"


class BuilderDispatchError(RuntimeError):
    """Raised on canon violation, CLI failure, or malformed builder output."""


def build_prompt(spec_body: str, feedback_block: str) -> str:
    template = BUILDER_TEMPLATE.read_text(encoding="utf-8")
    return template.replace("{spec_body}", spec_body).replace(
        "{feedback_block}", feedback_block or "(none)"
    )


def _subprocess_env() -> dict:
    env = dict(os.environ)
    for key in _SCRUB_KEYS:
        env.pop(key, None)
    return env


def dispatch_builder(
    *,
    repo_root: Path,
    lane: Path,
    prompt: str,
    codex_bin: str = "codex",
    timeout_seconds: int = 1800,
) -> dict:
    """Run the builder CLI write-enabled in the lane; return the parsed
    {summary, pushback, notes} dict. Raises BuilderDispatchError on any
    canon violation, CLI failure, timeout, or output-shape violation."""
    out_file = Path(tempfile.mkstemp(prefix="builder-out-", suffix=".json")[1])
    argv = [
        codex_bin, "exec", "--skip-git-repo-check",
        "--cd", str(lane),
        "--sandbox", "workspace-write",
        "--output-schema", str(BUILDER_SCHEMA),
        "-o", str(out_file), "-",
    ]
    violations = validate_builder_argv(argv, Path(repo_root))
    if violations:
        raise BuilderDispatchError(
            f"builder argv violates the write-enabled canon: {violations}"
        )
    try:
        proc = subprocess.run(
            argv, input=prompt.encode("utf-8"), capture_output=True,
            timeout=timeout_seconds, env=_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuilderDispatchError(f"builder CLI failed: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace") if isinstance(
            proc.stderr, bytes
        ) else (proc.stderr or "")
        raise BuilderDispatchError(
            f"builder CLI exited {proc.returncode}: {stderr.strip()[:500]}"
        )
    try:
        data = json.loads(out_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BuilderDispatchError(f"builder output unreadable: {exc}") from exc
    finally:
        try:
            out_file.unlink()
        except OSError:
            pass
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        raise BuilderDispatchError(
            f"builder output must be a dict with string 'summary'; got "
            f"{type(data).__name__} keys={sorted(data) if isinstance(data, dict) else None}"
        )
    pushback = data.get("pushback")
    if pushback is not None and not isinstance(pushback, str):
        raise BuilderDispatchError("builder 'pushback' must be string or null")
    return {
        "summary": data["summary"],
        "pushback": pushback,
        "notes": data.get("notes", "") if isinstance(data.get("notes"), str) else "",
    }
