"""OpenAI-compatible HTTP reviewer; transport uses only the standard library."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
from urllib import error, request

import jsonschema

from consensus_mcp.contributors.base import DispatchError


@contextmanager
def http_slot(state_root: Path, base_url: str, maximum: int, timeout: float):
    """Lease one OS-locked slot, shared by threads and dispatcher processes."""
    directory = Path(state_root) / "http-slots" / hashlib.sha256(base_url.encode()).hexdigest()[:16]
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    lease = None
    while lease is None:
        for index in range(maximum):
            handle = (directory / f"{index}.lock").open("a+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0, 2)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                continue
            lease = handle
            break
        if lease is None:
            if time.monotonic() >= deadline:
                raise DispatchError("HTTP concurrency slot timed out", failure_class="timeout")
            time.sleep(min(.05, max(0, deadline - time.monotonic())))
    try:
        yield max(.001, deadline - time.monotonic())
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                lease.seek(0)
                msvcrt.locking(lease.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
        finally:
            lease.close()


def review_schema(schema_path, profile=None):
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    builtin = Path(__file__).parent / "dispatch_templates"
    if profile and Path(schema_path).parent == builtin and Path(schema_path).name.startswith("gemini_"):
        prefix = profile.get("id_prefix") or f"{profile['name']}-rev"
        schema = json.loads(json.dumps(schema).replace("gemini-rev", prefix))
    return schema


def parse_json_output(raw: str, schema_path: Path, profile=None) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.partition("\n")[2].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
        schema = review_schema(schema_path, profile)
        jsonschema.validate(parsed, schema)
        if "goal_satisfied" in parsed:
            findings = parsed.get("findings", [])
            blocking = {finding["id"] for finding in findings
                        if finding.get("severity") in ("blocking", "critical")}
            objections = parsed.get("blocking_objections", [])
            if len(objections) != len(set(objections)) or set(objections) != blocking:
                raise ValueError("blocking_objections must contain exactly the blocking/critical finding IDs")
            if parsed["goal_satisfied"] and blocking:
                raise ValueError("goal_satisfied=true is incoherent with blocking findings")
            if len({finding["id"] for finding in findings}) != len(findings):
                raise ValueError("finding IDs must be unique")
            if any(finding.get("patch_proposal") is not None for finding in findings):
                raise ValueError("this reviewer is review-only; patch_proposal must be null")
    except (ValueError, OSError, KeyError, TypeError, AttributeError,
            jsonschema.ValidationError, jsonschema.SchemaError) as exc:
        raise DispatchError(f"review JSON/schema validation failed: {exc}", failure_class="parse") from exc
    return parsed


def invoke_http(profile: dict, prompt: str, *, state_root: Path, schema_path: Path,
                model: str | None = None, timeout_seconds: float | None = None,
                schema_overridden: bool = False) -> str:
    from consensus_mcp._dispatch_runner import classify_failure

    base_url = profile.get("base_url") or os.environ.get(profile.get("base_url_env") or "", "")
    if not base_url:
        raise DispatchError("HTTP reviewer requires base_url or its configured base_url_env", failure_class="auth")
    timeout = float(timeout_seconds or profile.get("timeout_seconds", 600))
    maximum = int(profile.get("max_concurrency", 4))
    if maximum < 1:
        raise DispatchError("HTTP max_concurrency must be positive", failure_class="auth")
    schema_profile = None if schema_overridden else profile
    schema = review_schema(schema_path, schema_profile)
    body = {"model": model or profile["model"], "messages": [{"role": "user", "content": prompt}],
            "max_tokens": profile.get("max_tokens", 8192)}
    # Structured-output ladder: strict JSON Schema -> JSON mode -> plain text.
    # Some engines (e.g. some self-hosted vLLM builds) reject every
    # response_format; profiles mark that with output.schema_enforced: false.
    # The identical schema is always enforced locally by parse_json_output.
    if (profile.get("output") or {}).get("schema_enforced") is False:
        formats = [None]
    else:
        formats = [{"type": "json_schema", "json_schema": {"name": "review", "strict": True, "schema": schema}},
                   {"type": "json_object"}, None]
    if formats[0] is not None:
        body["response_format"] = formats[0]
    if profile.get("reasoning_effort"):
        body["reasoning_effort"] = profile["reasoning_effort"]
    headers = {"Content-Type": "application/json"}
    key_env = profile.get("api_key_env")
    if key_env:
        key = os.environ.get(key_env)
        if not key:
            raise DispatchError(f"HTTP API key environment variable {key_env} is unset", failure_class="auth")
        headers["Authorization"] = f"Bearer {key}"
    with http_slot(state_root, base_url, maximum, timeout) as remaining:
        deadline = time.monotonic() + remaining
        for schema_attempt in range(len(formats)):
            req = request.Request(base_url.rstrip("/") + "/chat/completions",
                                  data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
            try:
                with request.urlopen(req, timeout=max(.001, deadline - time.monotonic())) as response:
                    envelope = json.loads(response.read().decode("utf-8"))
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                # Server rejected this structured-output mode: step down the
                # ladder; the schema is still enforced locally on the result.
                if schema_attempt + 1 < len(formats) and exc.code in (400, 422) and any(
                        marker in detail.lower() for marker in ("response_format", "json_schema", "guided", "structured output")):
                    nxt = formats[schema_attempt + 1]
                    if nxt is None:
                        body.pop("response_format", None)
                    else:
                        body["response_format"] = nxt
                    continue
                message = f"HTTP {exc.code}: {detail}"
                failure = ("auth" if exc.code in (401, 403) else
                    classify_failure(RuntimeError(message), quota_signatures=profile.get("quota_signatures", ())))
                raise DispatchError(message, failure_class=failure) from exc
            except (TimeoutError, socket.timeout) as exc:
                raise DispatchError(f"HTTP request timed out after {timeout}s", failure_class="timeout") from exc
            except error.URLError as exc:
                failure = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "crash"
                raise DispatchError(f"HTTP request failed: {exc.reason}", failure_class=failure) from exc
            except (ValueError, UnicodeError) as exc:
                raise DispatchError(f"HTTP response JSON parse failed: {exc}", failure_class="parse") from exc
            try:
                content = envelope["choices"][0]["message"].get("content")
                finish = envelope["choices"][0].get("finish_reason")
            except (KeyError, IndexError, TypeError, AttributeError) as exc:
                raise DispatchError("HTTP response missing choices[0].message.content", failure_class="parse") from exc
            if finish == "length":
                usage = envelope.get("usage") or {}
                raise DispatchError(
                    f"HTTP response hit max_tokens ({body['max_tokens']}) before finishing "
                    f"(completion_tokens={usage.get('completion_tokens')}); increase max_tokens "
                    "in the seat's profile", failure_class="parse")
            if not isinstance(content, str) or not content.strip():
                raise DispatchError("HTTP response has empty content; reasoning may have exhausted max_tokens "
                                    "(set reasoning_effort and increase max_tokens)", failure_class="parse")
            parse_json_output(content, schema_path, schema_profile)
            return content
    raise DispatchError("HTTP response_format negotiation failed", failure_class="parse")


def _parse(raw, goal_packet, review_packet, repo_root, ns, schema_path):
    return parse_json_output(raw, schema_path, None if ns.schema else ns.profile_data)


def prepare_template(text, ns):
    profile = ns.profile_data
    name = profile.get("name", ns.profile)
    prefix = profile.get("id_prefix") or f"{name}-rev"
    text = text.replace("gemini-rev", prefix).replace("gemini", name)
    start = text.find("The packet below is the canonical review surface.")
    end = text.find("{diff_packet_block}", start)
    if start >= 0 and end >= 0:
        if profile.get("kind") == "openai_compatible":
            instructions = ("The diff packet and bounded related excerpts below are your review input. "
                "No filesystem tools are available. Review only this packet and its open items. "
                "If required context is missing, identify it as a finding; do not invent content.\n\n")
        else:
            instructions = ("The diff packet below is your review input. "
                "You may use read-only tools to inspect additional context in the disposable "
                "repository copy. Do not modify files or execute project code.\n\n")
        text = text[:start] + instructions + text[end:]
    elif profile.get("kind") == "openai_compatible":
        text = ("No filesystem tools are available. Use the embedded diff packet and bounded "
                "related excerpts. Identify required missing context; do not invent content.\n\n" + text)
    if profile.get("kind") == "openai_compatible":
        text = text.replace("Use repository reads\nonly for relevant context; keep findings within the allowed scope.",
                            "Use the embedded context; keep findings within the allowed scope.")
    return text


def profile_from_argv(argv):
    """Allow engine adapters to forward an already merged operator profile."""
    # allow_abbrev=False: otherwise `--profile deepseek` (consult panel) is
    # taken as an abbreviation of --profile-json and parsed as JSON.
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--profile-json")
    options, _ = parser.parse_known_args(argv)
    return json.loads(options.profile_json) if options.profile_json else None


def _configure_parser(parser):
    parser.add_argument("--profile-json", help=argparse.SUPPRESS)


def profile_name_from_argv(argv, default):
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--profile", default=default)
    return parser.parse_known_args(argv)[0].profile


def main(argv=None):
    from consensus_mcp._dispatch_runner import ProviderSpec, main_for_provider
    argv = sys.argv[1:] if argv is None else argv

    def invoke(*, prompt, model, repo_root, schema_path, ns, log_path, anchors):
        from consensus_mcp._paths import state_root
        return invoke_http(ns.profile_data, prompt, state_root=state_root(),
                           schema_path=schema_path, model=model, timeout_seconds=ns.timeout_seconds,
                           schema_overridden=bool(ns.schema))

    name = profile_name_from_argv(argv, "deepseek")
    return main_for_provider(ProviderSpec(name=name, invoke=invoke, parse=_parse,
        template_name="gemini", profile=profile_from_argv(argv), configure_parser=_configure_parser,
        prepare_template=prepare_template), argv)


if __name__ == "__main__":
    raise SystemExit(main())
