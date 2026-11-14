"""Deterministic, bounded review inputs built from local Git state."""
from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
import re
import subprocess

import yaml


DEFAULT_BUDGET = {
    "max_changed_sections": 8,
    "max_section_excerpt_lines": 120,
    "max_diff_lines": 600,
    "max_prior_review_items": 20,
    "include_full_file_only_if_lines_under": 250,
}


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def _text(data: bytes) -> str:
    return data.decode("utf-8").replace("\r\n", "\n")


def _budget(repo: Path, overrides: dict | None) -> dict:
    result = dict(DEFAULT_BUDGET)
    config_path = repo / ".consensus" / "config.yaml"
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        result.update({k: v for k, v in config.get("packet_budget", {}).items() if k in result})
    result.update({k: v for k, v in (overrides or {}).items() if k in result})
    for key, value in result.items():
        if type(value) is not int or value < 0:
            raise ValueError(f"packet_budget.{key} must be a nonnegative integer")
    return result


def _fit_diff(diff: str, limit: int) -> tuple[str, int]:
    """Keep complete hunks (or a complete metadata-only rename)."""
    lines = diff.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith("@@ ")]
    if not starts:
        return (diff, 0) if len(lines) <= limit else ("", len(lines))
    header = lines[:starts[0]]
    kept = []
    for start, end in zip(starts, starts[1:] + [len(lines)]):
        hunk = lines[start:end]
        if len(header) + len(kept) + len(hunk) <= limit:
            kept.extend(hunk)
    included = header + kept if kept else []
    return "".join(included), len(lines) - len(included)


def build(
    repo, base_ref, scope, *, budget=None, previous_packet=None, open_items=None,
    include_related_excerpts=False, packet_mode="diff",
) -> dict:
    """Build a packet without modifying the repository or contacting a model.

    ``scope`` is one Git pathspec or a sequence of pathspecs. A previous packet
    switches the base to its exact reviewed HEAD; prior review bodies are never
    copied. Context sections (small full files and related excerpts) share the
    section limit. Every excluded section or partial diff is recorded.
    """
    if packet_mode not in {"diff", "full"}:
        raise ValueError("packet_mode must be 'diff' or 'full'")
    repo = Path(repo).resolve()
    limits = _budget(repo, budget)
    paths = [scope] if isinstance(scope, str) else list(scope)
    if not paths:
        raise ValueError("scope must contain at least one Git pathspec")
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    prior_head = (previous_packet or {}).get("head_sha")
    if previous_packet is not None and not prior_head:
        raise ValueError("previous_packet must contain the reviewed head_sha")
    if prior_head:
        base = _git(repo, "rev-parse", "--verify", f"{prior_head}^{{commit}}").decode().strip()
    else:
        base = _git(repo, "merge-base", base_ref, head).decode().strip() if base_ref else head
    packet = {
        "base_sha": base, "head_sha": head, "budget_applied": limits,
        "packet_mode": packet_mode, "unified_diff": "", "full_files": {},
        "scoped_files": {},
        "binary_files": [], "related_files": [], "related_excerpts": [],
        "open_items": [], "omitted": [],
    }

    def omit(path, section, reason):
        packet["omitted"].append({"path": path, "section": section, "reason": reason})

    if not prior_head and not base_ref:
        omit("", "base_ref", "original base unavailable; using HEAD")
    names = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--ignore-cr-at-eol", "--name-status", "-z", "--find-renames", base, "--", *paths).decode("utf-8").split("\0")
    changes = []
    index = 0
    while index < len(names) and names[index]:
        status, path = names[index:index + 2]
        index += 2
        old_path = path
        if status.startswith(("R", "C")):
            path = names[index]
            index += 1
        changes.append((path, old_path, False))
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--", *paths).decode("utf-8").split("\0")
    changes.extend((path, path, True) for path in untracked if path)
    changes = sorted(set(changes))
    if not changes:
        # A no-diff packet still needs the target code so remote reviewers can
        # inspect unchanged files. Keep it in a dedicated, explicitly listed
        # section rather than pretending the files changed.
        scoped = _git(repo, "ls-files", "-z", "--", *paths).decode("utf-8").split("\0")
        for path in (name for name in scoped if name):
            if len(packet["scoped_files"]) >= limits["max_changed_sections"]:
                omit(path, "scoped_file", "max_changed_sections")
                continue
            file_path = repo / path
            if file_path.is_symlink() or not file_path.resolve().is_relative_to(repo):
                omit(path, "scoped_file", "symlink or path resolves outside repository")
                continue
            if not file_path.is_file():
                continue
            data = file_path.read_bytes()
            try:
                content = _text(data)
            except UnicodeDecodeError:
                packet["binary_files"].append(path)
                omit(path, "scoped_file", "binary file")
                continue
            if b"\0" in data:
                packet["binary_files"].append(path)
                omit(path, "scoped_file", "binary file")
                continue
            lines = content.splitlines(keepends=True)
            line_count = len(content.splitlines())
            if line_count < limits["include_full_file_only_if_lines_under"]:
                packet["scoped_files"][path] = {"content": content}
            else:
                excerpt_lines = lines[:limits["max_section_excerpt_lines"]]
                if not excerpt_lines:
                    omit(path, "scoped_file", "max_section_excerpt_lines")
                    continue
                packet["scoped_files"][path] = {
                    "excerpt": "".join(excerpt_lines),
                    "start_line": 1,
                    "end_line": len(excerpt_lines),
                }
                if len(excerpt_lines) < len(lines):
                    omit(path, "scoped_file", "max_section_excerpt_lines")
    touched = {path for path, old, _ in changes} | {old for path, old, _ in changes}
    remaining_diff = limits["max_diff_lines"]
    context_sections = 0
    symbols = set()
    for position, (path, old_path, is_untracked) in enumerate(changes):
        file_path = repo / path
        if file_path.is_symlink():
            data = file_path.readlink().as_posix().encode("utf-8")
        elif not file_path.resolve().is_relative_to(repo):
            omit(path, "file", "path resolves outside repository")
            continue
        else:
            data = file_path.read_bytes() if file_path.is_file() else b""
        if is_untracked:
            raw_diff = ""
        else:
            try:
                raw_diff = _text(_git(repo, "diff", "--no-ext-diff", "--no-textconv", "--ignore-cr-at-eol", "--find-renames", "--unified=3", base, "--", old_path, path))
            except UnicodeDecodeError:
                packet["binary_files"].append(path)
                omit(path, "diff", "non-UTF-8 diff")
                continue
        try:
            content = _text(data)
        except UnicodeDecodeError:
            content = None
        if b"\0" in data or content is None or "\nBinary files " in "\n" + raw_diff:
            packet["binary_files"].append(path)
            omit(path, "diff", "binary file")
            continue
        if is_untracked:
            mode = "120000" if file_path.is_symlink() else "100644"
            raw_diff = f"diff --git a/{path} b/{path}\nnew file mode {mode}\n"
            raw_diff += "".join(difflib.unified_diff([], content.splitlines(keepends=True), fromfile="/dev/null", tofile=f"b/{path}"))
            if content and not content.endswith("\n"):
                raw_diff += "\n\\ No newline at end of file\n"
        symbols.add(Path(path).stem)
        symbols.update(re.findall(r"^[+-]\s*(?:async\s+)?(?:class|def|function)\s+(\w+)", raw_diff, re.MULTILINE))
        if position >= limits["max_changed_sections"]:
            omit(path, "diff", "max_changed_sections")
            omit(path, "full_file", "max_changed_sections")
            continue
        included, omitted_lines = _fit_diff(raw_diff, remaining_diff)
        packet["unified_diff"] += included
        if omitted_lines:
            omit(path, "diff", f"max_diff_lines: omitted {omitted_lines} lines")
        remaining_diff -= len(included.splitlines())
        if packet_mode == "diff":
            omit(path, "full_file", "packet mode diff")
        elif file_path.is_symlink():
            omit(path, "full_file", "symlink")
        elif not file_path.exists():
            continue
        elif len(content.splitlines()) >= limits["include_full_file_only_if_lines_under"]:
            omit(path, "full_file", "include_full_file_only_if_lines_under")
        elif context_sections >= limits["max_changed_sections"]:
            omit(path, "full_file", "max_changed_sections")
        else:
            packet["full_files"][path] = content
            context_sections += 1

    # Search tracked text only. Paths are useful to tool seats without resending
    # files; HTTP seats can request the same bounded hit excerpts inline.
    symbols.discard("")
    if symbols:
        pattern = r"\b(" + "|".join(re.escape(symbol) for symbol in sorted(symbols)) + r")\b"
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "grep", "-n", "-I", "-E", "-e", pattern, "--", "."],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode not in (0, 1):
            raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
        hits = {}
        for hit in result.stdout.decode("utf-8", errors="replace").splitlines():
            match = re.match(r"(.+?):(\d+):(.*)", hit)
            if match and match[1] not in touched:
                hits.setdefault(match[1], []).append({"line": int(match[2]), "text": match[3]})
        if hits and include_related_excerpts and context_sections and context_sections == limits["max_changed_sections"]:
            displaced = next(reversed(packet["full_files"]))
            del packet["full_files"][displaced]
            context_sections -= 1
            omit(displaced, "full_file", "max_changed_sections: reserved related excerpt")
        for position, path in enumerate(sorted(hits, key=lambda p: (-len(hits[p]), p))):
            if position >= limits["max_changed_sections"]:
                omit(path, "related_files", "max_changed_sections")
                continue
            packet["related_files"].append(path)
            if include_related_excerpts:
                if context_sections >= limits["max_changed_sections"]:
                    omit(path, "related_excerpt", "max_changed_sections")
                    continue
                excerpt = hits[path][:limits["max_section_excerpt_lines"]]
                packet["related_excerpts"].append({"path": path, "hits": excerpt})
                context_sections += 1
                if len(excerpt) < len(hits[path]):
                    omit(path, "related_excerpt", "max_section_excerpt_lines")
    items = open_items if open_items is not None else (previous_packet or {}).get("open_items", [])
    for item in items:
        if not isinstance(item, dict) or item.get("status", item.get("disposition")) not in {"unresolved", "deferred", "open"}:
            continue
        if len(packet["open_items"]) >= limits["max_prior_review_items"]:
            omit(str(item.get("id", "")), "open_items", "max_prior_review_items")
        else:
            packet["open_items"].append(item)
    packet["packet_sha256"] = hashlib.sha256(yaml.safe_dump(packet, sort_keys=True).encode("utf-8")).hexdigest()
    return packet
