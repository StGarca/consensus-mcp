# gate_state_approved fixture

Drives `consensus_gate.py --self-test` toward terminal state `approved`.

## Files

- `consensus.yaml` - passes all five `production_ready_if` conditions
- `verification.yaml` - `passed: true` and `scope_check.passed: true`
- `approval.yaml` - operator approval YAML with embedded hashes
- `_build_gate_fixtures.py` - helper that regenerates `approval.yaml` with
  the canonical sha256 of the current `consensus.yaml` + `verification.yaml`

## Important: hashes are LOAD-BEARING

`approval.yaml.approved_consensus_sha256` and `approved_verification_sha256`
are the canonical YAML hashes of the sibling files at fixture-write time.
If you edit `consensus.yaml` or `verification.yaml` here, the hashes drift
and the self-test will fail.

To regenerate after editing:

```
python_env\python.exe agent-loop\tests\fixtures\gate_state_approved\_build_gate_fixtures.py
```

That's by design - the gate's whole job is detecting drift between the
approved state and the current state.

## Test data, not real approvals

Real operator approvals live OUTSIDE the repo at:

```
C:/Users/<you>/agent-loop-approvals/operator-production-approval.yaml
```

per section 17 protected-store rule. These fixture files are test data, not
live approvals; do not copy them into the protected store.

## Target sha256

`approval.yaml.approved_target_sha256` is set to `"ab" * 32` (a fixed
fixture-only value). The self-test in `consensus_gate.py` passes the
same value via `--target-sha256` so the three-way hash bind closes.
