---
title: iteration_known_bad fixture
type: test-fixture
---

# iteration_known_bad

Seeded errors for `consensus_mcp/validators/validate_iteration.py` self-test.

| Seeded defect | Finding ID exercised |
|---|---|
| `claude-review.yaml` missing from directory | `MISSING_REQUIRED_ARTIFACT` |
| `codex-review.yaml` has `iteration_id: iteration-9999` | `ITERATION_ID_MISMATCH` |
| `codex-review.yaml` has `corroborated_by` on a `blocking_objection` | `CORROBORATED_BY_ON_REVIEW` |
| `codex-review.yaml` has `schema_version: 99` | `INVALID_SCHEMA_VERSION` |
| `independence-audit.yaml` records wrong sha256 for codex-review.yaml | `HASH_CHAIN_BROKEN` |
| `review-packet.yaml` omits `karpathy_principle_summary` | `PACKET_MISSING_REQUIRED_FIELD` |
| `codex-review.yaml.reviewed_packet_sha256 != actual canonical packet sha256` | `PACKET_SHA_MISMATCH` |
| `iteration-outcome.yaml` omits `outcome_quality` block | `MISSING_METRICS_BLOCK` |
| `iteration-outcome.yaml` has `independent_finding_rate: 1.7` (out of [0,1]) | `INVALID_INDEPENDENT_FINDING_RATE` |

`iteration_known_good/` is the matching positive fixture: same skeleton with internally consistent hashes; validator must return zero findings.

Hash chain math:
- canonical YAML sha256 convention: `hashlib.sha256(yaml.safe_dump(yaml.safe_load(open(p)), sort_keys=True).encode("utf-8")).hexdigest()`
- recompute by running `python_env\python.exe scripts\agent_loop\validate_iteration.py --self-test`.
