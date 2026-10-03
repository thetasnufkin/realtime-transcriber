@echo off
rem start.bat - start the WSL transcription server, open the caption viewer in the browser,
rem and run the Windows capture client. Close both windows to stop.
rem Works from any folder: copy it to the Desktop to avoid the UNC path warning of cmd.
rem Extra options are passed to the server, e.g.:  start.bat --speed 2
rem Other settings (models, glossary, speed) are in config.toml or the viewer's settings.

setlocal
rem ===== settings =====
set DISTRO=Ubuntu
set PROJECT=~/projects/realtime-transcriber
set PORT=8765
rem ====================

rem Run from a local folder (cmd cannot use a \\wsl.localhost path as the current directory)
cd /d "%TEMP%"

rem Windows path of the project (\\wsl.localhost\...) to run capture_client.py from there
set WINPROJ=
for /f "usebackq delims=" %%i in (`wsl -d %DISTRO% -- bash -lc "wslpath -w %PROJECT%"`) do set "WINPROJ=%%i"
if not defined WINPROJ (
  echo [ERROR] Cannot find %PROJECT% in WSL distro "%DISTRO%". Check the settings in this file.
  pause
  exit /b 1
)

start "transcribe-server" wsl -d %DISTRO% -- bash -lc "cd %PROJECT% && ./scripts/start_server.sh --port %PORT% %*; echo; read -p 'Press Enter to close...'"

rem Open the viewer as soon as the server answers (it shows the model loading state).
rem Poll 127.0.0.1: "localhost" is about 2 s slower in PowerShell 5.1 (IPv6 is tried first).
start "" powershell -NoProfile -WindowStyle Hidden -Command "for($i=0; $i -lt 1800; $i++){ try { $null=Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:%PORT%/api/health'; break } catch { if($_.Exception.Response){ break }; Start-Sleep 1 } }; Start-Process 'http://localhost:%PORT%/'"

title capture-client
uv run "%WINPROJ%\capture_client.py" --url ws://127.0.0.1:%PORT%/ingest
pause
