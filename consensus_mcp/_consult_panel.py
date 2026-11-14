"""Run independent dispatcher CLIs together and summarize their sealed results."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

import yaml

from consensus_mcp._start_consult import _configured_reviewers, _project_profiles, _resolve_repo
from consensus_mcp._atomic_io import atomic_write_text


class _SealIntegrityError(RuntimeError):
    failure_class = "integrity"


@contextmanager
def _panel_lock(path):
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _panel(rr, seats, tier, config, profiles):
    if tier not in {"quick", "standard", "deep"}:
        raise ValueError("tier must be quick, standard, or deep")
    if seats is None:
        seats = _configured_reviewers(rr)
        if seats is None:
            seats = ["claude", "codex", "kimi", "grok", "gemini"]
            if os.environ.get("CONSENSUS_MCP_DEEPSEEK_BASE_URL"):
                seats.append("deepseek")
            if os.environ.get("CONSENSUS_MCP_QWEN_BASE_URL"):
                seats.append("qwen")
        if tier == "quick":
            seats = (config.get("consult") or {}).get("quick_seats", seats[:2])
            if len(seats) != 2:
                raise ValueError("quick tier requires exactly two quick_seats")
        elif tier == "deep" and "opencode-deepseek" in profiles:
            command = (profiles["opencode-deepseek"].get("detect") or {}).get("command", "opencode")
            if shutil.which(command):
                seats = [*seats, "opencode-deepseek"]
    if isinstance(seats, str):
        seats = [s.strip() for s in seats.split(",") if s.strip()]
    if not isinstance(seats, (list, tuple)) or not seats:
        raise ValueError("at least one seat is required")
    seats = list(dict.fromkeys(seats))
    if tier == "quick" and len(seats) != 2:
        raise ValueError("quick tier requires exactly two seats")
    for seat in seats:
        profile = profiles.get(seat) or {}
        if not profile or profile.get("kind") in {"host_peer", "host"} and seat != "claude":
            raise ValueError(f"seat is not dispatchable: {seat}")
    return sorted(seats, key=lambda seat: seat != "deepseek")


SENSITIVITY_QUESTION = (
    "Does this project contain sensitive data (private, personal, customer or "
    "company-confidential code or data)? Answer yes to leave the {seats} seat(s) "
    "out of this run, or no to include them.")


def _sensitivity(value, config):
    """Resolve the yes/no sensitive-data answer: explicit argument, then the
    project's `sensitive_data` setting. Returns (answer, source) or (None, None)."""
    source = "argument"
    if value is None:
        if not isinstance(config, dict) or config.get("sensitive_data") is None:
            return None, None
        value, source = config["sensitive_data"], "config"
    from consensus_mcp import _sensitivity as policy
    answer = policy.normalize(value)
    if answer is None:
        raise ValueError("sensitive_data must be yes or no")
    return answer, source


def _command(seat, profile, iteration_dir, pass_id, timeout_seconds):
    if profile.get("kind") == "openai_compatible":
        module = "consensus_mcp._dispatch_openai_compat"
    elif profile.get("adapter") == "opencode":
        module = "consensus_mcp.contributors.opencode"
    elif seat in {"claude", "codex", "kimi", "grok", "gemini"}:
        module = f"consensus_mcp._dispatch_{seat}"
    else:
        from consensus_mcp._contributor_profiles import load_builtin_profiles
        builtin = load_builtin_profiles().get(seat)
        hint = ""
        if isinstance(builtin, dict):
            lost = sorted(k for k in ("kind", "adapter") if k in builtin and k not in profile)
            if lost:
                hint = (f"; contributors.profiles.{seat} replaces the built-in profile entirely "
                        f"and is missing {', '.join(lost)} - copy the full built-in profile and edit it")
        raise ValueError(f"no dispatcher entrypoint for seat: {seat} "
                         "(needs kind: openai_compatible, adapter: opencode, or a built-in CLI seat name)" + hint)
    target = iteration_dir / "review-packet.yaml"
    packet = yaml.safe_load(target.read_text(encoding="utf-8")) if target.is_file() else None
    mode = "review" if isinstance(packet, dict) and packet.get("pass_kind") in {"plan", "confirm"} else "proposal"
    command = [sys.executable, "-I", "-m", module, "--profile", seat,
            "--goal-packet", str(iteration_dir / "goal_packet.yaml"),
            "--iteration-dir", str(iteration_dir), "--reviewer-id", seat,
            "--pass-id", pass_id, "--mode", mode,
            "--review-target", str(target),
            "--timeout-seconds", str(math.ceil(timeout_seconds))]
    if isinstance(packet, dict) and packet.get("pass_kind") == "confirm":
        source = iteration_dir / "confirm-pass.md"
        custom_profile = profile.get("kind") == "openai_compatible" or profile.get("adapter") == "opencode" or seat == "claude"
        prefix = (profile.get("id_prefix") or f"{seat}-rev" if custom_profile else
                  "codex-rev" if seat == "codex" else "gemini-rev")
        schema_name = "codex_review_schema.json" if seat == "codex" and not custom_profile else "gemini_review_schema.json"
        schema_path = Path(__file__).with_name("dispatch_templates") / schema_name
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema_text = json.dumps(schema, ensure_ascii=True, indent=2).replace(
            "codex-rev" if schema_name.startswith("codex_") else "gemini-rev", prefix)
        prompt = source.read_text(encoding="utf-8") + (
            f"\n\nReturn only JSON matching this review output schema:\n{schema_text}\n"
            f"Finding IDs must match ^{prefix}-\\d+$; blocking_objections must contain "
            "only IDs of findings with blocking or critical severity. Set every "
            "patch_proposal to null and patch_not_proposed_reason to null or a short "
            "reason. No findings with goal_satisfied true confirms the edits. Otherwise "
            "every finding summary must start with [unfaithful] or [unrelated_change]. "
            "Do not suggest patches or new ideas.\n")
        rendered = iteration_dir / f"confirm-{seat}.md"
        atomic_write_text(rendered, prompt)
        command.extend(("--prompt-template", str(rendered)))
    return command


def _json_result(stdout):
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _sealed_packet(result, pass_id, rr, iteration, seat):
    from consensus_mcp.tools.review_write_and_seal import _canonical_yaml_sha256

    directory = rr / "consensus-state" / "active" / iteration
    archive = Path(os.environ.get("CONSENSUS_MCP_STATE_ROOT") or rr / "consensus-state") / "archive" / "review-passes"

    def contained(value, base):
        if not isinstance(value, str) or not value:
            raise ValueError("integrity: missing sealed path")
        path = Path(value)
        path = (path if path.is_absolute() else rr / path).resolve()
        path.relative_to(base.resolve())
        return path

    try:
        mirror = contained(result.get("sealed_path"), directory)
        path = contained(result.get("archive_sealed_path"), archive)
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("invalid archive packet size or type")
        packet = yaml.safe_load(path.read_text(encoding="utf-8"))
        if (result.get("pass_id") != pass_id or not isinstance(packet, dict)
                or packet.get("pass_id") != pass_id or packet.get("iteration_id") != iteration
                or packet.get("reviewer_id") != seat):
            raise ValueError("dispatcher did not return a sealed review for this pass")
        payload = dict(packet)
        digest = payload.pop("packet_sha256", None)
        if not digest or digest != result.get("packet_sha256") or digest != _canonical_yaml_sha256(payload):
            raise ValueError("sealed packet hash mismatch")
        mirrored = yaml.safe_load(mirror.read_text(encoding="utf-8"))
        if not isinstance(mirrored, dict) or mirrored.pop("packet_sha256", None) != digest or _canonical_yaml_sha256(mirrored) != digest:
            raise ValueError("mirror packet hash mismatch")
        index = yaml.safe_load((archive / "index.yaml").read_text(encoding="utf-8"))
        if not any(entry.get("id") == pass_id and entry.get("iteration_id") == iteration
                   and entry.get("reviewer_id") == seat and entry.get("packet_sha256") == digest
                   and contained(entry.get("path"), archive) == path
                   for entry in index.get("passes", []) if isinstance(entry, dict)):
            raise ValueError("sealed packet is missing from the archive index")
        return packet, path, mirror
    except Exception as exc:
        raise _SealIntegrityError(f"integrity: {exc}") from exc


def _cleanup(proc, row):
    from consensus_mcp._jobs import _kill_tree

    try:
        _kill_tree(proc)
    except Exception as exc:
        row["cleanup_error"] = str(exc)
    try:
        _, stderr = proc.communicate(timeout=2)
        if stderr:
            row["error_tail"] = stderr[-4000:]
    except Exception as exc:
        row["cleanup_error"] = "; ".join(filter(None, (row.get("cleanup_error"), str(exc))))
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                # Windows communicate readers may hold the stream lock. Let
                # closure finish with the reader without blocking the panel.
                def close(stream=pipe):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
                threading.Thread(target=close, daemon=True).start()


def _finish(proc, row, started, deadline, timeout_seconds, pass_id, rr, iteration):
    from consensus_mcp._dispatch_runner import classify_failure

    # Decide up front which limit binds: re-reading the clock after
    # TimeoutExpired mislabels the cause when the OS timer wakes a few ms early.
    deadline_binds = deadline <= started + timeout_seconds
    cleaned = False
    try:
        remaining = min(started + timeout_seconds, deadline) - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, max(0, remaining))
        stdout, stderr = proc.communicate(timeout=remaining)
        result = _json_result(stdout)
        if proc.returncode or not result.get("ok"):
            row["failure_class"] = result.get("failure_class") or classify_failure(
                RuntimeError(result.get("error") or stderr or stdout))
            row["error"] = result.get("error") or stderr.strip() or "dispatcher failed"
        else:
            packet, path, mirror = _sealed_packet(result, pass_id, rr, iteration, row["seat"])
            provenance = packet.get("dispatch_provenance") or {}
            if not isinstance(provenance, dict):
                raise ValueError("dispatch provenance must be a mapping")
            update = dict(status="sealed", failure_class=None, review_path=str(path),
                          archive_sealed_path=str(path), mirror_path=str(mirror),
                          packet_sha256=packet["packet_sha256"],
                          model_requested=provenance.get("model_requested", row["model_requested"]),
                          model_used=provenance.get("model_used") or provenance.get("model") or packet.get("family"),
                          fallback_reason=provenance.get("fallback_reason"))
            for source in (result, packet, provenance):
                tokens = source.get("token_counts") or source.get("usage")
                if tokens:
                    if not isinstance(tokens, dict):
                        raise ValueError("token counts must be a mapping")
                    update["token_counts"] = tokens
                reported = {key: source[key] for key in ("input_tokens", "output_tokens", "total_tokens") if key in source}
                if reported:
                    update["token_counts"] = reported
            row.update(update)
    except subprocess.TimeoutExpired as exc:
        row.update(failure_class="timeout", error="consult deadline exceeded" if deadline_binds else "seat timeout exceeded")
        if exc.stderr:
            row["error_tail"] = (exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr)[-4000:]
        _cleanup(proc, row)
        cleaned = True
    except Exception as exc:
        row.update(failure_class=classify_failure(exc), error=str(exc))
    finally:
        if not cleaned and proc.poll() is None:
            _cleanup(proc, row)
        row["wall_seconds"] = round(time.monotonic() - started, 6)
    return row


def consult_panel(iteration, seats=None, tier="standard", repo_root=None,
                  timeout_seconds=600, deadline_seconds=900, sensitive_data=None,
                  pass_kind="ideas") -> dict:
    started = time.monotonic()
    try:
        if not isinstance(iteration, str) or not iteration or iteration in {".", ".."} or "/" in iteration or "\\" in iteration:
            raise ValueError("iteration must be an iteration identifier")
        if pass_kind not in {"ideas", "plan", "confirm"}:
            raise ValueError("pass_kind must be ideas, plan, or confirm")
        if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0
                   for value in (timeout_seconds, deadline_seconds)):
            raise ValueError("timeout_seconds and deadline_seconds must be positive")
        rr = _resolve_repo(repo_root)
        directory = rr / "consensus-state" / "active" / iteration
        directory.resolve().relative_to(rr.resolve() / "consensus-state" / "active")
        if not (directory / "goal_packet.yaml").is_file() or not (directory / "review-packet.yaml").is_file():
            raise ValueError(f"consult iteration is not scaffolded: {iteration}")
        cfg = rr / ".consensus" / "config.yaml"
        config = yaml.safe_load(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("config must be a mapping")
        if pass_kind in {"plan", "confirm"}:
            summary = yaml.safe_load((directory / "consult-summary.yaml").read_text(encoding="utf-8"))
            if not isinstance(summary, dict) or not isinstance(summary.get("seats"), list):
                raise ValueError(f"{pass_kind} pass requires an ideas consult summary")
            if pass_kind == "confirm" and summary.get("tier") != "standard":
                raise ValueError("confirm pass is available only for standard-tier consults")
            original_seats = [row["seat"] for row in summary["seats"]]
            if seats is not None:
                requested = seats.split(",") if isinstance(seats, str) else seats
                if set(requested) != set(original_seats):
                    raise ValueError(f"{pass_kind} pass must use the same seats as the ideas pass")
            seats, tier = original_seats, summary.get("tier")
        profiles = _project_profiles(rr)
        panel = _panel(rr, seats, tier, config, profiles)
        # Seats marked sensitive_data_allowed: false never see sensitive data.
        # When nobody has said whether this project holds any, ask first.
        from consensus_mcp import _sensitivity as policy
        restricted = [s for s in panel if policy.is_restricted(profiles.get(s), s)]
        answer, source = _sensitivity(sensitive_data, config)
        excluded = []
        if restricted and answer is None:
            return {"ok": False, "status": "needs_sensitivity_answer", "iteration": iteration,
                    "restricted_seats": restricted,
                    "question": SENSITIVITY_QUESTION.format(seats=", ".join(restricted)),
                    "error": "ask the user whether this project contains sensitive data, then "
                             "re-run with sensitive_data yes or no (--sensitive-data on the CLI)"}
        if answer == "yes" and restricted:
            excluded = restricted
            panel = [s for s in panel if s not in restricted]
            if not panel:
                raise ValueError("no seats remain after excluding " + ", ".join(restricted)
                                 + " for sensitive data")
            if tier == "quick" and len(panel) != 2:
                raise ValueError("quick tier requires exactly two seats after excluding "
                                 + ", ".join(restricted) + " for sensitive data")
        sensitivity = {"sensitive_data": answer, "sensitivity_source": source,
                       "excluded_seats": excluded}
        contributors = config.get("contributors") or {}
        if not isinstance(contributors, dict):
            raise ValueError("contributors must be a mapping")
        adapters = contributors.get("adapters") or {}
        if not isinstance(adapters, dict):
            raise ValueError("contributors.adapters must be a mapping")
        prepared = []
        for seat in panel:
            profile = dict(profiles[seat])
            adapter = "opencode-deepseek" if profile.get("adapter") == "opencode" else seat
            overlay = adapters.get(adapter, {})
            if not isinstance(overlay, dict):
                raise ValueError(f"contributors.adapters.{adapter} must be a mapping")
            profile.update(overlay)
            pass_id = uuid4().hex
            # Validate entrypoints before owning any child processes.
            _command(seat, profile, directory, pass_id, timeout_seconds)
            prepared.append((seat, profile, pass_id))
    except Exception as exc:
        return {"ok": False, "status": "no_reviews", "error": str(exc)}
    with _panel_lock(directory / ".consult-panel.lock") as acquired:
        if not acquired:
            return {"ok": False, "status": "busy", "iteration": iteration}
        if pass_kind in {"plan", "confirm"}:
            from consensus_mcp._plan_passes import prepare_plan_pass, finish_plan_pass

            try:
                plan_pass = prepare_plan_pass(rr, directory, pass_kind=pass_kind)
                if plan_pass.get("status") == "needs_user_decision":
                    return plan_pass
                child = Path(plan_pass["child_dir"])
                result = _run_panel(rr, child, child.name, tier, prepared, started,
                                    timeout_seconds, deadline_seconds, sensitivity,
                                    pass_kind=pass_kind)
                return finish_plan_pass(directory, plan_pass, result)
            except Exception as exc:
                return {"ok": False, "status": "no_reviews", "error": str(exc)}
        return _run_panel(rr, directory, iteration, tier, prepared, started, timeout_seconds, deadline_seconds,
                          sensitivity)


def _run_panel(rr, directory, iteration, tier, prepared, started, timeout_seconds, deadline_seconds,
               sensitivity=None, pass_kind=None):
    sensitivity = sensitivity or {"sensitive_data": None, "sensitivity_source": None, "excluded_seats": []}
    deadline = started + deadline_seconds
    rows, pending, processes = [], [], []
    executor = ThreadPoolExecutor(max_workers=len(prepared))
    interrupted = False
    try:
        for seat, profile, pass_id in prepared:
            row = {"seat": seat, "model_requested": profile.get("model"), "model_used": None,
                   "fallback_reason": None, "status": "failed", "failure_class": "crash",
                   "wall_seconds": 0, "review_path": None, "pass_id": pass_id}
            rows.append(row)
            seat_start = time.monotonic()
            proc = None
            try:
                if seat_start >= deadline:
                    row.update(failure_class="timeout", error="consult deadline exceeded")
                    continue
                budget = min(timeout_seconds, deadline - seat_start)
                if budget <= 1:
                    row.update(failure_class="timeout", error="insufficient time for dispatcher watchdog")
                    continue
                child_timeout = max(1, math.floor(budget - min(30, budget / 2)))
                command = _command(seat, profile, directory, pass_id, child_timeout)
                options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
                           if os.name == "nt" else {"start_new_session": True})
                proc = subprocess.Popen(command, cwd=str(rr),
                                        env={**os.environ, "CONSENSUS_MCP_REPO_ROOT": str(rr), "CONSENSUS_MCP_PROJECT_ROOT": str(rr),
                                             "CONSENSUS_MCP_SENSITIVE_DATA": sensitivity["sensitive_data"] or ""}, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace", **options)
                processes.append((proc, row, None))
                future = executor.submit(_finish, proc, row, seat_start, deadline,
                                         timeout_seconds, pass_id, rr, iteration)
                processes[-1] = (proc, row, future)
                pending.append(future)
            except Exception as exc:
                row.update(error=str(exc), wall_seconds=round(time.monotonic() - seat_start, 6))
                if proc is not None:
                    _cleanup(proc, row)
        for future in pending:
            future.result()
    except BaseException:
        interrupted = True
        from consensus_mcp._jobs import _kill_tree
        for proc, row, future in processes:
            if future is not None and future.done() and proc.poll() is not None:
                continue
            try:
                _kill_tree(proc)
            except Exception as exc:
                row["cleanup_error"] = str(exc)
        raise
    finally:
        executor.shutdown(wait=not interrupted)
    sealed = sum(row["status"] == "sealed" for row in rows)
    summary = {"schema_version": 1, "run_id": uuid4().hex, "iteration": iteration, "tier": tier,
               "ok": bool(sealed), "status": "success" if sealed else "no_reviews",
               "total_wall_seconds": round(time.monotonic() - started, 6),
               "sealed_count": sealed, "failed_count": len(rows) - sealed, **sensitivity, "seats": rows}
    if pass_kind:
        summary["pass_kind"] = pass_kind
    if not sealed:
        summary["error"] = "no_reviews: no seat produced a sealed review"
    summary_path = directory / f"consult-summary-{summary['run_id']}.yaml"
    text = yaml.safe_dump(summary, sort_keys=False)
    atomic_write_text(summary_path, text)
    atomic_write_text(directory / "consult-summary.yaml", text)
    from consensus_mcp._plan_passes import record_run
    record_run(rr, iteration, tier, summary["run_id"], sealed)
    return {**summary, "summary_path": str(summary_path)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="consensus-mcp-consult")
    parser.add_argument("--iteration", required=True)
    parser.add_argument("--seats", nargs="+", help="seat names, separated by spaces or commas")
    parser.add_argument("--tier", choices=("quick", "standard", "deep"), default="standard")
    pass_group = parser.add_mutually_exclusive_group()
    pass_group.add_argument("--plan-pass", action="store_true", help="review the host's converged plan with the same panel")
    pass_group.add_argument("--confirm-pass", action="store_true", help="confirm standard-tier edits apply accepted objections without unrelated changes")
    parser.add_argument("--repo-root")
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--deadline-seconds", type=float, default=900)
    parser.add_argument("--sensitive-data", choices=("yes", "no"),
                        help="does the project contain sensitive data? yes leaves restricted seats out")
    args = vars(parser.parse_args(argv))
    confirm_pass = args.pop("confirm_pass")
    plan_pass = args.pop("plan_pass")
    args["pass_kind"] = "confirm" if confirm_pass else "plan" if plan_pass else "ideas"
    if args["seats"] is not None:
        args["seats"] = [seat for value in args["seats"] for seat in value.split(",") if seat]
    previous = None
    if os.name != "nt":
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        result = consult_panel(**args)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    print(json.dumps(result))
    if result.get("status") == "needs_sensitivity_answer":
        print(result["question"], file=sys.stderr)
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
