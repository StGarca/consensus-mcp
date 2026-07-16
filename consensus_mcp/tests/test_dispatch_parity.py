"""Characterization / parity matrix for the reviewer-dispatch surfaces.

Deep-audit remediation bar (iter eb8af083, unanimous panel finding F4-1):
the dedup refactor of the four contributor adapters and the four
reviewer.dispatch_* MCP wrappers claims behavior preservation but shipped
no parity evidence. This module pins the shared contract explicitly:

  - adapter argv snapshots per reviewer (bin/model/effort/thinking/stall,
    option precedence, round-keyed id derivation, review-target handling),
  - adapter DispatchError paths (SystemExit, ok=False, missing sealed_path,
    non-mapping sealed artifact),
  - wrapper structured-error dicts (SystemExit, helper exception, non-JSON
    stdout, rc-vs-ok reconciliation) and phase-vs-mode precedence.

It is written against PUBLIC surfaces only (Adapter.dispatch /
wrapper.handle), so it must pass UNCHANGED on both the pre-refactor
(duplicated) and post-refactor (shared-base) implementations - green on
both sides IS the parity evidence.
"""
from __future__ import annotations

import json

import pytest

from consensus_mcp.contributors.base import (
    DispatchError,
    DispatchPacket,
    PHASE_REVIEW,
)


# ---------------------------------------------------------------------------
# Reviewer surface table - the ONLY things allowed to differ per reviewer.
# ---------------------------------------------------------------------------

def _adapter(name):
    if name == "codex":
        from consensus_mcp.contributors.codex import CodexAdapter as A
    elif name == "gemini":
        from consensus_mcp.contributors.gemini import GeminiAdapter as A
    elif name == "grok":
        from consensus_mcp.contributors.grok import GrokAdapter as A
    else:
        from consensus_mcp.contributors.kimi import KimiAdapter as A
    return A


def _wrapper(name):
    if name == "codex":
        from consensus_mcp.tools import reviewer_dispatch_codex as w
    elif name == "gemini":
        from consensus_mcp.tools import reviewer_dispatch_gemini as w
    elif name == "grok":
        from consensus_mcp.tools import reviewer_dispatch_grok as w
    else:
        from consensus_mcp.tools import reviewer_dispatch_kimi as w
    return w


DISPATCH_MAIN = {
    "codex": "consensus_mcp._dispatch_codex.main",
    "gemini": "consensus_mcp._dispatch_gemini.main",
    "grok": "consensus_mcp._dispatch_grok.main",
    "kimi": "consensus_mcp._dispatch_kimi.main",
}

# Per-reviewer option surface: adapter_options passed -> expected extra argv,
# written out literally (characterization, not derived from the code under test).
OPTION_CASES = {
    "codex": (
        {"codex_bin": "codex-x", "model": "pkt-model", "effort": "high"},
        ["--codex-bin", "codex-x", "--model", "pkt-model", "--effort", "high",
         "--stall-silence-seconds", "7"],
    ),
    "gemini": (
        {"gemini_bin": "gem-x", "model": "pkt-model"},
        ["--gemini-bin", "gem-x", "--model", "pkt-model",
         "--stall-silence-seconds", "7"],
    ),
    "grok": (
        {"model": "pkt-model", "effort": "low"},
        ["--model", "pkt-model", "--effort", "low",
         "--stall-silence-seconds", "7"],
    ),
    "kimi": (
        {"kimi_bin": "kimi-x", "model": "pkt-model", "thinking": False},
        ["--kimi-bin", "kimi-x", "--model", "pkt-model", "--no-thinking",
         "--stall-silence-seconds", "7"],
    ),
}

REVIEWERS = ["codex", "gemini", "grok", "kimi"]


def _packet(name, tmp_path, *, reviewer_id="rid-1", pass_id="rid-1-pass1",
            review_target=True, adapter_options=None):
    return DispatchPacket(
        phase=PHASE_REVIEW,
        contributor=name,
        iteration_dir=tmp_path,
        goal_packet_path=tmp_path / "goal.yaml",
        review_target_path=(tmp_path / "review-packet.yaml") if review_target else None,
        reviewer_id=reviewer_id,
        pass_id=pass_id,
        timeout_seconds=600,
        adapter_options=adapter_options,
    )


def _fake_main(captured, tmp_path, *, ok=True, rc=0, non_json=False,
               drop_sealed_path=False, sealed_text=None, raise_exc=None):
    """Fake _dispatch_<reviewer>.main: captures argv, writes a sealed yaml,
    prints the helper's stdout JSON, returns rc."""
    sealed = tmp_path / "sealed.yaml"

    def main(argv):
        captured.extend(argv)
        if raise_exc is not None:
            raise raise_exc
        sealed.write_text(
            sealed_text if sealed_text is not None else
            "findings: []\ngoal_satisfied: true\nblocking_objections: []\n",
            encoding="utf-8",
        )
        if non_json:
            print("definitely not json")
        else:
            payload = {
                "ok": ok, "pass_id": "rid-1-pass1",
                "sealed_path": str(sealed), "archive_sealed_path": None,
                "packet_sha256": "0" * 64,
            }
            if drop_sealed_path:
                del payload["sealed_path"]
            print(json.dumps(payload))
        return rc
    return main


# ---------------------------------------------------------------------------
# Adapter argv snapshots (base argv + reviewer-specific flags + precedence)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", REVIEWERS)
def test_adapter_argv_snapshot_full_options(name, monkeypatch, tmp_path):
    """Exact argv for a fully-loaded packet. adapter_config supplies model +
    stall; packet.adapter_options overrides model (packet wins) and adds the
    reviewer-specific flags. stall survives from config (not overridden)."""
    cap = []
    monkeypatch.setattr(DISPATCH_MAIN[name], _fake_main(cap, tmp_path))
    options, expected_extra = OPTION_CASES[name]
    adapter = _adapter(name)(adapter_config={"model": "cfg-model",
                                             "stall_silence_seconds": 7})
    adapter.dispatch(_packet(name, tmp_path, adapter_options=options))
    expected = [
        "--goal-packet", str(tmp_path / "goal.yaml"),
        "--iteration-dir", str(tmp_path),
        "--reviewer-id", "rid-1",
        "--pass-id", "rid-1-pass1",
        "--timeout-seconds", "600",
        "--mode", "review",
        "--review-target", str(tmp_path / "review-packet.yaml"),
    ] + expected_extra
    assert cap == expected


@pytest.mark.parametrize("name", REVIEWERS)
def test_adapter_round_keyed_id_derivation_and_no_review_target(
        name, monkeypatch, tmp_path):
    """reviewer_id=None derives the round-keyed id (Bug A fix v1.30.2);
    review_target_path=None omits the flag entirely."""
    cap = []
    monkeypatch.setattr(DISPATCH_MAIN[name], _fake_main(cap, tmp_path))
    _adapter(name)().dispatch(_packet(
        name, tmp_path, reviewer_id=None, pass_id=None, review_target=False,
        adapter_options={"round_number": 2},
    ))
    rid = f"{name}-{tmp_path.name}-{PHASE_REVIEW}-2"
    assert cap[cap.index("--reviewer-id") + 1] == rid
    assert cap[cap.index("--pass-id") + 1] == f"{rid}-pass1"
    assert "--review-target" not in cap


def test_adapter_command_key_wins_over_bin_key(monkeypatch, tmp_path):
    """'command' takes precedence over '<name>_bin' (command or bin)."""
    cap = []
    monkeypatch.setattr(DISPATCH_MAIN["codex"], _fake_main(cap, tmp_path))
    _adapter("codex")().dispatch(_packet(
        "codex", tmp_path,
        adapter_options={"command": "cmd-bin", "codex_bin": "other-bin"},
    ))
    assert cap[cap.index("--codex-bin") + 1] == "cmd-bin"
    assert "other-bin" not in cap


def test_kimi_thinking_true_forwards_thinking_flag(monkeypatch, tmp_path):
    cap = []
    monkeypatch.setattr(DISPATCH_MAIN["kimi"], _fake_main(cap, tmp_path))
    _adapter("kimi")().dispatch(_packet(
        "kimi", tmp_path, adapter_options={"thinking": True},
    ))
    assert "--thinking" in cap
    assert "--no-thinking" not in cap


# ---------------------------------------------------------------------------
# Adapter DispatchError paths (uniform across all four reviewers)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", REVIEWERS)
def test_adapter_systemexit_raises_dispatcherror(name, monkeypatch, tmp_path):
    monkeypatch.setattr(
        DISPATCH_MAIN[name],
        _fake_main([], tmp_path, raise_exc=SystemExit(2)),
    )
    with pytest.raises(DispatchError, match="argparse SystemExit"):
        _adapter(name)().dispatch(_packet(name, tmp_path))


@pytest.mark.parametrize("name", REVIEWERS)
def test_adapter_ok_false_raises_dispatcherror(name, monkeypatch, tmp_path):
    monkeypatch.setattr(DISPATCH_MAIN[name], _fake_main([], tmp_path, ok=False))
    with pytest.raises(DispatchError, match="dispatch failed"):
        _adapter(name)().dispatch(_packet(name, tmp_path))


@pytest.mark.parametrize("name", REVIEWERS)
def test_adapter_nonzero_rc_with_ok_true_raises_dispatcherror(
        name, monkeypatch, tmp_path):
    """codex-rev-001 (post-review): rc != 0 must fail closed even when stdout
    claims ok=true - pins the rc side of the `rc != 0 or not ok` disjunction,
    which the ok=False case above cannot distinguish from."""
    monkeypatch.setattr(DISPATCH_MAIN[name], _fake_main([], tmp_path, ok=True, rc=3))
    with pytest.raises(DispatchError, match="dispatch failed"):
        _adapter(name)().dispatch(_packet(name, tmp_path))


@pytest.mark.parametrize("name", REVIEWERS)
def test_adapter_missing_sealed_path_raises_dispatcherror(
        name, monkeypatch, tmp_path):
    monkeypatch.setattr(
        DISPATCH_MAIN[name],
        _fake_main([], tmp_path, drop_sealed_path=True),
    )
    with pytest.raises(DispatchError, match="no sealed_path"):
        _adapter(name)().dispatch(_packet(name, tmp_path))


@pytest.mark.parametrize("name", REVIEWERS)
def test_adapter_non_mapping_sealed_raises_dispatcherror(
        name, monkeypatch, tmp_path):
    monkeypatch.setattr(
        DISPATCH_MAIN[name],
        _fake_main([], tmp_path, sealed_text="- a\n- list\n"),
    )
    with pytest.raises(DispatchError, match="not a YAML mapping"):
        _adapter(name)().dispatch(_packet(name, tmp_path))


# ---------------------------------------------------------------------------
# MCP wrapper structured-error parity + phase-vs-mode precedence
# ---------------------------------------------------------------------------

def _wrapper_fake_main(captured, *, stdout=None, rc=0, raise_exc=None):
    def main(argv):
        captured.append(list(argv))
        if raise_exc is not None:
            raise raise_exc
        if stdout is not None:
            print(stdout)
        return rc
    return main


def _handle(name, monkeypatch, fake, **kwargs):
    monkeypatch.setattr(DISPATCH_MAIN[name], fake)
    return _wrapper(name).handle(
        goal_packet_path="goal.yaml", iteration_dir="iter-dir", **kwargs,
    )


@pytest.mark.parametrize("name", REVIEWERS)
def test_wrapper_success_passthrough(name, monkeypatch):
    payload = {"ok": True, "pass_id": "p1", "sealed_path": "s.yaml"}
    result = _handle(name, monkeypatch,
                     _wrapper_fake_main([], stdout=json.dumps(payload)))
    assert result == payload


@pytest.mark.parametrize("name", REVIEWERS)
def test_wrapper_systemexit_becomes_structured_error(name, monkeypatch):
    result = _handle(name, monkeypatch,
                     _wrapper_fake_main([], raise_exc=SystemExit(2)))
    assert result["ok"] is False
    assert result["error_type"] == "ArgparseSystemExit"
    assert "argparse rejected input" in result["error"]


@pytest.mark.parametrize("name", REVIEWERS)
def test_wrapper_helper_exception_becomes_structured_error(name, monkeypatch):
    result = _handle(name, monkeypatch,
                     _wrapper_fake_main([], raise_exc=RuntimeError("boom")))
    assert result == {"ok": False, "error_type": "RuntimeError", "error": "boom"}


@pytest.mark.parametrize("name", REVIEWERS)
def test_wrapper_non_json_stdout_becomes_structured_error(name, monkeypatch):
    result = _handle(name, monkeypatch,
                     _wrapper_fake_main([], stdout="garbage <<<"))
    assert result["ok"] is False
    assert result["error_type"] == "WrapperJsonDecodeError"
    assert result["raw_stdout_sample"].startswith("garbage <<<")


@pytest.mark.parametrize("name", REVIEWERS)
def test_wrapper_nonzero_rc_forces_ok_false(name, monkeypatch):
    """iter-0028 F3: rc!=0 with stdout claiming ok=True is forced to
    ok=False and stamped with the marker key."""
    result = _handle(name, monkeypatch, _wrapper_fake_main(
        [], stdout=json.dumps({"ok": True, "pass_id": "p1"}), rc=3))
    assert result["ok"] is False
    assert result["wrapper_forced_ok_false_due_to_nonzero_rc"] is True


@pytest.mark.parametrize("name", REVIEWERS)
def test_wrapper_nonzero_rc_honest_failure_untouched(name, monkeypatch):
    """Honest failure passthrough: rc!=0 AND ok=False leaves the dict alone."""
    payload = {"ok": False, "error": "helper failed", "error_type": "X"}
    result = _handle(name, monkeypatch,
                     _wrapper_fake_main([], stdout=json.dumps(payload), rc=1))
    assert result == payload
    assert "wrapper_forced_ok_false_due_to_nonzero_rc" not in result


@pytest.mark.parametrize("name", REVIEWERS)
def test_wrapper_phase_translates_and_mode_wins(name, monkeypatch):
    """--mode precedence (iter-0043/0044): explicit mode > phase_to_mode(phase)
    > omitted entirely (dispatcher default)."""
    ok = json.dumps({"ok": True})

    cap = []
    _handle(name, monkeypatch, _wrapper_fake_main(cap, stdout=ok),
            phase="propose")
    argv = cap[0]
    assert argv[argv.index("--mode") + 1] == "proposal"

    cap = []
    _handle(name, monkeypatch, _wrapper_fake_main(cap, stdout=ok),
            phase="propose", mode="review")
    argv = cap[0]
    assert argv[argv.index("--mode") + 1] == "review"

    cap = []
    _handle(name, monkeypatch, _wrapper_fake_main(cap, stdout=ok))
    assert "--mode" not in cap[0]
