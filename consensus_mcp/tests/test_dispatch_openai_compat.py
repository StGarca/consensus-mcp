"""HTTP reviewer tests use only a loopback stub server."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import sys
import threading
import time

import pytest

from consensus_mcp._dispatch_openai_compat import http_slot, invoke_http, parse_json_output, prepare_template, review_schema
from consensus_mcp.contributors.base import DispatchError


@contextmanager
def server(respond):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            code, data = respond(payload, self.headers)
            encoded = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.fixture
def schema(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"type": "object", "required": ["findings"],
                               "properties": {"findings": {"type": "array"}}}), encoding="utf-8")
    return path


def response(content='{"findings": []}'):
    return {"choices": [{"message": {"content": content}}]}


def invoke(url, tmp_path, schema, **profile):
    return invoke_http({"base_url": url, "model": "local-model", "reasoning_effort": "low", **profile},
                       "review", state_root=tmp_path, schema_path=schema, timeout_seconds=5)


def test_http_success_requests_schema_and_effort(tmp_path, schema):
    seen = []
    def respond(body, headers):
        seen.append(body)
        return 200, response()
    with server(respond) as url:
        assert json.loads(invoke(url, tmp_path, schema)) == {"findings": []}
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert seen[0]["reasoning_effort"] == "low"
    assert seen[0]["model"] == "local-model"


def test_http_schema_unsupported_uses_json_mode_and_local_validation(tmp_path, schema):
    modes = []
    def respond(body, headers):
        modes.append(body["response_format"]["type"])
        if len(modes) == 1:
            return 400, {"error": "response_format json_schema unsupported"}
        return 200, response('{"bad": true}')
    with server(respond) as url:
        with pytest.raises(DispatchError) as raised:
            invoke(url, tmp_path, schema)
    assert raised.value.failure_class == "parse"
    assert modes == ["json_schema", "json_object"]


def test_http_truncated_output_names_max_tokens(tmp_path, schema):
    truncated = {"choices": [{"message": {"content": '{"findings": [{"title": "cut'},
                              "finish_reason": "length"}],
                 "usage": {"completion_tokens": 123}}
    with server(lambda *_: (200, truncated)) as url:
        with pytest.raises(DispatchError, match=r"hit max_tokens \(77\).*completion_tokens=123") as raised:
            invoke(url, tmp_path, schema, max_tokens=77)
    assert raised.value.failure_class == "parse"


def test_http_empty_reasoning_content_is_parse(tmp_path, schema):
    with server(lambda *_: (200, response(""))) as url:
        with pytest.raises(DispatchError, match="reasoning.*max_tokens") as raised:
            invoke(url, tmp_path, schema)
    assert raised.value.failure_class == "parse"


@pytest.mark.parametrize("detail, expected", [
    ("You've reached your Fable limit", "quota"),
    ("too many requests", "rate_limit"),
])
def test_http_429_classification(tmp_path, schema, detail, expected):
    with server(lambda *_: (429, {"error": detail})) as url:
        with pytest.raises(DispatchError) as raised:
            invoke(url, tmp_path, schema)
    assert raised.value.failure_class == expected


def test_http_quota_signature(tmp_path, schema):
    with server(lambda *_: (429, {"error": "daily bucket depleted"})) as url:
        with pytest.raises(DispatchError) as raised:
            invoke(url, tmp_path, schema, quota_signatures=["daily bucket depleted"])
    assert raised.value.failure_class == "quota"


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_status_overrides_quota_text(tmp_path, schema, status):
    with server(lambda *_: (status, {"error": "quota credit requires authentication"})) as url:
        with pytest.raises(DispatchError) as raised:
            invoke(url, tmp_path, schema)
    assert raised.value.failure_class == "auth"


def test_http_cap_four_shared_by_threads(tmp_path, schema):
    lock = threading.Lock()
    active = high_water = 0
    def respond(*_):
        nonlocal active, high_water
        with lock:
            active += 1
            high_water = max(high_water, active)
        time.sleep(.1)
        with lock:
            active -= 1
        return 200, response()
    with server(respond) as url:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: invoke(url, tmp_path, schema, max_concurrency=4), range(8)))
    assert len(results) == 8
    assert high_water == 4


def test_http_slot_is_shared_across_processes_and_released(tmp_path):
    script = ("from pathlib import Path; import sys; "
              "from consensus_mcp._dispatch_openai_compat import http_slot\n"
              "with http_slot(Path(sys.argv[1]), 'local-slot', 1, 5):\n"
              " print('locked', flush=True)\n"
              " sys.stdin.read(1)\n")
    proc = subprocess.Popen([sys.executable, "-I", "-c", script, str(tmp_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8")
    try:
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(DispatchError) as raised:
            with http_slot(tmp_path, "local-slot", 1, .15):
                pytest.fail("another process holds the only slot")
        assert raised.value.failure_class == "timeout"
        _, stderr = proc.communicate("x", timeout=5)
        assert proc.returncode == 0, stderr
        with http_slot(tmp_path, "local-slot", 1, .15):
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)


def test_http_resolves_base_url_env_and_optional_key(tmp_path, schema, monkeypatch):
    seen = []
    def respond(body, headers):
        seen.append(headers.get("Authorization"))
        return 200, response()
    with server(respond) as url:
        monkeypatch.setenv("LOCAL_HTTP_URL", url)
        monkeypatch.setenv("LOCAL_HTTP_KEY", "test-key")
        invoke_http({"base_url_env": "LOCAL_HTTP_URL", "model": "local", "api_key_env": "LOCAL_HTTP_KEY"},
                    "review", state_root=tmp_path, schema_path=schema)
    assert seen == ["Bearer test-key"]


@pytest.mark.parametrize("change", ["patch", "blocking", "duplicate"])
def test_review_only_output_guards(tmp_path, schema, change):
    output = {"findings": [], "goal_satisfied": True, "blocking_objections": []}
    finding = {"id": "seat-rev-001", "severity": "blocking", "patch_proposal": None}
    if change == "patch":
        finding.update(severity="low", patch_proposal={"unified_diff": "bad"})
        output["findings"] = [finding]
    elif change == "blocking":
        output["findings"] = [finding]
        output["blocking_objections"] = [finding["id"]]
    else:
        finding["severity"] = "low"
        output["findings"] = [finding, finding]
    with pytest.raises(DispatchError) as raised:
        parse_json_output(json.dumps(output), schema)
    assert raised.value.failure_class == "parse"


@pytest.mark.parametrize("findings", [[{"severity": "low"}], [None], "bad", 5])
def test_malformed_custom_schema_output_is_parse(tmp_path, findings):
    path = tmp_path / "permissive.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(DispatchError) as raised:
        parse_json_output(json.dumps({"goal_satisfied": True, "findings": findings}), path)
    assert raised.value.failure_class == "parse"


def test_tool_template_allows_readonly_exploration_and_own_ids():
    from pathlib import Path
    from types import SimpleNamespace
    from consensus_mcp import _dispatch_openai_compat as http
    templates = Path(http.__file__).parent / "dispatch_templates"
    ns = SimpleNamespace(profile="opencode-deepseek", profile_data={"name": "opencode-deepseek",
                         "kind": "cli_reviewer", "id_prefix": "opencode-rev"})
    text = prepare_template((templates / "gemini_review_template.md").read_text(encoding="utf-8"), ns)
    assert "read-only tools" in text
    assert "Do NOT attempt to read files" not in text
    assert "opencode-rev-001" in text
    schema = review_schema(templates / "gemini_review_schema.json", ns.profile_data)
    assert schema["properties"]["findings"]["items"]["properties"]["id"]["pattern"] == r"^opencode-rev-\d+$"


_DGX_400 = {"error": {"message": "structured output (response_format and the guided_* fields) "
                                 "is not supported by this engine yet", "type": "invalid_request_error"}}


def test_http_schema_not_enforced_profile_sends_no_response_format(tmp_path, schema):
    """Live 2026-11-13: a self-hosted engine rejects every response_format. A profile
    with output.schema_enforced: false must not send one; the schema is still
    enforced locally."""
    seen = []
    def respond(body, headers):
        seen.append(body)
        return (400, _DGX_400) if "response_format" in body else (200, response())
    with server(respond) as url:
        assert json.loads(invoke(url, tmp_path, schema, output={"schema_enforced": False})) == {"findings": []}
    assert len(seen) == 1 and "response_format" not in seen[0]


def test_http_ladder_steps_down_to_plain_text_when_engine_rejects_all_modes(tmp_path, schema):
    seen = []
    def respond(body, headers):
        seen.append((body.get("response_format") or {}).get("type"))
        return (400, _DGX_400) if "response_format" in body else (200, response())
    with server(respond) as url:
        assert json.loads(invoke(url, tmp_path, schema)) == {"findings": []}
    assert seen == ["json_schema", "json_object", None]
