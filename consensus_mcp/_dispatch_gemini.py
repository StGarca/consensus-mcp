"""Gemini provider quirks over the shared dispatch runner."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from consensus_mcp._dispatch_runner import (
    ProviderSpec,
    classify_failure,
    main_for_provider,
    run_process,
    subprocess_env,
    invoke_and_parse,
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
    _sha256_str,
    _build_sealed_packet,
    _seal_via_t6,
    _log_dispatch,
    _extract_json_from_text,
    build_failed_event,
    record_reader_error,
    scrub_env_keys,
    GEMINI_SCRUBBED_ENV_KEYS,
)
_GEMINI_FINDING_ID_PATTERN = re.compile('^gemini-rev-\\d+$')
_VALID_SEVERITIES = {'low', 'medium', 'high', 'blocking', 'critical'}
_REQUIRED_FINDING_FIELDS = ('id', 'severity', 'summary', 'citation', 'risk', 'recommendation')
_ALLOWED_FINDING_KEYS = set(_REQUIRED_FINDING_FIELDS) | {'patch_proposal', 'patch_not_proposed_reason'}
_ALLOWED_TOP_LEVEL_KEYS = {'findings', 'goal_satisfied', 'blocking_objections', 'goal_satisfied_rationale'}
_BLOCKING_SEVERITIES = {'blocking', 'critical'}
_DEFAULT_GEMINI_BIN = 'agy'
_DEFAULT_GEMINI_MODEL = load_builtin_profiles()['gemini']['model']

class GeminiInvocationError(DispatchError):
    pass

class GeminiOutputParseError(ValueError):
    pass

def _gemini_subprocess_env() -> dict:
    env = subprocess_env()
    env['GEMINI_CLI_TRUST_WORKSPACE'] = 'true'
    return env

def _resolve_gemini_bin(gemini_bin: str) -> str:
    if sys.platform == 'win32' and len(gemini_bin) >= 3 and (gemini_bin[0] == '/') and gemini_bin[1].isalpha() and (gemini_bin[2] == '/'):
        drive = gemini_bin[1].upper()
        rest = gemini_bin[3:].replace('/', '\\')
        gemini_bin = f'{drive}:\\{rest}'
    if os.path.sep in gemini_bin or (len(gemini_bin) > 1 and gemini_bin[1] == ':'):
        if sys.platform == 'win32':
            p = Path(gemini_bin)
            try:
                if p.exists() and p.stat().st_size == 0:
                    windows_apps = os.environ.get('LOCALAPPDATA', '') + '\\Microsoft\\WindowsApps'
                    if windows_apps and str(p).lower().startswith(windows_apps.lower()):
                        raise GeminiInvocationError(f'gemini binary {gemini_bin!r} is a Windows App Execution Alias stub (0-byte file in WindowsApps). subprocess cannot exec App Aliases - install the real gemini CLI binary or adjust PATH so a non-stub variant is preferred.')
            except OSError:
                pass
        return gemini_bin
    if sys.platform == 'win32' and '.' not in Path(gemini_bin).name:
        for ext in ('.exe', '.cmd', '.bat', '.ps1'):
            cand = shutil.which(gemini_bin + ext)
            if cand:
                return _resolve_gemini_bin(cand)
    resolved = shutil.which(gemini_bin)
    if resolved is None:
        return gemini_bin
    if sys.platform == 'win32' and resolved.lower().endswith('.ps1'):
        cmd_alt = shutil.which(gemini_bin + '.cmd')
        if cmd_alt:
            resolved = cmd_alt
    if sys.platform == 'win32':
        p = Path(resolved)
        try:
            if p.exists() and p.stat().st_size == 0:
                windows_apps = os.environ.get('LOCALAPPDATA', '') + '\\Microsoft\\WindowsApps'
                if windows_apps and str(p).lower().startswith(windows_apps.lower()):
                    raise GeminiInvocationError(f'gemini binary resolved to {resolved!r} which is a Windows App Execution Alias stub (0-byte file in WindowsApps). subprocess cannot exec App Aliases - install the real gemini CLI binary or adjust PATH so a non-stub variant is preferred.')
        except OSError:
            pass
    return resolved

def _get_gemini_version(gemini_bin: str) -> str:
    try:
        result = subprocess.run(
            [_resolve_gemini_bin(gemini_bin), '--version'],
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

_AGY_MODES = frozenset({'plan', 'auto', 'accept-edits'})


def _agy_roster(binary, timeout_seconds=30):
    """Return {model_id: display_name} from `agy models`, or None when the
    roster cannot be read (offline, old agy, unexpected output)."""
    try:
        out = subprocess.run([binary, 'models'], capture_output=True, text=True, encoding='utf-8',
                             errors='replace', timeout=timeout_seconds, stdin=subprocess.DEVNULL,
                             env=_gemini_subprocess_env())
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    roster = {}
    for line in out.stdout.splitlines():
        model_id, sep, name = line.partition('\t')
        if sep and model_id.strip() and ' ' not in model_id.strip():
            roster[model_id.strip()] = name.strip()
    return roster or None


def _resolve_agy_model(binary, model, roster_fn=None):
    """Map a configured model onto agy's current roster: exact model IDs pass,
    display names ("Gemini 4 Argon (High)") map to their ID, and a model agy no
    longer offers fails up front with the setting to change - instead of agy
    printing its roster and the review failing to parse."""
    roster = (roster_fn or _agy_roster)(binary)
    if not roster or model in roster:
        return model
    wanted = str(model).strip().casefold()
    for model_id, name in roster.items():
        if wanted in {name.casefold(), model_id.casefold()}:
            return model_id
    raise GeminiInvocationError(
        f"agy does not offer model {model!r}. Set the gemini model (contributors.profiles.gemini.model "
        f"or contributors.adapters.gemini.model in .consensus/config.yaml) to one of: "
        f"{', '.join(sorted(roster))}", 'crash')


def _agy_stream_message(prompt: str, instruction: str) -> str:
    """agy -p has no plain stdin input: piped text never reaches the model
    (verified with agy 1.5.8 transcripts). Its documented stdin channel is
    --input-format stream-json, one NDJSON user message per turn."""
    content = f'{prompt}\n\n{instruction}' if prompt else instruction
    return json.dumps({'event': 'user', 'message': {'role': 'user', 'content': content}},
                      ensure_ascii=False) + '\n'


def _agy_stream_response(raw: str) -> str:
    """The reviewer's text from agy's stream-json output (final result event)."""
    result = None
    for line in (raw or '').splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('event') == 'result' and isinstance(event.get('result'), dict):
            result = event['result']
    if result is None:
        raise GeminiInvocationError(f'agy returned no stream-json result event; first 300 chars: {(raw or "")[:300]!r}', 'parse')
    if result.get('status') != 'SUCCESS':
        error = str(result.get('error') or result.get('finished_reason') or 'unknown error')
        kind = classify_failure(RuntimeError(error))  # from agy's own error text
        raise GeminiInvocationError(f'agy run failed (status={result.get("status")}): {error}', kind)
    response = result.get('response')
    if not isinstance(response, str) or not response.strip():
        message = _agy_empty_response_message(result.get('conversation_id'))
        # A refused tool call would be refused again, so it is not retried;
        # a plain empty answer keeps the retryable parse class.
        raise GeminiInvocationError(message, 'crash' if 'agy refused a tool call' in message else 'parse')
    return response


_AGY_BRAIN = Path.home() / '.gemini' / 'antigravity-cli' / 'brain'


def _agy_step_errors(conversation_id, brain_dir=None) -> list:
    """Error text agy recorded for failed steps of one conversation, read from
    its own transcript (rows with status ERROR, e.g. "Encountered error in step
    execution: permission check failed for command ...")."""
    if not conversation_id or not re.fullmatch(r'[0-9A-Za-z-]+', str(conversation_id)):
        return []
    path = Path(brain_dir or _AGY_BRAIN) / str(conversation_id) / '.system_generated' / 'logs' / 'transcript_full.jsonl'
    errors = []
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get('status') != 'ERROR':
            continue
        text = str(row.get('error') or row.get('content') or '')
        marker = 'Encountered error in step execution:'
        if marker in text:
            text = text.split(marker, 1)[1]
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), '')
        if first:
            errors.append(first[:300])
    return errors


def _agy_empty_response_message(conversation_id, brain_dir=None) -> str:
    errors = _agy_step_errors(conversation_id, brain_dir)
    where = (f'; transcript: {Path(brain_dir or _AGY_BRAIN) / str(conversation_id)}'
             if conversation_id else '')
    if not errors:
        return 'agy ended without an answer (empty response)' + where
    message = (f'agy ended without an answer after {len(errors)} failed tool step(s); '
               f'last: {errors[-1]}')
    if any('permission check failed' in e for e in errors):
        message += ('. agy refused a tool call it needed: set contributors.adapters.gemini.skip_permissions: '
                    'true (plan mode stays read-only), or allow the command in agy settings')
    return message + where


def _invoke_gemini(
    prompt,
    gemini_bin,
    model,
    timeout_seconds,
    repo_root,
    log_path = None,
    anchors = None,
    heartbeat_interval = 30.0,
    stall_silence_seconds = 180.0,
    poll_interval = 0.5,
    time_fn = None,
    popen_factory = None,
    approval_mode = 'plan',
    skip_permissions = True,
):
    binary = _resolve_gemini_bin(gemini_bin)
    instruction = 'Now respond. Output ONLY the JSON described above; no prose, no markdown fences.'
    if Path(binary).name.lower() in {'agy', 'agy.exe'}:
        # Bare --sandbox made agy deny the reviewer's own read_file
        # on platforms that enforce it. Default = --mode plan (agy refuses to
        # write files in plan mode, verified live with agy 1.5.8) plus
        # --dangerously-skip-permissions so reads are not blocked on a prompt
        # nobody can answer. Prompts are only skipped in plan mode, so no
        # setting can combine auto-approval with a mode that writes.
        mode = approval_mode or 'plan'
        if mode not in _AGY_MODES:
            raise GeminiInvocationError(f'gemini approval_mode {mode!r} is not one of {sorted(_AGY_MODES)}; '
                                         'fix contributors.profiles.gemini.approval_mode (or contributors.adapters.gemini.approval_mode) '
                                         'in .consensus/config.yaml', 'crash')
        # The whole task travels over stdin as one stream-json message (no
        # command-line length limit); -p only switches agy to print mode.
        argv = [binary, '-p', '', '--input-format', 'stream-json', '--output-format', 'stream-json',
                '--model', model, '--sandbox', '--mode', mode]
        stdin_text = _agy_stream_message(prompt, instruction)
        if skip_permissions and mode == 'plan':
            argv.append('--dangerously-skip-permissions')
        argv += ['--print-timeout', f'{max(1, int(timeout_seconds))}s']
        # agy -p prints nothing until it finishes, so a silence watchdog would
        # kill every healthy run; agy's own --print-timeout bounds it instead.
        stall_silence_seconds = max(float(stall_silence_seconds), float(timeout_seconds))
    else:
        argv = [binary, '-p', instruction, '-m', model, '--approval-mode', 'plan', '--skip-trust']
        stdin_text = prompt
    try:
        raw = run_process(
            argv,
            prompt=stdin_text,
            cwd=repo_root,
            env=_gemini_subprocess_env(),
            timeout_seconds=timeout_seconds,
            stall_silence_seconds=stall_silence_seconds,
            log_path=log_path,
            anchors=anchors,
            heartbeat_interval=heartbeat_interval,
            poll_interval=poll_interval,
            time_fn=time_fn,
            popen_factory=popen_factory,
        )
    except DispatchError as exc:
        raise GeminiInvocationError(str(exc), exc.failure_class) from exc
    if Path(binary).name.lower() in {'agy', 'agy.exe'}:
        return _agy_stream_response(raw)
    return raw

_YAML_FENCE_RE = re.compile(r'```(?:ya?ml)\s*\n(.*?)```', re.DOTALL | re.IGNORECASE)


_BACKTICK_VALUE_RE = re.compile(r'^(\s*(?:-\s+|[A-Za-z_][\w-]*:\s+|-\s+[A-Za-z_][\w-]*:\s+))(`.*)$')


def _quote_backtick_scalars(source: str) -> str:
    """Single-quote YAML values that begin with a backtick, preserving their text."""
    out = []
    for line in source.splitlines():
        m = _BACKTICK_VALUE_RE.match(line)
        if m:
            value = m.group(2).rstrip()
            line = m.group(1) + "'" + value.replace("'", "''") + "'"
        out.append(line)
    return "\n".join(out)


def _load_review_object(text: str, label: str):
    """Parse a reviewer answer: JSON first; if that fails, YAML. Gemini
    sometimes answers in YAML despite the JSON instruction (live: a complete
    12,154-character plan review, fenced as ```yaml, was discarded). Schema
    and invariant checks run unchanged on the parsed object."""
    candidate = _extract_json_from_text(text)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as json_exc:
        fenced = _YAML_FENCE_RE.search(text or '')
        source = fenced.group(1) if fenced else (text or '').strip()
        import yaml
        try:
            loaded = yaml.safe_load(source)
        except Exception:
            # Models often start a plain YAML value with a backtick (invalid
            # YAML); quote only those single-line values, then re-parse.
            try:
                loaded = yaml.safe_load(_quote_backtick_scalars(source))
            except Exception:
                loaded = None
        if isinstance(loaded, dict):
            # Normalize YAML scalars (dates etc.) to JSON types for the schema.
            return json.loads(json.dumps(loaded, default=str))
        raise GeminiOutputParseError(f'{label} is not valid JSON (or a YAML mapping): {json_exc}; '
                                     f'first 500 chars of raw: {(text or "")[:500]!r}') from json_exc


def _parse_gemini_output(text: str, goal_packet: dict | None=None) -> dict:
    parsed = _load_review_object(text, 'gemini output')
    if not isinstance(parsed, dict):
        raise GeminiOutputParseError(f'gemini output JSON root must be an object, got {type(parsed).__name__}')
    unknown_top = set(parsed.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_top:
        raise GeminiOutputParseError(f'gemini output JSON has unexpected top-level keys: {sorted(unknown_top)}')
    for required in ('findings', 'goal_satisfied', 'blocking_objections', 'goal_satisfied_rationale'):
        if required not in parsed:
            raise GeminiOutputParseError(f'gemini output JSON missing required key: {required!r}')
    if not isinstance(parsed['findings'], list):
        raise GeminiOutputParseError("'findings' must be an array")
    if not isinstance(parsed['goal_satisfied'], bool):
        raise GeminiOutputParseError(f"'goal_satisfied' must be boolean, got {type(parsed['goal_satisfied']).__name__}")
    if not isinstance(parsed['blocking_objections'], list):
        raise GeminiOutputParseError(f"'blocking_objections' must be an array, got {type(parsed['blocking_objections']).__name__}")
    if not isinstance(parsed['goal_satisfied_rationale'], str):
        raise GeminiOutputParseError(f"'goal_satisfied_rationale' must be string, got {type(parsed['goal_satisfied_rationale']).__name__}")
    if not parsed['goal_satisfied_rationale'].strip():
        raise GeminiOutputParseError("'goal_satisfied_rationale' must be a non-empty string (prompt contract)")
    for i, item in enumerate(parsed['blocking_objections']):
        if not isinstance(item, str):
            raise GeminiOutputParseError(f'blocking_objections[{i}] must be string, got {type(item).__name__}')
    blocking_finding_ids = []
    for i, finding in enumerate(parsed['findings']):
        if not isinstance(finding, dict):
            raise GeminiOutputParseError(f'findings[{i}] must be an object, got {type(finding).__name__}')
        unknown_keys = set(finding.keys()) - _ALLOWED_FINDING_KEYS
        if unknown_keys:
            raise GeminiOutputParseError(f'findings[{i}] has unexpected keys: {sorted(unknown_keys)}')
        for required in _REQUIRED_FINDING_FIELDS:
            if required not in finding:
                raise GeminiOutputParseError(f'findings[{i}] missing required field: {required!r}')
        for str_field in ('id', 'severity', 'summary', 'citation', 'risk', 'recommendation'):
            if not isinstance(finding[str_field], str):
                raise GeminiOutputParseError(f'findings[{i}].{str_field} must be string, got {type(finding[str_field]).__name__}')
        if not finding['summary'].strip():
            raise GeminiOutputParseError(f'findings[{i}].summary must be a non-empty string (schema contract)')
        if finding['severity'] not in _VALID_SEVERITIES:
            raise GeminiOutputParseError(f"findings[{i}] has invalid severity {finding['severity']!r}; must be one of {sorted(_VALID_SEVERITIES)}")
        if not _GEMINI_FINDING_ID_PATTERN.match(finding['id']):
            raise GeminiOutputParseError(f"findings[{i}] id {finding['id']!r} does not match pattern ^gemini-rev-\\d+$")
        pp = finding.get('patch_proposal')
        if pp is not None:
            raise GeminiOutputParseError(f'findings[{i}].patch_proposal must be null in v1.14.0 - gemini is review-only (patch authoring deferred to iter-0013)')
        reason = finding.get('patch_not_proposed_reason')
        if reason is not None and (not isinstance(reason, str)):
            raise GeminiOutputParseError(f'findings[{i}].patch_not_proposed_reason must be string or null, got {type(reason).__name__}')
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
        raise GeminiOutputParseError('; '.join(msg_parts))
    if parsed['goal_satisfied'] is True and actual_blocking:
        raise GeminiOutputParseError(f'goal_satisfied=true is incoherent with non-empty blocking_objections {sorted(actual_blocking)}; a successful review cannot have blocking findings')
    return parsed
_GEMINI_PROPOSAL_SCHEMA_PATH = Path(__file__).parent / 'dispatch_templates' / 'gemini_proposal_schema.json'

def _parse_gemini_proposal_output(text: str, schema_path: Path | None=None) -> dict:
    parsed = _load_review_object(text, 'gemini proposal output')
    if not isinstance(parsed, dict):
        raise GeminiOutputParseError(f'gemini proposal output root must be a JSON object; got {type(parsed).__name__}')
    effective_schema_path = schema_path or _GEMINI_PROPOSAL_SCHEMA_PATH
    try:
        import jsonschema
    except ImportError as exc:
        raise GeminiOutputParseError(f'jsonschema package required for proposal-mode validation; install with `pip install jsonschema` or reinstall consensus-mcp: {exc}') from exc
    try:
        schema = json.loads(Path(effective_schema_path).read_text(encoding='utf-8'))
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise GeminiOutputParseError(f"gemini proposal output failed schema validation at {'/'.join((str(p) for p in exc.absolute_path)) or '<root>'}: {exc.message}") from exc
    except FileNotFoundError as exc:
        raise GeminiOutputParseError(f'proposal schema not found at {effective_schema_path}: {exc}') from exc
    if not isinstance(
        parsed.get('rationale_vs_alternatives'),
        str,
    ) or not parsed['rationale_vs_alternatives'].strip():
        raise GeminiOutputParseError('rationale_vs_alternatives must be a non-empty string (parser invariant)')
    if 'structural_abstention' not in parsed or not isinstance(parsed['structural_abstention'], bool):
        raise GeminiOutputParseError('structural_abstention must be present and boolean (parser invariant)')
    if not parsed['structural_abstention']:
        if parsed['selected_target'] is None:
            raise GeminiOutputParseError('selected_target is required when structural_abstention is false')
        if parsed['deliverable_scope'] is None:
            raise GeminiOutputParseError('deliverable_scope is required when structural_abstention is false')
    return parsed

def _invoke_gemini_with_retry(
    prompt: str,
    gemini_bin: str,
    model: str,
    timeout_seconds: int,
    repo_root: Path,
    stall_silence_seconds: float = 180.0,
    goal_packet: dict | None = None,
    log_path = None,
    anchors = None,
    mode: str = 'review',
    proposal_schema_path: Path | None = None,
    approval_mode: str = 'plan',
    skip_permissions: bool = True,
) -> tuple[str, dict]:
    def invoke(current, suffix):
        return _invoke_gemini(prompt=current, gemini_bin=gemini_bin, model=model,
                timeout_seconds=timeout_seconds, repo_root=repo_root,
                stall_silence_seconds=stall_silence_seconds, log_path=log_path, anchors=anchors,
                approval_mode=approval_mode, skip_permissions=skip_permissions)
    def parse(raw):
        return (_parse_gemini_proposal_output(raw, schema_path=proposal_schema_path)
                if mode == "proposal" else _parse_gemini_output(raw, goal_packet=goal_packet))
    return invoke_and_parse(invoke, parse, prompt, log_path=log_path, anchors=anchors)


def _agy_permissions(ns):
    """CLI flag > profile/project config > safe default (plan + skip prompts)."""
    profile = getattr(ns, 'profile_data', None) or {}
    mode = getattr(ns, 'approval_mode', None) or profile.get('approval_mode') or 'plan'
    skip = getattr(ns, 'skip_permissions', None)
    if skip is None:
        skip = profile.get('skip_permissions', True)
    return mode, bool(skip)


def _provider_invoke(*, prompt, model, repo_root, schema_path, ns, log_path, anchors):
    approval_mode, skip_permissions = _agy_permissions(ns)
    try:
        binary = _resolve_gemini_bin(ns.gemini_bin)
    except (OSError, DispatchError):
        binary = None  # _invoke_gemini reports the missing binary itself
    if binary and Path(binary).name.lower() in {'agy', 'agy.exe'}:
        model = _resolve_agy_model(binary, model)
    raw, _ = _invoke_gemini_with_retry(
        approval_mode=approval_mode,
        skip_permissions=skip_permissions,
        prompt=prompt,
        gemini_bin=ns.gemini_bin,
        model=model,
        timeout_seconds=ns.timeout_seconds,
        repo_root=repo_root,
        stall_silence_seconds=ns.stall_silence_seconds,
        goal_packet=ns.goal_packet_data,
        log_path=log_path,
        anchors=anchors,
        mode=ns.mode,
        proposal_schema_path=schema_path,
    )
    return raw

def _provider_parse(raw, *, goal_packet, review_packet, repo_root, ns, schema_path):
    if ns.mode == 'proposal':
        return _parse_gemini_proposal_output(raw, schema_path=schema_path)
    return _parse_gemini_output(raw, goal_packet=goal_packet)

def _configure_parser(parser):
    parser.add_argument('--approval-mode', choices=sorted(_AGY_MODES), default=None,
                        help="agy --mode for the reviewer (default: profile approval_mode, else 'plan')")
    parser.add_argument('--skip-permissions', dest='skip_permissions', action='store_true', default=None,
                        help='auto-approve agy tool prompts (only applied in plan mode; default on)')
    parser.add_argument('--no-skip-permissions', dest='skip_permissions', action='store_false')

def main(argv=None):
    return main_for_provider(
        ProviderSpec(
            name='gemini',
            invoke=_provider_invoke,
            parse=_provider_parse,
            default_model=_DEFAULT_GEMINI_MODEL,
            default_bin='agy',
            template_name='gemini',
            configure_parser=_configure_parser,
            disposable=False,
            namespace=sys.modules[__name__],
        ),
        argv,
    )

if __name__ == '__main__':
    sys.exit(main())
