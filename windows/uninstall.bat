@echo off
rem uninstall.bat - removes the Desktop and Start menu shortcuts created by setup.bat.
rem Logs (%LOCALAPPDATA%\realtime-transcriber) and config.toml are kept.
cd /d "%TEMP%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -Uninstall
echo.
pause
