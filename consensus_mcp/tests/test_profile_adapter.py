"""Unit tests for consensus_mcp.contributors.profile_adapter.ProfileAdapter (v1.18.0).

ProfileAdapter is the generic, profile-driven contributor adapter introduced by
the v1.18.0 B-routing + universal-profiles converged plan
(iteration-v1180-contributor-design-2026-05-22). It consumes a `kind:
cli_reviewer` profile dict and dispatches the described CLI by reusing the
shared `_dispatch_base` machinery (prompt build, JSON parse helpers, T6 seal),
with a PROFILE-DRIVEN invoke step.

These tests are hermetic: the real CLI subprocess is replaced by a fake invoker
(monkeypatched onto the adapter's `_invoke` seam, mirroring how
test_dispatch_gemini fakes `_invoke_gemini`), and the T6 seal is replaced by a
capturing fake so the sealed packet - and its `dispatch_provenance` block - can
be asserted without a real consensus-state tree.

Coverage (per converged-plan acceptance gates):
  * BOTH transports: a stdin profile (kimi: prompt via stdin, NO prompt_flag)
    and a flag profile (prompt via invoke.prompt_flag).
  * env injection, workdir_flag, base_args, model_flag wiring into the argv/env.
  * output.strip_patterns applied to the raw CLI output BEFORE JSON parse.
  * JSON parsed; T6 seal written.
  * dispatch_provenance carries ACCURATE model/adapter/contributor sourced from
    the profile (NOT another adapter's defaults).
  * Provenance regression: a kimi-profile seal MUST NOT carry
    model == 'gemini-2.5-pro' (the parent kimi-wrapper mislabel).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consensus_mcp import _contributor_profiles as profiles  # noqa: E402
from consensus_mcp.contributors import profile_adapter as pa  # noqa: E402
from consensus_mcp.contributors.base import (  # noqa: E402
    DispatchError,
    DispatchPacket,
    PHASE_REVIEW,
    SealedArtifact,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def _kimi_profile() -> dict:
    """The packaged kimi profile (the live ProfileAdapter contract in v1.18.0)."""
    return profiles.load_builtin_profiles()["kimi"]


def _flag_profile() -> dict:
    """A synthetic flag-transport cli_reviewer profile (the OTHER transport).

    Modeled on a gemini-shaped CLI: prompt delivered via a -p flag, model via
    --model, an injected env var, no workdir flag. Distinct model label so the
    provenance assertion is unambiguous.
    """
    return {
        "name": "acme",
        "kind": "cli_reviewer",
        "model": "acme-reviewer-v9",
        "detect": {"command": "acme-cli"},
        "invoke": {
            "transport": "flag",
            "base_args": ["--headless"],
            "prompt_flag": "-p",
            "workdir_flag": None,
            "model_flag": "--model",
        },
        "env": {"ACME_TRUST": "true"},
        "output": {"strip_patterns": [], "schema_enforced": False},
        "sealed_filename": "acme-review.yaml",
        "id_prefix": "acme-rev",
        "timeout_seconds": 1800,
    }


def _valid_review_json() -> str:
    """Minimal valid review-shaped JSON payload (parse target)."""
    return json.dumps({
        "findings": [],
        "goal_satisfied": True,
        "goal_satisfied_rationale": "No issues found.",
        "blocking_objections": [],
    })


def _write_goal_packet(tmp_path: Path) -> Path:
    gp = {
        "goal": {"summary": "test goal", "desired_end_state": "done"},
        "allowed_files": ["consensus_mcp/foo.py"],
        "acceptance_gates": [],
        "authorization": {
            "scope_signature": "sig-123",
            "authorized_by": "tester",
            "authorized_at_utc": "2026-05-22T00:00:00Z",
        },
    }
    p = tmp_path / "goal_packet.yaml"
    p.write_text(yaml.safe_dump(gp, sort_keys=False), encoding="utf-8")
    return p


class _CaptureInvoke:
    """Fake CLI invoker. Captures the kwargs ProfileAdapter passes (cmd, env,
    prompt, cwd) and returns scripted raw output. Mirrors the
    test_dispatch_gemini fake-invoke pattern."""

    def __init__(self, output: str):
        self.output = output
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.output


class _CaptureSeal:
    """Fake _seal_via_t6. Captures the packet (with dispatch_provenance) and the
    sealed_filename, and writes a minimal sealed YAML to iter_dir so the adapter
    can read it back. Returns the same dict shape real _seal_via_t6 returns."""

    def __init__(self):
        self.packets: list[dict] = []
        self.sealed_filenames: list[str] = []

    def __call__(self, packet, iteration_dir, sealed_filename="review.yaml"):
        self.packets.append(packet)
        self.sealed_filenames.append(sealed_filename)
        local = Path(iteration_dir) / sealed_filename
        local.write_text(yaml.safe_dump(packet, sort_keys=False), encoding="utf-8")
        return {
            "sealed_path": str(local),
            "archive_sealed_path": str(local),
            "packet_sha256": "deadbeef",
            "index_updated": True,
            "audit_event_id": "evt-1",
        }


def _make_packet(tmp_path: Path, profile_name: str) -> DispatchPacket:
    iter_dir = tmp_path / f"iter-{profile_name}"
    iter_dir.mkdir()
    return DispatchPacket(
        phase=PHASE_REVIEW,
        contributor=profile_name,
        iteration_dir=iter_dir,
        goal_packet_path=_write_goal_packet(tmp_path),
        review_target_path=None,
        reviewer_id=None,
        pass_id=None,
        timeout_seconds=900,
    )


def _wire(monkeypatch, invoke: _CaptureInvoke, seal: _CaptureSeal):
    """Replace the adapter's CLI invoke + T6 seal seams with capturing fakes."""
    monkeypatch.setattr(pa.ProfileAdapter, "_invoke", lambda self, **kw: invoke(**kw))
    monkeypatch.setattr(pa, "_seal_via_t6", seal)


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #

def test_profile_adapter_name_is_profile_name():
    adapter = pa.ProfileAdapter(_kimi_profile())
    assert adapter.name == "kimi"


def test_profile_adapter_rejects_host_profile():
    host = {"name": "claude", "kind": "host", "model": "claude (host)"}
    with pytest.raises(ValueError, match="cli_reviewer"):
        pa.ProfileAdapter(host)


def test_profile_adapter_validates_profile_at_construction():
    bad = {"name": "broken", "kind": "cli_reviewer"}  # missing detect/invoke/output
    with pytest.raises(ValueError):
        pa.ProfileAdapter(bad)


# --------------------------------------------------------------------------- #
# stdin transport (kimi): prompt via stdin, NO prompt flag
# --------------------------------------------------------------------------- #

def test_repo_root_resolved_above_consensus_state(tmp_path, monkeypatch):
    """codex-rev-002 (v1.18.0): the CLI cwd/-w must be the PROJECT ROOT (parent of
    the consensus-state ancestor), NOT iter_dir.parent (= consensus-state/active).
    An iteration dir nested at <repo>/consensus-state/active/<iter> must resolve
    repo_root == <repo>."""
    invoke = _CaptureInvoke(_valid_review_json())
    seal = _CaptureSeal()
    _wire(monkeypatch, invoke, seal)
    repo = tmp_path / "repo"
    iter_dir = repo / "consensus-state" / "active" / "iter-kimi"
    iter_dir.mkdir(parents=True)
    packet = DispatchPacket(
        phase=PHASE_REVIEW,
        contributor="kimi",
        iteration_dir=iter_dir,
        goal_packet_path=_write_goal_packet(iter_dir),
        review_target_path=None,
        reviewer_id=None,
        pass_id=None,
        timeout_seconds=900,
    )
    pa.ProfileAdapter(_kimi_profile()).dispatch(packet)
    call = invoke.calls[0]
    assert call["cwd"] == str(repo), (
        f"cwd should be the repo root {repo!s}, got {call['cwd']!r} "
        f"(iter_dir.parent would be the consensus-state/active bug)"
    )
    # kimi profile carries workdir_flag '-w' -> cmd must point it at the repo root.
    assert "-w" in call["cmd"] and str(repo) in call["cmd"]


def test_stdin_transport_delivers_prompt_via_stdin_no_flag(tmp_path, monkeypatch):
    invoke = _CaptureInvoke(_valid_review_json())
    seal = _CaptureSeal()
    _wire(monkeypatch, invoke, seal)

    adapter = pa.ProfileAdapter(_kimi_profile())
    adapter.dispatch(_make_packet(tmp_path, "kimi"))

    call = invoke.calls[0]
    # Prompt delivered through stdin, not as a flag value.
    assert call["prompt"]
    assert call["stdin_prompt"] is True
    cmd = call["cmd"]
    # NO -p (or any prompt flag) in argv for a stdin profile.
    assert "-p" not in cmd
    # base_args present.
    assert "--quiet" in cmd and "--thinking" in cmd
    # workdir_flag wired with the repo/cwd value.
    assert "-w" in cmd


def test_stdin_transport_seal_provenance_accurate(tmp_path, monkeypatch):
    invoke = _CaptureInvoke(_valid_review_json())
    seal = _CaptureSeal()
    _wire(monkeypatch, invoke, seal)

    adapter = pa.ProfileAdapter(_kimi_profile())
    adapter.dispatch(_make_packet(tmp_path, "kimi"))

    prov = seal.packets[0]["dispatch_provenance"]
    assert prov["model"] == "kimi (CLI-configured default)"
    assert prov["adapter"] == "profile"
    assert prov["contributor"] == "kimi"
    assert prov["bin"] == "kimi"
    assert prov["attestation_method"] == "auto_kimi_dispatch"
    # sha256s present (sourced via _dispatch_base._sha256_str).
    assert prov["prompt_sha256"]
    assert prov["output_sha256"]
    assert prov["goal_packet_sha256"]


def test_kimi_seal_not_mislabeled_as_gemini(tmp_path, monkeypatch):
    """REGRESSION (converged-plan gate): the parent kimi wrapper mislabeled
    model='gemini-2.5-pro'. A kimi-profile seal MUST NOT carry that label."""
    invoke = _CaptureInvoke(_valid_review_json())
    seal = _CaptureSeal()
    _wire(monkeypatch, invoke, seal)

    adapter = pa.ProfileAdapter(_kimi_profile())
    adapter.dispatch(_make_packet(tmp_path, "kimi"))

    prov = seal.packets[0]["dispatch_provenance"]
    assert prov["model"] != "gemini-2.5-pro"
    assert prov["adapter"] != "gemini"
    assert seal.sealed_filenames[0] == "kimi-review.yaml"


def test_stdin_strip_patterns_applied_before_parse(tmp_path, monkeypatch):
    """kimi profile carries a resume-footer strip_pattern. Output with that
    chrome appended must still parse (chrome removed before JSON parse)."""
    chrome = "\n\nTo resume this session: kimi -r abc-123-uuid"
    invoke = _CaptureInvoke(_valid_review_json() + chrome)
    seal = _CaptureSeal()
    _wire(monkeypatch, invoke, seal)

    adapter = pa.ProfileAdapter(_kimi_profile())
    art = adapter.dispatch(_make_packet(tmp_path, "kimi"))
    assert isinstance(art, SealedArtifact)
    assert art.parsed["goal_satisfied"] is True


# --------------------------------------------------------------------------- #
# flag transport: prompt via prompt_flag, model via model_flag, env injected
# --------------------------------------------------------------------------- #

def test_flag_transport_delivers_prompt_via_flag(tmp_path, monkeypatch):
    invoke = _CaptureInvoke(_valid_review_json())
    seal = _CaptureSeal()
    _wire(monkeypatch, invoke, seal)

    adapter = pa.ProfileAdapter(_flag_profile())
    adapter.dispatch(_make_packet(tmp_path, "acme"))

    call = invoke.calls[0]
    cmd = call["cmd"]
    # Flag transport: prompt is the value immediately following prompt_flag.
    assert "-p" in cmd
    p_idx = cmd.index("-p")
    assert cmd[p_idx + 1] == call["prompt"]
    # stdin NOT used for the prompt.
    assert call["stdin_prompt"] is False
    # base_args present.
    assert "--headless" in cmd


def test_flag_transport_model_flag_wired(tmp_path, monkeypatch):
    invoke = _CaptureInvoke(_valid_review_json())
    seal = _CaptureSeal()
    _wire(monkeypatch, invoke, seal)

    adapter = pa.ProfileAdapter(_flag_profile())
    adapter.dispatch(_make_packet(tmp_path, "acme"))

    cmd = invoke.calls[0]["cmd"]
    assert "--model" in cmd
    m_idx = cmd.index("--model")
    assert cmd[m_idx + 1] == "acme-reviewer-v9"


def test_env_injected_into_invoke(tmp_path, monkeypatch):
    invoke = _CaptureInvoke(_valid_review_json())
    seal = _CaptureSeal()
    _wire(monkeypatch, invoke, seal)

    adapter = pa.ProfileAdapter(_flag_profile())
    adapter.dispatch(_make_packet(tmp_path, "acme"))

    env = invoke.calls[0]["env"]
    assert env["ACME_TRUST"] == "true"


def test_flag_transport_seal_provenance_accurate(tmp_path, monkeypatch):
    invoke = _CaptureInvoke(_valid_review_json())
    seal = _CaptureSeal()
    _wire(monkeypatch, invoke, seal)

    adapter = pa.ProfileAdapter(_flag_profile())
    adapter.dispatch(_make_packet(tmp_path, "acme"))

    prov = seal.packets[0]["dispatch_provenance"]
    assert prov["model"] == "acme-reviewer-v9"
    assert prov["adapter"] == "profile"
    assert prov["contributor"] == "acme"
    assert prov["bin"] == "acme-cli"
    assert prov["attestation_method"] == "auto_acme_dispatch"
    assert seal.sealed_filenames[0] == "acme-review.yaml"


# --------------------------------------------------------------------------- #
# Parsing / failure surfaces
# --------------------------------------------------------------------------- #

def test_returns_sealed_artifact_with_parsed_payload(tmp_path, monkeypatch):
    invoke = _CaptureInvoke(_valid_review_json())
    seal = _CaptureSeal()
    _wire(monkeypatch, invoke, seal)

    adapter = pa.ProfileAdapter(_kimi_profile())
    art = adapter.dispatch(_make_packet(tmp_path, "kimi"))

    assert isinstance(art, SealedArtifact)
    assert art.contributor == "kimi"
    assert art.phase == PHASE_REVIEW
    assert art.parsed["goal_satisfied"] is True
    assert art.sealed_path.exists()


def test_non_json_output_raises_dispatch_error(tmp_path, monkeypatch):
    invoke = _CaptureInvoke("this is not json at all")
    seal = _CaptureSeal()
    _wire(monkeypatch, invoke, seal)

    adapter = pa.ProfileAdapter(_kimi_profile())
    with pytest.raises(DispatchError):
        adapter.dispatch(_make_packet(tmp_path, "kimi"))


def test_invoke_failure_surfaces_as_dispatch_error(tmp_path, monkeypatch):
    def boom(self, **_kw):
        raise RuntimeError("CLI exploded")
    monkeypatch.setattr(pa.ProfileAdapter, "_invoke", boom)
    monkeypatch.setattr(pa, "_seal_via_t6", _CaptureSeal())

    adapter = pa.ProfileAdapter(_kimi_profile())
    with pytest.raises(DispatchError):
        adapter.dispatch(_make_packet(tmp_path, "kimi"))
