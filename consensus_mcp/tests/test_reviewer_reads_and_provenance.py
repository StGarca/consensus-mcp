"""DriverLogs FU-06 / FU-11 regressions.

FU-06: a reviewer reported it could not read files because nothing told it
that read-only commands were allowed (the ideas-pass templates even called the
context "embedded for your reference"). Every reviewer template now states that
read-only shell commands are reading tools.

FU-11: a reviewer cited the packet's head_sha as the identity of the bytes it
reviewed, but packets are built from the working tree. The packet now records
that explicitly, including whether the scoped files differ from head_sha.
"""
import subprocess
from pathlib import Path

import pytest

from consensus_mcp._review_packet import build

TEMPLATES = Path(__file__).resolve().parents[1] / "dispatch_templates"
REVIEWER_TEMPLATES = [
    "codex_proposal_template.md", "gemini_proposal_template.md", "grok_proposal_template.md",
    "plan_review.md", "confirm_review.md",
    "codex_review_template.md", "gemini_review_template.md", "grok_review_template.md",
    "kimi_review_template.md", "host_peer_review_template.md",
]


def git(repo, *args):
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=repo,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Packet Test")
    git(tmp_path, "config", "user.email", "packet@example.invalid")
    (tmp_path / "module.py").write_text("def original():\n    return 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


@pytest.mark.parametrize("name", REVIEWER_TEMPLATES)
def test_every_reviewer_template_allows_read_only_commands(name):
    text = (TEMPLATES / name).read_text(encoding="ascii")
    assert "read-only shell commands" in text.lower()
    assert "`rg`" in text and "`Get-Content`" in text
    assert "embedded for your reference" not in text


@pytest.mark.parametrize("name", ["codex_proposal_template.md", "plan_review.md", "confirm_review.md"])
def test_read_permission_still_forbids_changes(name):
    text = (TEMPLATES / name).read_text(encoding="ascii")
    assert "Do not modify files, run tests, install packages, or execute project code." in text


def test_clean_packet_states_working_tree_and_matching_head(repo):
    packet = build(repo, None, "*.py")
    assert packet["reviewed_bytes"] == "working_tree"
    assert packet["worktree_dirty"] is False
    assert "scoped files match it" in packet["provenance_note"]


def test_dirty_packet_says_head_sha_does_not_identify_reviewed_bytes(repo):
    (repo / "module.py").write_text("def original():\n    return 2\n", encoding="utf-8")
    packet = build(repo, None, "*.py")
    assert packet["worktree_dirty"] is True
    assert "head_sha does NOT identify the reviewed bytes" in packet["provenance_note"]


def test_untracked_file_in_scope_marks_packet_dirty(repo):
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert build(repo, None, "*.py")["worktree_dirty"] is True


def test_change_outside_scope_does_not_mark_packet_dirty(repo):
    (repo / "notes.txt").write_text("unrelated\n", encoding="utf-8")
    assert build(repo, None, "*.py")["worktree_dirty"] is False
