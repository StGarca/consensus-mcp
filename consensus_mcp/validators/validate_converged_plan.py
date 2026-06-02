"""Structural + consequence validator for the converged-plan convention.

v1.15.1 - machine-enforcement of the v1.15.0 doctrine
(docs/workflows/converged-plan-convention.md). Converged plan:
iteration-converged-plan-machine-enforcement (Workflow A
weighted-synthesis: claude + codex + gemini).

HIGHEST-ORDER design constraint (the recursive trap, v1.15.0 lesson 1
applied to our own gate): this module checks PRESENCE and STRUCTURE and
the declared CONSEQUENCES of an orchestrator-attested bool. It has NO
code path that derives any approval / readiness state from the
convention blocks. A passing gate means "the required thinking was
RECORDED", never "the thinking is true". The auditable structural test
`test_validator_source_sets_no_correctness_state` greps this file and
must find zero such state being set - keep it that way.
"""
from __future__ import annotations

# Stamped on EVERY result, unconditionally, and surfaced adjacent to any
# pass marker by consensus_get_iteration_outcome. It must never be
# possible to read "the gate passed" without also reading this.
GATE_SCOPE_DISCLAIMER = (
    "presence-and-consistency only; NOT a soundness assertion. A passing "
    "gate is not evidence the hypothesis is true or the safeguard adequate "
    "- only that the required thinking was recorded. 'Would this safeguard "
    "still work if the root cause were entirely different?' remains a human "
    "judgement."
)

CONVENTION_SCHEMA_VERSION = 1

# Operator-DECLARED risk classes that mandate a root-cause-independent
# safeguard. The engine does NOT infer the class from keywords (heuristics
# are the shared-prior trap the v1.15.0 report documents); it only enforces
# the consequence of a declared/attested value.
RISK_CLASS_REQUIRING_SAFEGUARD = {
    "safety",
    "safety-critical",
    "data-loss",
    "bricking",
    "irreversible",
    "irreversible-risk",
}

ENFORCEMENT_LEVELS = ("off", "warn", "graduated", "strict")


def _nonempty(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _safeguard_conforms(sg: dict) -> bool:
    """A conforming safeguard is decoupled from the hypothesis by design.

    Structural check only - this asks 'are the decoupling attestations
    present and set to the doctrine-required values', NOT 'is the
    safeguard actually adequate' (that is the human judgement named in
    GATE_SCOPE_DISCLAIMER).
    """
    if not isinstance(sg, dict):
        return False
    return (
        sg.get("applicable") is True
        and sg.get("works_if_root_cause_wrong") is True
        and sg.get("ships_with_fix") is True
        and _nonempty(sg.get("why"))
        and _nonempty(sg.get("mechanism"))
    )


def validate_convention(
    convention: dict,
    *,
    risk_class: str | None,
    enforcement: str,
) -> dict:
    """Validate the orchestrator-authored convention object.

    Returns a result dict whose keys are deliberately neutral:
      - presence_ok          : every required block is present + consistent
      - violations           : human-readable presence/consequence gaps
      - hard_reject          : iteration must NOT seal (per `enforcement`)
      - hard_reject_reasons  : the specific load-bearing reasons
      - gate_scope           : GATE_SCOPE_DISCLAIMER, unconditionally
      - convention_schema_version

    The result intentionally carries NO field asserting the hypothesis or
    safeguard is right. Enforcement levels: off | warn | graduated | strict.
    """
    if enforcement not in ENFORCEMENT_LEVELS:
        enforcement = "graduated"

    violations: list[str] = []
    hard_reasons: list[str] = []

    if not isinstance(convention, dict):
        violations.append("convention must be a mapping")
        return _finish(False, violations, hard_reasons, enforcement, None)

    schema_ver = convention.get("convention_schema_version")
    # codex-rev-003: a present convention must pin the schema version
    # exactly. Never default a present-but-unversioned/foreign-version
    # convention to the current version at seal time.
    if schema_ver != CONVENTION_SCHEMA_VERSION:
        violations.append(
            "convention_schema_version must be exactly "
            f"{CONVENTION_SCHEMA_VERSION} for a present convention "
            f"(got {schema_ver!r}) - not defaulted, not grandfathered"
        )

    # ---- falsification block ----
    fal = convention.get("falsification")
    if not isinstance(fal, dict):
        violations.append("falsification block missing or not a mapping")
        fal = {}

    hypothesis = fal.get("hypothesis")
    if not _nonempty(hypothesis):
        violations.append("falsification.hypothesis must be a non-empty sentence")

    ffa = fal.get("falsifiable_from_artifacts")
    if not isinstance(ffa, bool):
        violations.append("falsification.falsifiable_from_artifacts must be a bool")

    status = fal.get("empirical_status")
    if status not in {"proven", "pending", "refuted", "n/a"}:
        violations.append(
            "falsification.empirical_status must be one of "
            "proven|pending|refuted|n/a"
        )

    refute = fal.get("refutation_observation")
    if _nonempty(refute) and _nonempty(hypothesis) and (
        refute.strip() == hypothesis.strip()
    ):
        violations.append(
            "falsification.refutation_observation must be DISTINCT from the "
            "hypothesis, not an echo of it (anti-theater)"
        )

    # Defined-class consequence rule. The engine enforces only the
    # consequence of the attested bool; it does not classify.
    if ffa is False:
        if not _nonempty(fal.get("discriminating_experiment")):
            violations.append(
                "falsifiable_from_artifacts=false => discriminating_experiment "
                "must be non-empty"
            )
        if not _nonempty(refute):
            violations.append(
                "falsifiable_from_artifacts=false => refutation_observation "
                "must be non-empty"
            )
        if status in {"n/a"}:
            violations.append(
                "falsifiable_from_artifacts=false => empirical_status must be "
                "pending|refuted|proven (never n/a) for the defined class"
            )

    # proven requires a recorded experiment result (load-bearing reason ii).
    if status == "proven" and not _nonempty(fal.get("experiment_result")):
        msg = (
            "empirical_status=proven without a recorded "
            "falsification.experiment_result"
        )
        violations.append(msg)
        hard_reasons.append(msg)

    # ---- independent_safeguard block ----
    sg = convention.get("independent_safeguard")
    if not isinstance(sg, dict) or "applicable" not in sg:
        violations.append(
            "independent_safeguard block missing - the judgement must be "
            "RECORDED even when not applicable"
        )
        sg = {}
    elif sg.get("applicable") is False:
        if not _nonempty(sg.get("why")):
            violations.append(
                "independent_safeguard.applicable=false => why must record "
                "the reason the risk class is not triggered"
            )
    else:
        if not _safeguard_conforms(sg):
            violations.append(
                "independent_safeguard present but not decoupled: requires "
                "works_if_root_cause_wrong=true, ships_with_fix=true, "
                "non-empty mechanism and why"
            )

    # Risk-class consequence (load-bearing reason i): operator-DECLARED
    # safety/data-loss/bricking/irreversible class needs a conforming
    # (decoupled) safeguard shipping with the fix.
    if _norm(risk_class) in RISK_CLASS_REQUIRING_SAFEGUARD:
        if not _safeguard_conforms(sg):
            msg = (
                f"risk_class={risk_class!r} requires a conforming "
                "(root-cause-independent) independent_safeguard"
            )
            violations.append(msg)
            hard_reasons.append(msg)

    # ---- decisive_experiment_before_next_iteration (codex-rev-002) ----
    # The THIRD named block. The key must be present. null is legitimate
    # ONLY when empirical_status is proven or n/a (convention doc section 3);
    # otherwise it must name a non-empty experiment.
    if "decisive_experiment_before_next_iteration" not in convention:
        violations.append(
            "decisive_experiment_before_next_iteration is one of the three "
            "named convention blocks and must be present (value may be null "
            "only when empirical_status is proven or n/a)"
        )
    else:
        dexp = convention.get("decisive_experiment_before_next_iteration")
        if dexp is None:
            if status not in {"proven", "n/a"}:
                violations.append(
                    "decisive_experiment_before_next_iteration: null is "
                    "allowed only when empirical_status is proven or n/a; "
                    f"status={status!r} must carry a decisive experiment "
                    "forward"
                )
        elif not _nonempty(dexp):
            violations.append(
                "decisive_experiment_before_next_iteration must be a "
                "non-empty string or null"
            )

    # ---- provenance-by-citation ----
    cited = convention.get("cited_pass_ids")
    if not (isinstance(cited, list) and cited and all(_nonempty(c) for c in cited)):
        violations.append(
            "cited_pass_ids must be a non-empty list (provenance-by-citation: "
            "the convention is sealed INTO converged-plan.yaml, not a loose "
            "sidecar, not a single-winner extraction)"
        )

    presence_ok = not violations
    return _finish(presence_ok, violations, hard_reasons, enforcement, schema_ver)


def _finish(
    presence_ok: bool,
    violations: list[str],
    hard_reasons: list[str],
    enforcement: str,
    schema_ver,
) -> dict:
    if enforcement == "off":
        # codex-rev-001 (pass-3): `off` disables BLOCKING, not VISIBILITY.
        # Fabricating presence_ok=True / empty violations would make
        # disabled enforcement masquerade as a clean structural pass -
        # exactly the recursive-trap this whole iteration defends against.
        # Preserve the REAL presence_ok + violations; only suppress
        # hard-reject; stamp an explicit disabled status.
        return {
            "presence_ok": presence_ok,
            "violations": violations,
            "hard_reject": False,
            "hard_reject_reasons": [],
            "enforcement_disabled": True,
            "gate_scope": GATE_SCOPE_DISCLAIMER,
            "enforcement": enforcement,
            "convention_schema_version": schema_ver,
        }

    if enforcement == "warn":
        hard = False
    elif enforcement == "strict":
        hard = bool(violations)
    else:  # graduated (default): only the two load-bearing reasons block
        hard = bool(hard_reasons)

    return {
        "presence_ok": presence_ok,
        "violations": violations,
        "hard_reject": hard,
        "hard_reject_reasons": hard_reasons,
        "gate_scope": GATE_SCOPE_DISCLAIMER,
        "enforcement": enforcement,
        "convention_schema_version": schema_ver,
    }
