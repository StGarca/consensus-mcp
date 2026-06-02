"""v1.30.3 - git-independent kimi isolation control + size-aware degrade.

The v1.30.2 disposable-copy isolation has two gaps in a CONSUMING repo with no `.git`
and heavy/derived dirs (the ebook2audiobook dogfood):

  - D2/D3: with no `.git`, `_repo_status_snapshot` returned {} -> a VACUOUS mutation
    control (the post-dispatch diff is always empty -> never blocks). v1.30.3 adds a
    git-INDEPENDENT content-hash manifest (`_filesystem_manifest_snapshot`) so a REAL
    control exists, and FAILS LOUD (_SnapshotIndexError) if the tree is too big to index
    within budget - never silently runs with zero control (the dissenter's invariant).
  - D4: if the disposable copy can't fit (ENOSPC), v1.30.2 failed the whole dispatch.
    v1.30.3 DEGRADES to no-copy (run against the real repo) - safe ONLY because the
    before/after snapshot above is now a real control that DETECTS + REJECTS any mutation.

This file covers the pure-unit surface (manifest + budget + degrade exception). The two
end-to-end degrade paths through main() live in test_dispatch_kimi.py beside the existing
main() seal/integrity tests (they reuse that file's goal-packet scaffolding).
"""
from __future__ import annotations

import errno
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consensus_mcp import _dispatch_kimi as dk  # noqa: E402


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------- D2: _filesystem_manifest_snapshot (content-hash control) ----------

def test_manifest_hashes_regular_files(tmp_path, monkeypatch):
    monkeypatch.delenv("CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS", raising=False)
    (tmp_path / "a.py").write_text("alpha", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("beta", encoding="utf-8")
    snap = dk._filesystem_manifest_snapshot(tmp_path)
    assert snap["a.py"] == _sha(b"alpha")
    assert snap["sub/b.txt"] == _sha(b"beta")  # POSIX-normalized relative path


def test_manifest_signs_symlink_by_target(tmp_path, monkeypatch):
    monkeypatch.delenv("CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS", raising=False)
    link = tmp_path / "thelink"
    try:
        link.symlink_to("target-a.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    snap = dk._filesystem_manifest_snapshot(tmp_path)
    assert snap["thelink"] == "link:" + _sha(b"target-a.txt")
    # repointing the symlink changes its signature (target rewrite is detected)
    link.unlink()
    link.symlink_to("target-b.txt")
    assert dk._filesystem_manifest_snapshot(tmp_path)["thelink"] == "link:" + _sha(b"target-b.txt")


def test_manifest_detects_add_remove_and_content_change(tmp_path, monkeypatch):
    monkeypatch.delenv("CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS", raising=False)
    f = tmp_path / "keep.py"
    f.write_text("v1", encoding="utf-8")
    (tmp_path / "gone.py").write_text("bye", encoding="utf-8")
    before = dk._filesystem_manifest_snapshot(tmp_path)

    f.write_text("v2-mutated", encoding="utf-8")        # content change
    (tmp_path / "gone.py").unlink()                      # removal
    (tmp_path / "new.py").write_text("hi", encoding="utf-8")  # addition
    after = dk._filesystem_manifest_snapshot(tmp_path)

    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    assert changed == {"keep.py", "gone.py", "new.py"}


def test_manifest_honors_gitignore_temp_and_env_ignore_dirs(tmp_path, monkeypatch):
    # A heavy/derived top-level dir is excluded if it is in _TEMP_WORKDIR_IGNORE_DIRS,
    # the repo's top-level .gitignore, OR CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS - the SAME
    # ignore set the disposable copy uses, so the control and the copy agree.
    builtin_ignored = next(iter(dk._TEMP_WORKDIR_IGNORE_DIRS))  # e.g. ".git"
    (tmp_path / builtin_ignored).mkdir()
    (tmp_path / builtin_ignored / "junk").write_text("x", encoding="utf-8")
    (tmp_path / "gitignored_dir").mkdir()
    (tmp_path / "gitignored_dir" / "big.bin").write_text("x" * 100, encoding="utf-8")
    (tmp_path / "env_dir").mkdir()
    (tmp_path / "env_dir" / "blob").write_text("y" * 100, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("code", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("gitignored_dir/\n", encoding="utf-8")
    monkeypatch.setenv("CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS", "env_dir")

    snap = dk._filesystem_manifest_snapshot(tmp_path)
    assert "src/real.py" in snap
    assert not any(k.startswith(f"{builtin_ignored}/") for k in snap)
    assert not any(k.startswith("gitignored_dir/") for k in snap)
    assert not any(k.startswith("env_dir/") for k in snap)


# ---------- D3: fail-LOUD on budget overrun (never zero control) ----------

def test_manifest_fails_loud_when_file_budget_exceeded(tmp_path, monkeypatch):
    monkeypatch.delenv("CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS", raising=False)
    monkeypatch.setattr(dk, "_SNAPSHOT_MAX_FILES", 0)  # any file trips it
    (tmp_path / "a.py").write_text("alpha", encoding="utf-8")
    with pytest.raises(dk._SnapshotIndexError, match="budget"):
        dk._filesystem_manifest_snapshot(tmp_path)


def test_manifest_fails_loud_when_byte_budget_exceeded(tmp_path, monkeypatch):
    monkeypatch.delenv("CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS", raising=False)
    monkeypatch.setattr(dk, "_SNAPSHOT_MAX_BYTES", 1)
    (tmp_path / "big.bin").write_text("xxxxxxxx", encoding="utf-8")  # 8 bytes > 1
    with pytest.raises(dk._SnapshotIndexError, match="budget"):
        dk._filesystem_manifest_snapshot(tmp_path)


def test_manifest_fails_loud_when_symlink_count_exceeds_budget(tmp_path, monkeypatch):
    # codex-rev-001: symlinks must count toward the FILE budget too - a symlink-only/heavy
    # no-.git tree must trip _SNAPSHOT_MAX_FILES, else D3's fail-LOUD invariant has a hole.
    monkeypatch.delenv("CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS", raising=False)
    monkeypatch.setattr(dk, "_SNAPSHOT_MAX_FILES", 1)
    try:
        (tmp_path / "l1").symlink_to("t1")
        (tmp_path / "l2").symlink_to("t2")  # 2 symlinks, budget 1 -> must raise
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    with pytest.raises(dk._SnapshotIndexError, match="budget"):
        dk._filesystem_manifest_snapshot(tmp_path)


def test_repo_status_snapshot_no_git_propagates_fail_loud(tmp_path, monkeypatch):
    # The fail-loud must reach the dispatcher: with no git, _repo_status_snapshot uses the
    # manifest, so an over-budget tree raises _SnapshotIndexError out of the bracket call
    # (the dispatch flow turns that into a clean ok:false dispatch failure).
    monkeypatch.delenv("CONSENSUS_MCP_KIMI_EXTRA_IGNORE_DIRS", raising=False)
    monkeypatch.setattr(dk.shutil, "which", lambda _name: None)  # no git binary
    monkeypatch.setattr(dk, "_SNAPSHOT_MAX_FILES", 0)
    (tmp_path / "a.py").write_text("alpha", encoding="utf-8")
    with pytest.raises(dk._SnapshotIndexError):
        dk._repo_status_snapshot(tmp_path)


# ---------- D4: ENOSPC -> degrade signal (errno form) ----------

def test_make_disposable_workdir_enospc_errno_raises_degrade(tmp_path, monkeypatch):
    # Complements the v1302 aggregated-shutil.Error (errno=None) test: the bare
    # OSError(errno=ENOSPC) form must ALSO raise _WorkdirTooLargeToIsolate (the degrade
    # signal), with the partial copy cleaned up so nothing leaks.
    monkeypatch.delenv("CONSENSUS_MCP_KIMI_WORKDIR_ROOT", raising=False)
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "f.txt").write_text("x", encoding="utf-8")  # no .git -> copytree path
    tmproot = tmp_path / "tmp"; tmproot.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmproot))

    def _boom(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", _boom)
    with pytest.raises(dk._WorkdirTooLargeToIsolate, match="ran out of space"):
        dk._make_disposable_workdir(repo)
    assert not list(tmproot.glob("kimi-workdir-*"))  # partial copy cleaned


def test_degrade_exception_is_not_oserror(tmp_path):
    # _WorkdirTooLargeToIsolate must NOT subclass OSError, so a stray `except OSError`
    # can't silently swallow the degrade signal - only the explicit callsite handles it.
    assert not issubclass(dk._WorkdirTooLargeToIsolate, OSError)
    assert issubclass(dk._WorkdirTooLargeToIsolate, RuntimeError)
