"""Codex contributor adapter - wraps consensus_mcp._dispatch_codex.

Per iter-0015 converged-plan Section C: codex's existing dispatch helper is
the underlying CLI invoker; this adapter normalizes its packet -> argv
translation and result extraction into the ContributorAdapter interface.

Phase semantics for v1: all three phases (propose, review, converge) reuse
the SAME prompt template (codex_review_template.md) - phase intent is
conveyed via the goal_packet's `desired_end_state` text that the workflow
engine writes appropriately per phase. This was empirically validated in
iter-0015's design consult (same template, different goal yielded design
proposals + convergence syntheses). Phase-specific templates can be added
in a later iteration if observed quality drops.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from consensus_mcp.contributors.base import (
    ContributorAdapter,
    DispatchError,
    DispatchPacket,
    SealedArtifact,
)


class CodexAdapter(ContributorAdapter):
    """Codex contributor - subprocess via _dispatch_codex.main."""

    name = "codex"

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        # Build argv mirroring reviewer.dispatch_codex MCP wrapper.
        # Bug A fix (v1.30.2): round-key the reviewer_id so a converge ROUND 2 does
        # not collide with round 1's immutable T6 seal (index_collision). round_number
        # is threaded via adapter_options (base.py); propose/review pass none -> 1.
        _round = (packet.adapter_options or {}).get("round_number", 1)
        reviewer_id = packet.reviewer_id or f"codex-{packet.iteration_dir.name}-{packet.phase}-{_round}"
        pass_id = packet.pass_id or f"{reviewer_id}-pass1"
        # iter-0044 per iter-0043 converged plan: forward packet.phase as
        # --mode to the dispatcher. Previously omitted, causing every
        # workflow #4 round-1 dispatch through the engine to silently
        # use review-mode templates/schemas. Strict mapping via
        # _phase_mode.phase_to_mode (raises ValueError on unknown phase).
        from consensus_mcp.contributors._phase_mode import phase_to_mode
        mode = phase_to_mode(packet.phase)
        argv = [
            "--goal-packet", str(packet.goal_packet_path),
            "--iteration-dir", str(packet.iteration_dir),
            "--reviewer-id", reviewer_id,
            "--pass-id", pass_id,
            "--timeout-seconds", str(packet.timeout_seconds),
            "--mode", mode,
        ]
        if packet.review_target_path is not None:
            argv += ["--review-target", str(packet.review_target_path)]

        from consensus_mcp import _dispatch_codex

        buf = io.StringIO()
        rc = 0
        with contextlib.redirect_stdout(buf):
            try:
                rc = _dispatch_codex.main(argv) or 0
            except SystemExit as exc:
                raise DispatchError(
                    f"codex argparse SystemExit: {exc.code!r}"
                ) from exc
            except Exception as exc:
                raise DispatchError(
                    f"codex dispatch failed ({type(exc).__name__}): {exc}"
                ) from exc

        output = buf.getvalue().strip()
        try:
            parsed_result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise DispatchError(
                f"codex dispatch returned non-JSON stdout: {exc}; sample: {output[:200]!r}"
            ) from exc

        if rc != 0 or not parsed_result.get("ok"):
            raise DispatchError(
                f"codex dispatch failed: rc={rc}, "
                f"error={parsed_result.get('error')!r}, "
                f"error_type={parsed_result.get('error_type')!r}"
            )

        # codex-rev-002 round-1 fix: wrap sealed-path extraction + YAML
        # reading in DispatchError so engine timeout/failure policy applies
        # uniformly regardless of post-helper failures.
        try:
            sealed_path_str = parsed_result["sealed_path"]
        except KeyError as exc:
            raise DispatchError(
                f"codex dispatch returned no sealed_path: {parsed_result!r}"
            ) from exc
        sealed_path = Path(sealed_path_str)
        try:
            import yaml
            sealed = yaml.safe_load(sealed_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise DispatchError(
                f"codex sealed artifact unreadable at {sealed_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(sealed, dict):
            raise DispatchError(
                f"codex sealed artifact at {sealed_path} is not a YAML mapping; "
                f"got {type(sealed).__name__}"
            )

        return SealedArtifact(
            contributor=self.name,
            phase=packet.phase,
            pass_id=parsed_result["pass_id"],
            sealed_path=sealed_path,
            archive_sealed_path=Path(parsed_result["archive_sealed_path"])
                if parsed_result.get("archive_sealed_path") else None,
            packet_sha256=parsed_result.get("packet_sha256", ""),
            parsed=sealed,
        )
