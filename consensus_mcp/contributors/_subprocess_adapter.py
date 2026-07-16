"""Shared base for subprocess-backed contributor adapters.

codex/gemini/grok/kimi are all thin adapters over their matching
``_dispatch_<reviewer>`` helper. Every one of them performs the identical
sequence:

  1. derive a round-keyed reviewer_id + pass_id (Bug A fix v1.30.2),
  2. build the base argv (goal-packet, iteration-dir, ids, timeout, --mode),
  3. append the review-target when present,
  4. merge ``adapter_config`` < ``packet.adapter_options`` and append the
     reviewer-specific option flags,
  5. run ``_dispatch_<reviewer>.main`` under thread-safe stdout capture,
  6. parse the JSON result, enforce rc/ok, read + validate the sealed YAML,
  7. return a :class:`SealedArtifact`.

Only step (4) differs per reviewer. This base class owns steps 1-3 and 5-7;
subclasses declare ``name`` and implement :meth:`_extra_argv`.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

from consensus_mcp.contributors.base import (
    capture_stdout_threadsafe,
    ContributorAdapter,
    DispatchError,
    DispatchPacket,
    SealedArtifact,
)


class SubprocessContributorAdapter(ContributorAdapter):
    """Base for contributors that shell out to a ``_dispatch_<reviewer>`` helper."""

    #: Dotted path of the dispatch module, imported lazily so tests can
    #: monkeypatch ``<module>.main``.
    dispatch_module: str = ""

    def _extra_argv(self, merged_options: dict) -> list[str]:
        """Reviewer-specific option flags (model, bin, effort, ...).

        ``merged_options`` is ``adapter_config`` overlaid with the packet's
        ``adapter_options`` (packet wins). Default: no extra flags.
        """
        return []

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        # Bug A fix (v1.30.2): round-key reviewer_id so a converge ROUND 2 does
        # not collide with round 1's immutable T6 seal. round_number is threaded
        # via adapter_options; propose/review pass none -> 1.
        _round = (packet.adapter_options or {}).get("round_number", 1)
        reviewer_id = packet.reviewer_id or (
            f"{self.name}-{packet.iteration_dir.name}-{packet.phase}-{_round}"
        )
        pass_id = packet.pass_id or f"{reviewer_id}-pass1"
        # iter-0044: forward packet.phase as --mode. Strict mapping via
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

        # Merge precedence: packet.adapter_options > adapter_config.
        merged_options: dict = {}
        merged_options.update(self.adapter_config or {})
        merged_options.update(packet.adapter_options or {})
        argv += self._extra_argv(merged_options)

        module = importlib.import_module(self.dispatch_module)

        rc = 0
        with capture_stdout_threadsafe() as buf:
            try:
                rc = module.main(argv) or 0
            except SystemExit as exc:
                raise DispatchError(
                    f"{self.name} argparse SystemExit: {exc.code!r}"
                ) from exc
            except Exception as exc:
                raise DispatchError(
                    f"{self.name} dispatch failed ({type(exc).__name__}): {exc}"
                ) from exc

        output = buf.getvalue().strip()
        try:
            parsed_result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise DispatchError(
                f"{self.name} dispatch returned non-JSON stdout: {exc}; "
                f"sample: {output[:200]!r}"
            ) from exc

        if rc != 0 or not parsed_result.get("ok"):
            raise DispatchError(
                f"{self.name} dispatch failed: rc={rc}, "
                f"error={parsed_result.get('error')!r}, "
                f"error_type={parsed_result.get('error_type')!r}"
            )

        # codex-rev-002 round-1 fix: wrap sealed-path extraction + YAML reading
        # in DispatchError so the engine timeout/failure policy applies
        # uniformly regardless of post-helper failures.
        try:
            sealed_path_str = parsed_result["sealed_path"]
        except KeyError as exc:
            raise DispatchError(
                f"{self.name} dispatch returned no sealed_path: {parsed_result!r}"
            ) from exc
        sealed_path = Path(sealed_path_str)
        try:
            import yaml
            sealed = yaml.safe_load(sealed_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise DispatchError(
                f"{self.name} sealed artifact unreadable at {sealed_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(sealed, dict):
            raise DispatchError(
                f"{self.name} sealed artifact at {sealed_path} is not a YAML "
                f"mapping; got {type(sealed).__name__}"
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
