"""iter-0021 - Author a review-packet skeleton with embedded touched-file contents.

Per iter-0020 empirical finding: codex's read-only sandbox cannot reliably
read repo files even when given paths. The fix: embed file contents directly
in the review-packet so codex sees them as part of the prompt.

USAGE
-----

  python -m consensus_mcp._author_review_packet \
    --iteration-dir consensus-state/active/iteration-NNNN-name \
    --files consensus_mcp/tools/apply_codex_patch.py,consensus_mcp/tests/test_apply_codex_patch.py

OUTPUT
------

Writes/updates ``<iteration-dir>/review-packet.yaml`` with the
``defect_target`` block populated from disk:

  defect_target:
    files: [...]                       # paths the patch is expected to touch
    base_sha: <bundle_sha at author-time>
    touched_files_contents:
      "<path>": <full content>

If the file already exists, MERGES the defect_target block but does NOT
overwrite other fields (operator-friendly).

EXIT CODES
----------

  0 = review-packet authored / updated successfully
  1 = a listed file does not exist on disk
  2 = unexpected error (write fail, etc.)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


class OutsideRepoPathError(ValueError):
    """iter-0038 containment hardening: a `files` entry resolves outside repo_root.

    Symmetric to ``_dispatch_codex.OutsideRepoPathError``. Closes
    codex-rev-001 (HIGH) from iter-audit-2026-05-11-security: without this
    check, an absolute path / ``../`` traversal / in-repo symlink-to-outside
    would let any caller pull arbitrary file contents into review-packet.yaml
    and downstream sealed artifacts.
    """


def _is_contained(resolved: Path, repo_root_resolved: Path) -> bool:
    """Return True iff ``resolved`` is inside ``repo_root_resolved``.

    Mirrors ``_dispatch_codex._normalize_relative_to_repo``'s containment
    logic, including the iter-0033 claude-rev-003 Windows case-fold fallback:
    Path.relative_to is a string compare, and Windows filesystems are case-
    insensitive, so mixed-case repo_root vs path triggers false-positive
    rejection unless we re-compare case-folded.
    """
    try:
        resolved.relative_to(repo_root_resolved)
        return True
    except ValueError:
        if sys.platform == "win32":
            resolved_lc = str(resolved).lower().replace("\\", "/")
            root_lc = str(repo_root_resolved).lower().replace("\\", "/")
            if resolved_lc == root_lc or resolved_lc.startswith(root_lc.rstrip("/") + "/"):
                return True
        return False


def _resolve_repo_root(override: str | None) -> Path:
    """Resolve repo root from --repo-root override, env, or cwd (lenient).

    M1 (consult iteration-m1-hardening-design-4d7d2469) Q2 shim over the ONE
    blessed resolver (_paths.resolve_repo_root). Documented intent kept: the
    helper runs from the operator's cwd at iteration-author time, so with no
    override and no env key set it stays LENIENT (require_markers=False:
    nearest containment-marker ancestor, else cwd) instead of failing closed.
    """
    if override:
        return Path(override).resolve()
    from consensus_mcp._paths import resolve_repo_root
    return resolve_repo_root(require_markers=False)


def _read_file_text(repo_root: Path, rel: str) -> str:
    """Read a file's text content under repo_root. Raises FileNotFoundError if missing."""
    full = (repo_root / rel).resolve()
    if not full.exists():
        raise FileNotFoundError(f"file does not exist: {rel} (resolved to {full})")
    return full.read_text(encoding="utf-8")


def author_review_packet(
    iteration_dir: Path,
    files: list[str],
    repo_root: Path,
) -> Path:
    """Author or update <iteration_dir>/review-packet.yaml with defect_target block.

    Returns the path to the written review-packet.yaml. Raises FileNotFoundError
    if any listed file does not exist on disk.
    """
    iteration_dir = Path(iteration_dir).resolve()
    iteration_dir.mkdir(parents=True, exist_ok=True)
    review_packet_path = iteration_dir / "review-packet.yaml"

    # Load existing content for merge semantics.
    existing: dict = {}
    if review_packet_path.exists():
        try:
            loaded = yaml.safe_load(review_packet_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except yaml.YAMLError:
            existing = {}

    # iter-0038 containment hardening (codex-rev-001 from iter-audit-2026-05-
    # 11-security): every `files` entry must resolve inside repo_root before
    # we open it. Fail closed BEFORE any read_text so a malicious abs-path /
    # ../ traversal / in-repo-symlink-to-outside can't exfil arbitrary file
    # contents into review-packet.yaml.
    repo_root_resolved = repo_root.resolve()
    for rel in files:
        full = (repo_root / rel).resolve()
        if not _is_contained(full, repo_root_resolved):
            raise OutsideRepoPathError(
                f"review packet file {rel!r} resolves to {full} which is "
                f"outside repo_root {repo_root_resolved}. _author_review_packet "
                f"only reads files inside the repo. Move the file into the repo "
                f"or pass a path relative to it."
            )

    # Read all files (fail-closed if any missing).
    contents: dict[str, str] = {}
    for rel in files:
        contents[rel] = _read_file_text(repo_root, rel)

    # Compute base_sha via bundle_sha (the canonical author-time hash).
    from consensus_mcp._closure_invariant import bundle_sha
    base_sha = bundle_sha(repo_root, files)

    # Build the defect_target block. Preserve any operator-authored sub-fields
    # not managed by this helper (e.g. file/function/shape/reviewer_question
    # narrative); only files / base_sha / touched_files_contents are owned by
    # the helper.
    existing_defect_target = existing.get("defect_target") if isinstance(existing.get("defect_target"), dict) else {}
    new_defect_target = dict(existing_defect_target)
    new_defect_target["files"] = list(files)
    new_defect_target["base_sha"] = base_sha
    new_defect_target["touched_files_contents"] = contents

    merged = dict(existing)
    merged["defect_target"] = new_defect_target

    # Default scaffolding for newly-authored packets.
    merged.setdefault("schema_version", 1)
    merged.setdefault("iteration_id", iteration_dir.name)

    # iter-orchestrator-framing-bias-2026-05-22: MECHANICAL anchoring lint.
    # The orchestrator authors the goal_packet, so its framing/anchoring bias
    # rides through consensus uncaught. Lint the goal_packet's contributor-term
    # skew at author time + embed the report so contributors (and the operator)
    # see it WITHOUT relying on anyone noticing. Contributor set comes from
    # config (never hardcoded - that would be the bias under guard).
    # FAIL-LOUD, not fail-open (1.17 consensus review, unanimous top finding:
    # codex-001/gemini-001/kimi-001). An anti-bias gate that silently returns
    # "no anchoring" on a crash is indistinguishable from a clean result and lets
    # bias through unseen. So a real audit FAILURE is surfaced as an explicit
    # `anchoring_audit_error` key in the packet (distinguishable from clean) plus
    # a loud stderr line - never a silent empty result.
    try:
        anchoring = _anchoring_audit(iteration_dir, repo_root)
    except Exception as exc:
        merged["anchoring_audit_error"] = f"{type(exc).__name__}: {exc}"
        sys.stderr.write(
            f"[anchoring-lint] AUDIT FAILED - bias was NOT checked: "
            f"{type(exc).__name__}: {exc}\n")
        anchoring = None
    if anchoring:
        merged["anchoring_audit"] = anchoring
        for a in anchoring:
            sys.stderr.write(f"[anchoring-lint] {a['detail']}\n")

    review_packet_path.write_text(
        yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return review_packet_path


def _configured_contributors(repo_root: Path | None = None) -> list[str]:
    """The configured contributor set (NOT hardcoded). Prefers the project's
    active config (so a project that adds kimi / any AI is covered); falls back
    to the packaged default."""
    # Project config, if present at the conventional location.
    if repo_root is not None:
        for rel in (".consensus/config.yaml", ".consensus/config.yml"):
            cfg_path = repo_root / rel
            if cfg_path.exists():
                try:
                    from consensus_mcp.config import load, normalize
                    enabled = (((normalize(load(cfg_path)) or {}).get("contributors") or {})
                               .get("enabled")) or []
                    if enabled:
                        return list(enabled)
                except Exception as exc:
                    sys.stderr.write(f"[anchoring-lint] project config load failed ({exc}); "
                                     f"falling back to KNOWN_CONTRIBUTORS\n")
                    break
    # Fallback = the FULL known contributor allow-list (NOT the narrower
    # enabled-default), so anchoring DETECTION covers every known contributor
    # including kimi. (QA caught that using `enabled` here dropped kimi -
    # blinding the linter to the exact bias it guards against.)
    try:
        from consensus_mcp.config import KNOWN_CONTRIBUTORS
        return list(KNOWN_CONTRIBUTORS)
    except Exception as exc:
        sys.stderr.write(f"[anchoring-lint] could not load KNOWN_CONTRIBUTORS ({exc})\n")
        return []


def _anchoring_audit(iteration_dir: Path, repo_root: Path | None = None) -> list[dict]:
    """Run the mechanical anchoring linter on the iteration's goal_packet.yaml."""
    gp = iteration_dir / "goal_packet.yaml"
    if not gp.exists():
        return []
    contributors = _configured_contributors(repo_root)
    if len(contributors) < 2:
        return []
    # NO broad except here (1.17 review): real failures must PROPAGATE to the
    # caller, which records `anchoring_audit_error` (fail-loud). Swallowing here
    # would re-introduce the silent fail-open. The empty early-returns above are
    # legitimate "nothing to audit", not errors.
    from consensus_mcp._anchoring_lint import detect_anchoring
    text = gp.read_text(encoding="utf-8")
    flags = detect_anchoring(text, {"contributors": contributors})
    return [
        {"group": f.group, "skewed_to": f.skewed_to,
         "skew_fraction": f.skew_fraction, "counts": f.counts,
         "never_mentioned": f.never_mentioned, "detail": f.detail}
        for f in flags
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="consensus_mcp._author_review_packet",
        description=(
            "Author a review-packet skeleton with embedded touched-file contents. "
            "Per iter-0021: codex's read-only sandbox cannot reliably read repo "
            "files; embedding contents in the review-packet replaces the filesystem read."
        ),
    )
    p.add_argument("--iteration-dir", required=True,
                   help="Path to the iteration directory (must exist or will be created).")
    p.add_argument("--files", required=True,
                   help="Comma-separated list of repo-relative paths to embed.")
    p.add_argument("--repo-root", default=None,
                   help="Repo root override (defaults to CONSENSUS_MCP_REPO_ROOT or cwd).")

    ns = p.parse_args(argv)

    repo_root = _resolve_repo_root(ns.repo_root)
    files = [f.strip() for f in ns.files.split(",") if f.strip()]
    if not files:
        print("error: --files must list at least one path", file=sys.stderr)
        return 2

    try:
        path = author_review_packet(
            iteration_dir=Path(ns.iteration_dir),
            files=files,
            repo_root=repo_root,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OutsideRepoPathError as exc:
        # iter-0038: containment failure surfaces with exit code 1 (same
        # bucket as "file missing") so callers can treat both as fail-closed.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: unexpected failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
