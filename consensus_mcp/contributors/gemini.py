"""Gemini contributor adapter — wraps consensus_mcp._dispatch_gemini.

Mirrors CodexAdapter. Phase semantics same as codex: all phases reuse the
existing gemini_review_template.md; phase intent conveyed via goal_packet.
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


class GeminiAdapter(ContributorAdapter):
    """Gemini contributor — subprocess via _dispatch_gemini.main."""

    name = "gemini"

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        reviewer_id = packet.reviewer_id or f"gemini-{packet.iteration_dir.name}-{packet.phase}-1"
        pass_id = packet.pass_id or f"{reviewer_id}-pass1"
        argv = [
            "--goal-packet", str(packet.goal_packet_path),
            "--iteration-dir", str(packet.iteration_dir),
            "--reviewer-id", reviewer_id,
            "--pass-id", pass_id,
            "--timeout-seconds", str(packet.timeout_seconds),
        ]
        if packet.review_target_path is not None:
            argv += ["--review-target", str(packet.review_target_path)]

        # codex-rev-003 round-1 fix: merge packet.adapter_options with
        # adapter_config so per-dispatch overrides take precedence over
        # static config. Precedence: packet.adapter_options.model >
        # adapter_config.model.
        merged_options = {}
        merged_options.update(self.adapter_config or {})
        merged_options.update(packet.adapter_options or {})
        model = merged_options.get("model")
        if model:
            argv += ["--model", model]

        from consensus_mcp import _dispatch_gemini

        buf = io.StringIO()
        rc = 0
        with contextlib.redirect_stdout(buf):
            try:
                rc = _dispatch_gemini.main(argv) or 0
            except SystemExit as exc:
                raise DispatchError(
                    f"gemini argparse SystemExit: {exc.code!r}"
                ) from exc
            except Exception as exc:
                raise DispatchError(
                    f"gemini dispatch failed ({type(exc).__name__}): {exc}"
                ) from exc

        output = buf.getvalue().strip()
        try:
            parsed_result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise DispatchError(
                f"gemini dispatch returned non-JSON stdout: {exc}; sample: {output[:200]!r}"
            ) from exc

        if rc != 0 or not parsed_result.get("ok"):
            raise DispatchError(
                f"gemini dispatch failed: rc={rc}, "
                f"error={parsed_result.get('error')!r}, "
                f"error_type={parsed_result.get('error_type')!r}"
            )

        # codex-rev-002 round-1 fix: wrap sealed-path extraction + YAML read
        # in DispatchError (same fix as codex.py).
        try:
            sealed_path_str = parsed_result["sealed_path"]
        except KeyError as exc:
            raise DispatchError(
                f"gemini dispatch returned no sealed_path: {parsed_result!r}"
            ) from exc
        sealed_path = Path(sealed_path_str)
        try:
            import yaml
            sealed = yaml.safe_load(sealed_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise DispatchError(
                f"gemini sealed artifact unreadable at {sealed_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(sealed, dict):
            raise DispatchError(
                f"gemini sealed artifact at {sealed_path} is not a YAML mapping; "
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
