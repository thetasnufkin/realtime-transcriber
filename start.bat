@echo off
rem start.bat - double-click to start the WSL transcription server and the Windows capture client.
rem Put this file in the same folder as capture_client.py.
rem Extra options are passed to the server, e.g.:  start.bat --speed 2

setlocal
rem ===== settings =====
set DISTRO=Ubuntu
set PROJECT=~/projects/realtime-transcriber
set SERVER_ARGS=--final-model large-v3
rem ====================

start "transcribe-server" wsl -d %DISTRO% -- bash -lc "cd %PROJECT% && source .venv/bin/activate && python server/transcribe_server.py %SERVER_ARGS% %*; echo; read -p 'Press Enter to close...'"

title capture-client
uv run "%~dp0capture_client.py"
pause
