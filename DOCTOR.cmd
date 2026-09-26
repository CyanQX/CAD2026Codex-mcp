@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\doctor.ps1"
echo.
if errorlevel 1 (
  echo Doctor reports that one or more ESSENTIAL backends are not ready.
) else (
  echo Essential backends are ready.
)
pause
