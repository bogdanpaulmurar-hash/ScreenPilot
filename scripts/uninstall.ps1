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

Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -eq $vpythonw } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if (Test-Path $vpython) {
    & $vpython -m screenpilot setup --remove
}
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
