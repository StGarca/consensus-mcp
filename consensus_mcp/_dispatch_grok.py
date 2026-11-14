"""Grok provider quirks over the shared dispatch runner."""
from __future__ import annotations
from consensus_mcp._dispatch_base import drop_echoed_envelope_keys
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
    GROK_SCRUBBED_ENV_KEYS,
)

def _grok_subprocess_env() -> dict:
    env = subprocess_env()
    return env
_GROK_FINDING_ID_PATTERN = re.compile('^grok-rev-\\d+$')
_VALID_SEVERITIES = {'low', 'medium', 'high', 'blocking', 'critical'}
_REQUIRED_FINDING_FIELDS = ('id', 'severity', 'summary', 'citation', 'risk', 'recommendation')
_ALLOWED_FINDING_KEYS = set(_REQUIRED_FINDING_FIELDS) | {'patch_proposal', 'patch_not_proposed_reason'}
_ALLOWED_TOP_LEVEL_KEYS = {'findings', 'goal_satisfied', 'blocking_objections', 'goal_satisfied_rationale'}
_BLOCKING_SEVERITIES = {'blocking', 'critical'}
_DEFAULT_GROK_MODEL = load_builtin_profiles()['grok']['model']
_DEFAULT_GROK_EFFORT = 'medium'
_GROK_MODEL_ALIASES = {'Grok Build': _DEFAULT_GROK_MODEL}

def _resolve_grok_model(model: str | None) -> str | None:
    return _GROK_MODEL_ALIASES.get(model, model)
_GROK_DISABLED_TOOLS = ('--no-memory', '--disable-web-search')
_GROK_INLINE_PROMPT_MAX_BYTES = 28 * 1024 if sys.platform == 'win32' else 96 * 1024

class GrokAuthRequiredError(DispatchError):
    pass

class GrokInvocationError(DispatchError):
    pass

class GrokEmptyOutputError(GrokInvocationError):
    pass

class GrokStreamCancelledError(GrokInvocationError):
    pass

class GrokOutputParseError(ValueError):
    pass

def _check_grok_auth() -> None:
    auth_path = Path.home() / '.grok' / 'auth.json'
    if not auth_path.exists():
        raise GrokAuthRequiredError(f'no grok authentication found at {auth_path}. Run `grok login` to authenticate, then re-run.')

def _resolve_grok_bin(grok_bin: str) -> str:
    if os.path.sep in grok_bin or (len(grok_bin) > 1 and grok_bin[1] == ':'):
        return grok_bin
    resolved = shutil.which(grok_bin)
    if resolved is not None:
        return resolved
    found = _search_extra_bin_dirs(grok_bin)
    return found if found is not None else grok_bin

def _search_extra_bin_dirs(name: str) -> str | None:
    raw = os.environ.get('CONSENSUS_MCP_BIN_DIRS', '')
    for d in raw.split(os.pathsep):
        if not d:
            continue
        cand = shutil.which(name, path=d)
        if cand is not None:
            return cand
    return None

def _get_grok_version(grok_bin: str) -> str:
    try:
        result = subprocess.run(
            [_resolve_grok_bin(grok_bin), '--version'],
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

def _write_per_pass_prompt(prompt: str, iter_dir: Path, pass_id: str) -> Path:
    safe_pass_id = re.sub('[^A-Za-z0-9._-]+', '_', pass_id)
    prompt_path = iter_dir / f'grok-prompt-{safe_pass_id}.txt'
    prompt_path.write_text(prompt, encoding='utf-8')
    return prompt_path

def _build_grok_cmd(
    grok_bin: str,
    prompt: str,
    model: str | None,
    run_cwd: str = '/tmp',
    prompt_file: 'Path | None' = None,
    effort: str | None = _DEFAULT_GROK_EFFORT,
) -> list[str]:
    too_large = len(prompt.encode('utf-8')) > _GROK_INLINE_PROMPT_MAX_BYTES
    if too_large and prompt_file is not None:
        cmd = [_resolve_grok_bin(grok_bin), '--prompt-file', str(prompt_file), '--output-format', 'streaming-json']
    else:
        cmd = [_resolve_grok_bin(grok_bin), '-p', prompt, '--output-format', 'streaming-json']
    cmd.extend(_GROK_DISABLED_TOOLS)
    cmd.append('--always-approve')
    cmd.extend(['--cwd', run_cwd])
    if model:
        cmd.extend(['--model', _resolve_grok_model(model)])
    if effort:
        cmd.extend(['--effort', effort])
    return cmd

def _invoke_grok(
    prompt,
    grok_bin,
    model,
    timeout_seconds,
    iter_dir,
    pass_id,
    repo_root,
    effort = _DEFAULT_GROK_EFFORT,
    log_path = None,
    anchors = None,
    heartbeat_interval = 30.0,
    stall_silence_seconds = 180.0,
    poll_interval = 0.5,
    time_fn = None,
    popen_factory = None,
    _sleep = None,
):
    from consensus_mcp._dispatch_runner import make_disposable_workdir, cleanup_disposable_workdir, repo_status_snapshot, rewrite_prompt_paths
    before = None
    workdir = None
    try:
        workdir = make_disposable_workdir(repo_root)
        prompt = rewrite_prompt_paths(prompt, repo_root, workdir)
        _write_per_pass_prompt(prompt, iter_dir, pass_id)
        before = repo_status_snapshot(repo_root)
        return _invoke_grok_in_cwd(
            str(workdir),
            prompt=rewrite_prompt_paths(prompt, repo_root, workdir),
            grok_bin=grok_bin,
            model=model,
            timeout_seconds=timeout_seconds,
            iter_dir=iter_dir,
            pass_id=pass_id,
            repo_root=repo_root,
            effort=effort,
            log_path=log_path,
            anchors=anchors,
            heartbeat_interval=heartbeat_interval,
            stall_silence_seconds=stall_silence_seconds,
            poll_interval=poll_interval,
            time_fn=time_fn,
            popen_factory=popen_factory,
            _sleep=_sleep,
        )
    finally:
        cleanup_disposable_workdir(workdir)
        after = repo_status_snapshot(repo_root) if before is not None else {}
        before = before or {}
        changed = sorted((p for p in set(before) | set(after) if before.get(p) != after.get(p)))
        if changed:
            raise DispatchError(
                'grok integrity check failed: real repo mutated: ' + ', '.join(changed),
                'integrity',
            )

def _invoke_grok_in_cwd(
    grok_run_cwd,
    *,
    prompt,
    grok_bin,
    model,
    timeout_seconds,
    iter_dir,
    pass_id,
    repo_root,
    effort = _DEFAULT_GROK_EFFORT,
    log_path = None,
    anchors = None,
    heartbeat_interval = 30.0,
    stall_silence_seconds = 180.0,
    poll_interval = 0.5,
    time_fn = None,
    popen_factory = None,
    _sleep = None,
):
    _check_grok_auth()
    prompt_path = _write_per_pass_prompt(prompt, iter_dir, pass_id)
    argv = _build_grok_cmd(
        grok_bin,
        prompt,
        model,
        run_cwd=grok_run_cwd,
        prompt_file=prompt_path,
        effort=effort,
    )
    try:
        raw = run_process(
            argv,
            cwd=Path(grok_run_cwd),
            env=_grok_subprocess_env(),
            timeout_seconds=timeout_seconds,
            stall_silence_seconds=stall_silence_seconds,
            log_path=log_path,
            anchors=anchors,
            heartbeat_interval=heartbeat_interval,
            poll_interval=poll_interval,
            time_fn=time_fn,
            popen_factory=popen_factory,
            _sleep=_sleep,
            abort_root=repo_root,
            startup_silence_seconds=timeout_seconds,
        )
    except DispatchError as exc:
        raise GrokInvocationError(str(exc), exc.failure_class) from exc
    return (_assemble_grok_stream(raw), prompt_path)

def _assemble_grok_stream(raw: str) -> str:
    if not raw.strip():
        raise GrokEmptyOutputError('grok exited successfully but emitted empty stdout; retry the dispatch with --debug and inspect the Grok CLI stderr/debug log')
    text_parts: list[str] = []
    thought_parts: list[str] = []
    thought_event_count = 0
    saw_event = False
    saw_text = False
    stop_reason: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(evt, dict) or 'type' not in evt:
            continue
        saw_event = True
        etype = evt.get('type')
        if etype == 'thought':
            thought_event_count += 1
            payload = evt.get('data')
            if payload is None:
                payload = evt.get('text')
            if isinstance(payload, str):
                thought_parts.append(payload)
        elif etype == 'text':
            payload = evt.get('data')
            if payload is None:
                payload = evt.get('text')
            if isinstance(payload, str):
                text_parts.append(payload)
                saw_text = True
        elif etype == 'end':
            stop_reason = evt.get('stopReason') or evt.get('stop_reason')
            break
    if not saw_event:
        return raw

    def recover_object(payload: str, channel: str) -> tuple[str | None, str]:
        if not payload:
            return (None, f'no string {channel} payload was emitted')
        try:
            candidate = _extract_json_from_text(payload)
            parsed_candidate = json.loads(candidate)
        except (ValueError, json.JSONDecodeError) as exc:
            return (None, f'{channel} payload had no valid JSON object ({exc})')
        if not isinstance(parsed_candidate, dict):
            return (None, f'{channel} payload JSON root was {type(parsed_candidate).__name__}, not object')
        return (candidate, 'recovered JSON object')
    answer = ''.join(text_parts)
    thought_text = ''.join(thought_parts)
    if stop_reason == 'Cancelled':
        text_candidate, text_detail = recover_object(answer, 'text')
        if text_candidate is not None:
            return text_candidate
        thought_candidate, thought_detail = recover_object(thought_text, 'thought')
        if thought_candidate is not None:
            return thought_candidate
        raise GrokStreamCancelledError(f"grok self-cancelled before emitting a complete JSON object; text_events={len(text_parts)}; text_bytes={len(answer.encode('utf-8'))}; thought_events={thought_event_count}; thought_bytes={len(thought_text.encode('utf-8'))}; text_recovery={text_detail}; thought_recovery={thought_detail}. Re-dispatch with a shorter, single-focus prompt.")
    if not saw_text:
        candidate, recovery_detail = recover_object(thought_text, 'thought')
        if candidate is not None:
            return candidate
        thought_bytes = len(thought_text.encode('utf-8'))
        message = f'grok streaming output contained no string text events and no recoverable answer; stopReason={stop_reason!r}; thought_events={thought_event_count}; thought_bytes={thought_bytes}; {recovery_detail}. Re-dispatch with --debug and a shorter, single-focus prompt.'
        raise GrokEmptyOutputError(message)
    if not answer.strip():
        raise GrokEmptyOutputError(f'grok streaming output emitted string text events but their assembled answer was empty; stopReason={stop_reason!r}; text_events={len(text_parts)}. Re-dispatch with --debug.')
    return answer

def _truncate_middle(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ''
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
    marker = '\n\n[... truncated for Grok cancel-retry ...]\n\n'
    marker_b = marker.encode('utf-8')
    keep = max(0, max_bytes - len(marker_b))
    head_b = encoded[:keep // 2]
    tail_b = encoded[-(keep - keep // 2):] if keep else b''
    return head_b.decode('utf-8', errors='ignore') + marker + tail_b.decode('utf-8', errors='ignore')

def _build_grok_cancel_retry_prompt(
    *,
    goal_packet: dict | None,
    review_target_text: str | None,
    review_target_path: str | None,
    review_target_hash: str | None,
    schema_text: str | None,
) -> str:
    goal_packet = goal_packet or {}
    goal = goal_packet.get('goal', {}) or {}
    auth = goal_packet.get('authorization', {}) or {}
    compact = {'goal_summary': goal.get('summary', ''), 'desired_end_state': goal.get('desired_end_state', ''), 'allowed_files': goal_packet.get('allowed_files', []) or [], 'acceptance_gates': goal_packet.get('acceptance_gates', []) or [], 'scope_signature': auth.get('scope_signature', ''), 'review_target_path': review_target_path, 'review_target_hash': review_target_hash}
    schema = schema_text or '{"selected_target": str|null, "rationale_vs_alternatives": str, "deliverable_scope": object|null, "risks": [str], "estimated_complexity": "small|medium|large", "structural_abstention": bool}'
    target_budget = min(18 * 1024, max(4 * 1024, _GROK_INLINE_PROMPT_MAX_BYTES - 8 * 1024))
    target_excerpt = _truncate_middle(review_target_text or '(not provided)', target_budget)
    prompt = f'You are Grok 4.5 acting as one independent contributor in a consensus-mcp design consult.\nYour previous full proposal prompt self-cancelled before producing text. Retry with this shorter single-focus prompt.\n\nTask: produce ONE design proposal. This is not a code review.\nReturn ONLY valid JSON. No markdown. No prose outside JSON.\n\nGoal packet summary:\n{json.dumps(compact, indent=2, ensure_ascii=False)}\n\nRequired JSON schema/shape:\n{schema}\n\nRules:\n- Pick exactly one selected_target unless structural_abstention is true.\n- deliverable_scope must include next_iteration_id, files_in_scope, files_out_of_scope, key_design_decisions, acceptance_gates unless abstaining.\n- Be concrete and concise. Prefer completion in the next iteration when scope is small and gates are clear.\n- If context is insufficient, set structural_abstention true and explain why.\n\nReview target content, truncated if needed:\n```yaml\n{target_excerpt}\n```\n'
    return _truncate_middle(prompt, min(24 * 1024, _GROK_INLINE_PROMPT_MAX_BYTES - 1024))

def _parse_grok_output(text: str, goal_packet: dict | None=None) -> dict:
    candidate = _extract_json_from_text(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise GrokOutputParseError(f'grok output is not valid JSON: {exc}; first 500 chars of raw: {text[:500]!r}') from exc
    if not isinstance(parsed, dict):
        raise GrokOutputParseError(f'grok output JSON root must be an object, got {type(parsed).__name__}')
    drop_echoed_envelope_keys(parsed)
    unknown_top = set(parsed.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_top:
        raise GrokOutputParseError(f'grok output JSON has unexpected top-level keys: {sorted(unknown_top)}')
    for required in ('findings', 'goal_satisfied', 'blocking_objections', 'goal_satisfied_rationale'):
        if required not in parsed:
            raise GrokOutputParseError(f'grok output JSON missing required key: {required!r}')
    if not isinstance(parsed['findings'], list):
        raise GrokOutputParseError("'findings' must be an array")
    if not isinstance(parsed['goal_satisfied'], bool):
        raise GrokOutputParseError(f"'goal_satisfied' must be boolean, got {type(parsed['goal_satisfied']).__name__}")
    if not isinstance(parsed['blocking_objections'], list):
        raise GrokOutputParseError(f"'blocking_objections' must be an array, got {type(parsed['blocking_objections']).__name__}")
    if not isinstance(parsed['goal_satisfied_rationale'], str):
        raise GrokOutputParseError(f"'goal_satisfied_rationale' must be string, got {type(parsed['goal_satisfied_rationale']).__name__}")
    if not parsed['goal_satisfied_rationale'].strip():
        raise GrokOutputParseError("'goal_satisfied_rationale' must be a non-empty string (prompt contract)")
    for i, item in enumerate(parsed['blocking_objections']):
        if not isinstance(item, str):
            raise GrokOutputParseError(f'blocking_objections[{i}] must be string, got {type(item).__name__}')
    blocking_finding_ids = []
    for i, finding in enumerate(parsed['findings']):
        if not isinstance(finding, dict):
            raise GrokOutputParseError(f'findings[{i}] must be an object, got {type(finding).__name__}')
        unknown_keys = set(finding.keys()) - _ALLOWED_FINDING_KEYS
        if unknown_keys:
            raise GrokOutputParseError(f'findings[{i}] has unexpected keys: {sorted(unknown_keys)}')
        for required in _REQUIRED_FINDING_FIELDS:
            if required not in finding:
                raise GrokOutputParseError(f'findings[{i}] missing required field: {required!r}')
        for str_field in ('id', 'severity', 'summary', 'citation', 'risk', 'recommendation'):
            if not isinstance(finding[str_field], str):
                raise GrokOutputParseError(f'findings[{i}].{str_field} must be string, got {type(finding[str_field]).__name__}')
        if not finding['summary'].strip():
            raise GrokOutputParseError(f'findings[{i}].summary must be a non-empty string (schema contract)')
        if finding['severity'] not in _VALID_SEVERITIES:
            raise GrokOutputParseError(f"findings[{i}] has invalid severity {finding['severity']!r}; must be one of {sorted(_VALID_SEVERITIES)}")
        if not _GROK_FINDING_ID_PATTERN.match(finding['id']):
            raise GrokOutputParseError(f"findings[{i}] id {finding['id']!r} does not match pattern ^grok-rev-\\d+$")
        pp = finding.get('patch_proposal')
        if pp is not None:
            raise GrokOutputParseError(f'findings[{i}].patch_proposal must be null in v1.31.0 - grok is review-only (patch authoring deferred)')
        reason = finding.get('patch_not_proposed_reason')
        if reason is not None and (not isinstance(reason, str)):
            raise GrokOutputParseError(f'findings[{i}].patch_not_proposed_reason must be string or null, got {type(reason).__name__}')
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
        raise GrokOutputParseError('; '.join(msg_parts))
    if parsed['goal_satisfied'] is True and actual_blocking:
        raise GrokOutputParseError(f'goal_satisfied=true is incoherent with non-empty blocking_objections {sorted(actual_blocking)}; a successful review cannot have blocking findings')
    return parsed
_GROK_PROPOSAL_SCHEMA_PATH = Path(__file__).parent / 'dispatch_templates' / 'grok_proposal_schema.json'

def _parse_grok_proposal_output(text: str, schema_path: Path | None=None) -> dict:
    try:
        cleaned = _extract_json_from_text(text)
    except ValueError as exc:
        raise GrokOutputParseError(f'grok proposal output: could not extract JSON: {exc}') from exc
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GrokOutputParseError(f'grok proposal output is not valid JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise GrokOutputParseError(f'grok proposal output root must be a JSON object; got {type(parsed).__name__}')
    effective_schema_path = schema_path or _GROK_PROPOSAL_SCHEMA_PATH
    try:
        import jsonschema
    except ImportError as exc:
        raise GrokOutputParseError(f'jsonschema package required for proposal-mode validation; install with `pip install jsonschema` or reinstall consensus-mcp: {exc}') from exc
    try:
        schema = json.loads(Path(effective_schema_path).read_text(encoding='utf-8'))
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise GrokOutputParseError(f"grok proposal output failed schema validation at {'/'.join((str(p) for p in exc.absolute_path)) or '<root>'}: {exc.message}") from exc
    except FileNotFoundError as exc:
        raise GrokOutputParseError(f'proposal schema not found at {effective_schema_path}: {exc}') from exc
    if not isinstance(
        parsed.get('rationale_vs_alternatives'),
        str,
    ) or not parsed['rationale_vs_alternatives'].strip():
        raise GrokOutputParseError('rationale_vs_alternatives must be a non-empty string (parser invariant)')
    if 'structural_abstention' not in parsed or not isinstance(parsed['structural_abstention'], bool):
        raise GrokOutputParseError('structural_abstention must be present and boolean (parser invariant)')
    if not parsed['structural_abstention']:
        if parsed['selected_target'] is None:
            raise GrokOutputParseError('selected_target is required when structural_abstention is false')
        if parsed['deliverable_scope'] is None:
            raise GrokOutputParseError('deliverable_scope is required when structural_abstention is false')
    return parsed

def _build_grok_json_retry_prompt(prompt: str, error: Exception) -> str:
    return prompt + '\n\n# Retry - your previous response produced no valid JSON answer\n\n' + f'Output error: {error}\n\n' + 'Re-emit ONLY valid JSON conforming to the schema in the prompt above. ' + 'No prose, no markdown fences, no commentary - JSON only, starting with `{` ' + 'and ending with `}`.'

def _invoke_grok_with_retry(
    prompt: str,
    grok_bin: str,
    model: str | None,
    timeout_seconds: int,
    iter_dir: Path,
    pass_id: str,
    repo_root: Path,
    effort: str | None = _DEFAULT_GROK_EFFORT,
    stall_silence_seconds: float = 180.0,
    goal_packet: dict | None = None,
    log_path = None,
    anchors = None,
    mode: str = 'review',
    proposal_schema_path: Path | None = None,
    cancel_retry_prompt: str | None = None,
) -> tuple[str, dict, Path]:
    def invoke(current, suffix):
        return _invoke_grok(prompt=current, grok_bin=grok_bin, model=model, effort=effort,
                timeout_seconds=timeout_seconds, iter_dir=iter_dir, pass_id=pass_id + suffix,
                repo_root=repo_root, stall_silence_seconds=stall_silence_seconds,
                log_path=log_path, anchors=anchors)
    def parse(result):
        raw, _ = result
        return (_parse_grok_proposal_output(raw, schema_path=proposal_schema_path)
                if mode == "proposal" else _parse_grok_output(raw, goal_packet=goal_packet))
    def recovery(exc):
        if isinstance(exc, GrokStreamCancelledError) and mode == "proposal" and cancel_retry_prompt:
            return cancel_retry_prompt, "-cancel-retry", "dispatch_retry_for_grok_cancel"
        if isinstance(exc, GrokEmptyOutputError):
            return _build_grok_json_retry_prompt(
                prompt,
                exc,
            ), "-empty-retry", "dispatch_retry_for_grok_empty_output"
        return None
    (raw, path), parsed = invoke_and_parse(invoke, parse, prompt, recovery=recovery,
                                          log_path=log_path, anchors=anchors)
    return raw, parsed, path


def _provider_invoke(*, prompt, model, repo_root, schema_path, ns, log_path, anchors):
    raw, _, _ = _invoke_grok_with_retry(
        prompt=prompt,
        grok_bin=ns.grok_bin,
        model=model,
        effort=ns.effort,
        timeout_seconds=ns.timeout_seconds,
        iter_dir=ns.iter_dir,
        pass_id=ns.pass_id,
        repo_root=repo_root,
        stall_silence_seconds=ns.stall_silence_seconds,
        goal_packet=ns.goal_packet_data,
        log_path=log_path,
        anchors=anchors,
        mode=ns.mode,
        proposal_schema_path=schema_path,
        cancel_retry_prompt=_build_grok_cancel_retry_prompt(
            goal_packet=ns.goal_packet_data,
            review_target_text=None,
            review_target_path=None,
            review_target_hash=None,
            schema_text=schema_path.read_text(encoding='utf-8'),
        ) if ns.mode == 'proposal' else None,
    )
    return raw

def _provider_parse(raw, *, goal_packet, review_packet, repo_root, ns, schema_path):
    if ns.mode == 'proposal':
        return _parse_grok_proposal_output(raw, schema_path=schema_path)
    return _parse_grok_output(raw, goal_packet=goal_packet)

def _configure_parser(parser):
    parser.add_argument('--effort', default=_DEFAULT_GROK_EFFORT)

def main(argv=None):
    return main_for_provider(
        ProviderSpec(
            name='grok',
            invoke=_provider_invoke,
            parse=_provider_parse,
            default_model=_DEFAULT_GROK_MODEL,
            default_bin='grok',
            template_name='grok',
            configure_parser=_configure_parser,
            disposable=False,
            namespace=sys.modules[__name__],
        ),
        argv,
    )

if __name__ == '__main__':
    sys.exit(main())
