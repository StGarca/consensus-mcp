"""Dispatch Kimi (Moonshot Kimi Code CLI) as a 4th consensus reviewer.

Per operator directive 2026-05-21: Kimi K2.6 (kimi-code/kimi-for-coding, the
subscription's top model) becomes the default 4th reviewer alongside
claude + codex + gemini. Auth is OAuth/subscription via `kimi login` (NOT
the metered API), mirroring how codex (ChatGPT) and gemini (Google) auth.

Design: this adapter lives in abkgen and REUSES consensus-mcp's public
dispatch machinery (prompt build, JSON parse, T6 seal) by importing
`consensus_mcp._dispatch_gemini` and monkeypatching ONLY its CLI-invocation
function (`_invoke_gemini`) with a Kimi invoker. consensus-mcp itself is
NOT modified (it is a separate, operator-protected project).

Usage (mirrors consensus-mcp-dispatch-gemini):
    python_env/python.exe run/_dispatch_kimi.py \
        --goal-packet <iter>/goal_packet.yaml \
        --iteration-dir <iter> \
        --reviewer-id kimi-<iter>-1 \
        --pass-id kimi-<iter>-pass1 \
        --mode proposal \
        --review-target <iter>/review-packet.yaml
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

import consensus_mcp._dispatch_gemini as g

KIMI_BIN = os.environ.get("KIMI_BIN", "kimi")
# The subscription's managed model is `kimi-code/kimi-for-coding` (the default
# in ~/.kimi/config.toml after `kimi login`); it has capabilities=["thinking",...].
# There is no `k26` alias to pass — the managed provider serves the plan's top
# model directly. We pass --thinking for deep-reasoning review (analogous to
# codex at xhigh reasoning effort).
KIMI_MODEL_NOTE = "kimi-code/kimi-for-coding (managed, +thinking)"

# kimi --quiet prints the final message then a footer line:
#   "To resume this session: kimi -r <uuid>"
# Strip it (and any leading thinking/preamble) before the JSON parser sees it.
_RESUME_FOOTER_RE = re.compile(r"\n*To resume this session:\s*kimi -r \S+\s*$", re.MULTILINE)


def _strip_kimi_chrome(text: str) -> str:
    text = _RESUME_FOOTER_RE.sub("", text)
    return text.strip()


def _invoke_kimi(
    prompt: str,
    gemini_bin: str = "kimi",   # ignored; kept for signature compat with _invoke_gemini
    model: str = "",            # ignored; managed model is set by the subscription
    timeout_seconds: int = 1800,  # was 600 — too short; kimi timed out on long
                                  # consults, wasting allowance AND discarding a
                                  # strong contributor. Match codex/gemini (1800s).
    repo_root=None,
    log_path=None,
    anchors=None,
    **_kw,
) -> str:
    """Shell out to `kimi --quiet --thinking`, prompt piped via stdin.

    --quiet == --print --output-format text --final-message-only (clean output,
    non-interactive, auto-approves). -w sets the working dir so kimi auto-loads
    project context (AGENTS.md / project rules) at startup.

    Input handling (verified 2026-05-21): kimi reads the prompt from STDIN when
    no -p is given; if -p IS given, kimi uses -p and IGNORES stdin (unlike
    gemini, which concatenates). So the full prompt MUST go via stdin with NO
    -p flag — the earlier `-p "Now respond..."` trigger caused kimi to ignore
    the packet entirely and emit non-JSON (parse fail).
    """
    cmd = [KIMI_BIN, "--quiet", "--thinking"]
    if repo_root is not None:
        cmd += ["-w", str(repo_root)]
    # Instrument duration: kimi runs --quiet (final-message-only) so there is no
    # progress signal to silence-detect on (unlike codex/gemini which stream);
    # the total timeout is the only hang-detector. Logging elapsed time to stderr
    # gives the data to set a sane ceiling instead of guessing, and tells the
    # operator/orchestrator HOW LONG things actually take ("when do they land").
    _t0 = time.perf_counter()
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(repo_root) if repo_root is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        _el = time.perf_counter() - _t0
        sys.stderr.write(f"[kimi-timing] TIMED OUT after {_el:.0f}s (ceiling {timeout_seconds}s)\n")
        raise g.GeminiInvocationError(f"kimi timed out after {timeout_seconds}s") from exc
    except FileNotFoundError:
        raise g.GeminiInvocationError(f"kimi binary not found: {KIMI_BIN}") from None
    _el = time.perf_counter() - _t0
    if result.returncode != 0:
        sys.stderr.write(f"[kimi-timing] FAILED (rc={result.returncode}) after {_el:.0f}s\n")
        raise g.GeminiInvocationError(
            f"kimi exited {result.returncode}; stderr tail: {(result.stderr or '')[-500:]}"
        )
    sys.stderr.write(f"[kimi-timing] landed in {_el:.0f}s (ceiling {timeout_seconds}s)\n")
    return _strip_kimi_chrome(result.stdout or "")


def _seal_kimi(packet, iter_dir, sealed_filename="kimi-review.yaml", **kw):
    """Force the iteration-local sealed file to kimi-review.yaml.

    The gemini dispatcher hardcodes sealed_filename="gemini-review.yaml" at its
    _seal_via_t6 call site, which would clobber the gemini review. Override it
    so kimi seals to its own file.
    """
    return _ORIG_SEAL(packet, iter_dir, sealed_filename="kimi-review.yaml", **kw)


_ORIG_SEAL = g._seal_via_t6


def main(argv: list[str] | None = None) -> int:
    # Monkeypatch the gemini dispatcher's CLI invoker with the Kimi one, and
    # its seal helper so the iteration-local file is kimi-review.yaml (not
    # gemini-review.yaml). Everything else (prompt build, JSON parse, T6 seal
    # mechanics, logging) is reused unchanged.
    g._invoke_gemini = _invoke_kimi
    g._seal_via_t6 = _seal_kimi
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default the bin/model flags to kimi-friendly values if not supplied
    # (they're ignored by _invoke_kimi but keep g.main's argparse happy).
    if "--gemini-bin" not in argv:
        argv += ["--gemini-bin", KIMI_BIN]
    return g.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
