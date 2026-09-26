@echo off
setlocal
cd /d "%~dp0"
echo === CAD Super MCP Installer ===
echo (options: INSTALL.cmd -Workspace "D:\CAD\Workspace" -Python "C:\Python312\python.exe" -WithFelix)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
if errorlevel 1 (
  echo.
  echo Installation failed. Review the error above.
  pause
  exit /b 1
)
echo.
echo Installation completed. Next open AutoCAD 2026 with a TEST DWG, then run DOCTOR.cmd.
pause
