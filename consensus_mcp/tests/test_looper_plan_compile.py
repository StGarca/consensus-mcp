"""compile.py - trimmed validator/renderer + stub synthesis (Tasks 2-3)."""
import textwrap

import pytest

from consensus_mcp.looper_plan import compile as lc


def _write(tmp_path, body):
    p = tmp_path / "loop.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


VALID = """
version: 1
meta: {name: t}
goal:
  statement: do x
  definition_of_done: x done
  verification:
    - {id: build, type: programmatic, check: ["true"], expect: exit_zero}
host: {cli: codex, model: m, invoke: ["codex","exec"]}
council:
  - {id: r1, role: judge, cli: claude, model: o, invoke: ["claude","-p"]}
gates:
  plan_gate: {when: after_plan, members: [r1], verdict_policy: revise_until_clean, verdict_source: r1, criteria: [build], max_revisions: 3}
  delivery_gate: {when: after_each_delivery, members: [r1], verdict_policy: revise_until_clean, verdict_source: r1, criteria: [build], max_revisions: 3}
loop_control: {max_iterations: 8}
workspace: {dir: ./loop-workspace}
"""


def test_compile_plan_returns_resolved_and_markdown(tmp_path):
    resolved, md = lc.compile_plan(_write(tmp_path, VALID))
    assert resolved["criteria_by_id"]["build"]["type"] == "programmatic"
    assert "## Flow Preview" in md


def test_reviewer_only_revise_until_clean_rejected(tmp_path):
    bad = VALID.replace("role: judge", "role: reviewer")
    with pytest.raises(lc.LooperError):
        lc.compile_plan(_write(tmp_path, bad))


def test_duplicate_criteria_id_rejected(tmp_path):
    bad = VALID.replace(
        '    - {id: build, type: programmatic, check: ["true"], expect: exit_zero}',
        '    - {id: build, type: programmatic, check: ["true"], expect: exit_zero}\n'
        "    - {id: build, type: human, prompt: again}")
    with pytest.raises(lc.LooperError):
        lc.compile_plan(_write(tmp_path, bad))


def test_synthesize_stub_fields_makes_coached_only_spec_compile(tmp_path):
    coached = {
        "version": 1,
        "meta": {"name": "g1"},
        "goal": {"statement": "do x", "definition_of_done": "x done",
                 "verification": [{"id": "build", "type": "programmatic",
                                   "check": ["pytest", "-q"], "expect": "exit_zero"}]},
        "loop_control": {"max_iterations": 8},
    }
    roles = {"architect": "claude", "builder": "codex", "reviewer": "codex"}
    full = lc.synthesize_stub_fields(coached, roles)
    import yaml
    p = tmp_path / "loop.yaml"
    p.write_text(yaml.safe_dump(full), encoding="utf-8")
    resolved, _ = lc.compile_plan(p)             # must not raise
    assert resolved["execution"]["mode"] == "orchestrated"
    assert resolved["execution"]["isolation"] == "worktree"
    assert resolved["council"], "council stub-seeded from roles"
    assert "plan_gate" in resolved["gates"] and "delivery_gate" in resolved["gates"]
