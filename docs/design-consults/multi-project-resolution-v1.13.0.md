# Design consult: multi-project resolution for v1.13.0 (ship spec template + split spec/state roots)

**Context for reviewer:** consensus-mcp v1.12.0 boots cleanly when installed from a local checkout where `REPO_ROOT/docs/architecture/orchestration-spec.md` exists. It does NOT boot when installed as a frozen wheel into an end user's site-packages — the wheel doesn't ship the spec, so `_run_disposition_check` fails with `spec not found`.

This means: opening Claude Code in a random project (where consensus-mcp is registered as a user-scope MCP server backed by the frozen wheel) results in the server refusing to start. End users cannot use consensus-mcp on arbitrary projects without manual per-project setup (clone consensus-mcp into the project, write a project-scope `.mcp.json` setting `CONSENSUS_MCP_REPO_ROOT`).

The operator's stated requirement: consensus-mcp should "just work" in any project — open Claude Code anywhere, the MCP server boots clean, state auto-initializes in the project, codex can review the project's files. Dev folder (`C:\Users\<you>\Downloads\consensus-mcp`) is the single exception: it overrides via its existing project-scope `.mcp.json`.

## Current resolution logic (v1.12.0 `server.py`)

```python
def _resolve_repo_root() -> Path:
    override = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent  # site-packages parent for frozen wheels

REPO_ROOT = _resolve_repo_root()
SPEC_PATH = REPO_ROOT / "docs" / "architecture" / "orchestration-spec.md"
AUDIT_LOG = REPO_ROOT / "consensus-state" / "state" / "mcp-server-audit.jsonl"
```

REPO_ROOT controls THREE concerns simultaneously: where the spec lives, where state lives, where reviewable files are referenced from. For frozen wheels, REPO_ROOT resolves to site-packages, which has no spec and no state directory.

## Proposed v1.13.0 design

### Two new functions, split out from `_resolve_repo_root`

```python
def _resolve_spec_path() -> Path:
    """Spec path resolution: env override > legacy REPO_ROOT > packaged template."""
    override = os.environ.get("CONSENSUS_MCP_SPEC_PATH")
    if override:
        return Path(override).resolve()

    # Legacy: if REPO_ROOT is explicitly set, honor its spec location
    repo_root_env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if repo_root_env:
        legacy = Path(repo_root_env).resolve() / "docs" / "architecture" / "orchestration-spec.md"
        if legacy.exists():
            return legacy

    # Implicit: if running from a consensus-mcp checkout, use it
    walked = Path(__file__).resolve().parent.parent / "docs" / "architecture" / "orchestration-spec.md"
    if walked.exists():
        return walked

    # Fallback: shipped spec template inside the package
    return Path(__file__).resolve().parent / "spec_template.md"


def _resolve_state_root() -> Path:
    """State root resolution: env override > legacy REPO_ROOT > CWD."""
    override = os.environ.get("CONSENSUS_MCP_STATE_ROOT")
    if override:
        return Path(override).resolve()

    repo_root_env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if repo_root_env:
        return Path(repo_root_env).resolve() / "consensus-state"

    return Path.cwd() / "consensus-state"
```

`SPEC_PATH = _resolve_spec_path()` and `AUDIT_LOG = _resolve_state_root() / "state" / "mcp-server-audit.jsonl"`. The legacy `_resolve_repo_root()` stays for back-compat but isn't load-bearing anymore.

### Packaging change

`pyproject.toml`:

```toml
[tool.setuptools.package-data]
consensus_mcp = [
  "dispatch_templates/*.md",
  "dispatch_templates/*.json",
  "docs/*.md",
  "spec_template.md",          # NEW — shipped scrubbed spec
  "tests/fixtures/dispatch_codex/*",
]
```

`consensus_mcp/spec_template.md` is a copy of `docs/architecture/orchestration-spec.md` from the release branch (with section 24 already empty per v1.12.0 scrub). Maintained alongside the canonical spec; either copied at release time or kept synced via a build-time script.

### Behavior matrix after v1.13.0

| Environment | CONSENSUS_MCP_REPO_ROOT | CONSENSUS_MCP_STATE_ROOT | Spec path | State root |
|---|---|---|---|---|
| Dev folder (own .mcp.json) | dev folder | (unset) | dev/docs/architecture/orchestration-spec.md | dev/consensus-state/ |
| ChilipadScreen (frozen wheel, no env) | (unset) | (unset) | site-packages/consensus_mcp/spec_template.md | `D:\Projects\ChilipadScreen\consensus-state\` |
| Test folder (clone, run from it) | (unset, walks up) | (unset) | tools/consensus-mcp/docs/architecture/orchestration-spec.md | CWD/consensus-state/ |
| Shared-state mode (legacy user) | C:/Users/<you>/tools/consensus-mcp | (unset) | tools/consensus-mcp/docs/architecture/orchestration-spec.md | tools/consensus-mcp/consensus-state/ |

End user opens Claude Code in any project → frozen wheel boots against the shipped spec template, state directory auto-creates in the project's CWD on first audit-log append, codex sees the project's files. Zero per-project setup.

## Risks / tradeoffs

1. **Spec template drift.** If `consensus_mcp/spec_template.md` falls out of sync with `docs/architecture/orchestration-spec.md`, end users get a stale spec. Mitigation: copy at release time (manual now; automated via `make build` later) and add a CI check that `spec_template.md` matches the latest release-branch spec.

2. **CWD coupling.** Defaulting state to CWD means starting the server from the wrong directory pollutes that directory with `consensus-state/`. Mitigation: documented in CHANGELOG and the env var lets users pin if they want.

3. **Back-compat.** Existing users with `CONSENSUS_MCP_REPO_ROOT` set (dev folder, shared-state setups) keep working unchanged — the env var still drives both spec and state when set.

4. **Audit log location.** Currently `AUDIT_LOG = REPO_ROOT/consensus-state/state/mcp-server-audit.jsonl`. After this change, it becomes `_resolve_state_root()/state/mcp-server-audit.jsonl`. For dev folder + shared-state users, no change. For ChilipadScreen users, the audit log starts living in ChilipadScreen's tree (correct).

5. **`_release_gate_check.py` and other dev-only tools.** These have hardcoded paths relative to REPO_ROOT. They're for self-hosted release work, not end-user operations. Keep them unchanged; they only matter on the dev folder where REPO_ROOT is correctly pinned.

## Question

Is this design correct for v1.13.0?

Pick ONE option:
- **A**: Approve as proposed. Land the change in v1.13.0.
- **B**: Approve the spirit but the proposed implementation has a defect — explain the defect in `risk` and put the correct approach in `recommendation`.
- **C**: Reject — the design is wrong; propose a different approach (e.g., DON'T ship the spec, require manual init; OR split REPO_ROOT differently; OR something else).

Emit ONE finding. severity: low (design choice). `recommendation` carries the chosen option (A/B/C). `risk` carries the rationale or the defect description. No `patch_proposal` needed — the operator implements after the verdict.

Empty findings is NOT acceptable — the operator needs a verdict.
