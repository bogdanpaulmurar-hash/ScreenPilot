<#
.SYNOPSIS
    Removes ScreenPilot for the current user (shortcut, autostart, private environment).
    Settings, logs and captures are kept unless -RemoveData is given.
#>
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\ScreenPilot",
    [switch]$RemoveData
)

$ErrorActionPreference = "Stop"
$venv = Join-Path $InstallDir "venv"
$vpython = Join-Path $venv "Scripts\python.exe"
$vpythonw = Join-Path $venv "Scripts\pythonw.exe"

function Stop-ScreenPilot {
    # Files must not be replaced under a running copy (mixed old/new modules crash it).
    # 1) ask the running app to quit; 2) stop whatever is left, however it was started
    #    (pythonw / python -m screenpilot, screenpilot.exe, the venv launcher's child process).
    $ErrorActionPreference = "Continue"
    $vpy = Join-Path $venv "Scripts\python.exe"
    if (Test-Path $vpy) {
        & $vpy -m screenpilot action app.quit --no-start 2>&1 | Out-Null
        Start-Sleep -Milliseconds 1500
    }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase)) -or
            ($_.CommandLine -and $_.CommandLine -match '(-m\s+screenpilot\b|screenpilot(-gui)?\.exe)' -and $_.Name -match '^(pythonw?|screenpilot(-gui)?)\.exe$')
        } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

Stop-ScreenPilot

if (Test-Path $vpython) {
    & $vpython -m screenpilot setup --remove
}
$desktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) "ScreenPilot.lnk"
if (Test-Path $desktopLink) { Remove-Item -Force $desktopLink; Write-Host "Removed $desktopLink" }
if (Test-Path $venv) {
    Remove-Item -Recurse -Force $venv
    Write-Host "Removed $venv"
}
if ($RemoveData) {
    foreach ($dir in @("$env:APPDATA\ScreenPilot", "$env:LOCALAPPDATA\ScreenPilot")) {
        if (Test-Path $dir) { Remove-Item -Recurse -Force $dir; Write-Host "Removed $dir" }
    }
}
Write-Host "ScreenPilot was uninstalled."
