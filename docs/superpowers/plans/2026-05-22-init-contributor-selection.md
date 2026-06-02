# Init Contributor-Selection Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `consensus init` offer the independent in-house AIs (claude/codex/gemini/kimi) dynamically, add the same-model claude reviewer as a conditional opt-in 0.5 supplemental, and move the ">=2 independent" floor into config validation - without touching the consensus gate.

**Architecture:** A small profile-kind helper layer in `_contributor_profiles.py` (resolve kind, count independents, map host<->host_peer, detect orphans) is consumed by both `config.py` (authoritative validation gate) and `_init_wizard.py` (UX). The consensus gate, convergence, and `_engine_factory` are untouched; `host_peer` stays `gate_eligible=false`.

**Tech Stack:** Python 3.11+, pytest. No new dependencies.

**Spec:** `docs/design-consults/init-contributor-selection-supplemental-review.md`
**Converged plan:** `consensus-state/active/iteration-init-supplemental-review-2026-05-22/converged-plan.yaml`

**Doctrine reminders:** strict TDD (red->green->commit); no success claims without running the command and seeing the output; `host_peer` is presentational 0.5 only - NEVER give it a gate vote.

---

### Task 1: Profile-kind helpers in `_contributor_profiles.py`

The shared, dependency-light foundation. Lives here (not the wizard) so `config.py` can import it with no cycle (config.py already imports `validate_profile` from this module). Do NOT import wizard code.

**Files:**
- Modify: `consensus_mcp/_contributor_profiles.py` (append after `validate_profile`)
- Test: `consensus_mcp/tests/test_contributor_profiles.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to consensus_mcp/tests/test_contributor_profiles.py
from consensus_mcp import _contributor_profiles as cp


def _profiles():
    # mirrors the built-in shape: a host, two cli_reviewers, one host_peer
    return {
        "claude": {"name": "claude", "kind": "host"},
        "codex": {"name": "codex", "kind": "cli_reviewer"},
        "gemini": {"name": "gemini", "kind": "cli_reviewer"},
        "claude-swe-reviewer": {
            "name": "claude-swe-reviewer", "kind": "host_peer", "family": "claude",
        },
    }


def test_resolve_kind_known_and_unknown():
    p = _profiles()
    assert cp.resolve_kind("codex", p) == "cli_reviewer"
    assert cp.resolve_kind("claude-swe-reviewer", p) == "host_peer"
    assert cp.resolve_kind("some-custom-ai", p) is None  # unknown -> None


def test_independent_count_excludes_host_peer_counts_unknown():
    p = _profiles()
    assert cp.independent_count(["claude", "codex"], p) == 2
    assert cp.independent_count(["claude", "claude-swe-reviewer"], p) == 1
    assert cp.independent_count(["claude", "codex", "claude-swe-reviewer"], p) == 2
    # unknown open contributor counts as independent (open-contributor model)
    assert cp.independent_count(["claude", "my-ai"], p) == 2


def test_host_family_host_and_host_peer():
    p = _profiles()
    assert cp.host_family("claude", p) == "claude"          # host: name fallback
    assert cp.host_family("claude-swe-reviewer", p) == "claude"  # host_peer: family
    assert cp.host_family("codex", p) is None               # cli_reviewer: none


def test_matching_host_peers():
    p = _profiles()
    assert cp.matching_host_peers("claude", p) == ["claude-swe-reviewer"]
    assert cp.matching_host_peers("codex", p) == []


def test_orphan_host_peers():
    p = _profiles()
    assert cp.orphan_host_peers(["claude", "claude-swe-reviewer"], p) == []
    assert cp.orphan_host_peers(["codex", "claude-swe-reviewer"], p) == ["claude-swe-reviewer"]
    assert cp.orphan_host_peers(["claude", "codex"], p) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest consensus_mcp/tests/test_contributor_profiles.py -k "resolve_kind or independent_count or host_family or matching_host_peers or orphan_host_peers" -v`
Expected: FAIL with `AttributeError: module 'consensus_mcp._contributor_profiles' has no attribute 'resolve_kind'`.

- [ ] **Step 3: Implement the helpers**

```python
# append to consensus_mcp/_contributor_profiles.py (after validate_profile)

def resolve_kind(name: str, profiles: dict) -> str | None:
    """Return the kind of an enabled contributor name, or None if it has no
    profile (an unknown/open contributor). Never raises."""
    p = profiles.get(name)
    return p.get("kind") if isinstance(p, dict) else None


def independent_count(enabled: list[str], profiles: dict) -> int:
    """Count INDEPENDENT contributors: everything except a known host_peer.
    Unknown names (no profile) count as independent - config.py keeps its open
    model; constructibility stays engine_factory's fail-closed job."""
    return sum(1 for n in enabled if resolve_kind(n, profiles) != KIND_HOST_PEER)


def host_family(name: str, profiles: dict) -> str | None:
    """The host-family key a name belongs to. host_peer -> its `family`; host ->
    its explicit `family` or, by convention, its own name; anything else -> None."""
    p = profiles.get(name)
    if not isinstance(p, dict):
        return None
    kind = p.get("kind")
    if kind == KIND_HOST_PEER:
        return p.get("family")
    if kind == KIND_HOST:
        return p.get("family") or p.get("name") or name
    return None


def matching_host_peers(host_name: str, profiles: dict) -> list[str]:
    """host_peer profile names whose `family` matches host_name's family.
    Sorted for determinism. Empty if host_name is not a host or has no peers."""
    fam = host_family(host_name, profiles)
    if fam is None:
        return []
    return sorted(
        n for n, p in profiles.items()
        if isinstance(p, dict)
        and p.get("kind") == KIND_HOST_PEER
        and p.get("family") == fam
    )


def orphan_host_peers(enabled: list[str], profiles: dict) -> list[str]:
    """Enabled host_peers whose host family is NOT also enabled (D4: a host_peer
    requires its host). Order follows `enabled`."""
    enabled_host_families = {
        host_family(n, profiles)
        for n in enabled
        if resolve_kind(n, profiles) == KIND_HOST
    }
    return [
        n for n in enabled
        if resolve_kind(n, profiles) == KIND_HOST_PEER
        and (profiles.get(n) or {}).get("family") not in enabled_host_families
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest consensus_mcp/tests/test_contributor_profiles.py -k "resolve_kind or independent_count or host_family or matching_host_peers or orphan_host_peers" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_contributor_profiles.py consensus_mcp/tests/test_contributor_profiles.py
git commit -m "feat(v1.20.1): profile-kind helpers (independent_count, host<->host_peer mapping)"
```

---

### Task 2: config.py - independent floor + orphan rejection

Move the authoritative floor from raw `len(enabled)` to `independent_count`, and reject orphan host_peers. This is the one gate all paths funnel through (`cfg.validate`).

**Files:**
- Modify: `consensus_mcp/config.py:405-465`
- Test: `consensus_mcp/tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

```python
# add to consensus_mcp/tests/test_config.py
import pytest
from consensus_mcp import config as cfg


def _base_config(enabled):
    c = cfg.default_config()
    c["contributors"]["enabled"] = list(enabled)
    c["workflow"]["mode"] = cfg.WORKFLOW_PROPOSE_CONVERGE
    c["convergence"]["rule"] = cfg.CONVERGE_UNANIMOUS
    return c


def test_host_peer_does_not_satisfy_floor():
    with pytest.raises(cfg.ConfigValidationError, match="2 independent|at least 2"):
        cfg.validate_config(_base_config(["claude", "claude-swe-reviewer"]))


def test_independent_pair_plus_host_peer_is_valid():
    cfg.validate_config(_base_config(["claude", "codex", "claude-swe-reviewer"]))


def test_orphan_host_peer_rejected():
    with pytest.raises(cfg.ConfigValidationError, match="orphan|requires its host|host"):
        cfg.validate_config(_base_config(["codex", "gemini", "claude-swe-reviewer"]))


def test_unknown_open_contributor_counts_independent():
    # open-contributor model: an unknown name is NOT rejected and counts toward floor
    cfg.validate_config(_base_config(["claude", "my-custom-ai"]))
```

> Adjust `validate_config` to the actual public validator name in `config.py` (e.g. `validate_config`/`validate`). Match the existing test file's import.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest consensus_mcp/tests/test_config.py -k "host_peer or orphan or unknown_open" -v`
Expected: FAIL - `test_host_peer_does_not_satisfy_floor` does NOT raise (raw len==2 passes today); `test_orphan_host_peer_rejected` does NOT raise.

- [ ] **Step 3: Implement - replace the count block**

Replace `config.py:405` and the four gate checks. Insert merged-profile resolution + orphan check, and swap `n_contributors` -> `n_independent` in the policy gates (keep operators identical):

```python
    # === Cross-validation rules per converged-plan.yaml ===
    # Floor is INDEPENDENT count (host_peer is a 0.5 supplemental, never a vote).
    # Resolve kinds via merged built-in + overlay profiles; unknown names count
    # as independent (open-contributor model). Keep the helper small - do NOT
    # import wizard code here.
    from consensus_mcp._contributor_profiles import (
        load_builtin_profiles, merge_profiles, independent_count, orphan_host_peers,
    )
    _merged = merge_profiles(load_builtin_profiles(), contributors.get("profiles") or {})
    n_independent = independent_count(enabled, _merged)

    # Rule (D4): a host_peer may be enabled only if its host family is too.
    _orphans = orphan_host_peers(enabled, _merged)
    if _orphans:
        raise ConfigValidationError(
            f"contributors.enabled has orphan supplemental reviewer(s) {_orphans!r}: "
            f"a host_peer (same-model supplemental) requires its host to also be "
            f"enabled. Add the host or remove the supplemental."
        )

    # Rule: workflow.mode=propose-converge requires >=2 INDEPENDENT
    if mode == WORKFLOW_PROPOSE_CONVERGE and n_independent < 2:
        raise ConfigValidationError(
            f"workflow.mode=propose-converge requires at least 2 independent "
            f"contributors (a same-model supplemental does not count); got "
            f"{n_independent} ({enabled!r}). Use post-review or advisory for solo setups."
        )

    if mode == WORKFLOW_AUTONOMOUS_EXECUTE and n_independent != 3:
        raise ConfigValidationError(
            f"workflow.mode=autonomous-execute requires exactly 3 independent "
            f"contributors for the wide cross-AI safety net; got {n_independent} "
            f"({enabled!r}). Use propose-converge for 2-AI setups."
        )

    # (disposition check unchanged - leave as-is) ...

    if rule == CONVERGE_STRICT_MAJ and n_independent == 1:
        raise ConfigValidationError(
            "convergence.rule=strict-majority is invalid with only 1 independent "
            "contributor; use unanimous or advisory"
        )

    if independence == INDEPENDENCE_SEQUENTIAL and n_independent < 2:
        raise ConfigValidationError(
            f"workflow.independence=sequential requires at least 2 independent "
            f"contributors; got {n_independent}"
        )
```

Leave the raw-count uniqueness/non-empty checks (322-343) and the disposition rule (432-439) unchanged.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest consensus_mcp/tests/test_config.py -k "host_peer or orphan or unknown_open" -v`
Expected: PASS (4 tests). Then full file: `python -m pytest consensus_mcp/tests/test_config.py -v` - fix any older test that assumed raw-count semantics (update to independent semantics).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/config.py consensus_mcp/tests/test_config.py
git commit -m "feat(v1.20.1): config floor counts independents + rejects orphan host_peer"
```

---

### Task 3: config.py - dynamic `default_config()` enabled

No hardcoded AI lists (decision 7): derive the default enabled set from built-in independent profiles in stable order.

**Files:**
- Modify: `consensus_mcp/config.py:174` (and the literal near `577`)
- Test: `consensus_mcp/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
def test_default_config_enabled_is_dynamic_independents_in_order():
    enabled = cfg.default_config()["contributors"]["enabled"]
    # derived from built-in independents (kind != host_peer), stable sorted order
    assert "claude-swe-reviewer" not in enabled       # host_peer excluded
    assert "kimi" in enabled                           # dynamic includes kimi
    assert enabled == sorted(enabled)                  # stable ordering pinned
    assert enabled == ["claude", "codex", "gemini", "kimi"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest consensus_mcp/tests/test_config.py -k default_config_enabled_is_dynamic -v`
Expected: FAIL - current default is the literal `["claude","codex","gemini"]` (no kimi).

- [ ] **Step 3: Implement**

Replace the literal at `config.py:174` inside `default_config()`:

```python
    # enabled: derived from built-in INDEPENDENT profiles (kind != host_peer) in
    # stable sorted order - no hardcoded AI list (decision 7). Adding a built-in
    # profile extends the default automatically.
    "enabled": _default_independent_enabled(),
```

Add the helper near the top of `config.py` (module level):

```python
def _default_independent_enabled() -> list[str]:
    from consensus_mcp._contributor_profiles import load_builtin_profiles, KIND_HOST_PEER
    profiles = load_builtin_profiles()
    return sorted(
        name for name, p in profiles.items()
        if isinstance(p, dict) and p.get("kind") != KIND_HOST_PEER
    )
```

Check the `["claude", "codex"]` literal near `config.py:577`: if it is a sample/minimal config used by code, route it through `_default_independent_enabled()` too; if it is only a docstring/example, leave it but add a comment noting the canonical default is dynamic.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest consensus_mcp/tests/test_config.py -k default_config_enabled_is_dynamic -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/config.py consensus_mcp/tests/test_config.py
git commit -m "feat(v1.20.1): default_config enabled derives from built-in independents"
```

---

### Task 4: wizard - main multi-select excludes host_peer (+ preselect support)

**Files:**
- Modify: `consensus_mcp/_init_wizard.py:369-460` (`_ordered_profile_names`, `_select_contributors_interactive`); delete the now-dead `_contributor_option_note` (393-408)
- Test: `consensus_mcp/tests/test_init_wizard_contributors.py`

- [ ] **Step 1: Write the failing tests**

```python
# add to consensus_mcp/tests/test_init_wizard_contributors.py
from consensus_mcp import _init_wizard as wiz

_PROFILES = {
    "claude": {"name": "claude", "kind": "host"},
    "codex": {"name": "codex", "kind": "cli_reviewer", "detect": {"command": "codex"}},
    "kimi": {"name": "kimi", "kind": "cli_reviewer", "detect": {"command": "kimi"}},
    "claude-swe-reviewer": {"name": "claude-swe-reviewer", "kind": "host_peer", "family": "claude"},
}


def test_selectable_names_exclude_host_peer():
    names = wiz._independent_ordered_names(_PROFILES)
    assert "claude-swe-reviewer" not in names
    assert names[0] == "claude"  # host first
    assert set(names) == {"claude", "codex", "kimi"}


def test_multiselect_list_has_no_host_peer(monkeypatch, capsys):
    monkeypatch.setattr(wiz.shutil, "which", lambda c: "/x/" + c)  # all installed
    monkeypatch.setattr("builtins.input", lambda *_: "1,2")  # claude, codex
    chosen = wiz._select_contributors_interactive(_PROFILES)
    out = capsys.readouterr().out
    assert "claude-swe-reviewer" not in out
    assert chosen == ["claude", "codex"]


def test_multiselect_preselected_defaults(monkeypatch):
    monkeypatch.setattr(wiz.shutil, "which", lambda c: None)  # none installed
    monkeypatch.setattr("builtins.input", lambda *_: "")  # accept default
    chosen = wiz._select_contributors_interactive(_PROFILES, preselected=["claude", "kimi"])
    assert chosen == ["claude", "kimi"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard_contributors.py -k "selectable_names or multiselect" -v`
Expected: FAIL - `_independent_ordered_names` missing; current list includes host_peer; no `preselected` param.

- [ ] **Step 3: Implement**

Add the filter helper and rewrite the selection function head:

```python
def _independent_ordered_names(profiles: dict) -> list[str]:
    """Display/selection order over INDEPENDENT profiles only (host first, then
    sorted). host_peer is excluded - it is offered via the conditional follow-up."""
    indep = {
        n: p for n, p in profiles.items()
        if isinstance(p, dict) and p.get("kind") != profiles_mod.KIND_HOST_PEER
    }
    host = sorted(n for n in indep if indep[n].get("kind") == profiles_mod.KIND_HOST)
    rest = sorted(n for n in indep if n not in host)
    return host + rest
```

Change `_select_contributors_interactive` signature and internals: use `_independent_ordered_names`, accept `preselected`, drop the `_contributor_option_note` suffix:

```python
def _select_contributors_interactive(profiles: dict, preselected: list[str] | None = None) -> list[str]:
    names = _independent_ordered_names(profiles)
    installed = {n: _profile_installed(profiles[n]) for n in names}
    if preselected is not None:
        prechecked = [n for n in names if n in preselected]
    else:
        prechecked = [n for n in names if installed[n]]

    print("Select the AI reviewers to use (>=2 required):")
    for idx, name in enumerate(names, start=1):
        status = "[ok] installed" if installed[name] else "[x] missing"
        mark = "x" if name in prechecked else " "
        print(f"  [{mark}] {idx}. {name} ({status})")
    default_hint = ",".join(str(names.index(n) + 1) for n in prechecked) or "none"

    while True:
        try:
            raw = input(f"Enter comma-separated numbers [default: {default_hint}]: ").strip()
        except EOFError as exc:
            raise KeyboardInterrupt from exc
        if not raw:
            chosen = list(prechecked)
        else:
            chosen, bad = [], False
            for tok in (t.strip() for t in raw.split(",") if t.strip()):
                if not tok.isdigit() or not (1 <= int(tok) <= len(names)):
                    print(f"  invalid selection {tok!r}", file=sys.stderr); bad = True; break
                cand = names[int(tok) - 1]
                if cand not in chosen:
                    chosen.append(cand)
            if bad:
                continue
        if len(chosen) < 2:
            print("  please select at least 2 independent reviewers", file=sys.stderr)
            continue
        return chosen
```

Delete `_contributor_option_note` (393-408) - host_peer is no longer in this list, so the note is dead.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard_contributors.py -k "selectable_names or multiselect" -v`
Expected: PASS (3). Then run the whole file and fix any test referencing `_contributor_option_note` or expecting host_peer in the list.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard_contributors.py
git commit -m "feat(v1.20.1): wizard main select lists independents only (+preselect)"
```

---

### Task 5: wizard - dynamic `_detect_available_contributors`

**Files:**
- Modify: `consensus_mcp/_init_wizard.py:332-339`
- Test: `consensus_mcp/tests/test_init_wizard.py`

- [ ] **Step 1: Write the failing test**

```python
def test_detect_available_is_dynamic_over_profiles(monkeypatch, tmp_path):
    from consensus_mcp import _init_wizard as wiz
    fake = {
        "claude": {"name": "claude", "kind": "host"},
        "codex": {"name": "codex", "kind": "cli_reviewer", "detect": {"command": "codex"}},
        "kimi": {"name": "kimi", "kind": "cli_reviewer", "detect": {"command": "kimi"}},
        "claude-swe-reviewer": {"name": "claude-swe-reviewer", "kind": "host_peer", "family": "claude"},
    }
    monkeypatch.setattr(wiz, "_load_merged_profiles", lambda *_: fake)
    monkeypatch.setattr(wiz.shutil, "which", lambda c: "/x/" + c if c in ("codex", "kimi") else None)
    got = wiz._detect_available_contributors(tmp_path)
    assert "kimi" in got                       # dynamic - not hardcoded
    assert "claude" in got                     # host always available
    assert "claude-swe-reviewer" not in got    # host_peer excluded
    assert "gemini" not in got                 # not installed
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard.py -k detect_available_is_dynamic -v`
Expected: FAIL - current function is hardcoded to claude/codex/gemini (no kimi).

- [ ] **Step 3: Implement**

```python
def _detect_available_contributors(repo_root: Path) -> list[str]:
    """Installed INDEPENDENT contributors, derived dynamically from the merged
    profile set (no hardcoded AI list - decision 7). host is always available;
    cli_reviewers iff their detect.command resolves on PATH; host_peer excluded
    (it is offered via the conditional follow-up, never auto-enabled)."""
    profiles = _load_merged_profiles(None)
    out = []
    for name in _independent_ordered_names(profiles):
        if _profile_installed(profiles[name]):
            out.append(name)
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard.py -k detect_available_is_dynamic -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard.py
git commit -m "feat(v1.20.1): dynamic contributor detection over merged profiles"
```

---

### Task 6: wizard - conditional supplemental follow-up (fresh path)

**Files:**
- Create helper in `consensus_mcp/_init_wizard.py` (near `_select_contributors_interactive`)
- Modify: `consensus_mcp/_init_wizard.py:690-699` (fresh branch of `interactive_overrides`)
- Test: `consensus_mcp/tests/test_init_wizard_contributors.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_followup_offered_when_host_selected(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    add = wiz._prompt_host_peer_followup(["claude", "codex"], _PROFILES, default_yes=False)
    assert add == "claude-swe-reviewer"


def test_followup_default_no(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "")  # empty -> default
    add = wiz._prompt_host_peer_followup(["claude", "codex"], _PROFILES, default_yes=False)
    assert add is None


def test_followup_skipped_when_no_host(monkeypatch):
    def boom(*_):
        raise AssertionError("must not prompt when no host selected")
    monkeypatch.setattr("builtins.input", boom)
    assert wiz._prompt_host_peer_followup(["codex", "kimi"], _PROFILES, default_yes=False) is None


def test_followup_default_yes_on_empty(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "")
    add = wiz._prompt_host_peer_followup(["claude", "codex"], _PROFILES, default_yes=True)
    assert add == "claude-swe-reviewer"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard_contributors.py -k followup -v`
Expected: FAIL - `_prompt_host_peer_followup` missing.

- [ ] **Step 3: Implement the follow-up + wire it into the fresh path**

```python
def _prompt_host_peer_followup(selection, profiles, default_yes: bool):
    """If a host is selected and a same-family host_peer profile exists (and is
    not already enabled), offer it as a 0.5 supplemental. Returns the host_peer
    profile name to append, or None. Multiple same-family host_peers -> mini-
    select defaulting to none (never silently pick the first)."""
    candidates = []
    for host in (n for n in selection if profiles_mod.resolve_kind(n, profiles) == profiles_mod.KIND_HOST):
        for hp in profiles_mod.matching_host_peers(host, profiles):
            if hp not in selection and hp not in candidates:
                candidates.append(hp)
    if not candidates:
        return None

    print("\nYou're using claude as the host. Add a same-model claude review agent?")
    print(
        "This is a SUPPLEMENTAL review (shown as +0.5 in the init summary only - NOT a\n"
        "fully independent reviewer; it shares the host model's blind spots). It gets no\n"
        "vote at the consensus gate and can't close consensus (claude already votes as\n"
        "host) - but every good idea it raises is still applied on merit. A useful extra\n"
        "pass if you have the tokens to spare."
    )
    if len(candidates) == 1:
        default = "y" if default_yes else "n"
        ans = (input(f"Add it? [{'Y/n' if default_yes else 'y/N'}]: ").strip().lower() or default)
        return candidates[0] if ans.startswith("y") else None

    # multiple same-family host_peers: deterministic mini-select, default none
    print("Multiple same-model reviewers available (choose one or none):")
    for i, hp in enumerate(candidates, start=1):
        print(f"  {i}. {hp}")
    raw = input("Number to add [default: none]: ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(candidates):
        return candidates[int(raw) - 1]
    return None
```

Wire into the fresh branch of `interactive_overrides` (after line 699):

```python
        if fresh:
            profiles = _load_merged_profiles((base.get("contributors") or {}).get("profiles"))
            selection = _select_contributors_interactive(profiles)
            hp = _prompt_host_peer_followup(selection, profiles, default_yes=False)
            if hp:
                selection.append(hp)
            base["contributors"]["enabled"] = selection
```

Add `from consensus_mcp import _contributor_profiles as profiles_mod` import if not present (the wizard already imports it as `profiles_mod` per `_load_merged_profiles`).

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard_contributors.py -k followup -v`
Expected: PASS (4).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard_contributors.py
git commit -m "feat(v1.20.1): conditional opt-in same-model supplemental follow-up"
```

---

### Task 7: wizard - `--contributors` flag floor + orphan rejection

**Files:**
- Modify: `consensus_mcp/_init_wizard.py:463-478` (`_validate_contributor_selection`)
- Test: `consensus_mcp/tests/test_init_wizard_contributors.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_flag_rejects_host_peer_padding():
    with pytest.raises(wiz.WizardError, match="independent"):
        wiz._validate_contributor_selection(["claude", "claude-swe-reviewer"], _PROFILES)


def test_flag_rejects_orphan_host_peer():
    with pytest.raises(wiz.WizardError, match="orphan|host"):
        wiz._validate_contributor_selection(["codex", "kimi", "claude-swe-reviewer"], _PROFILES)


def test_flag_accepts_independents_plus_supplemental():
    assert wiz._validate_contributor_selection(
        ["claude", "codex", "claude-swe-reviewer"], _PROFILES
    ) == ["claude", "codex", "claude-swe-reviewer"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard_contributors.py -k "flag_rejects or flag_accepts" -v`
Expected: FAIL - current `_validate_contributor_selection` uses raw `len(selection) < 2`, no orphan check.

- [ ] **Step 3: Implement**

```python
def _validate_contributor_selection(selection: list[str], profiles: dict) -> list[str]:
    """Validate a name list: known names only (wizard layer holds the profile
    set), >=2 INDEPENDENT, and no orphan host_peer."""
    unknown = [n for n in selection if n not in profiles]
    if unknown:
        raise WizardError(f"unknown contributor(s) {unknown}; known: {sorted(profiles)}")
    if profiles_mod.independent_count(selection, profiles) < 2:
        raise WizardError(
            f"at least 2 independent contributors are required (a same-model "
            f"supplemental does not count); got {selection!r}"
        )
    orphans = profiles_mod.orphan_host_peers(selection, profiles)
    if orphans:
        raise WizardError(
            f"orphan supplemental reviewer(s) {orphans}: a host_peer requires its "
            f"host to also be enabled"
        )
    return list(selection)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard_contributors.py -k "flag_rejects or flag_accepts" -v`
Expected: PASS (3).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard_contributors.py
git commit -m "feat(v1.20.1): --contributors enforces independent floor + orphan rejection"
```

---

### Task 8: wizard - reconfigure preserves a legacy host_peer

**Files:**
- Modify: `consensus_mcp/_init_wizard.py:700-705` (reconfigure branch of `interactive_overrides`)
- Test: `consensus_mcp/tests/test_init_wizard_contributors.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_reconfigure_preserves_existing_host_peer(monkeypatch):
    base = {"contributors": {"enabled": ["claude", "codex", "claude-swe-reviewer"]}}
    monkeypatch.setattr(wiz, "_load_merged_profiles", lambda *_: _PROFILES)
    monkeypatch.setattr(wiz.shutil, "which", lambda c: "/x/" + c)
    # accept preselected independents (empty), then accept supplemental (empty -> default Yes)
    answers = iter(["", ""])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    args = type("A", (), {})()
    base_cfg = {"contributors": {"enabled": ["claude", "codex", "claude-swe-reviewer"]},
                "workflow": {"mode": "x", "independence": "y"},
                "convergence": {"rule": "z"}}
    wiz._reconfigure_contributors(base_cfg, _PROFILES)
    assert "claude-swe-reviewer" in base_cfg["contributors"]["enabled"]
    assert set(base_cfg["contributors"]["enabled"]) >= {"claude", "codex"}


def test_reconfigure_invalid_legacy_forces_two_independents(monkeypatch):
    monkeypatch.setattr(wiz, "_load_merged_profiles", lambda *_: _PROFILES)
    monkeypatch.setattr(wiz.shutil, "which", lambda c: "/x/" + c)
    # legacy [claude, claude-swe-reviewer] -> 1 independent; multi-select must
    # re-prompt until >=2; user adds codex (option 2), then declines supplemental
    answers = iter(["1,2", "n"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    base_cfg = {"contributors": {"enabled": ["claude", "claude-swe-reviewer"]},
                "workflow": {"mode": "x", "independence": "y"},
                "convergence": {"rule": "z"}}
    wiz._reconfigure_contributors(base_cfg, _PROFILES)
    assert wiz.profiles_mod.independent_count(base_cfg["contributors"]["enabled"], _PROFILES) >= 2
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard_contributors.py -k reconfigure -v`
Expected: FAIL - `_reconfigure_contributors` does not exist; current reconfigure is free-text.

- [ ] **Step 3: Implement - extract a reconfigure helper and call it**

```python
def _reconfigure_contributors(base: dict, profiles: dict) -> None:
    """Reconfigure path: pre-seed the multi-select with the existing INDEPENDENT
    selection (the >=2 loop guides the user to fix an invalid legacy config), then
    offer the supplemental follow-up defaulting to its CURRENT state (preserve a
    legacy host_peer)."""
    existing = list((base.get("contributors") or {}).get("enabled") or [])
    existing_independent = [n for n in existing if profiles_mod.resolve_kind(n, profiles) != profiles_mod.KIND_HOST_PEER]
    had_host_peer = any(profiles_mod.resolve_kind(n, profiles) == profiles_mod.KIND_HOST_PEER for n in existing)
    selection = _select_contributors_interactive(profiles, preselected=existing_independent)
    hp = _prompt_host_peer_followup(selection, profiles, default_yes=had_host_peer)
    if hp:
        selection.append(hp)
    base["contributors"]["enabled"] = selection
```

Replace the reconfigure branch in `interactive_overrides` (700-705):

```python
        else:
            profiles = _load_merged_profiles((base.get("contributors") or {}).get("profiles"))
            _reconfigure_contributors(base, profiles)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard_contributors.py -k reconfigure -v`
Expected: PASS (2). Then run the whole wizard test file and update any test that asserted the old free-text reconfigure prompt.

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard_contributors.py
git commit -m "feat(v1.20.1): reconfigure preserves legacy host_peer; guides invalid configs"
```

---

### Task 9: wizard - weighted panel summary line

**Files:**
- Modify: `consensus_mcp/_init_wizard.py` (`cmd_init`, after the final config is built ~line 1062)
- Test: `consensus_mcp/tests/test_init_wizard.py`

- [ ] **Step 1: Write the failing test**

```python
def test_panel_summary_weighted(capsys):
    from consensus_mcp import _init_wizard as wiz
    wiz._print_panel_summary(["claude", "codex", "claude-swe-reviewer"], _PROFILES)
    out = capsys.readouterr().out
    assert "2.5 reviewers" in out
    assert "2 independent" in out
    assert "claude-swe-reviewer" in out


def test_panel_summary_no_supplemental(capsys):
    from consensus_mcp import _init_wizard as wiz
    wiz._print_panel_summary(["claude", "codex"], _PROFILES)
    out = capsys.readouterr().out
    assert "2 independent reviewers" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard.py -k panel_summary -v`
Expected: FAIL - `_print_panel_summary` missing.

- [ ] **Step 3: Implement + call from cmd_init**

```python
def _print_panel_summary(enabled: list[str], profiles: dict) -> None:
    indep = [n for n in enabled if profiles_mod.resolve_kind(n, profiles) != profiles_mod.KIND_HOST_PEER]
    peers = [n for n in enabled if profiles_mod.resolve_kind(n, profiles) == profiles_mod.KIND_HOST_PEER]
    if peers:
        total = f"{len(indep)}.5"
        print(
            f"Panel: {total} reviewers - {len(indep)} independent "
            f"({', '.join(indep)}) + 0.5 supplemental same-model ({', '.join(peers)})."
        )
    else:
        print(f"Panel: {len(indep)} independent reviewers ({', '.join(indep)}).")
```

In `cmd_init`, after `enabled = (new_config.get("contributors") or {}).get("enabled") or []` (~line 1067), call:

```python
        _print_panel_summary(enabled, merged_profiles)
```

(Reuse the `merged_profiles` already loaded there for detect+guide.)

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest consensus_mcp/tests/test_init_wizard.py -k panel_summary -v`
Expected: PASS (2).

- [ ] **Step 5: Commit**

```bash
git add consensus_mcp/_init_wizard.py consensus_mcp/tests/test_init_wizard.py
git commit -m "feat(v1.20.1): weighted panel summary line at init"
```

---

### Task 10: full suite + smoke + gate non-regression

**Files:** none (verification)

- [ ] **Step 1: Run the full suite**

Run: `python -m pytest consensus_mcp/tests/ -q`
Expected: all pass / 1 skip (the standing skip). Fix any test that assumed raw-count floor, host_peer in the main list, or the old free-text reconfigure.

- [ ] **Step 2: Assert the gate is untouched**

Run: `git diff --stat origin/v1.20.1 -- consensus_mcp/_engine_factory.py consensus_mcp/workflow_engine.py`
Expected: NO changes to gate/closure/convergence files. host_peer must remain `gate_eligible=false`. If either file shows in the diff, STOP - the change leaked into the gate.

- [ ] **Step 3: E2E smoke - non-interactive init**

Run: `python -m consensus_mcp._init_wizard --help` then a non-interactive init in a tmp dir with `--contributors claude,codex,claude-swe-reviewer` and confirm the written `.consensus/config.yaml` validates and the panel summary prints "2.5 reviewers". Also confirm `--contributors claude,claude-swe-reviewer` exits non-zero with the ">=2 independent" message.

- [ ] **Step 4: Commit any test fixups**

```bash
git add -A
git commit -m "test(v1.20.1): align suite with independent-floor + supplemental-followup redesign"
```

---

## Self-Review notes (author)

- Spec coverage: D1 (Task 4), D2 (Tasks 2,7), D3 open-contributor (Task 2 `test_unknown_open_contributor`), D4 orphan (Tasks 2,7), D5 reconfigure (Task 8), D6 default-state (Tasks 4,5,6), D7 trigger (Task 6), D8 multiple host_peer (Task 6 mini-select), D9 dynamic detection (Tasks 3,5), D10 messaging (Task 6 prompt text). All covered.
- Type consistency: helper names (`resolve_kind`, `independent_count`, `host_family`, `matching_host_peers`, `orphan_host_peers`) are used identically in config.py and the wizard. The wizard refers to the module as `profiles_mod` (existing alias).
- Verify-on-implement: the exact public validator name in config.py (`validate_config` vs `validate`) and the precise insertion lines in `cmd_init` may have shifted; the implementer confirms against the live file before editing.
