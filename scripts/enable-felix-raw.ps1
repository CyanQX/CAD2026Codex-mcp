param([switch]$Disable)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")
$ConfigPath = Join-Path $env:USERPROFILE ".cad-super-mcp\config.json"
if (-not (Test-Path $ConfigPath)) { throw "Config not found: $ConfigPath" }
$cfg = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$enable = -not $Disable
$cfg.safety | Add-Member -NotePropertyName enable_raw_felix -NotePropertyValue $enable -Force
if ($enable) {
    # The felix backend itself must be enabled too, otherwise raw calls fail with "backend is disabled".
    $cfg.backends.felix | Add-Member -NotePropertyName enabled -NotePropertyValue $true -Force
}
Write-Utf8NoBom -Path $ConfigPath -Text ($cfg | ConvertTo-Json -Depth 10)
if ($Disable) {
    Write-Host "Felix raw fallback disabled." -ForegroundColor Green
} else {
    Write-Host "Felix raw fallback ENABLED. Raw calls still require confirm=true and the gateway's command/LISP allow-lists." -ForegroundColor Yellow
}
Write-Host "Fully restart Codex (and any other MCP host): the gateway reads config.json only at startup." -ForegroundColor Cyan
