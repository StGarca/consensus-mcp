"""Claude contributor adapter - orchestrator-self artifact emission.

Per iter-0015 converged-plan Section C: claude IS the orchestrator running the
loop, so the "adapter" for claude doesn't spawn a subprocess. It accepts a
callback that produces the proposal/review/converge artifact and seals it via
T6 (review.write_and_seal) for symmetry with codex/gemini sealed outputs.

For tests: pass `proposal_callback=lambda packet: {...dict...}` to inject
deterministic claude artifacts.

For real runtime: the workflow engine intercepts claude's phases and writes
the artifact based on the conversation/orchestration state. This adapter is
the "shim" that gives claude a uniform ContributorAdapter face.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from consensus_mcp.contributors.base import (
    ContributorAdapter,
    DispatchError,
    DispatchPacket,
    SealedArtifact,
)


class ClaudeAdapter(ContributorAdapter):
    """Claude contributor - no subprocess; uses caller-provided callback to
    materialize the artifact. The callback receives the DispatchPacket and
    must return a dict in the standard sealed-artifact shape (findings,
    goal_satisfied, blocking_objections, ...).
    """

    name = "claude"

    def __init__(
        self,
        adapter_config: dict | None = None,
        *,
        artifact_callback: Callable[[DispatchPacket], dict] | None = None,
    ):
        super().__init__(adapter_config)
        self.artifact_callback = artifact_callback

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        if self.artifact_callback is None:
            raise DispatchError(
                "ClaudeAdapter has no artifact_callback set; cannot dispatch. "
                "Real-runtime orchestration must inject one; tests pass a fake."
            )
        try:
            artifact_dict = self.artifact_callback(packet)
        except Exception as exc:
            raise DispatchError(
                f"claude artifact_callback raised ({type(exc).__name__}): {exc}"
            ) from exc

        if not isinstance(artifact_dict, dict):
            raise DispatchError(
                f"claude artifact_callback must return dict; got {type(artifact_dict).__name__}"
            )

        # Required fields per sealed-artifact contract.
        for required in ("findings", "goal_satisfied", "blocking_objections"):
            if required not in artifact_dict:
                raise DispatchError(
                    f"claude artifact missing required field {required!r}"
                )

        # Fill in identifiers if the callback didn't.
        reviewer_id = packet.reviewer_id or artifact_dict.get(
            "reviewer_id",
            f"claude-{packet.iteration_dir.name}-{packet.phase}-1",
        )
        pass_id = packet.pass_id or artifact_dict.get(
            "pass_id", f"{reviewer_id}-pass1"
        )
        iteration_id = artifact_dict.get("iteration_id", packet.iteration_dir.name)

        # gemini-rev-001 round-1 style cleanup: spread callback's dict FIRST,
        # then assign canonical IDs so they authoritatively override any value
        # in artifact_dict - no redundant re-assignment.
        full_artifact = {
            **artifact_dict,
            "iteration_id": iteration_id,
            "reviewer_id": reviewer_id,
            "pass_id": pass_id,
        }

        # codex-rev-001 round-1 BLOCKING fix: route claude artifacts through
        # T6 (review.write_and_seal) for symmetric sealing semantics with
        # codex/gemini outputs.
        from consensus_mcp.tools.review_write_and_seal import handle as t6_handle
        t6_result = t6_handle(
            iteration_id=iteration_id,
            reviewer_id=reviewer_id,
            pass_id=pass_id,
            packet=full_artifact,
        )
        if "error" in t6_result:
            raise DispatchError(
                f"claude T6 seal failed: {t6_result.get('error')!r}"
            )

        archive_path = Path(t6_result["sealed_path"]).resolve()
        # codex-rev-001 round-2 BLOCKING fix: confinement check. T6's archive
        # path MUST be a regular file AND its filename MUST encode the same
        # iteration_id + reviewer_id + pass_id we asked T6 to seal. This
        # prevents a misconfigured / tampered T6 from leaking arbitrary files
        # into the iteration via the trusted-path return contract.
        if not archive_path.is_file():
            raise DispatchError(
                f"claude T6 returned a non-file sealed_path: {archive_path}"
            )
        # M1 (consult iteration-m1-hardening-design-4d7d2469) S5 follow-through:
        # T6 now caps long archive filenames (truncated components + 12-hex
        # sha256 suffix), so the legacy verbatim-substring token check would
        # spuriously reject a SUCCESSFUL seal whenever iteration_id exceeds the
        # per-component budget. Replaced with a STRICTLY STRONGER check:
        # recompute the bounded filename from the exact identity tuple we asked
        # T6 to seal (the first 10 chars of the name are its date component)
        # and require an exact match - covers both the legacy natural form and
        # the capped form, and rejects everything else.
        from consensus_mcp.tools.review_write_and_seal import _bounded_seal_filename
        fname = archive_path.name
        expected_fname = _bounded_seal_filename(
            fname[:10], iteration_id, reviewer_id, pass_id
        )
        if fname != expected_fname:
            raise DispatchError(
                f"claude T6 sealed_path filename {fname!r} does not match the "
                f"expected sealed name {expected_fname!r} for iteration_id="
                f"{iteration_id!r} reviewer_id={reviewer_id!r}; refusing to copy "
                f"unverified file into iteration_dir (potential confinement "
                f"violation)"
            )

        # Mirror T6's sealed file to iter_dir as claude-<phase>.yaml so adapters
        # have symmetric on-disk layout.
        local_path = packet.iteration_dir / f"claude-{packet.phase}.yaml"
        import shutil
        shutil.copyfile(str(archive_path), str(local_path))

        return SealedArtifact(
            contributor=self.name,
            phase=packet.phase,
            pass_id=pass_id,
            sealed_path=local_path,
            archive_sealed_path=archive_path,
            packet_sha256=t6_result.get("packet_sha256", ""),
            parsed=full_artifact,
        )
