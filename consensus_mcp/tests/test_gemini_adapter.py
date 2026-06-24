from __future__ import annotations




def test_gemini_adapter_threads_configured_command_to_dispatch(monkeypatch, tmp_path):
    from consensus_mcp.contributors.base import DispatchPacket
    from consensus_mcp.contributors.gemini import GeminiAdapter

    captured = {}

    def fake_main(argv):
        captured["argv"] = list(argv)
        sealed = tmp_path / "gemini-review.yaml"
        sealed.write_text("reviewer_id: gemini\npass_id: p1\nfindings: []\n", encoding="utf-8")
        print('{"ok": true, "pass_id": "p1", "sealed_path": "' + str(sealed) + '", "archive_sealed_path": "", "packet_sha256": "abc"}')
        return 0

    from consensus_mcp import _dispatch_gemini
    monkeypatch.setattr(_dispatch_gemini, "main", fake_main)

    goal_packet = tmp_path / "goal.yaml"
    goal_packet.write_text("goal: {}\n", encoding="utf-8")
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    adapter = GeminiAdapter(adapter_config={"command": "agy", "model": "Gemini 3.1 Pro (High)"})
    artifact = adapter.dispatch(DispatchPacket(
        phase="propose",
        contributor="gemini",
        goal_packet_path=goal_packet,
        iteration_dir=iter_dir,
        review_target_path=None,
        reviewer_id="gemini",
        pass_id="p1",
        timeout_seconds=10,
    ))

    assert artifact.pass_id == "p1"
    assert "--gemini-bin" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--gemini-bin") + 1] == "agy"
