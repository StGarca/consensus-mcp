"""Fresh-context Claude CLI reviewer; the host callback remains independent."""
from __future__ import annotations

import json
import sys

from consensus_mcp._dispatch_openai_compat import parse_json_output, prepare_template, profile_from_argv
from consensus_mcp.contributors.base import DispatchError


def _invoke_claude(*, prompt, model, repo_root, schema_path, ns, log_path, anchors):
    from consensus_mcp._dispatch_runner import run_process
    # `claude -p --output-format json` prints one envelope only when it
    # finishes, so a silence watchdog would kill every healthy review; the
    # run timeout bounds it instead (same as agy).
    stall = max(float(ns.stall_silence_seconds), float(ns.timeout_seconds))
    try:
        return run_process([ns.claude_bin, "-p", "--model", model, "--output-format", "json",
                            "--allowedTools", "Read,Grep,Glob"],
                           prompt=prompt, cwd=repo_root, timeout_seconds=ns.timeout_seconds,
                           stall_silence_seconds=stall,
                           log_path=log_path, anchors=anchors)
    except DispatchError as exc:
        # Claude reports API failures as JSON even when the process exits 1.
        raw = getattr(exc, "stdout", "")
        if raw:
            try:
                envelope = json.loads(raw)
            except ValueError:
                raise exc
            if isinstance(envelope, dict) and envelope.get("is_error"):
                _parse_claude(raw, None, None, repo_root, ns, schema_path)
        raise


def _parse_claude(raw, goal_packet, review_packet, repo_root, ns, schema_path):
    from consensus_mcp._dispatch_runner import classify_failure
    try:
        envelope = json.loads(raw)
    except ValueError as exc:
        raise DispatchError(f"Claude CLI envelope JSON parse failed: {exc}", failure_class="parse") from exc
    if not isinstance(envelope, dict):
        raise DispatchError("Claude CLI envelope must be an object", failure_class="parse")
    result = envelope.get("result", "")
    if envelope.get("is_error"):
        message = f"Claude CLI API {envelope.get('api_error_status')}: {result}"
        if envelope.get("api_error_status") in (401, 403):
            failure = "auth"
        elif envelope.get("api_error_status") == 429 and "limit" in str(result).lower():
            failure = "quota"
        else:
            failure = classify_failure(RuntimeError(message),
                quota_signatures=(getattr(ns, "profile_data", {}) or {}).get("quota_signatures", ()))
        raise DispatchError(message, failure_class=failure)
    if not isinstance(result, str):
        raise DispatchError("Claude CLI result must contain review JSON text", failure_class="parse")
    return parse_json_output(result, schema_path, None if getattr(ns, "schema", None) else ns.profile_data)


def _configure_parser(parser):
    import argparse
    parser.add_argument("--profile-json", help=argparse.SUPPRESS)


def main(argv=None):
    from consensus_mcp._dispatch_runner import ProviderSpec, main_for_provider
    argv = sys.argv[1:] if argv is None else argv
    return main_for_provider(ProviderSpec(name="claude", invoke=_invoke_claude, parse=_parse_claude,
        default_model="claude-fable-5-2", default_bin="claude", template_name="gemini",
        configure_parser=_configure_parser, disposable=True, profile=profile_from_argv(argv),
        prepare_template=prepare_template), argv)


if __name__ == "__main__":
    raise SystemExit(main())
