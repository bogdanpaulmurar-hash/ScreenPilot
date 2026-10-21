<#
.SYNOPSIS
    Installs ScreenPilot for the current user - no admin rights, no .exe.

.DESCRIPTION
    * Checks that this folder is up to date with GitHub (git fetch) and warns
      about local changes that keep "git pull" from updating it.
    * Creates a private virtual environment in %LOCALAPPDATA%\ScreenPilot\venv,
      so ScreenPilot never shares packages with your other Python apps.
    * Installs ScreenPilot from a temporary copy of this folder (works from
      Google Drive / OneDrive folders and paths with spaces).
    * Optionally installs FFmpeg for MP4 recordings (-Ffmpeg): winget, and when
      winget is broken or blocked, the official essentials build from gyan.dev
      into %LOCALAPPDATA%\ScreenPilot\ffmpeg (checked with its SHA-256).
    * Creates Start-menu (and with -DesktopShortcut, Desktop) shortcuts.
    * Optionally starts ScreenPilot with Windows (-Autostart).
    * Starts it with pythonw.exe: no console window, only the tray icon.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Autostart -Ffmpeg -DesktopShortcut
#>
param(
    [switch]$Autostart,
    [switch]$Ffmpeg,
    [switch]$DesktopShortcut,
    [string]$Python = "",
    [string]$InstallDir = "$env:LOCALAPPDATA\ScreenPilot",
    [switch]$NoLaunch,
    [switch]$SkipGitCheck
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

function Get-SourceVersion {
    $toml = Join-Path $source "pyproject.toml"
    $line = Select-String -Path $toml -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($line) { return $line.Matches[0].Groups[1].Value }
    return "?"
}

function Test-GitState {
    # Warn when this folder is not what GitHub has: the most common reason for "I pulled but nothing changed".
    $ErrorActionPreference = "Continue"  # git writes to stderr; Windows PowerShell would turn that into errors
    if ($SkipGitCheck -or -not (Test-Path (Join-Path $source ".git"))) { return }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return }
    $commit = (& git -C $source log -1 --format="%h %cd" --date=format:"%Y-%m-%d %H:%M" 2>$null)
    $branch = (& git -C $source rev-parse --abbrev-ref HEAD 2>$null)
    Write-Host "  git    : $branch @ $commit"
    & git -C $source fetch --quiet 2>$null
    $behind = (& git -C $source rev-list --count "HEAD..@{u}" 2>$null)
    $changes = @(& git -C $source status --porcelain 2>$null)
    if ($changes.Count -gt 0) {
        Write-Host ""
        Write-Host "  ATENTIE / WARNING: this folder has local changes that 'git pull' will not overwrite:" -ForegroundColor Yellow
        $changes | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
        Write-Host "  If 'git pull' said 'Aborting', run:  git stash --include-untracked   then   git pull" -ForegroundColor Yellow
    }
    if ($behind -and [int]$behind -gt 0) {
        Write-Host ""
        Write-Host "  ATENTIE / WARNING: this folder is $behind commit(s) behind GitHub: you would install an OLD version." -ForegroundColor Yellow
        Write-Host "  Run 'git pull' in $source first (or press Ctrl+C now)." -ForegroundColor Yellow
        Start-Sleep -Seconds 5
    }
}

function Find-Ffmpeg {
    $local = Join-Path $InstallDir "ffmpeg\bin\ffmpeg.exe"
    if (Test-Path $local) { return $local }
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $links = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\ffmpeg.exe"
    if (Test-Path $links) { return $links }
    $pkg = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages") -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pkg) { return $pkg.FullName }
    return $null
}

function Install-FfmpegFromGyan {
    $ErrorActionPreference = "Stop"
    # Official "essentials" build, per user, no admin. Verified with the published SHA-256.
    $url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    $tmp = Join-Path $env:TEMP ("screenpilot-ffmpeg-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp "ffmpeg.zip"
        Write-Host "    Downloading $url ..."
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        $expected = ((Invoke-WebRequest -Uri "$url.sha256" -UseBasicParsing).Content.ToString().Trim() -split "\s+")[0]
        $actual = (Get-FileHash -Path $zip -Algorithm SHA256).Hash
        if ($expected -and ($actual -ne $expected.ToUpperInvariant())) { throw "SHA-256 mismatch: the download is not the published file." }
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $exe = Get-ChildItem $tmp -Filter ffmpeg.exe -Recurse | Select-Object -First 1
        if (-not $exe) { throw "ffmpeg.exe was not found in the archive." }
        $bin = Join-Path $InstallDir "ffmpeg\bin"
        New-Item -ItemType Directory -Path $bin -Force | Out-Null
        Copy-Item -Path (Join-Path $exe.DirectoryName "*") -Destination $bin -Force
        return (Join-Path $bin "ffmpeg.exe")
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

function Install-Ffmpeg {
    $ErrorActionPreference = "Continue"  # winget writes progress / errors to stderr
    $found = Find-Ffmpeg
    if ($found) { Write-Host "    FFmpeg already available: $found" -ForegroundColor Green; return }
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "    Installing FFmpeg with winget..."
        & winget install --id Gyan.FFmpeg -e --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | Out-Host
        if (-not (Find-Ffmpeg)) {
            # 0x8a15000f "data required by the source is missing": a broken winget source. Refresh it once.
            & winget source update 2>&1 | Out-Null
            & winget install --id Gyan.FFmpeg -e --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | Out-Null
        }
        $found = Find-Ffmpeg
        if ($found) { Write-Host "    FFmpeg installed: $found" -ForegroundColor Green; return }
        Write-Host "    winget could not install FFmpeg; trying the direct download instead." -ForegroundColor Yellow
    }
    try {
        $found = Install-FfmpegFromGyan
        Write-Host "    FFmpeg installed: $found" -ForegroundColor Green
    } catch {
        Write-Host "    FFmpeg was not installed ($($_.Exception.Message))." -ForegroundColor Yellow
        Write-Host "    MP4 needs it; GIF recording works without it. Manual option: put ffmpeg.exe in $InstallDir\ffmpeg\bin" -ForegroundColor Yellow
    }
}

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

$version = Get-SourceVersion
Write-Host "ScreenPilot installer - version $version" -ForegroundColor Cyan
Write-Host "  source : $source"
Write-Host "  target : $venv"
Test-GitState

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

if ($Ffmpeg) {
    Write-Host "FFmpeg (optional, for MP4 with sound):"
    Install-Ffmpeg
}

# Stop a running copy so its files can be replaced.
Stop-ScreenPilot

# Build from a local temporary copy: pip cannot always write next to files that
# Google Drive / OneDrive keep "online only", and some paths contain spaces.
$build = Join-Path $env:TEMP ("screenpilot-src-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $build | Out-Null
try {
    foreach ($item in @("pyproject.toml", "README.md", "src")) {
        $from = Join-Path $source $item
        if (Test-Path $from) { Copy-Item -Path $from -Destination $build -Recurse -Force }
    }
    Get-ChildItem $build -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Installing ScreenPilot $version and its dependencies (Pillow, PySide6-Essentials)..."
    & $vpython -m pip install --disable-pip-version-check --upgrade pip | Out-Null
    & $vpython -m pip install --disable-pip-version-check --upgrade --force-reinstall --no-deps "$build"
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (behind a proxy? set HTTPS_PROXY and retry)." }
    & $vpython -m pip install --disable-pip-version-check --upgrade "$build[ui]"
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (behind a proxy? set HTTPS_PROXY and retry)." }
} finally {
    Remove-Item -Recurse -Force $build -ErrorAction SilentlyContinue
}

$setupArgs = @("-m", "screenpilot", "setup", "--start-menu")
if ($Autostart) { $setupArgs += @("--autostart", "on") }
& $vpython @setupArgs
if ($LASTEXITCODE -ne 0) { throw "Creating the shortcut failed." }

if ($DesktopShortcut) {
    $startLink = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\ScreenPilot.lnk"
    $desktop = [Environment]::GetFolderPath("Desktop")
    if ((Test-Path $startLink) -and $desktop) {
        Copy-Item -Path $startLink -Destination (Join-Path $desktop "ScreenPilot.lnk") -Force
        Write-Host "Desktop shortcut: $(Join-Path $desktop 'ScreenPilot.lnk')"
    }
}

$installed = (& $vpython -m screenpilot version)
Write-Host "Installed: $installed" -ForegroundColor Green
if ($installed -notmatch [regex]::Escape($version)) {
    Write-Host "  WARNING: expected $version - the installation did not update." -ForegroundColor Yellow
}

if (-not $NoLaunch) {
    Start-Process -FilePath $vpythonw -ArgumentList @("-m", "screenpilot", "gui") -WorkingDirectory $env:USERPROFILE
    Write-Host "ScreenPilot is running: look for its icon in the tray (^ next to the clock)." -ForegroundColor Green
}
Write-Host "Done. Start menu: ScreenPilot. Uninstall: Uninstall.bat (or scripts\uninstall.ps1)"
