# Shared helpers for the CAD Super MCP scripts (dot-sourced: . "$PSScriptRoot\_common.ps1").
# Written for Windows PowerShell 5.1 as well as PowerShell 7.

function Write-Utf8NoBom {
    # Windows PowerShell 5.1's `Set-Content -Encoding UTF8` writes a BOM; TOML/JSON readers may not like it.
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Add-Utf8NoBom {
    param([string]$Path, [string]$Text)
    [System.IO.File]::AppendAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function ConvertTo-TomlString {
    # A TOML *literal* string ('...') needs no escaping for Windows paths, unless the path contains a
    # single quote (e.g. C:\Users\O'Brien) or a newline: then use a basic string with escapes.
    param([string]$Value)
    if ($Value -notmatch "['\r\n]") { return "'" + $Value + "'" }
    $escaped = $Value.Replace('\', '\\').Replace('"', '\"')
    return '"' + $escaped + '"'
}

function Test-PythonCandidate {
    # Returns sys.executable when the candidate is a real 64-bit CPython 3.11/3.12, else $null.
    # (The Microsoft Store "python.exe" stub prints an error and exits non-zero, so it is rejected here.)
    param([string[]]$Command)
    $code = "import sys,struct; ok = sys.version_info[:2] in [(3,11),(3,12)] and struct.calcsize('P')*8 == 64; print(sys.executable if ok else '')"
    try {
        $exe = $Command[0]
        $rest = @()
        if ($Command.Length -gt 1) { $rest = $Command[1..($Command.Length - 1)] }
        $out = & $exe @rest -c $code 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $path = ($out | Select-Object -First 1).ToString().Trim()
            if ($path) { return $path }
        }
    } catch { }
    return $null
}

function Find-Python311 {
    # Order: -Python argument, $env:CAD_SUPER_PYTHON, py launcher, python on PATH, uv-managed Python.
    param([string]$Requested)
    $candidates = @()
    if ($Requested) { $candidates += , @($Requested) }
    if ($env:CAD_SUPER_PYTHON) { $candidates += , @($env:CAD_SUPER_PYTHON) }
    $candidates += , @('py', '-3.12')
    $candidates += , @('py', '-3.11')
    $candidates += , @('python')
    foreach ($c in $candidates) {
        $found = Test-PythonCandidate -Command $c
        if ($found) { return $found }
    }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        foreach ($ver in @('3.12', '3.11')) {
            try {
                $p = (& $uv.Source python find $ver 2>$null | Select-Object -First 1)
                if ($LASTEXITCODE -eq 0 -and $p) {
                    $found = Test-PythonCandidate -Command @($p.ToString().Trim())
                    if ($found) { return $found }
                }
            } catch { }
        }
    }
    return $null
}

function Invoke-Checked {
    # Native commands do not throw in Windows PowerShell 5.1: check the exit code explicitly.
    param([string]$What, [scriptblock]$Block)
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}
