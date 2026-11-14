"""Looper-plan compiler: validator + renderer for a loop spec.

Trimmed derivative of an MIT-licensed loop-design tool; model detection/registry,
the external run-loop runner, and session-prompt emission are intentionally
omitted. Full third-party attribution is retained in
consensus_mcp/looper_plan/NOTICE (with the kept-vs-trimmed manifest in
VENDORED.md).

New code (ours): synthesize_stub_fields, compile_plan."""
from __future__ import annotations

import datetime as _dt
import json
import os
import shlex
from pathlib import Path
from typing import Any


DEFAULT_REDACTIONS = [".env", ".env.*", "secrets/**", "**/*.key"]


class LooperError(RuntimeError):
    pass


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise LooperError(
            "PyYAML is required to compile loop.yaml. Install with: python -m pip install PyYAML"
        ) from exc

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise LooperError(f"Could not parse YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LooperError(f"{path} must contain a YAML mapping at the top level")
    return data


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return value


def normalize_argv(value: Any, field: str) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    if isinstance(value, str):
        return shlex.split(value, posix=os.name != "nt")
    raise LooperError(f"{field} must be an argv array or string")


def criteria_by_id(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    criteria = spec.get("goal", {}).get("verification", [])
    if not isinstance(criteria, list):
        raise LooperError("goal.verification must be a list")
    result: dict[str, dict[str, Any]] = {}
    for item in criteria:
        if not isinstance(item, dict):
            raise LooperError("Each verification criterion must be an object")
        cid = item.get("id")
        ctype = item.get("type")
        if not isinstance(cid, str) or not cid:
            raise LooperError("Each verification criterion needs a non-empty id")
        if cid in result:
            raise LooperError(f"Duplicate verification criterion id: {cid}")
        if ctype not in {"programmatic", "judge", "human"}:
            raise LooperError(f"Criterion {cid} has invalid type: {ctype}")
        if ctype == "programmatic":
            item["check"] = normalize_argv(item.get("check"), f"criterion {cid}.check")
            if item.get("expect") not in {"exit_zero", "exit_nonzero", "stdout_contains"}:
                raise LooperError(
                    f"Criterion {cid}.expect must be exit_zero, exit_nonzero, or stdout_contains"
                )
            if item.get("expect") == "stdout_contains" and not isinstance(item.get("contains"), str):
                raise LooperError(f"Criterion {cid} with stdout_contains needs contains")
        elif ctype == "judge" and not isinstance(item.get("rubric"), str):
            raise LooperError(f"Criterion {cid} needs a judge rubric")
        elif ctype == "human" and not isinstance(item.get("prompt"), str):
            raise LooperError(f"Criterion {cid} needs a human prompt")
        result[cid] = item
    return result


def validate_member(member: dict[str, Any]) -> None:
    mid = member.get("id")
    role = member.get("role")
    if not isinstance(mid, str) or not mid:
        raise LooperError("Each council member needs a non-empty id")
    if role not in {"reviewer", "judge"}:
        raise LooperError(f"Council member {mid} role must be reviewer or judge")
    member["invoke"] = normalize_argv(member.get("invoke"), f"council.{mid}.invoke")
    timeout = member.get("timeout_sec", 600)
    if not isinstance(timeout, int) or timeout <= 0:
        raise LooperError(f"Council member {mid}.timeout_sec must be a positive integer")
    member.setdefault("scope", ["plan", "delivery"])
    member.setdefault("local", member.get("cli") == "ollama")


def validate_gate(
    name: str,
    gate: dict[str, Any],
    criteria: dict[str, dict[str, Any]],
    members: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(gate, dict):
        raise LooperError(f"{name} must be an object")
    policy = gate.get("verdict_policy")
    if policy not in {"revise_until_clean", "fixed_passes"}:
        raise LooperError(f"{name}.verdict_policy must be revise_until_clean or fixed_passes")
    max_revisions = gate.get("max_revisions", 1)
    if not isinstance(max_revisions, int) or max_revisions < 0:
        raise LooperError(f"{name}.max_revisions must be a non-negative integer")
    for cid in gate.get("criteria", []):
        if cid not in criteria:
            raise LooperError(f"{name} references unknown criterion: {cid}")
    for mid in gate.get("members", []):
        if mid not in members:
            raise LooperError(f"{name} references unknown council member: {mid}")
    if policy == "revise_until_clean":
        source = gate.get("verdict_source")
        if source == "human":
            return
        if source not in members:
            raise LooperError(f"{name}.verdict_source must be a judge member or human")
        if members[source].get("role") != "judge":
            raise LooperError(f"{name}.verdict_source must name a judge, not a reviewer")


def normalize_spec(spec: dict[str, Any], source_path: Path) -> dict[str, Any]:
    if spec.get("version") != 1:
        raise LooperError("Only loop.yaml version: 1 is supported")

    goal = spec.get("goal")
    if not isinstance(goal, dict):
        raise LooperError("goal must be an object")
    if not isinstance(goal.get("statement"), str) or not goal["statement"].strip():
        raise LooperError("goal.statement is required")
    if not isinstance(goal.get("definition_of_done"), str) or not goal["definition_of_done"].strip():
        raise LooperError("goal.definition_of_done is required")

    for index, source in enumerate(goal.get("context_sources", [])):
        if not isinstance(source, dict):
            raise LooperError("goal.context_sources entries must be objects")
        if "cmd" in source:
            source["cmd"] = normalize_argv(source["cmd"], f"context_sources[{index}].cmd")

    criteria = criteria_by_id(spec)

    host = spec.get("host")
    if not isinstance(host, dict):
        raise LooperError("host must be an object")
    host["invoke"] = normalize_argv(host.get("invoke"), "host.invoke")
    host.setdefault("timeout_sec", 600)
    if not isinstance(host["timeout_sec"], int) or host["timeout_sec"] <= 0:
        raise LooperError("host.timeout_sec must be a positive integer")

    council_list = spec.get("council", [])
    if not isinstance(council_list, list):
        raise LooperError("council must be a list")
    for member in council_list:
        if not isinstance(member, dict):
            raise LooperError("council entries must be objects")
        validate_member(member)
    members = {member["id"]: member for member in council_list}

    gates = spec.get("gates")
    if not isinstance(gates, dict):
        raise LooperError("gates must be an object")
    for gate_name in ("plan_gate", "delivery_gate"):
        validate_gate(gate_name, gates.get(gate_name), criteria, members)

    control = spec.get("loop_control")
    if not isinstance(control, dict):
        raise LooperError("loop_control must be an object")
    max_iterations = control.get("max_iterations")
    if not isinstance(max_iterations, int) or max_iterations <= 0:
        raise LooperError("loop_control.max_iterations must be a positive integer")
    budget = control.setdefault("budget", {})
    if not isinstance(budget, dict):
        raise LooperError("loop_control.budget must be an object")
    if "wall_clock_min" not in budget:
        budget["wall_clock_min"] = 30
    no_progress = control.setdefault(
        "no_progress",
        {
            "max_stalled_iterations": 2,
            "signals": [
                "same blocking issue repeats",
                "delivery artifact has no material change",
                "verifier output is unchanged",
            ],
            "action": "stop",
        },
    )
    if not isinstance(no_progress, dict):
        raise LooperError("loop_control.no_progress must be an object")
    stalled = no_progress.setdefault("max_stalled_iterations", 2)
    if not isinstance(stalled, int) or stalled <= 0:
        raise LooperError("loop_control.no_progress.max_stalled_iterations must be a positive integer")
    signals = no_progress.setdefault("signals", ["same blocking issue repeats"])
    if not isinstance(signals, list) or not all(isinstance(item, str) for item in signals):
        raise LooperError("loop_control.no_progress.signals must be a list of strings")
    action = no_progress.setdefault("action", "stop")
    if action not in {"stop", "human_checkpoint"}:
        raise LooperError("loop_control.no_progress.action must be stop or human_checkpoint")

    execution = spec.setdefault(
        "execution",
        {
            "mode": "in_session",
            "isolation": "current_workspace",
            "side_effects": {"requires_approval": True, "duplicate_action_check": True},
        },
    )
    if not isinstance(execution, dict):
        raise LooperError("execution must be an object")
    execution.setdefault("mode", "in_session")
    execution.setdefault("isolation", "current_workspace")
    if execution["mode"] not in {"in_session", "external_runner", "orchestrated"}:
        raise LooperError("execution.mode must be in_session, external_runner, or orchestrated")
    if execution["isolation"] not in {"current_workspace", "branch", "worktree", "sandbox"}:
        raise LooperError("execution.isolation must be current_workspace, branch, worktree, or sandbox")
    side_effects = execution.setdefault("side_effects", {})
    if not isinstance(side_effects, dict):
        raise LooperError("execution.side_effects must be an object")
    side_effects.setdefault("requires_approval", True)
    side_effects.setdefault("duplicate_action_check", True)

    observability = spec.setdefault(
        "observability",
        {"state_file": "state.json", "run_log": "run-log.md", "checkpoint_granularity": "gate"},
    )
    if not isinstance(observability, dict):
        raise LooperError("observability must be an object")
    observability.setdefault("state_file", "state.json")
    observability.setdefault("run_log", "run-log.md")
    observability.setdefault("checkpoint_granularity", "gate")
    if not isinstance(observability["state_file"], str) or not observability["state_file"]:
        raise LooperError("observability.state_file must be a non-empty string")
    if not isinstance(observability["run_log"], str) or not observability["run_log"]:
        raise LooperError("observability.run_log must be a non-empty string")
    if observability["checkpoint_granularity"] not in {"gate", "step"}:
        raise LooperError("observability.checkpoint_granularity must be gate or step")

    workspace = spec.setdefault("workspace", {})
    if not isinstance(workspace, dict):
        raise LooperError("workspace must be an object")
    workspace.setdefault("dir", "./loop-workspace")
    layout = workspace.setdefault("layout", ["plan.md", "delivery-{n}.md", "review-{n}.md", "state.json", "run-log.md"])
    if not isinstance(layout, list) or not all(isinstance(item, str) for item in layout):
        raise LooperError("workspace.layout must be a list of strings")
    for required_file in (observability["state_file"], observability["run_log"]):
        if required_file not in layout:
            layout.append(required_file)

    privacy = spec.setdefault("privacy", {})
    if not isinstance(privacy, dict):
        raise LooperError("privacy must be an object")
    egress = privacy.setdefault("egress", [])
    if not isinstance(egress, list):
        raise LooperError("privacy.egress must be a list")
    for entry in egress:
        if not isinstance(entry, dict):
            raise LooperError("privacy.egress entries must be objects")
        entry.setdefault("redact", DEFAULT_REDACTIONS)
        entry.setdefault("consent", "required")

    resolved = {
        "$schema": "consensus-mcp/looper-plan/loop.resolved.v1",
        "compiled_at": _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat(),
        "source": str(source_path),
        **spec,
        "criteria_by_id": criteria,
        "council_by_id": members,
    }
    return to_jsonable(resolved)


def clip(text: Any, width: int) -> str:
    value = str(text or "")
    return value if len(value) <= width else value[: width - 1] + "~"


def ascii_box(*rows: str, width: int = 30) -> list[str]:
    border = "+" + "-" * (width + 2) + "+"
    body = [f"| {clip(row, width):<{width}} |" for row in rows if row is not None]
    return [border, *body, border]


def render_ascii_diagram(resolved: dict[str, Any]) -> str:
    gates = resolved.get("gates", {})
    control = resolved.get("loop_control", {})
    observability = resolved.get("observability", {})
    plan_gate = gates.get("plan_gate", {})
    delivery_gate = gates.get("delivery_gate", {})
    plan_revisions = plan_gate.get("max_revisions", 0)
    delivery_revisions = delivery_gate.get("max_revisions", 0)
    plan_source = plan_gate.get("verdict_source", "human")
    delivery_source = delivery_gate.get("verdict_source", "human")
    no_progress = control.get("no_progress", {})
    stalled = no_progress.get("max_stalled_iterations", 2)
    budget = control.get("budget", {})
    budget_bits = []
    if budget.get("wall_clock_min") is not None:
        budget_bits.append(f"{budget.get('wall_clock_min')}m")
    if budget.get("usd") is not None:
        budget_bits.append(f"${budget.get('usd')}")
    if budget.get("tokens") is not None:
        budget_bits.append(f"{budget.get('tokens')} tokens")
    budget_text = ", ".join(budget_bits) or "configured caps"

    lines: list[str] = []
    lines.extend(ascii_box("1. Goal + context", "read sources"))
    lines.extend(["               |", "               v"])
    lines.extend(ascii_box("2. Draft plan.md", f"state -> {observability.get('state_file', 'state.json')}"))
    lines.extend(["               |", "               v"])
    lines.extend(ascii_box("3. Plan gate", f"verdict: {plan_source}"))
    lines.extend([f"               | needs work -> revise <= {plan_revisions} -> step 2", "               | pass", "               v"])
    lines.extend(ascii_box("4. Write delivery-N.md", f"log -> {observability.get('run_log', 'run-log.md')}"))
    lines.extend(["               |", "               v"])
    lines.extend(ascii_box("5. Delivery gate", f"verdict: {delivery_source}"))
    lines.extend([f"               | needs work -> revise <= {delivery_revisions} -> step 4", "               | pass", "               v"])
    lines.extend(ascii_box("6. Final output", "all gates clean"))
    lines.extend(
        [
            "",
            f"Stops: pass gates | max {control.get('max_iterations')} iterations | "
            f"no progress x{stalled} | budget {budget_text}",
        ]
    )
    return "\n".join(lines)


def render_loop(resolved: dict[str, Any]) -> str:
    meta = resolved.get("meta", {})
    goal = resolved.get("goal", {})
    gates = resolved.get("gates", {})
    control = resolved.get("loop_control", {})
    execution = resolved.get("execution", {})
    observability = resolved.get("observability", {})
    title = meta.get("name") or "Looper Generated Loop"
    criteria = goal.get("verification", [])
    council = resolved.get("council", [])

    lines = [
        f"# {title}",
        "",
        meta.get("description", "").strip(),
        "",
        "## Goal",
        "",
        goal.get("statement", "").strip(),
        "",
        "## Definition of Done",
        "",
        goal.get("definition_of_done", "").strip(),
        "",
        "## Verification",
        "",
    ]
    for item in criteria:
        lines.append(f"- `{item['id']}` ({item['type']})")
    lines.extend(["", "## Council", ""])
    if council:
        for member in council:
            lines.append(
                f"- `{member['id']}`: {member.get('role')} via {member.get('cli')} "
                f"({member.get('model', 'default')})"
            )
    else:
        lines.append("- No council members configured.")
    lines.extend(
        [
            "",
            "## Gates",
            "",
            f"- Plan gate: {gates.get('plan_gate', {}).get('verdict_policy')}",
            f"- Delivery gate: {gates.get('delivery_gate', {}).get('verdict_policy')}",
            "",
            "## Loop Control",
            "",
            f"- Max iterations: {control.get('max_iterations')}",
            f"- Budget: `{json.dumps(control.get('budget', {}), sort_keys=True)}`",
            f"- No-progress: `{json.dumps(control.get('no_progress', {}), sort_keys=True)}`",
            "",
            "## Execution Boundary",
            "",
            f"- Mode: `{execution.get('mode', 'in_session')}`",
            f"- Isolation: `{execution.get('isolation', 'current_workspace')}`",
            f"- Side effects: `{json.dumps(execution.get('side_effects', {}), sort_keys=True)}`",
            "",
            "## Observability",
            "",
            f"- State file: `{observability.get('state_file', 'state.json')}`",
            f"- Run log: `{observability.get('run_log', 'run-log.md')}`",
            f"- Checkpoint granularity: `{observability.get('checkpoint_granularity', 'gate')}`",
            "",
            "## Flow Preview",
            "",
            "```text",
            render_ascii_diagram(resolved),
            "```",
            "",
        ]
    )
    return "\n".join(line for line in lines if line is not None)


# --- New code (ours): Build-facing entry + stub synthesis ---------------------

# Non-network sentinel invoke for the stub host/council. Build never executes
# these - they exist only so a goal/verification/caps-only coached spec passes
# normalize_spec (which requires host/council/gates/workspace).
_STUB_INVOKE = ["consensus", "build", "executes-this-not-looper"]


def synthesize_stub_fields(coached: dict, roles: dict) -> dict:
    """Fill the loop.v1 schema's required-but-unused-by-us fields (host, council,
    gates, workspace, execution) from Build's roles, so a goal/verification/caps
    -only coached spec validates. These stubs are NEVER executed - Build is the
    runner; they exist solely to keep normalize_spec verbatim."""
    spec = {**coached}
    crit_ids = [c["id"] for c in spec.get("goal", {}).get("verification", [])]
    first = crit_ids[:1]
    spec.setdefault("host", {"cli": roles.get("builder", "codex"),
                             "model": "build-resolved",
                             "invoke": list(_STUB_INVOKE)})
    spec.setdefault("council", [{"id": "reviewer", "role": "judge",
                                 "cli": roles.get("reviewer", "codex"),
                                 "model": "build-resolved",
                                 "invoke": list(_STUB_INVOKE)}])
    spec.setdefault("gates", {
        "plan_gate": {"when": "after_plan", "members": ["reviewer"],
                      "verdict_policy": "revise_until_clean",
                      "verdict_source": "reviewer", "criteria": list(first),
                      "max_revisions": 3},
        "delivery_gate": {"when": "after_each_delivery", "members": ["reviewer"],
                          "verdict_policy": "revise_until_clean",
                          "verdict_source": "reviewer", "criteria": list(crit_ids),
                          "max_revisions": 3},
    })
    spec.setdefault("execution", {"mode": "orchestrated", "isolation": "worktree",
                                  "side_effects": {"requires_approval": True,
                                                   "duplicate_action_check": True}})
    spec.setdefault("workspace", {"dir": "./looper-plan"})
    return spec


def compile_plan(loop_yaml_path) -> tuple[dict, str]:
    """Validate+normalize a loop.yaml and render LOOP.md. Returns (resolved, md)."""
    source = Path(loop_yaml_path).resolve()
    spec = load_yaml(source)
    resolved = normalize_spec(spec, source)
    return resolved, render_loop(resolved)
