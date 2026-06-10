"""architect.loop_step - supervisor state machine for workflow D.

Mirrors loop_run_goal: filesystem-inspect, advance ONE step, seal, return
next_action. Auto-runs ONLY the builder dispatch + the verification command;
architect and reviewer actions return next_action for the orchestrating
host. Never calls an LLM API. See docs/workflows/architect-build.md.
"""
from __future__ import annotations

import datetime as _dt
import os
import subprocess
from pathlib import Path

import consensus_mcp.config as cfg
from consensus_mcp import _architect_lane as lane_mod
from consensus_mcp import _architect_paths as ap
from consensus_mcp import _dispatch_builder as _db
from consensus_mcp._architect_handoff import write_handoff

# Indirection point so tests monkeypatch the supervisor's view of the
# builder dispatch without touching _dispatch_builder itself.
_dispatch_builder_fn = _db.dispatch_builder

IN_FLIGHT_TTL_SECONDS = int(
    os.environ.get("CONSENSUS_MCP_ARCHITECT_IN_FLIGHT_TTL", "3600")
)

SCHEMA = {
    "name": "architect.loop_step",
    "description": (
        "Supervisor for the architect-build loop (workflow D). Detects goal "
        "state from the filesystem, advances one mechanical step (builder "
        "dispatch / verification), seals artifacts, regenerates HANDOFF.md, "
        "and returns next_action for the orchestrating host."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "goal_dir": {"type": "string"},
            "config_path": {"type": ["string", "null"]},
            "auto_dispatch": {"type": ["boolean", "null"]},
        },
        "required": ["goal_dir"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "state": {"type": "string"},
            "next_action": {"type": "string"},
            "cycle": {"type": ["integer", "null"]},
            "actions_taken": {"type": "array"},
            "stop_rules_fired": {"type": "array"},
            "error": {"type": ["string", "null"]},
        },
        "required": ["ok", "state", "next_action"],
        "additionalProperties": False,
    },
}

_NEXT_ACTION = {
    "goal_invalid": "fix the goal dir / config and re-run loop_step",
    "closed": "goal is closed; nothing to do",
    "killed": "architect killed the goal; lane retained for forensics",
    "blocked_stop_rule": "a stop rule fired; operator decision required",
    "blocked_base_drift": (
        "main HEAD moved past the approved base_sha; operator decides: "
        "rebase the lane, restart the goal, or accept the risk explicitly"
    ),
    "dispatch_in_flight": "a dispatch is running; call loop_step again later",
    "needs_spec": (
        "ARCHITECT action: author the spec and seal it to <goal>/spec.yaml "
        "via _architect_paths.seal_artifact (host callback when architect="
        "claude; otherwise dispatch the architect CLI with "
        "architect_spec_template.md)"
    ),
    "awaiting_spec_approval": (
        "HUMAN gate: run architect.approve_spec (consensus-mcp-architect "
        "approve-spec --goal-dir <goal> --approver <you>)"
    ),
    "pushback_raised": (
        "ARCHITECT action: rule on the builder pushback - seal a ruling "
        "(disposition revise|overrule) to the current cycle dir; a spec "
        "revision goes to spec-rev-N.yaml (the human gate does NOT re-fire)"
    ),
    "needs_build": (
        "builder dispatch pending: re-run loop_step with auto_dispatch "
        "(default) or dispatch the builder manually and seal "
        "build-result.yaml"
    ),
    "cycle_advance": (
        "a revise ruling closed this cycle; call loop_step again to start "
        "the next cycle's build"
    ),
    "built": "builder ran and the lane committed; call loop_step again",
    "needs_verification": "call loop_step again to run the frozen gate",
    "verification_red": (
        "frozen gate RED: a mechanical revise ruling was sealed; call "
        "loop_step again to start the next cycle"
    ),
    "needs_review": (
        "REVIEWER action: review the lane diff and seal review.yaml "
        "{verdict, lane_head_sha} into the current cycle dir (dispatch the "
        "reviewer CLI read-only against the diff)"
    ),
    "needs_ruling": (
        "ARCHITECT action: read HANDOFF.md + the cycle review and seal "
        "ruling.yaml {disposition: accept|revise|kill, lane_head_sha, "
        "reason?} into the current cycle dir"
    ),
    "awaiting_delivery_approval": (
        "HUMAN gate: delivery approval, then merge the lane branch; the "
        "supervisor never merges"
    ),
}


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_utc(s: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _result(state: str, *, cycle: int | None = None, actions=None,
            stops=None, error: str | None = None, ok: bool = True) -> dict:
    return {
        "ok": ok, "state": state,
        "next_action": _NEXT_ACTION.get(state, ""),
        "cycle": cycle, "actions_taken": actions or [],
        "stop_rules_fired": stops or [], "error": error,
    }


def _load_config(goal: Path, config_path: str | None):
    if config_path:
        return cfg.load(config_path)
    root = goal.parent.parent.parent
    return cfg.load(root / ".consensus" / "config.yaml")


def _check_stop_rules(goal: Path, config: dict, cycle: int) -> list[dict]:
    stops: list[dict] = []
    loop = config.get("architect_loop", {})
    max_cycles = loop.get("max_cycles", 8)
    if cycle > max_cycles:
        stops.append({"rule": "max_cycle_count_reached",
                      "cycle": cycle, "max": max_cycles})
    inflight = ap._read_yaml_or_empty(goal / ap.IN_FLIGHT_FILENAME)
    if inflight:
        started = _parse_utc(inflight.get("started_at_utc", ""))
        if started is None or (
            (_utcnow() - started).total_seconds() > IN_FLIGHT_TTL_SECONDS
        ):
            stops.append({"rule": "stale_dispatch_in_flight",
                          "started_at_utc": inflight.get("started_at_utc")})
    breach = ap._read_yaml_or_empty(goal / "containment-breach.yaml")
    if breach:
        stops.append({"rule": breach.get("rule", "builder_containment_breach"),
                      "violations": breach.get("violations", [])})
    # repeated RED with identical signature across the last 3 cycles
    sigs = []
    for n in range(max(1, cycle - 2), cycle + 1):
        v = ap._read_yaml_or_empty(ap.cycle_dir(goal, n) / ap.VERIFICATION_FILENAME)
        if v and not v.get("passed"):
            sigs.append(v.get("signature"))
    if len(sigs) >= 3 and len(set(sigs)) == 1 and sigs[0]:
        stops.append({"rule": "repeated_verification_failure_same_signature",
                      "signature": sigs[0]})
    wall = loop.get("max_wall_clock_minutes", 0)
    if wall:
        approval = ap._read_yaml_or_empty(goal / ap.SPEC_APPROVAL_FILENAME)
        t0 = _parse_utc(approval.get("sealed_at_utc", ""))
        if t0 and (_utcnow() - t0).total_seconds() > wall * 60:
            stops.append({"rule": "wall_clock_budget_exceeded",
                          "max_minutes": wall})
    # cross_document_drift: HANDOFF claims a spec sha that does not match
    # the latest sealed spec EVEN THOUGH HANDOFF was written after that
    # spec seal. An OLDER HANDOFF is just pending regeneration (e.g. the
    # host sealed spec-rev-N a moment ago) - that is not drift. A NEWER
    # HANDOFF with the wrong sha means tampering or a renderer bug: stop.
    handoff_file = goal / ap.HANDOFF_FILENAME
    spec_file = ap.latest_spec_path(goal)
    if handoff_file.exists() and spec_file.exists():
        try:
            handoff_newer = (
                handoff_file.stat().st_mtime_ns >= spec_file.stat().st_mtime_ns
            )
            text = handoff_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            handoff_newer, text = False, ""
        if handoff_newer:
            sha = ap._read_yaml_or_empty(spec_file).get("payload_sha256")
            for line in text.splitlines():
                if line.startswith("spec payload_sha256:"):
                    recorded = line.split(":", 1)[1].strip()
                    if sha and recorded not in (sha, "UNSEALED"):
                        stops.append({"rule": "cross_document_drift",
                                      "handoff_spec_sha": recorded,
                                      "sealed_spec_sha": sha})
                    break
    return stops


def _signer_violations(goal: Path, cycle: int, roles: dict) -> list[str]:
    """GateEligibleCrossFamilySigner (consult Q2): cross-family + hash
    binding + freshness. Families are contributor names for the builtin set
    (profile-aware family equivalence was enforced at config time)."""
    c = ap.cycle_dir(goal, cycle)
    build = ap._read_yaml_or_empty(c / ap.BUILD_RESULT_FILENAME)
    review = ap._read_yaml_or_empty(c / ap.REVIEW_FILENAME)
    ruling = ap._read_yaml_or_empty(c / ap.RULING_FILENAME)
    builder = roles.get("builder", "")
    violations: list[str] = []
    signer_name, signer = (
        (roles.get("reviewer", ""), review)
        if roles.get("reviewer", "") != builder
        else (roles.get("architect", ""), ruling)
    )
    if signer_name == builder:
        violations.append("no cross-family signer available")
    lane_sha = build.get("lane_head_sha")
    if not lane_sha or signer.get("lane_head_sha") != lane_sha:
        violations.append(
            f"hash binding failed: signer binds "
            f"{signer.get('lane_head_sha')!r}, build is {lane_sha!r}"
        )
    b_t = _parse_utc(build.get("sealed_at_utc", ""))
    s_t = _parse_utc(signer.get("sealed_at_utc", ""))
    if not b_t or not s_t or s_t < b_t:
        violations.append("freshness failed: signer predates the build seal")
    return violations


def _run_build(goal: Path, config: dict, cycle: int, root: Path) -> dict:
    roles = config["roles"]
    loop = config["architect_loop"]
    approval = ap._read_yaml_or_empty(goal / ap.SPEC_APPROVAL_FILENAME)
    branch = f"{loop['lane_branch_prefix'].rstrip('/')}/{goal.name}".replace(
        "//", "/"
    )
    lane = lane_mod.create_lane(root, goal, branch, approval["base_sha"])
    before = lane_mod.snapshot_main_integrity(root)
    ap.seal_artifact(goal / ap.INTEGRITY_BEFORE_FILENAME, before)

    spec = ap._read_yaml_or_empty(ap.latest_spec_path(goal))
    feedback = ""
    if cycle > 1:
        prev = ap._read_yaml_or_empty(
            ap.cycle_dir(goal, cycle - 1) / ap.RULING_FILENAME
        )
        feedback = f"{prev.get('reason', '')}\n{prev.get('feedback', '')}".strip()
    prompt = _db.build_prompt(str(spec.get("body", "")), feedback)

    ap.seal_artifact(
        goal / ap.IN_FLIGHT_FILENAME,
        {"role": "builder", "cycle": cycle,
         "started_at_utc": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
    )
    try:
        result = _dispatch_builder_fn(
            repo_root=root, lane=lane, prompt=prompt,
            timeout_seconds=1800,
        )
    finally:
        try:
            (goal / ap.IN_FLIGHT_FILENAME).unlink()
        except OSError:
            pass

    lane_violations = lane_mod.scan_lane_integrity(lane)
    if lane_violations:
        ap.seal_artifact(goal / "containment-breach.yaml",
                         {"rule": "lane_integrity_violation",
                          "violations": lane_violations})
        return _result("blocked_stop_rule", cycle=cycle,
                       stops=[{"rule": "lane_integrity_violation",
                               "violations": lane_violations}])
    head = lane_mod.commit_lane(root, lane, f"builder cycle {cycle}: "
                                            f"{result['summary'][:60]}")
    main_violations = lane_mod.check_main_integrity(
        root, before, lane_branch=branch
    )
    if main_violations:
        ap.seal_artifact(goal / "containment-breach.yaml",
                         {"rule": "builder_containment_breach",
                          "violations": main_violations})
        return _result("blocked_stop_rule", cycle=cycle,
                       stops=[{"rule": "builder_containment_breach",
                               "violations": main_violations}])
    cdir = ap.cycle_dir(goal, cycle)
    cdir.mkdir(parents=True, exist_ok=True)
    ap.seal_artifact(
        cdir / ap.BUILD_RESULT_FILENAME,
        {"summary": result["summary"], "pushback": result["pushback"],
         "notes": result["notes"], "lane_head_sha": head, "cycle": cycle},
    )
    write_handoff(goal, roles=roles)
    return _result("built", cycle=cycle,
                   actions=[{"action": "builder_dispatched", "lane_head_sha": head}])


def _run_verification(goal: Path, config: dict, cycle: int, root: Path) -> dict:
    import hashlib
    cmd = config["architect_loop"].get("verification", "")
    lane = ap.lane_dir(goal)
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(lane), capture_output=True,
            text=True, timeout=1800,
        )
        passed = proc.returncode == 0
        tail = (proc.stdout + proc.stderr)[-2000:]
    except (OSError, subprocess.SubprocessError) as exc:
        passed, tail = False, f"verification command failed to run: {exc}"
    cdir = ap.cycle_dir(goal, cycle)
    cdir.mkdir(parents=True, exist_ok=True)
    ap.seal_artifact(
        cdir / ap.VERIFICATION_FILENAME,
        {"command": cmd, "passed": passed,
         "signature": hashlib.sha256(tail.encode("utf-8")).hexdigest(),
         "output_tail": tail},
    )
    if passed:
        write_handoff(goal, roles=config["roles"])
        return _result("needs_review", cycle=cycle,
                       actions=[{"action": "verification_green"}])
    # consult Q3: mechanical revise ruling, regular artifact shape
    ap.seal_artifact(
        cdir / ap.RULING_FILENAME,
        {"disposition": "revise", "reason": "verification_failed",
         "mechanical": True, "feedback": tail},
    )
    write_handoff(goal, roles=config["roles"])
    return _result("verification_red", cycle=cycle,
                   actions=[{"action": "mechanical_revise_sealed"}])


def handle(goal_dir: str, config_path: str | None = None,
           auto_dispatch: bool | None = None) -> dict:
    goal = Path(goal_dir)
    do_dispatch = True if auto_dispatch is None else bool(auto_dispatch)
    try:
        config = _load_config(goal, config_path)
    except Exception as exc:  # noqa: BLE001 - tool boundary: handle() never raises
        return _result("goal_invalid", ok=False,
                       error=f"config load failed: {exc}")
    if config["workflow"]["mode"] != cfg.WORKFLOW_ARCHITECT_BUILD:
        return _result("goal_invalid", ok=False,
                       error=f"workflow.mode is {config['workflow']['mode']!r}, "
                             f"not architect-build")
    if not (goal / ap.PROBLEM_FILENAME).exists():
        return _result("goal_invalid", ok=False,
                       error=f"no {ap.PROBLEM_FILENAME} in {goal}")
    root = goal.parent.parent.parent
    roles = config["roles"]

    outcome = ap._read_yaml_or_empty(goal / ap.OUTCOME_FILENAME)
    if outcome.get("closing_state"):
        state = (
            "killed"
            if outcome["closing_state"] == ap.KILLED_CLOSING_STATE
            else "closed"
        )
        return _result(state)

    cycle = ap.current_cycle(goal)
    stops = _check_stop_rules(goal, config, cycle)
    if stops:
        return _result("blocked_stop_rule", cycle=cycle, stops=stops)
    if ap._read_yaml_or_empty(goal / ap.IN_FLIGHT_FILENAME):
        return _result("dispatch_in_flight", cycle=cycle)

    spec = ap._read_yaml_or_empty(ap.latest_spec_path(goal))
    if not spec.get("payload_sha256"):
        return _result("needs_spec", cycle=cycle)
    approval = ap._read_yaml_or_empty(goal / ap.SPEC_APPROVAL_FILENAME)
    if not approval:
        return _result("awaiting_spec_approval", cycle=cycle)

    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root), check=True,
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return _result("goal_invalid", ok=False,
                       error=f"cannot read main HEAD: {exc}")
    if head != approval.get("base_sha"):
        return _result("blocked_base_drift", cycle=cycle)

    cdir = ap.cycle_dir(goal, cycle)
    build = ap._read_yaml_or_empty(cdir / ap.BUILD_RESULT_FILENAME)
    ruling = ap._read_yaml_or_empty(cdir / ap.RULING_FILENAME)
    if build.get("pushback") and not ruling:
        return _result("pushback_raised", cycle=cycle)
    if not build:
        if not do_dispatch:
            return _result("needs_build", cycle=cycle)
        try:
            return _run_build(goal, config, cycle, root)
        except (lane_mod.LaneError, _db.BuilderDispatchError) as exc:
            return _result("blocked_stop_rule", cycle=cycle, ok=False,
                           stops=[{"rule": "builder_dispatch_failed",
                                   "detail": str(exc)}],
                           error=str(exc))
    verification = ap._read_yaml_or_empty(cdir / ap.VERIFICATION_FILENAME)
    needs_gate = bool(config["architect_loop"].get("verification", "")) and not build.get("pushback")
    if needs_gate and not verification:
        return _run_verification(goal, config, cycle, root)
    review = ap._read_yaml_or_empty(cdir / ap.REVIEW_FILENAME)
    if not review and not ruling:
        return _result("needs_review", cycle=cycle)
    if not ruling:
        return _result("needs_ruling", cycle=cycle)
    disposition = ruling.get("disposition")
    if disposition == "revise":
        # current_cycle() advances past a revise-closed cycle, so this
        # branch is normally unreachable; defensive total-cascade fallback.
        return _result("cycle_advance", cycle=cycle)  # pragma: no cover
    if disposition == "kill":
        ap.seal_artifact(goal / ap.OUTCOME_FILENAME,
                         {"closing_state": ap.KILLED_CLOSING_STATE,
                          "cycle": cycle, "reason": ruling.get("reason", "")})
        write_handoff(goal, roles=roles)
        return _result("killed", cycle=cycle)
    if disposition == "accept":
        violations = _signer_violations(goal, cycle, roles)
        if violations:
            return _result("blocked_stop_rule", cycle=cycle,
                           stops=[{"rule": "signer_invariant_violated",
                                   "violations": violations}])
        write_handoff(goal, roles=roles)
        return _result("awaiting_delivery_approval", cycle=cycle)
    return _result("needs_ruling", cycle=cycle)


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
