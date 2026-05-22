"""Generic, profile-driven contributor adapter (v1.18.0).

Per the converged plan (iteration-v1180-contributor-design-2026-05-22):
**B-ROUTING + UNIVERSAL PROFILES.** claude/codex/gemini keep their existing
adapter classes; ``ProfileAdapter`` is the ONE generic adapter that dispatches
any ``kind: cli_reviewer`` contributor described purely by a profile dict — kimi
today, plus any user-added AI with ZERO new Python classes.

ProfileAdapter is the live replacement (in spirit) for the parent project's kimi
monkeypatch wrapper. That wrapper monkeypatched ``_dispatch_gemini._invoke_gemini``
and ``_seal_via_t6`` and — critically — left the gemini provenance defaults in
place, so a kimi review sealed with ``model='gemini-2.5-pro'``. ProfileAdapter
fixes this by building ``dispatch_provenance`` EXPLICITLY from the resolved
profile (model/contributor/bin/attestation_method), never copying another
adapter's defaults (converged-plan ``decision.provenance``; regression gate: a
kimi seal must NOT contain ``model='gemini-2.5-pro'``).

Reuse, don't reimplement
------------------------
The dispatch pipeline mirrors ``_dispatch_gemini.main`` but is parameterized by
the profile. It reuses the shared ``_dispatch_base`` machinery for everything
that is NOT CLI-shape-specific:

  * ``_load_goal_packet`` / ``_load_template`` — read goal_packet + prompt template
  * ``_build_prompt`` — substitute placeholders (same template the gemini adapter uses)
  * ``_sha256_str`` — provenance hashes
  * ``_build_sealed_packet`` — wrap findings in the T6 outer structure
  * ``_seal_via_t6`` — write the sealed YAML through review.write_and_seal (NOT reimplemented)

The ONLY profile-specific steps are:
  * the CLI invocation (``_invoke``: transport, base_args, prompt_flag,
    workdir_flag, model_flag, env, timeout)
  * output chrome removal (``output.strip_patterns``) before JSON parse

The parser intentionally reuses gemini's lenient extractor + shape validator
(``_dispatch_gemini._parse_gemini_output`` via ``_extract_json_from_text``):
a non-codex cli_reviewer (kimi/custom) does not enforce a JSON schema natively
(``output.schema_enforced: false``), so it needs the same fence/prose-tolerant
extraction gemini uses. Finding-ID prefix enforcement is gemini-specific, so we
do NOT route through gemini's id-pattern check; we use ``json.loads`` on the
stripped+extracted text and a minimal structural guard.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from consensus_mcp import _contributor_profiles as _profiles
from consensus_mcp._dispatch_base import (
    _build_prompt,
    _build_sealed_packet,
    _load_goal_packet,
    _load_template,
    _seal_via_t6,
    _sha256_str,
)
from consensus_mcp._dispatch_gemini import _extract_json_from_text
from consensus_mcp.contributors.base import (
    ContributorAdapter,
    DispatchError,
    DispatchPacket,
    SealedArtifact,
)

# Default prompt template (shared with the gemini adapter). A profile-driven
# cli_reviewer is a review/proposal contributor exactly like gemini; it consumes
# the same goal_packet-substituted template. Future profiles MAY name their own
# template, but v1.18.0 keeps a single shared default.
_DISPATCH_TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "dispatch_templates"
)
_DEFAULT_REVIEW_TEMPLATE = _DISPATCH_TEMPLATES_DIR / "gemini_review_template.md"


class ProfileAdapter(ContributorAdapter):
    """Profile-driven cli_reviewer adapter.

    Construct with a single ``profile`` dict (a merged built-in/override entry
    from ``_contributor_profiles``). The profile MUST validate and be
    ``kind: cli_reviewer``; a ``kind: host`` profile (claude) is rejected — the
    host is never a subprocess.
    """

    def __init__(self, profile: dict, adapter_config: dict | None = None):
        super().__init__(adapter_config)
        if not isinstance(profile, dict):
            raise ValueError(
                f"ProfileAdapter requires a profile dict; got {type(profile).__name__}"
            )
        name = profile.get("name")
        # Validate the profile up front (raises ValueError on any violation).
        _profiles.validate_profile(name or "<unnamed>", profile)
        if profile.get("kind") != _profiles.KIND_CLI_REVIEWER:
            raise ValueError(
                f"ProfileAdapter only dispatches kind={_profiles.KIND_CLI_REVIEWER!r} "
                f"profiles; profile {name!r} has kind={profile.get('kind')!r}. "
                f"A host (claude) is the in-process orchestrator, never a subprocess."
            )
        self.profile = profile
        self.name = name

    # ----- profile accessors -------------------------------------------------

    @property
    def _invoke_cfg(self) -> dict:
        return self.profile.get("invoke") or {}

    @property
    def _timeout_seconds(self) -> int:
        return int(self.profile.get("timeout_seconds", 1800))

    @property
    def _sealed_filename(self) -> str:
        return self.profile.get("sealed_filename") or f"{self.name}-review.yaml"

    @property
    def _bin(self) -> str:
        return (self.profile.get("detect") or {}).get("command") or self.name

    @property
    def _model(self) -> str:
        return self.profile.get("model") or ""

    # ----- CLI invocation (the ONE profile-specific step) --------------------

    def _build_cmd(self, prompt: str, repo_root: Path) -> tuple[list[str], bool]:
        """Build the argv from the profile. Returns (cmd, stdin_prompt).

        transport=stdin → prompt delivered via stdin; NO prompt_flag in argv.
        transport=flag  → prompt delivered as the value following prompt_flag.

        base_args, workdir_flag (+ repo_root value), and model_flag (+ model
        value) are wired in per the profile. model_flag is only added when the
        profile carries a non-empty model.
        """
        invoke = self._invoke_cfg
        transport = invoke.get("transport")
        cmd: list[str] = [self._bin]
        cmd.extend(list(invoke.get("base_args") or []))

        workdir_flag = invoke.get("workdir_flag")
        if workdir_flag:
            cmd.extend([workdir_flag, str(repo_root)])

        model_flag = invoke.get("model_flag")
        if model_flag and self._model:
            cmd.extend([model_flag, self._model])

        stdin_prompt = transport == _profiles.TRANSPORT_STDIN
        if transport == _profiles.TRANSPORT_FLAG:
            prompt_flag = invoke.get("prompt_flag")
            cmd.extend([prompt_flag, prompt])
        return cmd, stdin_prompt

    def _subprocess_env(self) -> dict:
        """Parent env + the profile's injected env vars (e.g.
        GEMINI_CLI_TRUST_WORKSPACE). Returns a COPY; never mutates os.environ."""
        env = os.environ.copy()
        for key, value in (self.profile.get("env") or {}).items():
            env[str(key)] = str(value)
        return env

    def _invoke(
        self,
        *,
        cmd: list[str],
        prompt: str,
        stdin_prompt: bool,
        env: dict,
        cwd: str,
        timeout_seconds: int,
    ) -> str:
        """Shell out to the profile's CLI and return raw stdout.

        This is the seam tests monkeypatch (mirrors the kimi wrapper's
        ``_invoke_kimi`` and the test_dispatch_gemini fake-invoke pattern). The
        real implementation runs the CLI non-interactively, piping the prompt
        via stdin when ``stdin_prompt`` is True. Raises DispatchError on any
        subprocess failure so the engine's timeout/failure policy applies.
        """
        popen_input = prompt if stdin_prompt else None
        _t0 = time.perf_counter()
        try:
            result = subprocess.run(
                cmd,
                input=popen_input,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=cwd,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise DispatchError(
                f"{self.name} CLI timed out after {timeout_seconds}s"
            ) from exc
        except FileNotFoundError as exc:
            raise DispatchError(
                f"{self.name} CLI binary not found: {self._bin!r}"
            ) from exc
        except OSError as exc:
            raise DispatchError(
                f"{self.name} CLI invocation failed ({type(exc).__name__}): {exc}"
            ) from exc
        if result.returncode != 0:
            _el = time.perf_counter() - _t0
            raise DispatchError(
                f"{self.name} CLI exited {result.returncode} after {_el:.0f}s; "
                f"stderr tail: {(result.stderr or '')[-500:]!r}"
            )
        return result.stdout or ""

    # ----- output chrome removal --------------------------------------------

    def _strip_chrome(self, text: str) -> str:
        """Apply the profile's output.strip_patterns (regexes) to remove CLI
        chrome (resume footers, banners) BEFORE JSON parse. Patterns are applied
        in declared order; MULTILINE + DOTALL match the kimi wrapper's
        _RESUME_FOOTER_RE semantics."""
        out = text
        for pattern in (self.profile.get("output") or {}).get("strip_patterns") or []:
            out = re.sub(pattern, "", out, flags=re.MULTILINE | re.DOTALL)
        return out.strip()

    # ----- parse ------------------------------------------------------------

    def _parse(self, raw: str) -> dict:
        """Strip chrome, extract a JSON object, parse + minimally validate shape.

        cli_reviewer profiles (kimi/custom) do NOT enforce a JSON schema
        natively, so this reuses gemini's prose/fence-tolerant
        ``_extract_json_from_text`` before ``json.loads``. The structural guard
        keeps it adapter-agnostic (no gemini-rev id-prefix check): the parsed
        object must be a mapping with the standard review keys.
        """
        stripped = self._strip_chrome(raw)
        candidate = _extract_json_from_text(stripped)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise DispatchError(
                f"{self.name} output is not valid JSON: {exc}; "
                f"first 300 chars: {stripped[:300]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise DispatchError(
                f"{self.name} output JSON root must be an object, "
                f"got {type(parsed).__name__}"
            )
        for required in ("findings", "goal_satisfied", "blocking_objections"):
            if required not in parsed:
                raise DispatchError(
                    f"{self.name} output JSON missing required key: {required!r}"
                )
        if not isinstance(parsed["findings"], list):
            raise DispatchError(f"{self.name} output 'findings' must be an array")
        return parsed

    # ----- dispatch ---------------------------------------------------------

    def dispatch(self, packet: DispatchPacket) -> SealedArtifact:
        iter_dir = Path(packet.iteration_dir)
        iteration_id = iter_dir.name
        reviewer_id = (
            packet.reviewer_id
            or f"{self.name}-{iteration_id}-{packet.phase}-1"
        )
        pass_id = packet.pass_id or f"{reviewer_id}-pass1"
        timeout_seconds = packet.timeout_seconds or self._timeout_seconds

        # The CLI's working directory = the PROJECT ROOT (where it loads project
        # context like AGENTS.md / CLAUDE.md). Iteration dirs live at
        # <repo>/consensus-state/active/<iter>, so the repo root is the parent of
        # the `consensus-state` ancestor — NOT iter_dir.parent (which would be
        # consensus-state/active). codex-rev-002 (v1.18.0 impl audit). Fall back
        # to iter_dir.parent for iteration dirs not nested under consensus-state.
        repo_root = iter_dir.parent
        for _anc in iter_dir.parents:
            if _anc.name == "consensus-state":
                repo_root = _anc.parent
                break

        try:
            goal_packet_text = Path(packet.goal_packet_path).read_text(encoding="utf-8")
            goal_packet = _load_goal_packet(Path(packet.goal_packet_path))
            template_text = _load_template(_DEFAULT_REVIEW_TEMPLATE)

            review_target_hash = None
            review_packet_data = None
            if packet.review_target_path is not None:
                rt = Path(packet.review_target_path)
                review_target_text = rt.read_text(encoding="utf-8")
                review_target_hash = _sha256_str(review_target_text)
                if rt.suffix.lower() in (".yaml", ".yml"):
                    import yaml
                    try:
                        candidate = yaml.safe_load(review_target_text)
                        if isinstance(candidate, dict):
                            review_packet_data = candidate
                    except yaml.YAMLError:
                        review_packet_data = None

            prompt = _build_prompt(
                goal_packet,
                template_text,
                iteration_dir=str(iter_dir),
                review_packet_path=str(packet.goal_packet_path),
                review_target_path=(
                    str(packet.review_target_path)
                    if packet.review_target_path is not None
                    else None
                ),
                review_target_hash=review_target_hash,
                review_packet=review_packet_data,
            )
            prompt_sha = _sha256_str(prompt)
            goal_packet_sha = _sha256_str(goal_packet_text)
            scope_sig = (
                (goal_packet or {}).get("authorization", {}) or {}
            ).get("scope_signature", "")

            cmd, stdin_prompt = self._build_cmd(prompt, repo_root)
            raw_output = self._invoke(
                cmd=cmd,
                prompt=prompt,
                stdin_prompt=stdin_prompt,
                env=self._subprocess_env(),
                cwd=str(repo_root),
                timeout_seconds=timeout_seconds,
            )
            extracted = self._parse(raw_output)
            output_sha = _sha256_str(raw_output)
        except DispatchError:
            raise
        except Exception as exc:
            raise DispatchError(
                f"{self.name} dispatch failed ({type(exc).__name__}): {exc}"
            ) from exc

        # PROVENANCE — built EXPLICITLY from the profile. Never copy another
        # adapter's defaults (converged-plan decision.provenance). This is the
        # regression fix for the kimi wrapper's gemini-2.5-pro mislabel.
        provenance = {
            "model": self._model,
            "adapter": "profile",
            "contributor": self.name,
            "bin": self._bin,
            "attestation_method": f"auto_{self.name}_dispatch",
            "prompt_sha256": prompt_sha,
            "output_sha256": output_sha,
            "goal_packet_sha256": goal_packet_sha,
            "schema_sha256": None,
            "scope_signature": scope_sig,
            "review_target_hash": review_target_hash,
        }

        attestation_method = f"auto_{self.name}_dispatch"
        sealed_packet = _build_sealed_packet(
            extracted,
            iteration_id,
            reviewer_id,
            pass_id,
            provenance=provenance,
            attestation_method=attestation_method,
            attestation_input_sources=[
                "goal_packet (path passed via packet.goal_packet_path)",
                "prompt_template (substituted by _build_prompt)",
                "review_target (path passed via packet.review_target_path; may be unspecified)",
                f"profile (name={self.name}, model={self._model}, bin={self._bin})",
            ],
        )

        try:
            result = _seal_via_t6(
                sealed_packet, iter_dir, sealed_filename=self._sealed_filename
            )
        except Exception as exc:
            raise DispatchError(
                f"{self.name} T6 seal failed ({type(exc).__name__}): {exc}"
            ) from exc

        return SealedArtifact(
            contributor=self.name,
            phase=packet.phase,
            pass_id=pass_id,
            sealed_path=Path(result["sealed_path"]),
            archive_sealed_path=(
                Path(result["archive_sealed_path"])
                if result.get("archive_sealed_path")
                else None
            ),
            packet_sha256=result.get("packet_sha256", ""),
            parsed=sealed_packet,
        )
