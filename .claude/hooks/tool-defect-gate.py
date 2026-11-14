"""PreToolUse hook: block edits/writes/bash to consensus-mcp protected
paths unless a valid tool-defect proof artifact exists.

Per codex-meta-author-3 sealed proposal in iteration
iteration-claude-screwup-prevention-meta-2026-05-27 (4-of-4 cross-family
convergence on hybrid A+D: PreToolUse mechanical gate + stateless
Tooling Defect Diagnoser subagent).

Reads PreToolUse JSON from stdin. Exits 2 with stderr to block; 0 to allow.

Self-test: --self-test runs four cases (no-proof, invalid-proof,
valid-proof, unprotected) and exits 0 on PASS / 1 on FAIL.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path


PROTECTED_GLOBS = [
    "**/consensus_mcp/**/*.py",
    "**/_dispatch_*.py",
    ".claude/agents/**",
]

# Anchor PROOF_DIR to __file__ so it is the same path regardless of the
# cwd Claude Code launches the hook with (matches the universal-action-gate
# pattern). Layout: <repo>/.claude/hooks/X.py -> <repo>/tmp.
PROOF_DIR = Path(__file__).resolve().parent.parent.parent / "tmp"
PROOF_FILENAME_FMT = "tool-defect-proof-{hash}.yaml"

REQUIRED_PROOF_FIELDS = [
    "target_file_path",
    "target_sha256",
    "command",
    "input",
    "observed_output",
    "expected_output",
    "reproduction_command",
    "independent_observer_id",
    "created_at_utc",
]


def matches_protected_glob(path: str) -> bool:
    if not path:
        return False
    norm = path.replace(os.sep, "/")
    for glob in PROTECTED_GLOBS:
        if fnmatch.fnmatch(norm, glob):
            return True
        if fnmatch.fnmatch(norm, "*/" + glob):
            return True
    return False


def extract_target_paths(tool_name: str, tool_input: dict) -> list[str]:
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        p = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")
        return [p] if p else []
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if not isinstance(cmd, str) or not cmd:
            return []
        # Round-6 Instance-12 fix: shlex token-based extraction. Prior versions
        # used re.findall across the whole command string, which false-positive
        # matched protected-path patterns appearing in quoted documentation
        # content (e.g., a grok command embedding a review-packet whose canon
        # doc lists .claude/agents/ paths as documentation text). Token-based
        # scan only catches the patterns when they appear as actual argv tokens.
        # Mirrors the shlex fix already applied to dispatch-canon-validator.py.
        try:
            tokens = shlex.split(cmd, posix=True)
        except ValueError:
            return []  # Couldn't tokenize cleanly; be permissive.
        # Anchored token-fullmatch (Instance-12 follow-up): the regex must
        # match the ENTIRE token, and the token must not contain whitespace
        # (real file path tokens don't). Without anchors + whitespace exclusion,
        # the grok `-p` argument (one big shlex token containing the embedded
        # packet text) still matched because protected-path strings appear
        # inside it. With anchors, only standalone path tokens match.
        py_pat = re.compile(
            r"^[^\s'\"]*(?:consensus[-_]mcp|consensus_mcp|_dispatch_)[^\s'\"]*\.py$"
        )
        agents_pat = re.compile(r"^[^\s'\"]*\.claude/agents/[^\s'\"]+$")
        candidates: list[str] = []
        for t in tokens:
            if not t or any(c.isspace() for c in t):
                continue
            if py_pat.match(t) or agents_pat.match(t):
                candidates.append(t)
        return list(dict.fromkeys(candidates))
    return []


def parse_simple_yaml(text: str) -> dict | None:
    """Top-level key: value pairs only. Scalar values. No nesting, no lists."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            return None
        key, _, val = line.partition(":")
        key_stripped = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if not key_stripped or key != key_stripped:
            return None
        result[key_stripped] = val
    return result


def validate_proof(proof: dict, expected_target: str) -> tuple[bool, str]:
    missing = [f for f in REQUIRED_PROOF_FIELDS if f not in proof or not proof[f]]
    if missing:
        return False, "missing or empty required field(s): " + ", ".join(missing)
    sha = proof["target_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return False, f"target_sha256 not 64-char lowercase hex: {sha!r}"
    if not proof["target_file_path"].startswith("/"):
        return False, "target_file_path must be absolute"
    if Path(proof["target_file_path"]).resolve() != Path(expected_target).resolve():
        return False, (
            f"target_file_path in proof ({proof['target_file_path']!r}) does "
            f"not match the actual edit target ({expected_target!r})"
        )
    # codex-rev-003 r6 fix: verify the proof's target_sha256 against the
    # ACTUAL CURRENT contents of the target file. Prior versions only checked
    # format, so a stale or fabricated proof artifact (e.g., all-zero sha)
    # could authorize edits to a file whose contents have changed since the
    # proof was produced. Now: the proof must be produced against THE EXACT
    # bytes of the file the orchestrator is about to edit.
    try:
        actual_sha = hashlib.sha256(
            Path(expected_target).read_bytes()
        ).hexdigest()
    except OSError as exc:
        return False, (
            f"target file unreadable for sha256 verification: "
            f"{type(exc).__name__}: {exc}"
        )
    if sha != actual_sha:
        return False, (
            f"target_sha256 in proof ({sha!r}) does not match the actual "
            f"sha256 of the current target file contents ({actual_sha!r}). "
            "The proof was either produced against a different version of "
            "the file OR is fabricated; either way the proof is not valid "
            "for the current edit."
        )
    return True, "ok"


def proof_path_for(target_file_path: str) -> Path:
    abs_target = str(Path(target_file_path).resolve())
    h = hashlib.sha256(abs_target.encode("utf-8")).hexdigest()
    return PROOF_DIR / PROOF_FILENAME_FMT.format(hash=h)


def check_one(target: str) -> tuple[bool, str]:
    abs_target = str(Path(target).resolve())
    pp = proof_path_for(abs_target)
    if not pp.exists():
        return False, (
            f"BLOCKED: edit to protected consensus-mcp path {target!r} requires "
            f"a tool-defect proof artifact at {pp}. "
            "Dispatch the Tooling Defect Diagnoser subagent "
            "(.claude/agents/tooling-defect-diagnoser.md) to produce it. "
            "See docs/consensus/tool-defect-bypass.md."
        )
    try:
        text = pp.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"BLOCKED: proof artifact at {pp} unreadable: {exc}"
    parsed = parse_simple_yaml(text)
    if parsed is None:
        return False, (
            f"BLOCKED: proof artifact at {pp} is not valid simple-YAML "
            "(key: value pairs only)."
        )
    ok, reason = validate_proof(parsed, abs_target)
    if not ok:
        return False, f"BLOCKED: proof artifact at {pp} schema-invalid: {reason}"
    return True, "ok"


def main_payload(payload: dict) -> int:
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    targets = extract_target_paths(tool_name, tool_input)
    if not targets:
        return 0
    for t in targets:
        if not matches_protected_glob(t):
            continue
        allow, reason = check_one(t)
        if not allow:
            sys.stderr.write(reason + "\n")
            return 2
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    # FUCKUP_MODE switch (iter-fuckup-mode-switch-2026-05-27 converged plan, 4-of-4
    # cross-family ratified). Default OFF: gate idle unless marker file exists.
    # Resolved from __file__ to survive arbitrary cwd. Re-checked every invocation.
    _fuckup_marker = Path(__file__).resolve().parent.parent / "FUCKUP_MODE"
    if not _fuckup_marker.exists():
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    return main_payload(payload)


def self_test() -> int:
    import shutil

    failures: list[str] = []
    test_target_rel = "consensus_mcp/_dispatch_grok.py"
    test_target = str(Path(test_target_rel).resolve())
    payload = {"tool_name": "Write", "tool_input": {"file_path": test_target}}

    pp = proof_path_for(test_target)
    backup = pp.with_suffix(".bak") if pp.exists() else None
    if backup is not None:
        shutil.copy(pp, backup)
        pp.unlink()

    try:
        # Test 1: no proof artifact -> block (rc=2)
        rc1 = main_payload(payload)
        if rc1 != 2:
            failures.append(f"Test 1 (no-proof block): expected rc=2, got rc={rc1}")

        # Test 2: invalid proof -> block (rc=2)
        PROOF_DIR.mkdir(parents=True, exist_ok=True)
        pp.write_text("target_file_path: /wrong/path\n", encoding="utf-8")
        rc2 = main_payload(payload)
        if rc2 != 2:
            failures.append(f"Test 2 (invalid-proof block): expected rc=2, got rc={rc2}")

        # Test 3: valid proof -> allow (rc=0). Use REAL sha of the target
        # file per codex-rev-003 r6 sha-verification fix; all-zero is no
        # longer accepted.
        try:
            real_sha = hashlib.sha256(
                Path(test_target).read_bytes()
            ).hexdigest()
        except OSError:
            real_sha = None
        if real_sha is not None:
            valid_proof = "\n".join([
                f"target_file_path: {test_target}",
                f"target_sha256: {real_sha}",
                "command: self-test-command",
                "input: self-test-input",
                "observed_output: self-test-observed",
                "expected_output: self-test-expected",
                "reproduction_command: self-test-repro",
                "independent_observer_id: tooling-defect-diagnoser-self-test",
                "created_at_utc: 2026-05-27T00:00:00Z",
            ]) + "\n"
            pp.write_text(valid_proof, encoding="utf-8")
            rc3 = main_payload(payload)
            if rc3 != 0:
                failures.append(
                    f"Test 3 (valid-proof allow): expected rc=0, got rc={rc3}"
                )

            # Test 3b (codex-rev-003 r6): sha MISMATCH proof -> BLOCK.
            mismatch_proof = "\n".join([
                f"target_file_path: {test_target}",
                f"target_sha256: {'a' * 64}",
                "command: self-test-command",
                "input: self-test-input",
                "observed_output: self-test-observed",
                "expected_output: self-test-expected",
                "reproduction_command: self-test-repro",
                "independent_observer_id: tooling-defect-diagnoser-self-test",
                "created_at_utc: 2026-05-27T00:00:00Z",
            ]) + "\n"
            pp.write_text(mismatch_proof, encoding="utf-8")
            rc3b = main_payload(payload)
            if rc3b != 2:
                failures.append(
                    f"Test 3b (sha-mismatch block): expected rc=2, got rc={rc3b}"
                )

        # Test 4: unprotected edit -> allow (rc=0)
        payload4 = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/random.txt"}}
        rc4 = main_payload(payload4)
        if rc4 != 0:
            failures.append(f"Test 4 (unprotected allow): expected rc=0, got rc={rc4}")
    finally:
        if pp.exists():
            pp.unlink()
        if backup is not None:
            shutil.copy(backup, pp)
            backup.unlink()

    if failures:
        sys.stderr.write("SELF-TEST FAILURES:\n" + "\n".join(failures) + "\n")
        return 1
    sys.stderr.write(
        "SELF-TEST PASS: no-proof block, invalid-proof block, "
        "valid-proof allow, unprotected allow.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
