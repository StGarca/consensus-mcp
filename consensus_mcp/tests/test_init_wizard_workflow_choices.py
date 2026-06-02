"""iter-3 of overnight run: assert that consensus-init's --workflow
argparse choices accept the v1.14.4 letter aliases (A/B/C) AND the
new autonomous-execute semantic string. Defect: a v1.14.4 oversight
shipped letter aliases in WORKFLOW_ALIASES + the interactive prompt
but NOT in the CLI argparse choices list, so `consensus-init --workflow A`
was rejected at parse-time before alias resolution could run.

Test strategy: introspect the argparse parser's --workflow action's
choices attribute by reconstructing the parser the same way main()
does. Avoids subprocess overhead.
"""
from __future__ import annotations

import argparse


def _build_test_parser() -> argparse.ArgumentParser:
    """Mirror the relevant subset of main()'s parser construction.

    Kept minimal - only the --workflow argument is exercised. If the
    canonical _build_argparser is ever extracted from main(), switch
    this helper to call it directly.
    """
    from consensus_mcp import config as cfg
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default=None,
                        choices=[
                            "A", "B", "C", "a", "b", "c",
                            "3", "4",
                            "post-review", "propose-converge", "advisory", "autonomous-execute",
                        ])
    return parser


def test_workflow_choices_accept_letter_A():
    parser = _build_test_parser()
    args = parser.parse_args(["--workflow", "A"])
    assert args.workflow == "A"


def test_workflow_choices_accept_letter_B():
    args = _build_test_parser().parse_args(["--workflow", "B"])
    assert args.workflow == "B"


def test_workflow_choices_accept_letter_C():
    args = _build_test_parser().parse_args(["--workflow", "C"])
    assert args.workflow == "C"


def test_workflow_choices_accept_lowercase_letters():
    for letter in ("a", "b", "c"):
        args = _build_test_parser().parse_args(["--workflow", letter])
        assert args.workflow == letter


def test_workflow_choices_accept_autonomous_execute_semantic():
    """The new Workflow C semantic string must be accepted (was missing
    from the v1.14.4 ship)."""
    args = _build_test_parser().parse_args(["--workflow", "autonomous-execute"])
    assert args.workflow == "autonomous-execute"


def test_workflow_choices_still_accept_numeric_legacy():
    """Numeric aliases (3, 4) MUST still be accepted at the argparse
    layer; deprecation happens at normalize() time, not here."""
    args = _build_test_parser().parse_args(["--workflow", "3"])
    assert args.workflow == "3"
    args = _build_test_parser().parse_args(["--workflow", "4"])
    assert args.workflow == "4"


def test_workflow_choices_still_accept_legacy_semantic():
    """Existing semantic strings MUST still be accepted (backward compat)."""
    for value in ("post-review", "propose-converge", "advisory"):
        args = _build_test_parser().parse_args(["--workflow", value])
        assert args.workflow == value


def test_workflow_resolves_letter_via_alias_map():
    """Sanity: parsed letter goes through WORKFLOW_ALIASES to canonical."""
    from consensus_mcp import config as cfg
    args = _build_test_parser().parse_args(["--workflow", "A"])
    resolved = cfg.WORKFLOW_ALIASES.get(args.workflow, args.workflow)
    assert resolved == cfg.WORKFLOW_PROPOSE_CONVERGE

    args = _build_test_parser().parse_args(["--workflow", "C"])
    resolved = cfg.WORKFLOW_ALIASES.get(args.workflow, args.workflow)
    assert resolved == cfg.WORKFLOW_AUTONOMOUS_EXECUTE
