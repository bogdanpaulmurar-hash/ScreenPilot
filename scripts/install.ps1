<#
.SYNOPSIS
    Installs ScreenPilot for the current user — no admin rights, no .exe.

.DESCRIPTION
    * Creates a private virtual environment in %LOCALAPPDATA%\ScreenPilot\venv,
      so ScreenPilot never shares packages with your other Python apps.
    * Installs ScreenPilot from this folder (pip install ".[ui]").
    * Creates a Start-menu shortcut with the ScreenPilot icon.
    * Optionally starts ScreenPilot with Windows (-Autostart).
    * Starts it with pythonw.exe: no console window, only the tray icon.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Autostart
#>
param(
    [switch]$Autostart,
    [string]$Python = "",
    [string]$InstallDir = "$env:LOCALAPPDATA\ScreenPilot",
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $InstallDir "venv"

function Find-Python {
    if ($Python) { return @($Python) }
    if (Get-Command py -ErrorAction SilentlyContinue) { return @("py", "-3") }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @("python") }
    throw "Python 3.10+ was not found. Install it from python.org (per user, no admin needed) and run this script again."
}

Write-Host "ScreenPilot installer" -ForegroundColor Cyan
Write-Host "  source : $source"
Write-Host "  target : $venv"

$py = Find-Python
$exe = $py[0]
$pyArgs = @()
if ($py.Count -gt 1) { $pyArgs = $py[1..($py.Count - 1)] }

& $exe @pyArgs -c "import sys; assert sys.version_info >= (3, 10), sys.version" 2>$null
if ($LASTEXITCODE -ne 0) { throw "ScreenPilot needs Python 3.10 or newer." }

if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "Creating the private environment..."
    & $exe @pyArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Creating the virtual environment failed." }
}

$vpython = Join-Path $venv "Scripts\python.exe"
$vpythonw = Join-Path $venv "Scripts\pythonw.exe"

# Stop a running copy so its files can be replaced.
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -eq $vpythonw } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "Installing ScreenPilot and its dependencies (Pillow, PySide6-Essentials)..."
& $vpython -m pip install --disable-pip-version-check --upgrade pip | Out-Null
& $vpython -m pip install --disable-pip-version-check --upgrade "$source[ui]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed (behind a proxy? set HTTPS_PROXY and retry)." }

$setupArgs = @("-m", "screenpilot", "setup", "--start-menu")
if ($Autostart) { $setupArgs += @("--autostart", "on") }
& $vpython @setupArgs
if ($LASTEXITCODE -ne 0) { throw "Creating the shortcut failed." }

if (-not $NoLaunch) {
    Start-Process -FilePath $vpythonw -ArgumentList @("-m", "screenpilot", "gui") -WorkingDirectory $env:USERPROFILE
    Write-Host "ScreenPilot is running: look for its icon in the tray (^ next to the clock)." -ForegroundColor Green
}
Write-Host "Done. Start menu: ScreenPilot. Uninstall: scripts\uninstall.ps1"
