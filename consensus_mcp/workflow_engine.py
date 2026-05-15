"""Workflow engine — orchestrates contributors per `.consensus/config.yaml`.

Third sub-component of iter-0016. Reads project config, validates, copies
effective config to iteration dir, then dispatches enabled contributors per
workflow.mode (post-review, propose-converge, advisory) using the adapter
layer from consensus_mcp/contributors/.

Per iter-0015 converged-plan Section C, engine outputs:
  - <iter_dir>/effective-config.yaml (copy of applied config)
  - workflow-specific artifacts per phase
  - workflow-specific final-decision artifact

The engine itself is sealed-artifact-aware: contributor outputs already go
through T6 via their adapters; engine just orchestrates the sequence.

Convergence evaluation:
  - unanimous: ALL enabled responsive contributors must goal_satisfied=true
    with no blocking_objections
  - strict-majority: > N/2 (strict; even-N ties refuse)
  - inclusive-majority: >= N/2 (ties pass)
  - advisory: claude (orchestrator) decides regardless of peer votes
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from consensus_mcp import config as cfg
from consensus_mcp.contributors import (
    PHASE_CONVERGE,
    PHASE_PROPOSE,
    PHASE_REVIEW,
    ContributorAdapter,
    DispatchError,
    SealedArtifact,
)
from consensus_mcp.contributors.base import DispatchPacket


class WorkflowError(RuntimeError):
    """Raised on engine-level failures (missing config, all contributors timed out, etc.)."""


@dataclass
class ConvergenceOutcome:
    """Result of evaluating a convergence rule against contributor outputs."""
    converged: bool                        # True if convergence rule passed
    rule: str                              # rule applied (from config.convergence.rule)
    contributors_responsive: list[str]     # contributors that returned SealedArtifact
    contributors_timed_out: list[str]      # contributors that raised DispatchError
    approve_votes: list[str]               # contributors with goal_satisfied=True + no blocking
    block_votes: list[str]                 # contributors with goal_satisfied=False or blocking
    blocking_objection_ids: list[str]      # union of all blocking_objections across contributors
    rationale: str


@dataclass
class IterationOutcome:
    """Engine's final report on an iteration run."""
    iteration_id: str
    workflow_mode: str
    effective_config_path: Path
    contributor_artifacts: dict[str, list[SealedArtifact]] = field(default_factory=dict)
    convergence: ConvergenceOutcome | None = None
    final_artifact_path: Path | None = None
    error: str | None = None


# -------------------------------------------------------------------------
# WorkflowEngine
# -------------------------------------------------------------------------


class WorkflowEngine:
    """Orchestrates an iteration end-to-end per `.consensus/config.yaml`.

    Construction:
      - `adapters`: dict mapping contributor name → ContributorAdapter instance.
        For tests, pass fake adapters. For real runtime, the engine factory
        builds these from config.
      - `config`: the normalized + validated config dict (use `cfg.load(...)`
        or `cfg.synthesize_legacy_config(...)` to produce).
      - `repo_root`: repo root path (for goal_packet paths, archive root, etc.)

    Use `run_iteration(iter_dir, problem_or_target_path, ...)` to execute.
    """

    def __init__(
        self,
        config: dict,
        adapters: dict[str, ContributorAdapter],
        repo_root: Path,
    ):
        self.config = config
        self.adapters = adapters
        self.repo_root = Path(repo_root)
        # Validate the adapter set matches contributors.enabled.
        enabled = config.get("contributors", {}).get("enabled", [])
        missing = [c for c in enabled if c not in adapters]
        if missing:
            raise WorkflowError(
                f"adapters missing for enabled contributors: {missing}. "
                f"Available: {sorted(adapters.keys())}"
            )

    def write_effective_config(self, iteration_dir: Path) -> Path:
        """Copy normalized config to iter_dir/effective-config.yaml. Returns path."""
        out = Path(iteration_dir) / "effective-config.yaml"
        out.write_text(
            yaml.safe_dump(self.config, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        return out

    def run_iteration(
        self,
        iteration_dir: Path,
        goal_packet_path: Path,
        target_path: Path,
    ) -> IterationOutcome:
        """Dispatch contributors per workflow.mode. Returns IterationOutcome.

        `target_path` is:
          - workflow #3: the patch/diff/file under review
          - workflow #4: the problem statement (blind phase input)
          - advisory:    the artifact contributors recommend on
        """
        iteration_dir = Path(iteration_dir)
        iteration_dir.mkdir(parents=True, exist_ok=True)
        effective_config_path = self.write_effective_config(iteration_dir)
        mode = self.config["workflow"]["mode"]
        iter_id = iteration_dir.name
        outcome = IterationOutcome(
            iteration_id=iter_id,
            workflow_mode=mode,
            effective_config_path=effective_config_path,
        )

        try:
            if mode == cfg.WORKFLOW_POST_REVIEW:
                self._run_workflow_3(iteration_dir, goal_packet_path, target_path, outcome)
            elif mode == cfg.WORKFLOW_PROPOSE_CONVERGE:
                self._run_workflow_4(iteration_dir, goal_packet_path, target_path, outcome)
            elif mode == cfg.WORKFLOW_ADVISORY:
                self._run_advisory(iteration_dir, goal_packet_path, target_path, outcome)
            elif mode == cfg.WORKFLOW_AUTONOMOUS_EXECUTE:
                # iter-workflow-abc-introduce: v1.14.4 ships the contract
                # (alias, validators, scope_check helper, schema,
                # autonomy_contract block) but NOT the multi-iteration
                # auto-execution loop. Engine path lands in v1.15.0
                # (named blocker: cross-platform interrupt-file watching
                # validation + integration tests with real peer dispatches +
                # autonomy-ledger replay design). Operators can write and
                # validate Workflow C goal_packets in v1.14.4; running them
                # surfaces this clear NotImplementedError.
                raise NotImplementedError(
                    "Workflow C (autonomous-execute) engine path is not "
                    "implemented in v1.14.4; the contract (config alias, "
                    "validators, scope_check, autonomy_contract schema) "
                    "ships in v1.14.4 for staging only. Multi-iteration "
                    "auto-execution lands in v1.15.0. See "
                    "docs/workflows/workflow-c-autonomous.md for status."
                )
            else:
                raise WorkflowError(f"unknown workflow.mode {mode!r}")
        except WorkflowError as exc:
            outcome.error = str(exc)
        except NotImplementedError as exc:
            outcome.error = f"NotImplementedError: {exc}"
        return outcome

    # ---- Workflow runners ----

    def _run_workflow_3(
        self,
        iteration_dir: Path,
        goal_packet_path: Path,
        target_path: Path,
        outcome: IterationOutcome,
    ) -> None:
        """Post-review: claude is assumed to have already produced target;
        non-claude contributors review it in parallel (conceptually — the
        engine dispatches them sequentially within this synchronous loop,
        but they're independent of each other)."""
        enabled = self.config["contributors"]["enabled"]
        # Dispatch all non-claude contributors with review packets.
        review_artifacts: list[SealedArtifact] = []
        for c in enabled:
            if c == cfg.CLAUDE:
                # Claude's "implementation" is the target_path; engine doesn't
                # re-emit it. (Real runtime: claude wrote target_path before
                # calling the engine.)
                continue
            adapter = self.adapters[c]
            try:
                art = adapter.review(iteration_dir, goal_packet_path, target_path)
            except DispatchError as exc:
                outcome.contributor_artifacts.setdefault(c, [])
                # Record timeout per workflow.timeout_policy.
                self._record_timed_out(c, exc, outcome)
                continue
            review_artifacts.append(art)
            outcome.contributor_artifacts.setdefault(c, []).append(art)
        # codex-rev-001 round-1 BLOCKING fix: workflow #3 dispatches only the
        # non-claude reviewers; claude is the orchestrator/author, not a voter.
        # Pass the dispatched set as eligible_voters so claude isn't falsely
        # reported as timed-out (which would break unanimous + treat-as-blocking).
        post_review_voters = [c for c in enabled if c != cfg.CLAUDE]
        outcome.convergence = self._evaluate_convergence(
            review_artifacts, outcome, eligible_voters=post_review_voters
        )
        # Final artifact for workflow #3 is the target itself (already written
        # by claude pre-engine); engine doesn't synthesize.
        outcome.final_artifact_path = target_path

    def _run_workflow_4(
        self,
        iteration_dir: Path,
        goal_packet_path: Path,
        problem_statement_path: Path,
        outcome: IterationOutcome,
    ) -> None:
        """Propose-converge with blind-first-reveal independence.

        Phases:
          1. BLIND PROPOSAL: all enabled contributors propose against the
             problem statement; none see each other's outputs.
          2. REVEAL + CONVERGE round N: bundle all blind proposals into a
             convergence packet; dispatch each contributor in converge phase.
             Each contributor proposes the synthesized plan based on full
             visibility.
          3. Evaluate convergence rule; if passed → seal final converged_plan;
             else loop to step 2 up to max_convergence_rounds.
        """
        enabled = self.config["contributors"]["enabled"]
        max_rounds = self.config["workflow"]["max_convergence_rounds"]

        # Phase 1: blind proposals (sequential dispatch in engine; the
        # contributors themselves don't see each other's outputs because
        # the input to each is just the problem statement).
        proposals: list[SealedArtifact] = []
        for c in enabled:
            adapter = self.adapters[c]
            try:
                art = adapter.propose(iteration_dir, goal_packet_path, problem_statement_path)
            except DispatchError as exc:
                self._record_timed_out(c, exc, outcome)
                continue
            proposals.append(art)
            outcome.contributor_artifacts.setdefault(c, []).append(art)

        if not proposals:
            raise WorkflowError(
                "workflow #4: no contributors produced blind proposals; "
                "cannot proceed to convergence"
            )

        # Phase 2-3: convergence rounds.
        proposal_paths = [str(p.sealed_path) for p in proposals]
        last_convergence_artifacts: list[SealedArtifact] = []
        for round_n in range(1, max_rounds + 1):
            convergence_packet_path = self._build_convergence_packet(
                iteration_dir, proposal_paths, round_n
            )
            convergence_artifacts: list[SealedArtifact] = []
            for c in enabled:
                if c not in self.adapters:
                    continue
                adapter = self.adapters[c]
                try:
                    art = adapter.converge(
                        iteration_dir, goal_packet_path,
                        convergence_packet_path, round_number=round_n,
                    )
                except DispatchError as exc:
                    self._record_timed_out(c, exc, outcome)
                    continue
                convergence_artifacts.append(art)
                outcome.contributor_artifacts.setdefault(c, []).append(art)
            last_convergence_artifacts = convergence_artifacts
            conv = self._evaluate_convergence(convergence_artifacts, outcome)
            if conv.converged:
                outcome.convergence = conv
                # Seal final converged plan.
                final_path = self._seal_converged_plan(
                    iteration_dir, convergence_artifacts, conv, round_n
                )
                outcome.final_artifact_path = final_path
                return
            # gemini-rev-001 round-1 BLOCKING fix: EXTEND not replace.
            # Subsequent rounds must see ALL prior artifacts (original blind
            # proposals + each round's convergence views) to avoid losing
            # historical context. Replacing the list discards the blind
            # proposals after round 1, defeating workflow #4's purpose.
            proposal_paths.extend(str(a.sealed_path) for a in convergence_artifacts)

        # max rounds reached without convergence; outcome records final state.
        outcome.convergence = self._evaluate_convergence(last_convergence_artifacts, outcome)
        outcome.error = (
            f"workflow #4: convergence not reached after {max_rounds} rounds"
        )

    def _run_advisory(
        self,
        iteration_dir: Path,
        goal_packet_path: Path,
        target_path: Path,
        outcome: IterationOutcome,
    ) -> None:
        """Advisory: all contributors produce recommendations; claude decides.

        For this engine, "claude decides" means: collect everyone's outputs,
        evaluate convergence rule (always 'advisory' here), and surface the
        contributor artifacts to the outcome. The orchestrator (real claude)
        consumes the outcome and makes the final call externally.
        """
        enabled = self.config["contributors"]["enabled"]
        artifacts: list[SealedArtifact] = []
        for c in enabled:
            adapter = self.adapters[c]
            try:
                art = adapter.review(iteration_dir, goal_packet_path, target_path)
            except DispatchError as exc:
                self._record_timed_out(c, exc, outcome)
                continue
            artifacts.append(art)
            outcome.contributor_artifacts.setdefault(c, []).append(art)
        # codex-rev-002 round-1 fix: use _artifact_contributor_key for
        # advisory reporting too (prior version used adapter.name which
        # breaks for fake adapters or any wrapper with mismatched names).
        artifact_keys = [self._artifact_contributor_key(a, outcome) for a in artifacts]
        responsive = list(dict.fromkeys(artifact_keys))
        timed_out = [c for c in enabled if c not in responsive]
        approve = [k for a, k in zip(artifacts, artifact_keys) if a.parsed.get("goal_satisfied")]
        block = [k for a, k in zip(artifacts, artifact_keys) if not a.parsed.get("goal_satisfied")]
        outcome.convergence = ConvergenceOutcome(
            converged=True,
            rule=cfg.CONVERGE_ADVISORY,
            contributors_responsive=responsive,
            contributors_timed_out=timed_out,
            approve_votes=approve,
            block_votes=block,
            blocking_objection_ids=self._collect_blocking_ids(artifacts),
            rationale="advisory: claude decides regardless of peer votes",
        )

    # ---- Helpers ----

    def _build_convergence_packet(
        self,
        iteration_dir: Path,
        proposal_paths: list[str],
        round_number: int,
    ) -> Path:
        """Bundle blind proposals into a convergence review-packet."""
        touched: dict[str, str] = {}
        for p in proposal_paths:
            p_path = Path(p)
            if p_path.exists():
                # store as repo-relative posix path
                try:
                    rel = str(p_path.relative_to(self.repo_root)).replace("\\", "/")
                except ValueError:
                    rel = p_path.name
                touched[rel] = p_path.read_text(encoding="utf-8")
        packet = {
            "defect_target": {
                "files": list(touched.keys()),
                "base_sha": "HEAD",
                "touched_files_contents": touched,
            },
            "schema_version": 1,
            "iteration_id": iteration_dir.name,
            "convergence_round": round_number,
        }
        out = iteration_dir / f"convergence-packet-round-{round_number}.yaml"
        out.write_text(
            yaml.safe_dump(packet, sort_keys=False, default_flow_style=False, allow_unicode=True, width=10000),
            encoding="utf-8",
        )
        return out

    def _record_timed_out(self, contributor: str, exc: Exception, outcome: IterationOutcome) -> None:
        """Record that a contributor timed out by ensuring its key exists in
        the outcome's artifacts dict but without adding an artifact. The
        convergence evaluator later detects the timeout by comparing the set
        of responsive contributors (artifacts present) against enabled
        (configured) and applies workflow.timeout_policy accordingly."""
        outcome.contributor_artifacts.setdefault(contributor, [])

    def _artifact_contributor_key(self, art: SealedArtifact, outcome: IterationOutcome) -> str:
        """Return the contributor_KEY (from config.contributors.enabled) for an
        artifact, falling back to art.contributor. Engine tracks artifacts under
        their config key (e.g., 'codex') even when the adapter's internal name
        differs (e.g., 'fake-block')."""
        for key, arts in outcome.contributor_artifacts.items():
            if art in arts:
                return key
        return art.contributor

    def _evaluate_convergence(
        self,
        artifacts: list[SealedArtifact],
        outcome: IterationOutcome,
        eligible_voters: list[str] | None = None,
    ) -> ConvergenceOutcome:
        """Evaluate convergence rule against the contributor outputs.

        `eligible_voters` is the set of contributors expected to produce
        artifacts for this convergence evaluation. Defaults to
        `config.contributors.enabled` for workflow #4 (all contributors
        participate). Workflow #3 passes [non-claude...] because the
        orchestrator (claude) doesn't dispatch in post-review (codex-rev-001
        round-1 fix).

        Per workflow.timeout_policy:
          - treat-as-no-vote: timed-out contributors are absent from the vote count
          - treat-as-blocking: timed-out contributors count as block votes
          - shrink-quorum: timed-out contributors reduce N (the denominator)
        """
        rule = self.config["convergence"]["rule"]
        timeout_policy = self.config["workflow"]["timeout_policy"]
        enabled = eligible_voters if eligible_voters is not None else self.config["contributors"]["enabled"]

        # Map each artifact to its config contributor KEY (not adapter.name).
        artifact_keys = [self._artifact_contributor_key(a, outcome) for a in artifacts]
        responsive = list(dict.fromkeys(artifact_keys))  # preserve order, dedupe
        timed_out = [c for c in enabled if c not in responsive]

        # Per-contributor vote evaluation, indexed by contributor KEY.
        approve_votes: list[str] = []
        block_votes: list[str] = []
        blocking_ids: list[str] = []
        for art, key in zip(artifacts, artifact_keys):
            p = art.parsed
            if p.get("goal_satisfied") and not p.get("blocking_objections"):
                approve_votes.append(key)
            else:
                block_votes.append(key)
            for bid in (p.get("blocking_objections") or []):
                blocking_ids.append(bid)

        # Apply timeout policy to the effective denominator + extra block votes.
        if timeout_policy == cfg.TIMEOUT_BLOCKING:
            block_votes.extend(timed_out)
            n = len(enabled)
        elif timeout_policy == cfg.TIMEOUT_SHRINK:
            n = len(responsive)
        else:  # TIMEOUT_NO_VOTE
            n = len(enabled)

        # Apply convergence rule.
        n_approve = len(approve_votes)
        n_block = len(block_votes)
        converged: bool
        rationale: str
        if rule == cfg.CONVERGE_UNANIMOUS:
            converged = n_block == 0 and n_approve == n and n > 0
            rationale = f"unanimous: need all {n} approve; got {n_approve} approve, {n_block} block"
        elif rule == cfg.CONVERGE_STRICT_MAJ:
            threshold = (n // 2) + 1
            converged = n_approve >= threshold and not blocking_ids
            rationale = f"strict-majority: need >={threshold}/{n} approve and no blocking; got {n_approve} approve, {len(blocking_ids)} blocking-id(s)"
        elif rule == cfg.CONVERGE_INCL_MAJ:
            threshold = math.ceil(n / 2)
            converged = n_approve >= threshold and not blocking_ids
            rationale = f"inclusive-majority: need >={threshold}/{n} approve and no blocking; got {n_approve} approve, {len(blocking_ids)} blocking-id(s)"
        elif rule == cfg.CONVERGE_ADVISORY:
            converged = True
            rationale = "advisory: claude decides regardless"
        else:
            converged = False
            rationale = f"unknown convergence rule {rule!r}"

        return ConvergenceOutcome(
            converged=converged,
            rule=rule,
            contributors_responsive=responsive,
            contributors_timed_out=timed_out,
            approve_votes=approve_votes,
            block_votes=block_votes,
            blocking_objection_ids=blocking_ids,
            rationale=rationale,
        )

    def _collect_blocking_ids(self, artifacts: list[SealedArtifact]) -> list[str]:
        ids: list[str] = []
        for a in artifacts:
            for bid in (a.parsed.get("blocking_objections") or []):
                ids.append(bid)
        return ids

    def _seal_converged_plan(
        self,
        iteration_dir: Path,
        convergence_artifacts: list[SealedArtifact],
        conv: ConvergenceOutcome,
        round_number: int,
    ) -> Path:
        """Write converged-plan.yaml summarizing the converged round."""
        plan = {
            "iteration_id": iteration_dir.name,
            "workflow_mode": self.config["workflow"]["mode"],
            "convergence_rule": conv.rule,
            "converged_at_round": round_number,
            "contributors_responsive": conv.contributors_responsive,
            "contributors_timed_out": conv.contributors_timed_out,
            "approve_votes": conv.approve_votes,
            "rationale": conv.rationale,
            "convergence_round_artifacts": [
                {
                    "contributor": a.contributor,
                    "pass_id": a.pass_id,
                    "sealed_path": str(a.sealed_path),
                    "packet_sha256": a.packet_sha256,
                }
                for a in convergence_artifacts
            ],
        }
        out = iteration_dir / "converged-plan.yaml"
        out.write_text(
            yaml.safe_dump(plan, sort_keys=False, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
        return out
