# Known issue: pytest test-ordering flake in `test_dispatch_codex.py`

## Symptom

Five tests fail when running the full pytest suite but pass when run in isolation:

- `consensus_mcp/tests/test_dispatch_codex.py::test_main_smoke_with_mocked_codex`
- `consensus_mcp/tests/test_dispatch_codex.py::test_main_smoke_flag_with_env_proceeds`
- `consensus_mcp/tests/test_dispatch_codex.py::test_main_sealed_packet_embeds_dispatch_provenance`
- `consensus_mcp/tests/test_dispatch_codex.py::test_main_review_target_arg_threaded_through`
- `consensus_mcp/tests/test_dispatch_codex.py::test_dispatch_done_includes_archive_path_and_audit_id`

Full suite result on main as of v1.13.0: `5 failed, 502 passed, 1 skipped`. Same result on bare main with all v1.13.0 changes stashed — **the flake predates v1.13.0**.

## Root cause hypothesis (unconfirmed)

`test_dispatch_codex.py` invokes `_dispatch_codex.main([...])`, which calls `_dispatch_codex._resolve_repo_root()`. That resolver tries:
1. `CONSENSUS_MCP_REPO_ROOT` env var
2. `Path.cwd()` (must have repo markers `consensus-state/` and `consensus_mcp/`)
3. Walk parents of `__file__`

The failing tests scaffold a temp `tmp_path` with markers and `monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", tmp_path)`. That should resolve cleanly. The fact that they pass in isolation but fail in suite suggests some earlier test:
- Changes the process CWD via `os.chdir` and doesn't restore it, OR
- Mutates `os.environ` directly (bypassing monkeypatch) and leaves state behind, OR
- Imports/caches a module in a way that pins state at first-import time

`rc=2` (the failure mode) comes from `RepoRootResolutionError` in the early-fail path of `_dispatch_codex.main` (`_dispatch_codex.py:1588`).

## Reproduction

```bash
# Fails (in full-suite order):
py -3.11 -m pytest -q
# Passes (in isolation):
py -3.11 -m pytest consensus_mcp/tests/test_dispatch_codex.py -q
# Passes (single test):
py -3.11 -m pytest consensus_mcp/tests/test_dispatch_codex.py::test_main_smoke_with_mocked_codex -x
```

## Status

Not blocking v1.13.0 release — issue predates this branch and the production code is correct. Five tests pass independently; the failure is in test-environment state, not in `_dispatch_codex.main` or any v1.13.0 code.

## Fix sketch (for whichever release picks this up)

1. Bisect via `pytest --random-order` or by progressively shrinking the test set to find the polluting test.
2. Likely candidates: any test that uses `os.chdir` without `monkeypatch.chdir`, or any test that mutates `os.environ["CONSENSUS_MCP_REPO_ROOT"]` directly.
3. Switch the offender to `monkeypatch.chdir(...)` / `monkeypatch.setenv(...)` so pytest restores state at test end.
4. Add a session-level `conftest.py` autouse fixture that snapshots `os.getcwd()` and `os.environ` at session start and asserts they're restored between tests, to prevent regression.

Tracked outside any specific release — pick up when convenient.
