param([string]$Gateway = "")
. (Join-Path $PSScriptRoot "_common.ps1")
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Gateway) { $Gateway = Join-Path $Root ".venv-gateway\Scripts\cad-super-mcp.exe" }
$Config = Join-Path $env:USERPROFILE ".cad-super-mcp\config.json"
$g = ConvertTo-TomlString $Gateway
$c = ConvertTo-TomlString $Config
@"
[mcp_servers.codex-CADmcp]
command = $g
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 240
default_tools_approval_mode = "writes"

[mcp_servers.codex-CADmcp.env]
CAD_SUPER_CONFIG = $c
"@
