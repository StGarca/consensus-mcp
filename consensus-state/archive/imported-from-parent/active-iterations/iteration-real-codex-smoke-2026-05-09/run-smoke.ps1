$ErrorActionPreference = "Stop"

# Per codex review of smoke setup (2026-05-09):
# (1) AGENT_LOOP_MCP_REPO_ROOT must point at THIS repo, otherwise the helper
#     resolves repo_root from __file__.parent.parent.parent which lands wherever
#     the wheel is installed (e.g., global Python's Lib dir) instead of here.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..\..\..")).Path
Set-Location $repoRoot
$env:AGENT_LOOP_MCP_REPO_ROOT = $repoRoot
$env:AGENT_LOOP_MCP_RUN_REAL_CODEX_SMOKE = "1"

# (2) Force the python_env interpreter so the v1.10.3 hot-fixes (PATHEXT
#     resolution, .cmd preference, binary-mode UTF-8 stdin) are actually loaded.
#     PowerShell's bare `agent-loop-mcp-dispatch-codex` resolves to whatever's
#     first on PATH, which may be the global Python install (without v1.10.3
#     fixes). Invoking via `python_env\python.exe -m ...` pins the install we
#     want.
$pythonExe = ".\python_env\python.exe"

# Resolve full codex path from PowerShell (where it's findable).
$codexBin = (Get-Command codex.cmd).Source
$runId = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss-fff")
$reviewerId = "codex-real-smoke-$runId"
$passId = "$reviewerId-pass1"

Write-Host "REPO_ROOT: $env:AGENT_LOOP_MCP_REPO_ROOT"
Write-Host "Python:    $pythonExe"
Write-Host "Codex:     $codexBin"
Write-Host "Reviewer:  $reviewerId"
Write-Host "Pass:      $passId"

# (3) Pass a real --review-target so codex has something concrete to review,
#     not the F6 fallback "(not specified)" path.
& $pythonExe -m agent_loop_mcp._dispatch_codex `
    --goal-packet "scripts/agent_loop_mcp/tests/fixtures/dispatch_codex/goal_packet_smoke.yaml" `
    --iteration-dir "agent-loop/active/iteration-real-codex-smoke-2026-05-09" `
    --reviewer-id "$reviewerId" `
    --pass-id "$passId" `
    --codex-bin $codexBin `
    --review-target "agent-loop/active/iteration-real-codex-smoke-2026-05-09/review-target.md" `
    --timeout-seconds 300 `
    --smoke

Write-Host ""
Write-Host "Exit code: $LASTEXITCODE"
