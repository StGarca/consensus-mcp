"""consensus-mcp-deliver (P0.3): mint a delivery token, refusing self-judging."""
from __future__ import annotations

from consensus_mcp import _deliver


def test_deliver_refuses_unsealed_ref(tmp_path):
    f = tmp_path / "x.py"; f.write_text("x = 1", encoding="utf-8")
    (tmp_path / "consensus-state").mkdir()
    rc = _deliver.main([
        "--file", str(f), "--design-consensus-ref", "no-such-iter",
        "--vetted-by", "codex,gemini", "--repo-root", str(tmp_path),
    ])
    assert rc == 2  # not sealed -> refused (no self-judging)


def test_deliver_refuses_missing_file(tmp_path):
    (tmp_path / "consensus-state").mkdir()
    rc = _deliver.main([
        "--file", str(tmp_path / "nope.py"), "--design-consensus-ref", "x",
        "--vetted-by", "codex,gemini", "--repo-root", str(tmp_path),
    ])
    assert rc == 2
