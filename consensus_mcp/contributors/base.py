"""ContributorAdapter abstract base + supporting types.

Per iter-0015 converged-plan Section C, every contributor (claude, codex,
gemini, future AIs) implements this interface. The workflow engine calls
`dispatch(phase, packet)` to get a sealed artifact regardless of how the
underlying contributor works (in-process for claude, subprocess for codex/
gemini, RPC for future remote AIs).

Phases:
  - PROPOSE: contributor writes its own design/plan against a problem statement
    (workflow #4 blind phase). Receives only the problem statement; no peer
    proposals visible.
  - REVIEW: contributor evaluates an already-written change against acceptance
    gates (workflow #3 post-review, or convergence in advisory mode).
  - CONVERGE: contributor sees all sibling proposals revealed and proposes the
    synthesized plan (workflow #4 reveal phase).

The packet structure passed to `dispatch` mirrors the existing goal_packet +
review_packet pair used by the dispatch helpers, but with phase-specific
fields that the engine fills in.
"""
from __future__ import annotations

import contextlib
import io
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


# --- Hazard H1: thread-safe stdout capture for concurrent dispatch ----------
# The peer adapters capture a dispatch's stdout to parse its JSON result. The
# original `contextlib.redirect_stdout(buf)` swaps the PROCESS-GLOBAL sys.stdout
# for the whole (multi-minute) dispatch; once contributors fan out concurrently
# (consult-ratified parallel dispatch) those swaps race and cross-capture each
# other's output. The proxy below is installed as sys.stdout ONCE and never
# swapped per-call: each thread routes its writes to its own buffer via a
# thread-local, so concurrent captures stay isolated.

class _ThreadLocalStdoutProxy:
    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def _target(self):
        buf = getattr(self._local, "buf", None)
        return buf if buf is not None else self._real

    def write(self, s):
        return self._target().write(s)

    def flush(self):
        return self._target().flush()

    def __getattr__(self, name):
        # isatty / encoding / fileno / etc. -> the active target.
        return getattr(self._target(), name)


_STDOUT_PROXY_LOCK = threading.Lock()


def _ensure_stdout_proxy() -> "_ThreadLocalStdoutProxy":
    """Install the proxy as sys.stdout if it isn't already. Re-wraps when
    something else (e.g. pytest's per-test capture) has replaced sys.stdout."""
    with _STDOUT_PROXY_LOCK:
        cur = sys.stdout
        if not isinstance(cur, _ThreadLocalStdoutProxy):
            cur = _ThreadLocalStdoutProxy(cur)
            sys.stdout = cur
        return cur


@contextlib.contextmanager
def capture_stdout_threadsafe():
    """Capture THIS thread's stdout into a fresh StringIO WITHOUT swapping the
    process-global sys.stdout per call -- safe under concurrent dispatch (H1).
    Yields the StringIO buffer."""
    buf = io.StringIO()
    proxy = _ensure_stdout_proxy()
    prev = getattr(proxy._local, "buf", None)
    proxy._local.buf = buf
    try:
        yield buf
    finally:
        proxy._local.buf = prev


# Phase identifiers as Literal type alias.
Phase = Literal["propose", "review", "converge"]

PHASE_PROPOSE: Phase = "propose"
PHASE_REVIEW: Phase = "review"
PHASE_CONVERGE: Phase = "converge"


class DispatchError(RuntimeError):
    """Raised when a contributor's dispatch fails (subprocess error, parse fail,
    file IO error, etc.). The engine decides how to handle (per timeout_policy)."""


@dataclass(frozen=True)
class SealedArtifact:
    """The outcome of a successful contributor dispatch.

    Mirrors the dict shape returned by `reviewer.dispatch_codex` MCP tool
    plus the engine's normalization. `sealed_path` is the on-disk YAML the
    contributor wrote; `parsed` is the loaded dict (findings, goal_satisfied, ...).
    """
    contributor: str            # 'claude' | 'codex' | 'gemini' | future
    phase: Phase
    pass_id: str
    sealed_path: Path
    archive_sealed_path: Path | None
    packet_sha256: str
    parsed: dict


@dataclass(frozen=True)
class DispatchPacket:
    """Input to a contributor's dispatch.

    Carries the phase-specific configuration the engine has assembled.
    """
    phase: Phase
    contributor: str
    iteration_dir: Path
    goal_packet_path: Path
    review_target_path: Path | None
    reviewer_id: str | None     # if None, contributor adapter derives
    pass_id: str | None         # if None, contributor adapter derives
    timeout_seconds: int = 600
    # Phase-specific extras (e.g., {model: 'Gemini 3.1 Pro (High)'} for gemini)
    adapter_options: dict | None = None


class ContributorAdapter(ABC):
    """Abstract base for all contributors.

    Concrete subclasses override `dispatch` (and conventionally expose
    `propose`, `review`, `converge` thin wrappers that build the packet and
    call `dispatch` with the right phase).
    """

    #: Stable identifier - used in reviewer_id derivation, config matching, etc.
    name: str = ""

    def __init__(self, adapter_config: dict | None = None):
        """`adapter_config` is the sub-dict from `.consensus/config.yaml`'s
        `contributors.adapters.<name>` block (or None for synthetic tests)."""
        self.adapter_config = adapter_config or {}

    @abstractmethod
    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        """Run the contributor for the given phase + packet. Return sealed result.

        Must raise DispatchError on any failure (subprocess crash, parse fail,
        watchdog timeout, etc.). The engine handles fallout per
        `workflow.timeout_policy`.
        """

    # ----- Phase-specific helpers (default implementations build a packet
    # and call dispatch). Subclasses MAY override for adapter-specific quirks. -----

    def propose(
        self,
        iteration_dir: Path,
        goal_packet_path: Path,
        problem_statement_path: Path,
        *,
        timeout_seconds: int = 600,
    ) -> SealedArtifact:
        """Workflow #4 BLIND PROPOSAL phase. `problem_statement_path` is the
        shared design challenge; no peer proposals visible."""
        packet = DispatchPacket(
            phase=PHASE_PROPOSE,
            contributor=self.name,
            iteration_dir=iteration_dir,
            goal_packet_path=goal_packet_path,
            review_target_path=problem_statement_path,
            reviewer_id=None,
            pass_id=None,
            timeout_seconds=timeout_seconds,
            adapter_options=None,
        )
        return self.dispatch(packet)

    def review(
        self,
        iteration_dir: Path,
        goal_packet_path: Path,
        review_target_path: Path,
        *,
        timeout_seconds: int = 600,
    ) -> SealedArtifact:
        """Workflow #3 / post-review phase. Existing review-shaped dispatch."""
        packet = DispatchPacket(
            phase=PHASE_REVIEW,
            contributor=self.name,
            iteration_dir=iteration_dir,
            goal_packet_path=goal_packet_path,
            review_target_path=review_target_path,
            reviewer_id=None,
            pass_id=None,
            timeout_seconds=timeout_seconds,
            adapter_options=None,
        )
        return self.dispatch(packet)

    def converge(
        self,
        iteration_dir: Path,
        goal_packet_path: Path,
        convergence_packet_path: Path,
        round_number: int = 1,
        *,
        timeout_seconds: int = 600,
    ) -> SealedArtifact:
        """Workflow #4 REVEAL phase. `convergence_packet_path` contains all
        revealed sibling proposals."""
        packet = DispatchPacket(
            phase=PHASE_CONVERGE,
            contributor=self.name,
            iteration_dir=iteration_dir,
            goal_packet_path=goal_packet_path,
            review_target_path=convergence_packet_path,
            reviewer_id=None,
            pass_id=None,
            timeout_seconds=timeout_seconds,
            adapter_options={"round_number": round_number},
        )
        return self.dispatch(packet)


class FakeAlwaysApprove(ContributorAdapter):
    """Test adapter that always returns a clean approval. Used in engine tests."""

    name = "fake-approve"

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        from consensus_mcp.contributors.base import SealedArtifact
        out_path = packet.iteration_dir / f"{self.name}-{packet.phase}.yaml"
        parsed = {
            "iteration_id": packet.iteration_dir.name,
            "reviewer_id": f"{self.name}-{packet.contributor}-1",
            "pass_id": f"{self.name}-{packet.contributor}-1-pass1",
            "findings": [],
            "goal_satisfied": True,
            "goal_satisfied_rationale": f"FakeAlwaysApprove for {packet.phase}",
            "blocking_objections": [],
        }
        import yaml
        out_path.write_text(yaml.safe_dump(parsed, sort_keys=False, default_flow_style=False), encoding="utf-8")
        import hashlib
        return SealedArtifact(
            contributor=self.name,
            phase=packet.phase,
            pass_id=parsed["pass_id"],
            sealed_path=out_path,
            archive_sealed_path=None,
            packet_sha256=hashlib.sha256(out_path.read_bytes()).hexdigest(),
            parsed=parsed,
        )


class FakeAlwaysBlock(ContributorAdapter):
    """Test adapter that always returns a blocking finding."""

    name = "fake-block"

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        out_path = packet.iteration_dir / f"{self.name}-{packet.phase}.yaml"
        finding_id = f"{self.name}-rev-001"
        parsed = {
            "iteration_id": packet.iteration_dir.name,
            "reviewer_id": f"{self.name}-{packet.contributor}-1",
            "pass_id": f"{self.name}-{packet.contributor}-1-pass1",
            "findings": [{
                "id": finding_id,
                "severity": "blocking",
                "summary": f"FakeAlwaysBlock objects to {packet.phase}",
                "citation": "<fake>",
                "risk": "test-only",
                "recommendation": "do not ship",
            }],
            "goal_satisfied": False,
            "goal_satisfied_rationale": "FakeAlwaysBlock by construction",
            "blocking_objections": [finding_id],
        }
        import yaml
        out_path.write_text(yaml.safe_dump(parsed, sort_keys=False, default_flow_style=False), encoding="utf-8")
        import hashlib
        return SealedArtifact(
            contributor=self.name,
            phase=packet.phase,
            pass_id=parsed["pass_id"],
            sealed_path=out_path,
            archive_sealed_path=None,
            packet_sha256=hashlib.sha256(out_path.read_bytes()).hexdigest(),
            parsed=parsed,
        )


class FakeRaisesDispatchError(ContributorAdapter):
    """Test adapter that always raises DispatchError. For timeout-policy tests."""

    name = "fake-raise"

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        raise DispatchError(f"FakeRaisesDispatchError for {packet.phase} (by construction)")
