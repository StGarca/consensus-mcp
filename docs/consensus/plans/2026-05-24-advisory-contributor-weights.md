# Advisory Contributor Weights - static structure + safety firewall (Plan 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use consensus:subagent-driven-development (recommended) or consensus:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the *advisory, static* contributor-weight structure + the safety firewall the weighted-consensus consult converged on - with NO learner and NO external ledger (those are Plan 2). This is the panel's explicit "ship weak-prior + caps only" fallback, valuable on its own and a prerequisite the learner plugs into later.

**Architecture:** A pure-function module `consensus_mcp/_contributor_weights.py` computes a per-(contributor, domain) advisory weight from a weak Beta(2,2) seed (posterior-mean mapping, discount-only) and applies floor / cap / same-family-aggregate caps. Weights are ADVISORY: they only ever re-ORDER findings for synthesis prominence - never add/remove findings, never feed the convergence rule or the cross-family gate. The firewall is enforced by tests (weights-off equivalence as a permutation property; no-self-grade as a structural absence-of-write-API). Learning from external outcomes is deliberately out of scope (Plan 2).

**Tech Stack:** Python 3.11+, stdlib only, pytest. Pure functions; no I/O, no engine wiring in this plan.

**Source spec:** `consensus-state/active/iteration-weighted-consensus-converge-2026-05-24/converged-plan.yaml` (D1-D5). Build sequence step 3 ("static advisory structure"); steps 1/4/5 (ledger, learner, A/B) are Plan 2.

---

## File Structure

- Create `consensus_mcp/_contributor_weights.py` - the whole advisory-weight module (one responsibility: compute + apply advisory weights; reorder findings). Small, pure, no I/O.
- Create `consensus_mcp/tests/test_contributor_weights.py` - the unit + firewall tests.
- No changes to `workflow_engine.py` or `config.py` in this plan: weights are computed as a standalone advisory artifact. Wiring the ordering into a live synthesis consumer is a Plan-2 task (so the weights-off-equivalence test in this plan is a property of the pure functions, kept non-vacuous by the permutation assertion).

**Constants (single source of truth, top of the module):**
```python
NEUTRAL_MEAN = 0.5      # Beta(2,2) seed posterior mean
SEED_ALPHA, SEED_BETA = 2.0, 2.0
FLOOR = 0.25            # hard non-zero floor; no enabled voice is silenced
CAP = 1.0              # discount-only: weights in [FLOOR, CAP], never amplify above baseline
SAME_FAMILY_AGGREGATE_CAP = 1.0  # one independent-contributor-equivalent
```

---

### Task 1: posterior-mean -> weight mapping (discount-only)

**Files:**
- Create: `consensus_mcp/_contributor_weights.py`
- Test: `consensus_mcp/tests/test_contributor_weights.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from consensus_mcp import _contributor_weights as cw

@pytest.mark.parametrize("mean, expected", [
    (0.5, 1.0),    # neutral seed -> full (not discounted)
    (1.0, 1.0),    # proven-good -> still full (never amplified above CAP)
    (0.75, 1.0),   # above neutral -> capped at full (discount-only)
    (0.25, 0.625), # below neutral -> linearly discounted: 0.25 + 0.75*(0.25/0.5)
    (0.0, 0.25),   # worst -> floor
])
def test_weight_from_mean_is_discount_only(mean, expected):
    assert cw.weight_from_mean(mean) == pytest.approx(expected)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest consensus_mcp/tests/test_contributor_weights.py::test_weight_from_mean_is_discount_only -v`
Expected: FAIL - module `_contributor_weights` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
"""Advisory contributor weights (static structure + firewall).

Per the weighted-consensus convergence consult (2026-05-24): weights are ADVISORY
ONLY. They re-order findings for synthesis prominence; they NEVER add/remove a
finding, feed the convergence rule, or affect the cross-family gate. This module
is the static "weak-prior + caps" structure - no learner, no external ledger
(Plan 2). Discount-only: a contributor can lose attention by being proven
unreliable but can never be amplified above baseline.
"""
from __future__ import annotations

NEUTRAL_MEAN = 0.5
SEED_ALPHA, SEED_BETA = 2.0, 2.0
FLOOR = 0.25
CAP = 1.0
SAME_FAMILY_AGGREGATE_CAP = 1.0


def weight_from_mean(posterior_mean: float) -> float:
    """Map a Beta posterior mean to a discount-only advisory weight in [FLOOR, CAP].
    Neutral (0.5) and above map to CAP (full, not discounted); below neutral is
    linearly discounted toward FLOOR. Never amplifies above CAP."""
    if not 0.0 <= posterior_mean <= 1.0:
        raise ValueError(f"posterior_mean must be in [0,1], got {posterior_mean}")
    raw = FLOOR + (CAP - FLOOR) * (posterior_mean / NEUTRAL_MEAN)
    return max(FLOOR, min(CAP, raw))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest consensus_mcp/tests/test_contributor_weights.py::test_weight_from_mean_is_discount_only -v`
Expected: PASS (5 params).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_contributor_weights.py consensus_mcp/tests/test_contributor_weights.py
git commit -m "feat(weights): discount-only posterior-mean->weight mapping (advisory, Plan 1 T1)"
```

---

### Task 2: seed posterior mean (cold-start = neutral, no learning)

**Files:**
- Modify: `consensus_mcp/_contributor_weights.py`
- Test: `consensus_mcp/tests/test_contributor_weights.py`

- [ ] **Step 1: Write the failing test**

```python
def test_seed_mean_is_neutral_and_weight_is_full():
    # Plan 1 has no learner: every (contributor, domain) sits at the Beta(2,2) seed.
    assert cw.seed_posterior_mean() == pytest.approx(0.5)
    assert cw.weight_for(contributor="codex", domain="security") == pytest.approx(1.0)
    assert cw.weight_for(contributor="gemini", domain="ux") == pytest.approx(1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest consensus_mcp/tests/test_contributor_weights.py::test_seed_mean_is_neutral_and_weight_is_full -v`
Expected: FAIL - `seed_posterior_mean` / `weight_for` not defined.

- [ ] **Step 3: Write minimal implementation** (append to the module)

```python
def seed_posterior_mean() -> float:
    """Beta(2,2) seed mean. Plan 1 has no learner, so every cell sits here."""
    return SEED_ALPHA / (SEED_ALPHA + SEED_BETA)


def weight_for(contributor: str, domain: str) -> float:
    """Advisory weight for a (contributor, domain) cell. Plan 1: always the seed
    (no learning). Plan 2 replaces the mean source with the ledger-fed posterior."""
    return weight_from_mean(seed_posterior_mean())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest consensus_mcp/tests/test_contributor_weights.py::test_seed_mean_is_neutral_and_weight_is_full -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(weights): Beta(2,2) seed cold-start, neutral weight (Plan 1 T2)"
```

---

### Task 3: advisory re-ordering is a permutation (weights-off equivalence)

**Files:**
- Modify: `consensus_mcp/_contributor_weights.py`
- Test: `consensus_mcp/tests/test_contributor_weights.py`

- [ ] **Step 1: Write the failing test**

```python
def test_order_by_weight_is_a_permutation_never_drops():
    findings = [
        {"id": "f1", "contributor": "codex", "domain": "security"},
        {"id": "f2", "contributor": "gemini", "domain": "ux"},
        {"id": "f3", "contributor": "host_peer", "domain": "security"},
    ]
    weights = {("codex", "security"): 1.0, ("gemini", "ux"): 0.5,
               ("host_peer", "security"): 0.25}
    ordered = cw.order_by_weight(findings, weights)
    # SAME set of finding ids (advisory reorder NEVER adds/removes) -> weights-off equivalence
    assert {f["id"] for f in ordered} == {f["id"] for f in findings}
    assert len(ordered) == len(findings)
    # higher weight first; stable for ties
    assert [f["id"] for f in ordered] == ["f1", "f2", "f3"]

def test_order_by_weight_with_no_weights_is_identity():
    findings = [{"id": "a", "contributor": "x", "domain": "d"},
                {"id": "b", "contributor": "y", "domain": "d"}]
    assert cw.order_by_weight(findings, {}) == findings  # unknown cells -> neutral, stable
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest consensus_mcp/tests/test_contributor_weights.py -k order_by_weight -v`
Expected: FAIL - `order_by_weight` not defined.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def order_by_weight(findings: list[dict], weights: dict[tuple[str, str], float]
                    ) -> list[dict]:
    """Return findings re-ordered by descending advisory weight. This is the ONLY
    effect weights have: a STABLE permutation for synthesis reading-order. It never
    adds, removes, or mutates a finding (weights-off equivalence: the SET the gate
    and convergence rule see is unchanged). Unknown cells default to the neutral
    seed weight."""
    neutral = weight_from_mean(seed_posterior_mean())

    def key(item):
        i, f = item
        w = weights.get((f.get("contributor"), f.get("domain")), neutral)
        return (-w, i)  # -w: higher weight first; i: stable tie-break by original index

    return [f for _, f in sorted(enumerate(findings), key=key)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest consensus_mcp/tests/test_contributor_weights.py -k order_by_weight -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(weights): advisory order_by_weight is a stable permutation (Plan 1 T3)"
```

---

### Task 4: same-family aggregate cap

**Files:**
- Modify: `consensus_mcp/_contributor_weights.py`
- Test: `consensus_mcp/tests/test_contributor_weights.py`

- [ ] **Step 1: Write the failing test**

```python
def test_same_family_aggregate_capped_to_one_independent():
    # orchestrator + host_peer are the same family; their COMBINED effective weight
    # must not exceed one independent-contributor-equivalent (1.0).
    raw = {"orchestrator": 1.0, "host_peer": 1.0, "codex": 1.0}
    families = {"orchestrator": "claude", "host_peer": "claude", "codex": "codex"}
    capped = cw.apply_same_family_cap(raw, families)
    assert capped["orchestrator"] + capped["host_peer"] == pytest.approx(1.0)
    assert capped["codex"] == pytest.approx(1.0)  # independent unaffected
    # each scaled proportionally (equal here -> 0.5 each)
    assert capped["orchestrator"] == pytest.approx(0.5)
    assert capped["host_peer"] == pytest.approx(0.5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest consensus_mcp/tests/test_contributor_weights.py::test_same_family_aggregate_capped_to_one_independent -v`
Expected: FAIL - `apply_same_family_cap` not defined.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def apply_same_family_cap(weights_by_contributor: dict[str, float],
                          family_by_contributor: dict[str, str]
                          ) -> dict[str, float]:
    """Scale each family's contributors down proportionally so no single family's
    aggregate effective weight exceeds SAME_FAMILY_AGGREGATE_CAP. Families at or
    under the cap are untouched."""
    by_family: dict[str, list[str]] = {}
    for c, fam in family_by_contributor.items():
        by_family.setdefault(fam, []).append(c)
    out = dict(weights_by_contributor)
    for fam, contributors in by_family.items():
        total = sum(out.get(c, 0.0) for c in contributors)
        if total > SAME_FAMILY_AGGREGATE_CAP and total > 0:
            scale = SAME_FAMILY_AGGREGATE_CAP / total
            for c in contributors:
                out[c] = out.get(c, 0.0) * scale
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest consensus_mcp/tests/test_contributor_weights.py::test_same_family_aggregate_capped_to_one_independent -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(weights): same-family aggregate cap (Plan 1 T4)"
```

---

### Task 5: no-self-grade structural guard (the firewall, locked by a test)

**Files:**
- Modify: `consensus_mcp/_contributor_weights.py`
- Test: `consensus_mcp/tests/test_contributor_weights.py`

- [ ] **Step 1: Write the failing test**

```python
import inspect

def test_module_exposes_no_weight_write_api():
    """no-self-grade (static form): in Plan 1 weights derive ONLY from the seed +
    config; there is NO public function that writes/sets a contributor's weight or
    usefulness credit from caller input. A learner (Plan 2) may only add a writer
    that reads the external ledger - never an agent-callable setter. This test locks
    that: no public callable name implies a write/set/update/record/grade of weight
    or credit."""
    forbidden = ("set_weight", "update_weight", "record_credit", "grade",
                 "set_credit", "update_credit", "write_weight")
    public = [n for n, _ in inspect.getmembers(cw, callable) if not n.startswith("_")]
    offending = [n for n in public
                 if any(tok in n.lower() for tok in
                        ("set_weight", "update_weight", "record_credit", "grade",
                         "set_credit", "update_credit", "write_weight"))]
    assert offending == [], f"weight/credit write API present (no-self-grade violation): {offending}"
```

- [ ] **Step 2: Run test to verify it fails-then-passes**

Run: `python -m pytest consensus_mcp/tests/test_contributor_weights.py::test_module_exposes_no_weight_write_api -v`
Expected: PASS immediately (the module has no such API yet). This is a *characterization/firewall* test - it locks the no-self-grade invariant so a future change that adds an agent-callable weight setter fails here. (If it does not pass, a forbidden API already exists - remove it.)

- [ ] **Step 3: (no implementation needed - the invariant is "absence")**

No code change. The test guards that the absence is intentional and permanent.

- [ ] **Step 4: Run the whole module's tests**

Run: `python -m pytest consensus_mcp/tests/test_contributor_weights.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "test(weights): lock no-self-grade firewall (no weight/credit write API) (Plan 1 T5)"
```

---

### Task 6: full-suite regression + docstring cross-reference

- [ ] **Step 1:** Run the full suite: `python -m pytest consensus_mcp/tests/ -q` - expected: all green (prior 1506 + the new module's tests).
- [ ] **Step 2:** Confirm the module docstring cites the converged-plan spec path and the "advisory-only / no learner / Plan 2 = ledger+learner" boundary (already in Task 1 Step 3). If missing, add it.
- [ ] **Step 3:** Commit any docstring fix: `git commit -am "docs(weights): cite converged spec + Plan-2 boundary"`.

---

## Self-Review

**1. Spec coverage:** D2 cold-start (Beta(2,2) seed) -> T2. D3 posterior-mean mapping -> T1. D4 floor/cap/discount-only -> T1, same-family cap -> T4. D5 weights-off equivalence -> T3 (permutation property), no-self-grade -> T5. **Out of scope by design (Plan 2):** D1 useful-signal ledger, the learner (mean from real outcomes), decay/min-sample, A/B. Per-domain weights = the (contributor, domain) key throughout. [ok] The one D5 item only partially covered here is the *engine-level* weights-off equivalence (delete-the-table-replay-the-gate) - that lands in Plan 2 when weights are actually wired into a synthesis consumer; in Plan 1 the permutation property is the honest static form, noted in File Structure.

**2. Placeholder scan:** every code step has complete code; no TBD/TODO. [ok]

**3. Type consistency:** `weight_from_mean`, `seed_posterior_mean`, `weight_for`, `order_by_weight`, `apply_same_family_cap` - names consistent across tasks; weights keyed by `(contributor, domain)` tuple consistently; `apply_same_family_cap` keyed by contributor (a later aggregation step), which is the correct granularity for the family cap. [ok]

## Notes for Plan 2 (do NOT build here)
External-outcome adjudication ledger (append-only, sealed, AI-read-only) feeding GOLD/SECONDARY labels; the Beta learner (posterior-mean from ledger, decay half-life 20 + model-version reset, min-sample 5 with linear dampening); wiring `order_by_weight` into the live synthesis consumer in `workflow_engine.py` + the engine-level weights-off-equivalence replay test; A/B vs uniform with revert. Ship the learner ONLY if the ledger is wirable (D5c).
