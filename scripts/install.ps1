param(
    [string]$Workspace = "$env:USERPROFILE\Documents\CAD-Super-Workspace",
    # Path to a Python 3.11/3.12 x64 interpreter (optional; auto-detected: py launcher, python, uv).
    [string]$Python = "",
    # Git ref of the Slacker backend to install (a tag or commit SHA is recommended for reproducible installs).
    [string]$SlackerRef = "main",
    # Build the optional felix .NET plugin (raw AutoCAD command / AutoLISP channel). OFF by default:
    # loading it opens a standing localhost command endpoint that is independent of the gateway's own switch.
    [switch]$WithFelix,
    # Kept for compatibility with v0.1 (felix is now opt-in, so this is a no-op).
    [switch]$SkipFelix
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")
$Root = Split-Path -Parent $PSScriptRoot
Write-Host "CAD Super MCP root: $Root" -ForegroundColor Cyan

$PythonExe = Find-Python311 -Requested $Python
if (-not $PythonExe) {
    throw "Python 3.11 or 3.12 (64-bit) was not found. Install it from python.org (tick the py launcher), or use uv (uv python install 3.12), or pass -Python <path>."
}
Write-Host "Python: $PythonExe" -ForegroundColor Green

New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Workspace "output") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Workspace ".cad_super") | Out-Null

function New-Venv {
    param([string]$Path, [string]$Label)
    Write-Host "Creating $Label environment..." -ForegroundColor Cyan
    Invoke-Checked "creating $Label venv" { & $PythonExe -m venv $Path }
    $py = Join-Path $Path "Scripts\python.exe"
    Invoke-Checked "upgrading pip in $Label venv" { & $py -m pip install --upgrade pip setuptools wheel }
    return $py
}

# Gateway: MCP SDK 2.x
$GwVenv = Join-Path $Root ".venv-gateway"
$GwPy = New-Venv -Path $GwVenv -Label "gateway"
Invoke-Checked "installing the gateway" { & $GwPy -m pip install -e $Root }

# best-cad-mcp: isolated because it requires MCP SDK 2.x and many AutoCAD dependencies.
$BestVenv = Join-Path $Root ".venv-best"
$BestPy = New-Venv -Path $BestVenv -Label "best-cad-mcp"
Invoke-Checked "installing best-cad-mcp" { & $BestPy -m pip install "best-cad-mcp[visual]==1.7.0" }

# Slacker: isolated because current upstream intentionally pins MCP SDK <2.
$SlackerVenv = Join-Path $Root ".venv-slacker"
$SlackerPy = New-Venv -Path $SlackerVenv -Label "Slacker"
Invoke-Checked "installing Slacker AutoCAD MCP" { & $SlackerPy -m pip install "https://github.com/Slacker-LLC/autocad-mcp/archive/$SlackerRef.zip" }

# Optional felix .NET plugin. It stays disabled for raw execution in the gateway config by default.
$FelixEnabled = $false
$FelixDll = ""
if ($WithFelix) {
    $Dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
    if ($Dotnet) {
        $Vendor = Join-Path $Root "vendor"
        New-Item -ItemType Directory -Force -Path $Vendor | Out-Null
        $Zip = Join-Path $Vendor "felix-autocad-mcp.zip"
        $Extract = Join-Path $Vendor "felix-src"
        if (Test-Path $Extract) { Remove-Item -Recurse -Force $Extract }
        Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/felixalmesberger/AUTOCAD-MCP/archive/refs/heads/main.zip" -OutFile $Zip
        Expand-Archive -Path $Zip -DestinationPath $Extract -Force
        $Repo = Get-ChildItem -Path $Extract -Directory | Select-Object -First 1
        if ($Repo) {
            & dotnet build (Join-Path $Repo.FullName "src\Infomatik.AutoCAD.Mcp.csproj") -c Release
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "felix build failed (exit $LASTEXITCODE). Continuing without the raw fallback plugin."
            } else {
                $Found = Get-ChildItem -Path $Repo.FullName -Filter "Infomatik.AutoCAD.Mcp.dll" -Recurse | Where-Object { $_.FullName -match "Release" } | Select-Object -First 1
                if ($Found) { $FelixDll = $Found.FullName; $FelixEnabled = $true }
                else { Write-Warning "felix built but Infomatik.AutoCAD.Mcp.dll was not found." }
            }
        }
    } else {
        Write-Warning ".NET 8 SDK not found. Skipping the felix raw-command/LISP fallback plugin."
    }
} else {
    Write-Host "felix raw fallback: not installed (opt-in with -WithFelix)." -ForegroundColor DarkGray
}

$BestExe = Join-Path $BestVenv "Scripts\cad-mcp.exe"
$SlackerExe = Join-Path $SlackerVenv "Scripts\autocad-mcp.exe"
$GwExe = Join-Path $GwVenv "Scripts\cad-super-mcp.exe"
foreach ($exe in @($GwExe, $BestExe, $SlackerExe)) {
    if (-not (Test-Path $exe)) { throw "Expected executable not found after install: $exe" }
}
$AuditPath = Join-Path $Workspace ".cad_super\audit.jsonl"
$ConfigDir = Join-Path $env:USERPROFILE ".cad-super-mcp"
$ConfigPath = Join-Path $ConfigDir "config.json"
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null

$Config = [ordered]@{
    workspace_root = $Workspace
    audit_log_path = $AuditPath
    backends = [ordered]@{
        best = [ordered]@{
            enabled = $true; kind = "stdio"; command = $BestExe; args = @(); cwd = $Workspace
            env = [ordered]@{ CAD_MCP_TOOL_PROFILE = "core"; CAD_MCP_WORKSPACE_ROOT = $Workspace }
            timeout_seconds = 180; session_mode = "auto"; protocol = "auto"
        }
        slacker = [ordered]@{
            enabled = $true; kind = "stdio"; command = $SlackerExe; args = @(); cwd = $Workspace
            env = [ordered]@{ ACAD_MCP_OUTPUT_ROOT = (Join-Path $Workspace "output") }
            timeout_seconds = 90; session_mode = "auto"; protocol = "legacy"
        }
        official = [ordered]@{ enabled = $true; kind = "autodiscover_http"; timeout_seconds = 30 }
        felix = [ordered]@{ enabled = $FelixEnabled; kind = "http"; url = "http://localhost:7410/"; timeout_seconds = 90 }
        product_help = [ordered]@{ enabled = $true; kind = "http"; url = "https://developer.api.autodesk.com/knowledge/public/v1/mcp"; timeout_seconds = 60 }
    }
    safety = [ordered]@{
        enable_raw_felix = $false
        require_confirm_for_destructive = $true
        require_confirm_for_save = $true
        require_confirm_for_plan_execute = $true
        audit_log = $true
    }
    runtime = [ordered]@{
        cross_process_lock = $true
        verify_writes = $true
    }
}
Write-Utf8NoBom -Path $ConfigPath -Text ($Config | ConvertTo-Json -Depth 10)

# Record exactly what was installed (reproducibility: the Slacker ref above defaults to a moving branch).
$lock = @("# CAD Super MCP install lock - generated $(Get-Date -Format s)", "# Slacker ref requested: $SlackerRef", "")
foreach ($pair in @(@("gateway", $GwPy), @("best-cad-mcp", $BestPy), @("slacker", $SlackerPy))) {
    $lock += "## $($pair[0])"
    $lock += (& $pair[1] -m pip freeze)
    $lock += ""
}
Write-Utf8NoBom -Path (Join-Path $ConfigDir "install-lock.txt") -Text ($lock -join "`r`n")

Write-Host ""
Write-Host "Installed successfully." -ForegroundColor Green
Write-Host "Config:    $ConfigPath"
Write-Host "Workspace: $Workspace"
Write-Host "Gateway:   $GwExe"
Write-Host "Versions:  $(Join-Path $ConfigDir 'install-lock.txt')"
if ($FelixEnabled) {
    Write-Host "Felix DLL: $FelixDll" -ForegroundColor Yellow
    Write-Host "In AutoCAD 2026 run NETLOAD and select that DLL, then MCPSTATUS. Raw fallback stays disabled until you run scripts\enable-felix-raw.ps1."
}
Write-Host "Next: open AutoCAD 2026 with a TEST DWG, then run DOCTOR.cmd" -ForegroundColor Cyan
