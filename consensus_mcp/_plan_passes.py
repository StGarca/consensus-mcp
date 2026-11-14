"""Plan review rounds and the user-owned resolution of reviewer holdouts."""
from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path
import re

import yaml

from consensus_mcp._atomic_io import atomic_write_text

FILENAME = "plan-objections.yaml"
KINDS = {"wrong_rejection", "wrong_deferral", "wrong_acceptance", "conflict", "gap", "other"}
CONFIRM_KINDS = {"unfaithful", "unrelated_change"}


def _read(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a mapping")
    return data


def _write(path: Path, data: dict) -> None:
    atomic_write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=False))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _diff(before: str, after: str, filename: str) -> str:
    lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                 fromfile="reviewed/" + filename, tofile="current/" + filename)
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def parse_summary(summary: str) -> tuple[str, str]:
    """Tags belong in the existing summary, never in reviewer output schemas."""
    match = re.match(r"^\[([^]]+)\]\s*(.*)", summary.strip(), re.DOTALL)
    if match and match[1] in KINDS:
        return match[1], match[2].strip() or summary.strip()
    return "other", summary.strip()


RUNS_LOG = "consult-runs.jsonl"


def _state_root(rr: Path) -> Path:
    return Path(os.environ.get("CONSENSUS_MCP_STATE_ROOT") or rr / "consensus-state")


def record_run(rr: Path, iteration: str, tier: str, run_id: str, sealed_count: int) -> None:
    """Append one line per panel run to the archive. The mutable summaries can
    be deleted or edited; this log is what the tier and plan-pass checks fall
    back to, so removing a summary cannot downgrade a consult or hide a pass."""
    import json
    from consensus_mcp.tools.review_write_and_seal import _file_lock

    archive = _state_root(rr) / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"iteration": iteration, "tier": tier, "run_id": run_id,
                       "sealed_count": int(sealed_count)}, sort_keys=True)
    with _file_lock(archive / "consult-runs.lock"):
        with open(archive / RUNS_LOG, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")


def _logged_runs(rr: Path) -> list[dict]:
    import json

    path = _state_root(rr) / "archive" / RUNS_LOG
    if not path.exists():
        return []
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            if isinstance(entry, dict):
                runs.append(entry)
    return runs


def _tier(iter_dir: Path) -> str:
    path = iter_dir / "consult-summary.yaml"
    logged = [r.get("tier") for r in _logged_runs(iter_dir.parents[2]) if r.get("iteration") == iter_dir.name]
    if path.exists():
        tier = _read(path).get("tier", "standard")
        if logged and tier != logged[-1]:
            raise ValueError(f"consult summary tier {tier!r} differs from the archived run log ({logged[-1]!r})")
    else:
        # Fail closed: a deleted summary falls back to the archived run log.
        # Only an iteration that never ran through the panel is legacy (quick).
        tier = logged[-1] if logged else "quick"
    if tier not in {"quick", "standard", "deep"}:
        raise ValueError("consult summary tier must be quick, standard, or deep")
    return tier


def _rounds(iter_dir: Path) -> list[tuple[int, Path, dict]]:
    rounds = []
    prefix = iter_dir.name + "-plan-"
    # A plan pass is evidenced by its folder, its sealed archive entries or the
    # archived run log - never only by its (deletable) summary.
    evidenced = {child.name for child in iter_dir.parent.glob(prefix + "*")
                 if child.name[len(prefix):].isdigit()}
    evidenced |= {r.get("iteration") for r in _logged_runs(iter_dir.parents[2])
                  if str(r.get("iteration", "")).startswith(prefix) and int(r.get("sealed_count") or 0) > 0}
    index_path = _state_root(iter_dir.parents[2]) / "archive" / "review-passes" / "index.yaml"
    if index_path.exists():
        from consensus_mcp._consult_panel import read_review_index
        evidenced |= {str(r.get("iteration_id")) for r in read_review_index(index_path.parent).get("passes", [])
                      if str(r.get("iteration_id", "")).startswith(prefix)}
    for name in sorted(evidenced):
        if name[len(prefix):].isdigit() and not (iter_dir.parent / name / "consult-summary.yaml").is_file():
            raise ValueError(f"{name}: plan pass is missing its consult summary")
    for child in iter_dir.parent.glob(iter_dir.name + "-plan-*"):
        suffix = child.name[len(iter_dir.name + "-plan-"):]
        if suffix.isdigit() and (child / "consult-summary.yaml").is_file():
            summary = _read(child / "consult-summary.yaml")
            if summary.get("pass_kind") not in {"plan", "confirm"} or summary.get("parent_iteration") != iter_dir.name:
                raise ValueError(f"{child.name}: missing plan-pass binding")
            packet = _read(child / "review-packet.yaml")
            if packet.get("pass_kind") != summary.get("pass_kind"):
                raise ValueError(f"{child.name}: pass kind differs from review packet")
            if packet.get("converged_plan_sha256") != summary.get("converged_plan_sha256"):
                raise ValueError(f"{child.name}: plan hash binding differs from review packet")
            plan = packet.get("converged_plan")
            if not isinstance(plan, str) or hashlib.sha256(plan.encode("utf-8")).hexdigest() != packet.get("converged_plan_sha256"):
                raise ValueError(f"{child.name}: reviewed plan content does not match hash")
            if not isinstance(packet.get("dispositions_sha256"), str):
                raise ValueError(f"{child.name}: review packet does not bind dispositions.yaml")
            if summary.get("pass_kind") == "confirm" and packet["dispositions_sha256"] != summary.get("dispositions_sha256"):
                raise ValueError(f"{child.name}: dispositions hash binding differs from review packet")
            _check_canonical_child(iter_dir, child, packet)
            rounds.append((int(suffix), child, summary))
    return sorted(rounds, key=lambda item: item[0])


_TEMPLATES = {"plan": "plan_review.md", "confirm": "confirm_review.md"}


class _ReadableDumper(yaml.SafeDumper):
    """Multi-line text (plans, decision tables, diffs) as literal blocks, so a
    reviewer reads it verbatim instead of YAML-escaped."""


def _readable_str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ReadableDumper.add_representer(str, _readable_str)


def _normalize_newlines(value):
    if isinstance(value, str):
        return value.replace("\r\n", "\n")
    if isinstance(value, dict):
        return {k: _normalize_newlines(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_newlines(v) for v in value]
    return value


def _render_packet(packet: dict) -> str:
    """Reviewer-facing rendering of a plan/confirm packet. Only the prompt text
    is normalized; review-packet.yaml keeps the exact hashed content."""
    return yaml.dump(_normalize_newlines(packet), Dumper=_ReadableDumper, sort_keys=False,
                     allow_unicode=False, width=10**6)


def _confirm_goal(child_name: str, instructions: str, packet: dict) -> dict:
    """The exact goal packet a confirmation pass sends reviewers."""
    return {"schema_version": 1, "iteration_id": child_name,
            "allowed_files": [":(exclude)**"],
            "goal": {"summary": "Confirm faithful application of accepted objections",
                     "desired_end_state": instructions + "\n\n" + _render_packet(packet)}}


def _child_goal(parent_goal: dict, child_name: str, instructions: str, packet: dict) -> dict:
    """The exact goal packet a plan pass sends reviewers (single source for
    prepare_plan_pass and the canonical check)."""
    goal = dict(parent_goal)
    goal["iteration_id"] = child_name
    # Dispatch templates always include desired_end_state, while the dispatch
    # diff packet may omit the raw review target. Include the plan in both.
    goal["goal"] = dict(goal.get("goal") or {})
    goal["goal"]["desired_end_state"] = (str(goal["goal"].get("desired_end_state", "")) +
        "\n\n" + instructions + "\n\n" + _render_packet(packet))
    return goal


def _check_canonical_child(iter_dir: Path, child: Path, packet: dict) -> None:
    """What reviewers were shown must be exactly what the tool builds: shipped
    instructions, the real decisions, and the generated goal packet - so a host
    cannot weaken the instructions or substitute content before dispatch."""
    template = _TEMPLATES.get(packet.get("pass_kind"))
    if template is not None:
        shipped = (Path(__file__).parent / "dispatch_templates" / template).read_text(encoding="utf-8")
        if packet.get("instructions") != shipped:
            raise ValueError(f"{child.name}: reviewer instructions differ from the shipped template")
    if packet.get("pass_kind") == "confirm":
        if isinstance(packet.get("instructions"), str):
            if _read(child / "goal_packet.yaml") != _confirm_goal(child.name, packet["instructions"], packet):
                raise ValueError(f"{child.name}: goal packet sent to reviewers is not the generated one")
            prompt = child / "confirm-pass.md"
            if prompt.exists() and prompt.read_text(encoding="utf-8") != packet["instructions"] + "\n\n{desired_end_state}\n":
                raise ValueError(f"{child.name}: confirmation prompt sent to reviewers is not the generated one")
        return
    dispositions_path = iter_dir / "dispositions.yaml"
    if dispositions_path.is_file() and packet.get("dispositions_sha256") == _sha(dispositions_path):
        current = _read(dispositions_path)
        if packet.get("dispositions") != current:
            raise ValueError(f"{child.name}: dispositions shown to reviewers differ from dispositions.yaml")
        expected = [r for r in current.get("dispositions", []) if r.get("disposition") in {"rejected", "deferred"}]
        if packet.get("rejected_or_deferred_ideas") != expected:
            raise ValueError(f"{child.name}: rejected/deferred ideas shown to reviewers were altered")
    parent_goal = iter_dir / "goal_packet.yaml"
    if parent_goal.is_file() and isinstance(packet.get("instructions"), str):
        expected_goal = _child_goal(_read(parent_goal), child.name, packet["instructions"], packet)
        if _read(child / "goal_packet.yaml") != expected_goal:
            raise ValueError(f"{child.name}: goal packet sent to reviewers is not the generated one")


def _reviews(child: Path, summary: dict):
    """Recheck both archive and mirror, not merely the mutable summary status."""
    from consensus_mcp._consult_panel import _sealed_packet, read_review_index

    rr = child.parents[2]
    archive = Path(os.environ.get("CONSENSUS_MCP_STATE_ROOT") or rr / "consensus-state") / "archive" / "review-passes"
    index_path = archive / "index.yaml"
    if index_path.exists():
        represented = {(r.get("pass_id"), r.get("seat"), r.get("packet_sha256"))
                       for r in summary.get("seats", []) if r.get("status") == "sealed"}
        archived = {(r.get("id"), r.get("reviewer_id"), r.get("packet_sha256"))
                    for r in read_review_index(archive).get("passes", []) if r.get("iteration_id") == child.name}
        if not archived <= represented:
            rows = {r.get("seat"): r for r in summary.get("seats", [])}
            detail = "; ".join(
                f"{seat} pass {pass_id}: summary "
                + (f"status={rows[seat].get('status')} failure_class={rows[seat].get('failure_class')} "
                   f"error={(rows[seat].get('error') or '')[:200]!r}" if seat in rows else "has no row")
                for pass_id, seat, _ in sorted(archived - represented, key=str))
            raise ValueError(f"{child.name}: sealed archive review omitted or marked failed in summary ({detail})")
    for row in summary.get("seats", []):
        if row.get("status") != "sealed":
            continue
        result = {"sealed_path": row.get("mirror_path"),
                  "archive_sealed_path": row.get("archive_sealed_path"),
                  "packet_sha256": row.get("packet_sha256"), "pass_id": row.get("pass_id")}
        try:
            packet, _, _ = _sealed_packet(result, row.get("pass_id"), rr, child.name, row.get("seat"))
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        provenance = packet.get("dispatch_provenance") or {}
        for key, filename in (("review_target_hash", "review-packet.yaml"),
                              ("goal_packet_sha256", "goal_packet.yaml")):
            digest = hashlib.sha256((child / filename).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            if provenance.get(key) != digest:
                raise ValueError(f"{child.name}: sealed {key} does not match reviewed input")
        model = (provenance.get("actual_model") or provenance.get("model_used")
                 or provenance.get("model") or "unknown")
        yield str(row["seat"]), str(model), packet


def _sources(iter_dir: Path) -> tuple[list[dict], dict]:
    sources, reviews = [], {}
    for number, child, summary in _rounds(iter_dir):
        reviews[number] = {}
        for reviewer, model, packet in _reviews(child, summary):
            findings = list(packet.get("findings") or [])
            ids = {f.get("id") for f in findings if isinstance(f, dict)}
            findings.extend(o for o in packet.get("blocking_objections", [])
                            if not isinstance(o, str) or o not in ids)
            if not findings and packet.get("goal_satisfied") is not True:
                findings = [{"summary": "Reviewer did not affirm that the plan satisfies the goal."}]
            rows = []
            for index, finding in enumerate(findings):
                summary_text = str(finding.get("summary", finding)) if isinstance(finding, dict) else str(finding)
                kind, text = parse_summary(summary_text)
                if summary.get("pass_kind") == "confirm":
                    match = re.match(r"^\[([^]]+)\]\s*(.*)", summary_text.strip(), re.DOTALL)
                    kind = "unrelated_change" if match and match[1] == "unrelated_change" else "unfaithful"
                    text = (match[2].strip() or summary_text.strip()) if match else summary_text.strip()
                row = {"id": f"plan-{number}/{reviewer}/{index}", "pass": number,
                       "reviewer": reviewer, "model": model, "kind": kind, "summary": text,
                       "response": "pending", "host_response": "", "evidence": "",
                       "user_decision": None, "decided_by": None}
                sources.append(row)
                rows.append(row)
            reviews[number][reviewer] = {"model": model, "rows": rows}
    return sources, reviews


def init_objections(iter_dir: Path) -> Path:
    """Append sealed source rows without overwriting previous host responses."""
    sources, _ = _sources(iter_dir)
    if not _rounds(iter_dir):
        raise ValueError("Run a plan pass before --init-objections")
    path = iter_dir / FILENAME
    data = _read(path) if path.exists() else {"schema_version": 1, "send_back_after_pass": 0, "objections": []}
    from consensus_mcp.validators.validate_plan_objections import validate_plan_objections
    errors = validate_plan_objections(data, sources, require_complete=False)
    if errors:
        raise ValueError("; ".join(errors))
    known = {row["id"] for row in data["objections"]}
    data["objections"].extend(row for row in sources if row["id"] not in known)
    _write(path, data)
    return path


def objections_hash(iter_dir: Path) -> str | None:
    path = iter_dir / FILENAME
    return _sha(path) if path.exists() else None


def _table(iter_dir: Path, sources: list[dict]) -> dict:
    from consensus_mcp.validators.validate_plan_objections import validate_plan_objections
    path = iter_dir / FILENAME
    data = _read(path) if path.exists() else {"schema_version": 1, "send_back_after_pass": 0, "objections": []}
    errors = validate_plan_objections(data, sources)
    if errors:
        raise ValueError("; ".join(errors))
    anchor = data.get("send_back_after_pass", 0)
    rounds = _rounds(iter_dir)
    grant = next((r for r in data["objections"] if r["id"] == data.get("send_back_objection_id")
                  and r["user_decision"] is not None and r["decided_by"] == "user"), None)
    if anchor and (grant is None or not rounds or anchor > rounds[-1][0] or anchor < grant["pass"]):
        raise ValueError("send_back_after_pass must match a recorded user send_back within completed passes")
    return data


def record_user_decisions(iter_dir: Path, decisions: list[str]) -> Path:
    """Record answers supplied by the user; the host must never supply its own."""
    path = init_objections(iter_dir)
    sources, _ = _sources(iter_dir)
    data = _table(iter_dir, sources)
    indexed = {row["id"]: row for row in data["objections"]}
    parsed, seen = [], set()
    for value in decisions:
        identifier, sep, decision = value.rpartition("=")
        if not sep or identifier not in indexed or decision not in {"yay", "nay", "send_back"}:
            raise ValueError("user decision must name an objection: <id>=yay|nay|send_back")
        row = indexed[identifier]
        if identifier in seen:
            raise ValueError(f"{identifier}: duplicate user decision")
        seen.add(identifier)
        if row["user_decision"] is not None and not (row["user_decision"] == "send_back" and decision in {"yay", "nay"}):
            raise ValueError(f"{identifier}: user decision already recorded")
        if decision == "send_back" and _tier(iter_dir) != "deep":
            raise ValueError("send_back is available only for deep consultations")
        parsed.append((row, decision))
    for row, decision in parsed:
        row.update(user_decision=decision, decided_by="user")
        if decision == "send_back":
            data["send_back_after_pass"] = max(n for n, _, _ in _rounds(iter_dir))
            data["send_back_objection_id"] = row["id"]
    _write(path, data)
    return path


def _families(reviewers: dict) -> set[str]:
    return {r["model"].strip().lower() for seat, r in reviewers.items()
            if seat != "claude" and "claude" not in r["model"].lower() and r["model"] != "unknown"}


def _clean_confirmation(rounds: list, reviews: dict, current_sha: str, dispositions_sha: str, after: int = 0) -> bool:
    return any(number > after and summary.get("pass_kind") == "confirm"
               and summary.get("converged_plan_sha256") == current_sha
               and summary.get("dispositions_sha256") == dispositions_sha
               and len(_families(reviews[number])) >= 2
               and all(not r["rows"] for r in reviews[number].values())
               for number, _, summary in rounds)


def _resolved(row: dict, rounds: list, reviews: dict, current_sha: str, tier: str,
              dispositions_sha: str = "") -> bool:
    decision = row["user_decision"] if row["decided_by"] == "user" else None
    if decision == "nay":
        return True
    _, child, summary = next(r for r in rounds if r[0] == row["pass"])
    baseline = summary["converged_plan_sha256"]
    changed = current_sha != baseline
    if tier == "standard" and decision != "yay" and row["kind"] not in CONFIRM_KINDS:
        changed = changed or dispositions_sha != _read(child / "review-packet.yaml")["dispositions_sha256"]
    accepted = row["response"] == "accepted" and bool(row["host_response"].strip()) and changed
    if row["kind"] in CONFIRM_KINDS:
        return (decision == "yay" and accepted
                and _clean_confirmation(rounds, reviews, current_sha, dispositions_sha, row["pass"]))
    if decision == "yay":
        return accepted
    if tier == "deep":
        # Deep closes an objection only when the reviewer who raised it (same
        # seat and model) returns a clean review of the CURRENT plan. The host
        # marking it accepted and editing the plan is not enough, and a later
        # pass where that reviewer failed or was absent never clears it.
        for number, _, summary in rounds:
            later = reviews[number].get(row["reviewer"])
            if (number > row["pass"] and summary.get("converged_plan_sha256") == current_sha
                    and later is not None and later["model"] == row["model"] and not later["rows"]):
                return True
        return False
    return accepted


def _holdouts(iter_dir: Path) -> list[dict]:
    sources, reviews = _sources(iter_dir)
    data = _table(iter_dir, sources)
    rounds = _rounds(iter_dir)
    current_sha = _sha(iter_dir / "converged-plan.yaml")
    dispositions_sha = _sha(iter_dir / "dispositions.yaml")
    return [row for row in data["objections"]
            if not _resolved(row, rounds, reviews, current_sha, _tier(iter_dir), dispositions_sha)]


def _needs_user(iter_dir: Path, holdouts: list[dict]) -> dict:
    choices = "yay or nay" if any(r["kind"] in CONFIRM_KINDS for r in holdouts) else "yay, nay, or send_back"
    return {"ok": False, "status": "needs_user_decision", "iteration": iter_dir.name,
            "holdouts": holdouts,
            "error": f"Ask the user to decide each holdout ({choices}). The host must never answer for the user."}


def _max_passes(rr: Path) -> int:
    path = rr / ".consensus" / "config.yaml"
    value = ((_read(path).get("consult") or {}).get("max_plan_passes", 5) if path.exists() else 5)
    if type(value) is not int or value < 1:
        raise ValueError("consult.max_plan_passes must be a positive integer")
    return value


def _pass_count(rounds: list, after: int) -> int:
    # Failed attempts and insufficient-family runs cannot consume the budget.
    return sum(number > after and len({model.lower() for seat, model, _ in _reviews(child, summary)
               if seat != "claude" and "claude" not in model.lower() and model != "unknown"}) >= 2
               for number, child, summary in rounds)


def prepare_plan_pass(rr: Path, iter_dir: Path, pass_kind: str = "plan") -> dict:
    tier = _tier(iter_dir)
    if pass_kind not in {"plan", "confirm"}:
        raise ValueError("pass_kind must be plan or confirm")
    if pass_kind == "confirm" and tier != "standard":
        raise ValueError("confirm passes are available only for standard tier")
    if tier == "quick":
        raise ValueError("quick tier has one ideas pass and no plan passes")
    original = _read(iter_dir / "consult-summary.yaml")
    seats = [r["seat"] for r in original.get("seats", [])]
    if not seats:
        raise ValueError("ideas consult summary has no seats")
    plan = iter_dir / "converged-plan.yaml"
    if not plan.is_file() or not (iter_dir / "dispositions.yaml").is_file():
        raise ValueError("Author converged-plan.yaml and dispositions.yaml before a plan pass")
    rounds = _rounds(iter_dir)
    data = {"send_back_after_pass": 0, "objections": []}
    if rounds:
        init_objections(iter_dir)
        sources, _ = _sources(iter_dir)
        data = _table(iter_dir, sources)
        anchor = data.get("send_back_after_pass", 0)
        if tier == "deep" and anchor:
            undecided = [r for r in _holdouts(iter_dir) if r["pass"] <= anchor and r["user_decision"] is None]
            if undecided:
                return _needs_user(iter_dir, undecided)
        if tier == "deep" and _pass_count(rounds, data.get("send_back_after_pass", 0)) >= _max_passes(rr):
            holdouts = _holdouts(iter_dir)
            if holdouts:
                return _needs_user(iter_dir, holdouts)
            if rounds[-1][2]["converged_plan_sha256"] == _sha(plan):
                raise ValueError("deep plan review is already complete; approve the plan")
    latest_plan = next((r for r in reversed(rounds) if r[2].get("pass_kind") == "plan"), None)
    accepted = []
    reviewed_packet = None
    if pass_kind == "confirm":
        if latest_plan is None:
            raise ValueError("Run a plan pass before --confirm-pass")
        reviewed_packet = _read(latest_plan[1] / "review-packet.yaml")
        if (reviewed_packet["converged_plan_sha256"] == _sha(plan)
                and reviewed_packet["dispositions_sha256"] == _sha(iter_dir / "dispositions.yaml")):
            raise ValueError("--confirm-pass requires a changed plan or dispositions.yaml since the latest plan pass")
        accepted = [r for r in data["objections"] if r["pass"] == latest_plan[0]
                    and r["response"] == "accepted" and r["host_response"].strip()]
        if not accepted:
            raise ValueError("--confirm-pass requires an accepted objection from the plan pass with a host response")
        confirm_holdouts = [r for r in _holdouts(iter_dir) if r["kind"] in CONFIRM_KINDS]
        undecided = [r for r in confirm_holdouts if r["user_decision"] is None]
        if undecided:
            return _needs_user(iter_dir, undecided)
        for row in confirm_holdouts:
            baseline = next(s["converged_plan_sha256"] for n, _, s in rounds if n == row["pass"])
            if row["user_decision"] == "yay" and (row["response"] != "accepted"
                    or not row["host_response"].strip() or _sha(plan) == baseline):
                raise ValueError(f"{row['id']}: user upheld the confirmation objection; accept it and change the plan before --confirm-pass")
    number = max((int(p.name[len(iter_dir.name + '-plan-'):]) for p in iter_dir.parent.glob(iter_dir.name + '-plan-*')
                  if p.name[len(iter_dir.name + '-plan-'):].isdigit()), default=0) + 1
    child = iter_dir.parent / f"{iter_dir.name}-plan-{number}"
    plan_text = plan.read_bytes().decode("utf-8")
    sha = _sha(plan)
    dispositions_sha = _sha(iter_dir / "dispositions.yaml")
    dispositions = _read(iter_dir / "dispositions.yaml")
    template = "confirm_review.md" if pass_kind == "confirm" else "plan_review.md"
    instructions = (Path(__file__).parent / "dispatch_templates" / template).read_text(encoding="utf-8")
    packet = {"schema_version": 1, "iteration_id": child.name, "pass_kind": pass_kind,
              "parent_iteration": iter_dir.name, "plan_pass": number, "converged_plan_sha256": sha,
              "dispositions_sha256": dispositions_sha,
              "instructions": instructions, "converged_plan": plan_text}
    if pass_kind == "confirm":
        old_dispositions = reviewed_packet.get("dispositions_text")
        if old_dispositions is None:
            old_dispositions = yaml.safe_dump(reviewed_packet["dispositions"], sort_keys=False, allow_unicode=False)
            if hashlib.sha256(old_dispositions.encode("utf-8")).hexdigest() != reviewed_packet["dispositions_sha256"]:
                raise ValueError("latest plan pass lacks an exact dispositions snapshot; run a fresh plan pass before --confirm-pass")
        packet.update(reviewed_plan=reviewed_packet["converged_plan"], accepted_objections=accepted,
                      plan_diff=_diff(reviewed_packet["converged_plan"], plan_text, "converged-plan.yaml"),
                      dispositions_diff=_diff(old_dispositions,
                          (iter_dir / "dispositions.yaml").read_bytes().decode("utf-8"), "dispositions.yaml"))
        goal = _confirm_goal(child.name, instructions, packet)
    else:
        packet.update(dispositions=dispositions,
              dispositions_text=(iter_dir / "dispositions.yaml").read_bytes().decode("utf-8"),
              rejected_or_deferred_ideas=[r for r in dispositions.get("dispositions", [])
                                             if r.get("disposition") in {"rejected", "deferred"}],
              earlier_objections_and_host_responses=data["objections"])
        goal = _child_goal(_read(iter_dir / "goal_packet.yaml"), child.name, instructions, packet)
    child.mkdir()
    if pass_kind == "confirm":
        atomic_write_text(child / "confirm-pass.md", instructions + "\n\n{desired_end_state}\n")
    _write(child / "goal_packet.yaml", goal)
    _write(child / "review-packet.yaml", packet)
    return {"child_dir": child, "number": number, "tier": tier, "plan_sha256": sha,
            "dispositions_sha256": dispositions_sha, "seats": seats, "pass_kind": pass_kind}


def finish_plan_pass(iter_dir: Path, prepared: dict, result: dict) -> dict:
    child = prepared["child_dir"]
    binding = {"pass_kind": prepared.get("pass_kind", "plan"), "parent_iteration": iter_dir.name,
               "plan_pass": prepared["number"], "converged_plan_sha256": prepared["plan_sha256"],
               "dispositions_sha256": prepared["dispositions_sha256"]}
    for path in child.glob("consult-summary*.yaml"):
        summary = _read(path)
        summary.update(binding)
        _write(path, summary)
    result = {**result, **binding}
    if not result.get("ok"):
        return result
    init_objections(iter_dir)
    if binding["pass_kind"] == "confirm":
        holdouts = [r for r in _holdouts(iter_dir) if r["kind"] in CONFIRM_KINDS]
        if holdouts:
            result.update(_needs_user(iter_dir, holdouts))
            for path in child.glob("consult-summary*.yaml"):
                summary = _read(path)
                summary.update(ok=False, status=result["status"], holdouts=holdouts, error=result["error"])
                _write(path, summary)
    elif prepared["tier"] == "deep":
        data = _read(iter_dir / FILENAME)
        if _pass_count(_rounds(iter_dir), data.get("send_back_after_pass", 0)) >= _max_passes(iter_dir.parents[2]):
            holdouts = _holdouts(iter_dir)
            if holdouts:
                result.update(_needs_user(iter_dir, holdouts))
                for path in child.glob("consult-summary*.yaml"):
                    summary = _read(path)
                    summary.update(ok=False, status=result["status"], holdouts=holdouts, error=result["error"])
                    _write(path, summary)
    return result


def check_plan_passes(iter_dir: Path) -> list[str]:
    tier = _tier(iter_dir)
    if tier == "quick":
        return []
    rounds = _rounds(iter_dir)
    plan_rounds = [r for r in rounds if r[2].get("pass_kind") == "plan"]
    if not plan_rounds:
        return [f"{tier} tier requires a sealed plan pass on the current plan"]
    latest = plan_rounds[-1]
    current_sha = _sha(iter_dir / "converged-plan.yaml")
    dispositions_sha = _sha(iter_dir / "dispositions.yaml")
    sources, reviews = _sources(iter_dir)
    reviewed_dispositions = _read(latest[1] / "review-packet.yaml").get("dispositions_sha256")
    dispositions_changed = reviewed_dispositions != dispositions_sha
    errors = []
    if latest[2].get("converged_plan_sha256") != current_sha or dispositions_changed:
        if tier == "deep":
            return ["latest plan pass is stale: the plan or dispositions.yaml changed; "
                    "run a plan pass on the current version"]
        confirmations = [r for r in rounds if r[0] > latest[0] and r[2].get("pass_kind") == "confirm"
                         and r[2].get("converged_plan_sha256") == current_sha
                         and r[2].get("dispositions_sha256") == dispositions_sha]
        if not confirmations:
            errors.append("latest plan pass is stale: the plan or dispositions.yaml changed; run --confirm-pass on the current version")
        elif not any(len(_families(reviews[number])) >= 2 for number, _, _ in confirmations):
            errors.append("confirm pass needs >=2 distinct non-host model families from sealed reviewers; run --confirm-pass")
    if len(_families(reviews[latest[0]])) < 2:
        return ["plan pass needs >=2 distinct non-host model families from sealed reviewers"]
    data = _table(iter_dir, sources)
    holdouts = [r for r in data["objections"]
                if not _resolved(r, rounds, reviews, current_sha, tier, dispositions_sha)]
    return errors + [f"{r['id']}: open objection; host must ask the user and never answer for them" for r in holdouts]
