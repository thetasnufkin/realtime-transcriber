# setup.ps1 - one-time setup for the tray app (run it by double-clicking setup.bat).
#   Creates shortcuts on the Desktop and in the Start menu that start windows\tray_app.py
#   with uvw (no console window) directly from the WSL folder. Safe to run again.
#   Code updates in the repository take effect without running this again.
#   -Uninstall removes the shortcuts. -ShortcutDirs changes where they go (for testing).
# Keep this file ASCII only: PowerShell 5.1 reads it in the ANSI code page.
param(
    [switch]$Uninstall,
    [string[]]$ShortcutDirs = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
)
$ErrorActionPreference = 'Stop'
# powershell -File passes a list as one comma-separated string
$ShortcutDirs = @($ShortcutDirs | ForEach-Object { $_ -split ',' } | Where-Object { $_ })

# Shortcut name (Japanese "realtime captions"), built from char codes to keep this file ASCII
$AppName = -join [char[]](0x30EA, 0x30A2, 0x30EB, 0x30BF, 0x30A4, 0x30E0, 0x5B57, 0x5E55)
$DataDir = Join-Path $env:LOCALAPPDATA 'realtime-transcriber'
$Icon = Join-Path $DataDir 'app.ico'
$Tray = Join-Path $PSScriptRoot 'tray_app.py'

function Fail([string]$Message) {
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    exit 1
}

if ($Uninstall) {
    foreach ($dir in $ShortcutDirs) {
        $lnk = Join-Path $dir "$AppName.lnk"
        if (Test-Path -LiteralPath $lnk) {
            Remove-Item -LiteralPath $lnk
            Write-Host "Removed: $lnk"
        }
    }
    Write-Host "Shortcuts removed. Logs stay in $DataDir, settings stay in config.toml."
    exit 0
}

# 1. Checks: running from the WSL folder, uv installed, server environment ready in WSL
if ($PSScriptRoot -match '^\\\\wsl(\.localhost|\$)\\([^\\]+)(\\.*)$') {
    $Distro = $Matches[2]
    $Project = ($Matches[3] -replace '\\', '/') -replace '/windows$', ''
} else {
    Fail "Run setup.bat from the WSL folder (\\wsl.localhost\...\windows). Current folder: $PSScriptRoot"
}
$uv = Get-Command uv.exe -ErrorAction SilentlyContinue
$uvw = Get-Command uvw.exe -ErrorAction SilentlyContinue
if (-not $uv -or -not $uvw) {
    Fail "uv is not installed on Windows. See https://docs.astral.sh/uv/getting-started/installation/"
}
Write-Host "WSL distro: $Distro / project: $Project"
& wsl.exe -d $Distro -- bash -lc "cd '$Project' && test -f .venv/bin/activate && test -x scripts/start_server.sh"
if ($LASTEXITCODE -ne 0) {
    Fail "The server environment is not ready in WSL. Run 'uv sync' in $Project first."
}

# 2. Install the tray app's dependencies now and write the icon.
#    uvw shows no window, so installing them on the first start would look like nothing happens.
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
Write-Host "Preparing the tray app (installs its dependencies the first time)..."
& $uv.Source run $Tray --write-icon $Icon
if ($LASTEXITCODE -ne 0) {
    Fail "Could not prepare the tray app. See the messages above."
}

# 3. Shortcuts
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in $ShortcutDirs) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $lnk = $shell.CreateShortcut((Join-Path $dir "$AppName.lnk"))
    $lnk.TargetPath = $uvw.Source
    $lnk.Arguments = "run `"$Tray`""
    $lnk.WorkingDirectory = $DataDir  # a local folder: a \\wsl.localhost folder cannot be the current directory of cmd
    $lnk.IconLocation = "$Icon,0"
    $lnk.Description = 'Local real-time Japanese captions (tray app)'
    $lnk.Save()
    Write-Host "Created: $($lnk.FullName)"
}
Write-Host ''
Write-Host 'Done. Double-click the shortcut to start. Quit from the menu of the tray icon.'
