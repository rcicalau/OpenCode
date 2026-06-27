param(
    [string]$ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$pyagentDir = Join-Path $resolvedProjectRoot ".pyagent"
$configPath = Join-Path $pyagentDir "config.toml"
$authPath = Join-Path $repoRoot "src\codebuddy\ai_mart.py"

New-Item -ItemType Directory -Force -Path $pyagentDir | Out-Null

if (-not (Test-Path -LiteralPath $configPath)) {
    Copy-Item -LiteralPath (Join-Path $repoRoot "examples\project_config.azure_openai.toml") -Destination $configPath
    Write-Host "Created $configPath"
} else {
    Write-Host "Kept existing $configPath"
}

Write-Host "AI Mark auth client hook: $authPath"
Write-Host "Edit auth_client and base_url there. authenticate_broker().access_token must return a token."

Write-Host ""
Write-Host "Next:"
Write-Host "  1. Edit $authPath."
Write-Host "  2. Run: buddy auth check azure_openai"
Write-Host "  3. Run: buddy"
