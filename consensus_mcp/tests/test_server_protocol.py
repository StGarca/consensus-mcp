"""Protocol-envelope tests for the hand-rolled MCP JSON-RPC layer in server.py.

v2.2.1 audit M0.2 (docs/audits/2026-07-01-v2.2.1-repo-audit.md)

server.py implements the raw MCP stdio JSON-RPC 2.0 wire format directly
(no SDK). These tests drive `_handle_request` with plain dict requests --
no server process is spawned -- and cover: the initialize handshake shape,
tools/list wire translation (snake_case internal schema -> camelCase
inputSchema, per tool_registry.ToolRegistry.list_tools), the tools/call
happy path result wrapping, refusal paths (-32601 unknown tool / unknown
method, -32000 handler exception with message == str(exc), -32000 handler
SystemExit containment per M1 S1 -- consult
iteration-m1-hardening-design-4d7d2469), notification suppression
semantics, and one `_serve_stdio` smoke over StringIO pipes.

Hermetic: tools/call tests swap in a fresh ToolRegistry via
monkeypatch.setattr(server, "registry", ...) so no real tool handler runs.
"""
from __future__ import annotations

import io
import json
import sys

import pytest

import consensus_mcp.server as server
from consensus_mcp.tool_registry import ToolRegistry


def _install_registry(monkeypatch) -> ToolRegistry:
    """Swap the module-level registry for a fresh one; monkeypatch restores it."""
    fresh = ToolRegistry()
    monkeypatch.setattr(server, "registry", fresh)
    return fresh


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------

def test_initialize_response_shape():
    resp = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert resp == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": "consensus-mcp",
                "version": server._package_version(),
            },
            "capabilities": {"tools": {}},
        },
    }


def test_initialize_echoes_string_id():
    resp = server._handle_request({"jsonrpc": "2.0", "id": "init-abc", "method": "initialize"})
    assert resp["id"] == "init-abc"
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert "error" not in resp


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------

def test_tools_list_real_registry_wire_shape():
    """The real module-level registry lists every registered tool in the flat
    MCP wire shape: exactly {name, description, inputSchema} per entry, with
    the internal snake_case keys translated away."""
    resp = server._handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 2
    tools = resp["result"]["tools"]
    assert isinstance(tools, list)
    names = [t["name"] for t in tools]
    # Spot-check tools registered at import time in server.py.
    assert "state.read_decision_ledger" in names
    assert "audit.append_event" in names
    assert "reviewer.dispatch_codex" in names
    assert len(names) == len(set(names)), "duplicate tool names on the wire"
    for entry in tools:
        assert set(entry.keys()) == {"name", "description", "inputSchema"}
        assert isinstance(entry["inputSchema"], dict)
        # snake_case internals must never leak onto the wire
        assert "input_schema" not in entry
        assert "output_schema" not in entry
        assert "outputSchema" not in entry


def test_tools_list_translates_snake_case_input_schema(monkeypatch):
    fresh = _install_registry(monkeypatch)
    input_schema = {
        "type": "object",
        "properties": {"target": {"type": "string"}},
        "required": ["target"],
    }
    fresh.register(
        "stub.translate",
        {
            "name": "stub.translate",
            "description": "stub for wire translation",
            "input_schema": input_schema,
            "output_schema": {"type": "object"},
        },
        lambda **kw: {},
    )
    resp = server._handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    tools = resp["result"]["tools"]
    assert tools == [
        {
            "name": "stub.translate",
            "description": "stub for wire translation",
            "inputSchema": input_schema,
        }
    ]


def test_tools_list_defaults_for_sparse_schema(monkeypatch):
    """A schema dict missing name/description/input_schema falls back to the
    registration key, empty description, and {"type": "object"}."""
    fresh = _install_registry(monkeypatch)
    fresh.register("bare.tool", {}, lambda **kw: {})
    resp = server._handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    assert resp["result"]["tools"] == [
        {"name": "bare.tool", "description": "", "inputSchema": {"type": "object"}}
    ]


def test_tools_list_schema_name_overrides_registration_key(monkeypatch):
    """list_tools prefers schema["name"] over the registration key, while
    tools/call dispatch still keys on the registration name."""
    fresh = _install_registry(monkeypatch)
    fresh.register("internal.key", {"name": "public.name"}, lambda **kw: {"ok": True})
    resp = server._handle_request({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
    assert resp["result"]["tools"][0]["name"] == "public.name"
    # Dispatch is by registration key, not the advertised schema name.
    call_by_key = server._handle_request(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "internal.key", "arguments": {}}}
    )
    assert "result" in call_by_key
    call_by_wire_name = server._handle_request(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "public.name", "arguments": {}}}
    )
    assert call_by_wire_name["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# tools/call happy path
# ---------------------------------------------------------------------------

def test_tools_call_happy_path_wraps_result_as_text_content(monkeypatch):
    fresh = _install_registry(monkeypatch)
    seen: list[dict] = []

    def handler(value, count):
        seen.append({"value": value, "count": count})
        return {"echoed": value, "doubled": count * 2}

    fresh.register("stub.echo", {"name": "stub.echo"}, handler)
    resp = server._handle_request(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
         "params": {"name": "stub.echo", "arguments": {"value": "hello", "count": 3}}}
    )
    # Arguments are splatted as keyword args onto the handler.
    assert seen == [{"value": "hello", "count": 3}]
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 10
    assert "error" not in resp
    content = resp["result"]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert content[0]["text"] == json.dumps({"echoed": "hello", "doubled": 6})
    assert set(resp["result"].keys()) == {"content"}


def test_tools_call_json_encodes_non_dict_results(monkeypatch):
    """The wrapper json.dumps()-encodes whatever the handler returns; a bare
    string comes back JSON-quoted, not str()-flattened."""
    fresh = _install_registry(monkeypatch)
    fresh.register("stub.str", {"name": "stub.str"}, lambda: "plain result")
    resp = server._handle_request(
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
         "params": {"name": "stub.str"}}
    )
    assert resp["result"]["content"][0]["text"] == '"plain result"'


def test_tools_call_defaults_missing_and_null_arguments(monkeypatch):
    """Both an absent "arguments" key and an explicit null coerce to {} before
    the **splat, so zero-arg handlers work either way."""
    fresh = _install_registry(monkeypatch)
    fresh.register("stub.noargs", {"name": "stub.noargs"}, lambda: {"ran": True})
    for params in ({"name": "stub.noargs"}, {"name": "stub.noargs", "arguments": None}):
        resp = server._handle_request(
            {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": params}
        )
        assert json.loads(resp["result"]["content"][0]["text"]) == {"ran": True}


# ---------------------------------------------------------------------------
# tools/call refusal paths
# ---------------------------------------------------------------------------

def test_tools_call_unknown_tool_returns_32601(monkeypatch):
    _install_registry(monkeypatch)  # empty registry: nothing resolvable
    resp = server._handle_request(
        {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
         "params": {"name": "no.such.tool", "arguments": {"x": 1}}}
    )
    assert resp == {
        "jsonrpc": "2.0",
        "id": 13,
        "error": {"code": -32601, "message": "tool not found: no.such.tool"},
    }


def test_tools_call_without_params_reports_empty_tool_name():
    """No params at all -> name defaults to "" -> registry KeyError -> -32601
    with the empty name embedded in the refusal message."""
    resp = server._handle_request({"jsonrpc": "2.0", "id": 14, "method": "tools/call"})
    assert resp["error"]["code"] == -32601
    assert resp["error"]["message"] == "tool not found: "


def test_tools_call_handler_exception_maps_to_32000(monkeypatch):
    fresh = _install_registry(monkeypatch)

    def exploding():
        raise ValueError("goal packet rejected: allowed_files empty")

    fresh.register("stub.boom", {"name": "stub.boom"}, exploding)
    resp = server._handle_request(
        {"jsonrpc": "2.0", "id": 15, "method": "tools/call",
         "params": {"name": "stub.boom"}}
    )
    assert resp == {
        "jsonrpc": "2.0",
        "id": 15,
        "error": {
            "code": -32000,
            "message": "goal packet rejected: allowed_files empty",
        },
    }


def test_tools_call_argument_mismatch_maps_to_32000(monkeypatch):
    """A caller passing arguments the handler signature does not accept hits
    the same except-Exception net: TypeError surfaces as -32000."""
    fresh = _install_registry(monkeypatch)
    fresh.register("stub.strict", {"name": "stub.strict"}, lambda value: {"value": value})
    resp = server._handle_request(
        {"jsonrpc": "2.0", "id": 16, "method": "tools/call",
         "params": {"name": "stub.strict", "arguments": {"bogus": 1}}}
    )
    assert resp["error"]["code"] == -32000
    assert "unexpected keyword argument 'bogus'" in resp["error"]["message"]


def test_tools_call_handler_systemexit_maps_to_32000_and_server_survives(monkeypatch):
    """M1 S1 (consult iteration-m1-hardening-design-4d7d2469): SystemExit is a
    BaseException subclass the except-Exception net never saw, so a handler
    raising it (proven: the ledger tool's validator on a missing spec) used to
    kill the whole stdio MCP server. Now it maps to the -32000 envelope
    carrying the message, and the SAME registry answers the next request."""
    fresh = _install_registry(monkeypatch)

    def exiting():
        raise SystemExit("spec not found: /nowhere/orchestration-spec.md")

    fresh.register("stub.exit", {"name": "stub.exit"}, exiting)
    fresh.register("stub.after", {"name": "stub.after"}, lambda: {"alive": True})

    resp = server._handle_request(
        {"jsonrpc": "2.0", "id": 17, "method": "tools/call",
         "params": {"name": "stub.exit"}}
    )
    assert resp == {
        "jsonrpc": "2.0",
        "id": 17,
        "error": {
            "code": -32000,
            "message": "spec not found: /nowhere/orchestration-spec.md",
        },
    }

    # Server survives: a subsequent tools/call on the same registry works.
    follow_up = server._handle_request(
        {"jsonrpc": "2.0", "id": 18, "method": "tools/call",
         "params": {"name": "stub.after"}}
    )
    assert "error" not in follow_up
    assert json.loads(follow_up["result"]["content"][0]["text"]) == {"alive": True}


def test_tools_call_handler_sys_exit_code_maps_to_32000(monkeypatch):
    """sys.exit(2)-style handlers (SystemExit with an int code) are contained
    by the same M1 S1 catch; str(exc) renders the code as the message."""
    fresh = _install_registry(monkeypatch)

    def exiting():
        sys.exit(2)

    fresh.register("stub.exit2", {"name": "stub.exit2"}, exiting)
    resp = server._handle_request(
        {"jsonrpc": "2.0", "id": 19, "method": "tools/call",
         "params": {"name": "stub.exit2"}}
    )
    assert resp["error"] == {"code": -32000, "message": "2"}


# ---------------------------------------------------------------------------
# notifications and unknown methods
# ---------------------------------------------------------------------------

def test_notification_without_id_is_suppressed():
    assert server._handle_request(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ) is None


def test_notification_with_explicit_null_id_is_suppressed():
    assert server._handle_request(
        {"jsonrpc": "2.0", "id": None, "method": "notifications/cancelled"}
    ) is None


def test_initialize_and_tools_list_notifications_still_answered():
    """Documented quirk: the initialize/tools/list branches run BEFORE the
    id-is-None notification check, so those methods sent without an id still
    produce a response carrying "id": None (not suppressed)."""
    init = server._handle_request({"jsonrpc": "2.0", "method": "initialize"})
    assert init is not None
    assert init["id"] is None
    assert init["result"]["protocolVersion"] == "2024-11-05"

    listing = server._handle_request({"jsonrpc": "2.0", "method": "tools/list"})
    assert listing is not None
    assert listing["id"] is None
    assert isinstance(listing["result"]["tools"], list)


def test_unknown_method_with_id_returns_32601():
    resp = server._handle_request(
        {"jsonrpc": "2.0", "id": 20, "method": "resources/list"}
    )
    assert resp == {
        "jsonrpc": "2.0",
        "id": 20,
        "error": {"code": -32601, "message": "method not found: resources/list"},
    }


def test_missing_method_field_with_id_returns_32601():
    resp = server._handle_request({"jsonrpc": "2.0", "id": 21})
    assert resp["error"]["code"] == -32601
    assert resp["error"]["message"] == "method not found: "


def test_id_zero_treated_as_request_not_notification():
    """id 0 is falsy but not None; the notification check is `is None`, so a
    request with id 0 still receives an error response echoing id 0."""
    resp = server._handle_request({"jsonrpc": "2.0", "id": 0, "method": "no/such"})
    assert resp is not None
    assert resp["id"] == 0
    assert resp["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# _serve_stdio smoke (StringIO pipes; no process spawned)
# ---------------------------------------------------------------------------

def test_serve_stdio_smoke(monkeypatch):
    """One pass through the stdio loop: blank line skipped, malformed JSON
    answered with -32700, initialize answered, notification suppressed,
    unknown tool refused; EOF ends the loop cleanly."""
    lines = "\n".join([
        "",  # blank -> skipped, no output
        "{not json",  # -> -32700 parse error
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "no.such.tool"}}),
    ]) + "\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    server._serve_stdio()

    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(responses) == 3

    parse_err = responses[0]
    assert parse_err["id"] is None
    assert parse_err["error"]["code"] == -32700
    assert parse_err["error"]["message"].startswith("parse error:")

    init = responses[1]
    assert init["id"] == 1
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["serverInfo"]["name"] == "consensus-mcp"

    unknown = responses[2]
    assert unknown["id"] == 2
    assert unknown["error"] == {
        "code": -32601,
        "message": "tool not found: no.such.tool",
    }
