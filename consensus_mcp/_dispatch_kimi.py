"""Kimi provider quirks over the shared dispatch runner."""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from consensus_mcp._dispatch_runner import (
    ProviderSpec,
    main_for_provider,
    run_process,
    subprocess_env,
    invoke_and_parse,
    _SNAPSHOT_MAX_BYTES,
    _SNAPSHOT_MAX_FILES,
    _SnapshotIndexError,
    _TEMP_WORKDIR_IGNORE_DIRS,
    _WORKDIR_STALE_SECONDS,
    _WorkdirTooLargeToIsolate,
    _cleanup_disposable_workdir,
    _extra_ignore_dirs,
    _filesystem_manifest_snapshot,
    _gitignored_top_level_dirs,
    _make_disposable_workdir,
    _repo_status_snapshot,
    _rewrite_prompt_paths,
    _strip_symlinks,
    _sweep_stale_workdirs,
)
from consensus_mcp.contributors.base import DispatchError
from consensus_mcp._contributor_profiles import load_builtin_profiles
# Compatibility reexports keep existing imports and test seams available.
from consensus_mcp._dispatch_base import (
    derive_pass_id,
    RepoRootResolutionError,
    _resolve_repo_root,
    OutsideRepoPathError,
    _normalize_relative_to_repo,
    _load_goal_packet,
    _load_template,
    _build_prompt,
    _terminate_process_tree,
    _sha256_str,
    _build_sealed_packet,
    _seal_via_t6,
    _log_dispatch,
    _extract_json_from_text,
    build_failed_event,
    record_reader_error,
    scrub_env_keys,
    KIMI_SCRUBBED_ENV_KEYS,
)
_KIMI_FINDING_ID_PATTERN = re.compile('^kimi-rev-\\d+$')
_VALID_SEVERITIES = {'low', 'medium', 'high', 'blocking', 'critical'}
_REQUIRED_FINDING_FIELDS = ('id', 'severity', 'summary', 'citation', 'risk', 'recommendation')
_ALLOWED_FINDING_KEYS = set(_REQUIRED_FINDING_FIELDS) | {'patch_proposal', 'patch_not_proposed_reason'}
_ALLOWED_TOP_LEVEL_KEYS = {'findings', 'goal_satisfied', 'blocking_objections', 'goal_satisfied_rationale'}
_BLOCKING_SEVERITIES = {'blocking', 'critical'}
_DEFAULT_STALL_SILENCE_SECONDS = 240.0
_DEFAULT_TIMEOUT_SECONDS = 1800
_KIMI_RETRYABLE_EXIT = 75
_DEFAULT_KIMI_BIN = 'kimi'
_DEFAULT_KIMI_MODEL = load_builtin_profiles()['kimi']['model']
_KIMI_MODEL_ID_BY_DISPLAY = {'Kimi K3': 'kimi-code/k3', 'kimi-k3': 'kimi-code/k3', 'Kimi K3.1': _DEFAULT_KIMI_MODEL, 'kimi-k3.1': _DEFAULT_KIMI_MODEL}
_KIMI_CODE_INLINE_PROMPT_MAX_BYTES = 28 * 1024 if sys.platform == 'win32' else 96 * 1024

def _resolve_kimi_model(model: str | None) -> str | None:
    if model is None:
        return None
    return _KIMI_MODEL_ID_BY_DISPLAY.get(model, model)
_KIMI_STRIP_PATTERNS = (re.compile('\\n*To resume this session:\\s*kimi -r \\S+\\s*$'),)




def _strip_kimi_output_chrome(text: str) -> str:
    for pat in _KIMI_STRIP_PATTERNS:
        text = pat.sub('', text)
    return text

class KimiInvocationError(DispatchError):

    def __init__(self, message: str, retryable: bool=False):
        super().__init__(message)
        self.retryable = retryable

class KimiOutputParseError(ValueError):
    pass

class KimiIntegrityError(DispatchError):
    pass

def _kimi_subprocess_env() -> dict:
    env = subprocess_env()
    return env









def _effective_stall_silence(default: float=_DEFAULT_STALL_SILENCE_SECONDS) -> float:
    env_silence = os.environ.get('CONSENSUS_MCP_STALL_SILENCE_SECONDS')
    if env_silence:
        try:
            return float(env_silence)
        except ValueError:
            print(
                f'warning: ignoring invalid CONSENSUS_MCP_STALL_SILENCE_SECONDS={env_silence!r} (not a number); using default {default}s',
                file=sys.stderr,
            )
    return default

def _resolve_kimi_bin(kimi_bin: str) -> str:
    if sys.platform == 'win32' and len(kimi_bin) >= 3 and (kimi_bin[0] == '/') and kimi_bin[1].isalpha() and (kimi_bin[2] == '/'):
        drive = kimi_bin[1].upper()
        rest = kimi_bin[3:].replace('/', '\\')
        kimi_bin = f'{drive}:\\{rest}'
    if os.path.sep in kimi_bin or (len(kimi_bin) > 1 and kimi_bin[1] == ':'):
        if sys.platform == 'win32':
            p = Path(kimi_bin)
            try:
                if p.exists() and p.stat().st_size == 0:
                    windows_apps = os.environ.get('LOCALAPPDATA', '') + '\\Microsoft\\WindowsApps'
                    if windows_apps and str(p).lower().startswith(windows_apps.lower()):
                        raise KimiInvocationError(f'kimi binary {kimi_bin!r} is a Windows App Execution Alias stub (0-byte file in WindowsApps). subprocess cannot exec App Aliases - install the real kimi CLI binary or adjust PATH so a non-stub variant is preferred.')
            except OSError:
                pass
        return kimi_bin
    if sys.platform == 'win32' and '.' not in Path(kimi_bin).name:
        for ext in ('.exe', '.cmd', '.bat', '.ps1'):
            cand = shutil.which(kimi_bin + ext)
            if cand:
                return _resolve_kimi_bin(cand)
    resolved = shutil.which(kimi_bin)
    if resolved is None and kimi_bin == 'kimi':
        kimi_code_bin = Path.home() / '.kimi-code' / 'bin' / 'kimi'
        if kimi_code_bin.exists():
            return str(kimi_code_bin)
    if resolved is None:
        return kimi_bin
    if sys.platform == 'win32' and resolved.lower().endswith('.ps1'):
        cmd_alt = shutil.which(kimi_bin + '.cmd')
        if cmd_alt:
            resolved = cmd_alt
    if sys.platform == 'win32':
        p = Path(resolved)
        try:
            if p.exists() and p.stat().st_size == 0:
                windows_apps = os.environ.get('LOCALAPPDATA', '') + '\\Microsoft\\WindowsApps'
                if windows_apps and str(p).lower().startswith(windows_apps.lower()):
                    raise KimiInvocationError(f'kimi binary resolved to {resolved!r} which is a Windows App Execution Alias stub (0-byte file in WindowsApps). subprocess cannot exec App Aliases - install the real kimi CLI binary or adjust PATH so a non-stub variant is preferred.')
        except OSError:
            pass
    return resolved

def _is_kimi_code_cli(resolved_bin: str) -> bool:
    p = Path(resolved_bin)
    if '.kimi-code' in str(p):
        return True
    if p.name == 'kimi' and str(p) == 'kimi':
        return True
    return False

def _get_kimi_version(kimi_bin: str) -> str:
    try:
        result = subprocess.run(
            [_resolve_kimi_bin(kimi_bin), '--version'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip() or 'unknown'
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return 'unknown'

def _peel_assistant_content(raw_output: str) -> str:
    if not raw_output or not raw_output.strip():
        raise KimiOutputParseError('kimi produced empty output; expected a stream-json line {"role":"assistant","content":"..."}')
    last_content: str | None = None
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get('role') == 'assistant' and ('content' in obj):
            content = obj['content']
            if isinstance(content, str):
                last_content = content
            elif content is not None:
                last_content = json.dumps(content)
    if last_content is not None:
        return last_content
    stripped = raw_output.strip()
    if stripped.startswith('{') or stripped.startswith('[') or stripped.startswith('```') or ('{' in stripped and '}' in stripped):
        return stripped
    raise KimiOutputParseError(f'kimi output did not contain an assistant stream-json envelope and is not bare JSON; first 500 chars: {raw_output[:500]!r}')

def _invoke_kimi(
    prompt,
    kimi_bin,
    timeout_seconds,
    repo_root,
    model = None,
    thinking = True,
    log_path = None,
    anchors = None,
    heartbeat_interval = 30.0,
    stall_silence_seconds = _DEFAULT_STALL_SILENCE_SECONDS,
    poll_interval = 0.5,
    time_fn = None,
    popen_factory = None,
):
    binary = _resolve_kimi_bin(kimi_bin)
    native = _is_kimi_code_cli(binary)
    if native:
        if len(prompt.encode('utf-8')) > _KIMI_CODE_INLINE_PROMPT_MAX_BYTES:
            raise KimiInvocationError('Kimi Code CLI prompt exceeds safe inline argv size and this CLI does not expose a stdin/prompt-file transport')
        argv = [binary, '-p', prompt, '--output-format', 'text']
    else:
        argv = [binary, '--quiet', '--thinking' if thinking else '--no-thinking']
    if model:
        argv += ['--model', _resolve_kimi_model(model)]
    if not native:
        argv += ['--work-dir', str(repo_root)]
    try:
        return run_process(
            argv,
            prompt=None if native else prompt,
            cwd=repo_root,
            env=_kimi_subprocess_env(),
            timeout_seconds=timeout_seconds,
            stall_silence_seconds=stall_silence_seconds,
            startup_silence_seconds=timeout_seconds,
            log_path=log_path,
            anchors=anchors,
            heartbeat_interval=heartbeat_interval,
            poll_interval=poll_interval,
            time_fn=time_fn,
            popen_factory=popen_factory,
        )
    except DispatchError as exc:
        error = KimiInvocationError(
            str(exc),
            retryable=exc.failure_class in {'timeout', 'rate_limit'} or 'exit=75' in str(exc),
        )
        error.failure_class = exc.failure_class
        raise error from exc

def _parse_kimi_output(text: str) -> dict:
    candidate = _extract_json_from_text(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise KimiOutputParseError(f'kimi output is not valid JSON: {exc}; first 500 chars of raw: {text[:500]!r}') from exc
    if not isinstance(parsed, dict):
        raise KimiOutputParseError(f'kimi output JSON root must be an object, got {type(parsed).__name__}')
    unknown_top = set(parsed.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_top:
        raise KimiOutputParseError(f'kimi output JSON has unexpected top-level keys: {sorted(unknown_top)}')
    for required in ('findings', 'goal_satisfied', 'blocking_objections', 'goal_satisfied_rationale'):
        if required not in parsed:
            raise KimiOutputParseError(f'kimi output JSON missing required key: {required!r}')
    if not isinstance(parsed['findings'], list):
        raise KimiOutputParseError("'findings' must be an array")
    if not isinstance(parsed['goal_satisfied'], bool):
        raise KimiOutputParseError(f"'goal_satisfied' must be boolean, got {type(parsed['goal_satisfied']).__name__}")
    if not isinstance(parsed['blocking_objections'], list):
        raise KimiOutputParseError(f"'blocking_objections' must be an array, got {type(parsed['blocking_objections']).__name__}")
    if not isinstance(parsed['goal_satisfied_rationale'], str):
        raise KimiOutputParseError(f"'goal_satisfied_rationale' must be string, got {type(parsed['goal_satisfied_rationale']).__name__}")
    if not parsed['goal_satisfied_rationale'].strip():
        raise KimiOutputParseError("'goal_satisfied_rationale' must be a non-empty string (prompt contract)")
    for i, item in enumerate(parsed['blocking_objections']):
        if not isinstance(item, str):
            raise KimiOutputParseError(f'blocking_objections[{i}] must be string, got {type(item).__name__}')
    blocking_finding_ids = []
    for i, finding in enumerate(parsed['findings']):
        if not isinstance(finding, dict):
            raise KimiOutputParseError(f'findings[{i}] must be an object, got {type(finding).__name__}')
        unknown_keys = set(finding.keys()) - _ALLOWED_FINDING_KEYS
        if unknown_keys:
            raise KimiOutputParseError(f'findings[{i}] has unexpected keys: {sorted(unknown_keys)}')
        for required in _REQUIRED_FINDING_FIELDS:
            if required not in finding:
                raise KimiOutputParseError(f'findings[{i}] missing required field: {required!r}')
        for str_field in ('id', 'severity', 'summary', 'citation', 'risk', 'recommendation'):
            if not isinstance(finding[str_field], str):
                raise KimiOutputParseError(f'findings[{i}].{str_field} must be string, got {type(finding[str_field]).__name__}')
        if not finding['summary'].strip():
            raise KimiOutputParseError(f'findings[{i}].summary must be a non-empty string (schema contract)')
        if finding['severity'] not in _VALID_SEVERITIES:
            raise KimiOutputParseError(f"findings[{i}] has invalid severity {finding['severity']!r}; must be one of {sorted(_VALID_SEVERITIES)}")
        if not _KIMI_FINDING_ID_PATTERN.match(finding['id']):
            raise KimiOutputParseError(f"findings[{i}] id {finding['id']!r} does not match pattern ^kimi-rev-\\d+$")
        pp = finding.get('patch_proposal')
        if pp is not None:
            raise KimiOutputParseError(f'findings[{i}].patch_proposal must be null - kimi is review-only')
        reason = finding.get('patch_not_proposed_reason')
        if reason is not None and (not isinstance(reason, str)):
            raise KimiOutputParseError(f'findings[{i}].patch_not_proposed_reason must be string or null, got {type(reason).__name__}')
        if finding['severity'] in _BLOCKING_SEVERITIES:
            blocking_finding_ids.append(finding['id'])
    expected_blocking = set(blocking_finding_ids)
    actual_blocking = set(parsed['blocking_objections'])
    if expected_blocking != actual_blocking:
        missing = expected_blocking - actual_blocking
        extra = actual_blocking - expected_blocking
        msg_parts = ['blocking_objections invariant violated:']
        if missing:
            msg_parts.append(f'missing finding ids: {sorted(missing)}')
        if extra:
            msg_parts.append(f'unexpected ids (not blocking/critical findings): {sorted(extra)}')
        msg_parts.append(f'expected = {{f.id : f.severity in {{blocking, critical}}}} = {sorted(expected_blocking)}')
        msg_parts.append(f'actual = {sorted(actual_blocking)}')
        raise KimiOutputParseError('; '.join(msg_parts))
    if parsed['goal_satisfied'] is True and actual_blocking:
        raise KimiOutputParseError(f'goal_satisfied=true is incoherent with non-empty blocking_objections {sorted(actual_blocking)}; a successful review cannot have blocking findings')
    return parsed
_KIMI_PROPOSAL_SCHEMA_PATH = Path(__file__).parent / 'dispatch_templates' / 'gemini_proposal_schema.json'

def _parse_kimi_proposal_output(text: str, schema_path: Path | None=None) -> dict:
    try:
        cleaned = _extract_json_from_text(text)
    except ValueError as exc:
        raise KimiOutputParseError(f'kimi proposal output: could not extract JSON: {exc}') from exc
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise KimiOutputParseError(f'kimi proposal output is not valid JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise KimiOutputParseError(f'kimi proposal output root must be a JSON object; got {type(parsed).__name__}')
    effective_schema_path = schema_path or _KIMI_PROPOSAL_SCHEMA_PATH
    try:
        import jsonschema
    except ImportError as exc:
        raise KimiOutputParseError(f'jsonschema package required for proposal-mode validation; install with `pip install jsonschema` or reinstall consensus-mcp: {exc}') from exc
    try:
        schema = json.loads(Path(effective_schema_path).read_text(encoding='utf-8'))
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise KimiOutputParseError(f"kimi proposal output failed schema validation at {'/'.join((str(p) for p in exc.absolute_path)) or '<root>'}: {exc.message}") from exc
    except FileNotFoundError as exc:
        raise KimiOutputParseError(f'proposal schema not found at {effective_schema_path}: {exc}') from exc
    if not isinstance(
        parsed.get('rationale_vs_alternatives'),
        str,
    ) or not parsed['rationale_vs_alternatives'].strip():
        raise KimiOutputParseError('rationale_vs_alternatives must be a non-empty string (parser invariant)')
    if 'structural_abstention' not in parsed or not isinstance(parsed['structural_abstention'], bool):
        raise KimiOutputParseError('structural_abstention must be present and boolean (parser invariant)')
    if not parsed['structural_abstention']:
        if parsed['selected_target'] is None:
            raise KimiOutputParseError('selected_target is required when structural_abstention is false')
        if parsed['deliverable_scope'] is None:
            raise KimiOutputParseError('deliverable_scope is required when structural_abstention is false')
    return parsed

def _invoke_kimi_with_retry(
    prompt: str,
    kimi_bin: str,
    timeout_seconds: int,
    repo_root: Path,
    model: str | None = None,
    thinking: bool = True,
    stall_silence_seconds: float = _DEFAULT_STALL_SILENCE_SECONDS,
    log_path = None,
    anchors = None,
    mode: str = 'review',
    proposal_schema_path: Path | None = None,
    retry_invocation = True,
) -> tuple[str, dict]:
    def invoke(current, suffix):
        return _invoke_kimi(prompt=current, kimi_bin=kimi_bin, timeout_seconds=timeout_seconds,
                repo_root=repo_root, model=model, thinking=thinking,
                stall_silence_seconds=stall_silence_seconds, log_path=log_path, anchors=anchors)
    def parse(raw):
        content = _peel_assistant_content(_strip_kimi_output_chrome(raw))
        return (_parse_kimi_proposal_output(content, schema_path=proposal_schema_path)
                if mode == "proposal" else _parse_kimi_output(content))
    return invoke_and_parse(invoke, parse, prompt, log_path=log_path, anchors=anchors,
                            retry_invocation=retry_invocation)


def _sealed_mirror_filename(reviewer_id: str, pass_id: str) -> str:
    anchor = (pass_id or reviewer_id or '').strip()
    if not anchor:
        return 'kimi-review.yaml'
    safe = re.sub('[^A-Za-z0-9_-]+', '-', anchor)
    safe = re.sub('-{2,}', '-', safe).strip('-')
    if not safe:
        return 'kimi-review.yaml'
    return f'kimi-review-{safe}.yaml'

def _provider_invoke(*, prompt, model, repo_root, schema_path, ns, log_path, anchors):
    raw, _ = _invoke_kimi_with_retry(
        prompt=prompt,
        kimi_bin=ns.kimi_bin,
        timeout_seconds=ns.timeout_seconds,
        repo_root=repo_root,
        model=model,
        thinking=ns.thinking,
        stall_silence_seconds=ns.stall_silence_seconds,
        log_path=log_path,
        anchors=anchors,
        mode=ns.mode,
        proposal_schema_path=schema_path,
        retry_invocation=False,
    )
    return raw

def _provider_parse(raw, *, goal_packet, review_packet, repo_root, ns, schema_path):
    if ns.mode == 'proposal':
        return _parse_kimi_proposal_output(
            _peel_assistant_content(_strip_kimi_output_chrome(raw)),
            schema_path=schema_path,
        )
    return _parse_kimi_output(_peel_assistant_content(_strip_kimi_output_chrome(raw)))

def _configure_parser(parser):
    parser.add_argument('--thinking', action=argparse.BooleanOptionalAction, default=True)
    parser.set_defaults(no_thinking=False)

def main(argv=None):
    return main_for_provider(
        ProviderSpec(
            name='kimi',
            invoke=_provider_invoke,
            parse=_provider_parse,
            default_model=_DEFAULT_KIMI_MODEL,
            default_bin='kimi',
            template_name='kimi',
            configure_parser=_configure_parser,
            disposable=True,
            namespace=sys.modules[__name__],
            timeout_seconds=1800,
            stall_silence_seconds=240,
        ),
        argv,
    )

if __name__ == '__main__':
    sys.exit(main())
