"""The panel must read archive/index.yaml under the writers' lock.

Seats seal at the same time and each writer replaces index.yaml while holding
archive/index.lock. On Windows an unlocked read that races a replace fails with
"Access is denied", which marked a sealed seat failed; the plan-pass check then
refused the pass ("sealed archive review omitted or marked failed in summary").
Seen only on GitHub's Windows runners.
"""
import threading
import time

import yaml

from consensus_mcp import _consult_panel as cp
from consensus_mcp.tools.review_write_and_seal import _canonical_yaml_sha256, _file_lock


def _sealed_layout(root):
    iteration, seat, pass_id = "iteration-x", "codex", "pass-1"
    directory = root / "consensus-state" / "active" / iteration
    archive = root / "consensus-state" / "archive" / "review-passes"
    directory.mkdir(parents=True)
    archive.mkdir(parents=True)
    packet = {"iteration_id": iteration, "pass_id": pass_id, "reviewer_id": seat, "findings": []}
    packet["packet_sha256"] = _canonical_yaml_sha256(dict(packet))
    archived = archive / f"{pass_id}.yaml"
    archived.write_text(yaml.safe_dump(packet), encoding="utf-8")
    mirror = directory / f"{seat}-review.yaml"
    mirror.write_text(yaml.safe_dump(packet), encoding="utf-8")
    (archive / "index.yaml").write_text(yaml.safe_dump({"passes": [{
        "id": pass_id, "iteration_id": iteration, "reviewer_id": seat,
        "packet_sha256": packet["packet_sha256"], "path": str(archived)}]}), encoding="utf-8")
    result = {"ok": True, "pass_id": pass_id, "sealed_path": str(mirror),
              "archive_sealed_path": str(archived), "packet_sha256": packet["packet_sha256"]}
    return archive, result, pass_id, iteration, seat


def test_sealed_packet_reads_index_under_writer_lock(tmp_path, monkeypatch):
    monkeypatch.delenv("CONSENSUS_MCP_STATE_ROOT", raising=False)
    archive, result, pass_id, iteration, seat = _sealed_layout(tmp_path)
    holding, release, done = threading.Event(), threading.Event(), threading.Event()
    outcome = {}

    def writer():
        with _file_lock(archive / "index.lock"):
            holding.set()
            release.wait(10)

    def reader():
        try:
            outcome["packet"] = cp._sealed_packet(result, pass_id, tmp_path, iteration, seat)[0]
        except Exception as exc:  # pragma: no cover - surfaced by the asserts below
            outcome["error"] = exc
        done.set()

    w = threading.Thread(target=writer)
    w.start()
    assert holding.wait(5)
    r = threading.Thread(target=reader)
    r.start()
    # While a writer holds the lock the read must wait, not race the replace.
    assert not done.wait(0.5), outcome
    release.set()
    assert done.wait(10)
    w.join(5)
    r.join(5)
    assert "error" not in outcome, outcome
    assert outcome["packet"]["pass_id"] == pass_id


def test_read_review_index_returns_mapping(tmp_path):
    archive = tmp_path / "review-passes"
    archive.mkdir()
    (archive / "index.yaml").write_text("passes: []\n", encoding="utf-8")
    assert cp.read_review_index(archive) == {"passes": []}
