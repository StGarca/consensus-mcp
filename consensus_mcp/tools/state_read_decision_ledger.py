"""state.read_decision_ledger MCP tool. Phase 1 G5 partial.

Replaces direct yaml.safe_load of consensus-state/state/disposition-ledger.yaml
with mediated read + cache + auto-invalidate.

Cache invalidation: cache stores (ledger_sha256, yaml_text, mtime_ns). On read,
check stat(disposition-ledger.yaml).st_mtime_ns; if changed since cached,
re-read. Else serve from cache.

iter-0026 (Phase B step 1 per iter-0024 converged plan): migrated from
module-level REPO_ROOT capture to lazy `_paths.state_root()` resolution.
Path resolution now reads env state on every call, so test fixtures using
`monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT", str(tmp_path))` work without
needing iter-0019's `_isolate_archive_root` monkeypatch for this tool.

Per design spec:
- callable_by: any (read-only)
- inputs: {} (no parameters)
- outputs: {ledger_yaml: string, ledger_sha256: string}
- ledger_sha256 uses canonical_yaml_sha256 formula per spec section 7:
    hashlib.sha256(yaml.safe_dump(yaml.safe_load(open(p)), sort_keys=True).encode('utf-8')).hexdigest()
"""
from __future__ import annotations
import hashlib
from pathlib import Path

import yaml

from consensus_mcp._paths import state_root


def _ledger_path() -> Path:
    """Resolve <state_root>/state/disposition-ledger.yaml lazily.

    Honors CONSENSUS_MCP_STATE_ROOT / CONSENSUS_MCP_REPO_ROOT at call time.
    """
    return state_root() / "state" / "disposition-ledger.yaml"


SCHEMA = {
    "name": "state.read_decision_ledger",
    "description": (
        "Read the disposition ledger. Cached; auto-invalidates when the file changes on disk. "
        "Returns ledger content as YAML string plus canonical SHA-256."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "ledger_yaml": {"type": "string"},
            "ledger_sha256": {"type": "string"},
        },
        "required": ["ledger_yaml", "ledger_sha256"],
    },
}

_CACHE: dict = {"sha256": None, "yaml_text": None, "mtime_ns": None}


def _canonical_sha256(path: Path) -> tuple[str, str]:
    """Return (yaml_text, sha256_hex) for the file at path using canonical formula."""
    raw = path.read_bytes()
    loaded = yaml.safe_load(raw)
    yaml_text = yaml.safe_dump(loaded, sort_keys=True)
    sha = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
    return yaml_text, sha


def handle() -> dict:
    """MCP tool handler. No inputs per schema (inputs: {}).

    Called via handler(**{}) from server dispatch, or directly as handle().
    """
    ledger_path = _ledger_path()
    try:
        current_mtime_ns = ledger_path.stat().st_mtime_ns
    except FileNotFoundError:
        return {"error": f"ledger not found: {ledger_path}"}

    if _CACHE["mtime_ns"] == current_mtime_ns and _CACHE["sha256"] is not None:
        return {"ledger_yaml": _CACHE["yaml_text"], "ledger_sha256": _CACHE["sha256"]}

    yaml_text, sha = _canonical_sha256(ledger_path)
    _CACHE["mtime_ns"] = current_mtime_ns
    _CACHE["yaml_text"] = yaml_text
    _CACHE["sha256"] = sha
    return {"ledger_yaml": yaml_text, "ledger_sha256": sha}


def register(registry) -> None:
    """Register this tool with the server's ToolRegistry."""
    registry.register(SCHEMA["name"], SCHEMA, handle)
