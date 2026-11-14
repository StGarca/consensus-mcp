"""Shared process lifecycle, failure policy and dispatch/sealing pipeline."""
from __future__ import annotations

import argparse
from contextvars import ContextVar
import hashlib
import shutil
import stat as stat_module
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable

import yaml

from consensus_mcp import _dispatch_base as base
from consensus_mcp.contributors.base import DispatchError

_ACTIVE_PROFILE = ContextVar("dispatch_profile", default={})


def classify_failure(exc, quota_signatures=()) -> str:
    """Classify preserved provider evidence; explicit classes take precedence."""
    parts, seen, explicit_class = [], set(), None
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ValueError):
            return "parse"
        explicit = getattr(exc, "failure_class", None)
        if explicit and explicit not in {"crash", "rate_limit", "quota"}:
            return explicit
        explicit_class = explicit_class or explicit
        parts.append(f"{type(exc).__name__}: {exc}")
        exc = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    text = " ".join(parts).lower()
    if any(str(marker).lower() in text for marker in quota_signatures):
        return "quota"
    if explicit_class:
        return explicit_class
    for kind, markers in (
        ("integrity", ("integrity", "repo mutated", "snapshotindexerror")),
        ("timeout", ("timeout", "timed out", "timedout", "stall", "stuck: no output")),
        ("quota", ("quota", "usage limit", "usage_limit", "you've reached", "credit", "billing", "429 limit")),
        ("rate_limit", ("rate limit", "rate_limit", "ratelimit", "429", "too many requests")),
        ("auth", ("auth", "401", "403", "api key", "api_key", "credential", "permission denied")),
        ("parse", ("parse", "json", "yaml", "schema", "missing required", "empty output", "empty content")),
    ):
        if any(marker in text for marker in markers):
            return kind
    return "crash"


def subprocess_env(env=None, api_key_envs=()) -> dict:
    result = dict(os.environ if env is None else env)
    active = _ACTIVE_PROFILE.get()
    result.update(active.get("env", {}))
    from consensus_mcp._contributor_profiles import load_builtin_profiles
    api_keys = {str(key).upper() for key in api_key_envs if key}
    api_keys.update(str(p.get("api_key_env", "")).upper() for p in load_builtin_profiles().values())
    api_keys.update(str(key).upper() for key in active.get("_api_key_envs", ()))
    for key in list(result):
        upper = key.upper()
        if upper.endswith("API_KEY") or upper in api_keys or upper in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "ANTHROPIC_AUTH_TOKEN"}:
            result.pop(key, None)
    result["PYTHONUTF8"] = "1"
    result["PYTHONIOENCODING"] = "utf-8"
    return result


def run_process(argv, *, prompt=None, cwd=None, env=None, timeout_seconds=600,
                stall_silence_seconds=180, log_path=None, anchors=None,
                heartbeat_interval=30, poll_interval=.05, time_fn=None,
                popen_factory=None, _sleep=None, _drain_stderr=True,
                abort_root=None, startup_silence_seconds=None):
    """Run one CLI with binary UTF-8 pipes, bounded lifetime and tree cleanup."""
    clock, sleep = time_fn or time.time, _sleep or time.sleep
    factory = popen_factory or subprocess.Popen
    anchors = anchors or {}
    def log(event, **fields):
        if log_path is not None:
            base._log_dispatch(log_path, {"event": event, **anchors, **fields})
    kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
              if sys.platform == "win32" else {"start_new_session": True})
    try:
        proc = factory(list(argv), stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                       cwd=str(cwd) if cwd is not None else None,
                       env=subprocess_env(env), **kwargs)
    except OSError as exc:
        raise DispatchError(f"binary not found or could not start: {argv[0]}: {exc}", "crash") from exc
    buffers = {"stdout": [], "stderr": []}
    state = {"last": None, "seq": 0}
    lock = threading.Lock()
    errors = []
    def reader(stream):
        try:
            pipe = getattr(proc, stream)
            read = (lambda: pipe.read(4096)) if hasattr(pipe, "read") else pipe.readline
            for line in iter(read, b""):
                if not line:
                    break
                if isinstance(line, str):
                    line = line.encode("utf-8")
                with lock:
                    buffers[stream].append(line)
                    state["last"] = clock()
                    if stream == "stdout":
                        seq = state["seq"]
                        state["seq"] += 1
                if stream == "stdout":
                    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    log("dispatch_streamed_line", stream=stream, line_truncated=text[:200],
                        line_full_length=len(text), truncated=len(text) > 200, seq=seq)
        except Exception as exc:
            base.record_reader_error(buffers[stream], stream, exc)
            errors.append(exc)
    threads = [threading.Thread(target=reader, args=(stream,), daemon=True,
                                name=f"dispatch-{stream}-reader")
               for stream in ("stdout", "stderr") if stream != "stderr" or _drain_stderr]
    for thread in threads:
        thread.start()
    def writer():
        try:
            proc.stdin.write(prompt.encode("utf-8"))
        except (BrokenPipeError, OSError, AttributeError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, AttributeError):
                pass
    writer_thread = None
    if prompt is not None:
        writer_thread = threading.Thread(target=writer, daemon=True, name="dispatch-stdin-writer")
        writer_thread.start()
    start = last_heartbeat = clock()
    abort_root = abort_root or _ACTIVE_PROFILE.get().get("_abort_root")
    abort_path = ((Path(abort_root or cwd) / "consensus-state" / "state" /
                   f"abort-dispatch-{anchors['pass_id']}.signal")
                  if (abort_root or cwd) is not None and "pass_id" in anchors else None)
    try:
        while proc.poll() is None:
            now = clock()
            with lock:
                last, seq = state["last"], state["seq"]
            silence = now - (last if last is not None else start)
            threshold = (startup_silence_seconds if last is None and startup_silence_seconds is not None
                         else stall_silence_seconds)
            source = reason = None
            if abort_path is not None and abort_path.exists():
                try:
                    reason = abort_path.read_text(encoding="utf-8").strip() or "operator_signal_file"
                    abort_path.unlink()
                except OSError:
                    reason = "operator_signal_file (unreadable)"
                source = "operator_signal_file"
            elif threshold > 0 and silence >= threshold:
                source, reason = "watchdog_silence", f"stuck: no output for {silence:.0f}s"
            elif timeout_seconds > 0 and now - start >= timeout_seconds:
                source, reason = "wall_time_hard_ceiling", f"exceeded {timeout_seconds}s wall timeout"
            if source:
                log("dispatch_aborted", abort_source=source, abort_reason=reason,
                    age_seconds=now-start, last_streamed_line_age_seconds=silence if last is not None else None)
                raise DispatchError(reason, "timeout" if source != "operator_signal_file" else "crash")
            if now - last_heartbeat >= heartbeat_interval:
                log("dispatch_heartbeat", age_seconds=now-start,
                    last_streamed_line_age_seconds=silence if last is not None else None,
                    last_streamed_line_seq=seq-1 if seq else None)
                last_heartbeat = now
            sleep(poll_interval)
        for thread in threads:
            thread.join(timeout=5)
        if writer_thread is not None:
            writer_thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads) or errors:
            base._terminate_process_tree(proc)
            raise DispatchError("reader thread did not drain; possible partial output", "crash")
        stdout = b"".join(buffers["stdout"]).decode("utf-8", errors="replace")
        stderr = b"".join(buffers["stderr"]).decode("utf-8", errors="replace")
        if proc.returncode:
            error = DispatchError(f"exit={proc.returncode}; stderr_tail={stderr.strip()[-4000:]!r}; stdout_tail={stdout.strip()[-500:]!r}")
            error.stdout, error.stderr, error.returncode = stdout, stderr, proc.returncode
            raise error
        return stdout
    finally:
        if proc.poll() is None:
            base._terminate_process_tree(proc)
        for thread in threads:
            thread.join(timeout=1)


def run_model_chain(invoke, model, fallback_models=(), quota_signatures=(), sleep=None):
    from consensus_mcp import _dispatch_retry_policy as policy
    models = list(dict.fromkeys([model, *(fallback_models or ())]))
    requested, reason = model, None
    for index, current in enumerate(models):
        attempt = 1
        while True:
            try:
                value = invoke(current)
                return value, {"model_requested": requested, "model_used": current,
                               "fallback_reason": reason, "family": current}
            except Exception as exc:
                kind = classify_failure(exc, quota_signatures)
                decision = policy.decide(kind, attempt=attempt,
                                         fallback_model_available=index + 1 < len(models))
                if decision["action"] == policy.FALLBACK_MODEL:
                    reason = "quota"
                    break
                if decision["action"] == policy.RETRY:
                    (sleep or time.sleep)(.25)
                    attempt += 1
                    continue
                if isinstance(exc, DispatchError):
                    exc.failure_class = kind
                    raise
                raise DispatchError(str(exc), kind) from exc
    raise AssertionError("model chain exhausted without a result or failure")


def invoke_and_parse(invoke, parse, prompt, *, recovery=None, log_path=None,
                     anchors=None, retry_invocation=False):
    """One shared bounded repair attempt for schemaless providers."""
    first_error = None
    current, suffix = prompt, ""
    for attempt in range(2):
        try:
            raw = invoke(current, suffix)
            return raw, parse(raw)
        except Exception as exc:
            if attempt:
                if first_error is not None and type(first_error) is type(exc) and "empty" in type(exc).__name__.lower():
                    raise type(exc)(f"initial={first_error}; retry={exc}") from exc
                raise
            repair = recovery(exc) if recovery else None
            if repair is not None:
                current, suffix, event = repair
            elif isinstance(exc, ValueError) or classify_failure(exc) == "parse":
                current = (prompt + "\n\n# Retry - your previous response failed JSON validation\n\n"
                           + f"Parse error: {exc}\n\nRe-emit ONLY valid JSON conforming to the schema in the prompt above. "
                           + "No prose, no markdown fences, no commentary - JSON only, starting with `{` and ending with `}`.")
                suffix, event = "-retry", "dispatch_retry_for_parse_fail"
            elif retry_invocation and getattr(exc, "retryable", False):
                current, event = prompt, "dispatch_retry_for_retryable_invocation"
            else:
                raise
            first_error = exc
            if log_path is not None and anchors is not None:
                base._log_dispatch(log_path, {"event": event, **anchors,
                                             "first_parse_error": str(exc)[:1000]})


@dataclass
class ProviderSpec:
    name: str
    invoke: Callable
    parse: Callable
    default_model: str = ""
    default_bin: str = ""
    template_name: str = "codex"
    configure_parser: Callable | None = None
    disposable: bool = False
    profile: dict | None = None
    namespace: object | None = None
    prepare_template: Callable | None = None
    timeout_seconds: int = 600
    stall_silence_seconds: float = 180


_TEMP_WORKDIR_IGNORE_DIRS = ('.git', 'consensus-state', 'node_modules', '__pycache__', '.venv', 'venv', '.mypy_cache', '.pytest_cache', '.tox', 'dist', 'build', '.ruff_cache')

_WORKDIR_STALE_SECONDS = float(os.environ.get('CONSENSUS_MCP_KIMI_WORKDIR_STALE_SECONDS', '3600'))

def _sweep_stale_workdirs(max_age_seconds: float=_WORKDIR_STALE_SECONDS) -> int:
    import tempfile
    import time
    removed = 0
    tmp = Path(tempfile.gettempdir())
    now = time.time()
    try:
        candidates = list(tmp.glob('kimi-workdir-*'))
    except OSError:
        return 0
    for d in candidates:
        try:
            if now - d.stat().st_mtime >= max_age_seconds:
                shutil.rmtree(str(d), ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed

def _extra_ignore_dirs() -> set[str]:
    raw = os.environ.get('CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS', '')
    return {d.strip() for d in raw.split(',') if d.strip()}

def _gitignored_top_level_dirs(repo_root: Path) -> set[str]:
    out: set[str] = set()
    try:
        lines = (repo_root / '.gitignore').read_text(encoding='utf-8', errors='ignore').splitlines()
    except OSError:
        return out
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith('#') or s.startswith('!'):
            continue
        s = s.rstrip('/')
        if s and '/' not in s and ('*' not in s) and ('?' not in s):
            out.add(s)
    return out

def _rewrite_prompt_paths(prompt: str, repo_root: Path, workdir: Path, *, _case_insensitive: bool | None=None) -> str:
    if _case_insensitive is None:
        _case_insensitive = os.name == 'nt'
    rewritten = prompt
    seen: set[str] = set()
    sources: list[str] = []
    for src, dst in ((str(repo_root), str(workdir)), (str(repo_root).replace(os.sep, '/'), str(workdir).replace(os.sep, '/')), (repo_root.as_posix(), workdir.as_posix())):
        if src in seen:
            continue
        seen.add(src)
        sources.append(src)
        rewritten = rewritten.replace(src, dst)
    haystack = rewritten.lower() if _case_insensitive else rewritten
    for src in sources:
        needle = src.lower() if _case_insensitive else src
        if needle in haystack:
            raise DispatchError('prompt still contains the real repo absolute path after isolation rewrite; refusing dispatch', "integrity")
    return rewritten

def _strip_symlinks(root: Path) -> None:
    try:
        for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
            for name in list(dirnames) + list(filenames):
                fp = Path(dirpath) / name
                try:
                    if fp.is_symlink():
                        fp.unlink()
                except OSError:
                    pass
    except OSError:
        pass

class _WorkdirTooLargeToIsolate(RuntimeError):
    pass

def _make_disposable_workdir(repo_root: Path) -> Path:
    import tempfile
    _sweep_stale_workdirs()
    _root = os.environ.get('CONSENSUS_MCP_KIMI_WORKDIR_ROOT') or None
    if _root:
        Path(_root).mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix='kimi-workdir-', dir=_root))
    dest = tmp_root / 'repo'
    ignore_names = (set(_TEMP_WORKDIR_IGNORE_DIRS) | _extra_ignore_dirs()
                    | _gitignored_top_level_dirs(repo_root))
    try:
        # Copy the current working tree, including uncommitted changes/deletions.
        shutil.copytree(repo_root, dest, symlinks=True,
                        ignore=lambda directory, names: [n for n in names if n in ignore_names])
    except (OSError, shutil.Error) as exc:
        shutil.rmtree(tmp_root, ignore_errors=True)
        import errno
        if getattr(exc, "errno", None) == errno.ENOSPC or "No space left on device" in str(exc) or "[Errno 28]" in str(exc):
            raise _WorkdirTooLargeToIsolate(
                "disposable work-dir copy ran out of space; refusing to run in the real repo. "
                "Set CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS or CONSENSUS_MCP_KIMI_WORKDIR_ROOT."
            ) from exc
        raise
    _strip_symlinks(dest)
    return dest

def _cleanup_disposable_workdir(workdir: Path | None) -> None:
    if workdir is None:
        return
    try:
        target = workdir.parent if workdir.name == 'repo' else workdir
        shutil.rmtree(str(target), ignore_errors=True)
    except OSError:
        pass

class _SnapshotIndexError(RuntimeError):
    pass

_SNAPSHOT_MAX_FILES = int(os.environ.get('CONSENSUS_MCP_KIMI_SNAPSHOT_MAX_FILES', '50000'))

_SNAPSHOT_MAX_BYTES = int(os.environ.get('CONSENSUS_MCP_KIMI_SNAPSHOT_MAX_BYTES', str(2 * 1024 ** 3)))

def _filesystem_manifest_snapshot(repo_root: Path) -> dict[str, str]:
    ignore = set(_TEMP_WORKDIR_IGNORE_DIRS) | _extra_ignore_dirs() | _gitignored_top_level_dirs(repo_root)
    snap: dict[str, str] = {}
    nfiles = 0
    nbytes = 0
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for fn in filenames:
            full = Path(dirpath) / fn
            try:
                rel = str(full.relative_to(repo_root)).replace('\\', '/')
            except ValueError:
                continue
            try:
                st = full.lstat()
            except OSError:
                continue
            try:
                is_link = stat_module.S_ISLNK(st.st_mode)
                if not is_link and (not stat_module.S_ISREG(st.st_mode)):
                    continue
                nfiles += 1
                if not is_link:
                    nbytes += st.st_size
                if nfiles > _SNAPSHOT_MAX_FILES or nbytes > _SNAPSHOT_MAX_BYTES:
                    raise _SnapshotIndexError(f"integrity snapshot exceeded its budget walking {repo_root} (> {_SNAPSHOT_MAX_FILES} files or > {_SNAPSHOT_MAX_BYTES} bytes). Exclude heavy dirs via the repo's .gitignore or CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS so a mutation control can be established (or raise CONSENSUS_MCP_KIMI_SNAPSHOT_MAX_BYTES).")
                if is_link:
                    snap[rel] = 'link:' + hashlib.sha256(os.readlink(full).encode('utf-8', 'surrogateescape')).hexdigest()
                else:
                    snap[rel] = hashlib.sha256(full.read_bytes()).hexdigest()
            except OSError:
                snap[rel] = 'err:unreadable'
    return snap

def _repo_status_snapshot(repo_root: Path) -> dict[str, str]:
    git_bin = shutil.which('git')
    if git_bin is None or not (repo_root / '.git').exists():
        return _filesystem_manifest_snapshot(repo_root)
    try:
        result = subprocess.run([git_bin, 'status', '--porcelain'], cwd=str(repo_root), capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if result.returncode != 0:
        return {}
    snap: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        code, path = (line[:2], line[3:])
        if ' -> ' in path:
            path = path.split(' -> ', 1)[1]
        path = path.strip().strip('"')
        if not path:
            continue
        # consensus-state/ is the panel's own write area: sibling seats seal
        # reviews and take lock files there concurrently. Reviewers run in
        # disposable copies, so it is never a reviewer mutation (the
        # filesystem-manifest path already ignores it).
        if path.replace('\\', '/').startswith('consensus-state/'):
            continue
        full = repo_root / path
        try:
            if full.is_symlink():
                snap[path] = 'link:' + hashlib.sha256(os.readlink(full).encode('utf-8', 'surrogateescape')).hexdigest()
            elif full.is_file():
                snap[path] = hashlib.sha256(full.read_bytes()).hexdigest()
            else:
                snap[path] = 'code:' + code
        except OSError:
            snap[path] = 'code:' + code
    return snap

make_disposable_workdir = _make_disposable_workdir
cleanup_disposable_workdir = _cleanup_disposable_workdir
repo_status_snapshot = _repo_status_snapshot
rewrite_prompt_paths = _rewrite_prompt_paths


def main_for_provider(spec, argv=None):
    """Common CLI preflight, prompt, fallback, provenance, seal and audit path."""
    from consensus_mcp._contributor_profiles import load_builtin_profiles
    from consensus_mcp.config import resolve_reviewer_model
    ns_module = spec.namespace or base
    def helper(name):
        return getattr(ns_module, name, getattr(base, name, None))
    parser = argparse.ArgumentParser(prog=f"consensus-mcp-dispatch-{spec.name}")
    for flag in ("goal-packet", "iteration-dir"):
        parser.add_argument("--" + flag, required=True)
    for flag in ("reviewer-id", "pass-id", "prompt-template", "schema", "review-target", "model"):
        parser.add_argument("--" + flag)
    parser.add_argument("--profile", default=spec.name)
    parser.add_argument("--mode", choices=("review", "proposal"), default="review")
    parser.add_argument("--timeout-seconds", type=int, default=spec.timeout_seconds)
    default_stall = spec.stall_silence_seconds
    override = os.environ.get("CONSENSUS_MCP_STALL_SILENCE_SECONDS")
    if override:
        try:
            default_stall = float(override)
        except ValueError:
            print(f"warning: ignoring invalid CONSENSUS_MCP_STALL_SILENCE_SECONDS={override!r}; using default {default_stall}s", file=sys.stderr)
    parser.add_argument("--stall-silence-seconds", type=float, default=default_stall)
    parser.add_argument("--smoke", action="store_true")
    if spec.default_bin:
        parser.add_argument(f"--{spec.name}-bin", default=spec.default_bin)
    if spec.configure_parser:
        spec.configure_parser(parser)
    ns = parser.parse_args(argv)
    log_path, anchors, provenance = None, {}, {}
    try:
        repo = helper("_resolve_repo_root")()
    except base.RepoRootResolutionError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__, "failure_class": "crash"}))
        return 4
    log_path = repo / "consensus-state" / "state" / "dispatch-log.jsonl"
    iteration_id = Path(ns.iteration_dir).name or "unknown-iteration"
    reviewer_id = ns.reviewer_id or f"{spec.name}-{iteration_id}-1"
    anchors = {"iteration_id": iteration_id, "reviewer_id": reviewer_id,
               "pass_id": ns.pass_id or base.derive_pass_id(iteration_id, ns.review_target, reviewer_id)}
    def log(event, **fields):
        helper("_log_dispatch")(log_path, {"event": event, **anchors, **fields})
    try:
        normalize = helper("_normalize_relative_to_repo")
        iteration = normalize(ns.iteration_dir, repo)
        iteration.mkdir(parents=True, exist_ok=True)
        iteration_id = iteration.name
        reviewer_id = ns.reviewer_id or f"{spec.name}-{iteration_id}-1"
        anchors.update(iteration_id=iteration_id, reviewer_id=reviewer_id,
                       pass_id=ns.pass_id or base.derive_pass_id(iteration_id, ns.review_target, reviewer_id))
        goal_path, target = normalize(ns.goal_packet, repo), normalize(ns.review_target, repo)
        templates = Path(__file__).parent / "dispatch_templates"
        template_name = "gemini" if spec.name == "kimi" and ns.mode == "proposal" else spec.template_name
        schema_name = "gemini" if spec.name == "kimi" or (spec.name == "grok" and ns.mode == "review") else spec.template_name
        template = normalize(ns.prompt_template, repo) if ns.prompt_template else templates / f"{template_name}_{ns.mode}_template.md"
        schema = normalize(ns.schema, repo) if ns.schema else templates / f"{schema_name}_{ns.mode}_schema.json"
    except (base.OutsideRepoPathError, OSError) as exc:
        log("dispatch_failed", error=str(exc), error_type=type(exc).__name__, timeout_seconds=ns.timeout_seconds)
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__, "failure_class": "crash"}))
        return 5
    if ns.smoke and os.environ.get(f"CONSENSUS_MCP_RUN_REAL_{spec.name.upper()}_SMOKE") != "1":
        message = f"--smoke requires CONSENSUS_MCP_RUN_REAL_{spec.name.upper()}_SMOKE=1 in the environment"
        log("dispatch_refused", error=message, error_type="smoke_env_gate")
        print(json.dumps({"ok": False, "error": message, "error_type": "smoke_env_gate", "failure_class": "auth"}))
        return 3
    target_display = str(target.relative_to(repo)).replace("\\", "/") if target else None
    if repo.resolve() == Path.home().resolve() or not (repo / ".git").exists():
        message = f"repo root {repo} must be a proper git repository, not the user's home directory"
        log("dispatch_failed", error=message, error_type="DispatchPreflightError")
        print(json.dumps({"ok": False, "error": message, "error_type": "DispatchPreflightError", "failure_class": "crash"}))
        return 4
    profile_token = None
    try:
        from consensus_mcp._contributor_profiles import merge_profiles
        project_path = repo / ".consensus" / "config.yaml"
        project = yaml.safe_load(project_path.read_text(encoding="utf-8")) if project_path.exists() else {}
        contributors = (project or {}).get("contributors", {}) or {}
        profiles = merge_profiles(load_builtin_profiles(), contributors.get("profiles", {}))
        profile = dict(spec.profile or profiles.get(ns.profile, {}))
        profile.update((contributors.get("adapters", {}) or {}).get(spec.name, {}) or {})
        profile["_api_key_envs"] = [p.get("api_key_env") for p in profiles.values() if p.get("api_key_env")]
        profile["_abort_root"] = repo
        if profile.get("api_key_env"):
            profile["_api_key_envs"].append(profile["api_key_env"])
        from consensus_mcp import _sensitivity
        if _sensitivity.is_restricted(profile, ns.profile) or _sensitivity.is_restricted(profile, spec.name):
            try:
                answer = _sensitivity.standing_answer(project or {})
            except ValueError:
                answer = None
            if answer != "no":
                message = (f"seat {ns.profile} does not review sensitive data and this project "
                           "or run is marked sensitive_data: yes" if answer == "yes" else
                           f"seat {ns.profile} does not review sensitive data and nobody has said "
                           "whether this project holds any. Ask the user: "
                           + _sensitivity.question([ns.profile])
                           + " Then run consensus-mcp-consult with --sensitive-data yes|no, or set "
                           "sensitive_data in .consensus/config.yaml.")
                log("dispatch_failed", error=message, error_type="SensitiveDataPolicy")
                print(json.dumps({"ok": False, "error": message, "error_type": "SensitiveDataPolicy",
                                  "failure_class": "auth"}))
                return 3
        profile_token = _ACTIVE_PROFILE.set(profile)
        ns.profile_data = profile
        supplied = list(sys.argv[1:] if argv is None else argv)
        if not any(arg == "--timeout-seconds" or arg.startswith("--timeout-seconds=") for arg in supplied):
            ns.timeout_seconds = profile.get("timeout_seconds", spec.timeout_seconds)
        ns.model = ns.model or profile.get("model") or spec.default_model
        ns.iter_dir, ns.pass_id = iteration, anchors["pass_id"]
        provenance.update(model=ns.model, adapter=spec.name, mode=ns.mode,
                          review_target_path=target_display)
        if hasattr(ns, "effort"):
            provenance["effort"] = ns.effort
        log("dispatch_start", **provenance, timeout_seconds=ns.timeout_seconds, smoke=ns.smoke)
        goal_text = goal_path.read_text(encoding="utf-8")
        goal = helper("_load_goal_packet")(goal_path)
        ns.goal_packet_data = goal
        template_text = helper("_load_template")(template)
        if spec.prepare_template:
            template_text = spec.prepare_template(template_text, ns)
        if spec.name == "kimi" and ns.mode == "proposal" and not ns.prompt_template:
            template_text = template_text.replace("gemini", "kimi").replace("Gemini", "Kimi")
        target_text = target.read_text(encoding="utf-8") if target else None
        target_hash = base._sha256_str(target_text) if target_text is not None else None
        review = None
        if target and target.suffix.lower() in (".yaml", ".yml"):
            try:
                candidate = yaml.safe_load(target_text)
                review = candidate if isinstance(candidate, dict) else None
            except yaml.YAMLError:
                pass
        prompt = helper("_build_prompt")(goal, template_text, iteration_dir=str(iteration),
                    review_packet_path=str(goal_path), review_target_path=str(target) if target else None,
                    review_target_hash=target_hash, review_packet=review, review_target_content=target_text)
        provenance.update(prompt_sha256=base._sha256_str(prompt),
                          schema_sha256=base._sha256_str(schema.read_text(encoding="utf-8")),
                          goal_packet_sha256=base._sha256_str(goal_text),
                          scope_signature=(goal.get("authorization") or {}).get("scope_signature", ""),
                          review_target_hash=target_hash)
        if ns.mode == "proposal":
            provenance.update(proposal_schema_path=str(schema),
                              proposal_schema_sha256=provenance["schema_sha256"])
        version = getattr(ns_module, f"_get_{spec.name}_version", None)
        if version:
            provenance[f"{spec.name}_version"] = version(getattr(ns, f"{spec.name}_bin"))
        def invoke(model):
            workdir, before = None, None
            try:
                if spec.disposable:
                    snapshot = getattr(ns_module, "_repo_status_snapshot", repo_status_snapshot)
                    before = snapshot(repo)
                    try:
                        workdir = getattr(ns_module, "_make_disposable_workdir", make_disposable_workdir)(repo)
                    except _WorkdirTooLargeToIsolate as exc:
                        error_class = getattr(ns_module, f"{spec.name.title()}InvocationError", DispatchError)
                        raise error_class(f"{exc}; refusing to run {spec.name.title()} against the real repo") from exc
                    actual_prompt = getattr(ns_module, "_rewrite_prompt_paths", rewrite_prompt_paths)(prompt, repo, workdir)
                else:
                    actual_prompt = prompt
                raw = spec.invoke(prompt=actual_prompt, model=model, repo_root=workdir or repo,
                                  schema_path=schema, ns=ns, log_path=log_path, anchors=anchors)
                extracted = spec.parse(raw, goal_packet=goal, review_packet=review, repo_root=repo,
                                       ns=ns, schema_path=schema)
                return raw, extracted
            finally:
                if workdir is not None:
                    getattr(ns_module, "_cleanup_disposable_workdir", cleanup_disposable_workdir)(workdir)
                if before is not None:
                    after = snapshot(repo)
                    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
                    if changed:
                        log("dispatch_integrity_violation", changed_paths="\n".join(changed))
                        error_class = getattr(ns_module, f"{spec.name.title()}IntegrityError", DispatchError)
                        raise error_class("post-dispatch integrity check FAILED: real repo mutated: " + ", ".join(changed))
        started = time.monotonic()
        (raw, extracted), model_provenance = run_model_chain(invoke, ns.model, profile.get("fallback_models", ()), profile.get("quota_signatures", ()))
        if spec.name == "kimi":
            print(f"[kimi-timing] landed in {time.monotonic() - started:.1f}s (ceiling {ns.timeout_seconds}s)", file=sys.stderr)
        resolve_model = getattr(ns_module, f"_resolve_{spec.name}_model", None)
        if resolve_model:
            model_provenance["model_used"] = model_provenance["family"] = resolve_model(model_provenance["model_used"])
        provenance.update(model_provenance, model=model_provenance["model_used"], output_sha256=base._sha256_str(raw))
        packet = helper("_build_sealed_packet")(extracted, iteration_id, reviewer_id, anchors["pass_id"],
                    provenance=provenance, attestation_method=f"auto_{spec.name}_dispatch")
        packet["family"] = model_provenance["model_used"]
        if spec.name == "codex":
            result = helper("_seal_via_t6")(packet, iteration)
        else:
            filename = (getattr(ns_module, "_sealed_mirror_filename")(reviewer_id, anchors["pass_id"])
                        if hasattr(ns_module, "_sealed_mirror_filename") else f"{spec.name}-review.yaml")
            result = helper("_seal_via_t6")(packet, iteration, sealed_filename=filename)
    except Exception as exc:
        kind = classify_failure(exc)
        log("dispatch_failed", **provenance, error=str(exc), error_type=type(exc).__name__, failure_class=kind, timeout_seconds=ns.timeout_seconds)
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__, "failure_class": kind}))
        return 1 if isinstance(exc, (DispatchError, ValueError)) else 2
    finally:
        if profile_token is not None:
            _ACTIVE_PROFILE.reset(profile_token)
    log("dispatch_done", **provenance, timeout_seconds=ns.timeout_seconds, exit_code=0,
        packet_sha256=result.get("packet_sha256"), archive_sealed_path=result.get("archive_sealed_path"),
        local_mirror_path=result.get("sealed_path"), sealed_path=result.get("sealed_path"),
        t6_audit_event_id=result.get("audit_event_id"))
    print(json.dumps({"ok": True, "pass_id": anchors["pass_id"], **result}))
    return 0
