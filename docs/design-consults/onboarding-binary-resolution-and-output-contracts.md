# Design consult: consensus-mcp onboarding adapter system

**Context for reviewer:** consensus-mcp is adding a standalone `consensus` CLI for headless cross-AI review. Adapters declare how to invoke each AI (claude, codex, gemini, plugins). Two open design choices need a recommendation. The reviewer's job is to **pick a winner per question** and explain why.

Output format: emit ONE finding per question. The `recommendation` field holds the winning option; `risk` field holds the rationale (what could go wrong with the loser, or what cost the winner pays). Severity should be `low` for both - these are design choices, not bugs.

---

## Question 1 - Binary path resolution across platforms

Adapters need to find their CLI binary (`claude`, `codex`, etc.) on disk. Two designs are on the table:

### Option A - `windows_suffixes` array per adapter manifest

```yaml
binary:
  name: claude
  windows_suffixes: [".cmd", ".exe", ""]
```

Resolution: `shutil.which(name)` -> if not found on Windows, retry with each suffix appended.

**Pros:** Explicit. Each adapter declares which suffixes its binary uses (some npm shims are `.cmd`, some compiled binaries are `.exe`). No hidden behavior.
**Cons:** Platform-specific noise in every adapter manifest. Awkward when extending to macOS-specific or homebrew-specific quirks later.

### Option B - `binary_resolution_strategy` enum

```yaml
binary:
  name: claude
  resolution_strategy: npm           # one of: standard_path | npm | brew | custom
```

Resolution: strategy-specific logic in Python registry (e.g., `npm` knows to check `.cmd` on Windows and look in `npm root -g`; `brew` knows to check `/opt/homebrew/bin` and `/usr/local/bin`).

**Pros:** Hides platform quirks behind a single named strategy. Adding macOS or homebrew nuance later doesn't require touching every manifest. More declarative - manifest says *what the CLI is* (an npm package), not *how to find it*.
**Cons:** Magic. Plugin authors need to read docs to know what each strategy does. Implementing `custom` requires a Python hook, breaking the YAML-only plugin promise. Wrong strategy gives confusing errors.

### My current lean: **A**, but soft.

Reason: explicit-over-magic is consistent with the rest of consensus-mcp (the project leans heavily on declarative YAML, sealed packets, no eval). The cost of B's hidden behavior bites first-time plugin authors hardest - exactly the audience this redesign is for.

---

## Question 2 - Output contracts in v1

The `output_contract` field on an adapter declares how to parse the AI's stdout. Two known contracts will ship in v1:

- `unified_diff_in_fenced_block` - extracts the first ```` ```diff ``` ```` block from stdout. Multi-block output fails. Validated with `git apply --check`.
- `codex_review_schema_v1` - strict JSON matching `codex_review_schema.json` (the existing reviewer-output schema).

**Question:** should v1 also include a third contract?

### Option A - Just the two contracts above

Keep v1 minimal. Adapters that emit bare diffs without fences fail. Authors must wrap their AI's output in fences (or pre-process via a custom Python parser).

**Pros:** Smaller surface, less to test. Easier to reason about. Forces consistent output shape across adapters.
**Cons:** Some AI CLIs (e.g., aider's pure-diff output mode) don't emit fences naturally. Plugin authors with such CLIs have to write a wrapper script or a custom Python parser, neither of which is YAML-friendly.

### Option B - Add `unified_diff_only` (no fence requirement)

Third contract: stdout IS the unified diff. No extraction step. Still validated with `git apply --check`.

**Pros:** Lets adapters whose CLI is already diff-native plug in without wrapper code. Lower friction for plugin authors. Trivially parseable.
**Cons:** Loosens the "stdout has structure" invariant. Adapters that accidentally print extra lines (e.g., a banner or version string) break in opaque ways. Adds another supported case to test.

### My current lean: **B**, but soft.

Reason: the user's stated goal for this feature is "smoother first-run" and lower friction for plugin authors. `unified_diff_only` is a small addition (one more parser, ~30 lines) that materially lowers the bar to write a plugin. The "extra banner" failure mode is detectable at first-use during `consensus init`'s probe step.

---

## What we need from the reviewer

Two findings, one per question. Each:

- `severity: low` (these are design choices, not defects)
- `summary`: which option wins, in one sentence
- `recommendation`: the winning option label (e.g., `Option A`)
- `risk`: rationale - what cost or failure mode the loser carries, OR what the winner gives up
- `citation`: `docs/design-consults/onboarding-binary-resolution-and-output-contracts.md:<line>` pointing at the question heading

If your verdict matches my lean, say so explicitly - confirmation is a valid finding. If it differs, the rationale must address why my lean is wrong.

No patches. No iteration. Single pass. Empty `findings` is NOT acceptable - we need a verdict on both questions.
