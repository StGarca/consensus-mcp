"""Packet budgets and local Git inputs, using only throwaway repositories."""
import hashlib
import subprocess
from pathlib import Path

import pytest
import yaml

from consensus_mcp._review_packet import build


def git(repo, *args):
    result = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=repo,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return result.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Packet Test")
    git(tmp_path, "config", "user.email", "packet@example.invalid")
    git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / "module.py").write_text("def original():\n    return 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_committed_staged_unstaged_and_untracked(repo):
    base = git(repo, "rev-parse", "HEAD")
    (repo / "committed.py").write_text("committed = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "committed")
    (repo / "module.py").write_text("def original():\n    return 2\n")
    git(repo, "add", "module.py")
    (repo / "module.py").write_text("def original():\n    return 3\n")
    (repo / "new file.py").write_text("untracked = 1\n")
    packet = build(repo, base, "*.py", packet_mode="full")
    assert "+committed = 1" in packet["unified_diff"]
    assert "+    return 3" in packet["unified_diff"]
    assert "+untracked = 1" in packet["unified_diff"]
    assert packet["full_files"]["new file.py"] == "untracked = 1\n"


def test_budget_cap_keeps_whole_hunks_and_lists_every_cut(repo):
    (repo / "large.py").write_text("".join(f"line{i}\n" for i in range(700)))
    packet = build(repo, "HEAD", "*.py", packet_mode="full")
    assert len(packet["unified_diff"].splitlines()) <= 600
    assert any(o["path"] == "large.py" and "max_diff_lines" in o["reason"] for o in packet["omitted"])
    assert any(o["path"] == "large.py" and "include_full_file" in o["reason"] for o in packet["omitted"])
    assert not packet["unified_diff"]  # a partial 700-line hunk is not a valid diff


def test_binary_and_rename(repo):
    git(repo, "mv", "module.py", "renamed.py")
    (repo / "binary.dat").write_bytes(b"\x00\x01\x02")
    packet = build(repo, "HEAD", ".")
    assert "rename from module.py" in packet["unified_diff"]
    assert "rename to renamed.py" in packet["unified_diff"]
    assert packet["binary_files"] == ["binary.dat"]
    assert {"path": "binary.dat", "section": "diff", "reason": "binary file"} in packet["omitted"]


def test_tracked_binary_is_listed(repo):
    (repo / "data.bin").write_bytes(b"\0before")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "binary base")
    (repo / "data.bin").write_bytes(b"\0after")
    packet = build(repo, "HEAD", ".")
    assert packet["binary_files"] == ["data.bin"]
    assert "Binary files" not in packet["unified_diff"]


def test_crlf_and_canonical_hash_are_deterministic(repo):
    (repo / "module.py").write_bytes(b"def original():\r\n    return 2\r\n")
    first = build(repo, "HEAD", "*.py", packet_mode="full")
    second = build(repo, "HEAD", "*.py", packet_mode="full")
    assert first == second
    assert "\r" not in first["unified_diff"]
    assert "\r" not in first["full_files"]["module.py"]
    content = {key: value for key, value in first.items() if key != "packet_sha256"}
    assert first["packet_sha256"] == hashlib.sha256(yaml.safe_dump(content, sort_keys=True).encode()).hexdigest()


def test_round_two_only_delta_and_open_items(repo):
    base = git(repo, "rev-parse", "HEAD")
    (repo / "first.py").write_text("first_round = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "round one")
    first = build(repo, base, "*.py")
    (repo / "second.py").write_text("second_round = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "round two")
    packet = build(repo, base, "*.py", previous_packet=first, open_items=[
        {"id": "keep", "status": "unresolved"}, {"id": "later", "status": "deferred"},
        {"id": "done", "status": "resolved"},
    ])
    assert packet["base_sha"] == first["head_sha"]
    assert "second_round" in packet["unified_diff"]
    assert "first_round" not in packet["unified_diff"]
    assert packet["full_files"] == {}
    assert [item["id"] for item in packet["open_items"]] == ["keep", "later"]


def test_related_paths_and_bounded_http_excerpts(repo):
    (repo / "consumer.py").write_text("from module import original\n" * 125)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "consumer")
    (repo / "module.py").write_text("def original():\n    return 2\n")
    cli = build(repo, "HEAD", "module.py")
    http = build(repo, "HEAD", "module.py", include_related_excerpts=True)
    assert cli["related_files"] == ["consumer.py"]
    assert cli["related_excerpts"] == []
    assert len(http["related_excerpts"][0]["hits"]) == 120
    assert any(o["section"] == "related_excerpt" for o in http["omitted"])


def test_config_budget_and_override(repo):
    (repo / ".consensus").mkdir()
    (repo / ".consensus" / "config.yaml").write_text("packet_budget:\n  max_diff_lines: 3\n")
    (repo / "module.py").write_text("changed\n")
    assert build(repo, "HEAD", "module.py")["unified_diff"] == ""
    packet = build(repo, "HEAD", "module.py", budget={"max_diff_lines": 20})
    assert "+changed" in packet["unified_diff"]
    assert packet["budget_applied"]["max_diff_lines"] == 20


@pytest.mark.parametrize("bad", [-1, True, 1.2, "600"])
def test_invalid_budget_is_rejected(repo, bad):
    with pytest.raises(ValueError, match="max_diff_lines"):
        build(repo, "HEAD", ".", budget={"max_diff_lines": bad})


def test_full_file_threshold_and_section_omissions(repo):
    (repo / "under.py").write_text("x\n" * 249)
    (repo / "at.py").write_text("x\n" * 250)
    packet = build(repo, "HEAD", "*.py", packet_mode="full")
    assert "under.py" in packet["full_files"]
    assert "at.py" not in packet["full_files"]
    packet = build(repo, "HEAD", "*.py", budget={"max_changed_sections": 1})
    assert any(o["path"] == "under.py" and o["reason"] == "max_changed_sections" for o in packet["omitted"])


def test_prior_item_cap_and_missing_reviewed_head(repo):
    packet = build(repo, "HEAD", ".", open_items=[{"id": str(i), "status": "deferred"} for i in range(22)])
    assert len(packet["open_items"]) == 20
    assert len([o for o in packet["omitted"] if o["section"] == "open_items"]) == 2
    with pytest.raises(ValueError, match="head_sha"):
        build(repo, "HEAD", ".", previous_packet={})


def test_missing_base_is_explicit(repo):
    packet = build(repo, None, ".")
    assert packet["base_sha"] == packet["head_sha"]
    assert packet["omitted"][0]["section"] == "base_ref"


def test_empty_untracked_file_is_represented(repo):
    (repo / "empty.py").write_bytes(b"")
    packet = build(repo, "HEAD", "*.py")
    assert "diff --git a/empty.py b/empty.py" in packet["unified_diff"]
    assert "new file mode 100644" in packet["unified_diff"]


def test_crlf_conversion_alone_is_not_a_change(repo):
    (repo / "module.py").write_bytes(b"def original():\r\n    return 1\r\n")
    assert build(repo, "HEAD", "module.py")["unified_diff"] == ""


def test_no_diff_includes_scoped_files_full_or_bounded(repo):
    (repo / "under.py").write_text("x\n" * 249, encoding="utf-8")
    (repo / "at.py").write_text("x\n" * 250, encoding="utf-8")
    git(repo, "add", "under.py", "at.py")
    git(repo, "commit", "-qm", "scoped files")

    packet = build(repo, "HEAD", "*.py", include_related_excerpts=True)

    assert packet["unified_diff"] == ""
    assert packet["full_files"] == {}
    assert packet["scoped_files"]["module.py"] == {"content": "def original():\n    return 1\n"}
    assert packet["scoped_files"]["under.py"] == {"content": "x\n" * 249}
    excerpt = packet["scoped_files"]["at.py"]
    assert excerpt["start_line"] == 1
    assert excerpt["end_line"] == 120
    assert len(excerpt["excerpt"].splitlines()) == 120
    assert "at.py" in {item["path"] for item in packet["omitted"]}


def test_no_diff_scoped_files_exclude_binary_and_symlink(repo, monkeypatch):
    (repo / "binary.dat").write_bytes(b"\0binary")
    (repo / "link.py").write_text("must not be included\n", encoding="utf-8")
    git(repo, "add", "binary.dat", "link.py")
    git(repo, "commit", "-qm", "binary and symlink target")

    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "link.py" or original_is_symlink(path))
    packet = build(repo, "HEAD", ".")
    assert "link.py" not in packet["scoped_files"]
    assert any(entry["path"] == "link.py" for entry in packet["omitted"])
    assert packet["binary_files"] == ["binary.dat"]


def test_no_diff_scoped_files_respect_section_and_excerpt_caps(repo):
    (repo / "large.py").write_text("x\n" * 250, encoding="utf-8")
    git(repo, "add", "large.py")
    git(repo, "commit", "-qm", "large scoped file")

    packet = build(repo, "HEAD", ".", budget={"max_changed_sections": 0})
    assert packet["scoped_files"] == {}
    assert {entry["path"] for entry in packet["omitted"]} >= {"module.py", "large.py"}

    packet = build(repo, "HEAD", "large.py", budget={"max_section_excerpt_lines": 0})
    assert packet["scoped_files"] == {}
    assert any(entry["path"] == "large.py" for entry in packet["omitted"])


def test_round_two_crlf_does_not_resend_previous_change(repo):
    (repo / "first.py").write_bytes(b"first_round = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "round one")
    first = build(repo, "HEAD~1", "*.py")
    (repo / "first.py").write_bytes(b"first_round = 1\r\n")
    (repo / "second.py").write_bytes(b"second_round = 1\r\n")
    second = build(repo, "HEAD~1", "*.py", previous_packet=first)
    assert "first_round" not in second["unified_diff"]
    assert "second_round" in second["unified_diff"]
