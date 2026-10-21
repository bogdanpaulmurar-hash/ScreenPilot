@echo off
setlocal
cd /d "%~dp0"
echo  ScreenPilot - dezinstalare (setarile si capturile raman)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall.ps1"
echo.
pause
