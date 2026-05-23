#!/usr/bin/env bash
# consensus-mcp SessionStart hook.
#
# Re-injects the consensus orchestration discipline at the start of EVERY session,
# independent of plugin hook ordering. The superpowers `using-superpowers` bootstrap
# rides the superpowers plugin's own SessionStart hook, which can be superseded by
# another plugin's SessionStart hook after a reboot/resume (observed 2026-05-22:
# context-mode's cache-heal hook surfaced instead, and the orchestration discipline
# was lost). This hook makes the discipline the DEFAULT when working in this repo.
#
# Emits SessionStart additionalContext (the documented hook contract). Reads no stdin.
cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"CONSENSUS ORCHESTRATION — consensus-mcp project default. Before responding or acting: (1) invoke Skill superpowers:using-superpowers to load skill-first discipline; (2) for any feature / design / behavior change, use superpowers:brainstorming FIRST; (3) route design DECISIONS to a consensus consult — consensus is the approver, not the operator — and offer panel size (2/3/4) + framing (anchored vs open-contest) when launching; (4) maximize parallel-agent dispatch for independent work; (5) verify before claiming done (run the suite; seal provenance). If you see THIS directive but NOT the superpowers using-superpowers block in context, the bootstrap was superseded on resume — invoke that skill NOW before doing anything else."}}
JSON
