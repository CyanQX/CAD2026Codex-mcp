$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Exe = Join-Path $Root ".venv-gateway\Scripts\cad-super-doctor.exe"
if (-not (Test-Path $Exe)) { throw "Gateway is not installed. Run .\scripts\install.ps1 first." }
& $Exe
exit $LASTEXITCODE
