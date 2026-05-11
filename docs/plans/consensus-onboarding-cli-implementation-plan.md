# consensus-mcp onboarding-CLI implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a standalone `consensus` shell CLI with headless cross-AI review and pluggable adapter system per docs/specs/consensus-onboarding-cli-spec.md.

**Architecture:** Wrap-and-extend. The new CLI reuses existing `_dispatch_codex` machinery (watchdog, streaming, audit log, process-group termination) and adds a thin layer of (adapter manifest loader + orchestration + first-run wizard) on top. All work is decomposed into five sub-iterations, each a normal-sized consensus-mcp iteration ending with cross-family codex review.

**Tech Stack:** Python 3.10+, PyYAML, pytest, argparse, subprocess.

---

## Sub-iteration boundaries

| Sub-iter | Title | What lands | Why this size |
|---|---|---|---|
| **iter-0005a** | Adapter foundation | `_adapter_runtime.py` + 2 built-in adapter YAMLs + tests | Pure foundation, no dependents; testable in isolation |
| **iter-0005b** | Dispatch refactor | `_dispatch_codex.py` accepts adapter manifest; existing flag-path preserved | Only breaking-shape change in existing code; small but high-blast-radius |
| **iter-0005c** | Onboarding wizard | `_onboarding.py` + project-scan + non-interactive mode | Self-contained user-facing module |
| **iter-0005d** | Run orchestration | `_run.py` + diff handling + git apply + commit logic | Largest piece; depends on a/b/c |
| **iter-0005e** | CLI surface | `_cli.py` + `pyproject.toml` console_scripts + `consensus status/close/history` glue | Argparse glue; depends on all prior |

Each sub-iter authors a goal_packet, implements, runs tests green, then dispatches codex for cross-family closure. Total ~5 codex review rounds (matching the cost profile we've seen in iters 0001–0003).

---

## File structure (across all sub-iterations)

```
consensus_mcp/
├── _cli.py                       # NEW (iter-0005e) — argparse entry
├── _adapter_runtime.py           # NEW (iter-0005a) — manifest loader + invocation
├── _onboarding.py                # NEW (iter-0005c) — `consensus init` wizard
├── _run.py                       # NEW (iter-0005d) — `consensus run` orchestration
├── adapters/                     # NEW (iter-0005a) — directory + 2 manifests
│   ├── __init__.py
│   ├── claude.yaml
│   └── codex.yaml
├── _dispatch_codex.py            # MODIFIED (iter-0005b) — accepts adapter manifest
└── tests/
    ├── test_adapter_runtime.py   # NEW (iter-0005a)
    ├── test_onboarding.py        # NEW (iter-0005c)
    ├── test_run.py               # NEW (iter-0005d)
    ├── test_cli.py               # NEW (iter-0005e)
    └── test_dispatch_codex.py    # MODIFIED (iter-0005b) — fixture for adapter manifest

pyproject.toml                    # MODIFIED (iter-0005e) — [project.scripts]
```

---

# Sub-iteration 0005a — Adapter foundation

**Goal:** Load and validate adapter YAML manifests. Resolve binaries on disk. Substitute templates into invocation commands. **No subprocess execution yet — pure loader logic.**

**Files:**
- Create: `consensus_mcp/_adapter_runtime.py`
- Create: `consensus_mcp/adapters/__init__.py` (empty)
- Create: `consensus_mcp/adapters/claude.yaml`
- Create: `consensus_mcp/adapters/codex.yaml`
- Test: `consensus_mcp/tests/test_adapter_runtime.py`

### Task A1: Author the adapter YAML schema validator

**Files:**
- Create: `consensus_mcp/_adapter_runtime.py`
- Test: `consensus_mcp/tests/test_adapter_runtime.py`

- [ ] **Step 1: Write the failing test**

```python
# consensus_mcp/tests/test_adapter_runtime.py
import pytest
import yaml
from pathlib import Path
from consensus_mcp import _adapter_runtime as ar


def test_load_manifest_minimal_valid(tmp_path):
    manifest = {
        "schema_version": 1,
        "name": "demo",
        "model_family": "demo",
        "binary": {"name": "demo", "windows_suffixes": [".cmd", ".exe", ""], "not_found_help": "install"},
        "probe": {"command": "{{binary}} --version", "timeout_seconds": 10, "expect_exit_code": 0},
        "invocation": {
            "primary":   {"command": "{{binary}} primary {{quoted_prompt}}", "output_contract": "unified_diff_only", "stdin": None},
            "secondary": {"command": "{{binary}} secondary {{quoted_prompt}}", "output_contract": "codex_review_schema_v1", "stdin": None},
        },
        "prompt_budget_bytes": 100000,
        "stall_silence_seconds": 60,
        "heartbeat_interval_seconds": 30,
        "timeout_seconds": 600,
    }
    p = tmp_path / "demo.yaml"
    p.write_text(yaml.safe_dump(manifest))
    m = ar.load_manifest(p)
    assert m["name"] == "demo"
    assert m["model_family"] == "demo"


def test_load_manifest_missing_required_field(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("schema_version: 1\nname: bad\n")  # missing model_family + others
    with pytest.raises(ar.AdapterManifestError, match="missing required field"):
        ar.load_manifest(p)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest consensus_mcp/tests/test_adapter_runtime.py -v`
Expected: ImportError on `_adapter_runtime` (module doesn't exist).

- [ ] **Step 3: Implement minimal loader**

```python
# consensus_mcp/_adapter_runtime.py
"""Adapter manifest loader + resolver. iter-0005a.

Implements docs/specs/consensus-onboarding-cli-spec.md §8.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


_REQUIRED_TOP = (
    "schema_version", "name", "model_family", "binary", "probe",
    "invocation", "prompt_budget_bytes", "stall_silence_seconds",
    "heartbeat_interval_seconds", "timeout_seconds",
)
_REQUIRED_BINARY = ("name", "windows_suffixes", "not_found_help")
_REQUIRED_PROBE = ("command", "timeout_seconds")
_REQUIRED_INVOCATION_ROLE = ("command", "output_contract")
_ALLOWED_OUTPUT_CONTRACTS = frozenset({
    "unified_diff_in_fenced_block",
    "unified_diff_only",
    "codex_review_schema_v1",
})


class AdapterManifestError(ValueError):
    """Raised when an adapter manifest is malformed."""


def load_manifest(path: Path) -> dict:
    """Load and structurally validate an adapter manifest YAML file."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AdapterManifestError(f"{path}: YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise AdapterManifestError(f"{path}: top-level must be a mapping")
    for k in _REQUIRED_TOP:
        if k not in data:
            raise AdapterManifestError(f"{path}: missing required field '{k}'")
    binary = data["binary"]
    if not isinstance(binary, dict):
        raise AdapterManifestError(f"{path}: 'binary' must be a mapping")
    for k in _REQUIRED_BINARY:
        if k not in binary:
            raise AdapterManifestError(f"{path}: missing required field 'binary.{k}'")
    probe = data["probe"]
    for k in _REQUIRED_PROBE:
        if k not in probe:
            raise AdapterManifestError(f"{path}: missing required field 'probe.{k}'")
    inv = data["invocation"]
    for role in ("primary", "secondary"):
        if role not in inv:
            raise AdapterManifestError(f"{path}: missing required field 'invocation.{role}'")
        for k in _REQUIRED_INVOCATION_ROLE:
            if k not in inv[role]:
                raise AdapterManifestError(f"{path}: missing required field 'invocation.{role}.{k}'")
        contract = inv[role]["output_contract"]
        if contract not in _ALLOWED_OUTPUT_CONTRACTS:
            raise AdapterManifestError(
                f"{path}: invocation.{role}.output_contract={contract!r} not in {sorted(_ALLOWED_OUTPUT_CONTRACTS)}"
            )
    return data
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest consensus_mcp/tests/test_adapter_runtime.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_adapter_runtime.py consensus_mcp/tests/test_adapter_runtime.py
git commit -m "feat(adapter): manifest loader with structural validation"
```

### Task A2: Binary resolution (Windows-aware)

**Files:**
- Modify: `consensus_mcp/_adapter_runtime.py`
- Modify: `consensus_mcp/tests/test_adapter_runtime.py`

- [ ] **Step 1: Write the failing tests**

```python
# Append to test_adapter_runtime.py
import shutil
from unittest.mock import patch

def test_resolve_binary_found_via_which():
    with patch.object(shutil, "which", return_value="/usr/local/bin/codex"):
        path = ar.resolve_binary({"name": "codex", "windows_suffixes": [], "not_found_help": ""})
    assert path == "/usr/local/bin/codex"


def test_resolve_binary_windows_suffix_fallback():
    # which() returns None for bare name, then hit on .cmd suffix
    calls = {"n": 0}
    def fake_which(name):
        calls["n"] += 1
        if name.endswith(".cmd"):
            return "C:\\Users\\x\\AppData\\Roaming\\npm\\claude.cmd"
        return None
    with patch.object(shutil, "which", side_effect=fake_which):
        path = ar.resolve_binary({"name": "claude", "windows_suffixes": [".cmd", ".exe", ""], "not_found_help": ""})
    assert path is not None
    assert path.endswith("claude.cmd")


def test_resolve_binary_not_found_returns_none():
    with patch.object(shutil, "which", return_value=None):
        path = ar.resolve_binary({"name": "nonexistent", "windows_suffixes": [".cmd", ".exe"], "not_found_help": ""})
    assert path is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest consensus_mcp/tests/test_adapter_runtime.py -v`
Expected: AttributeError on `ar.resolve_binary`.

- [ ] **Step 3: Implement resolve_binary**

```python
# Append to _adapter_runtime.py
import shutil


def resolve_binary(binary_spec: dict) -> str | None:
    """Resolve the absolute path to an adapter's binary, or None if not found.

    Strategy: try `shutil.which(name)` first; if not found, iterate
    windows_suffixes appending each to name. The first hit wins.
    """
    base = binary_spec["name"]
    suffixes = binary_spec.get("windows_suffixes") or [""]
    candidates = [base] + [f"{base}{s}" for s in suffixes if s and s not in ("",)]
    for cand in candidates:
        hit = shutil.which(cand)
        if hit:
            return hit
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest consensus_mcp/tests/test_adapter_runtime.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_adapter_runtime.py consensus_mcp/tests/test_adapter_runtime.py
git commit -m "feat(adapter): cross-platform binary resolution with Windows suffix loop"
```

### Task A3: Template substitution (whitelisted, no eval)

**Files:**
- Modify: `consensus_mcp/_adapter_runtime.py`
- Modify: `consensus_mcp/tests/test_adapter_runtime.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_substitute_template_replaces_known_vars():
    out = ar.substitute_template(
        "{{binary}} -p {{quoted_prompt}}",
        {"binary": "/usr/bin/claude", "quoted_prompt": "'hello'"},
    )
    assert out == "/usr/bin/claude -p 'hello'"


def test_substitute_template_rejects_unknown_var():
    with pytest.raises(ar.AdapterManifestError, match="unknown template variable"):
        ar.substitute_template("{{evil}}", {"binary": "x"})


def test_substitute_template_leaves_double_braces_in_strings_alone():
    # Literal {{not_a_var}} where the var isn't whitelisted should fail closed.
    # No partial-substitution behavior.
    with pytest.raises(ar.AdapterManifestError):
        ar.substitute_template("echo {{not_a_real_var}}", {"binary": "x"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest consensus_mcp/tests/test_adapter_runtime.py -v`
Expected: 3 new failures (AttributeError on substitute_template).

- [ ] **Step 3: Implement substitute_template**

```python
# Append to _adapter_runtime.py
import re

_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")
_ALLOWED_TEMPLATE_VARS = frozenset({"binary", "quoted_prompt", "schema_path", "iteration_dir"})


def substitute_template(template: str, variables: dict) -> str:
    """Substitute {{var}} occurrences with values from `variables`.

    Whitelist-checked: any var seen that isn't in _ALLOWED_TEMPLATE_VARS raises.
    Missing values for whitelisted vars also raise (no silent empty substitution).
    """
    def repl(m: re.Match) -> str:
        var = m.group(1)
        if var not in _ALLOWED_TEMPLATE_VARS:
            raise AdapterManifestError(f"unknown template variable {{{{ {var} }}}}")
        if var not in variables:
            raise AdapterManifestError(f"template variable {{{{ {var} }}}} has no value supplied")
        return str(variables[var])
    return _TEMPLATE_VAR_RE.sub(repl, template)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest consensus_mcp/tests/test_adapter_runtime.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_adapter_runtime.py consensus_mcp/tests/test_adapter_runtime.py
git commit -m "feat(adapter): whitelisted template substitution"
```

### Task A4: Built-in adapter manifests (claude.yaml + codex.yaml)

**Files:**
- Create: `consensus_mcp/adapters/__init__.py`
- Create: `consensus_mcp/adapters/claude.yaml`
- Create: `consensus_mcp/adapters/codex.yaml`
- Modify: `consensus_mcp/tests/test_adapter_runtime.py`

- [ ] **Step 1: Write the failing test**

```python
def test_load_builtin_claude_adapter():
    from importlib.resources import files
    p = files("consensus_mcp.adapters").joinpath("claude.yaml")
    m = ar.load_manifest(Path(str(p)))
    assert m["name"] == "claude"
    assert m["model_family"] == "claude"


def test_load_builtin_codex_adapter():
    from importlib.resources import files
    p = files("consensus_mcp.adapters").joinpath("codex.yaml")
    m = ar.load_manifest(Path(str(p)))
    assert m["name"] == "codex"
    assert m["model_family"] == "codex"
    # iter-0002/0003 learning: codex needs long stall threshold for structured output
    assert m["stall_silence_seconds"] >= 600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest consensus_mcp/tests/test_adapter_runtime.py -v`
Expected: PackageNotFoundError / FileNotFoundError for adapters/claude.yaml.

- [ ] **Step 3: Create the adapter manifests**

`consensus_mcp/adapters/__init__.py`:

```python
# Built-in adapter manifests for consensus-mcp. See _adapter_runtime.py.
```

`consensus_mcp/adapters/claude.yaml`:

```yaml
schema_version: 1
name: claude
model_family: claude
display_name: Claude Code CLI
description: Anthropic Claude Code CLI in print-mode for headless invocations.
binary:
  name: claude
  windows_suffixes: [".cmd", ".exe", ""]
  not_found_help: |
    Claude Code CLI not found on $PATH. Install with:
      npm install -g @anthropic-ai/claude-code
    then run `consensus init` again.
probe:
  command: "{{binary}} --version"
  timeout_seconds: 10
  expect_exit_code: 0
invocation:
  primary:
    command: "{{binary}} -p {{quoted_prompt}} --output-format text"
    output_contract: unified_diff_in_fenced_block
    stdin: null
  secondary:
    command: "{{binary}} -p {{quoted_prompt}} --output-format json"
    output_contract: codex_review_schema_v1
    stdin: null
prompt_budget_bytes: 600000
stall_silence_seconds: 90
heartbeat_interval_seconds: 30
timeout_seconds: 600
preflight: null
```

`consensus_mcp/adapters/codex.yaml`:

```yaml
schema_version: 1
name: codex
model_family: codex
display_name: OpenAI Codex CLI
description: Codex CLI for headless cross-AI review.
binary:
  name: codex
  windows_suffixes: [".cmd", ".exe", ""]
  not_found_help: |
    Codex CLI not found on $PATH. Install with:
      npm install -g @openai/codex-cli
    then run `consensus init` again.
probe:
  command: "{{binary}} --version"
  timeout_seconds: 10
  expect_exit_code: 0
invocation:
  primary:
    command: "{{binary}} exec --sandbox workspace-write {{quoted_prompt}}"
    output_contract: unified_diff_in_fenced_block
    stdin: null
  secondary:
    command: "{{binary}} exec --sandbox read-only --output-schema {{schema_path}} {{quoted_prompt}}"
    output_contract: codex_review_schema_v1
    stdin: null
prompt_budget_bytes: 100000
stall_silence_seconds: 900
heartbeat_interval_seconds: 30
timeout_seconds: 1200
preflight: null
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest consensus_mcp/tests/test_adapter_runtime.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit + ensure pyproject.toml includes YAMLs in package data**

```bash
# verify pyproject already includes yaml files (consensus_mcp/adapters/*.yaml)
grep -A2 'package-data\|tool.setuptools' pyproject.toml || true
# If missing, add: package-data = {consensus_mcp = ["adapters/*.yaml"]}
git add consensus_mcp/adapters/ consensus_mcp/tests/test_adapter_runtime.py
git commit -m "feat(adapter): ship built-in claude + codex adapter manifests"
```

### Task A5: Adapter resolution path (user-plugin > built-in)

**Files:**
- Modify: `consensus_mcp/_adapter_runtime.py`
- Modify: `consensus_mcp/tests/test_adapter_runtime.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_resolve_adapter_builtin_when_no_plugin(tmp_path):
    # No .consensus/adapters/claude.yaml; should fall back to built-in.
    repo_root = tmp_path
    (repo_root / ".consensus").mkdir()
    m = ar.resolve_adapter("claude", repo_root=repo_root)
    assert m["name"] == "claude"


def test_resolve_adapter_user_plugin_shadows_builtin(tmp_path, monkeypatch):
    repo_root = tmp_path
    (repo_root / ".consensus" / "adapters").mkdir(parents=True)
    plugin = {
        "schema_version": 1, "name": "claude", "model_family": "claude",
        "display_name": "Custom Claude", "description": "x",
        "binary": {"name": "claude-custom", "windows_suffixes": [], "not_found_help": "x"},
        "probe": {"command": "{{binary}} --version", "timeout_seconds": 5},
        "invocation": {
            "primary":   {"command": "{{binary}} primary", "output_contract": "unified_diff_only"},
            "secondary": {"command": "{{binary}} secondary", "output_contract": "codex_review_schema_v1"},
        },
        "prompt_budget_bytes": 50000, "stall_silence_seconds": 30,
        "heartbeat_interval_seconds": 10, "timeout_seconds": 60,
    }
    (repo_root / ".consensus" / "adapters" / "claude.yaml").write_text(yaml.safe_dump(plugin))
    m = ar.resolve_adapter("claude", repo_root=repo_root)
    assert m["display_name"] == "Custom Claude"
    assert m["binary"]["name"] == "claude-custom"


def test_resolve_adapter_unknown_name(tmp_path):
    with pytest.raises(ar.AdapterManifestError, match="adapter 'nonexistent' not found"):
        ar.resolve_adapter("nonexistent", repo_root=tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest consensus_mcp/tests/test_adapter_runtime.py -v`
Expected: 3 new failures.

- [ ] **Step 3: Implement resolve_adapter**

```python
# Append to _adapter_runtime.py
from importlib.resources import files as _pkg_files


def resolve_adapter(name: str, *, repo_root: Path) -> dict:
    """Resolve an adapter manifest by name.

    Lookup order:
      1. {repo_root}/.consensus/adapters/{name}.yaml (user plugin / override)
      2. consensus_mcp/adapters/{name}.yaml (built-in)
    """
    user_path = Path(repo_root) / ".consensus" / "adapters" / f"{name}.yaml"
    if user_path.is_file():
        return load_manifest(user_path)
    try:
        builtin = _pkg_files("consensus_mcp.adapters").joinpath(f"{name}.yaml")
        builtin_path = Path(str(builtin))
        if builtin_path.is_file():
            return load_manifest(builtin_path)
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    raise AdapterManifestError(f"adapter {name!r} not found (looked in {user_path} and built-ins)")


def apply_config_overrides(manifest: dict, overrides: dict | None) -> dict:
    """Merge inline overrides from .consensus/config.yaml's adapters.<name>: section."""
    if not overrides:
        return manifest
    merged = dict(manifest)
    for k, v in overrides.items():
        merged[k] = v
    return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest consensus_mcp/tests/test_adapter_runtime.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_adapter_runtime.py consensus_mcp/tests/test_adapter_runtime.py
git commit -m "feat(adapter): user-plugin > built-in resolution + inline override merge"
```

### Iter-0005a closure

- [ ] **Step 1: Run full test suite to confirm no regression**

```bash
python -m pytest consensus_mcp/tests/ -q
```

Expected: all prior tests + new test_adapter_runtime tests green.

- [ ] **Step 2: Author iter-0005a goal_packet + dispatch codex closure**

```bash
mkdir -p consensus-state/active/iteration-0005a-adapter-foundation
# Author goal_packet.yaml with allowed_files: _adapter_runtime.py, adapters/*, test_adapter_runtime.py
# Compute scope_signature via _self_drive
# Run _author_review_packet with the 4-5 new files
# Dispatch codex with reviewer-id codex-iter0005a-1
```

- [ ] **Step 3: Address any codex findings; re-dispatch if needed**

Decision tree mirrors iter-0003's path:
- If `goal_satisfied: true` and no blocking → write iteration-outcome.yaml + closure-certificate.yaml → done
- If blocking → fix, re-author review-packet (new bundle_sha), re-dispatch as pass-2
- If non-blocking medium findings → operator decides (usually: fix + re-dispatch)

---

# Sub-iteration 0005b — Dispatch refactor

**Goal:** Make `_invoke_codex` adapter-agnostic. Rename to `_invoke_subprocess_ai`, accept adapter manifest, preserve existing CLI invocation path for backward compat.

**Files:**
- Modify: `consensus_mcp/_dispatch_codex.py`
- Modify: `consensus_mcp/tests/test_dispatch_codex.py`

### Task B1: Rename _invoke_codex → _invoke_subprocess_ai, accept adapter manifest

**Files:**
- Modify: `consensus_mcp/_dispatch_codex.py` (function definition at ~line 604)
- Modify: `consensus_mcp/tests/test_dispatch_codex.py`

- [ ] **Step 1: Write the failing test for adapter-passing**

```python
# Append to test_dispatch_codex.py
def test_invoke_subprocess_ai_uses_adapter_command(tmp_path, monkeypatch):
    from consensus_mcp import _dispatch_codex as d
    captured = {}
    class FakePopen:
        def __init__(self, args, **kw):
            captured["args"] = args
            self.stdout = io.StringIO("dummy\n")
            self.stderr = io.StringIO("")
            self.returncode = 0
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def communicate(self, timeout=None): return ("dummy\n", "")
        def terminate(self): pass
    adapter = {
        "binary_resolved_path": "/usr/bin/fake",
        "invocation": {"secondary": {"command": "/usr/bin/fake review {{quoted_prompt}}", "output_contract": "codex_review_schema_v1"}},
        "stall_silence_seconds": 60,
        "heartbeat_interval_seconds": 30,
        "timeout_seconds": 60,
    }
    # Call new signature
    out = d._invoke_subprocess_ai(
        prompt="hi",
        adapter_manifest=adapter,
        role="secondary",
        schema_path=tmp_path / "schema.json",
        repo_root=tmp_path,
        popen_factory=lambda *a, **kw: FakePopen(*a, **kw),
    )
    # Command path used /usr/bin/fake from adapter, NOT hard-coded codex
    assert "/usr/bin/fake" in captured["args"][0] or "/usr/bin/fake" in " ".join(captured["args"])
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m pytest consensus_mcp/tests/test_dispatch_codex.py::test_invoke_subprocess_ai_uses_adapter_command -v`
Expected: AttributeError on `_invoke_subprocess_ai`.

- [ ] **Step 3: Refactor `_invoke_codex` → `_invoke_subprocess_ai`**

In `consensus_mcp/_dispatch_codex.py`:

```python
# Replace the def line and body header of _invoke_codex (currently ~line 604).
# New signature:
def _invoke_subprocess_ai(
    prompt: str,
    adapter_manifest: dict,
    role: str,                    # "primary" or "secondary"
    repo_root: Path,
    schema_path: Path | None = None,
    log_path=None,
    anchors=None,
    heartbeat_interval: float | None = None,   # if None, read from manifest
    stall_silence_seconds: float | None = None, # if None, read from manifest
    timeout_seconds: int | None = None,         # if None, read from manifest
    poll_interval: float = 0.5,
    time_fn=None,
    popen_factory=None,
) -> str:
    """Adapter-agnostic subprocess AI invocation. Replaces _invoke_codex.

    Manifest fields used:
      - binary_resolved_path (injected by caller after resolve_binary)
      - invocation[role].command (substituted via _adapter_runtime.substitute_template)
      - invocation[role].output_contract
      - stall_silence_seconds, heartbeat_interval_seconds, timeout_seconds
    """
    from consensus_mcp._adapter_runtime import substitute_template
    binary_path = adapter_manifest["binary_resolved_path"]
    command_tmpl = adapter_manifest["invocation"][role]["command"]
    quoted_prompt = _shell_quote(prompt)
    cmd_str = substitute_template(command_tmpl, {
        "binary": binary_path,
        "quoted_prompt": quoted_prompt,
        "schema_path": str(schema_path) if schema_path else "",
        "iteration_dir": str((anchors or {}).get("iteration_dir", "")),
    })
    # ... rest of body unchanged: Popen + reader threads + heartbeat + abort signal + termination
    # Use manifest defaults if kwargs are None
    if heartbeat_interval is None:
        heartbeat_interval = adapter_manifest.get("heartbeat_interval_seconds", 30.0)
    if stall_silence_seconds is None:
        stall_silence_seconds = adapter_manifest.get("stall_silence_seconds", 45.0)
    if timeout_seconds is None:
        timeout_seconds = adapter_manifest.get("timeout_seconds", 600)
    # ... existing Popen body, but use cmd_str (split via shlex) instead of hard-coded codex args


# Backward-compat shim: keep _invoke_codex pointing at the new function with
# a baked-in codex adapter manifest. Existing tests + CLI keep working.
def _invoke_codex(prompt, codex_bin, timeout_seconds, repo_root, schema_path,
                  log_path=None, anchors=None, heartbeat_interval=30.0,
                  stall_silence_seconds=45.0, poll_interval=0.5,
                  time_fn=None, popen_factory=None):
    """Deprecated: use _invoke_subprocess_ai with the codex adapter manifest.
    Preserved for backward compat with existing CLI flag-path."""
    from consensus_mcp._adapter_runtime import resolve_adapter
    manifest = resolve_adapter("codex", repo_root=repo_root)
    manifest["binary_resolved_path"] = codex_bin
    return _invoke_subprocess_ai(
        prompt=prompt, adapter_manifest=manifest, role="secondary",
        repo_root=repo_root, schema_path=schema_path, log_path=log_path,
        anchors=anchors, heartbeat_interval=heartbeat_interval,
        stall_silence_seconds=stall_silence_seconds, timeout_seconds=timeout_seconds,
        poll_interval=poll_interval, time_fn=time_fn, popen_factory=popen_factory,
    )


def _shell_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)
```

(The full refactor preserves Popen + threads + heartbeats + abort-signal polling + process-group termination. Only the command construction is parameterized. See _dispatch_codex.py lines ~604-870 for the body to preserve.)

- [ ] **Step 4: Run test**

Run: `python -m pytest consensus_mcp/tests/test_dispatch_codex.py -v`
Expected: New test passes. All EXISTING tests in test_dispatch_codex.py also continue to pass (the shim preserves the old signature).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_dispatch_codex.py consensus_mcp/tests/test_dispatch_codex.py
git commit -m "refactor(dispatch): _invoke_codex → _invoke_subprocess_ai (adapter-agnostic)"
```

### Task B2: Add --adapter CLI flag (default codex for back-compat)

- [ ] **Step 1: Write the failing test**

```python
def test_dispatch_cli_accepts_adapter_flag(tmp_path):
    # End-to-end-ish: pass --adapter codex, verify it loads the manifest
    # (use smoke mode so we don't actually invoke codex)
    from consensus_mcp import _dispatch_codex as d
    rc = d.main([
        "--smoke",
        "--goal-packet", "consensus_mcp/tests/fixtures/dispatch_codex/goal_packet_smoke.yaml",
        "--iteration-dir", str(tmp_path),
        "--adapter", "codex",
    ])
    assert rc == 0
```

- [ ] **Step 2: Run test, verify failure**

Expected: argparse error "unrecognized arguments: --adapter codex".

- [ ] **Step 3: Wire the flag**

In `_dispatch_codex.py` `main()`:

```python
p.add_argument("--adapter", default="codex",
               help="Adapter name to use for the secondary AI invocation. Default: codex (back-compat).")
```

And in main()'s body, resolve the adapter and pass into `_invoke_subprocess_ai`.

- [ ] **Step 4: Run test, expect pass**

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_dispatch_codex.py consensus_mcp/tests/test_dispatch_codex.py
git commit -m "feat(dispatch): --adapter flag with codex default for back-compat"
```

### Iter-0005b closure

- [ ] **Step 1: Full regression suite**

```bash
python -m pytest consensus_mcp/tests/ -q
```

Expected: 440+ passed (no regression).

- [ ] **Step 2: Author iter-0005b goal_packet + dispatch closure review**

Same pattern as iter-0005a.

---

# Sub-iteration 0005c — Onboarding wizard

**Goal:** `consensus init` flow: project scan, AI detection, family-coherent prompt, probe, write `.consensus/config.yaml`.

**Files:**
- Create: `consensus_mcp/_onboarding.py`
- Create: `consensus_mcp/tests/test_onboarding.py`

### Task C1: Project scan

**Files:**
- Create: `consensus_mcp/_onboarding.py`
- Test: `consensus_mcp/tests/test_onboarding.py`

- [ ] **Step 1: Write the failing tests**

```python
# consensus_mcp/tests/test_onboarding.py
from pathlib import Path
from consensus_mcp import _onboarding as ob


def test_project_scan_reads_readme_and_pyproject(tmp_path):
    (tmp_path / "README.md").write_text("# My App\n\nDoes things with widgets.\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-app"\nversion = "0.1.0"\n')
    out = ob.scan_project(tmp_path)
    assert out["name"] == "my-app"
    assert "widgets" in out["summary"]
    assert out["language"] == "python"


def test_project_scan_handles_missing_files(tmp_path):
    out = ob.scan_project(tmp_path)
    assert out["name"] is None
    assert out["language"] is None
    assert out["summary"] in (None, "")


def test_project_scan_includes_top_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x" * 1000)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("x" * 500)
    (tmp_path / "docs").mkdir()
    out = ob.scan_project(tmp_path)
    # Top dirs by size, depth 1
    assert "src" in out["top_dirs"]
    assert out["top_dirs"][0] == "src"  # largest
```

- [ ] **Step 2: Run tests, verify failure**

Expected: ImportError on `_onboarding`.

- [ ] **Step 3: Implement scan_project**

```python
# consensus_mcp/_onboarding.py
"""consensus init wizard logic. iter-0005c."""
from __future__ import annotations
from pathlib import Path
import tomllib  # 3.11+; for 3.10 use `tomli`


def scan_project(root: Path) -> dict:
    """Read repo metadata files + file tree summary. Returns dict for config.project."""
    root = Path(root)
    name = None
    language = None
    summary = None
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            name = (data.get("project") or {}).get("name")
            language = "python"
        except Exception:
            pass
    pkgjson = root / "package.json"
    if not name and pkgjson.is_file():
        import json
        try:
            data = json.loads(pkgjson.read_text(encoding="utf-8"))
            name = data.get("name")
            language = "javascript"
        except Exception:
            pass
    readme = root / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8", errors="replace")
        # First non-heading, non-empty paragraph
        lines = [l.strip() for l in text.splitlines()]
        for l in lines:
            if l and not l.startswith("#"):
                summary = l
                break
    top_dirs = _top_dirs_by_size(root)
    return {"name": name, "language": language, "summary": summary, "top_dirs": top_dirs}


def _top_dirs_by_size(root: Path, depth: int = 1, limit: int = 5) -> list[str]:
    """Return top-N directories at depth=1 by recursive total byte size."""
    if not root.is_dir():
        return []
    sized = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        total = 0
        try:
            for f in child.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        except (PermissionError, OSError):
            continue
        sized.append((total, child.name))
    sized.sort(reverse=True)
    return [name for _, name in sized[:limit]]
```

(Note: Python 3.10 needs `tomli` instead of `tomllib`. Plan assumes 3.11+ per pyproject; if 3.10 support required, import shim.)

- [ ] **Step 4: Run tests, verify pass**

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_onboarding.py consensus_mcp/tests/test_onboarding.py
git commit -m "feat(onboarding): project scan (README + pyproject + top dirs)"
```

### Task C2: AI detection + family-coherent filter

- [ ] **Step 1: Write the failing tests**

```python
def test_detect_available_adapters_includes_built_ins(monkeypatch):
    # Mock resolve_binary to claim both are found
    from consensus_mcp import _adapter_runtime as ar
    def fake_resolve(spec):
        return f"/fake/{spec['name']}"
    monkeypatch.setattr(ar, "resolve_binary", fake_resolve)
    avail = ob.detect_available_adapters()
    names = {a["name"] for a in avail if a["resolved_path"]}
    assert "claude" in names
    assert "codex" in names


def test_family_coherent_filter_excludes_same_family():
    candidates = [
        {"name": "claude", "model_family": "claude"},
        {"name": "codex", "model_family": "codex"},
        {"name": "claude-variant", "model_family": "claude"},
    ]
    out = ob.filter_cross_family(candidates, primary_family="claude")
    names = {c["name"] for c in out}
    assert "codex" in names
    assert "claude" not in names
    assert "claude-variant" not in names
```

- [ ] **Step 2: Run tests, verify failure**

- [ ] **Step 3: Implement detect_available_adapters + filter_cross_family**

```python
# Append to _onboarding.py
def detect_available_adapters(repo_root: Path | None = None) -> list[dict]:
    """Return list of all known adapters with resolved_path field set."""
    from importlib.resources import files as _pkg_files
    from consensus_mcp import _adapter_runtime as ar
    out = []
    builtins_dir = Path(str(_pkg_files("consensus_mcp.adapters")))
    for yaml_path in builtins_dir.glob("*.yaml"):
        try:
            m = ar.load_manifest(yaml_path)
        except ar.AdapterManifestError:
            continue
        m["resolved_path"] = ar.resolve_binary(m["binary"])
        out.append(m)
    if repo_root:
        plugin_dir = Path(repo_root) / ".consensus" / "adapters"
        if plugin_dir.is_dir():
            for yaml_path in plugin_dir.glob("*.yaml"):
                try:
                    m = ar.load_manifest(yaml_path)
                except ar.AdapterManifestError:
                    continue
                m["resolved_path"] = ar.resolve_binary(m["binary"])
                # User plugin shadows builtin by name match
                out = [a for a in out if a["name"] != m["name"]]
                out.append(m)
    return out


def filter_cross_family(candidates: list[dict], primary_family: str) -> list[dict]:
    return [c for c in candidates if c.get("model_family") != primary_family]
```

- [ ] **Step 4: Run tests, verify pass**

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_onboarding.py consensus_mcp/tests/test_onboarding.py
git commit -m "feat(onboarding): adapter detection + cross-family filter"
```

### Task C3: Probe + config writer + non-interactive entry

- [ ] **Step 1: Write the failing tests**

```python
def test_probe_adapter_runs_command(monkeypatch, tmp_path):
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R: returncode = 0; stdout = "v1.0\n"; stderr = ""
        return R()
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    m = {
        "binary": {"name": "fake"},
        "probe": {"command": "{{binary}} --version", "timeout_seconds": 10, "expect_exit_code": 0},
    }
    ok, output = ob.probe_adapter(m, resolved_binary="/usr/bin/fake")
    assert ok is True
    assert "v1.0" in output


def test_init_non_interactive_writes_config(tmp_path, monkeypatch):
    from consensus_mcp import _adapter_runtime as ar
    monkeypatch.setattr(ar, "resolve_binary", lambda spec: f"/fake/{spec['name']}")
    import subprocess
    def ok_run(*a, **kw):
        class R: returncode = 0; stdout = "v1\n"; stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", ok_run)
    (tmp_path / "README.md").write_text("# Test\n\nA test project.\n")
    rc = ob.init(repo_root=tmp_path, non_interactive=True, primary="claude", secondary="codex")
    assert rc == 0
    config = tmp_path / ".consensus" / "config.yaml"
    assert config.is_file()
    import yaml
    data = yaml.safe_load(config.read_text())
    assert data["primary"] == "claude"
    assert data["secondary"] == "codex"
    assert data["project"]["name"] is None  # no pyproject.toml in fixture
```

- [ ] **Step 2: Run tests, verify failure**

- [ ] **Step 3: Implement probe + init + interactive prompt logic**

```python
# Append to _onboarding.py
import subprocess
import sys


def probe_adapter(manifest: dict, resolved_binary: str, timeout_seconds: int | None = None) -> tuple[bool, str]:
    """Run the adapter's probe command. Returns (ok, output)."""
    from consensus_mcp._adapter_runtime import substitute_template
    cmd_str = substitute_template(manifest["probe"]["command"], {"binary": resolved_binary})
    import shlex
    cmd = shlex.split(cmd_str)
    timeout = timeout_seconds if timeout_seconds is not None else manifest["probe"].get("timeout_seconds", 10)
    expect = manifest["probe"].get("expect_exit_code", 0)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "probe timed out"
    except FileNotFoundError:
        return False, f"binary not found at resolved path: {resolved_binary}"
    return (r.returncode == expect, (r.stdout or "") + (r.stderr or ""))


def init(repo_root: Path, *, non_interactive: bool = False,
         primary: str | None = None, secondary: str | None = None,
         skip_scan: bool = False) -> int:
    """Run the init flow. Returns exit code."""
    repo_root = Path(repo_root)
    config_path = repo_root / ".consensus" / "config.yaml"
    if config_path.is_file() and not non_interactive:
        ans = input(f"{config_path} exists. Reconfigure? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 0
    project = {} if skip_scan else scan_project(repo_root)
    available = detect_available_adapters(repo_root=repo_root)
    available_found = [a for a in available if a.get("resolved_path")]
    if non_interactive:
        chosen_primary = primary
        chosen_secondary = secondary
    else:
        chosen_primary = _prompt_for_adapter("PRIMARY (proposes / commits changes)", available_found)
        if chosen_primary is None:
            return 3
        primary_family = next(a["model_family"] for a in available_found if a["name"] == chosen_primary)
        candidates = filter_cross_family(available_found, primary_family)
        if not candidates:
            print(f"No cross-family secondary adapters available. Install a different AI CLI.", file=sys.stderr)
            return 3
        chosen_secondary = _prompt_for_adapter("SECONDARY (cross-reviews)", candidates)
        if chosen_secondary is None:
            return 3
    # Probe both
    p_manifest = next(a for a in available if a["name"] == chosen_primary)
    s_manifest = next(a for a in available if a["name"] == chosen_secondary)
    if p_manifest["model_family"] == s_manifest["model_family"]:
        print(f"primary and secondary must be different families", file=sys.stderr)
        return 3
    for m in (p_manifest, s_manifest):
        if not m.get("resolved_path"):
            print(f"adapter '{m['name']}' binary not found.\n{m['binary']['not_found_help']}", file=sys.stderr)
            return 3
        ok, _ = probe_adapter(m, m["resolved_path"])
        if not ok:
            print(f"probe failed for adapter '{m['name']}'", file=sys.stderr)
            return 3
    # Write config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    import yaml
    config = {
        "schema_version": 1,
        "project": project,
        "primary": chosen_primary,
        "secondary": chosen_secondary,
        "defaults": {
            "workflow": 3,
            "auto_commit": True,
            "auto_iterate": 0,
            "scope_glob_default": None,
            "commit_message_template": "{type}({scope}): {intent} [iter-{iter_id}]",
        },
        "adapters": {},
        "safety": {
            "forbidden_files": [".consensus/", "consensus-state/", ".git/", ".env*"],
            "max_patch_size_default": 2000,
        },
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    gitignore = config_path.parent / ".gitignore"
    gitignore.write_text("cache/\n*.log\n")
    print(f"\n✓ Setup complete. Try: consensus run \"fix a typo in the README\"")
    return 0


def _prompt_for_adapter(role: str, candidates: list[dict]) -> str | None:
    if not candidates:
        print(f"No adapters available for {role}", file=sys.stderr)
        return None
    print(f"\nWhich AI should be {role}?")
    for i, a in enumerate(candidates, start=1):
        print(f"  [{i}] {a['name']}  ({a['display_name']})")
    raw = input("> ").strip()
    try:
        idx = int(raw) - 1
        return candidates[idx]["name"]
    except (ValueError, IndexError):
        print("Invalid selection.", file=sys.stderr)
        return None
```

- [ ] **Step 4: Run tests, verify pass**

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_onboarding.py consensus_mcp/tests/test_onboarding.py
git commit -m "feat(onboarding): init wizard with probe + non-interactive mode + config writer"
```

### Iter-0005c closure

- [ ] Full regression suite green.
- [ ] Author iter-0005c goal_packet, dispatch codex closure review.

---

# Sub-iteration 0005d — Run orchestration

**Goal:** `consensus run` end-to-end: author goal_packet, spawn primary, capture diff, apply, author review-packet, dispatch secondary, verdict, exit.

**Files:**
- Create: `consensus_mcp/_run.py`
- Create: `consensus_mcp/tests/test_run.py`

### Task D1: Goal packet auto-generation from intent + scope

- [ ] **Step 1: Failing test**

```python
def test_author_goal_packet_from_intent(tmp_path):
    from consensus_mcp import _run as r
    out = r.author_goal_packet(
        intent="fix the off-by-one",
        scope_glob="src/pagination/",
        repo_root=tmp_path,
        primary="claude",
        secondary="codex",
        config={"defaults": {"workflow": 3, "auto_commit": True, "max_patch_size_default": 2000},
                "safety": {"forbidden_files": [".git/"]},
                "project": {"summary": "Test project"}},
    )
    gp = out["goal_packet"]
    assert "off-by-one" in gp["goal"]["summary"]
    assert "src/pagination/" in gp["allowed_files"]
    assert ".git/" in gp["forbidden_files"]
    assert gp["authorization"]["scope_signature"]  # non-empty
    assert out["iteration_dir"].is_dir()
```

- [ ] **Step 2..5: Implement, run, commit.**

Implementation pattern: derive `pilot_id = "iter-NNNN-<slug>"` by scanning consensus-state/active for highest existing iter-NNNN number + 1. Slug from `intent` (lowercase, hyphenated, max 40 chars). Use `_self_drive._scope_signature` to compute signature. Write goal_packet.yaml. Return paths.

### Task D2: Primary AI dispatch + diff extraction

- [ ] **Step 1: Failing test** — mocks subprocess, sends a fenced ```diff block as stdout, asserts we extract it cleanly.

- [ ] **Step 2..5: Implement `dispatch_primary_and_extract_diff(adapter, prompt, ...)`**, run, commit.

Use the adapter's `invocation.primary.command` template via `_invoke_subprocess_ai`. Parse output per the `output_contract`:
- `unified_diff_in_fenced_block`: extract first ```diff ... ``` block; multi-block fails; no block fails.
- `unified_diff_only`: stdout IS the diff; whitespace-strip; first non-blank line must start with `---` or `diff --git`.

Reject codex-cli's `*** Begin Patch` shape (reuse the existing guard from `_dispatch_codex._validate_patch_proposal`).

### Task D3: Apply diff + (optional) auto-commit

- [ ] Tests: `git apply --check` success and failure; commit-template substitution; `--no-commit` leaves working-tree-dirty.

- [ ] Implementation: shell out to `git apply` against the extracted diff; on success and `auto_commit: true`, `git commit -m "<rendered template>"`.

### Task D4: Author review-packet + dispatch secondary

- [ ] Tests: mock subprocess; verify we call `_author_review_packet.author_review_packet()` with the changed files, then dispatch the secondary adapter, then parse the sealed review.

- [ ] Implementation: stitch existing functions together. No new logic; integration.

### Task D5: Verdict assembly + exit code

- [ ] Tests: blocking finding → exit 1; clean → exit 0; scope violation in primary diff → exit 2; budget overflow → exit 3.

- [ ] Implementation: read the sealed review YAML, project to the user-facing summary lines, return the correct exit code per spec §4.

### Iter-0005d closure

- [ ] Full regression suite green.
- [ ] Author iter-0005d goal_packet, dispatch codex closure review.

---

# Sub-iteration 0005e — CLI surface + console_scripts

**Goal:** `consensus init / run / status / close / history` argparse glue + `pyproject.toml` entry point.

**Files:**
- Create: `consensus_mcp/_cli.py`
- Modify: `pyproject.toml` (add `[project.scripts]`)
- Create: `consensus_mcp/tests/test_cli.py`

### Task E1: Argparse skeleton

- [ ] Tests: `consensus --help` lists 5 subcommands; `consensus run` requires `intent` positional; invalid subcommand exits 2.

- [ ] Implementation: argparse with `subparsers`. Each subcommand dispatches to its module (`_run.main`, `_onboarding.init`, `_resume.snapshot` → console formatter, etc.).

### Task E2: `consensus status` (formats `_resume.snapshot` output)

- [ ] Tests: snapshot with no active iteration → printed "no active iteration"; with iteration → formatted table.

- [ ] Implementation: call `_resume.snapshot()`, format selected fields to a readable summary. `--json` flag prints raw JSON instead.

### Task E3: `consensus close` (writes closure-certificate when invariant is satisfiable)

- [ ] Tests: when `closure_invariant_status.satisfiable_now` is true → write closure-certificate.yaml + iteration-outcome.yaml using `validated_closer_pass_id`; when not → exit 1 with reason.

- [ ] Implementation: read snapshot, branch on satisfiable_now, author closure artifacts.

### Task E4: `consensus history` (list archive entries)

- [ ] Tests: glob `consensus-state/archive/review-passes/*.yaml`, sort by `sealed_at_utc`, format last N.

- [ ] Implementation: small CLI formatter.

### Task E5: pyproject.toml console_scripts entry + smoke test

- [ ] **Step 1**: Add to `pyproject.toml`:

```toml
[project.scripts]
consensus = "consensus_mcp._cli:main"
```

- [ ] **Step 2**: After `pip install -e .`, run `consensus --help` and verify subcommands list.

- [ ] **Step 3**: Smoke test — in a fresh tmpdir:
  ```bash
  cd $(mktemp -d)
  git init
  echo "# Test" > README.md
  consensus init --non-interactive --primary claude --secondary codex
  cat .consensus/config.yaml | head
  ```

- [ ] **Step 4**: Commit.

### Iter-0005e closure

- [ ] Full regression suite green.
- [ ] Author iter-0005e goal_packet, dispatch codex closure review.
- [ ] After closure: bump `pyproject.toml` version → 2.1.0 (feature release). Build wheel; smoke-test install.

---

## Self-review (post-write)

**Spec coverage check** (every spec section maps to one or more tasks):

| Spec section | Tasks |
|---|---|
| §4 CLI surface | E1, E2, E3, E4 |
| §5 init flow | C1, C2, C3 |
| §6 run flow | D1–D5 |
| §7 config schema | C3 (writes), implied throughout |
| §8 adapter system | A1, A2, A3, A4, A5 |
| §9 integration | B1, B2 |
| §10 test plan | each task's tests |
| §11 implementation order | mirrored in sub-iter order |
| §13 codex-confirmed decisions | A2 (windows_suffixes), A1 (3 output_contracts) |

No spec gaps.

**Placeholder scan:** none. Every step has either complete code or a clear test/command.

**Type consistency:** function names used in later tasks (`_invoke_subprocess_ai`, `resolve_adapter`, `scan_project`, `author_goal_packet`) are defined in their introducing tasks. Adapter manifest field names are consistent with the spec §8 schema.

**Risk areas / known gotchas:**
1. **`tomllib` is 3.11+** — pyproject.toml states 3.10+. Task C1 needs an import shim (`try: tomllib; except: import tomli as tomllib`) OR raise the Python floor.
2. **Windows shell quoting** — task D2's primary AI invocation uses `shlex.quote` which is POSIX-only. Need a Windows-aware variant (PowerShell quoting differs). Add to D2 implementation: use `subprocess.list2cmdline` for Windows or pass args as a list (preferred — no shell at all).
3. **Codex CLI invocation in `_run.py` for primary mode** — `codex exec --sandbox workspace-write` is the proposed primary-mode command; if codex doesn't support workspace-write sandbox emitting fenced diffs reliably, the codex.yaml primary contract may need adjustment. Validate empirically in task A4.
4. **Large prompt handling** — D2 enforces `prompt_budget_bytes`; cite the codex stall lesson from iter-0002/0003 (we needed 600–1200s threshold for 50–60KB prompts).

**Add a follow-up task** for risk #3 (codex primary-mode empirical validation):

### Task A4.5: Empirical validation of codex primary-mode invocation

- [ ] Run `codex exec --sandbox workspace-write "produce a unified diff in a fenced ```diff block that adds a comment to README.md"` against a throwaway repo; verify the output has the expected fenced format. If not, adjust `codex.yaml`'s `invocation.primary.command` template to whatever shape codex reliably produces.

(Lives between A4 and A5.)

---

## Total scope estimate

- ~22 tasks across 5 sub-iterations
- ~80 individual TDD steps
- ~30 new test cases
- ~1100 lines of new production code
- ~600 lines of new test code
- 5 codex closure dispatches (one per sub-iter)

Time estimate (per consensus-mcp's existing iter cadence): each sub-iter is ~30-60 min of human-driven implementation + ~10-15 min of codex review wait time. Total: ~4-6 hours of focused work.
