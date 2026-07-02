"""Host-peer contributor adapter - same-family blind SWE-reviewer (v1.20.0).

Per docs/design-consults/v1.20.0-host-peer-agent.md (converged design): a
`kind: host_peer` contributor runs the host's OWN AI family (e.g. claude when
claude hosts) as a *fresh-context, adversarial* code reviewer. It is:

  * **supplementary** - it augments cross-family review, never replaces it; and
  * **EXCLUDED from the cross-family closure invariant** - its sealed artifact
    carries `gate_eligible == False`, so `_closure_invariant.check_closure_invariant`
    can never treat it as the different-family signer (even when its family
    differs from the mutator's - the host is not independent of its own
    orchestration).

Like ClaudeAdapter it does NOT shell out: it accepts a DEDICATED host review
callback (separate from the orchestrator's `claude_artifact_callback`) so the
reviewer seam is explicitly a fresh-context blind reviewer, never the
orchestrator's own voice/context. `dispatch(packet)` invokes the callback with
ONLY the phase DispatchPacket (structural blindness - no peer artifacts), then
stamps the canonical host_peer provenance and seals a normal SealedArtifact via
the shared T6 (review.write_and_seal) path.

Fresh-context isolation is a host-runtime CONTRACT (consensus-mcp cannot prove
memory isolation) - recorded via the independence_attestation, documented as a
contract, not a mechanical guarantee.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from consensus_mcp.contributors.base import (
    ContributorAdapter,
    DispatchError,
    DispatchPacket,
    SealedArtifact,
)

# Canonical, non-negotiable provenance for a host_peer artifact. The adapter
# stamps these regardless of any profile-supplied values so a host_peer can
# NEVER be laundered as an independent cross-family signer.
_ROLE_SWE_REVIEWER = "swe_reviewer"
_WEIGHT_SUPPLEMENTARY = "supplementary"


class HostPeerAdapter(ContributorAdapter):
    """Same-family blind SWE-reviewer. No subprocess; uses a dedicated host
    review callback to materialize the review artifact, then seals it with the
    canonical supplementary / gate_eligible=False provenance.
    """

    name = "host_peer"

    def __init__(
        self,
        adapter_config: dict | None = None,
        *,
        host_peer_review_callback: Callable[[DispatchPacket], dict] | None = None,
    ):
        super().__init__(adapter_config)
        self.host_peer_review_callback = host_peer_review_callback
        # `family` + `role` come from the profile/adapter config; `family`
        # MUST be the host family (the engine factory passes the profile's
        # `family`). `role` defaults to swe_reviewer.
        self.family = self.adapter_config.get("family")
        self.role = self.adapter_config.get("role") or _ROLE_SWE_REVIEWER
        # v1.21 (converged-plan D): when the review was dispatched as a REAL
        # Claude Code subagent, the host runtime gives the reviewer an isolated
        # context window - strengthening fresh_context from a runtime CONTRACT
        # to a structural guarantee. Recorded as an ADDITIVE provenance field
        # (not a `method` rename) so no existing consumer of `method` breaks.
        # PROVISIONAL per codex's falsifiability point: consensus-mcp cannot
        # mechanically verify memory isolation, so this is a host-runtime
        # attestation, falsifiable only by an external transcript showing
        # leaked context. Absent by default (backward-compat: callback-driven
        # inline dispatch stamps no isolation field).
        self.runtime_isolation = self.adapter_config.get("runtime_isolation")

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        if self.host_peer_review_callback is None:
            raise DispatchError(
                "HostPeerAdapter has no host_peer_review_callback set; cannot "
                "dispatch. The engine factory only builds host_peer when a "
                "dedicated callback is wired; tests pass a fake."
            )
        if not self.family:
            raise DispatchError(
                "HostPeerAdapter requires a 'family' in adapter_config (the host "
                "model family); none was provided."
            )

        # --- Structural blindness: invoke the callback with ONLY the packet ---
        # No peer artifacts, no revealed proposals, no orchestrator context.
        try:
            artifact_dict = self.host_peer_review_callback(packet)
        except Exception as exc:
            raise DispatchError(
                f"host_peer review callback raised ({type(exc).__name__}): {exc}"
            ) from exc

        if not isinstance(artifact_dict, dict):
            raise DispatchError(
                f"host_peer review callback must return dict; got "
                f"{type(artifact_dict).__name__}"
            )

        # Required fields per sealed-artifact contract (same as ClaudeAdapter).
        for required in ("findings", "goal_satisfied", "blocking_objections"):
            if required not in artifact_dict:
                raise DispatchError(
                    f"host_peer artifact missing required field {required!r}"
                )

        # --- Identifiers ---
        reviewer_id = packet.reviewer_id or artifact_dict.get(
            "reviewer_id",
            f"{packet.contributor}-{packet.iteration_dir.name}-{packet.phase}-1",
        )
        pass_id = packet.pass_id or artifact_dict.get(
            "pass_id", f"{reviewer_id}-pass1"
        )
        iteration_id = artifact_dict.get("iteration_id", packet.iteration_dir.name)

        # --- Canonical host_peer provenance (authoritative; overrides callback) ---
        independence_attestation = {
            "method": "host_peer_callback",
            "fresh_context": True,
            "no_peer_review_visible_at_dispatch": True,
        }
        # v1.21 additive: stamp the (PROVISIONAL) subagent-isolation claim only
        # when the host indicated the review ran as a real Claude Code subagent.
        # `method` stays host_peer_callback for backward-compat.
        if self.runtime_isolation:
            independence_attestation["runtime_isolation"] = self.runtime_isolation
        dispatch_provenance = {
            "adapter": "host_peer",
            "family": self.family,
            "role": self.role,
            "weight": _WEIGHT_SUPPLEMENTARY,
            "gate_eligible": False,
            "independence_attestation": independence_attestation,
        }

        # Spread the callback dict first, then assign canonical fields LAST so
        # they authoritatively override anything the callback emitted. The
        # `actor` carries model_family + role so the closure invariant + results
        # log can identify the same-family supplementary reviewer; the top-level
        # `gate_eligible`/`weight` are what the closure invariant keys on when
        # this artifact is the closing verdict.
        full_artifact = {
            **artifact_dict,
            "iteration_id": iteration_id,
            "reviewer_id": reviewer_id,
            "pass_id": pass_id,
            "actor": {
                "id": reviewer_id,
                "model_family": self.family,
                "role": self.role,
                "pass_id": pass_id,
            },
            "gate_eligible": False,
            "weight": _WEIGHT_SUPPLEMENTARY,
            "role": self.role,
            "independence_attestation": independence_attestation,
            "dispatch_provenance": dispatch_provenance,
        }

        # --- Seal via T6 (review.write_and_seal) - shared seal path ---
        from consensus_mcp.tools.review_write_and_seal import handle as t6_handle
        t6_result = t6_handle(
            iteration_id=iteration_id,
            reviewer_id=reviewer_id,
            pass_id=pass_id,
            packet=full_artifact,
        )
        if "error" in t6_result:
            raise DispatchError(
                f"host_peer T6 seal failed: {t6_result.get('error')!r}"
            )

        archive_path = Path(t6_result["sealed_path"]).resolve()
        # Confinement check mirroring ClaudeAdapter: the returned sealed_path
        # MUST be a regular file AND its filename MUST encode the iteration_id +
        # reviewer_id we asked T6 to seal, so a misconfigured/tampered T6 cannot
        # leak an arbitrary file into the iteration via the trusted-path return.
        if not archive_path.is_file():
            raise DispatchError(
                f"host_peer T6 returned a non-file sealed_path: {archive_path}"
            )
        # M1 (consult iteration-m1-hardening-design-4d7d2469) S5 follow-through:
        # exact-match against the recomputed bounded filename (see
        # ClaudeAdapter for the rationale) - strictly stronger than the legacy
        # substring check and correct for capped long-iteration names.
        from consensus_mcp.tools.review_write_and_seal import _bounded_seal_filename
        fname = archive_path.name
        expected_fname = _bounded_seal_filename(
            fname[:10], iteration_id, reviewer_id, pass_id
        )
        if fname != expected_fname:
            raise DispatchError(
                f"host_peer T6 sealed_path filename {fname!r} does not match the "
                f"expected sealed name {expected_fname!r} for iteration_id="
                f"{iteration_id!r} reviewer_id={reviewer_id!r}; refusing to copy "
                f"unverified file into iteration_dir (potential confinement "
                f"violation)"
            )

        # Mirror the sealed file into the iteration dir for symmetric on-disk
        # layout (named distinctly from claude-<phase>.yaml so the orchestrator's
        # own review and the host_peer review never collide).
        local_path = packet.iteration_dir / f"host-peer-{packet.phase}.yaml"
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
