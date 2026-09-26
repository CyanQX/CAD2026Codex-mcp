param([int]$Port = 8765)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Exe = Join-Path $Root ".venv-gateway\Scripts\cad-super-mcp.exe"
if (-not (Test-Path $Exe)) { throw "Gateway is not installed. Run .\scripts\install.ps1 first." }
& $Exe --transport streamable-http --host 127.0.0.1 --port $Port
