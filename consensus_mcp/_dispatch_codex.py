"""Codex provider quirks over the shared dispatch runner."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from consensus_mcp._dispatch_runner import (
    ProviderSpec,
    main_for_provider,
    run_process,
    subprocess_env,
)
from consensus_mcp.contributors.base import DispatchError
from consensus_mcp._contributor_profiles import load_builtin_profiles
# Compatibility reexports keep existing imports and test seams available.
from consensus_mcp._dispatch_base import (
    derive_pass_id,
    _REPO_ROOT_MARKERS,
    RepoRootResolutionError,
    _has_repo_markers,
    _resolve_repo_root,
    OutsideRepoPathError,
    _normalize_for_compare,
    _normalize_relative_to_repo,
    _load_goal_packet,
    _load_template,
    _FENCE_LANG_BY_EXT,
    _format_touched_files_contents,
    _build_prompt,
    _terminate_process_tree,
    _compute_per_patch_base_sha,
    _sha256_str,
    _build_sealed_packet,
    _seal_via_t6,
    _log_dispatch,
    _PATCH_PROPOSAL_REQUIRED,
    _PATCH_PROPOSAL_OPTIONAL,
    _PATCH_PROPOSAL_ALLOWED,
    _PATCH_ID_PATTERN,
    _DIFF_FILE_HEADER_RE,
    _APPLY_PATCH_BEGIN_MARKER,
    _APPLY_PATCH_UPDATE_MARKER,
    _validate_patch_proposal as _base_validate_patch_proposal,
    build_failed_event,
    record_reader_error,
    scrub_env_keys,
    CODEX_SCRUBBED_ENV_KEYS,
)

def _codex_subprocess_env() -> dict:
    env = subprocess_env()
    return env

def _effective_stall_silence(default: float=180.0) -> float:
    value = os.environ.get('CONSENSUS_MCP_STALL_SILENCE_SECONDS')
    if value:
        try:
            return float(value)
        except ValueError:
            print(
                f'warning: ignoring invalid CONSENSUS_MCP_STALL_SILENCE_SECONDS={value!r} (not a number); using default {default}s',
                file=sys.stderr,
            )
    return default

def _validate_patch_proposal(
    finding_index: int,
    finding_id: str,
    pp: dict,
    all_finding_ids: set,
    goal_packet: dict | None,
    review_packet: dict | None = None,
    repo_root: Path | None = None,
) -> None:
    _base_validate_patch_proposal(
        finding_index=finding_index,
        finding_id=finding_id,
        pp=pp,
        all_finding_ids=all_finding_ids,
        goal_packet=goal_packet,
        review_packet=review_packet,
        repo_root=repo_root,
        error_class=CodexOutputParseError,
    )
_VALID_SEVERITIES = {'low', 'medium', 'high', 'blocking', 'critical'}
_FINDING_ID_PATTERN = re.compile('^codex-rev-\\d+$')
_REQUIRED_FINDING_FIELDS = ('id', 'severity', 'summary', 'citation', 'risk', 'recommendation')
_ALLOWED_FINDING_KEYS = set(_REQUIRED_FINDING_FIELDS) | {'patch_proposal', 'patch_not_proposed_reason'}
_ALLOWED_TOP_LEVEL_KEYS = {'findings', 'goal_satisfied', 'blocking_objections', 'goal_satisfied_rationale'}
_BLOCKING_SEVERITIES = {'blocking', 'critical'}
_DEFAULT_CODEX_MODEL = load_builtin_profiles()['codex']['model']
_DEFAULT_CODEX_EFFORT = 'low'

class CodexInvocationError(DispatchError):
    pass

class CodexOutputParseError(ValueError):
    pass

def _resolve_codex_bin(codex_bin: str) -> str:
    if sys.platform == 'win32' and len(codex_bin) >= 3 and (codex_bin[0] == '/') and codex_bin[1].isalpha() and (codex_bin[2] == '/'):
        drive = codex_bin[1].upper()
        rest = codex_bin[3:].replace('/', '\\')
        codex_bin = f'{drive}:\\{rest}'
    if os.path.sep in codex_bin or (len(codex_bin) > 1 and codex_bin[1] == ':'):
        if sys.platform == 'win32':
            p = Path(codex_bin)
            try:
                if p.exists() and p.stat().st_size == 0:
                    windows_apps = os.environ.get('LOCALAPPDATA', '') + '\\Microsoft\\WindowsApps'
                    if windows_apps and str(p).lower().startswith(windows_apps.lower()):
                        raise CodexInvocationError(f'codex binary {codex_bin!r} is a Windows App Execution Alias stub (0-byte file in WindowsApps). subprocess cannot exec App Aliases - install the real codex CLI binary or adjust PATH so a non-stub variant is preferred.')
            except OSError:
                pass
        return codex_bin
    if sys.platform == 'win32' and '.' not in Path(codex_bin).name:
        for ext in ('.exe', '.cmd', '.bat', '.ps1'):
            cand = shutil.which(codex_bin + ext)
            if cand:
                return _resolve_codex_bin(cand)
    resolved = shutil.which(codex_bin)
    if resolved is None:
        return codex_bin
    if sys.platform == 'win32' and resolved.lower().endswith('.ps1'):
        cmd_alt = shutil.which(codex_bin + '.cmd')
        if cmd_alt:
            resolved = cmd_alt
    if sys.platform == 'win32':
        p = Path(resolved)
        try:
            if p.exists() and p.stat().st_size == 0:
                windows_apps = os.environ.get('LOCALAPPDATA', '') + '\\Microsoft\\WindowsApps'
                if windows_apps and str(p).lower().startswith(windows_apps.lower()):
                    raise CodexInvocationError(f'codex binary resolved to {resolved!r} which is a Windows App Execution Alias stub (0-byte file in WindowsApps). subprocess cannot exec App Aliases - install the real codex CLI binary or adjust PATH so a non-stub variant is preferred.')
        except OSError:
            pass
    return resolved

def _get_codex_version(codex_bin: str) -> str:
    try:
        result = subprocess.run(
            [_resolve_codex_bin(codex_bin), '--version'],
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

def _invoke_codex(
    prompt,
    codex_bin,
    timeout_seconds,
    repo_root,
    schema_path,
    model = _DEFAULT_CODEX_MODEL,
    effort = _DEFAULT_CODEX_EFFORT,
    log_path = None,
    anchors = None,
    heartbeat_interval = 30.0,
    stall_silence_seconds = 180.0,
    poll_interval = 0.5,
    time_fn = None,
    popen_factory = None,
    *,
    _sleep = None,
    _drain_stderr = True,
):
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp:
        output = Path(tmp.name)
    try:
        argv = [_resolve_codex_bin(codex_bin), 'exec', '--model', model, '-c', f'model_reasoning_effort="{effort}"', '--skip-git-repo-check', '--cd', str(repo_root), '--sandbox', 'read-only', '--output-schema', str(schema_path), '-o', str(output), '-']
        run_process(
            argv,
            prompt=prompt,
            cwd=repo_root,
            env=_codex_subprocess_env(),
            timeout_seconds=timeout_seconds,
            stall_silence_seconds=stall_silence_seconds,
            log_path=log_path,
            anchors=anchors,
            heartbeat_interval=heartbeat_interval,
            poll_interval=poll_interval,
            time_fn=time_fn,
            popen_factory=popen_factory,
            _sleep=_sleep,
            _drain_stderr=_drain_stderr,
        )
        return output.read_text(encoding='utf-8')
    except DispatchError as exc:
        raise CodexInvocationError(str(exc), exc.failure_class) from exc
    finally:
        _unlink_quietly(output)


def _unlink_quietly(path, attempts = 5, delay = 0.05):
    # Best-effort temp cleanup. On Windows a just-killed child (or an AV
    # scanner) can hold the -o file for a moment; a cleanup OSError must never
    # mask the real dispatch outcome (pre-runner code swallowed it too).
    for i in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError:
            if i + 1 < attempts:
                time.sleep(delay)

def _parse_codex_output(
    text: str,
    goal_packet: dict | None = None,
    review_packet: dict | None = None,
    repo_root: Path | None = None,
) -> dict:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CodexOutputParseError(f'codex output is not valid JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise CodexOutputParseError(f'codex output JSON root must be an object, got {type(parsed).__name__}')
    unknown_top = set(parsed.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_top:
        raise CodexOutputParseError(f'codex output JSON has unexpected top-level keys: {sorted(unknown_top)}')
    for required in ('findings', 'goal_satisfied', 'blocking_objections', 'goal_satisfied_rationale'):
        if required not in parsed:
            raise CodexOutputParseError(f'codex output JSON missing required key: {required!r}')
    if not isinstance(parsed['findings'], list):
        raise CodexOutputParseError("'findings' must be an array")
    if not isinstance(parsed['goal_satisfied'], bool):
        raise CodexOutputParseError(f"'goal_satisfied' must be boolean, got {type(parsed['goal_satisfied']).__name__}")
    if not isinstance(parsed['blocking_objections'], list):
        raise CodexOutputParseError(f"'blocking_objections' must be an array, got {type(parsed['blocking_objections']).__name__}")
    for i, item in enumerate(parsed['blocking_objections']):
        if not isinstance(item, str):
            raise CodexOutputParseError(f'blocking_objections[{i}] must be string, got {type(item).__name__}')
    if not isinstance(parsed['goal_satisfied_rationale'], str):
        raise CodexOutputParseError(f"'goal_satisfied_rationale' must be string, got {type(parsed['goal_satisfied_rationale']).__name__}")
    all_finding_ids: set = set()
    for i, finding in enumerate(parsed['findings']):
        if isinstance(finding, dict) and isinstance(finding.get('id'), str):
            all_finding_ids.add(finding['id'])
    blocking_finding_ids = []
    for i, finding in enumerate(parsed['findings']):
        if not isinstance(finding, dict):
            raise CodexOutputParseError(f'findings[{i}] must be an object, got {type(finding).__name__}')
        unknown_keys = set(finding.keys()) - _ALLOWED_FINDING_KEYS
        if unknown_keys:
            raise CodexOutputParseError(f'findings[{i}] has unexpected keys: {sorted(unknown_keys)}')
        for required in _REQUIRED_FINDING_FIELDS:
            if required not in finding:
                raise CodexOutputParseError(f'findings[{i}] missing required field: {required!r}')
        for str_field in ('id', 'severity', 'summary', 'citation', 'risk', 'recommendation'):
            if not isinstance(finding[str_field], str):
                raise CodexOutputParseError(f'findings[{i}].{str_field} must be string, got {type(finding[str_field]).__name__}')
        if finding['severity'] not in _VALID_SEVERITIES:
            raise CodexOutputParseError(f"findings[{i}] has invalid severity {finding['severity']!r}; must be one of {sorted(_VALID_SEVERITIES)}")
        if not _FINDING_ID_PATTERN.match(finding['id']):
            raise CodexOutputParseError(f"findings[{i}] id {finding['id']!r} does not match pattern ^codex-rev-\\d+$")
        pp = finding.get('patch_proposal')
        if pp is not None:
            _validate_patch_proposal(
                finding_index=i,
                finding_id=finding['id'],
                pp=pp,
                all_finding_ids=all_finding_ids,
                goal_packet=goal_packet,
                review_packet=review_packet,
                repo_root=repo_root,
            )
        reason = finding.get('patch_not_proposed_reason')
        if reason is not None:
            if not isinstance(reason, str):
                raise CodexOutputParseError(f'findings[{i}].patch_not_proposed_reason must be string, got {type(reason).__name__}')
            if not reason.strip():
                raise CodexOutputParseError(f'findings[{i}].patch_not_proposed_reason must be non-empty')
            if pp is not None:
                raise CodexOutputParseError(f'findings[{i}]: patch_proposal and patch_not_proposed_reason are mutually exclusive (both provided)')
        if finding['severity'] in _BLOCKING_SEVERITIES:
            blocking_finding_ids.append(finding['id'])
    if goal_packet is not None:
        policy = goal_packet.get('fix_author_policy', 'permissive')
        if policy not in ('permissive', 'strict'):
            raise CodexOutputParseError(f"goal_packet.fix_author_policy={policy!r} is not a recognized value; allowed: 'permissive' (default), 'strict'")
        if policy == 'strict':
            for i, finding in enumerate(parsed['findings']):
                has_patch = isinstance(finding.get('patch_proposal'), dict)
                has_reason = bool(finding.get('patch_not_proposed_reason'))
                if not (has_patch or has_reason):
                    raise CodexOutputParseError(f"fix_author_policy=strict: findings[{i}] (id={finding.get('id')!r}) must have either patch_proposal OR patch_not_proposed_reason; neither present")
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
        raise CodexOutputParseError('; '.join(msg_parts))
    return parsed
_CODEX_PROPOSAL_SCHEMA_PATH = Path(__file__).parent / 'dispatch_templates' / 'codex_proposal_schema.json'

def _parse_codex_proposal_output(text: str, schema_path: Path | None=None) -> dict:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CodexOutputParseError(f'codex proposal output is not valid JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise CodexOutputParseError(f'codex proposal output root must be a JSON object; got {type(parsed).__name__}')
    effective_schema_path = schema_path or _CODEX_PROPOSAL_SCHEMA_PATH
    try:
        import jsonschema
    except ImportError as exc:
        raise CodexOutputParseError(f'jsonschema package required for proposal-mode validation; install with `pip install jsonschema` or reinstall consensus-mcp: {exc}') from exc
    try:
        schema = json.loads(Path(effective_schema_path).read_text(encoding='utf-8'))
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise CodexOutputParseError(f"codex proposal output failed schema validation at {'/'.join((str(p) for p in exc.absolute_path)) or '<root>'}: {exc.message}") from exc
    except FileNotFoundError as exc:
        raise CodexOutputParseError(f'proposal schema not found at {effective_schema_path}: {exc}') from exc
    if not isinstance(
        parsed.get('rationale_vs_alternatives'),
        str,
    ) or not parsed['rationale_vs_alternatives'].strip():
        raise CodexOutputParseError('rationale_vs_alternatives must be a non-empty string (parser invariant)')
    if 'structural_abstention' not in parsed or not isinstance(parsed['structural_abstention'], bool):
        raise CodexOutputParseError('structural_abstention must be present and boolean (parser invariant)')
    if not parsed['structural_abstention']:
        if parsed['selected_target'] is None:
            raise CodexOutputParseError('selected_target is required when structural_abstention is false')
        if parsed['deliverable_scope'] is None:
            raise CodexOutputParseError('deliverable_scope is required when structural_abstention is false')
    return parsed

def _provider_invoke(*, prompt, model, repo_root, schema_path, ns, log_path, anchors):
    return _invoke_codex(
        prompt,
        ns.codex_bin,
        ns.timeout_seconds,
        repo_root,
        schema_path,
        model,
        ns.effort,
        log_path=log_path,
        anchors=anchors,
        stall_silence_seconds=ns.stall_silence_seconds,
    )

def _provider_parse(raw, *, goal_packet, review_packet, repo_root, ns, schema_path):
    if ns.mode == 'proposal':
        return _parse_codex_proposal_output(raw, schema_path=schema_path)
    return _parse_codex_output(raw, goal_packet=goal_packet, review_packet=review_packet, repo_root=repo_root)

def _configure_parser(parser):
    parser.add_argument('--effort', default=_DEFAULT_CODEX_EFFORT, choices=['low', 'medium', 'high', 'xhigh'])

def main(argv=None):
    return main_for_provider(
        ProviderSpec(
            name='codex',
            invoke=_provider_invoke,
            parse=_provider_parse,
            default_model=_DEFAULT_CODEX_MODEL,
            default_bin='codex',
            template_name='codex',
            configure_parser=_configure_parser,
            disposable=False,
            namespace=sys.modules[__name__],
        ),
        argv,
    )

if __name__ == '__main__':
    sys.exit(main())
