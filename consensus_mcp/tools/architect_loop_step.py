"""architect.loop_step - supervisor state machine for workflow D.

Mirrors loop_run_goal: filesystem-inspect, advance ONE step, seal, return
next_action. Auto-runs ONLY the builder dispatch + the verification command;
architect and reviewer actions return next_action for the orchestrating
host. Never calls an LLM API. See docs/workflows/architect-build.md.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import subprocess
from pathlib import Path

import consensus_mcp.config as cfg
from consensus_mcp import _architect_lane as lane_mod
from consensus_mcp import _architect_paths as ap
from consensus_mcp import _contributor_profiles as _profiles
from consensus_mcp import _dispatch_builder as _db
from consensus_mcp._architect_handoff import write_handoff
from consensus_mcp._dispatch_base import (
    CODEX_SCRUBBED_ENV_KEYS,
    GEMINI_SCRUBBED_ENV_KEYS,
    GROK_SCRUBBED_ENV_KEYS,
    KIMI_SCRUBBED_ENV_KEYS,
    _terminate_process_tree,
    scrub_env_keys,
)

# Indirection point so tests monkeypatch the supervisor's view of the
# builder dispatch without touching _dispatch_builder itself.
_dispatch_builder_fn = _db.dispatch_builder

IN_FLIGHT_TTL_SECONDS = int(
    os.environ.get("CONSENSUS_MCP_ARCHITECT_IN_FLIGHT_TTL", "3600")
)

# Operator-overridable frozen-gate ceiling (also the unit tests' injection
# point for the timeout path).
_VERIFICATION_TIMEOUT_SECONDS = int(
    os.environ.get("CONSENSUS_MCP_VERIFICATION_TIMEOUT_SECONDS", "1800")
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
        "main HEAD moved past the approved base_sha and the supervisor has "
        "no drift override (the approval binds the exact base): restart the "
        "goal from the new HEAD, or take over manually - inspect the lane "
        "branch and rebase/merge it yourself outside the loop"
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
        "a revise/overrule ruling closed this cycle; call loop_step again "
        "to start the next cycle's build"
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


def _lane_branch_name(loop: dict, goal: Path) -> str:
    """The ONE spelling of the lane branch (shared by the build dispatch and
    the delivery-gate integrity re-check, so the lane-ref exemption can
    never drift between the two sites)."""
    return f"{loop['lane_branch_prefix'].rstrip('/')}/{goal.name}".replace(
        "//", "/"
    )


# Volatile tokens normalized out of the verification output before hashing:
# the repeated-RED stop rule keys on signature EQUALITY across cycles, and a
# raw stdout+stderr hash is unreachable for the flagship 'pytest -q' command
# (its tail ends with the wall-clock '... failed in 1.23s' line, different
# every run). Spec section 4 wants a STABLE failure signature.
_SIGNATURE_VOLATILE_RES = (
    # memory addresses / object ids: '<Foo object at 0x7f8b...>'
    re.compile(r"0[xX][0-9A-Fa-f]+"),
    # wall-clock durations: pytest '1 failed in 1.23s', unittest 'in 0.001s'
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:s|ms|us|ns|secs?|seconds?|mins?|minutes?)\b"),
    # clock times: '12:34:56.789'
    re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b"),
)


def _verification_signature(tail: str) -> str:
    norm = tail
    for rx in _SIGNATURE_VOLATILE_RES:
        norm = rx.sub("<volatile>", norm)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _seal_mechanical_revise(cdir: Path, tail: str) -> None:
    """consult Q3: the RED-gate mechanical revise ruling, regular artifact
    shape. ONE writer for both the in-step seal and the resume re-seal so
    the two can never diverge."""
    cdir.mkdir(parents=True, exist_ok=True)
    ap.seal_artifact(
        cdir / ap.RULING_FILENAME,
        {"disposition": "revise", "reason": "verification_failed",
         "mechanical": True, "feedback": tail},
    )


def _derive_root(goal: Path) -> Path | None:
    """The VALIDATED inversion of the L1 layout, mirroring
    architect_gates._repo_root: never trust blind parent-hopping - a
    mis-shaped goal_dir would anchor git at a garbage root and rev-parse
    would walk UP to whatever repo encloses it. loop_step runs rev-parse
    AND supervisor-owned commits against this root, so it is the
    highest-stakes caller of the derivation."""
    return lane_mod._derive_repo_root(ap.lane_dir(goal))


def _load_config(root: Path, config_path: str | None):
    if config_path:
        return cfg.load(config_path)
    return cfg.load(root / ".consensus" / "config.yaml")


def _merged_profiles(config: dict) -> dict:
    """Builtin profiles overlaid with the config's contributors.profiles -
    the same merge config._validate_architect_build and render_handoff use,
    so family resolution can never drift between the three sites."""
    return _profiles.merge_profiles(
        _profiles.load_builtin_profiles(),
        config.get("contributors", {}).get("profiles", {}) or {},
    )


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
    breach = ap._read_yaml_or_empty(goal / ap.CONTAINMENT_BREACH_FILENAME)
    if breach:
        stops.append({"rule": breach.get("rule", "builder_containment_breach"),
                      "violations": breach.get("violations", [])})
    # repeated RED with identical signature across the last 3 CLOSED
    # cycles. `cycle` is the next OPEN cycle: a RED verification seals a
    # mechanical revise in the SAME step, which closes its cycle and
    # advances current_cycle before this rule ever runs - so the open
    # slot's verification.yaml never exists and including it (cycle + 1
    # upper bound) would leave at most 2 scannable signatures, making the
    # rule unreachable.
    sigs = []
    for n in range(max(1, cycle - 3), cycle):
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
    # STRICTLY newer: on coarse-timestamp filesystems (FAT 2s, ext3 1s) a
    # HANDOFF legitimately written moments before the spec seal can TIE the
    # spec mtime, and a tie cannot distinguish stale-pending-regeneration
    # from tamper - so it fails open, per the pending-regeneration rationale.
    handoff_file = goal / ap.HANDOFF_FILENAME
    spec_file = ap.latest_spec_path(goal)
    if handoff_file.exists() and spec_file.exists():
        try:
            handoff_newer = (
                handoff_file.stat().st_mtime_ns > spec_file.stat().st_mtime_ns
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


def _signer_violations(goal: Path, cycle: int, roles: dict,
                       profiles: dict) -> list[str]:
    """GateEligibleCrossFamilySigner (consult Q2): cross-family + hash
    binding + freshness. Families resolve via cfg._contributor_family over
    the MERGED profiles, never by contributor name: config validation
    guarantees at least one cross-family role EXISTS, NOT that
    name-inequality implies family-inequality - a family: overlay can give
    a differently-named reviewer the builder's family, in which case the
    architect's ruling is the only true cross-family attestation and must
    be the hash-bound, freshness-checked signer."""
    c = ap.cycle_dir(goal, cycle)
    build = ap._read_yaml_or_empty(c / ap.BUILD_RESULT_FILENAME)
    review = ap._read_yaml_or_empty(c / ap.REVIEW_FILENAME)
    ruling = ap._read_yaml_or_empty(c / ap.RULING_FILENAME)
    builder_fam = cfg._contributor_family(roles.get("builder", ""), profiles)
    reviewer_fam = cfg._contributor_family(roles.get("reviewer", ""), profiles)
    architect_fam = cfg._contributor_family(roles.get("architect", ""), profiles)
    violations: list[str] = []
    lane_sha = build.get("lane_head_sha")
    b_t = _parse_utc(build.get("sealed_at_utc", ""))
    # The reviewer is REQUIRED in v1 (consult Q4) - and that is a RUNTIME
    # invariant, not just a config-time one: in the canonical cheap config
    # (reviewer shares the builder's family) the architect's ruling is the
    # cross-family signer below, which would otherwise let an accept with
    # NO sealed review reach the delivery gate. A fresh, hash-bound
    # review.yaml must exist before any accept can deliver.
    if not review:
        violations.append(
            "review.yaml missing: the v1-required reviewer has not "
            "reviewed this cycle"
        )
    else:
        if not lane_sha or review.get("lane_head_sha") != lane_sha:
            violations.append(
                f"review hash binding failed: review binds "
                f"{review.get('lane_head_sha')!r}, build is {lane_sha!r}"
            )
        r_t = _parse_utc(review.get("sealed_at_utc", ""))
        if not b_t or not r_t or r_t < b_t:
            violations.append(
                "review freshness failed: review predates the build seal"
            )
    signer_fam, signer = (
        (reviewer_fam, review)
        if reviewer_fam != builder_fam
        else (architect_fam, ruling)
    )
    if signer_fam == builder_fam:
        violations.append("no cross-family signer available")
    if not lane_sha or signer.get("lane_head_sha") != lane_sha:
        violations.append(
            f"hash binding failed: signer binds "
            f"{signer.get('lane_head_sha')!r}, build is {lane_sha!r}"
        )
    s_t = _parse_utc(signer.get("sealed_at_utc", ""))
    if not b_t or not s_t or s_t < b_t:
        violations.append("freshness failed: signer predates the build seal")
    return violations


def _run_build(goal: Path, config: dict, cycle: int, root: Path,
               profiles: dict) -> dict:
    roles = config["roles"]
    loop = config["architect_loop"]
    # The spec drives the builder at POINT OF USE, so its seal is verified
    # here, not only at the approval gate: spec-rev-N.yaml is legitimately
    # ungated between human gates, and a body edited after sealing
    # (payload_sha256 no longer reproduces) must never reach the prompt.
    spec_file = ap.latest_spec_path(goal)
    spec = ap._read_yaml_or_empty(spec_file)
    if not ap.seal_is_intact(spec):
        return _result(
            "blocked_stop_rule", cycle=cycle,
            stops=[{"rule": "spec_seal_invalid",
                    "detail": (
                        f"{spec_file.name}: payload_sha256 does not "
                        f"reproduce - refusing to build from a tampered spec"
                    )}])
    approval = ap._read_yaml_or_empty(goal / ap.SPEC_APPROVAL_FILENAME)
    branch = _lane_branch_name(loop, goal)
    # TEST-AND-SET the in-flight lock BEFORE any lane work. The read check
    # in handle() is only the polite wait state; this O_EXCL create is the
    # actual mutex (spec section 4: 'never double-dispatches') - two
    # concurrent loop_steps must never both send a write-enabled builder
    # into the SAME lane worktree, and seal_artifact's os.replace would
    # silently clobber the winner's lock.
    try:
        acquired = ap.acquire_lock_artifact(
            goal / ap.IN_FLIGHT_FILENAME,
            {"role": "builder", "cycle": cycle,
             "started_at_utc": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
    except OSError as exc:
        raise lane_mod.LaneError(
            f"in-flight lock creation failed: {exc}"
        ) from exc
    if acquired is None:
        return _result("dispatch_in_flight", cycle=cycle)
    goal_violations: list[str] = []
    try:
        lane = lane_mod.create_lane(root, goal, branch, approval["base_sha"])
        before = lane_mod.snapshot_main_integrity(root)
        ap.seal_artifact(goal / ap.INTEGRITY_BEFORE_FILENAME, before)

        feedback = ""
        if cycle > 1:
            prev = ap._read_yaml_or_empty(
                ap.cycle_dir(goal, cycle - 1) / ap.RULING_FILENAME
            )
            feedback = f"{prev.get('reason', '')}\n{prev.get('feedback', '')}".strip()
        prompt = _db.build_prompt(str(spec.get("body", "")), feedback)
        # Goal-artifact snapshot/check pair around the dispatch, mirroring
        # the verification window (L5 doctrine, root-cause-independent):
        # the main-integrity status view excludes the whole architect tree
        # (and consensus-init gitignores .consensus/), so WITHOUT this pair
        # a builder escaping the lane could forge THIS goal's seals (spec,
        # approval, cycle review/ruling - content hashes, not authenticity
        # signatures) invisibly. Every supervisor write happens OUTSIDE the
        # bracket, so there is ZERO expected delta - no exemptions.
        goal_before = lane_mod.snapshot_goal_artifacts(goal)
        try:
            result = _dispatch_builder_fn(
                repo_root=root, lane=lane, prompt=prompt,
                timeout_seconds=1800,
            )
        finally:
            # Runs on the failure paths too: a builder that tampers AND
            # then times out / crashes must still seal the breach so the
            # next step blocks persistently.
            goal_violations = lane_mod.check_goal_artifacts(goal, goal_before)
            if goal_violations:
                ap.seal_artifact(goal / ap.CONTAINMENT_BREACH_FILENAME,
                                 {"rule": "builder_containment_breach",
                                  "violations": goal_violations})
    finally:
        try:
            (goal / ap.IN_FLIGHT_FILENAME).unlink()
        except OSError:
            pass
    if goal_violations:
        return _result("blocked_stop_rule", cycle=cycle,
                       stops=[{"rule": "builder_containment_breach",
                               "violations": goal_violations}])

    lane_violations = lane_mod.scan_lane_integrity(lane)
    if lane_violations:
        ap.seal_artifact(goal / ap.CONTAINMENT_BREACH_FILENAME,
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
        ap.seal_artifact(goal / ap.CONTAINMENT_BREACH_FILENAME,
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
    write_handoff(goal, roles=roles, profiles=profiles)
    return _result("built", cycle=cycle,
                   actions=[{"action": "builder_dispatched", "lane_head_sha": head}])


# The frozen gate executes builder-authored lane code (conftest.py, test
# modules), so it must not inherit the supervisor's AI-provider credentials:
# the L5 snapshot catches filesystem deltas but cannot catch network
# exfiltration of secrets read from env. Scrub the union of every
# dispatcher's key set (the builder dispatch itself scrubs the codex set).
_VERIFICATION_SCRUBBED_ENV_KEYS = tuple(dict.fromkeys(
    CODEX_SCRUBBED_ENV_KEYS + GEMINI_SCRUBBED_ENV_KEYS
    + GROK_SCRUBBED_ENV_KEYS + KIMI_SCRUBBED_ENV_KEYS
))


def _run_verification(goal: Path, config: dict, cycle: int, root: Path,
                      profiles: dict) -> dict:
    cmd = config["architect_loop"].get("verification", "")
    lane = ap.lane_dir(goal)
    # The frozen gate executes builder-authored lane content UNSANDBOXED
    # (operator command, shell=True, cwd=lane) - the one window where lane
    # code could forge the supervisor-owned cycle artifacts (review.yaml /
    # ruling.yaml are content-hash seals, not authenticity signatures) or
    # escape into the main tree. Sandboxing an arbitrary operator shell
    # command is not portable ('init platform consistency'), so apply the
    # root-cause-independent L5 doctrine instead: snapshot the goal
    # artifacts + main-repo integrity before the run, re-check after, and
    # treat ANY delta as a containment breach (no lane_branch exemption -
    # verification must not commit). Residual: a background process that
    # outlives the command and writes later evades the window; the human
    # delivery gate remains the backstop for that.
    #
    # Same O_EXCL test-and-set lock as the builder dispatch: the gate is a
    # long-running lane subprocess, and a concurrent loop_step would
    # double-run the command AND seal goal artifacts inside this window
    # (false verification_containment_breach).
    try:
        acquired = ap.acquire_lock_artifact(
            goal / ap.IN_FLIGHT_FILENAME,
            {"role": "verification", "cycle": cycle,
             "started_at_utc": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
    except OSError as exc:
        raise lane_mod.LaneError(
            f"in-flight lock creation failed: {exc}"
        ) from exc
    if acquired is None:
        return _result("dispatch_in_flight", cycle=cycle)
    try:
        return _run_verification_locked(goal, config, cycle, root, profiles,
                                        cmd, lane)
    finally:
        try:
            (goal / ap.IN_FLIGHT_FILENAME).unlink()
        except OSError:
            pass


def _run_verification_locked(goal: Path, config: dict, cycle: int,
                             root: Path, profiles: dict, cmd: str,
                             lane: Path) -> dict:
    main_before = lane_mod.snapshot_main_integrity(root)
    goal_before = lane_mod.snapshot_goal_artifacts(goal)
    # Process-GROUP spawn + tree termination (mirrors _dispatch_builder):
    # a bare subprocess.run timeout kills only the direct shell, leaving
    # descendants (pytest workers etc.) WRITING in the lane while the
    # integrity re-check below runs - a TOCTOU on the very safeguard this
    # function enforces. utf-8/replace decoding matches _architect_lane._git
    # ('init platform consistency': no cp1252-dependent UnicodeDecodeError
    # through the never-raises tool boundary).
    try:
        if os.name == "nt":
            popen_kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            popen_kwargs = {"start_new_session": True}
        proc = subprocess.Popen(
            cmd, shell=True, cwd=str(lane),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env=scrub_env_keys(
                os.environ.copy(), _VERIFICATION_SCRUBBED_ENV_KEYS
            ),
            **popen_kwargs,
        )
        try:
            out, err = proc.communicate(timeout=_VERIFICATION_TIMEOUT_SECONDS)
            passed = proc.returncode == 0
            tail = ((out or "") + (err or ""))[-2000:]
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc)
            passed = False
            tail = (
                f"verification timed out after "
                f"{_VERIFICATION_TIMEOUT_SECONDS}s; process tree terminated"
            )
    except (OSError, subprocess.SubprocessError) as exc:
        passed, tail = False, f"verification command failed to run: {exc}"
    violations = lane_mod.check_main_integrity(root, main_before)
    violations += lane_mod.check_goal_artifacts(goal, goal_before)
    if violations:
        ap.seal_artifact(goal / ap.CONTAINMENT_BREACH_FILENAME,
                         {"rule": "verification_containment_breach",
                          "violations": violations})
        return _result("blocked_stop_rule", cycle=cycle,
                       stops=[{"rule": "verification_containment_breach",
                               "violations": violations}])
    cdir = ap.cycle_dir(goal, cycle)
    cdir.mkdir(parents=True, exist_ok=True)
    ap.seal_artifact(
        cdir / ap.VERIFICATION_FILENAME,
        {"command": cmd, "passed": passed,
         "signature": _verification_signature(tail),
         "output_tail": tail},
    )
    if passed:
        write_handoff(goal, roles=config["roles"], profiles=profiles)
        return _result("needs_review", cycle=cycle,
                       actions=[{"action": "verification_green"}])
    # consult Q3: mechanical revise ruling, regular artifact shape
    _seal_mechanical_revise(cdir, tail)
    write_handoff(goal, roles=config["roles"], profiles=profiles)
    return _result("verification_red", cycle=cycle,
                   actions=[{"action": "mechanical_revise_sealed"}])


def handle(goal_dir: str, config_path: str | None = None,
           auto_dispatch: bool | None = None) -> dict:
    goal = Path(goal_dir)
    do_dispatch = True if auto_dispatch is None else bool(auto_dispatch)
    root = _derive_root(goal)
    if root is None:
        return _result(
            "goal_invalid", ok=False,
            error=f"cannot derive repo root: goal_dir {goal} is not shaped "
                  f"<root>/{'/'.join(ap.GOAL_ROOT_PARTS)}/<goal-id>",
        )
    try:
        config = _load_config(root, config_path)
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
    roles = config["roles"]
    # Resolved ONCE per step and threaded everywhere a family or a handoff
    # is computed (quality findings 1+3): signer selection and the
    # consult-Q2 transparency NOTE must both see the operator's family:
    # overlays, never builtin-only data.
    profiles = _merged_profiles(config)

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

    # The hardened lane git, not a raw subprocess (mirrors approve_spec):
    # a GIT_DIR leaked from a hook context would make rev-parse ignore cwd
    # and read a DIFFERENT repository's HEAD - which would defeat or
    # permanently mis-fire this very base-drift guard.
    try:
        head = lane_mod._git(root, "rev-parse", "HEAD").strip()
    except lane_mod.LaneError as exc:
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
            return _run_build(goal, config, cycle, root, profiles)
        except (lane_mod.LaneError, _db.BuilderDispatchError) as exc:
            return _result("blocked_stop_rule", cycle=cycle, ok=False,
                           stops=[{"rule": "builder_dispatch_failed",
                                   "detail": str(exc)}],
                           error=str(exc))
    verification = ap._read_yaml_or_empty(cdir / ap.VERIFICATION_FILENAME)
    needs_gate = bool(config["architect_loop"].get("verification", "")) and not build.get("pushback")
    if needs_gate and not verification:
        try:
            return _run_verification(goal, config, cycle, root, profiles)
        except lane_mod.LaneError as exc:
            # Transient git/snapshot failure mid-verification must surface
            # as a blocked result, not escape the never-raises boundary.
            return _result("blocked_stop_rule", cycle=cycle, ok=False,
                           stops=[{"rule": "verification_machinery_failed",
                                   "detail": str(exc)}],
                           error=str(exc))
    if needs_gate and verification and not verification.get("passed") and not ruling:
        # RED resume hole: verification.yaml and the mechanical revise are
        # two separate seals - an interrupt between them must not let mere
        # file-existence routing send a RED build to the reviewer ('red
        # builds never reach the reviewer', spec state 10). The transition
        # is gated on verification CONTENT; re-seal the revise idempotently.
        _seal_mechanical_revise(cdir, verification.get("output_tail", ""))
        write_handoff(goal, roles=roles, profiles=profiles)
        return _result("verification_red", cycle=cycle,
                       actions=[{"action": "mechanical_revise_resealed"}])
    review = ap._read_yaml_or_empty(cdir / ap.REVIEW_FILENAME)
    if not review and not ruling:
        return _result("needs_review", cycle=cycle)
    if not ruling:
        return _result("needs_ruling", cycle=cycle)
    disposition = ruling.get("disposition")
    if disposition in ap.CYCLE_ADVANCING_DISPOSITIONS:
        # revise AND overrule (the architect rejecting builder pushback)
        # close the cycle: the next step re-dispatches the builder, whose
        # prompt carries the ruling's reason/feedback. The overruled
        # pushback build itself is never verified or reviewed - it gets
        # superseded by the next cycle's build, so the pushback-skips-
        # verification guard above stays correct. current_cycle() advances
        # past both, so this branch is normally unreachable; defensive
        # total-cascade fallback.
        return _result("cycle_advance", cycle=cycle)  # pragma: no cover
    if disposition == "kill":
        ap.seal_artifact(goal / ap.OUTCOME_FILENAME,
                         {"closing_state": ap.KILLED_CLOSING_STATE,
                          "cycle": cycle, "reason": ruling.get("reason", "")})
        write_handoff(goal, roles=roles, profiles=profiles)
        return _result("killed", cycle=cycle)
    if disposition == "accept":
        if build.get("pushback"):
            # A pushback cycle has NO verification and NO review (its build
            # is a refusal, not work) - accepting it would route an
            # unverified, unreviewed cycle straight to delivery. The
            # documented disposition set for pushback rulings is
            # revise|overrule (kill also remains legal); accept is
            # structurally forbidden here.
            return _result(
                "blocked_stop_rule", cycle=cycle,
                stops=[{
                    "rule": "pushback_accept_forbidden",
                    "detail": (
                        "ruling disposition 'accept' is not legal on a "
                        "pushback cycle; allowed: revise, overrule, kill"
                    ),
                }])
        violations = _signer_violations(goal, cycle, roles, profiles)
        if violations:
            return _result("blocked_stop_rule", cycle=cycle,
                           stops=[{"rule": "signer_invariant_violated",
                                   "violations": violations}])
        # Spec 6.5: the delivery gate INDEPENDENTLY re-checks the integrity
        # snapshot before awaiting_delivery_approval - the build-time check
        # can be stale by the time the human approves (review + ruling
        # steps, possibly multiple cycles, intervene). State 14 then binds
        # the exact lane HEAD + base sha for the downstream delivery mint.
        recheck: list[str] = []
        before_snap = ap._read_yaml_or_empty(goal / ap.INTEGRITY_BEFORE_FILENAME)
        if not all(k in before_snap for k in
                   ("head", "status", "refs", "hooks", "config_sha")):
            recheck.append(
                f"{ap.INTEGRITY_BEFORE_FILENAME} missing or malformed"
            )
        else:
            try:
                recheck += lane_mod.check_main_integrity(
                    root, before_snap,
                    lane_branch=_lane_branch_name(
                        config["architect_loop"], goal
                    ),
                )
            except lane_mod.LaneError as exc:
                recheck.append(f"main integrity recheck failed to run: {exc}")
        lane = ap.lane_dir(goal)
        lane_scan = lane_mod.scan_lane_integrity(lane)
        recheck += lane_scan
        lane_head = None
        if not lane_scan:
            # The scan just verified the .git pointer, so the lane may
            # receive a supervisor git op (the commit_lane invariant).
            try:
                lane_head = lane_mod._git(lane, "rev-parse", "HEAD").strip()
            except lane_mod.LaneError as exc:
                recheck.append(f"cannot read lane HEAD: {exc}")
            else:
                if lane_head != build.get("lane_head_sha"):
                    recheck.append(
                        f"lane HEAD moved after the build seal: "
                        f"{build.get('lane_head_sha')!r} -> {lane_head!r}"
                    )
        if recheck:
            return _result(
                "blocked_stop_rule", cycle=cycle,
                stops=[{"rule": "delivery_integrity_recheck_failed",
                        "violations": recheck}])
        write_handoff(goal, roles=roles, profiles=profiles)
        return _result(
            "awaiting_delivery_approval", cycle=cycle,
            actions=[{"action": "delivery_integrity_recheck",
                      "lane_head_sha": lane_head,
                      "base_sha": approval.get("base_sha")}])
    return _result("needs_ruling", cycle=cycle)


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="consensus-mcp-architect",
        description="architect-build (workflow D) supervisor CLI.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    step = sub.add_parser("step", help="run one loop_step")
    step.add_argument("--goal-dir", required=True)
    step.add_argument("--config", default=None)
    step.add_argument("--no-dispatch", action="store_true")
    approve = sub.add_parser("approve-spec", help="human spec gate")
    approve.add_argument("--goal-dir", required=True)
    approve.add_argument("--approver", required=True)
    approve.add_argument("--repo-root", default=None)
    clean = sub.add_parser("cleanup", help="lane lifecycle for a closed goal")
    clean.add_argument("--goal-dir", required=True)
    clean.add_argument("--repo-root", default=None)
    clean.add_argument("--prune-lane", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "step":
        out = handle(goal_dir=args.goal_dir, config_path=args.config,
                     auto_dispatch=not args.no_dispatch)
    elif args.cmd == "approve-spec":
        from consensus_mcp.tools.architect_gates import handle_approve_spec
        out = handle_approve_spec(goal_dir=args.goal_dir,
                                  approver=args.approver,
                                  repo_root=args.repo_root)
    else:
        from consensus_mcp.tools.architect_gates import handle_cleanup
        out = handle_cleanup(goal_dir=args.goal_dir,
                             repo_root=args.repo_root,
                             prune_lane=args.prune_lane)
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
