# Design consult: scrub 53 stale archive references from main's section 24

**Context for reviewer:** consensus-mcp v1.12.0 was just released with section 24 fully scrubbed (empty resolved/deferred/dropped/archived lists) for shipping. Maintainer's local `main` keeps the full dev-history disposition lists. BUT main's section 24 `archived` block has 53 entries that point at files under `consensus-state/archive/review-passes/` dated `2026-05-08` through `2026-05-11` (pre-iter-0009 work). Those files NEVER existed in this standalone repo — they were extraction-time leftover from the parent project (`ebook2audiobook-26.4.16`) where the predecessor `agent_loop_mcp` package was developed.

**Concrete state:**
- `consensus-state/archive/review-passes/` on disk contains 11 yaml files dated 2026-05-11 (iter-0001..0004 of THIS standalone repo) + `index.yaml`
- `docs/architecture/orchestration-spec.md` section 24 `archived:` list has 53 entries referencing files that don't exist locally
- `_run_disposition_check` at boot reports 54 findings: 52× `ARCHIVED_FILE_MISSING` (blocking) + 1× `ARCHIVE_INDEX_HAS_PASSES_NOT_IN_SPEC_24` (medium) + 1× `SPEC_24_HAS_PASSES_NOT_IN_ARCHIVE_INDEX` (medium)
- The server refuses to boot on the maintainer's dev tree as a result

**Maintainer constraint:** "local history must remain intact, in use, and updated." The resolved/deferred/dropped lists on main (130 + 49 + 1 = 180 entries) ARE legitimate local audit-trail data — those dispositions trace findings against this spec's own sections. Those must stay. Only the 53 archive references are questionable.

## Question — should the 53 stale archive references be scrubbed from main?

### Option A — Scrub the 53 stale entries; set `review_archive_index: null`; zero `status_counts.archived`

```yaml
# docs/architecture/orchestration-spec.md frontmatter
review_archive_index: null  # was: "consensus-state/archive/review-passes/index.yaml"

# section 24 status_counts
status_counts:
  resolved: 130   # unchanged
  archived: 0     # was: 52
  deferred: 49    # unchanged
  dropped: 1      # unchanged
  # ...

# section 24 archived block
archived: []  # was: 53 entries pointing at parent-project files
```

**Pros:**
- Boot gate passes → maintainer's dev tree becomes usable
- Aligns with section 24's stated "index-only" invariant (codex-rev-030, validate_disposition_index.py line 5)
- The 53 entries are pointers to files that *cannot* exist in this repo (different repo's working state) — they are not recoverable history; they are dangling references
- Smoke tests `test_disposition_index_clean` and `test_audit_log_started` (currently failing on main) will pass
- One-time fix, zero ongoing cost

**Cons:**
- Removes 53 entries from main's section 24 → loses the spec-level record of which parent-project review passes preceded the standalone extraction
- The maintainer's stated intent was to keep "local history intact" — argument depends on whether broken pointers count as history

### Option B — Add env-var escape hatch (`CONSENSUS_MCP_ALLOW_MISSING_ARCHIVES=1`) that downgrades `ARCHIVED_FILE_MISSING` to non-blocking

```python
# server.py _run_disposition_check
allow_missing = os.environ.get("CONSENSUS_MCP_ALLOW_MISSING_ARCHIVES", "").lower() in ("1", "true", "yes")
findings = report.get("findings", [])
if allow_missing:
    findings = [f for f in findings if f.get("id") != "ARCHIVED_FILE_MISSING"]
return len(findings)
```

**Pros:**
- Preserves all 53 entries on main as historical record
- End users (production) see no change — env var defaults to off

**Cons:**
- Doesn't fully solve the problem — the 53 entries also trigger `ARCHIVE_INDEX_HAS_PASSES_NOT_IN_SPEC_24` and `SPEC_24_HAS_PASSES_NOT_IN_ARCHIVE_INDEX` (cross-reference between section 24 archived list and `archive/review-passes/index.yaml`), which the env var does NOT suppress
- `status_counts.archived: 52` will drift if list length changes → `STATUS_COUNT_LIST_LENGTH_DRIFT` finding
- Smoke tests still need env var injected; CI complexity
- Code change to server.py + tests, requires a new release (v1.12.1)
- Ongoing maintenance tax: every dev shell, every CI env, every smoke run needs the var set
- Introduces a foot-gun: future user could set env var in prod and miss real archive-integrity defects

### Option C — Hybrid: scrub on main AND add env-var for future-proofing

Both. Scrub the current 53 stale entries to unblock boot today. Also add env var for any future case where missing-archive findings show up but are intentional.

**Pros:** Unblocks today + safety net for tomorrow

**Cons:** Strictly more work; env var doesn't pull weight if the maintenance discipline is "section 24 should always be in sync with disk reality"

## Your task

Pick ONE option (A, B, or C) and emit a single finding with:
- `severity: low` (this is a design choice, not a defect)
- `summary`: one-line statement of which option you pick
- `recommendation`: the winning option (A / B / C)
- `risk`: the rationale — what's the cost of the loser(s), or what's the cost the winner pays
- No `patch_proposal` needed — this is a design call, the maintainer applies the chosen option manually

Empty findings is NOT acceptable — the maintainer needs a verdict.
