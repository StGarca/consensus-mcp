"""Regression tests for H-4: SHA-1 dirty-detection ignores CRLF normalization.

With ``core.autocrlf=true`` (and no .gitattributes), git stores LF-normalized
blobs but the working tree keeps CRLF. The old ``_detect_dirty_paths`` hashed
the RAW working-tree bytes in Python, so the computed OID never matched the
stored (LF) blob OID -> every CRLF text file read as ALWAYS-DIRTY.

The fix delegates OID computation to ``git hash-object``, which applies the
repo's clean filter (autocrlf) so the OID matches the stored blob.

NOTE: these tests require real git and CANNOT mock it. Git's clean-filter
(autocrlf) behavior is the entire point under test; a mock would have to
reimplement that behavior, which would test the mock, not the code.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Make consensus_mcp importable without install.
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consensus_mcp import _snapshot_state as ss


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args} failed: {r.stderr}")
    return r


def _make_autocrlf_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal git repo with core.autocrlf=true and a CRLF text file
    under consensus-state/ (so it falls within SNAPSHOTTED_PATHS).

    Returns (repo_root, crlf_file_path).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    # The crux: autocrlf=true makes git store LF blobs from CRLF working trees.
    _git(["config", "core.autocrlf", "true"], repo)
    # Repo markers expected by _resolve_repo_root.
    (repo / "consensus_mcp").mkdir()
    (repo / "consensus_mcp" / "validators").mkdir()
    iter_dir = repo / "consensus-state" / "active" / "iteration-0001-alpha"
    iter_dir.mkdir(parents=True)
    crlf_file = iter_dir / "notes.txt"
    # Write CRLF bytes directly -- this is what makes the working tree differ
    # from the LF-normalized blob git stores.
    crlf_file.write_bytes(b"line one\r\nline two\r\nline three\r\n")
    # Gitignore the iteration dirs (mimics real repo); snapshot force-adds them.
    (repo / ".gitignore").write_text("consensus-state/active/iteration-*/\n", encoding="utf-8")
    (repo / "README.md").write_text("# crlf repo\n", encoding="utf-8")
    _git(["add", "README.md", ".gitignore"], repo)
    _git(["commit", "-m", "init"], repo)
    return repo, crlf_file


def _with_repo_root(monkeypatch, repo: Path) -> None:
    monkeypatch.setattr(ss, "_resolve_repo_root", lambda: repo)


def test_untouched_crlf_file_is_not_dirty(tmp_path, monkeypatch):
    """An UNTOUCHED CRLF text file must NOT be reported dirty after snapshot.

    This is the RED case today: the Python SHA-1 over raw CRLF bytes never
    matches the stored LF blob OID, so the file is wrongly flagged dirty.
    """
    repo, crlf_file = _make_autocrlf_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    ss.main(["snapshot", "--label", "base"])
    tag = _git(["tag", "-l", "snapshot-*-base"], repo).stdout.strip()
    dirty = ss._detect_dirty_paths(repo, target_tag=tag)
    rel = "consensus-state/active/iteration-0001-alpha/notes.txt"
    assert rel not in dirty, (
        f"untouched CRLF file wrongly flagged dirty (autocrlf normalization "
        f"not honored); got {dirty}"
    )


def test_modified_crlf_file_is_dirty(tmp_path, monkeypatch):
    """A genuinely modified CRLF file MUST still be reported dirty.

    Guards against over-correcting H-4 into 'never dirty' for CRLF files.
    """
    repo, crlf_file = _make_autocrlf_repo(tmp_path)
    _with_repo_root(monkeypatch, repo)
    ss.main(["snapshot", "--label", "base"])
    # Change the content (still CRLF) AFTER the snapshot.
    crlf_file.write_bytes(b"line one CHANGED\r\nline two\r\nline three\r\n")
    tag = _git(["tag", "-l", "snapshot-*-base"], repo).stdout.strip()
    dirty = ss._detect_dirty_paths(repo, target_tag=tag)
    rel = "consensus-state/active/iteration-0001-alpha/notes.txt"
    assert rel in dirty, (
        f"genuinely modified CRLF file must be flagged dirty; got {dirty}"
    )
