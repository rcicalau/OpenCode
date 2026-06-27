param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$BaseUrl = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$pyagentDir = Join-Path $resolvedProjectRoot ".pyagent"
$configPath = Join-Path $pyagentDir "config.toml"
$authPath = Join-Path $repoRoot "src\codebuddy\azure_auth.py"

New-Item -ItemType Directory -Force -Path $pyagentDir | Out-Null

if (-not (Test-Path -LiteralPath $configPath)) {
    Copy-Item -LiteralPath (Join-Path $repoRoot "examples\project_config.azure_openai.toml") -Destination $configPath
    Write-Host "Created $configPath"
} else {
    Write-Host "Kept existing $configPath"
}

Write-Host "Azure auth hook: $authPath"
Write-Host "Edit AzureAuthClient.get_token() there with your workspace auth code."

if ($BaseUrl.Trim()) {
    setx AZURE_OPENAI_BASE_URL $BaseUrl | Out-Null
    $env:AZURE_OPENAI_BASE_URL = $BaseUrl
    Write-Host "Saved AZURE_OPENAI_BASE_URL for future terminals and current process."
} else {
    Write-Host "AZURE_OPENAI_BASE_URL not changed. Pass -BaseUrl `"https://your-endpoint/openai/v1`" to set it."
}

Write-Host ""
Write-Host "Next:"
Write-Host "  1. Open a new terminal if you used -BaseUrl."
Write-Host "  2. Edit $authPath."
Write-Host "  3. Run: buddy auth check azure_openai"
Write-Host "  4. Run: buddy"
