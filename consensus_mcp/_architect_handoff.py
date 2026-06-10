"""HANDOFF.md renderer - 'the repo is the brain' (spec section 7).

Regenerated after every sealed artifact. The architect reads THIS digest,
never the whole repo - the load-bearing cost optimization of workflow D.
Rolling window (consult Q7): last WINDOW cycles inline; older cycles get a
one-line pointer so HANDOFF cost stays flat across cycles.
"""
from __future__ import annotations

from pathlib import Path

from consensus_mcp import _architect_paths as ap
from consensus_mcp._atomic_io import atomic_write_text

WINDOW = 5


def _family(name: str) -> str:
    # family == contributor name for the builtin set; profile-aware family
    # resolution lives in config validation, which already rejected illegal
    # maps. The HANDOFF flag only needs the simple comparison.
    return name


def render_handoff(goal: Path, *, roles: dict) -> str:
    goal = Path(goal)
    spec = ap._read_yaml_or_empty(ap.latest_spec_path(goal))
    approval = ap._read_yaml_or_empty(goal / ap.SPEC_APPROVAL_FILENAME)
    lines: list[str] = []
    lines.append("# HANDOFF - architect-build goal state")
    lines.append("")
    lines.append(f"roles: architect={roles.get('architect')} "
                 f"builder={roles.get('builder')} reviewer={roles.get('reviewer')}")
    if (
        _family(roles.get("reviewer", "")) == _family(roles.get("builder", ""))
        and _family(roles.get("architect", "")) != _family(roles.get("builder", ""))
    ):
        lines.append(
            "NOTE: the architect is the ONLY cross-family signer vs the "
            "builder (reviewer shares the builder's family). Consult Q2 "
            "transparency flag."
        )
    lines.append("")
    lines.append("## Spec")
    lines.append(f"spec file: {ap.latest_spec_path(goal).name}")
    lines.append(f"spec payload_sha256: {spec.get('payload_sha256', 'UNSEALED')}")
    lines.append(f"approved: {'yes' if approval else 'NO - spec gate pending'}")
    if approval:
        lines.append(f"base_sha: {approval.get('base_sha')}")
    body = spec.get("body", "")
    lines.append("")
    lines.append(str(body))
    lines.append("")
    lines.append("## Cycle history")
    cycles = sorted(
        (
            int(m.group(1)), p
        )
        for p in goal.iterdir()
        if (m := ap.CYCLE_DIR_RE.match(p.name))
    ) if goal.exists() else []
    older = [c for c in cycles if c[0] <= len(cycles) - WINDOW]
    recent = [c for c in cycles if c not in older]
    if older:
        lines.append(
            f"(older cycles 1..{older[-1][0]} summarized - raw artifacts in "
            f"their cycle-N/ dirs)"
        )
        for n, p in older:
            ruling = ap._read_yaml_or_empty(p / ap.RULING_FILENAME)
            lines.append(
                f"- cycle-{n}: ruling={ruling.get('disposition', 'open')}"
            )
    for n, p in recent:
        build = ap._read_yaml_or_empty(p / ap.BUILD_RESULT_FILENAME)
        verification = ap._read_yaml_or_empty(p / ap.VERIFICATION_FILENAME)
        review = ap._read_yaml_or_empty(p / ap.REVIEW_FILENAME)
        ruling = ap._read_yaml_or_empty(p / ap.RULING_FILENAME)
        lines.append("")
        lines.append(f"### cycle-{n}")
        lines.append(f"- build: {build.get('summary', '(pending)')}")
        if build.get("pushback"):
            lines.append(f"- PUSHBACK: {build['pushback']}")
        lines.append(f"- lane_head_sha: {build.get('lane_head_sha', '-')}")
        if verification:
            lines.append(
                f"- verification: {'GREEN' if verification.get('passed') else 'RED'}"
            )
        if review:
            lines.append(f"- review: {review.get('verdict', 'present')}")
        lines.append(
            f"- ruling: {ruling.get('disposition', '(pending)')}"
            + (f" - {ruling.get('reason')}" if ruling.get("reason") else "")
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def write_handoff(goal: Path, *, roles: dict) -> Path:
    out = Path(goal) / ap.HANDOFF_FILENAME
    atomic_write_text(out, render_handoff(goal, roles=roles))
    return out
