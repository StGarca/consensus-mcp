# Tool reference

10 MCP tools (T2-T11). All callable via JSON-RPC 2.0 `tools/call` with the
tool name and `arguments` mapping. Schemas summarized; see `tools/*.py`
SCHEMA constants for full JSONSchema with descriptions.

Asterisk (*) marks required input fields.

## state.read_decision_ledger (T2)

Read the disposition ledger. Cached; auto-invalidates when the ledger file
changes on disk. Returns ledger content as YAML string plus canonical
SHA-256.

- Inputs: none
- Outputs: `ledger_yaml`, `ledger_sha256`
- Failure: `error="ledger not found"` if file missing.

## audit.append_event (T3)

Append a canonical event to an iteration's `independence-audit.yaml`.

- Inputs: `iteration_id*`, `event_type*`, plus event-specific named fields
  (`actor`, `artifact`, `sha256`, `independence_attestation`,
  `sealed_inputs`, `staging_dir`, `validator`, `effect`, `closing_state`,
  `note`, `validator_status`, `invocation_protocol`, `consensus_state`,
  `authorization_form_response`, `interpretation`, `files_modified`,
  `findings`, `gate_decision`, `governance_milestone_closed`,
  `next_constraint`, `staged_files`), and `extra_fields` catch-all.
- Outputs: `event_id`, `audit_yaml_post_sha256`
- Failure: rejects non-canonical `event_type`; rejects per-agent-prefixed
  `event_type` with canonical hint.

## patch.stage_and_dry_run (T4 - G2)

Stage proposed patches to a temp dir, run validators on the hypothetical
post-edit state, return findings + gate decision per canonical-006
anti-regression rule.

- Inputs: `iteration_id` (nullable for spec-only patches),
  `proposed_patches*` (array), `validators_to_run` (array; default all 4)
- Outputs: `staging_dir_used`, `dry_run_findings`, `gate_decision`
  (APPROVED | BLOCKED), `dry_run_isolation_caveats`
- Failure: validator import or staging IO error.

## patch.apply_consensus_patch (T5 - G2)

Gate-then-apply: runs `patch.stage_and_dry_run`; if APPROVED and no
high/blocking findings, atomically writes patches into the iteration dir
and emits an `apply_step_landed` event. Refuses on BLOCKED or any
high/blocking/critical finding.

- Inputs: `iteration_id*`, `patches*` (object: relpath -> full file
  contents), `rationale*`, `require_dry_run_clean` (default True),
  `validators_to_run` (default all 4)
- Outputs: `applied_files`, `audit_event_id`, `dry_run_summary`
- Failure: `iteration_not_found`, `path_traversal`, BLOCKED dry run,
  high/blocking finding.

## review.write_and_seal (T6 - G1)

Seal a review packet and register it in `archive/review-passes/index.yaml`.
Computes `packet_sha256` (self-hash exception: hashes packet sans the
`packet_sha256` field), writes atomically to a deterministic path, appends
a `review_returned_and_sealed` audit event.

- Inputs: `iteration_id*`, `reviewer_id*`, `pass_id*`, `packet*`
- Outputs: `archive_path`, `packet_sha256`, `index_post_sha256`,
  `audit_event_id`
- Failure: `path_collision` if deterministic path exists; missing required
  packet fields.

## review.read_post_seal (T7 - G1)

Read and verify a sealed review packet by `pass_id` (via index.yaml) or
`path`. Re-computes the canonical SHA via the self-hash exception and
compares to the stored value. Read-only.

- Inputs: exactly one of `pass_id*` | `path*`
- Outputs: `packet`, `recorded_sha256`, `computed_sha256`, `verified`,
  `legacy_unsealed`
- Failure: path outside archive refused; pass_id not found in index.

## state.update_decision_ledger (T8 - G5)

Validate-then-write the disposition ledger. Stages proposed YAML, runs
`validate_disposition_index` against the spec under the staged ledger,
commits atomic write iff post-write findings are zero.

- Inputs: `proposed_ledger_yaml*`, `consensus_yaml_sha256*`, `iteration_id`
- Outputs: `ledger_post_sha256`, `findings_post_write`, `audit_event_id`
- Failure: nonzero post-write findings -> refuses; real ledger bytes
  unchanged.

## repo.get_section (T9 - G3)

Section-aware read of a spec md region. Returns ONLY the requested section
(frontmatter or `section_N`). Refuses paths outside REPO_ROOT.

- Inputs: `file*`, `section_id*` ('frontmatter' or 'section_N')
- Outputs: `section_text`, `section_sha256`, `file`
- Failure: `section_not_found` -> includes `available` list of section ids.

## repo.set_section (T10 - G3)

Section-aware write of a spec md region. Refuses unless the section is in
`consensus.implementation_scope`, AND any other section would change as a
side effect (round-trip safety check).

- Inputs: `file*`, `section_id*`, `new_section_text*`,
  `consensus_yaml_sha256*`, `consensus_yaml_path*`, `iteration_id`
- Outputs: `file_post_sha256`, `audit_event_id`
- Failure: `unintended_section_change`, `section_not_in_scope`, missing
  consensus_yaml.

## gate.evaluate_production_with_scope_match (T11 - G4)

Read-only production-readiness evaluator. Reads `consensus.yaml`,
`verification.yaml`, `approval.yaml`; returns production_state with strict
scope-match (exact|prefix). Replaces lenient enum-membership scope check.

- Inputs: `consensus_yaml_path*`, `verification_yaml_path*`,
  `approval_yaml_path*`, `current_target_sha256*`,
  `scope_match_mode` (exact|prefix; default exact)
- Outputs: `production_state` (approved |
  ready_pending_operator_approval | blocked), `gate_findings`,
  `operator_production_scope_match_strict_check`
- Failure: `error="missing_production_scope"` if consensus lacks the field.
