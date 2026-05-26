# Changelog

## 1.31.0 - 2026-05-26

**Grok joins the panel — 5-AI cross-family consults by default in deep tier.**
Add `grok` (xAI Grok CLI) as the 5th first-class contributor alongside
`claude` (host), `codex`, `gemini`, and `kimi`. UX is identical to the
existing four contributors — same `consensus-mcp-dispatch-grok` shell
binary, same `--mode {review,proposal}`, same sealed verdict YAML shape,
same `>=2 non-claude reviewer families` gate (grok counts as a distinct
family automatically).

Converged 3-AI consult (`iteration-v131-grok-design-2026-05-26`,
deep-tier, anchored): unanimous gemini-twin adoption across three
different differentials (anchor=UX-parity history; codex=verification-
first verification of every spec claim against artifacts; gemini=
strategy comparison + parse-with-retry precedent).

### Added
- **`consensus_mcp/_dispatch_grok.py`**: gemini-twin dispatcher with
  grok-specific deltas — `--prompt-file` per-pass dispatch (codex D3
  refinement; avoids the shell-quoting class that prompted v1.30.7),
  auth pre-flight via `~/.grok/auth.json` existence-check raising
  `GrokAuthRequiredError`, and the root-cause-independent safeguard
  flag set: `--no-memory --no-plan --no-subagents --disable-web-search
  --max-turns 1 --permission-mode dontAsk` (logged in
  `dispatch_provenance.disabled_tools` per codex D8). Output: plain
  text, YAML parsed from response (no schema enforcement — same parity
  as gemini/kimi). Finding ID pattern `^grok-rev-\d+$`.
- **`consensus_mcp/contributors/grok.py`**: GrokAdapter wraps the
  dispatcher in the same DispatchPacket round-trip shape as
  GeminiAdapter.
- **`consensus_mcp/contributor_profiles/grok.yaml`**: metadata-only
  profile (B-routing — real dispatch goes through GrokAdapter, same as
  gemini). Wizard list, detect/install hints, OAuth (`grok login`).
- **Templates**: `grok_review_template.md`, `grok_proposal_template.md`,
  `grok_proposal_schema.json` — mirror the gemini equivalents.
- **`consensus-mcp-dispatch-grok`**: console script registered in
  `pyproject.toml`.
- **Test surface (44 new tests)**: `test_dispatch_grok.py` (auth
  pre-flight, CLI flag shape, parser, smoke env gate, per-pass prompt
  filename), `test_grok_adapter.py` (phase→mode forwarding, error
  mapping, model override precedence), `test_dispatch_grok_smoke.py`
  (env-gated end-to-end smoke, opt-in via
  `CONSENSUS_MCP_RUN_REAL_GROK_SMOKE=1`).

### Changed
- **`consensus_mcp/_engine_factory.py`**: `grok` added to
  `_BUILTIN_ADAPTERS` (alongside claude/codex/gemini/kimi).
- **`consensus_mcp/config.py`**: `KNOWN_CONTRIBUTORS` closed enum
  extended with `"grok"`. Default panel auto-derives from built-in
  profiles, so `default_config().contributors.enabled` now includes
  grok — every deep-tier consult dispatches 5-AI by default.
- **Tests**: `test_config.py` and `test_contributor_profiles.py`
  updated to assert the new built-in set including grok.

### Operator notes
- **Auth**: run `grok login` to authenticate (OAuth, token cached at
  `~/.grok/auth.json`). The dispatcher emits a clean `GrokAuthRequiredError`
  with a `grok login` hint if the auth file is absent.
- **Default-panel cost**: every deep-tier consult is now ~25% larger in
  wall-clock + tokens (5 dispatches instead of 4). Per-project disable:
  ```yaml
  # .consensus/config.yaml
  contributors:
    enabled: [claude, codex, gemini, kimi]   # omit grok
  ```
- **Quick + standard tiers unchanged**: quick stays 2-AI (claude +
  codex), standard stays 4-AI. Only deep tier grows.
- **Model pinning**: by default the dispatcher does NOT pass `--model`,
  letting grok roll forward without dispatcher releases. Pin via
  `.consensus/config.yaml: contributors.adapters.grok.model: "<id>"` if
  you want determinism.

### Known limitation (R3, decisive_experiment_before_next_iteration)
Auth pre-flight is existence-check only — an expired token in
`~/.grok/auth.json` will not be caught before invoking the CLI; the
underlying grok error surfaces at runtime. Refuting observation for a
v1.31.x probe-adding follow-up: the env-gated smoke test on a machine
with a valid (existence-wise) but expired auth file exits with a
recognizable expired-token diagnostic that a `grok inspect`-style probe
would have caught pre-invocation.

## 1.30.7 - 2026-05-26

**Fix Windows hook commands — Claude Code runs hooks via Git Bash, not cmd.exe.**
A v1.30.6 user installing on Windows hit `/usr/bin/bash: line 1:
C:Usersstgarciapipxvenvsconsensus-mcpScriptspython.exe: command not found` on
every Stop hook fire. Diagnosis: the v1.26 `os.name == "nt"` branch in
`_build_consensus_hook_command` emitted `subprocess.list2cmdline` output
(`C:\Users\...\python.exe ...`), which is valid for cmd/PowerShell but
collapses to nothing in bash because each unquoted `\X` is consumed as a
shell escape (`\U`→`U`, `\s`→`s`, `\p`→`p`, …). Claude Code's hook executor
on Windows is Git Bash (`/usr/bin/bash`, MSYS), not cmd — the v1.26 branch
pinned to **Windows CI's shell** (PowerShell), not Claude Code's **runtime
hook shell**.

Converged 3-AI consult (`iteration-v1307-quoting-design-2026-05-26`,
deep-tier, anchored): unanimous adoption of strategy (A) — always
`shlex.join`. Three different differentials (anchor reasoned from the
literal error; gemini from strategy comparison; codex from channel
verification with provisional-until-proven framing) — not shared-prior
unanimity.

### Fixed
- **`consensus_mcp/_init_wizard.py::_build_consensus_hook_command`**: drop
  the `os.name == "nt"` branch + the `subprocess.list2cmdline` call.
  Always return `shlex.join([sys.executable, str(script_path)])`. POSIX
  single-quoting preserves backslashes literally in bash, so a Windows
  path `C:\Users\…\python.exe` survives bash unquoting unchanged and is
  accepted by Windows Python via MSYS's exec layer. One code path, no
  platform-detect branching, no shell-version assumption.
- **`consensus_mcp/tests/test_hook_activation.py`**: switch the embedded-
  command parse from naive `c.rsplit(None, 1)[-1].strip('"\\\'')` to
  `shlex.split(c)[-1]`. The naive parse would garble any script path
  containing a space (e.g. `'C:\Program Files\…'`), which the new
  cross-platform shlex.join contract permits.

### Added
- **Cross-OS regression net** in `tests/test_install_workflow_v124.py`:
  parametrized `test_hook_command_roundtrips_cross_os_shapes` exercises
  paths with (a) Windows backslashes (the literal failing shape from the
  user report), (b) spaces, (c) embedded single quote, (d) non-ASCII
  segment, (e) UNC shape (literal-preservation only — exec acceptance
  under MSYS would need a Windows smoke test, deferred per R2). Each
  case asserts `shlex.split(cmd) == [sys.executable, path]`.

### Changed
- **Removed `@pytest.mark.skipif(os.name == "nt", …)`** on
  `test_hook_command_quote_safe`. shlex.split round-trip is shell-
  agnostic under the new single-strategy contract; the test now exercises
  every CI platform.

### Documented (immediate-user repair)
If you installed v1.30.6 on Windows and your Stop hook prints
`command not found`, run the following in Git Bash on the Windows PC to
repair `~/.claude/settings.json` in place (no upgrade needed for the
repair — but upgrade to v1.30.7 to make future installs clean):

```bash
python <<'PY'
import json, re, shlex
from pathlib import Path
p = Path.home() / ".claude" / "settings.json"
p.with_suffix(".json.bak").write_bytes(p.read_bytes())
s = json.loads(p.read_text(encoding="utf-8"))
pat = re.compile(r'^(?:"([^"]+)"|(\S+))\s+(?:"([^"]+)"|(\S+))\s*$')
fixed = 0
for groups in s.get("hooks", {}).values():
    for g in groups:
        for h in g.get("hooks", []):
            if h.get("_consensus_mcp_managed") and h.get("type") == "command":
                m = pat.match(h.get("command", ""))
                if m:
                    py = m.group(1) or m.group(2); sc = m.group(3) or m.group(4)
                    h["command"] = shlex.join([py, sc]); fixed += 1
p.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
print(f"Repaired {fixed} consensus hook commands.")
PY
```

### Known limitation
The diagnosis assumes Claude Code on Windows invokes hooks via Git Bash.
This is empirically proven for the reporting user's install but is not
falsifiable from repository artifacts alone. If a Claude Code Windows
configuration exists that uses `cmd.exe` / PowerShell for hook execution,
single-quoted backslashed paths would not run there. Refuting observation
for v1.30.8: a Windows trace showing the same settings.json command
string handed to `cmd.exe` / `powershell.exe` rather than `/usr/bin/bash`.

## 1.30.6 - 2026-05-24

**Synthesis-aware propose-converge.** A plan-deliverable consult could not converge in the
autonomous engine: it bundles the proposals + every prior round's review artifacts and votes
on the pile, never merging to ONE plan — so contributors, asked "is the single converged plan
satisfied?", correctly answer no forever. (Panel-found, r6.)

### Fixed / Added
- `_run_workflow_4` now FAILS LOUD when the goal_packet declares
  `convergence.requires_synthesis: true` — instead of silently never-converging — pointing at
  the host-driven Path A flow. The max-rounds error also hints at this for the undeclared case.
- New Path A helpers `WorkflowEngine.evaluate_plan_convergence` (delegates to the convergence
  rule — synthesis is the same vote, on the plan instead of a bundle) and `seal_plan_iteration`
  (seals the host-authored plan as the converged artifact via `iteration-outcome.yaml`, never
  overwriting it) — so a host can converge contributors on ONE synthesized plan and mint
  design-approval from it.
- Operator-declared (never inferred) `convergence.requires_synthesis` goal_packet field +
  `docs/workflows/path-a-plan-convergence.md`.

## 1.30.5 - 2026-05-24

**Unblock multi-round consults + add an operator escape hatch.** Two targeted fixes
(test + suite; no consult — these are mechanical, not design decisions).

### Fixed
- **Multi-round consults deadlocked at the convergence packet.** `_build_prompt` embedded the
  review-target content only `if review_target_content and not touched_contents` — but a
  convergence dispatch carries the round's CONSTITUENT files in `touched_files_contents`, so
  the guard SUPPRESSED the target embed exactly when it was needed. The reviewer, pointed at
  `convergence-packet-round-N.yaml` (which is not in the touched set), got "canonical target
  not provided" and the consult could never converge past round 1. The review-target content
  is now ALWAYS embedded (additive to the touched-files block; distinct content). Completes
  the v1.30.2 Bug B fix.

### Added
- **Operator gate escape hatch — `CONSENSUS_MCP_GATE_DISABLE=1`.** Set in the launch
  environment, it fully disables the PreToolUse design gate for the session (fail open). A
  safety gate must never be able to deadlock its own operator — the human is the trust root,
  so "can't mint a seal" must never mean "can't work." Read from the process env (set by the
  operator before launch), so an in-session agent cannot self-enable it.

## 1.30.4 - 2026-05-24

**Fix the design gate's over-broad scope** — it denied legitimate out-of-repo writes (converged
3-AI consult `iteration-gate-scope-design-2026-05-24`: gemini+kimi adopted the 3-class model,
codex abstained on verification-first with a no-over-reach caution; codex+gemini Workflow B
reviewed).

The GLOBAL PreToolUse design gate denied **all** out-of-repo `Edit`/`Write` in an opted-in
project — including the agent's own persistent **memory dir** under `~/.claude/...` — because it
reused the sealing-confinement check (`_confine_to_repo`, "out-of-repo edits are not
consensus-sealable") for a *scope* decision.

### Fixed
- **3-class path scoping** in the gate: `PROTECTED-install → in-repo (design-approval) →
  out-of-repo (allow)`. An out-of-repo target (agent memory, `/tmp` scratch) is not a repo
  modification and is no longer the gate's concern → allowed. `_design_approval.py`'s
  sealing semantics are left untouched (gate-only change).
- **Always-on tamper guard:** writes to the consensus *enforcement surface* —
  `~/.claude/settings.json` and `~/.claude/hooks/consensus_*.py` — are denied regardless of
  opt-in (the check runs **before** the opt-in early-return), since the hook is global and that
  is the self-disable vector. Minimal set by design: the pipx venv is excluded (tampering it
  fails *open* and is reinstall-recoverable, not a silent disable) and skills are instructions,
  not enforcement. Symlink-escape is resolved via `Path.resolve()`; the fail-OPEN invariant is
  preserved (any resolution error → allow).

## 1.30.3 - 2026-05-24

**Harden kimi isolation for no-`.git` / large consuming repos** (the v1.30.2 design
follow-through; converged 4-AI consult, codex Workflow B reviewed). v1.30.2 was never
*unsafe* — it copies or fails loud, never zero-control — but a no-`.git` repo whose heavy
dirs can't be excluded had two gaps this closes.

### Fixed / Hardened
- **Git-independent mutation control (no more vacuous control on a no-`.git` repo).** With
  no `.git`, `git status` is unavailable, so the kimi post-dispatch integrity snapshot used
  to return `{}` — a VACUOUS control (the before/after diff is always empty, so a real-repo
  mutation would never be caught). v1.30.3 adds a git-independent **content-hash manifest**
  (`_filesystem_manifest_snapshot`): it walks the working tree honoring the same ignore set
  as the disposable copy (`_TEMP_WORKDIR_IGNORE_DIRS` + `CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS`
  + the repo's top-level `.gitignore`) and hashes file contents + symlink targets. A real
  control now exists with or without git.
- **Fail-LOUD instead of silent zero-control.** If the tree exceeds the snapshot budget
  (`CONSENSUS_MCP_KIMI_SNAPSHOT_MAX_FILES`, default 50000; `…_MAX_BYTES`, default 2 GiB), the
  dispatch fails with a clear `_SnapshotIndexError` (`ok:false`) telling the operator to
  exclude heavy dirs — it never proceeds with no mutation control (the dissenter's invariant
  from the consult).
- **Size-aware degrade.** If the disposable copy can't fit (ENOSPC), the dispatch no longer
  aborts — it DEGRADES to running against the real repo with no copy (`_WorkdirTooLargeToIsolate`),
  relying on the now-real before/after snapshot to DETECT and REJECT any mutation. Safe
  precisely because that snapshot is a genuine control; if even the snapshot can't be built,
  it fails loud first.
- **Multi-round-seal regression test (the ebook2audiobook dogfood).** Locks the v1.30.2
  Bug A fix end-to-end at the seal boundary: round-keyed reviewer_ids → distinct pass_ids →
  every convergence round seals, with a non-vacuous negative control proving the original
  `index_collision` symptom returns if round-keying ever regresses.

## 1.30.2 - 2026-05-24

**Hot-patch: three bugs that blocked a Workflow A consult from a consuming project**
(found by the ebook2audiobook emotion-engine consult; codex Workflow B reviewed).

### Fixed
- **Bug A — multi-round consults couldn't seal.** Convergence `reviewer_id` was
  hardcoded `-1`, ignoring the threaded `round_number` — so a round-2 re-seal hit the
  immutable T6 `index_collision`. The `codex` / `gemini` / `kimi` adapters now key the
  `reviewer_id` off `adapter_options.round_number` (propose/review still default to 1 —
  no behavior change). Any consult that needs a round 2 (a round-1 block) can now complete.
- **Bug B — sandboxed reviewers couldn't read the convergence packet.** `_build_prompt`
  embedded only the review-target's path + hash; a read-only reviewer (codex) can't open
  files under `consensus-state/`. It now embeds the review-target **content** (the
  dispatcher already read it for the hash), via a new `review_target_content` block — only
  when the code-review `touched_files_contents` path didn't already embed it (no double-embed).
- **Bug C — kimi filled tmpfs in a no-`.git` / large repo.** The disposable-workdir
  copytree fallback (used when there's no `.git` to `git clone`) tried to copy giant
  derived dirs into the 16G tmpfs. Now: proposal-mode dispatches **skip the copy
  entirely** (read-only; the integrity snapshot is the backstop); the copytree ignore is
  extended by `CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS` + the repo's top-level `.gitignore`
  dirs; the work-dir root is overridable off-tmpfs via `CONSENSUS_MCP_KIMI_WORKDIR_ROOT`;
  and an out-of-space copy (OSError ENOSPC **or** aggregated `shutil.Error`) now fails with
  a clear, actionable message and cleans up the partial copy instead of a raw ENOSPC.

## 1.30.1 - 2026-05-24

**Hot-patch: three fresh-install / robustness fixes.**

### Fixed
- **Subagent launch crash (the headline):** `review.read_post_seal` expressed
  "exactly one of `pass_id` | `path`" as a **top-level `oneOf` in its
  `input_schema`** — which the Anthropic tool API rejects, so any subagent granted
  that tool died on launch (0 tokens). Flattened to optional properties; the
  "exactly one" rule was already enforced in `handle()`
  (`must_provide_exactly_one_mode`). Added a **data-driven guard test** asserting
  no tool's `input_schema` uses a top-level `oneOf/anyOf/allOf` — closing the class.
- **kimi work-dir disk leak:** a watchdog-killed kimi dispatch skips its `finally`
  cleanup, leaking `/tmp/kimi-workdir-*`; one such copy of `consensus-state` filled
  `/tmp` and broke all Bash. Now `consensus-state` is excluded from the disposable
  copy, and every dispatch sweeps stale `kimi-workdir-*` (>1h, env-overridable via
  `CONSENSUS_MCP_KIMI_WORKDIR_STALE_SECONDS`) on startup — robust against SIGKILL.
- **Gate crash-safety:** the global PreToolUse gate now **fails OPEN on any
  unexpected exception** (`_main_fail_open`). Because it's a global hook, an
  internal crash could block Edit/Bash in *every* project; it must never be able
  to. Deliberate DENY (exit 2) is preserved.

## 1.30.0 - 2026-05-24

**Cross-AI weighting, cost-tiers, operator-declared rigor, and a contributor
scorecard — built through ~7 cross-AI consults, shipped user-centric and
defaults-unchanged.** Designed so the human decides any bias: the system measures,
the operator declares.

### Added (live / user-visible)
- **Contributor scorecard** in `consensus results` — a per-contributor track record
  (useful N/total + rate, ranked) read from an external, append-only outcome ledger.
  DESCRIPTIVE decision-support for declaring an AI "lean"; `<5` outcomes show
  "insufficient data"; **the score never judges an individual finding**.
- **Operator-declared rigor tiers** (quick/standard/deep) codified as an operative
  step in the `consensus-workflow` skill + `docs/consensus/routing-decision-table.md`:
  the tier is operator-DECLARED (never inferred — "heuristics are the shared-prior
  trap"); `_tier_router.suggest_tier` is a non-binding suggestion; the sole automatic
  move is the MONOTONE governance/security safety floor (DEEP+locked, can only raise).
- Per-iteration **telemetry** (cost-per-blocking-finding by tier) and an
  **interaction_surface** declaration check (flags a reflexive "none" on a
  governance-touching change).

### Added (present, DEFAULT-OFF / dormant — opt-in, defaults byte-identical)
- The advisory **contributor-weight** engine (discount-only, hard floor, same-family
  cap, no-self-grade firewall at write AND read), the **Beta learner** (repurposed as
  the scorecard's measure), the **outcome ledger**, and the external-CLI
  **retry/fallback** policy ship as tested libraries. They do not change behavior by
  default.
- The **lean-application** (using a declared lean to reorder synthesis reading-order of
  NON-blocking findings) ships present-but-OFF, **prove-or-delete**: per a unanimous
  4-AI consult it is enabled only if it beats the equal-weight baseline on recorded
  history; until then it is dormant. **Blocks are weight-blind** — a critical/blocking
  finding from the lowest-leaned contributor blocks exactly as hard (weights-off
  equivalence), and weights NEVER touch the cross-family gate.

### Invariants (locked by tests)
- **merit → score, never score → merit** (no doom loop); **weights-off equivalence**
  (delete the weights and gate verdicts are byte-identical); **no-self-grade** (no AI
  writes usefulness credit — only operator disposition / objective test outcomes).

## 1.29.5 - 2026-05-24

**Regression guard: consensus-mcp's own tooling can't be re-broken by the gate.**
A governed-project integration smoke locks the class of bug fixed in v1.29.4 (the
PreToolUse gate blocking `consensus init`/`--repair`).

### Added
- `tests/test_governed_self_tooling_smoke.py` — activates the gate via REAL
  governed-project detection (an on-disk `.consensus/`, not the test opt-in
  shortcut) and asserts the project's own console scripts pass the gate while
  ordinary writes stay blocked. The own-binary allow-list check is **data-driven
  from `pyproject [project.scripts]`**, so a newly-added console script that isn't
  exempted in the gate's `_CONSENSUS_TOOLING` fails the test — closing the bug
  *class*, not just the one binary. Verified non-vacuous against the pre-v1.29.4
  hook (own tooling blocked without the exemption).

## 1.29.4 - 2026-05-23

**Fix: the PreToolUse gate no longer blocks consensus's own tooling.** In a
consensus-governed project, the gate denied `consensus init` / `--check` /
`--repair` / `--reconfigure` (and the dispatchers) because they weren't on the
read-only allowlist — chicken-and-egg for bootstrap, and it defeated `--repair`
(remediation) and `--check` (read-only).

### Fixed
- `consensus_pretooluse_gate.py` now exempts consensus's own console scripts
  (`consensus`, `consensus-init`, `consensus-mcp`, `consensus-results`, and the
  `consensus-mcp-dispatch-*` reviewers) via an explicit `_CONSENSUS_TOOLING`
  allowlist. Leading-token only; the existing redirection / subshell /
  command-substitution rejection + per-segment split still deny a chained writer
  riding on an allowed token (e.g. `consensus-init && rm x`).

## 1.29.3 - 2026-05-23

**`consensus init` guards against bootstrapping a workspace folder.** Running
init in a *non-repo directory that contains git projects* (a workspace umbrella)
used to silently bootstrap the umbrella — blanketing every sub-project via the
hierarchical `CLAUDE.md`. It now detects that and steers you to a real project.

### Added
- Workspace-umbrella guard: when the resolved root is not a git repo but has ≥1
  immediate child git repo, `consensus init` refuses by default. In a terminal it
  asks for confirmation; under Claude Code / CI it exits **8** with a stable
  `STATUS: looks-like-workspace-umbrella` token (the `consensus` skill turns it
  into a pick-a-project menu). Pass **`--here`** to initialize the current
  directory anyway. The guard is skipped when a config already exists and for
  `--reconfigure`/`--repair`/`--check` (so re-running to clean up still works).

## 1.29.2 - 2026-05-23

**4-AI consensus runs in-engine + the iteration pipeline is codified.**

### Added
- `KimiAdapter` (`contributors/kimi.py`), registered as a built-in, so
  `consensus.run_iteration` dispatches all four AIs (claude/codex/gemini/kimi)
  out of the box — kimi keeps its hardened `_dispatch_kimi` behavior instead of
  the generic `ProfileAdapter` path. (`ProfileAdapter` remains for user-defined
  `cli_reviewer`s; a config profile named `kimi` is now superseded by the
  built-in, same as claude/codex/gemini.)
- `consensus-workflow` skill now codifies the end-to-end **iteration pipeline**
  (superpowers ↔ consensus), the **dual-path** consult-selection rule (Path B
  `run_iteration` for execution; Path A orchestrator-driven for high-design-surface),
  the **host_peer dispatch procedure**, the `.consensus` gate caveat, and the
  **release install-currency** steps (`pipx install --force` + `--install-claude-code
  --force` + version-asserting smoke). Mirrored in `docs/workflows/iteration-pipeline.md`.

### Known follow-up
- Per-round host re-convergence in Path B (the claude/host_peer callbacks echo one
  YAML across convergence rounds) needs a `run_iteration` pause/resume API; until
  then use Path A for design-surface convergence.

## 1.29.1 - 2026-05-23

**`consensus init --repair` — verify & repair a partially-broken install.**
Re-running init on a project whose `.mcp.json`, `.gitignore` block,
`.claude/agents/` files, or instruction blocks went missing can now restore
them non-destructively, instead of only leave/reconfigure/force.

### Added
- `consensus init --repair` (and the `consensus-init --repair` binary): a
  deterministic verify of all install components that **re-creates missing**
  project pieces and **reports diverged** ones (`pass --force`), never clobbering
  local edits. Emits version-stable summary lines (`OK:`/`REPAIRED:`/`SKIP:`/
  `REPORT-GLOBAL:`); composes with `--dry-run` (preview, no writes); idempotent.
- Exit codes: `0` healthy-or-repaired, `2` config missing, `3` config invalid,
  `7` repair incomplete (diverged left for `--force`, or global enforcement
  detected dead). Global enforcement (`~/.claude` hooks) is **reported** (run
  `consensus-init --install-claude-code`), not written from a per-project repair.
- The existing-config menu (TTY + the `consensus` skill) gains a **Verify /
  repair** option; `--repair` is exempt from the already-configured gate.

## 1.29.0 - 2026-05-23

**Graceful re-init when consensus is already configured.** Re-running
`consensus init` on a bootstrapped project no longer hard-errors with a raw
"Exit code 4". In a real terminal you get an interactive menu (`[1]` leave /
`[2]` reconfigure / `[3]` force); under Claude Code the `consensus` skill
presents the same three choices.

### Added
- The `_init_wizard` existing-config guard is now install-aware. TTY → an
  interactive leave/reconfigure/force menu. Non-TTY (`--from-claude-code`, CI,
  a pipe, or `--non-interactive`/`--accept-defaults`) → exit 4 plus a stable
  `STATUS: already-configured` token on the first line of stdout and
  human-readable guidance on stderr. The `consensus` skill and `consensus-init`
  command detect that contract and present an `AskUserQuestion` menu, then
  re-invoke once with `--reconfigure` or `--force`.
- `--force` now supersedes `--reconfigure` when both are passed (overwrite has
  nothing to diff).

### Known gap / follow-up
- "Leave as-is" is a pure no-op: it does NOT verify or repair a partially
  broken install (e.g. a missing `.mcp.json` or a dead enforcement hook). A
  future release will add an explicit "verify/repair" option. (4-AI consult
  Q3c — `iteration-init-already-installed-ux-2026-05-23`.)

## 1.28.1 - 2026-05-23

**Non-interactive init no longer crashes under Claude Code / CI / pipes.** Running
`consensus-init --from-claude-code` (or any bare `consensus init`) without
`--non-interactive`/`--accept-defaults` from a non-TTY stdin drove the interactive
wizard straight into an `input()` EOF: the contributor multi-select aborted with
"aborted by user", and — once contributor input was piped — the host-peer follow-up
prompt raised an **uncaught `EOFError` traceback**. The `--from-claude-code` flag, of
all paths, signals "I'm being called by Claude Code" (which has no interactive
terminal), so it should never have prompted.

### Fixed
- **The wizard now detects a non-TTY stdin and falls back to the non-interactive
  path** (auto-detected reviewers + defaults) instead of crashing/aborting, printing
  a guidance note on how to customize (explicit flags, or `consensus init
  --reconfigure` in a real terminal). New `_stdin_is_interactive()` gate in
  `cmd_init`; `--from-claude-code` gets a Claude-Code-specific note.
- **`_prompt_host_peer_followup` now catches `EOFError` → `KeyboardInterrupt`**,
  matching its sibling `_select_contributors_interactive` — a Ctrl+D mid-session is a
  clean exit 1, never an uncaught traceback.

### Tests
- `test_init_wizard_non_tty.py` (new): `_stdin_is_interactive` detection, host-peer
  EOF handling, `cmd_init` interactive/non-interactive gating, and end-to-end
  `--from-claude-code` + bare non-TTY init.
- `test_init_wizard.py`: the 5 interactive tests now force the TTY precondition the
  gate makes explicit (no behavior change to what they assert).

## 1.28.0 - 2026-05-23

**CI green on Windows + py3.12 (the matrix was red on `push`, masked by a passing
parallel run).** Fixes a real Windows product bug + three test-portability issues.

### Fixed
- **Windows hook command was invalid (product bug from v1.24).** `_build_consensus_hook_command`
  used `shlex.join`, whose POSIX quoting single-quotes backslash paths
  (`'C:\…python.exe'`) — which Windows cmd/PowerShell cannot run. Now platform-aware:
  `subprocess.list2cmdline` on Windows, `shlex.join` on POSIX.

### Tests (portability — no product change)
- `test_invoke_kimi_builds_expected_argv`: compare against `str(Path(...))` (OS-native
  separator) instead of a hardcoded `/tmp/...`.
- `test_merge_writes_consensus_hook_entries`: parse the hook command's script token
  robustly across POSIX/Windows quoting.
- `test_hook_command_quote_safe`: POSIX-only (a `"` in a path is not a Windows case).
- `test_operator_abort_signal_file_triggers_abort`: write the signal file atomically so
  a slow CI never reads it empty (it was falling back to `operator_signal_file`).

## 1.27.0 - 2026-05-23

**Init-review convergence.** Round-4 of the full-panel init review came back with the
security classes clean (gemini `goal_satisfied: True`; no symlink/exec/TOCTOU
residuals) — the only finding, raised by both codex and kimi, was a benign over-deny.

### Fixed
- **`git branch --list <pattern>` no longer over-denied.** `--list` is a read-only
  listing form whose positional is a glob (not a new branch name); added to the gate's
  read-only branch-filter set. Bare-name creates (`git branch evil`) and write flags
  are still denied.

This closes the init/install + enforcement review loop (16 → 5 → 6 → 1 findings across
four fix→push→re-review rounds; all resolved, no security residuals).

## 1.26.0 - 2026-05-23

**Root fixes for the recurring symlink / git-allowlist residuals (codex + gemini +
kimi, round-3).** Earlier per-site patches kept leaving cousin holes, so this release
fixes them at the root. Suite 1393 passed / 7 skipped.

### Fixed
- **One hardened atomic-write primitive everywhere.** `_atomic_write_bytes` creates the
  temp file with `O_CREAT|O_EXCL|O_WRONLY` and an **unpredictable** name, then
  `os.replace` — so a pre-planted `<dst>.tmp` symlink can't redirect the write, and a
  destination symlink is replaced (the link, not its target). All writers
  (`_atomic_write_text`, `_atomic_write_json`, `write_config`, `update_gitignore`) now
  route through it (the predictable-`<dst>.tmp` symlink class is closed in every site).
- **gov-dir check via a single `lstat`** (no `is_symlink()`→`resolve()` double-stat
  TOCTOU); an absent `.consensus` still permits the bootstrap write.
- **Git allowlist:** reject `--ext-diff`/`--textconv` (run repo-configured external
  commands); `git branch` now allows read-only positional args (`--contains <sha>`,
  `--merged <branch>`) while still denying write flags and bare-name creates.
- **Uninstall** settings.json write now fails soft (WARN) like install.

## 1.25.0 - 2026-05-23

**Convergence re-review fixes (codex + gemini + kimi).** The full re-review of the
v1.24 fixes found 5 residuals — mostly in the v1.24 fixes themselves (symlink handling
was the soft spot). All fixed + tested (suite 1383 passed / 7 skipped).

### Fixed — gate
- **Governance-dir symlink bypass (BLOCKING).** `_is_governance_path` now rejects a
  *symlinked* `.consensus`/`consensus-state` (even one pointing at an in-repo dir like
  `src/`), which previously let code paths be treated as governance.
- **`git branch` write variants** (`-d`/`-D`/`-m`/`-M`/bare new-name) removed from the
  read-only allowlist — only listing flags are permitted.
- **Subshell command tokens** (`(rm x)`) rejected; quoted parens in args (`grep '(x)'`)
  are unaffected.

### Fixed — install workflow
- **TOCTOU in symlink replacement (BLOCKING).** Both installers now write via atomic
  `os.replace`, which replaces a destination symlink (the link, not its target) in one
  step — closing the `is_symlink()→unlink()→write` race.
- **settings.json write IO failure** now fails soft (WARN → incomplete-install rc 6)
  instead of crashing the installer.

## 1.24.0 - 2026-05-23

**Full init-workflow review fixes (codex + gemini + kimi).** A full-panel review of
the init/install code found 16 issues — including 2 BLOCKING security holes in the
v1.23 gate fixes. All fixed, each with tests.

### Fixed — enforcement gate (security)
- **[BLOCKING] Governance-path symlink escape.** `_is_governance_path` now requires the
  resolved base (`.consensus/`, `consensus-state/`) to be strictly inside `repo_root`,
  so a `.consensus` symlink to `/` or the repo root can't turn the bootstrap allow into
  a universal write bypass.
- **[BLOCKING] Single `&` not split.** The quote-aware splitter now treats a lone `&`
  (background) as a separator, so `allowed_cmd & rm -rf y` no longer slips a writer past
  the allowlist.
- **[HIGH] `pytest` removed from the read-only allowlist.** Running tests executes
  arbitrary test/conftest/plugin code, so `pytest` / `python -m pytest` are no longer
  pre-approval read-only (run them behind a sealed marker, or in a non-opted-in repo).

### Fixed — init/install workflow
- **[HIGH]** Install no longer crashes on a single file IO error — each read/write is
  guarded; failures `WARN` and continue.
- Instruction-file **path-traversal** refused + **atomic** write; **destination symlinks**
  no longer followed on install; `CLAUDE_HOME` override is resolved; `.mcp.json` compares
  semantically (key-order); hook commands are `shlex`-quoted (safe with quotes in paths).
- **Incomplete installs now surface + return distinct nonzero codes** instead of a silent
  rc 0: freshness-stale aborts before copying (**6**, `--force` overrides), settings.json
  activation failure (**6**), per-project agent SKIP (**7**), managed-file SKIP (**5**).

## 1.23.0 - 2026-05-23

**Enforcement + install-workflow fixes (codex install-workflow review).** A codex
review of `consensus init --install-claude-code` found 5 real issues (2 blocking) —
all now fixed, each with tests.

### Fixed
- **Enforcement is now opt-in per repo (BLOCKING).** The PreToolUse design gate
  only enforces when the repo has a `.consensus/` directory; a repo that never
  enabled consensus (incl. consensus-mcp's own) **fails open**, instead of the prior
  global default-deny that bricked development everywhere. `CONSENSUS_MCP_FORCE_OPTED_IN`
  forces enforcement for tests.
- **Gate can bootstrap its own marker (BLOCKING).** Writes under `.consensus/`
  (incl. the `design-approved` marker) and `consensus-state/` are always permitted,
  breaking the circular lock where minting the marker required edits the gate blocked.
  Safe because the marker is re-validated against the live seal on use.
- **Quote-aware Bash segment splitter.** A read-only command whose argument contains
  `|`/`;`/`&&` inside quotes (e.g. `grep -E 'a|b'`) is no longer mis-parsed as a
  pipeline and wrongly denied. Genuine pipelines with non-allowlisted segments are
  still denied.
- **Stale-skill SKIP surfaced.** `--install-claude-code` now warns loudly and returns
  a distinct nonzero (5) when a divergent managed skill/hook is skipped on upgrade
  (was a silent rc=0); user-edited content is still preserved (use `--force` to update).
- **Package freshness self-check.** `--install-claude-code` warns when the package
  ships fewer than the expected vendored-skill floor (a stale/partial pipx install),
  instead of silently deploying an old asset set.

## 1.22.0 - 2026-05-23

**Completed the vendored-skills (the v1.21 vendoring shipped with dangling
references).** A 3-family consensus review (codex/gemini/kimi) found several adapted
`SKILL.md` files referenced companion files that were never vendored — links the
install/packaging tests didn't catch. Resolved over three review→fix→re-review rounds.

### Fixed
- **Vendored the 4 referenced companions** (MIT-attributed): `implementer-prompt.md`,
  `spec-reviewer-prompt.md`, `code-quality-reviewer-prompt.md`
  (`consensus-subagent-driven-development`); `testing-anti-patterns.md`
  (`consensus-test-driven-development`).
- **Stripped out-of-scope dependencies** instead of vendoring them into a code-review
  tool: removed the `consensus-brainstorming` "Visual Companion" feature (an upstream
  browser-based mockup HTTP server + its `scripts/`), and replaced the dangling
  `elements-of-style:writing-clearly-and-concisely` skill reference with plain prose.
- **Rewrote** `code-quality-reviewer-prompt.md`'s reference to the un-vendored
  `requesting-code-review/code-reviewer.md` to point at the consensus flow.
- **Clarified** that `consensus-subagent-driven-development`'s two-stage local review is
  a fast inner loop and the BINDING gate is a consensus Workflow B cross-family review.

### Added
- **`tests/test_vendored_skill_references.py`** — mechanically enforces reference
  integrity (same-dir `./x.md`/`@x.md`, subdirectory `dir/x.md` in any wrapper, and
  `consensus:`-namespaced skill cross-refs all resolve). This is the guard that was
  missing; verified non-vacuous against a reintroduced dangling ref.

### Changed
- **Packaging**: skills glob `SKILL.md` → `*.md` so vendored companions ship in the
  wheel; installer (`_CLAUDE_EXTENSION_FILES`) enumerates the 4 companions.

## 1.21.0 - 2026-05-23

**Consensus-enforced host integration + self-contained Superpowers workflows.**
v1.21 wires the consensus discipline into the editing host itself: edits are
gated behind a real cross-family seal, the host-family roles install as callable
subagents, and ten Superpowers workflow skills ship vendored (MIT) so no external
prerequisite is required. Full suite 1326 passed / 7 skipped.

### Added
- **Vendored Superpowers skills (MIT).** Ten workflow skills (brainstorming,
  writing/executing plans, subagent-driven & test-driven development, requesting/
  receiving code review, verification-before-completion, finishing-a-branch,
  using-git-worktrees) copied + adapted under `claude_extensions/skills/` with
  NOTICE + VENDORED.md attribution (obra/superpowers v5.1.0). No Superpowers
  install required.
- **Consensus enforcement hooks.** PreToolUse design-approval gate (Edit/Write/
  MultiEdit blocked until a sealed cross-family iteration mints a scoped
  `.consensus/design-approved` marker; Bash default-deny with a read-only
  allowlist), Stop gate (committed-but-unverified guard), and a SessionStart
  bootstrap. Installed + activated in `.claude/settings.json` at `init`.
- **Design-approval marker as pointer.** The marker re-validates against the live
  T6 seal (≥2 non-claude reviewers, converged-plan hash match, repo-confined
  fnmatch scope) on every check — no trusted boolean to forge.
- **Host-family subagents installed at init.** `consensus-orchestrator` (holds
  `Agent` + the consensus MCP tools) and the read-only
  `consensus-host-peer-reviewer` (no `Agent`, no mutation) written to
  `.claude/agents/` so the host roles are real, callable subagents.
- **Host-peer ".5" review path activated.** `consensus.run_iteration` accepts
  `host_peer_review_yaml`; an enabled host_peer profile seals a supplementary
  review (`gate_eligible=false`), and gracefully soft-skips (surfacing
  `supplementary_skipped`) when none is supplied.
- **Kimi reviewer + dispatcher.** `consensus-mcp-dispatch-kimi` mirrors the
  gemini path with UX parity: stdin transport, a disposable temp work-dir, a
  post-dispatch content+symlink integrity check, and API-key scrubbing — the kimi
  reviewer is strictly read-only and portable.
- **End-to-end integration test.** `test_integration_host_peer_flow.py` dogfoods
  the full flow: marker→gate deny/allow flip, forged-marker rejection, host_peer
  activation + soft-skip, and the Agent-only-on-orchestrator dispatch contract.

### Changed
- **Smooth init.** Post-init status summary, `.mcp.json` command-resolvability
  warning, and a degenerate-panel guard (warns on <2 independent contributors in
  the non-interactive path) — all fail-soft.
- **LF line-ending policy.** Added `.gitattributes` (`* text=auto eol=lf`) to
  normalize the tree and prevent CRLF churn from Windows-side editors; normalized
  `_engine_factory.py` (the one file committed with CRLF).

## 1.20.3 - unreleased

_Unreleased._

## 1.20.2 - 2026-05-22

**Internal cleanup — no functional change.** Test-coverage and dead-code
follow-ups from the v1.20.1 final review; `consensus init` behaves identically
to v1.20.1. Full suite 1114 passed / 1 skipped.

### Changed
- Removed dead wizard helpers `_validate_contributor_selection` and
  `_resolve_contributor_selection` (and the now-orphaned `WizardError`). They
  were never wired into the live `--contributors` path, which already enforces
  the ≥2-independent floor + orphan rejection via config validation — the single
  gate, consistent with the open-contributor model.

### Tests
- Added coverage for the multiple-same-family `host_peer` mini-select (numbered
  choice, default none, never silently picks the first).

## 1.20.1 - 2026-05-22

**`consensus init` contributor-selection redesign.** Setup now offers the
independent AIs as the panel and treats a same-model reviewer as an explicit
0.5 supplemental, with the "≥2 independent" floor enforced in config validation
and contributor detection made fully dynamic (no hardcoded AI lists). Designed
via a 3-way Workflow A consult
(`docs/design-consults/init-contributor-selection-supplemental-review.md`), built
subagent-driven with per-task spec + quality review. Full suite 1118 passed /
1 skipped; the consensus gate is unchanged.

### Changed
- **Main multi-select lists independent AIs only** (claude/codex/gemini/kimi + any
  operator profile). The same-model claude reviewer (`claude-swe-reviewer`,
  `kind: host_peer`) is no longer a flat list item — it is a **conditional opt-in
  follow-up**, offered only when the host AI is on the panel (default No on fresh
  setup; defaults to its current state on reconfigure, preserving a legacy choice).
- **The "≥2 contributors" floor now means ≥2 _independent_ contributors**, enforced
  in `config` validation — the authoritative gate for every entry path (interactive,
  `--contributors`, reconfigure, non-interactive defaults). A same-model supplemental
  never counts toward the floor.
- **Contributor detection is dynamic** — init derives the available panel and the
  default enabled set from the profile set, so kimi and any operator-added AI are
  picked up automatically (no hardcoded name lists).
- End-of-init panel summary shows the weighted count, e.g.
  `2.5 reviewers — 2 independent + 0.5 supplemental same-model`.

### Added
- Orphan-supplemental rejection: a `host_peer` can be enabled only when its host
  AI is also enabled (rejected on every entry path).

### Note
The consensus gate, convergence, and engine are untouched: a same-model supplemental
stays `gate_eligible=false` and can never close consensus — but its findings are
still weighed on merit (good ideas are always applied).

## 1.20.0 - 2026-05-22

**Host-family specialist agents.** A same-family blind SWE-reviewer you can add as
a supplementary "second opinion", plus a distinct orchestrator role — without
weakening the cross-family closure invariant. Designed via a 4-way Workflow A
consult (`docs/design-consults/v1.20.0-host-peer-agent.md`); codex diff-reviewed
the landed change (clean, 0 findings). Full suite 1096 passed / 1 skipped.

### Added
- **`kind: host_peer` contributor** — a blind, fresh-context, adversarial
  SWE-reviewer that runs the host's own AI family (e.g. Claude when Claude hosts),
  invoked in-process via a dedicated host review callback (no CLI). It's
  **supplementary** and **excluded from the cross-family closure invariant** — it
  augments cross-family review, never replaces it (same model = same blind spots).
- **`consensus init` option** for the same-model second opinion, clearly labelled:
  "a useful extra check if you have the tokens, but NOT independent multi-AI
  consensus (same model as the host/orchestrator)."
- Distinct **orchestrator role** prompt (neutral scoping/synthesis/gate-
  enforcement/anti-anchoring) + an adversarial SWE-reviewer prompt, so the
  integrator never blind-reviews its own synthesis.

### Changed
- `_closure_invariant`: a closer tagged `gate_eligible: false` (host_peer /
  supplementary) can **never** be the different-family signer — even when its
  family differs from the mutator. Minimal + additive: only the literal `False`
  excludes, so every existing closer is unchanged. (A RED test caught a real hole:
  a codex-authored change + claude host_peer would have wrongly closed.)
- Results record + schema tag supplementary same-family reviewers separately from
  independent cross-family review.

### Note
The host_peer **seam** ships here; actually running it requires the host runtime to
wire the `host_peer_review_callback` with a guaranteed-fresh context. Without it,
host_peer is gracefully absent. Fresh-context is a host-runtime contract (recorded
via attestation), not something consensus-mcp can mechanically prove.

## 1.19.0 - 2026-05-22

**Result logging.** consensus-mcp now records the RESULTS of every run, not just
the sealed review passes — so findings, dispositions, and fixes aggregate into a
trustworthy project scorecard. Closes the gap where the history existed but
wasn't queryable. Designed via a 4-way Workflow A consult
(`docs/design-consults/v1.19.0-result-logging.md`); Workflow B review skipped per
operator. +21 tests; full suite 1076 passed / 1 skipped.

### Added
- **Per-run results record** written automatically at `iteration_closed`: findings
  by severity + source pass, each finding's **disposition** (validated-and-fixed /
  dismissed-with-evidence / deferred / open), fixes applied, and the convergence
  outcome — conforming to a versioned schema (`schemas/results-v1.schema.json`).
  Written to a durable, gitignored project ledger
  (`consensus-state/state/results-v1.jsonl`, one snapshot per iteration) plus a
  co-located human-readable `iteration-results.yaml`.
- **`consensus results`** (and `consensus-results --json`) — a read-only project
  scorecard: total findings by severity, validated vs dismissed-with-evidence vs
  deferred, fixes applied, iteration count, convergence rate, and date span.
  Backfilled/best-effort records are reported separately, never folded into the
  authoritative totals. Also exposed as a read-only MCP tool.

### Changed
- Audit events carry structured result fields at event time —
  `apply_step_landed` gains `{finding_ids, files_touched, fix_summary}` and
  `iteration_closed` gains `{finding_dispositions}` (additive, backward-
  compatible). This is what makes "fixes applied" and "validated vs dismissed"
  machine-countable instead of prose-only.
- README "Why": replaced the vague "skip assumptions" with a concrete
  description (acting on unstated assumptions — a signature, a dependency, what
  "done" means).

## 1.18.0 - 2026-05-22

**Extensible, config-driven contributor profiles + cross-platform install.** Add
an AI reviewer to the consensus panel without writing code, choose your panel at
`consensus init`, and install consistently on Windows or Linux. Designed via a
4-way Workflow A consult (claude+codex+gemini+kimi) and reviewed via Workflow B
(codex). Strict TDD; full suite green.

### Added
- **Config-driven contributor profiles.** A profile (`name`, `kind`, `detect`,
  `invoke`, `env`, `output`, `install`, `auth`, `model`, `instructions`, …)
  describes everything quirky about one AI as data. Built-in profiles ship for
  claude/codex/gemini/kimi; add more via `contributors.profiles` in
  `.consensus/config.yaml`. **Adding an AI = adding a profile, no new code.**
- **`ProfileAdapter`** — a generic contributor adapter that reuses the shared
  dispatch core and is driven entirely by a profile. Kimi is now a first-class
  built-in profile (no more out-of-tree wrapper); its sealed provenance reports
  the correct model.
- **`consensus init` contributor selection** — interactive numbered multi-select
  with installed/missing status, a minimum of two contributors, and Claude
  optional. Non-interactive `--contributors a,b,c` for scripted installs.
- **Detect-and-guide setup** — for any selected AI whose CLI isn't installed,
  the wizard prints the OS-appropriate install + login commands (it never runs
  them).
- **Per-AI instruction files** — seeds shared operating guidelines into each
  selected AI's convention file (`CLAUDE.md` / `AGENTS.md` / `GEMINI.md`) as a
  non-destructive, idempotent managed block (`--no-instructions` to skip).

### Changed
- Committed `.mcp.json` now uses the cross-platform `consensus-mcp` entry point
  instead of the Windows-only `py -3.11` launcher.
- `requires-python` is now `>=3.11`.
- Contributor configs are more flexible: `claude` is optional and a per-contributor
  `contributors.adapters` entry is optional — whether a contributor is
  constructible is enforced (fail-closed) when the engine is built.

### Workflow B review (codex)
codex caught two real issues (gemini + kimi, run as bonus, were clean), both
fixed before release: the new multi-select UX was not yet wired into the wizard
flow, and `ProfileAdapter` computed the project root one level too deep. Both are
regression-tested.

## 1.17.5 - 2026-05-22

Gates/state + orchestration clusters from the 2026-05-22 code review
(`docs/code-review-2026-05-22.md`), one consensus-audited batch. Each finding was
independently re-verified before fixing (H-1 had already been dismissed as a
false finding; H-8's HIGH framing proved unsupported). Strict TDD; full suite
977 passed / 1 skipped / 0 failed (+24 tests).

### Fixed
- **H-3** `self_drive.cmd_close` emitted 5 JSON blobs (4 sub-commands + its own)
  → now suppresses the children's stdout and emits a single
  `{can_close, components}` object, so machine consumers can parse `close` again.
- **H-4** `_snapshot_state` dirty-detection hashed raw working-tree bytes, so
  under `core.autocrlf=true` every text file read as dirty on Windows → now uses
  `git hash-object` (applies the clean filter; matches the indexed blob).
- **H-5** the `iteration_closed` mutation-completeness gate returned `[]` (pass)
  on any git failure → now fails CLOSED with `mutation_completeness_unverifiable`
  when git inspection is unavailable (no git-optional mode exists, so this is
  non-configurable).
- **H-6** release gates passed on literal test-count strings (`"95 passed"`,
  `"60/60 tests passed"`) — brittle, and already failing (the suite had grown to
  96) → now parsed robustly (returncode + no failure word + `passed >= floor`).
  Also fixed a dead `scripts/` path that made the dispatch-codex gate a no-op.
- **H-7** under `timeout_policy=blocking`, a timed-out contributor counted toward
  `n_block` but did not veto a majority rule → it now does, while a responsive
  "soft no" still does not (majority semantics preserved). Convergence rationale
  now reports `n_block`.
- **M-11** `self_drive.cmd_transition` persisted nothing while its docstrings
  claimed it "records" transitions → contract corrected to stateless (the
  disposition ledger keeps its single authorized writer).
- **H-8** narrowed `_resume.py` bare `except Exception` to the established
  IO/parse set so programmer errors propagate (parity with `_self_drive`,
  iter-0036). The review's "silent nothing-in-flight" framing was not supported —
  the dispatch-log read already surfaces a warning.
- The four pre-existing `test_visibility_watchdog` failures (a time-bomb test)
  were de-time-bombed earlier on this branch.

### Audit
Workflow B consensus audit (codex-cli 0.132.0 + gemini-2.5-pro + kimi 1.44.0):
gemini and kimi clean (kimi endorsed all four design-judgment calls); codex's
lone HIGH finding (codex-rev-001 — claiming `git hash-object` skips clean
filters) was dismissed by a decisive experiment showing `git hash-object`
matches the stored normalized blob OID, refuting the premise.

## 1.17.4 - 2026-05-22

**Security cluster — fail-closed path/scope/auth boundaries.** A thorough code
review (codex+gemini consensus-audited) found several tools using operator/AI-
supplied file paths without containment or scope checks. All now fail closed via
a shared `_paths.resolve_contained` helper (mirrors the existing
`_author_review_packet` containment guard, Windows case-fold included). Each fix
has a TDD regression test (`tests/test_security_cluster_2026_05_22.py`).

### Fixed (security)
- **CR-1** `apply.codex_patch`: a `files_touched` path traversal could write
  outside the repo — now containment-checked before any read/write.
- **CR-2** `apply.codex_patch`: `files_touched` is now constrained to
  `goal_packet.allowed_files` (previously only `validate_disposition_index` ran,
  which never checks source-file targets).
- **CR-3** `build_review_packet`: `target_files` entries resolving outside the
  repo are skipped (previously read/exfil'd into the sealed packet).
- **CR-4** `scope_check`: empty / missing / non-list `allowed_files` now fails
  CLOSED (every touched file out-of-scope) instead of a silent allow-all.
- **CR-5** `apply.codex_patch`: `iteration_dir` must be under
  `consensus-state/active/` before authorization is read (a caller-chosen dir
  could otherwise self-authorize, defeating the goal_packet interlock).
- **H-2** `patch.stage_and_dry_run`: `file` paths are containment-resolved before
  read/stage (a `../` relpath previously escaped the staging dir on write).
- **M-6** `patch.stage_and_dry_run`: `gate_decision` now blocks on `critical`
  severity, not only high/blocking.

### Changed
- `.mcp.json`: dropped the hardcoded absolute-path env block; the server resolves
  the repo root via cwd + `__file__` walk-up, so the committed file is portable.
- Public-readiness: the git history rewrite + author-email scrub that 1.17.3 left
  "for an explicit decision" is **done** — all commits now use the GitHub noreply
  identity and the personal email/domain are removed from history (force-pushed).

### Known issues
- 4 `test_visibility_watchdog` tests fail. **Pre-existing, NOT a 1.17.4
  regression** (they fail on 1.17.3 too — verified by stashing the 1.17.4
  patches). Scoped for the next cut.

## 1.17.3 - 2026-05-22

**Public-readiness sanitization (round 2): removed remaining host-project name
references** that had shipped publicly in 1.16.x/1.17.x. The internal project
name appeared in code comments (`_delivery_readiness.py`), docs
(`delivery-gate.md`, `architecture/orchestration-spec.md`,
`architecture/phase-1-completion.md`, `spec_template.md`),
`validators/scope_check.py`, this CHANGELOG, AND the GitHub release notes for
v1.16.0 / v1.16.1 — all scrubbed to neutral wording ("internal project" /
"host-project" / generic media examples). No functional change.

### Still outstanding (maintainer decisions, NOT auto-done)
- `consensus-state/archive/imported-from-parent/` (~89 files) still contains the
  parent project's name/paths — removal is deletion of maintainer historical
  data.
- Two historical commit *messages* (v1.16.0 / v1.16.1) name the internal
  project + all commits carry a personal author email; scrubbing those needs a
  git history rewrite + force-push (destructive) — left for an explicit decision.

## 1.17.2 - 2026-05-22

**Fixes from the 1.17 consensus code review** (4-way: claude+codex+gemini+kimi —
the changes were independently audited, not self-shipped). Each fix has a test
that proves it (not vacuous).

### Fixed
- **Anchoring audit fails LOUD, not open** (codex-001 / gemini-001 / kimi-001,
  unanimous top finding): a crash in the lint previously returned `[]`,
  indistinguishable from "no bias found." It now surfaces an explicit
  `anchoring_audit_error` in the packet + a loud stderr line. An anti-bias gate
  must never silently disable itself.
- **`build_adapters` read the wrong config key** (codex-002): it read
  `contributors.config` (never populated) instead of `contributors.adapters`
  (what `default_config()` + `validate()` use), so per-contributor config (e.g.
  model overrides) was silently always empty. Now reads `.adapters` (legacy
  `.config` honored as fallback).
- **`validate()` rejects whitespace-only contributor names** (kimi-004).

### Changed
- **Acceptance test now goes through the REAL `register_contributor` +
  `build_adapters` path** (codex-003 / kimi-002,003), N=50 actually BUILDS (not
  just validates), and a NEGATIVE convergence case (blocking minority ⇒ no
  convergence) was added. Tests unregister in `finally` (registry isolation,
  codex-005).

### Note (schema $id)
- v1.17.1 changed `converged_plan_convention.schema.json` `$id` from a personal
  absolute URL to a neutral relative id (it is not `$ref`'d internally). If any
  EXTERNAL consumer keyed on the old `https://example.org/...` id, update to
  the relative id `consensus_mcp/schemas/converged_plan_convention.schema.json`
  (unanimous low finding: codex-006 / gemini-003 / kimi-005).

## 1.17.1 - 2026-05-22

**Public-readiness sanitization (code/schema personal-info).** Removes
machine-specific personal info from shipped code:
- `_import_parent_history.py`: dropped the hardcoded `DEFAULT_PARENT =
  C:\Users\<you>\...` absolute path (a personal path in a public package);
  `--parent` is now required.
- `schemas/converged_plan_convention.schema.json`: `$id` changed from a personal
  domain (`example.org`) to a neutral relative id (not `$ref`'d anywhere).

### Still outstanding (flagged, NOT auto-done)
- `consensus-state/archive/imported-from-parent/` is a large imported history
  archive full of personal paths + other-project references. Removing it is a
  deletion of maintainer historical data — left for an explicit maintainer
  decision, not auto-`git rm`'d.
- Cosmetic `C:\Users\<you>\...` EXAMPLE paths remain in several `docs/` files.

## 1.17.0 - 2026-05-22

**Open contributor model — any AI, any number (min 2, no upper cap) + mechanical
anchoring lint.** Answers "will a clean install work with 2 or 20 or 200 AIs?":
yes. Previously a closed enum (`KNOWN_CONTRIBUTORS`) + a fixed adapter dict
rejected any contributor outside `{claude,codex,gemini}`. Now contributors are an
OPEN set — register any adapter under any name with zero core edits.

### Added
- `_engine_factory.register_contributor(name, AdapterClass)` / `unregister_contributor`
  / `known_contributor_keys()` — an open contributor registry. `build_adapters`
  resolves registered ∪ built-in and fail-closes with a register hint.
- `consensus_mcp/_anchoring_lint.py` — a MECHANICAL term-skew linter that flags
  orchestrator contributor-anchoring (one peer named N×, others 0×) at
  goal-packet author time, contributor-set-configurable (never hardcoded).
  Wired into `_author_review_packet` (emits an `anchoring_audit` block).
- `tests/test_n_contributor_acceptance.py` — DECISIVE acceptance test: N=2 and
  N=20 ARBITRARILY-NAMED contributors run an iteration to convergence; N=50
  validates (no cap). Independently QA-verified + mutation-proven genuine.

### Changed
- `config.validate()` — contributor validation is now STRUCTURAL (no closed
  enum); per-mode min-2 (propose-converge / sequential / strict-majority) is
  preserved. Constructibility is enforced at build time by `engine_factory`.
- `KNOWN_CONTRIBUTORS` += `kimi` (it was excluded — a second-class identity that
  also blinded the anchoring lint to kimi-anchoring; found by independent QA).

### Known limitations (honest)
- A REAL external CLI AI still needs a `ContributorAdapter` subclass + a
  `register_contributor` call; CONFIG-ONLY onboarding of an arbitrary CLI AI (a
  generic `SubprocessCliAdapter`) is a tracked follow-up. The framework is open;
  the config-only convenience is not yet shipped.

Origin: internal consensus iterations uniform-contributor-arch +
orchestrator-framing-bias + qa-verifier-mechanism (2026-05-22); the kimi
exclusion + a docstring overclaim were caught by an independent QA subagent, not
self-review.

## 1.16.1 - 2026-05-22

**Follow-up completeness gate — mechanically binds the *existing*
complete-fulfillment rule.** Extends the 1.16.0 delivery-gate so a task that
declares an `action_class` cannot mint a delivery token while that class's
required follow-ups are unresolved. Prevents the "tunnel-vision" failure mode
(e.g. merge a version bump to main but skip cutting the GitHub release). This is
NOT a new rule — CLAUDE.md "Complete fulfillment" + Karpathy #4 already require
it; passive rules don't bind, so this enforces it the same way 1.16.0 enforced
verify-before-report.

### Added
- `consensus_mcp/_followup_completeness.py` + `consensus_mcp/data/required_followups.yaml`
  (config-driven action-class → required-follow-ups map: e.g. `version_bump` →
  `[tag, github_release, changelog_entry]`). A follow-up is satisfied when
  RESOLVED or EXPLICITLY DEFERRED-WITH-REASON.
- `consensus_mcp/tests/test_followup_completeness.py` (6 tests).

### Changed
- `consensus_mcp/_delivery_readiness.py`: `mint_delivery_token()` accepts
  `action_classes` and REFUSES while required follow-ups are unresolved; token
  gains `followups_resolved` / `open_followups`; `verify_delivery_token()`
  re-checks against the live ledger. Declared `action_class` is authoritative;
  heuristics may only ADD requirements (fail-toward-enforcement).

### Deferred (with reason)
- Wiring a `required_followups_unresolved` stop-rule into `_self_drive.close`
  (a second enforcement at iteration-close): editing the core stop-rule
  contract needs its own pass against the contract validator + full self_drive
  suite. The token chokepoint already blocks the failure at delivery time.
  Deferred *explicitly with reason* — the discipline this gate enforces.

## 1.16.0 - 2026-05-22

**Delivery-readiness gate (anti-self-judge enforcement) — HEADLINE FEATURE.**
Fixes a major logic flaw: an agent self-judging artifact soundness instead of
routing it through consensus, which caused real-world bad deliverables and work
stoppage. The gate makes that failure mode *mechanically impossible*, not merely
discouraged.

### Added
- `consensus_mcp/_delivery_readiness.py`: fail-closed gate. `mint_delivery_token()`
  refuses unless `design_consensus_ref` resolves to a SEALED/closed consensus
  iteration (`closing_state` in `{quorum_close_passed,
  implementation_ready_apply_landed}`, mirroring `_release_gate_check.gate_real_iter`),
  >=2 non-claude reviewers vetted it, and `known_flaws == []` (unless
  `operator_ack`). `verify_delivery_token()` is fail-closed and also rejects a
  stale/edited artifact (sha256 drift). Reuses
  `_self_drive._canonical_sha256_of_yaml_file` + `_resolve_repo_root`.
- MCP tools `delivery.request` / `delivery.mint` (`consensus_mcp/tools/delivery_gate.py`),
  registered in `server.py` — the portable enforcement surface.
- CLI: `python -m consensus_mcp._delivery_readiness {mint,verify}`.
- `contrib/delivery_gate_pretooluse.py`: optional Claude-Code PreToolUse hook template.
- `consensus_mcp/tests/test_delivery_readiness.py`: 7 fail-closed tests.
- `docs/delivery-gate.md`: integration guide.

### Why it binds
The invariant is unforgeable by the agent: you cannot obtain a delivery token
from your own assertion — only a prior cryptographically sealed consensus
iteration. Origin: internal consensus iterations
`iteration-antistall-protocol-2026-05-22` + `iteration-antistall-impl-2026-05-22`
(both 4/4 unanimous).

## 1.15.11 - unreleased

_Development branch. No iteration scoped yet; release-cut Friday if any iter lands this week (release-cadence doctrine)._

## 1.15.10 - 2026-05-16

**Stderr-pipe backpressure modeling — the v1.15.9 named
follow-up, Workflow A converged.** `test_stderr_drain_prevents_
deadlock` now genuinely proves what its name claims: a real
codex deadlocks if its stderr pipe fills and nobody drains it.

- `_FakePipeReader` models a **byte-bounded** OS pipe buffer
  (not line-count); `write()` is **non-blocking** (returns
  buffered/would-overflow) so the runner never parks inside
  `poll()` — it parks normally in the virtual clock, so nothing
  can fall back to a real-time ceiling.
- A private, default-on `_invoke_codex(_drain_stderr=…)` test
  seam (mirrors the v1.15.9 `_sleep=` precedent — zero default
  behaviour change) drives a **parametrized mutant gate**: with
  the stderr reader the dispatch exits cleanly; without it the
  same clean-exit contract **fails deterministically and fast**
  (the deadlock is observed via a notify-driven wait, not a
  timeout). The gate has teeth both ways — a production
  stderr-drain regression now fails the test.
- Workflow A consult + a multi-pass cross-AI Workflow B audit
  (codex+gemini) that caught and fixed real defects across the
  passes: a corrupted/incomplete handed-off implementation, an
  unwired seam, a timeout-driven proof, a semantic-inversion in
  the mutant gate, line-vs-byte capacity, and a blocking-poll
  fallback. gemini clean on the model throughout; codex drove
  the rigor — converged: codex `goal_satisfied=true`/0-findings,
  gemini clean.

Per provisional-until-proven, v1.15.10 is cut only after the
release commit passes ≥3 distinct green Windows-CI run attempts
(attempts-API verified). Full suite 969 passed / 1 skipped.

## 1.15.9 - 2026-05-16

**Deterministic streaming-test harness — the v1.15.8 Q2(a) named
follow-up, root-caused via Workflow A consult.** v1.15.8 shipped
the `_locked_append` integrity fix with four timing-fragile
`test_dispatch_codex_streaming.py` tests skipped on Windows CI
(`@_FLAKY_WINDOWS_CI`, the converged-plan-sanctioned interim).
Consult `iteration-v1159-deterministic-clock-harness`
(claude+codex+gemini, weighted-synthesis, shared-prior self-check
PASSED — agreement on diagnosis, DISAGREEMENT on mechanism;
dogfoods the v1.15.1 convention incl. an independent_safeguard).

- **Root cause:** test↔runner coordination-by-hope — the harness
  advanced a clock + real `time.sleep` + `join(timeout)` and hoped
  the daemon runner/reader threads were scheduled within a
  wall-clock budget. Loaded Windows GitHub runners starved them
  → "runner did not finish". Not a product defect (Linux CI +
  local always green; logic is driven by the injected `time_fn`).
- **Fix (mechanism — weighted-synthesis of 3 distinct proposals):**
  a private, keyword-only `_sleep=` seam on `_invoke_codex`
  **defaulting to `time.sleep`** (zero production behavior change;
  chosen over monkeypatching global `time.sleep` — no global-state
  leak) + a `threading.Condition`-backed `_SyncClock` (lockstep
  `wait_runner_parked` / observable `wait_for` / Condition-driven
  `_FakePipeReader`). All real sleep is removed from the drive
  path; the harness is now event-driven and deterministic.
- **Independent safeguard (ships with the fix):** `_SyncClock.
  release_all()` — any failed/timed-out wait frees every waiter,
  terminates the fake proc, and fails LOUD with harness state, so
  a misdiagnosed or deadlocked harness can never wedge a CI job
  (works regardless of whether the timing hypothesis is right).
- **Scope:** class-wide — all 8 clock-driven tests migrated;
  `_drive_clock_until_done` deleted; `@_FLAKY_WINDOWS_CI` DELETED
  (no phased follow-up — gates concrete, cost bounded);
  `test_stderr_drain_prevents_deadlock` keeps REAL reader threads.
- **Verification discipline (provisional-until-proven):** v1.15.9
  is cut ONLY after the skip is gone, the four tests RUN on
  Windows CI, and **≥3 consecutive green Windows CI runs of the
  same commit** (attempts-API verified — one green run / N polls
  of one run id is explicitly insufficient).

**Workflow B audit (codex + gemini, multi-pass — caught real
defects, integrated not dismissed):**
- Pass-1: gemini clean (`goal_satisfied=true`, 0 findings);
  codex 0 blocking, `codex-rev-001` (governance, high) — the
  sealed goal_packet `forbidden_files` listed `_dispatch_codex.py`
  while Q1 + `deliverable.files` ratified the seam there.
  Integrated: goal_packet scope reconciled to the converged plan
  (no code change; the authorization pre-existed via the
  `non_goals` Q1 carve-out).
- Pass-2: gemini clean again; codex raised a NEW **blocking**
  `codex-rev-001` (correctness) — `_drive`/`_drive_streaming`
  ignored the per-round `wait_for` return, so a real wedge would
  hang ≤ `max_rounds`×`_CEILING` (~21 min) before the safeguard
  fired, contradicting the claimed deadlock-free invariant.
  gemini cleared Q2 both passes on design intent; codex traced
  the control flow (convergence ≠ correctness). Integrated: every
  per-round/per-step wait return is acted on — `False` →
  `release_all()` + raise immediately; `max_*` exhaustion is a
  hard fail. The invariant is now true (genuine wedge fails in
  ≤20 s). Pass-3 re-dispatched for confirmation.

- Pass-3: codex confirmed the pass-2 deadlock-invariant fix
  RESOLVED; gemini clean a 3rd time. codex pass-3 raised two
  valid coverage-fidelity findings (reasoning from "H2 coverage
  must be preserved"): (a, blocking) the operator-abort test
  never asserted the SIGTERM/terminate side-effect — a
  `_terminate_process_tree` regression would pass while leaking
  a live process; (b, high) the rewrite dropped the original's
  consume-then-go-silent synchronization so the silence test
  could degrade from post-stream to startup-silence coverage.
  Integrated: `StreamingFakeCodexPopen._terminated` +
  `factory.instances` and an explicit SIGTERM assertion;
  `_drive_post_stream` (process N lines, THEN drive) + an
  assertion that the abort is tied to prior streamed output.

- Pass-4: codex 0 blocking (pass-3 resolved); gemini clean 4th.
  Three valid non-blocking findings integrated: (a, high)
  `release_all()` didn't terminate the fake proc → daemon runner
  spun after a pre-exit timeout (claim-vs-code) — fixed by
  `is_released()` + fake `poll()` self-terminate; (b, high)
  operator-abort test not provably mid-run before signal — fixed
  via `_advance_until_streamed`; (c, medium) reader-produced log
  events had no clock waker so event-count waits fell back to
  `_REPOLL` — fixed by a `readline()`-entry notify (true
  happens-before).

- Pass-5: codex 0 prior blocking re-raised; gemini clean 5th.
  One new blocking (integrated): the lockstep heartbeat driver
  advanced exactly `interval`/step so it only proved "one per
  step" — an emit-every-poll regression still passed; the
  interval-GATE coverage the pre-rewrite test had was lost.
  Fixed: each round asserts NO heartbeat at +½interval, exactly
  one at the boundary.

- Pass-6: codex 0 prior blocking re-raised; gemini clean 6th.
  New blocking: `test_stderr_drain_prevents_deadlock` doesn't
  model pipe backpressure. A focused **scope-adjudication
  consult** (claude+codex+gemini, weighted-synthesis;
  `iteration-v1159-stderr-scope`) verified UNANIMOUSLY — each
  independently checking `git show da62d54^` — that this is a
  PRE-EXISTING gap (identical in v1.15.8 baseline), NOT a
  regression, NOT a valid v1.15.9 blocker. Resolved-in-part
  (zero-risk, shipped): de-overclaimed docstring + an assertion
  that production's real reader threads drained ALL scheduled
  stdout/stderr (`_FakePipeReader._idx`), so a stderr-reader
  regression now fails deterministically. Full OS-pipe
  backpressure modeling = tracked follow-up with a concrete
  blocker (its own determinism-risking design surface) — see
  `docs/advisories.md` 2026-05-16, with the converged design
  seed + mandatory `release_all`/mutant-gate.

- Pass-7: codex-v1159-wfb-6 (stderr-drain) RESOLVED (bounded fix
  + scope verdict accepted); gemini clean 7th. New blocking on
  `_drive_heartbeats`: startup used a bare assert (teardown
  bypass) + the half-interval gate missed a wrong-threshold
  regression. Integrated codex's patch + refinements: startup
  uses release_all()+join; `gap = max(interval/1000,
  poll_interval*5)` (poll-safe + float-safe) checks no-emit at
  interval-gap, crosses by 2*gap. (codex's exact-epsilon both
  had a float edge AND was < poll_interval so the runner never
  re-polled — caught via TDD.)

The multi-pass Workflow B audit caught **9 substantive defects**
self-certification would have shipped (governance scope;
deadlock-invariant claim-vs-code; SIGTERM coverage; post-stream
coverage; release_all-doesn't-terminate; operator mid-run
determinism; reader-event waker; heartbeat interval-gate
coverage; heartbeat startup-teardown + poll/float-safe gate),
plus a unanimous scope-adjudication consult disposing the
stderr-drain finding (pre-existing, not a regression — resolved
in part + named follow-up, `docs/advisories.md` 2026-05-16).
gemini was clean on ALL 8 passes (determinism/design prior);
codex drove every finding (coverage-preservation + control-flow
prior) — distinct, non-redundant priors; unanimity would have
shipped a coverage-hollow harness. **Audit CONVERGED: pass-8
codex `goal_satisfied=true`/0-findings, gemini clean ×8.**

Verification discipline (provisional-until-proven): the
audit-clean commit `0484cf5` passed the
≥3-consecutive-green-Windows-CI gate — verified as **3 distinct
completed/success run attempts** with distinct timestamps via
the GitHub attempts API (NOT N polls of one run id). v1.15.9 is
cut on the release-content commit, which re-runs the same gate
so the proven artifact IS the tagged artifact (one green run is
explicitly insufficient).

## 1.15.8 - 2026-05-15

**Windows-CI flakes — root-caused via Workflow A consult.** The
re-enabled CI kept showing the *same commit* green on one run, red
on another (2 Windows-only flakes). Consult
`iteration-v1158-flaky-ci-and-locked-append` (claude+codex+gemini,
weighted-synthesis, shared-prior self-check PASSED — three distinct
differentials; dogfoods the v1.15.1 convention incl. an
independent_safeguard).

- **Q1(d) — real defect in `_visibility_watchdog._locked_append`
  (the sealed-provenance/audit + watchdog integrity primitive).**
  It caught `OSError` from `msvcrt.locking` then wrote **UNLOCKED**,
  silently losing audit lines under contention (windows-py3.10 CI:
  `test_locked_append_serializes_concurrent_writes` lost 4 of 50
  from a 50-thread, ONE-process fan-out — intra-process contention,
  the v1.15.7 OS-lock-vs-threads class in a different primitive;
  latent + CI-exposed, not introduced this session). Fix: a
  module-level `threading.Lock` (`_APPEND_LOCK`) serializes all
  in-process callers deterministically; the OS lock now only
  governs genuine cross-process contention; and the failure path
  **fails LOUD** (raises) instead of a silent unlocked write — an
  independent safeguard so a sealed-provenance/audit log can never
  be silently incomplete, regardless of *why* the OS lock failed.
  Fixes the test deterministically (not a quarantine).
- **Cross-process Windows lock — `codex-rev-001` (pass-1 blocking,
  resolved).** The Workflow B pass-1 audit caught a real residual
  hole: `msvcrt.locking` locks N bytes from the *current* file
  position, and `_locked_append` opens the log `"ab"`, so that
  position is the *calling process's EOF* — which differs per
  process as the file grows. Cross-process writers therefore
  locked *different* byte ranges and could append concurrently:
  the fail-loud safeguard's premise was not actually met
  cross-process. Fix: `f.seek(0)` before `msvcrt.locking(fileno,
  LK_LOCK, 1)` so every process contends on the *same* fixed byte
  (offset 0). Writes still land at EOF — Python `"ab"` opens with
  `_O_APPEND`, so the write goes to EOF regardless of the seek
  (the seek positions only the lock range); proven by the
  50-thread test (no overwrite/loss).
- **Q2(c interim) — timing-fragile heartbeat-pattern tests.** Four
  tests drive the `_invoke_codex` runner thread + advance a
  controllable clock with real `time.sleep` ticks then `join`;
  they flake on loaded Windows GitHub runners (not a product
  defect — same logic green on Linux CI + local; driven by the
  injected `time_fn`). Per the converged plan's explicit (c)
  allowance, `skipif(win32 and GITHUB_ACTIONS)` — they still run on
  **Linux CI every push + local Windows dev**, so coverage is
  retained. **Named follow-up:** rework `_ControllableClock` into a
  deterministic synchronizing clock so they run everywhere and the
  skip drops (advisory 2026-05-15).
- **Verification discipline:** per the converged plan
  (provisional-until-proven), v1.15.8 is cut ONLY after a
  determinism argument AND **≥3 consecutive green Windows CI runs**
  of the same commit — one green run is explicitly insufficient
  (the over-claim that bit this session).

Full suite 968 passed / 1 skipped / 0 regressions. Workflow B
audit: gemini clean both passes (`goal_satisfied=true`, 0
findings); codex pass-1 blocked on `codex-rev-001` (cross-process
byte-range) → fixed → codex pass-2 **0 blocking objections**. The
two non-blocking codex pass-2 findings (Q2(c) skip breadth +
deterministic-rework deferral) are integrated, not dismissed, in
`docs/advisories.md` (2026-05-15 v1.15.8) with the shared-mechanism
evidence and the explicitly-named residual Windows coverage gap.

## 1.15.7 - 2026-05-15

**CI fix: 4 codex-dispatch tests required a real `codex` binary.**
First green CI surfaced this: `test_dispatch_codex.py`'s
`test_main_smoke_with_mocked_codex`,
`test_main_smoke_flag_with_env_proceeds`,
`test_main_sealed_packet_embeds_dispatch_provenance`,
`test_dispatch_done_includes_archive_path_and_audit_id` predate the
iter-0037 refactor that moved the codex invocation from
`subprocess.run` to `subprocess.Popen`. They still mock only
`subprocess.run` (now just the `codex --version` probe), so the
actual dispatch executes the **real** codex via Popen. They passed
on dev machines purely because a real `codex` is on PATH; on CI
runners with none they failed `CodexInvocationError: codex binary
not found` (windows legs → exit 1) or reached a process-group kill
that SIGTERM'd the runner (ubuntu legs → exit 143). It stayed
hidden because CI was dormant v1.13.0→v1.15.3 (main-only trigger)
until v1.15.4 re-enabled it — so these were never actually
CI-covered.

Honest interim fix: a `skipif(shutil.which("codex") is None)`
guard so they skip cleanly when no real codex is present (they are
de-facto integration tests as written). Verified both ways: codex
present → all 4 run + pass (no regression, full suite 968/0);
codex absent (simulated) → 4 skip, 0 failed. **Named follow-up
(tracked):** rewrite the 4 to mock the Popen path via the existing
`make_fake_codex_popen_factory` / `popen_factory=` kwarg (the
pattern the genuinely-hermetic `main()` tests already use) and drop
the skip. Test-only change; no production code, no behavior.

Two more pre-existing CI-env debts re-enabled CI surfaced, fixed
here too:

- **Validator self-test `20/21` (windows legs).**
  `run_validator_tests.py::test_packet_build_sanitizes_injection`
  failed because `review_packet_known_good/input.yaml` listed
  `target_files: agent-loop/tests/fixtures/prompt_injection_doc.md`
  — a **parent-project path never rewritten during the standalone
  extraction**. The doc actually lives at
  `consensus-state/tests/fixtures/prompt_injection_doc.md`; the
  stale path meant the builder read nothing, sanitized nothing, and
  the empty `sanitization_log` failed the `>=7` check. Repointed the
  fixture path (the doc contains all 7 `SANITIZE_PATTERNS`); now
  **21/21**. Not Windows-specific — reproduced locally; latent since
  iter-0001, masked by dormant CI.
- **ubuntu job-kill (exit 143 / "operation canceled" ~25%) —
  ROOT-CAUSED + fixed.** Not a hang and not the 4 above:
  `test_dispatch_codex_streaming.py::_FakePopen.pid` returned
  **`0`**. On POSIX, `_dispatch_base._terminate_process_tree` does
  `os.killpg(os.getpgid(proc.pid), SIG*)`; `os.getpgid(0)` resolves
  to **the caller's own process group**, so the abort/watchdog
  tests SIGTERM'd the pytest / GitHub-Actions job *itself* (instant,
  hence pytest-timeout never fired). Windows uses the
  `send_signal`/`taskkill` branch, so it was masked locally and CI
  was dormant v1.13.0→v1.15.3 so it stayed hidden until v1.15.4
  re-enabled CI. Latent since iter-0039. Fixed two ways:
  (a) corrected the synthetic pid to a never-live value so
  `os.getpgid` raises `ProcessLookupError` → the documented
  `proc.terminate()` fallback runs; (b) a suite-wide
  `tests/conftest.py` autouse guard that neutralizes
  `os.killpg`/`os.getpgid` so NO test can ever signal a real
  process group (production `_terminate_process_tree` logic still
  runs; every `dispatch_aborted` assertion unchanged). `pytest-
  timeout` retained as permanent hardening. Verified: targeted
  abort/dispatch suites 144/0; full suite 968 passed / 1 skipped /
  0 regressions with the guard active.
- **POSIX-only `signal` AttributeError (the FINAL ubuntu blocker).**
  Once (3) stopped the runner self-kill, ubuntu ran to completion
  and surfaced the real last failure:
  `test_terminate_process_tree_uses_signal_on_posix` asserted
  `_dispatch_codex.signal.SIGTERM`, but the iter-0037 refactor moved
  `_terminate_process_tree` to `_dispatch_base` and `_dispatch_codex`
  only re-imports the function, not the `signal` module → 
  `AttributeError`. This test is Windows-skipped, so it only ran on
  POSIX CI — masked the entire time CI was dormant. Fixed: assert
  against the stdlib `signal.SIGTERM` enum directly (module-agnostic;
  the same object `_dispatch_base` passes). ubuntu was "1 failed,
  898 passed, 10 skipped" — this was the 1.
- **dispatch-log write atomicity (real defect — found by auditing
  the audit).** With the runner-kill gone, windows-py3.10 surfaced
  a non-JSON line in `dispatch-log.jsonl` during abort/thread
  teardown. The Workflow-B audit's pass-1 (gemini) called it
  test-only on the premise "the production append is already
  lock-atomic"; **first-hand code verification REFUTED that** —
  `_dispatch_base._log_dispatch` was a *bare unlocked* `open("a")`
  + `write`, while the streaming path emits events from the main
  thread AND both reader threads concurrently. So torn lines were
  possible in production, not just the test. Per the
  convergence-correctness doctrine the 2-AI agreement-on-a-false-
  prior was rejected; corrected fix-shape **A+C**, unanimously
  re-confirmed by codex + gemini on the corrected premise:
  - **(A)** `_log_dispatch` now appends via the codebase's
    existing OS-exclusive-lock primitive
    (`_visibility_watchdog._locked_append`; msvcrt/fcntl) — the
    same mechanism the audit log uses. Concurrent emitters +
    abrupt teardown can no longer tear a line.
  - **(C)** the test-only `_read_log_events` skips a line that
    fails `json.loads` — a defensive telemetry-reader belt, not
    the primary fix.
  Full suite 968 passed / 1 skipped / 0 regressions with A+C.
  **Workflow B audit clean** (corrected premise): codex pass-2 +
  gemini pass-2 both goal_satisfied=true, 0 blocking, 0 findings;
  shared-prior self-check passed (the false prior was exposed by
  artifact verification, not laundered).
- **(A) primitive corrected — CI refuted the pass-2-audited
  implementation.** Pass-2's (A) routed `_log_dispatch` through
  `_visibility_watchdog._locked_append`. Both auditors endorsed
  the *concept*, but that helper uses a **blocking cross-process**
  lock (`msvcrt.locking LK_LOCK` / `fcntl.flock`); the streaming
  concurrency is **intra-process** (main + stdout + stderr reader
  threads of one dispatcher), and contending that blocking OS lock
  across the same process's threads on Windows stalled the runner
  thread → windows-py3.12 regression (`test_heartbeat_fires_at_
  interval` "runner did not finish"; windows-py3.10 was fixed,
  proving (A)+(C)'s direction right, but py3.12 traded). The
  multi-platform CI caught what audit+local-green could not — the
  reason CI is the load-bearing oracle for thread/platform bugs.
  Corrected (A): a module-level `threading.Lock`
  (`_DISPATCH_LOG_LOCK`) guarding a plain text append — the
  correct, deadlock-free primitive for intra-process thread
  serialization. Cross-process serialization of a shared
  dispatch-log under *parallel dispatcher processes* is a
  separate, unobserved concern, named explicitly (NOT solved with
  the blocking OS lock that just regressed). Full suite 968/1-skip/
  0-reg; the previously-stalling test passes locally. Re-audited
  (pass-3) on the corrected primitive.

## 1.15.6 - 2026-05-15

**Literal-zero pass** — completes the v1.15.5 identifier migration
per the operator "true zero" directive. The v1.15.5
changelog/commit/doctrine that *documented* the migration
necessarily still contained the legacy tokens (you cannot record
"X was removed" without writing "X"). Those are now phrased only
obliquely ("the former upstream project name", "the prior account
handle, now `stgarca`"). The entire working tree (tracked +
gitignored scratch + binary caches; `__pycache__` purged) was
swept, and a second `git filter-repo` pass (same substitutions)
cleaned all historical blobs, every commit/tag message, and the
snapshot branch. Verified: **zero literal occurrences in any blob
across every ref, in any commit message, in any tag message, and
in the working tree (including binary)** — including the record of
the migration itself. 18 release tags + all branches intact; full
suite green; a fresh pre-rewrite backup bundle was kept outside
the repo. Semantically identical to v1.15.5 (de-literalization
only; no doctrine/behavior change — the substance was
Workflow-B-audited in v1.15.5). Every published tag SHA changed
again; tag-pinned `pipx …@vX.Y.Z` URLs still resolve.
Canonical repo: `github.com/stgarca/consensus-mcp`.

## 1.15.5 - 2026-05-15

**Identifier migration + provenance scrub + doctrine reconciliation.**

Operator directed (1) removal of every reference to the former
upstream project's name, and (2) migration to the renamed GitHub
account (now `stgarca`). Both reached immutable commit/tag
messages, so — with explicit operator authorization — a full
`git filter-repo` history rewrite was performed:

- **Rewrite:** literal substitution of the former upstream
  project name → `upstream` and the prior account handle →
  `stgarca`, across ALL blob contents AND commit/tag messages,
  all 18 branches + 66 tags (127 commits). Verified before
  pushing: zero occurrences of either legacy token in any blob or
  message across every ref; all 17 release tags + branches
  present; full suite green on the rewritten tree (substitutions
  internally consistent). Pre-rewrite safety bundle kept outside
  the repo.
- **Consequence (artifact-scoped truth):** every published tag SHA
  changed. Tag-pinned `pipx install …@vX.Y.Z` URLs keep working
  (tags moved with the rewrite); any raw-commit-SHA pin or clone
  against the previous account remote is dead. The canonical repo
  is now `https://github.com/stgarca/consensus-mcp`.
- **Doctrine reconciliation:** the v1.15.4 doctrine says `main` is
  never force-pushed. This rewrite force-pushed everything, so the
  bundled `consensus-workflow` skill now carries a
  **sanctioned-exception carve-out**: a full-history rewrite is the
  ONE non-routine reason to force-push `main`, gated on explicit
  authorization + verified backup + pre-push verification + suite
  green. Leaving the doctrine contradicting the action would be the
  exact currency-drift v1.15.3/v1.15.4 fixed.
- **`consensus-state/README.md`** added (tracked) — documents the
  runtime-state tree + recovery, and (doctrine-correctly, via a
  fresh commit rather than more history surgery) relabels the
  GitHub `consensus-state/` folder off the old root commit the
  operator flagged.
- **Literal-zero pass:** a follow-up scrub removed the legacy
  tokens from this changelog/commit/doctrine wording itself (they
  are described only obliquely now) and from snapshot scratch, so
  the repo contains zero literal occurrences anywhere — including
  the record of the migration itself.

No engine/config/behavior code touched. Full suite green. Workflow
B audit: codex + gemini (bundled-doctrine change).

## 1.15.4 - 2026-05-15

**Repo-presentation + CI + branch-doctrine fix.** Operator observed
that the GitHub landing page looked stuck at v1.13.0 and showed a
hallucinated "v2.0.0" commit label on `.github/workflows/`.

Root cause: the pre-v1.15.4 release-branching convention froze
`main` forever (releases lived only on `v<X.Y.Z>` branches/tags,
never merged back). `main` is GitHub's default branch, so the
landing page was frozen at v1.13.0 — and, more seriously,
`.github/workflows/test.yml` triggered **only** on `main`, so
GitHub Actions CI was **dormant from v1.13.0 → v1.15.3** (every
release ran on a `v*` branch CI never saw; those releases were
verified by local pytest only).

- **CI** (`.github/workflows/test.yml`): now triggers on push/PR to
  `main` **and** `v*` branches, so release-branch work is actually
  exercised by GitHub Actions.
- **Branch doctrine evolved** (`consensus-workflow` SKILL.md): the
  "`main` frozen forever / never merge back" convention is replaced
  by "`main` = latest released state; every release cut
  fast-forwards `main` to the just-cut tag (clean ff, never a merge
  or force-push); development continues on `v<next>` branches." A
  new cut-sequence step 8 documents the fast-forward with an
  ancestor-check safety guard (and step 3 moves README install/
  status currency pre-tag, since the tag's README is now the
  landing page). Updating the bundled doctrine here
  prevents the same currency-drift v1.15.3 just fixed.
- **`main` fast-forwarded** from v1.13.0 (`64f70ec`) to the v1.15.4
  tag — a verified clean fast-forward (no history rewrite, no
  dropped commits, no force-push). The GitHub landing page now
  reflects the current state, including the accessible README.
- **README** rewritten as an accessible ~150-line summary (was 364
  dense lines); deep internals moved behind CHANGELOG/`docs/`
  links. Load-bearing facts preserved.

No engine/config/behavior change. Full suite green. Workflow B
audit: codex + gemini.

## 1.15.3 - 2026-05-15

**Bundled-doctrine + status currency hot-patch** — doc/string only,
no behavior change. Triggered by an operator question ("on a new
install, will 2-AI follow all the upgraded workflow lessons, same
as 3-AI?"). Investigation answer: **yes — doctrine + v1.15.1
machine-enforcement are AI-count-AGNOSTIC, scoped by workflow mode,
never by contributor count.** But the investigation found shipped
v1.15.2 artifacts carrying stale forward-references that misled
every new installer (2-AI and 3-AI identically).

Scope set by a **Workflow A consult** (`iteration-v1153-bundled-
skill-currency`; claude + codex + gemini; weighted-synthesis;
shared-prior self-check PASSED-WITH-CORRECTION — fast 3/3 unanimity
on scope was partly a shared prior; claude's differing differential
surfaced F7, which was integrated, not laundered by the unanimity).

Fixed (one failure mode — a shipped artifact's forward-reference
whose target version already came due):
- **F1** `consensus-workflow/SKILL.md`: the converged-plan
  convention is no longer described as "machine validation is a
  sequenced follow-up" — it is **machine-enforced as of v1.15.1**
  (`validate_converged_plan` + fail-closed seal-time gate +
  `convergence.converged_plan_enforcement` knob, default
  `graduated`).
- **F2** same skill, Gemini section: empty gemini output is NOT a
  429 — `GEMINI_CLI_TRUST_WORKSPACE` guidance added (fixed v1.15.2;
  manual workaround only ≤ v1.15.1; don't burn the 429 budget).
- **F3** both bundled skills (`consensus-workflow` +
  bootstrap `consensus`): Workflow C engine status corrected — it
  did **not** ship in v1.15.0; it is UNIMPLEMENTED as of v1.15.2,
  no committed target version.
- **F4** `workflow_engine.py`: the Workflow C `NotImplementedError`
  message + comment no longer promise "lands in v1.15.0" (string/
  comment only — no control flow; no test pinned it).
- **F7** `docs/workflows/workflow-c-autonomous.md`: 5 stale
  "v1.15.0" forward-references corrected (the consult's
  shared-prior-correction finding — the doc F4's text points at).
- **Q4** `consensus-workflow/SKILL.md`: new normative
  **consistency invariant** stating doctrine + enforcement are
  workflow-mode-scoped, never contributor-count-scoped — 2-AI and
  3-AI installs governed identically; only the convergence-rule
  default differs (unanimous@2 vs strict-majority@3). Converts the
  operator's implicit invariant into a written guarantee.

No invented replacement version anywhere (artifact-scoped truth —
that would repeat the exact defect). Code+doc bundled in one tag
per the shared-failure-mode hot-patch doctrine (verified precedent:
v1.14.5, v1.14.6). Full suite green, 0 regressions. Workflow B
audit: codex + gemini.

## 1.15.2 - 2026-05-15

**gemini-dispatch workspace-trust fix** — closes the v1.15.1
named blocker (advisory 2026-05-15, now **resolved**).

`consensus_mcp/_dispatch_gemini.py` now injects
`GEMINI_CLI_TRUST_WORKSPACE=true` into the gemini subprocess env
via a new `_gemini_subprocess_env()` helper (returns a COPY of the
parent env; forces the var to `true` even if an inherited value is
`false`; never mutates `os.environ`). gemini CLI
`0.43.0-preview.0`+ refuses headless runs in an "untrusted"
directory — it writes the trust error to stderr and produces
**empty stdout**, which the dispatcher then fails as
`GeminiOutputParseError`.

**Empirically verified 2026-05-15** (the v1.15.1 audit diagnosed
this first-hand; gemini pass-1 failed twice for exactly this):
- `--skip-trust` alone (still passed, defense-in-depth) is NOT
  load-bearing on this version — gemini bypassed trust but went
  autonomous and 429'd.
- `GEMINI_CLI_TRUST_WORKSPACE=true` produced clean deterministic
  output, and the two clean v1.15.1 Workflow B audit approvals.

`_dispatch_gemini.py` was in the v1.15.1 iteration's
`forbidden_files`, so the dispatcher-level fix was correctly
deferred to this version rather than patched out-of-scope.

Workflow B audit clean: gemini `goal_satisfied=true`, 0 blocking,
0 findings — **dispatched from source with
`GEMINI_CLI_TRUST_WORKSPACE` explicitly unset**, so the audit pass
of this fix was made possible BY this fix (end-to-end in-vivo
proof; acceptance gate A4). codex: 0 blocking; doc-accuracy
findings on the advisory/changelog wording integrated. Full suite
green: 968 passed, 1 skipped, 0 regressions; 4 new unit tests for
`_gemini_subprocess_env` in `test_dispatch_gemini.py`.

## 1.15.1 - 2026-05-15

**Machine-enforcement of the converged-plan convention** — closes the
v1.15.0 NAMED BLOCKER. From `iteration-converged-plan-machine-
enforcement` (Workflow A weighted-synthesis: claude + codex + gemini;
shared-prior self-check PASSED; no blocking objections; the consult
dogfooded the very convention it enforces). The recorded v1.15.0
"starting design" (gemini's `severity` field + `consensus_gate.py`
mechanism) was **partially refuted by first-hand code-reading** during
the consult: `consensus_mcp/validators/consensus_gate.py` is the
Phase-0 production-readiness gate (P0-V6) and is the WRONG component —
the v1.15.0 doctrine working correctly on its own audit trail.

**Artifact truth (scoped claim):** the **v1.15.0 tag `4e81f9e` is
DOCTRINE-ONLY** — it shipped the convention as an authoring convention
enforced by the bundled skill + Workflow B audit, with **zero engine
code**. Machine enforcement exists **only from the v1.15.1 tag
forward**. Users on v1.15.0 get doctrine; they must upgrade to v1.15.1
to get the gate.

Shipped (real code paths — meaningful regression signal, unlike
v1.15.0's doc-only change):

- **`consensus_mcp/schemas/converged_plan_convention.schema.json`**
  (NEW; `schemas/` is net-new) — JSON Schema for the convention
  object; `empirical_status` enum `proven|pending|refuted|n/a` matches
  v1.15.0 verbatim.
- **`consensus_mcp/validators/validate_converged_plan.py`** (NEW;
  small; structure + consequence ONLY). Enforces the consequence of
  the orchestrator-attested `falsifiable_from_artifacts` bool — the
  engine does **not** classify the defect (keyword heuristics are the
  shared-prior trap the v1.15.0 report documents). **Recursive-trap
  defense (highest-order constraint):** the validator has zero code
  path deriving any approved/correct/ready/sound state from the
  blocks (pinned by `test_validator_source_sets_no_correctness_state`
  grepping the module), and every result carries an unconditional
  `gate_scope` disclaimer: *"presence-and-consistency only; NOT a
  soundness assertion … remains a human judgement."* A pass means the
  required thinking was **recorded**, never that it is **true**.
- **`workflow_engine._seal_converged_plan`** — ingests the optional
  orchestrator-authored `convention-input.yaml` via the ONE channel
  that already reaches seal time (a file in `iteration_dir`; no new
  parameter threaded through `run_iteration`/the MCP tool — codex's
  external-refuting-observation honored), validates it, and seals the
  blocks **INTO** `converged-plan.yaml` (same write, same hash) with
  required `cited_pass_ids` (provenance-by-citation: not a loose
  untracked sidecar, not a single-winner extraction — gemini's
  chain-of-evidence requirement). **Fail-closed:** a hard-reject does
  NOT write `converged-plan.yaml`.
- **Graduated strictness** (`convergence.converged_plan_enforcement`:
  `off|warn|graduated|strict`, default `graduated`). Hard-reject ONLY
  (i) operator-declared safety/data-loss/bricking/irreversible risk
  class missing a conforming root-cause-independent
  `independent_safeguard`, and (ii) `empirical_status:proven` with no
  recorded `experiment_result`; warn + annotate `convention_violations`
  otherwise.
- **`consensus_get_iteration_outcome`** surfaces `enforcement` +
  `convention_gate_scope` + `convention_violations`; a reader can
  never see a pass marker without the non-soundness disclaimer next
  to it. Legacy / absent-convention plans (this session's iter-0043
  .. v1.15.0) still load, explicitly marked `enforcement:
  doctrine-only` — **NOT silently valid, NOT rejected**.

**Named blocker (need-evidence, deferred — not a cop-out):** operator
goal_packet `defect_class`/`risk_class` declaration UX + an
anti-gaming cross-check on `falsifiable_from_artifacts`. Both are
ill-posed until the shipped slice produces real usage data.

**Workflow B audit chain** (codex + gemini, post-implementation):
gemini APPROVED (`goal_satisfied=true`, no blocking). codex pass-1
raised 2 blocking + 2 high — all verified against real code (no
hallucinations; all correctly caught the slice under-implementing
the converged plan) and integrated TDD-first: the third named block
(`decisive_experiment_before_next_iteration`) is now schema-required
+ validated; `doctrine-only` is a READ-time legacy classification
only (a new convergence missing blocks is validated under the
configured level, not bypassed); `convention_schema_version` is
pinned exactly (never defaulted/rewritten); a hard-reject removes any
stale `converged-plan.yaml` (fail-closed truly closed). **Design
note (intentional, peer-converged):** under the default `graduated`
level a new *non-safety* convergence missing blocks is sealed as
**warn + annotate** (loudly marked via `convention_gate.
enforcement_note`, never a silent pass), NOT hard-rejected — this is
the converged plan's deliberate q4/q5 decision (incl. codex's own
consult position) to avoid the rejected-goal_packet papercut. Strict
enforcement is opt-in via `converged_plan_enforcement: strict`.

Full suite green: 964 passed, 1 skipped, 0 regressions (38 new
code-path tests in `test_converged_plan_convention.py`, including
all audit-integration fixes — they close the v1.15.0 doc-only loose
end with a meaningful regression signal).

## 1.15.0 - 2026-05-15

**Convergence-correctness doctrine** — minor bump (cross-project
doctrine evolution, not a hot-patch). From
`iteration-convergence-correctness-doctrine` (Workflow A
weighted-synthesis: claude + codex + gemini, no blocking
objections; gemini dissented substantively on prominence +
machine-enforcement and that dissent was incorporated, not
outvoted — the shared-prior self-check passed), derived from a
real downstream failure: the ChilipadScreen ESP32-P4 i2c
boot-loop consensus-failure report (2026-05-15) where two clean
strict-majority convergences (one round-1 *unanimous*) were both
refuted on-device.

Doctrine added to the bundled `consensus-workflow` skill + both
dispatch-template preambles + a new
`docs/workflows/converged-plan-convention.md`:

- **Safety interlock first (headline rule).** Safety-critical /
  data-loss / bricking / irreversible-risk defects MUST ship a
  root-cause-INDEPENDENT safeguard with the hypothesized fix,
  valuable even if the hypothesis is 100% false. Auditable bar:
  "would it still work if the root cause were entirely
  different?" Field-proven: an independent boot-loop breaker
  un-bricked a medical-safety device across two failed
  root-cause iterations ("the single highest-value decision").
- **Convergence is agreement, not truth.** Fast independent
  unanimity is a verify-harder flag — a shared-prior artifact is
  the multi-agent analog of single-agent rationalize-away.
  Dispatch templates now require each contributor to state the
  differential/prior it reasoned from (shared prior exposed at
  reveal instead of laundered as "independent agreement").
- **Provisional-until-proven** for the defined not-falsifiable-
  from-artifacts class (hardware/firmware state, environment/
  toolchain, concurrency/timing): converged root cause is
  PROVISIONAL; "fixed/shipped/root-cause-correct" language
  forbidden until a pre-specified EXTERNAL discriminating
  experiment runs.
- **Anti-theater property:** a falsification is real only if its
  refuting observation is pre-specified, a specific observable,
  and (for that class) external to the reasoning that produced
  the hypothesis.
- **Converged-plan convention:** `falsification`,
  `independent_safeguard`, and
  `decisive_experiment_before_next_iteration` blocks documented
  as an authoring standard, doctrine-enforced now.

**Named blocker (sequenced follow-up):** engine/validator
MACHINE-enforcement of the converged-plan blocks (gemini's
`severity` field + `consensus_gate.py` mechanism is the recorded
starting design). Concrete blocker, file-verified: no standalone
converged-plan schema exists; `workflow_engine.py:505-525` reads
generic YAML keys. Needs its own schema-design consult + a
not-falsifiable-from-artifacts classifier first.

**Field validation of v1.14.9:** the same report independently
confirmed the v1.14.9 void-seal preservation working correctly —
void rounds from the v1.14.0 path-only review bug were preserved
as honest audit history and correctly not counted toward
convergence. No new tooling; cited as regression evidence.

Doc/skill/template/memory only — no engine, config, or code
paths touched. Full test suite green, 0 regressions.

## 1.14.9 - 2026-05-15

Seal-pipeline defect fix from iteration-seal-archive-collision-fix
(workflow A weighted-synthesis convergence; codex + gemini + claude;
no blocking objections). Workflow B audit of the implementation by
codex + gemini.

**Defect:** `review_write_and_seal.handle` built the archive filename
from `reviewer_id` only (`{date}-{iteration_id}-{reviewer_id}-pass.yaml`),
while the index keys uniqueness on `pass_id`. Re-using a reviewer_id
across passes (e.g. `gemini-iteration-0012-2` for pass1 AND pass2)
produced a hard `packet_path_collision` at the path-exists guard
*before* the pass_id-aware index logic ever ran — forcing operators
to mint throwaway reviewer-ids per attempt. `test_contributors.py:135`'s
docstring ("filename must contain iteration_id + reviewer_id + pass_id
tokens") documented the intended 4-token contract; the implementation
had silently regressed to 3 tokens at/before extraction.

**Fix (`consensus_mcp/tools/review_write_and_seal.py`):**

- Archive filename is now 4-token:
  `{date}-{iteration_id}-{reviewer_id}-{safe_pass_id}-pass.yaml`.
  Filename uniqueness key == index uniqueness key (pass_id).
- New `_sanitize_for_filename`: maps `[^A-Za-z0-9._-]`→`-`, collapses
  runs, strips, falls back to `pass`. Applied to the FILENAME only —
  the raw `pass_id` is preserved verbatim in the index entry and
  packet body.
- The index `pass_id` lookup is REORDERED ahead of the path-exists
  guard, and idempotency is judged on **content identity** — the
  canonical hash of the packet with the volatile seal-provenance
  fields (`sealed_at_utc`, `packet_sha256`) stripped — compared
  against the actual ON-DISK archived packet. An exact re-seal is
  an idempotent SUCCESS (`idempotent: true`, `index_updated:
  false`, existing recorded path; scheme-agnostic so old-scheme
  archives still resolve) **regardless of seal time**. This
  matters because a dispatch retry builds a fresh packet with no
  `sealed_at_utc`; a sha-based check would see a new timestamped
  hash and mis-classify the retry as a collision — defeating the
  whole feature for its primary use case (caught by codex Workflow
  B audit, HIGH).
- Integrity guard on the idempotent path: a tampered archive that
  no longer matches the incoming content identity yields
  `index_collision`; one that parses as valid **non-mapping** YAML
  (list/scalar) yields `idempotent_target_integrity_mismatch`
  without crashing; a deleted/unreadable archive yields
  `idempotent_target_missing` / `_unreadable`. The idempotent
  success return reports the **authoritative on-disk artifact
  hash** (the file's own `packet_sha256`, or a reconstruction if
  absent), never a missing/stale value copied from the index.
- The sealed body now always self-records the canonical `pass_id`
  (`packet.setdefault`), including for pass-label-only packets,
  and a parameter-vs-body `pass_id` consistency guard mirrors the
  existing `iteration_id` / `reviewer_id` guards.
- `index_collision` (same pass_id, substantively *different*
  content) preserved with a content-identity-based detail string.
- path-exists guard retained as a defense-in-depth backstop with a
  distinct detail string (unreachable in normal flow).
- `output_schema` + the `handle()` docstring updated to document
  the new `idempotent` success field and all new error codes; a
  schema-contract test pins every `handle()` error code to the
  schema enum so this drift cannot silently regress.

**Workflow B audit chain** (post-review; codex iterated, gemini
approved the core design): gemini APPROVED with no findings; codex
surfaced and drove resolution of 6 distinct real gaps across 4
audit passes — output-schema drift, missing pass_id consistency
guard, a HIGH timestamp-dependent-idempotency defect, a
non-mapping-archive crash, pass-label body identity, and a
stale/missing idempotent-return hash — before reaching
`goal_satisfied: true` with no blocking objections. The HIGH
finding (idempotency silently broken by the pre-existing
`sealed_at_utc`-in-hash stamp) would have shipped a dead feature
without this audit.

**Backward compat:** the 162 existing archives are NOT migrated.
`review_read_post_seal` resolves via the index's stored relative
path and never parses the filename, so old-scheme archives remain
fully readable. Mixed-scheme archive directory across the v1.14.x
boundary is expected; the index is the resolution source of truth.

**Tests:** new `consensus_mcp/tests/test_seal_collision_fix.py`
(19 cases: the exact `gemini-iteration-0012-2` same-reviewer-
distinct-pass scenario; timestamp-independent idempotent success;
non-mapping-archive integrity guard; target-missing; pass_id
parameter/body consistency; pass-label body identity stamping;
authoritative-hash return when the index sha is blank;
hostile-pass_id sanitization; schema-contract pinning of every
`handle()` error code); `test_contributors.py` fake_t6 helpers
updated to the 4-token scheme. Full suite **723 pass, 0
regressions** (excluding the pre-existing `test_dispatch_codex`
ordering flake + `jsonschema`-missing environmental issue, both
predating this work).

**Deferred with stated reasons** (recorded, not silently dropped):

- `superseded_by` index annotation for void seals — 2/3
  contributors scoped it out as beyond the minimal defect; file
  preservation alone already satisfies the audit-history
  requirement. Own small consult if causal supersession tracking
  is wanted.
- Forensic scan of the 162 existing archives for latent mis-seals
  — blocked on an undefined remediation model for immutable sealed
  history.
- `index.yaml` non-atomic read-modify-write race — pre-existing,
  orthogonal; separate hardening consult.

Carried forward from the overnight run (still open, unchanged):
Workflow C multi-iteration engine (v1.15.0 named blocker); iter-0045
PHASE_CONVERGE mapping empirical evaluation; project-level
`.consensus/autonomous-policy.yaml`.

## 1.14.8 - 2026-05-14

iter-5 of autonomous run-2026-05-15-overnight. Bundled
`consensus_mcp/claude_extensions/skills/consensus/SKILL.md` (the
bootstrap skill that triggers on "consensus init") gains a
"Workflow modes the operator can pick (v1.14.4+)" section:

- Documents Workflow A (propose-converge, default), Workflow B
  (post-review, lightweight), Workflow C (autonomous-execute,
  v1.14.4 contract / v1.15.0 engine), and advisory.
- Notes the numeric-alias deprecation cycle.
- Cross-references the operating-procedure skill
  (`~/.claude/skills/consensus-workflow/SKILL.md`) as the
  load-bearing reference for dispatch rules and halt conditions.

Operators running `consensus init` for the first time will now see
the workflow-mode vocabulary at bootstrap time instead of having to
discover it from the `--help` output.

No code changes; doc only. 704 tests pass.

## 1.14.7 - 2026-05-14

Doc hot-patch from autonomous run-2026-05-15-overnight iter-4. Two
stale references that user-facing surfaces still showed:

- `README.md` "Status" section bumped from "1.14.0 — multi-AI
  contributor pool, blind-first-reveal workflow #4 ..." to a
  "v1.14.6 (current)" header + per-release train summary covering
  v1.14.0 → v1.14.6. Includes pointer to `docs/advisories.md` for
  known-defect-release upgrade guidance.
- `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`
  one stale "Workflow #3" reference in the "Workflow A/B in one line"
  section body (the global rename in v1.14.4 missed this hyphenated
  form). Bumped to "Workflow B".

No code changes; docs only. 704 tests pass (no regression).

## 1.14.6 - 2026-05-14

Hot-patch from autonomous run-2026-05-15-overnight iter-3.

**Defect fixed:** `consensus-init --workflow A` (and B, C, lowercase
variants, and the new `autonomous-execute` semantic string) was
rejected at argparse parse-time before alias resolution could run.
v1.14.4 added letter aliases to `WORKFLOW_ALIASES` + the interactive
wizard prompt but missed the CLI `--workflow` argparse `choices` list,
which still hardcoded the pre-rename set
`["3","4","post-review","propose-converge","advisory"]`.

**Fix:** `consensus_mcp/_init_wizard.py` argparse `--workflow` choices
list expanded to include `["A","B","C","a","b","c","3","4",
"post-review","propose-converge","advisory","autonomous-execute"]`.
Help text updated to document A/B/C semantics inline so
`consensus-init --help` is self-documenting. Numeric aliases (3, 4)
still accepted at parse-time; deprecation warning fires at
`normalize()` time per the v1.14.4 contract (unchanged).

**Tests:** 8 new in `consensus_mcp/tests/test_init_wizard_workflow_choices.py`
asserting argparse acceptance for each new alias variant + sanity
test that letter parses through `WORKFLOW_ALIASES` to the canonical
semantic string. Total suite: 704 pass (was 696; +8 for iter-3).

## 1.14.5 - 2026-05-14

Bundled hot-patch from autonomous-mode `run-2026-05-15-overnight`
(operator-initiated; running per the v1.14.4 Workflow C contract,
manually-orchestrated since the v1.15.0 engine path is the named
blocker). Two completed iterations bundled per the no-deferral rule:

**iter-0044 — adapter `--mode` forwarding fix** (Workflow B audit;
codex + gemini both `goal_satisfied=true, blocking_objections=[]`).

The original defect: `CodexAdapter.dispatch` and
`GeminiAdapter.dispatch` built argv without `--mode`, causing every
workflow A round-1 dispatch through the engine to silently use the
review template + review schema even when `packet.phase ==
PHASE_PROPOSE`. The shell binaries (`consensus-mcp-dispatch-codex`,
`consensus-mcp-dispatch-gemini`) already accepted `--mode {review,
proposal}` per iter-0028; the adapter just wasn't forwarding it.
Test coverage didn't catch this because
`test_dispatch_codex_proposal_mode.py` only tested the dispatcher's
own argparse in isolation, never the adapter boundary.

Implementation per iter-0043 converged plan:

- `consensus_mcp/contributors/_phase_mode.py` NEW — single source
  of truth: `PHASE_PROPOSE → "proposal"`, `PHASE_REVIEW → "review"`,
  `PHASE_CONVERGE → "review"` (interim per iter-0043 q1
  weighted-synthesis; iter-0045 candidate revisits with empirical
  data). Strict-dict lookup; raises `ValueError` on unmapped phase
  (no silent default — that's exactly what allowed the original
  defect).
- `consensus_mcp/contributors/codex.py` — append `--mode` to argv
  via `phase_to_mode(packet.phase)`.
- `consensus_mcp/contributors/gemini.py` — same.
- `consensus_mcp/tools/reviewer_dispatch_codex.py` — schema gains
  `phase` (engine abstraction, primary) + `mode` (dispatcher escape
  hatch, optional). `_resolve_mode(phase, mode)` helper: explicit
  mode wins; otherwise translate phase via `_phase_mode`; otherwise
  None (caller omits `--mode` for backward compat).
- `consensus_mcp/tools/reviewer_dispatch_gemini.py` — mirrors codex
  wrapper exactly.
- `consensus_mcp/tests/test_phase_mode_forwarding.py` NEW — 21 tests
  covering: phase_to_mode mapping (all 3 phases + ValueError on
  unknown); CodexAdapter + GeminiAdapter argv forwarding for all 3
  phases (mock-based); MCP wrapper `_resolve_mode` + `_build_argv`
  (phase-to-mode mapping + mode-wins-over-phase + neither =
  backward-compat omission).

Test results: 696 pass (was 675; +21 for iter-0044). Adapter
boundary now covered by tests that would have caught the original
defect.

**README staleness sweep** (doc-only; no peer audit needed).

- `README.md`: "What it does" section updated to Workflow A/B/C
  vocabulary + Workflow C mention; install URLs already at
  `@v1.14.4`; "Pre-commit vs post-commit catch" section + table-of-
  contents reference Workflow A / Workflow B with "(was workflow
  #4 / #3 prior to v1.14.4)" historical note.
- `docs/workflows/workflow-4-preferred.md`: filename preserved for
  stable cross-references; frontmatter + body updated to Workflow A
  vocabulary; transition note at top.

Audit log: `consensus-state/autonomous-runs/run-2026-05-15-overnight/log.jsonl`
records both iterations + halt-set checks + scope-check decisions
per the v1.14.4 Workflow C contract schema.

## 1.14.4 - 2026-05-14

## 1.14.4 - 2026-05-14

Workflow A/B/C rename + Workflow C contract from
iter-workflow-abc-introduce (workflow A weighted-synthesis
convergence across claude + codex + gemini; no blocking objections).

**Operator-facing vocabulary: numeric → letter aliases.**

- Workflow A = propose-converge (was numbered #4) — DEFAULT
- Workflow B = post-review (was numbered #3) — LIGHTWEIGHT
- Workflow C = autonomous-execute (NEW) — LONG-FORM/OVERNIGHT
- Numeric aliases (3, 4) still resolve but emit `DeprecationWarning`;
  scheduled for removal in a future minor release.

**Workflow C — autonomous-execute (CONTRACT shipped, engine deferred).**

`consensus init` operators can configure a project for Workflow C;
goal_packet authors can declare an `autonomy_contract` block with
file boundaries, halt conditions, and iteration/wall-clock caps.
The validator and `check_autonomy_scope` helper are fully
implemented and tested. The actual multi-iteration auto-execution
loop is the named blocker for v1.15.0 (requires cross-platform
interrupt-file watching validation, integration tests with real
peer dispatches, autonomy-ledger replay design — multi-session
work that does not fit a hot-patch).

Workflow C requires exactly 3 contributors (claude + codex + gemini)
enforced at config-load — the wide cross-AI safety net is mandatory
for autonomous mode by default; v1.15.0+ may relax with explicit
operator opt-in.

When an operator runs a Workflow C goal_packet in v1.14.4, the
engine raises `NotImplementedError` with a clear message naming
v1.15.0 as the engine ship target and pointing at
`docs/workflows/workflow-c-autonomous.md`.

**Files in scope:**

- `consensus_mcp/config.py`: `WORKFLOW_AUTONOMOUS_EXECUTE` constant +
  alias map (A/B/C primary; numeric deprecated) + 3-AI requirement
  validator + DeprecationWarning emission for numeric aliases.
- `consensus_mcp/validators/scope_check.py`: new
  `validate_autonomy_contract` + `check_autonomy_scope` functions
  (approve/park/halt decisions); `DEFAULT_HALT_ON` constant lists
  the wide-by-default halt conditions; `AUTONOMY_CONTRACT_REQUIRED_FIELDS`
  documents required schema.
- `consensus_mcp/workflow_engine.py`: recognizes
  `WORKFLOW_AUTONOMOUS_EXECUTE`; raises `NotImplementedError` with
  v1.15.0 reference.
- `consensus_mcp/_init_wizard.py`: workflow prompt accepts letter
  aliases (A/B/C); resolves to canonical semantic string before
  storing.
- `consensus_mcp/tests/test_config.py`: 11 new tests covering alias
  rename, deprecation warning, 3-AI requirement, and Workflow C
  validation.
- `consensus_mcp/tests/test_scope_check_autonomy.py` (NEW): 17 tests
  covering autonomy_contract validation + check_autonomy_scope
  decision logic + glob matching edge cases.
- `consensus_mcp/dispatch_templates/codex_proposal_template.md`,
  `gemini_proposal_template.md`: A/B reference instead of #3/#4.
- `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`:
  new "Workflow A / B / C in one line each" section; #3/#4 references
  bumped to A/B throughout.
- `docs/workflows/workflow-c-autonomous.md` (NEW): operator-facing
  doc on Workflow C usage (autonomy_contract example, halt set table,
  scope-check decisions, interrupt mechanism, audit log location,
  v1.15.0 status note).

**Test summary:**

- 675 tests pass (full suite excluding pre-existing
  `test_dispatch_codex.py` ordering flake and `jsonschema`-missing
  environmental issue in the proposal-mode tests; both predate this
  release per `docs/known-issues/pytest-ordering-flake.md`).
- New: 11 in `test_config.py` for alias + Workflow C; 17 in
  `test_scope_check_autonomy.py` for the validator and decision
  logic.

**Named blockers for deferred work:**

- v1.15.0 — Workflow C multi-iteration auto-execution loop:
  requires cross-platform interrupt-file watching validation
  (Windows ReadDirectoryChangesW vs Unix select/poll), integration
  tests with real peer dispatches (cost: dispatcher latency × N
  iterations per test run), resume-after-halt semantics design,
  autonomy-ledger replay for failure recovery. Multi-session work.
- v1.16.0+ — project-level `.consensus/autonomous-policy.yaml` as
  default with goal_packet override: deferred until empirical
  evidence operators want it across multiple Workflow C runs (we
  have zero runs today; designing for hypothetical reuse is
  premature per the no-deferral rule's "real blocker" requirement).
- iter-0044 (adapter `--mode` forwarding fix) deferred to v1.14.5
  with named blocker (still requires adapter-boundary test
  infrastructure).

## 1.14.3 - 2026-05-14

Stabilization hot-patch from iter-audit-2026-05-14-three-followup-gaps
(workflow #4 weighted-synthesis convergence; codex + gemini + claude;
no blocking objections). Bundles all converged scope into one tag per
the no-deferral doctrine (operator directive: "consensus runs to
completion when goals + acceptance gates are clear").

**Doctrine: consensus runs to completion (no gratuitous deferral).**

New bundled-skill section "Consensus runs to COMPLETION" + dispatch-
template completion mandate codify the rule: deferring well-defined
work to "future iterations" without naming a specific blocker is an
anti-pattern. Includes the completion test (acceptance gates concrete?
open design surface? implementation cost small enough?) and explicit
anti-patterns ("iter-XXXX candidate" with no blocker, "Phase B" when
phases share a doctrine boundary, splitting hot-patches across tags
when fixes share a single failure mode).

**Doctrine in the data layer (Q1 — config.py):**

- `DISPOSITION_WEIGHTED_SYNTHESIS = "weighted-synthesis"` constant
  added to `consensus_mcp/config.py`.
- `VALID_DISPOSITION` now includes the new value.
- `VALID_DISPOSITION_FOR_PROPOSE_CONVERGE = {all-or-nothing,
  weighted-synthesis}` — workflow #4 accepts these two only;
  `per-finding` stays post-review-only (its semantics fit defect
  lists, not plan synthesis).
- `default_config()` workflow #4 default flips from `all-or-nothing`
  to `weighted-synthesis` (matches the iter-0043 doctrine).
- Validator at `config.py:303-309` updated to allow either valid
  workflow #4 disposition; rejects `per-finding` with actionable
  error message.
- `_init_wizard.py` defaults updated: workflow #4 setup now lands
  in `weighted-synthesis` by default; workflow #3 / advisory keep
  `all-or-nothing` as their default.
- 4 new tests in `test_config.py` cover both valid dispositions for
  workflow #4 + reject `per-finding` with regex-matched message.
  Pre-existing `test_default_disposition_all_or_nothing` renamed
  to `test_default_disposition_weighted_synthesis` and updated.
- All 107 tests in `test_config.py` + `test_init_wizard.py` pass.

**Procedural enforcement of disconfirming-evidence pattern (Q2):**

- Bundled skill: completion mandate in templates + skill (also
  lands the no-deferral rule above as the structural sibling).
- Dispatch templates (`codex_proposal_template.md`,
  `gemini_proposal_template.md`): completion mandate preamble so
  peer AIs default to in-scope completion, not deferral.

**Stale-tag mitigation (Q3):**

- `README.md` install URLs bumped from `@v1.14.0` to `@v1.14.3`
  (both pipx and pip-in-venv examples). Stops the bleeding wound
  where new installs were getting the v1.14.0 buggy bundled skill.
- `docs/advisories.md` NEW — standing channel for "shipped artifact
  has known doctrine drift" notices. First entry covers v1.14.0
  and v1.14.1 with explicit upgrade instructions (`pipx install
  --force` + re-run `consensus init --install-claude-code`).
- Bundled skill cut sequence (steps 8 + 10) gains explicit "bump
  README install URL on the new dev branch" + "add advisory entry
  if applicable" steps so this drift cannot recur structurally.

iter-0044 (adapter `--mode` forwarding fix per iter-0043 converged
plan) deferred to v1.14.4 with named blocker: still requires test
infrastructure setup for adapter-boundary fixtures that does not
fit in this hot-patch's scope.

## 1.14.2 - 2026-05-14

## 1.14.2 - 2026-05-14

Doctrine hot-patch from iter-audit-2026-05-14-pypi-invention
(workflow #4 postmortem consult; weighted-synthesis convergence
across claude + codex + gemini, no blocking objections).

The audit examined two compounding errors observed in the v1.14.0
release cycle: (1) inventing a "publish to PyPI" step in the cut
sequence based on the inference "Python project + pipx → PyPI"
without verifying the actual install URL form (the package is
not registered on PyPI; ships via git tags), and (2) gluing two
correct statements ("v1.14.0 tag is on origin" + "git tags are
the only channel") into a misleading composite ("v1.14.0 is
fully shipped via the only channel that exists") that masked
the artifact defect (the v1.14.0 tag at commit 8e0dab2 still
ships the buggy skill in its wheel).

Three orthogonal failure modes converged: verification gap +
defaulting bias + sloppy framing. Layered defenses applied:

- **Bundled skill: "Verify before invent"** section requires
  positive citation of the source for any step touching an
  external system / channel. Disconfirming evidence (missing
  creds, missing workflow, registry 404) is treated as the
  SIGNAL, not as a credential gap to fill.
- **Bundled skill: "Artifact-scoped claims"** section forbids
  global "fixed" / "shipped" claims when the surface is broader
  than what was changed. Required form names version + commit/
  tag + install path + bundled content + residual defects;
  immutable tag and dev branch are NOT the same artifact.
- **Dispatch templates** (codex_proposal_template,
  gemini_proposal_template) gain a verification-first mandate
  preamble so all peer AIs in future consults apply the rule,
  not just claude.
- **Project-local memories** (gitignored, claude-personal):
  feedback_verify_before_invent, feedback_partial_fix_surfacing,
  feedback_disconfirming_evidence (names the
  bias-rationalizes-evidence pattern explicitly), and
  reference_default_priors_to_distrust (enumerated list of
  high-risk inferences: PyPI/npm/Docker/MIT/pytest/etc.,
  append-only).

No code changes; doctrine-only. Scope unchanged for v1.14.3
(iter-0044 adapter `--mode` forwarding fix per iter-0043
converged plan).

## 1.14.1 - 2026-05-14

## 1.14.1 - 2026-05-14

Hot-patch: corrects the bundled `consensus-workflow` skill so it
no longer documents a PyPI publish step in the release cut
sequence. consensus-mcp ships via git tags + pipx (`pipx install
git+https://github.com/.../@vX.Y.Z`); the package is not
registered on PyPI. The v1.14.0 skill incorrectly added a PyPI
step that does not match the project's actual distribution model;
v1.14.1 removes it and adds an explicit "if you catch yourself
proposing PyPI, stop" warning so future sessions don't repeat
the mistake.

iter-0044 (adapter `--mode` forwarding fix per iter-0043 converged
plan) is open scope on v1.14.2 — not in this hot-patch.

## 1.14.0 - 2026-05-14

Multi-AI contributor pool, blind-first-reveal workflow #4, configurable
governance, snapshot/restore, Claude Code bootstrap pack, bundled
operating-procedure skill, and codified operator-directive defaults
(parallelism, weighted-synthesis convergence, Friday release cadence).
Adds gemini-cli as a third peer alongside codex-cli; introduces a
workflow engine that orchestrates N contributors per project-chosen
rules; ships an interactive `consensus init` wizard for operator-
configurable workflow/independence/convergence/disposition/snapshot/
patch-authoring/timeout dimensions.

**Claude Code integration + skill bundling (iter-0040, iter-0041):**

- `consensus init --install-claude-code` standalone global op installs
  the Claude Code bootstrap pack (skill + slash command) for any
  Claude Code project that uses consensus-mcp.
- `consensus_mcp/claude_extensions/skills/consensus-workflow/SKILL.md`
  ships in the wheel: load-bearing operating procedures (workflow
  selection, dispatch, gemini 429 handling, codex auth, iteration-
  state persistence, peer-cited content verification, peer-review
  thresholds, "consensus" trigger word) automatically present in
  every project that runs the bootstrap pack.

**Operator-directive defaults codified (iter-0043):**

- Maximize parallelism — always. Default to parallel; serial is the
  choice that needs justification. Applies to round-1 peer dispatch,
  round-2+ batches, multi-file investigation, background long-running
  ops, and cross-iteration parallelism.
- Weighted-synthesis convergence as default. All ideas of all
  proposals weighed for benefit to the project as a whole. No good
  ideas lost; no babies tossed with bathwater. `all-or-nothing`
  finding-disposition is now edge-case opt-in only (binary scope
  decisions, safety gates, compliance verdicts). Engine-level
  follow-up flagged: `config.py:295-308` still enforces all-or-
  nothing for workflow #4 — separate iteration will lift that
  constraint so the data layer matches doctrine.
- Friday release-cadence rule. Cut a release tag every Friday if at
  least one iteration closed that week. Release-cut is a procedure
  with a trigger, not an ad-hoc decision. 10-step cut sequence
  documented in skill (CHANGELOG date stamp, version verify, test
  suite, tag, build, smoke, publish, push, branch next). This
  release is the first cut under the new cadence rule and clears
  3 days of accumulated work (iter-0009..iter-0043).

**Known issue carried forward:**

- 5 tests in `consensus_mcp/tests/test_dispatch_codex.py` flake when
  the full pytest suite runs (pass in isolation). Documented in
  `docs/known-issues/pytest-ordering-flake.md`. Predates v1.13.0;
  not a v1.14.0 regression. Tracked for a future fix iteration.

**New contributors + dispatch (iter-0009 through iter-0011):**

- `_dispatch_base.py` extracted shared dispatcher helpers (`_resolve_repo_root`,
  `_load_goal_packet`, `_build_prompt`, `_terminate_process_tree`,
  `_compute_per_patch_base_sha`, `_validate_patch_proposal`,
  `_build_sealed_packet`, `_seal_via_t6`, `_log_dispatch`).
- `_dispatch_gemini.py` wraps `gemini -p '<JSON directive>' -m <model>
  --approval-mode plan --skip-trust` with validator-retry semantics.
- `reviewer_dispatch_gemini` MCP tool surfaces the gemini adapter in the
  pool.
- Reader threads start before stdin write to avoid the codex-rev-001
  deadlock pattern.
- Codex stall-silence threshold now overridable via
  `CONSENSUS_MCP_STALL_SILENCE_SECONDS` env var (default 180s, was 45s).
- `reviewer_dispatch_codex` MCP wrapper catches `argparse.SystemExit` so
  bad CLI args surface as MCP errors instead of hangs.

**Snapshot/restore + parent history (iter-0012 through iter-0014):**

- Orphan git branch `consensus-state-snapshots` stores point-in-time
  snapshots of the gitignored `consensus-state/active/` tree, providing a
  recovery path against `git clean -fdX` accidents.
- `_snapshot_state.py` exposes `snapshot`, `list`, `restore`, `diff`
  subcommands; ISO-timestamped `snapshot-<TS>-<label>` tags;
  `^[A-Za-z0-9_-]{1,64}$` label regex; path-traversal validation; tag
  uniqueness with retry-suffix; temp-worktree extraction + filesystem
  copy on restore (no risky `git checkout`).
- `_import_parent_history.py` performs a one-time mirror of the parent
  agent-loop project's iter-0000..0042 (41 directories, 53 review passes)
  into `consensus-state/archive/imported-from-parent/` with byte-for-byte
  idempotency checks.
- Integration tests cover restore-after-dirty-state, restore-into-detached-
  HEAD, and concurrent-snapshot race handling.

**Configurable governance + workflow engine (iter-0015 through iter-0016d):**

Per iter-0015 converged design (the canonical workflow #4 reference run
across claude/codex/gemini), v1.14.0 introduces 9 operator-configurable
dimensions surfaced through `.consensus/config.yaml`:

- `workflow.mode`: `post-review` (#3), `propose-converge` (#4), `advisory`
- `workflow.independence`: `blind-first-reveal`, `visible`, `sequential`
- `convergence.rule`: `unanimous`, `strict-majority`, `inclusive-majority`,
  `advisory`
- `convergence.finding_disposition`: `all-or-nothing`, `per-finding`
- `snapshots.trigger`: `manual-only`, `on-iteration-close`, `periodic`
- `snapshots.periodic.every_iterations`: integer cadence
- `patches.authoring`: `claude-only`, `any-contributor`, `none`
- `workflow.timeout_policy`: `treat-as-no-vote`, `treat-as-blocking`,
  `shrink-quorum`
- `contributors.enabled`: ordered list (claude orchestrator always present;
  codex/gemini optional)

Components shipped:

- `config.py` — schema constants, defaults, normalize, validate, load,
  effective-config sha256, legacy-mode synthesis. Cross-validation
  enforces (e.g.) workflow #4 with N≥2 contributors, strict-majority
  with N≥2, manual-only snapshots requiring null cadence.
- `contributors/` package — `ContributorAdapter` ABC + `DispatchPacket`,
  `SealedArtifact`, phase constants. Concrete `ClaudeAdapter` (in-process
  orchestrator self), `CodexAdapter` (subprocess wrapper),
  `GeminiAdapter` (subprocess wrapper). All seal via T6 with confinement
  checks. Fake adapters (`FakeAlwaysApprove`/`FakeAlwaysBlock`/
  `FakeRaisesDispatchError`) for hermetic tests.
- `workflow_engine.py` — `WorkflowEngine.run_iteration()` routes per
  `workflow.mode` to one of three runners. Workflow #3 dispatches non-
  claude contributors as post-review reviewers. Workflow #4 runs blind-
  proposal phase then reveal-and-converge rounds with all contributors
  seeing the full set of prior round artifacts. Convergence evaluation
  applies the rule across responsive contributors, mapped to config
  contributor keys (not adapter names). Timeout policies adjust the
  effective denominator and block-vote set.
- `_init_wizard.py` — `consensus init` CLI. Interactive (default) +
  `--non-interactive` + `--accept-defaults` + `--reconfigure` + `--check`
  + `--print-defaults` + `--dry-run` + `--force` + `--config <path>` +
  `--no-update-gitignore`. Exit codes 0/1/2/3/4 per converged-plan
  Section A. Atomic temp+rename writes. `.gitignore` managed-block with
  bracketed markers (`# >>> consensus-mcp managed <<<` /
  `# <<< consensus-mcp managed >>>`), idempotent across reruns even with
  malformed (orphan, reversed, nested) user-supplied markers — orphan
  markers are preserved untouched. Repo-root detection walks upward for
  `.git`.

**Packaging (iter-0017):**

- `[tool.setuptools].packages` includes `consensus_mcp.contributors`.
- `[project.scripts]` adds `consensus-init` and
  `consensus-mcp-dispatch-gemini` alongside existing entries.

**Workflow + provenance discipline:**

Every non-trivial change shipped on v1.14.0 went through workflow #3 with
codex + gemini reviewing in parallel. iter-0015 was the canonical
workflow #4 design consult (blind proposals from claude + codex + gemini,
then convergence rounds). iter-0016d converged after 6 review rounds
(both reviewers goal_satisfied=true, zero findings). iter-0017 converged
on first pass.

**Test isolation (iter-0019):**

- The 5 long-standing test_dispatch_codex.py full-suite failures are
  fixed. Root cause: `review_write_and_seal.py` and `audit_append_event.py`
  cache `REPO_ROOT` / `ARCHIVE_DIR` / `INDEX_PATH` / `ACTIVE_DIR` at module
  import time, so `monkeypatch.setenv("CONSENSUS_MCP_REPO_ROOT", str(tmp_path))`
  in tests had no effect — sealed packets landed in the real archive index,
  polluting subsequent test runs. iter-0019 adds an `_isolate_archive_root`
  test helper that monkeypatches the cached module attributes directly, so
  every dispatch test now seals into a tmp_path-isolated archive. Full
  suite: 658 pass + 1 skipped + 0 fail under any ordering.

**Lazy path resolution (iter-0024 design consult → iter-0025 Phase A
→ iter-0026 first per-tool migration):**

- iter-0024 ran the workflow #4 consult on whether to refactor the
  module-level `REPO_ROOT` caches into lazy resolvers. Converged on
  SHIP-PHASED: introduce `_paths.py` first (Phase A), then migrate
  tools one at a time (Phase B).
- iter-0025 (Phase A): introduced `consensus_mcp/_paths.py` with 9
  lazy-resolver functions (`repo_root`, `state_root`, `project_root`,
  `spec_path`, `archive_dir`, `index_path`, `active_dir`,
  `audit_log_path`, `dispatch_log_path`). Each reads env state on
  every call. Backward compatible — no existing tool touched.
- iter-0026 (Phase B step 1): migrated `state_read_decision_ledger`
  to use `_paths.state_root()`. First test coverage for that tool
  (5 new tests including the lazy-resolution regression demo).
- iter-0034 (Phase B step 2): migrated `repo_get_section` to
  `_paths.project_root()`. Removed local `_resolve_repo_root`.
- iter-0035 (Phase B steps 3-8): batched 6 LOW-impact tools onto
  the lazy resolvers in one commit — `repo_set_section`,
  `state_update_decision_ledger`, `patch_stage_and_dry_run`,
  `patch_apply_consensus_patch`,
  `gate_evaluate_production_with_scope_match`, `review_read_post_seal`.
  PEP 562 `__getattr__` hooks added per-tool for external
  `module.REPO_ROOT` back-compat.
- iter-0036 (Phase B step 9, HIGH-impact audit-trail tool): migrated
  `audit_append_event` from cached `REPO_ROOT/ACTIVE_DIR` to lazy
  `project_root()`/`active_dir()`. Cleaned up 3 closure_invariant
  tests that used unsafe `monkeypatch.setattr(audit_append_event,
  "ACTIVE_DIR", tmp_path)` — pytest's monkeypatch captures the
  `__getattr__`-synthesized value at setattr time and restores it
  into `__dict__` at teardown, permanently poisoning subsequent
  tests. Switched to `monkeypatch.setenv("CONSENSUS_MCP_STATE_ROOT",
  ...)` matching the iter_0018 pattern.
- iter-0037 (Phase B step 10, final + HIGHEST-impact seal-pipeline
  tool): migrated `review_write_and_seal` from cached
  `REPO_ROOT/ARCHIVE_DIR/INDEX_PATH` plus the in-handle
  `from consensus_mcp.tools.audit_append_event import ACTIVE_DIR`
  capture to lazy `project_root()`/`archive_dir()`/`index_path()`/
  `active_dir()` resolvers. `_isolate_archive_root` test helper in
  `test_dispatch_codex.py` simplified — only `monkeypatch.setenv(
  "CONSENSUS_MCP_REPO_ROOT", ...)` remains; the previously-required
  5 `setattr` calls (now unsafe against `__getattr__` attributes)
  are gone.

**Claude Code bootstrap pack (iter-0039 design → iter-0040 implementation):**

- iter-0039 ran a workflow #4 consult (claude + codex + gemini, all
  proposal-mode, strict-majority convergence) on the discoverability
  gap reported by the operator after iter-0033: in a fresh project,
  typing "consensus init" into Claude Code chat returned "I don't
  recognize this command" because the pipx install registers a
  shell binary (`consensus-init`) not a Claude Code surface. All
  three contributors converged on shipping a Claude-Code-native entry
  point that wraps the existing shell binary.
- iter-0040 implemented the converged plan. New shipped assets:
  - `consensus_mcp/claude_extensions/skills/consensus/SKILL.md`
    triggers on "consensus init", "bootstrap consensus", "set up
    consensus" and runs the shell binary via Bash.
  - `consensus_mcp/claude_extensions/commands/consensus-init.md`
    gives explicit `/consensus-init` slash-command discoverability.
- New `consensus-init --install-claude-code` flag copies both into
  `$CLAUDE_HOME` (default `~/.claude`) using a managed idempotent
  pattern: byte-identical = no-op; divergent = skip-with-warning;
  `--force` overwrites. Honors `CLAUDE_HOME` env override for
  non-default installs (CI, devcontainers, multi-user systems).
- New `consensus-init --from-claude-code` flag prints contextual
  reload guidance after a successful init ("restart Claude Code or
  run `/mcp` to activate the consensus-mcp server"). Used by both
  the skill and slash command bodies; deterministic so we don't
  rely on env-var sniffing (codex's preferred convergence
  resolution per iter-0039 Q4).
- Bonus from iter-0039 Q6: added `consensus` console-script alias
  to pyproject.toml so `consensus init` (with a SPACE) at the
  shell works alongside the hyphenated `consensus-init`. The
  `init` subcommand is stripped from argv[0] inside `main()`;
  everything else flows through the same argparse setup.
- Side-effect hot-fix landed in the same release: the proposal-mode
  validation path in `_dispatch_codex.py` / `_dispatch_gemini.py`
  did `try: import jsonschema; ...; except jsonschema.ValidationError`
  — when the import failed (because `jsonschema` wasn't a declared
  dep), the except clause's reference to `jsonschema.ValidationError`
  surfaced as `UnboundLocalError`, masking the real problem and
  blocking iter-0039 from running in the pipx venv. Added
  `jsonschema>=4.0` to deps + split the try/except so `ImportError`
  surfaces with actionable wording. (workflow #3 hot-patch because
  it blocked iter-0039 progress.)

**Phase B closed.** All 10 tool migrations landed (iter-0026..0037).
No tool now holds a cached module-level `REPO_ROOT`/`ACTIVE_DIR`/
`ARCHIVE_DIR`/`INDEX_PATH`/etc. — every path read is lazy and honors
env-var redirection at call time. Suite green at 773 passed,
1 skipped. The cross-project pipx install pattern from iter-0030
now works in any project: `pipx install consensus-mcp` once, then
`consensus-init` in any project writes a working `.mcp.json` and
the lazy resolvers pick up the per-project state root automatically.

**Codex proposal mode (iter-0027 → iter-0028):**

- iter-0021 and iter-0024 design consults discovered that codex
  structurally-abstained on every workflow #4 dispatch because the
  codex review template is hard-coded for code-review tasks: codex
  kept returning "missing review target" errors instead of engaging
  with design questions. Documented as "structural-abstention" in
  prior converged plans.
- iter-0027 ran the workflow #4 consult on what to do about it.
  Claude and gemini independently picked "fix codex template friction"
  as the highest-leverage move. Codex itself structurally-abstained
  on this very consult (proving the problem).
- iter-0028 shipped `--mode {review,proposal}` on both codex and
  gemini dispatchers. New `codex_proposal_template.md` and
  `codex_proposal_schema.json` (plus gemini equivalents) frame the
  task as proposal generation, not code review. Proposal schema
  enforces `selected_target`, `rationale_vs_alternatives`,
  `deliverable_scope`, `risks`, `estimated_complexity`,
  `structural_abstention`. The seal pipeline detects proposal-shape
  payloads and embeds them under a top-level `proposal` block in the
  sealed YAML while keeping outer review-shape fields valid (empty
  findings, computed goal_satisfied) for audit-event compatibility.
- iter-0029: smoke test of proposal mode on a synthetic design
  question — codex returned a proper proposal-shape sealed YAML.
- iter-0030: **first real workflow #4 consult where codex engaged
  substantively as a peer.** The meta-arc iter-0021 → iter-0024 →
  iter-0027 → iter-0028 → iter-0030 closed: the project that proves
  cross-AI consensus works now genuinely produces three-AI consensus
  on its own design questions (gemini env-abstaining due to upstream
  capacity issues notwithstanding).

**`consensus-init` auto-bootstraps `.mcp.json` (iter-0030 design →
iter-0031 implementation):**

- iter-0030 converged on extending `consensus-init` to write
  `.mcp.json` automatically. Two substantive votes (claude + codex)
  agreed on merge-mode for existing files + opt-out flag +
  marker-based project-root detection + PATH-portable command
  discovery + byte-for-byte idempotency.
- iter-0031 implemented the converged design. New flags: `--no-mcp-json`
  (opt out), `--mcp-command STR` (override), `--mcp-force` (replace
  existing consensus-mcp entry on divergence). Merge mode preserves
  other MCP servers; conflict detection skip+warns instead of
  clobbering; malformed JSON skip+warns instead of mutating.
- `_detect_repo_root` extended from `.git`-only to marker-based:
  `git rev-parse --show-toplevel` → strong markers (.git,
  pyproject.toml, package.json, CLAUDE.md, .mcp.json, consensus-state)
  → cwd fallback.
- Operator UX is now one command: `pipx install consensus-mcp`, then
  `cd <any-project>; consensus-init`. The wizard writes
  `.consensus/config.yaml` + `.gitignore` managed block + `.mcp.json`
  (with correct PATH-portable command + per-project env vars).

**Defaulting to workflow #4 for design questions** (operator policy
correction mid-v1.14.0):

The original heuristic "workflow #3 for execution; workflow #4 for
explicit design questions" was systematically biased toward #3
because of cost asymmetry. Corrected mid-cycle: default to workflow
#4 for any decision with real design surface; require an explicit
reason to fall back to #3. iter-0027 onward applies this rule. Saved
to operator memory as
[[feedback-default-workflow-4-for-design]].

**Operator memories saved during v1.14.0 cycle:**

- `feedback_no_phantom_proceed.md` — never end a turn with "proceeding
  with X" unless the tool call is in the same turn
- `feedback_gemini_429_skip.md` — priority-tiered handling of upstream
  Gemini 429 errors: low-priority iterations skip gemini on first
  failure; high-priority iterations allow one retry

**Deferred to a follow-up iteration:**

- 20 stale `iter-9999-*` fixture entries remain in
  `consensus-state/archive/review-passes/index.yaml`. Cosmetic only;
  separate cleanup iteration if desired.
- Phase C cleanup of `_isolate_archive_root`-style fixtures that are
  now no-ops or simplifiable; the test helpers still work but most of
  the `monkeypatch.setattr` calls inside them became redundant once
  Phase B finished. Pure refactor, no behavior change.

## 1.13.0 - 2026-05-12

Multi-project resolution: consensus-mcp now boots in any project without a
local checkout. The frozen wheel ships a spec template, and state auto-
initializes in CWD.

**Changes (per iter-0007 codex consult, sealed packet 39f8b7a8b...):**

- New resolvers in `server.py` split the legacy `REPO_ROOT` into three
  concerns: `_resolve_spec_path` (spec source), `_resolve_state_root`
  (`consensus-state/` location), `_resolve_project_root` (reviewable-file
  root for goal_packet `allowed_files`).
- Resolution order for each:
  - Spec: `CONSENSUS_MCP_SPEC_PATH` > legacy `CONSENSUS_MCP_REPO_ROOT` >
    walked-up checkout > shipped `consensus_mcp/spec_template.md`
  - State: `CONSENSUS_MCP_STATE_ROOT` > legacy `CONSENSUS_MCP_REPO_ROOT` >
    `Path.cwd() / "consensus-state"`
  - Project: `CONSENSUS_MCP_PROJECT_ROOT` > legacy `CONSENSUS_MCP_REPO_ROOT`
    > `Path.cwd()`
- `consensus_mcp/spec_template.md` shipped in the wheel (added to
  `pyproject.toml [tool.setuptools.package-data]`). Frozen-wheel users get a
  bootable spec without cloning.
- `_resolve_repo_root` kept for back-compat; no longer load-bearing for
  spec/state/project-root in v1.13.0.

**Deferred to v1.14.0** (per codex Q1 scoping):

- Dispatcher auto-discovery of `iteration_dir/review-packet.yaml` when
  `--review-target` is non-yaml (currently silent-failure, prompt ships
  without embedded touched-file contents).
- Cold-start grace period for the watchdog (pre-first-byte vs.
  post-first-byte silence thresholds; 45s is too aggressive for bigger
  prompts).
- Visibility upgrade: dispatch_heartbeat events should expose a `status`
  field (loading / streaming / stalled) for operator clarity.

**Known pre-existing pytest flake** (predates v1.13.0; **fixed in v1.14.0
iter-0019**): 5 tests in `test_dispatch_codex.py` that pass in isolation
but fail in the full suite due to test-ordering pollution. Not introduced
by v1.13.0; tracked separately. See the v1.14.0 "Test isolation" section
above for the resolution.

## 1.12.0 - 2026-05-11

Standalone release. Extracted from upstream-26.4.16 source
project. See `docs/architecture/codex-fix-author-roadmap.md` for the work
history that led here.

Renames vs the prior internal-only releases (1.0.0 - 1.11.0):

- Python package `agent_loop_mcp` -> `consensus_mcp`
- Python package `agent_loop` (validators) -> `consensus_mcp.validators`
- State directory `agent-loop/` -> `consensus-state/`
- Env var `AGENT_LOOP_MCP_REPO_ROOT` -> `CONSENSUS_MCP_REPO_ROOT`
- MCP server name `agent-loop-mcp` -> `consensus-mcp`
- Repo-root markers updated to `("consensus-state", "consensus_mcp")`
- Flat layout (no `scripts/` prefix)
