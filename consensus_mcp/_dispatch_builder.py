"""Builder dispatch for architect-build (workflow D): codex, workspace-write,
confined to the lane worktree. The argv MUST pass the separate write-enabled
canon (validators/validate_builder_dispatch) before Popen - fail-closed.

v1 simplification (documented in the plan): no streaming watchdog (that is a
named follow-up); the builder runs under Popen + communicate with a hard
timeout. It DOES reuse the codex dispatcher's containment primitives:

- argv[0] resolves through _dispatch_codex._resolve_codex_bin so npm-installed
  codex (codex.cmd / .ps1) works on Windows ('init platform consistency';
  subprocess on Windows does not apply PATHEXT to bare names).
- The process spawns in its own group (CREATE_NEW_PROCESS_GROUP /
  start_new_session) and timeout/failure paths call _terminate_process_tree.
  codex spawns Node descendants that orphan otherwise; for a workspace-write
  builder an orphaned descendant is not litter but a containment/TOCTOU
  hazard - it could keep WRITING in the lane after the supervisor times out
  and moves on to the integrity snapshot and supervisor-owned git commit.
- Env scrubbing uses the shared scrub_env_keys + CODEX_SCRUBBED_ENV_KEYS from
  _dispatch_base (the same primitive all four read-only dispatchers route
  through) so the scrub list cannot drift.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from consensus_mcp._dispatch_base import (
    CODEX_SCRUBBED_ENV_KEYS,
    _terminate_process_tree,
    scrub_env_keys,
)
from consensus_mcp._dispatch_codex import (
    CodexInvocationError,
    _resolve_codex_bin,
)
from consensus_mcp.validators.validate_builder_dispatch import (
    validate_builder_argv,
)

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
    try:
        resolved_bin = _resolve_codex_bin(codex_bin)
    except CodexInvocationError as exc:
        raise BuilderDispatchError(
            f"builder binary resolution failed: {exc}"
        ) from exc
    # The out-file path is generated (not yet created) so the argv can be
    # canon-validated BEFORE anything touches the filesystem: a
    # canon-violating dispatch must abort leaving zero litter.
    out_file = Path(tempfile.gettempdir()) / (
        "builder-out-" + os.urandom(16).hex() + ".json"
    )
    argv = [
        resolved_bin, "exec", "--skip-git-repo-check",
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
    # Pre-create the out-file and close the handle immediately (neighbor
    # pattern: _dispatch_codex's NamedTemporaryFile(delete=False) close-
    # before-use). No fd leaks per dispatch, and no open handle to make the
    # finally-unlink raise PermissionError on Windows. O_EXCL refuses a
    # pre-existing path (including a planted symlink) at the random name.
    try:
        fd = os.open(out_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except OSError as exc:
        raise BuilderDispatchError(
            f"builder out-file creation failed: {exc}"
        ) from exc
    # From here the ENTIRE dispatch is inside try/finally unlink: spawn
    # failure, timeout, nonzero exit, and output-parse failure all clean up
    # the temp file (failures are the expected case on those paths).
    try:
        # Spawn in a new process group so the whole tree can be signalled.
        if sys.platform == "win32":
            popen_kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            popen_kwargs = {"start_new_session": True}
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=scrub_env_keys(os.environ.copy(), CODEX_SCRUBBED_ENV_KEYS),
                **popen_kwargs,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BuilderDispatchError(f"builder CLI failed: {exc}") from exc
        try:
            _stdout_raw, stderr_raw = proc.communicate(
                input=prompt.encode("utf-8"), timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run(timeout=...) would kill only the direct child
            # (on Windows only the .cmd shell); Node descendants would keep
            # writing in the lane. Kill the whole process group.
            _terminate_process_tree(proc)
            raise BuilderDispatchError(
                f"builder CLI timed out after {timeout_seconds}s; "
                f"process tree terminated"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            _terminate_process_tree(proc)
            raise BuilderDispatchError(f"builder CLI failed: {exc}") from exc
        if proc.returncode != 0:
            stderr = stderr_raw.decode("utf-8", "replace") if isinstance(
                stderr_raw, bytes
            ) else (stderr_raw or "")
            raise BuilderDispatchError(
                f"builder CLI exited {proc.returncode}: {stderr.strip()[:500]}"
            )
        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise BuilderDispatchError(
                f"builder output unreadable: {exc}"
            ) from exc
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
