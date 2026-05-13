"""consensus.get_iteration_outcome MCP tool — read-only inspector for an
iteration's outcome.

Per iter-0021 converged plan (Section A): returns the parsed converged-plan
(workflow #4) or the sealed contributor artifacts (workflow #3) for an
iteration_dir, so operators / downstream tools can introspect outcomes
without re-running.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml


def _resolve_repo_root(override: str | None) -> Path:
    if override:
        return Path(override).resolve()
    env = os.environ.get("CONSENSUS_MCP_REPO_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def _resolve_path(p: str | Path, repo_root: Path) -> Path:
    """Resolve `p` against `repo_root` when relative. Mirrors consensus_run_iteration
    (codex pass-2 rev-001 — fixes the inconsistency where this tool advertised
    repo-relative paths in its schema but resolved against process cwd)."""
    p = Path(p)
    if not p.is_absolute():
        p = repo_root / p
    return p.resolve()


SCHEMA = {
    "name": "consensus.get_iteration_outcome",
    "description": (
        "Read-only inspector for an iteration's outcome. Returns the parsed "
        "converged-plan.yaml (workflow #4 propose-converge) if present, "
        "otherwise lists the sealed contributor artifacts (workflow #3 "
        "post-review or advisory). Does NOT re-run anything."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "iteration_dir": {
                "type": "string",
                "description": "Absolute or repo-relative path to the iteration directory.",
            },
            "repo_root": {
                "type": ["string", "null"],
                "description": (
                    "Override repo root. Defaults to CONSENSUS_MCP_REPO_ROOT "
                    "env var or current working directory. Relative iteration_dir "
                    "paths are resolved against this root."
                ),
            },
        },
        "required": ["iteration_dir"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "iteration_id": {"type": ["string", "null"]},
            "converged_plan": {"type": ["object", "null"]},
            "contributor_artifacts": {
                "type": "array",
                "description": "List of {contributor, path, sealed_at} for each sealed artifact found in the iteration dir.",
            },
            "effective_config_path": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
            "error_type": {"type": ["string", "null"]},
        },
    },
}


_KNOWN_CONTRIB_FILES = {
    "claude-review.yaml": "claude",
    "claude-proposal.yaml": "claude",
    "codex-review.yaml": "codex",
    "gemini-review.yaml": "gemini",
}


def handle(iteration_dir: str, repo_root: str | None = None) -> dict:
    """Read iteration outcome from disk. Never re-runs the engine."""
    try:
        rr = _resolve_repo_root(repo_root)
        iter_dir = _resolve_path(iteration_dir, rr)
        if not iter_dir.exists():
            return {
                "ok": False,
                "error": f"iteration_dir does not exist: {iter_dir}",
                "error_type": "FileNotFoundError",
            }
        if not iter_dir.is_dir():
            return {
                "ok": False,
                "error": f"iteration_dir is not a directory: {iter_dir}",
                "error_type": "NotADirectoryError",
            }

        iteration_id = iter_dir.name

        effective_config_path = iter_dir / "effective-config.yaml"
        effective_config_str: str | None = (
            str(effective_config_path) if effective_config_path.exists() else None
        )

        converged_plan_path = iter_dir / "converged-plan.yaml"
        converged_plan = None
        if converged_plan_path.exists():
            try:
                converged_plan = yaml.safe_load(converged_plan_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                return {
                    "ok": False,
                    "iteration_id": iteration_id,
                    "error": f"converged-plan.yaml is not valid YAML: {exc}",
                    "error_type": "YAMLError",
                }

        contributor_artifacts = []
        for fname, contributor in _KNOWN_CONTRIB_FILES.items():
            fpath = iter_dir / fname
            if not fpath.exists():
                continue
            entry = {
                "contributor": contributor,
                "path": str(fpath),
            }
            try:
                parsed = yaml.safe_load(fpath.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    if "sealed_at_utc" in parsed:
                        entry["sealed_at"] = parsed["sealed_at_utc"]
                    if "pass_id" in parsed:
                        entry["pass_id"] = parsed["pass_id"]
                    if "goal_satisfied" in parsed:
                        entry["goal_satisfied"] = parsed["goal_satisfied"]
            except yaml.YAMLError:
                # Malformed artifact — still report its existence.
                entry["parse_error"] = True
            contributor_artifacts.append(entry)

        return {
            "ok": True,
            "iteration_id": iteration_id,
            "converged_plan": converged_plan,
            "contributor_artifacts": contributor_artifacts,
            "effective_config_path": effective_config_str,
            "error": None,
            "error_type": None,
        }
    except Exception as exc:  # noqa: BLE001 — boundary translation
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


def register(registry) -> None:
    registry.register(SCHEMA["name"], SCHEMA, handle)
