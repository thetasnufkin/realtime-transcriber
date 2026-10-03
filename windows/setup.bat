@echo off
rem setup.bat - double-click once to create the Desktop and Start menu shortcuts for the tray app.
rem Safe to run again. uninstall.bat removes the shortcuts.
rem (cmd may show a warning about UNC paths first. It is harmless.)
cd /d "%TEMP%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause
