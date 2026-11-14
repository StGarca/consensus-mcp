---
title: Test fixture - dirty spec
status: draft-test-dirty
active_contract_readiness: blocked_pending_v1_5
known_blockers:
  architectural:
    - test-blocker-001-points-at-section-2-no-marker
---

# Test spec - dirty

## 0. Agent read contract

Active contract: sections 1-2.

## 1. Goal

Test fixture seeded with multiple findings the validator should catch.

## 2. Section without disable marker

This section is referenced by a known_blocker but has no inline DISABLED marker.
Validator should flag KNOWN_BLOCKER_SECTION_LACKS_DISABLE_MARKER.

## 24. Disposition index

```yaml
resolved:
  - id: dead-ref-finding
    promoted_to: section_999_does_not_exist
    landed_in: v0.1
```

```yaml
archived:
  - id: phantom-pass
    archived_at: "agent-loop/archive/review-passes/this-file-does-not-exist.yaml"
```

```yaml
pending_v1_5_blocking:
  - id: test-blocker-001-points-at-section-2-no-marker
    proposed_target: section_2
    archive_ref: "fixture"
    rationale: "this rationale prose triggers SECTION_24_INDEX_ONLY_VIOLATION"
    note: "this note prose also counts"
    summary: "and this summary too"
```

```yaml
deferred: []
```
