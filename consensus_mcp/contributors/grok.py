"""Grok contributor adapter - wraps consensus_mcp._dispatch_grok.

Gemini-twin (converged consult iteration-v131-grok-design-2026-05-26).
Phase semantics same as gemini/codex: all phases reuse the existing
grok templates; phase intent conveyed via goal_packet + forwarded as
`--mode` to the dispatcher.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from consensus_mcp.contributors.base import (
    capture_stdout_threadsafe,
    ContributorAdapter,
    DispatchError,
    DispatchPacket,
    SealedArtifact,
)


class GrokAdapter(ContributorAdapter):
    """Grok contributor - subprocess via _dispatch_grok.main."""

    name = "grok"

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        # Round-key reviewer_id so converge round 2 doesn't collide with
        # round 1's T6 seal (Bug A fix v1.30.2 pattern, codex.py/gemini.py).
        _round = (packet.adapter_options or {}).get("round_number", 1)
        reviewer_id = packet.reviewer_id or f"grok-{packet.iteration_dir.name}-{packet.phase}-{_round}"
        pass_id = packet.pass_id or f"{reviewer_id}-pass1"
        # iter-0044 pattern: forward packet.phase as --mode (propose -> proposal,
        # review/converge -> review).
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

        # Merge precedence: packet.adapter_options > adapter_config (codex-rev-003).
        merged_options = {}
        merged_options.update(self.adapter_config or {})
        merged_options.update(packet.adapter_options or {})
        model = merged_options.get("model")
        if model:
            argv += ["--model", model]

        from consensus_mcp import _dispatch_grok

        rc = 0
        with capture_stdout_threadsafe() as buf:
            try:
                rc = _dispatch_grok.main(argv) or 0
            except SystemExit as exc:
                raise DispatchError(
                    f"grok argparse SystemExit: {exc.code!r}"
                ) from exc
            except Exception as exc:
                raise DispatchError(
                    f"grok dispatch failed ({type(exc).__name__}): {exc}"
                ) from exc

        output = buf.getvalue().strip()
        try:
            parsed_result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise DispatchError(
                f"grok dispatch returned non-JSON stdout: {exc}; sample: {output[:200]!r}"
            ) from exc

        if rc != 0 or not parsed_result.get("ok"):
            raise DispatchError(
                f"grok dispatch failed: rc={rc}, "
                f"error={parsed_result.get('error')!r}, "
                f"error_type={parsed_result.get('error_type')!r}"
            )

        try:
            sealed_path_str = parsed_result["sealed_path"]
        except KeyError as exc:
            raise DispatchError(
                f"grok dispatch returned no sealed_path: {parsed_result!r}"
            ) from exc
        sealed_path = Path(sealed_path_str)
        try:
            import yaml
            sealed = yaml.safe_load(sealed_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise DispatchError(
                f"grok sealed artifact unreadable at {sealed_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(sealed, dict):
            raise DispatchError(
                f"grok sealed artifact at {sealed_path} is not a YAML mapping; "
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
