# State schema

Minimal layout of the on-disk state the MCP server reads and writes.
Derived from spec section 13 (consensus schema), section 16 (verification),
and section 17 (production gates).

## Repo layout

```
consensus-state/
  active/
    iteration-NNNN-<slug>/
      input.yaml              # operator brief
      review-packet.yaml      # built per spec section 13
      <reviewer>-review.yaml  # one per reviewer (codex, claude)
      independence-audit.yaml # canonical events; T3 appends here
      consensus.yaml          # post-review consensus; spec section 13
      verification.yaml       # post-implementation verification
      iteration-outcome.yaml  # closing record
      shippable-boundary.md   # what files this iteration may touch
  archive/
    review-passes/
      index.yaml                       # T6 appends here
      YYYY-MM-DD-iteration-NNNN-...-pass.yaml  # T6 writes here
  state/
    disposition-ledger.yaml   # T2 reads, T8 writes
    mcp-server-audit.jsonl    # server boot/stop log
```

## consensus.yaml minimum required

T11 (`gate.evaluate_production_with_scope_match`) requires:

```yaml
production_scope:
  scope_value: <exact string or prefix>   # required
  scope_match_mode: exact | prefix         # required
implementation_scope:
  allowed_sections: [<spec-md-section>, ...]   # primary scope source (v1.9.2+)
  allowed_files: [<glob>, ...]                  # legacy fallback (used when allowed_sections absent)
  forbidden_files: [<glob>, ...]
  allowed_actions: [...]
  forbidden_actions: [...]
canonical_findings:
  - id: <stable-id>
    source: codex | claude
    summary: <text>
    severity: low | medium | high | blocking | critical
    status: accepted_for_v1_X_apply | deferred_... | rejected
    landed_in: <effect>
```

T10 (`repo.set_section`) reads `consensus.implementation_scope.allowed_sections`
as the primary source of truth for which spec-md sections may be written
(v1.9.2+). When `allowed_sections` is absent, T10 falls back to
`consensus.implementation_scope.allowed_files` (glob-style, e.g.,
`scripts/foo/**`) as a legacy compatibility path.

## review packet (sealed by T6)

Required fields enforced by `review.write_and_seal`:

```yaml
iteration_id: <str>
reviewer_id: <str>
pass_id: <str>            # unique; index-enforced
packet: {...}             # full reviewer output
packet_sha256: <hex>      # COMPUTED by T6 (self-hash exception); do not pre-fill
```

The deterministic archive path is
`archive/review-passes/<YYYY-MM-DD>-<iteration_id>-<reviewer_id>-pass.yaml`.
T6 refuses path collisions (no overwrite).

T7 (`review.read_post_seal`) re-computes the SHA via:

```python
canonical = yaml.safe_dump(
    {k: v for k, v in packet.items() if k != "packet_sha256"},
    sort_keys=True,
)
sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

`legacy_unsealed=True` (with `verified=False`) means a pre-T6 packet that
was archived without `packet_sha256`. Not a corruption indicator; expected
for archive entries pre-Phase-1.

## independence-audit.yaml

T3 (`audit.append_event`) appends one record per call. Canonical event
types only (rejects unknown / per-agent-prefixed types). Each record:

```yaml
- event_id: <uuid-or-counter>
  timestamp_utc: <iso8601>
  event_type: <canonical>
  actor: <agent_id>
  artifact: <path|null>
  sha256: <hex|null>
  # event-specific named fields per spec section 13
  extra_fields: {...}     # only when no canonical name fits
```

`audit_append_event` returns `audit_yaml_post_sha256` so callers can
include the post-write SHA in their own iteration record.

## disposition-ledger.yaml

T2 reads (cached on `mtime_ns`), T8 writes. Must validate clean against
`consensus_mcp/validators/validate_disposition_index.py` post-write or T8
refuses. Layout per spec section 17:

```yaml
spec_version: <semver>      # e.g. 1.9.3-rc
v1_X_application_log:
  - finding_id: <id>
    landed_at: <iso8601>
    landed_in: <artifact>
findings:
  <finding_id>:
    spec_section: <int>
    status: open | dispositioned | applied | deferred
    disposition: <text>
```

## verification.yaml

Filled post-implementation (typically by T5 `apply_consensus_patch` plus
manual operator updates). T11 reads this for production-readiness checks.
Required fields per spec section 16:

```yaml
implementation_landed: <bool>
checks:
  smoke_test_pass: <bool>
  validator_test_pass: <bool>
  frontmatter_valid: <bool>
target_sha256: <hex>        # commit or artifact hash
```

T11 compares `current_target_sha256` against `approval.yaml`'s approved
SHA (operator-protected file outside the repo). Mismatch -> blocked or
ready_pending_operator_approval per `scope_match_mode`.

## Codex review packet shape (auto-dispatch)

When `_dispatch_codex` invokes codex CLI, codex is constrained via `--output-schema`
to emit JSON matching `consensus_mcp/dispatch_templates/codex_review_schema.json`:

```json
{
  "findings": [
    {
      "id": "codex-rev-NNN",
      "severity": "low | medium | high | blocking | critical",
      "summary": "<one-line>",
      "citation": "<file:line>",
      "risk": "<impact if not fixed>",
      "recommendation": "<concrete action>"
    }
  ],
  "goal_satisfied": true,
  "goal_satisfied_rationale": "<why>",
  "blocking_objections": []
}
```

`_dispatch_codex` parses this JSON via `_parse_codex_output`, wraps it in T6's
required outer structure (iteration_id, reviewer_id, pass_id, findings), and
calls T6 to seal. The seal writes to T6's archive path under
`consensus-state/archive/review-passes/`; `_seal_via_t6` then mirrors the sealed YAML
to `<iteration_dir>/codex-review.yaml`.

**Local schema validation (v1.10.1+, F2 hardening):** even though codex is
invoked with `--output-schema`, `_parse_codex_output` ALSO performs
defense-in-depth validation: required top-level keys (findings, goal_satisfied,
blocking_objections), boolean `goal_satisfied`, list `blocking_objections`,
severity in the 5-value enum, finding id matching `^codex-rev-\d+$`, and all
required finding fields (id, severity, summary). Malformed output is rejected
before T6 sealing.

**Sealed packet provenance (v1.10.1+, F5 hardening):** the sealed packet
includes a `dispatch_provenance` block keyed by codex_version, prompt_sha256,
output_sha256, schema_sha256, goal_packet_sha256, scope_signature. T6 hashes
the whole packet, so the sealed YAML is independently verifiable without
consulting `dispatch-log.jsonl`.

Codex is invoked with reviewer-safe flags: `--cd <repo_root> --sandbox read-only
--skip-git-repo-check --output-schema <schema.json> -o <tempfile> -`. Sandbox
prevents codex from mutating the workspace during review.

**Real-codex smoke env-gate (v1.10.1+, F3 hardening):** the `--smoke` CLI flag
is gated by the `CONSENSUS_MCP_RUN_REAL_CODEX_SMOKE=1` environment variable.
If `--smoke` is passed without the env var set, `main()` refuses early with
exit code 3 and logs a `dispatch_refused` event before any codex invocation.
This prevents accidental real-codex calls under cost/auth/session implications.

**Review-target prompt fields (v1.10.1+, F6 hardening):** the prompt template
includes a "# Review target" section with placeholders `{iteration_dir}`,
`{review_packet_path}`, `{review_target_path}`, `{review_target_hash}`. The
`--review-target <path>` CLI arg points the helper at the file codex should
review (a saved diff, patch, or any input); `_dispatch_codex` reads it, computes
sha256, and threads both path + hash into the prompt. When `--review-target` is
not provided, placeholders render as `(not specified)` and the template
instructs codex to fall back to `allowed_files` with a note (severity medium,
"review target not provided").

Each invocation appends two events to `consensus-state/state/dispatch-log.jsonl`:
- `dispatch_start` with timeout_seconds, codex_bin, schema_path
- `dispatch_done` with full provenance: codex_version, prompt_sha256,
  output_sha256, schema_sha256, goal_packet_sha256, scope_signature,
  reviewer_id, pass_id, timeout_seconds, exit_code, sealed_path, packet_sha256
- `dispatch_refused` (only on `--smoke` without env-gate set) with
  error_type=smoke_env_gate, error message naming the required env var

KNOWN ISSUE (carry-forward 2026-05-09): T6's `ARCHIVE_DIR` resolves at module
import time. If `CONSENSUS_MCP_REPO_ROOT` is set AFTER first import of T6 (e.g.,
in a test fixture), archive writes leak to the real repo. Mitigation: set the
env var BEFORE any consensus_mcp import. Hardening to call-time resolution is
v1.10.x followup. Test suites use unique pass_ids to avoid T6 archive index
collisions across the session.

RESOLVED 2026-05-09 (v1.10.2 F5): the prior known-issue wording was incorrect.
Lex-sort actually puts `1.10.1` BEFORE `1.9.3rc0` (`'1' < '9'` at position 2 of
the version string), so `sorted(dist.glob(...))[-1]` would mis-pick `1.9.3rc0`
as the "latest" wheel - exactly the wrong outcome. v1.10.2's `gate_install`
pre-cleans `dist/` before each build so the post-build wheel is the only one
present, sidestepping the lex-sort bug entirely.
