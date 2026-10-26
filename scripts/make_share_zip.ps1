<#
.SYNOPSIS
    Builds dist\ScreenPilot-<version>.zip to send to someone: unzip, double-click Install.bat.

.DESCRIPTION
    Contains only what the installer needs (Install.bat, Uninstall.bat, scripts, src,
    pyproject.toml, README.md) - no .git, tests, caches or build folders.
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = (Select-String -Path (Join-Path $root "pyproject.toml") -Pattern '^version\s*=\s*"(.+)"').Matches[0].Groups[1].Value
$dist = Join-Path $root "dist"
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("screenpilot-share-" + [guid]::NewGuid().ToString("N"))
$folder = Join-Path $stage "ScreenPilot"
New-Item -ItemType Directory -Path $folder -Force | Out-Null
try {
    foreach ($item in @("Install.bat", "Uninstall.bat", "pyproject.toml", "README.md")) {
        Copy-Item (Join-Path $root $item) $folder
    }
    New-Item -ItemType Directory -Path (Join-Path $folder "scripts") | Out-Null
    foreach ($script in @("bootstrap.ps1", "install.ps1", "uninstall.ps1")) {
        Copy-Item (Join-Path $root "scripts\$script") (Join-Path $folder "scripts")
    }
    Copy-Item (Join-Path $root "src") $folder -Recurse
    Get-ChildItem $folder -Recurse -Directory -Include "__pycache__", "*.egg-info" | Remove-Item -Recurse -Force
    New-Item -ItemType Directory -Path $dist -Force | Out-Null
    $zip = Join-Path $dist "ScreenPilot-$version.zip"
    if (Test-Path $zip) { Remove-Item $zip }
    Compress-Archive -Path $folder -DestinationPath $zip
    Write-Host "Created $zip ($([math]::Round((Get-Item $zip).Length / 1KB)) KB)"
    Write-Host "Send it; the receiver unzips it and double-clicks Install.bat."
} finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
}
