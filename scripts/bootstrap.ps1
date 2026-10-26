<#
.SYNOPSIS
    Everything Install.bat needs: Python (if missing), ffmpeg (optional), then ScreenPilot.

.DESCRIPTION
    No admin rights and no executable of our own (antivirus friendly):
    * Python 3.10+ is looked up; if missing it is installed for the current user only, with
      winget when available, otherwise with the official signed installer from python.org.
    * ffmpeg (needed only for MP4 recordings) is installed with winget when available.
    * scripts\install.ps1 then creates the private environment, installs the dependencies,
      adds Start-menu and desktop shortcuts, turns on "start with Windows" and starts the app.
#>
param(
    [switch]$NoAutostart,
    [switch]$NoFfmpeg,
    [switch]$NoDesktop,
    [switch]$NoLaunch,
    [string]$InstallDir = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$PythonFallback = "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe"

function Step([string]$text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Info([string]$text) { Write-Host "    $text" }

function Test-Python([string]$exe, [string[]]$prefix = @()) {
    # The "python" alias from the Microsoft Store (WindowsApps) is only a stub: it must be skipped.
    try {
        $out = & $exe @prefix -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
    } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $path = ("$out").Trim()
    if ($path -like "*\WindowsApps\*" -or -not (Test-Path $path)) { return $null }
    return $path
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $p = Test-Python "py" @("-3")
        if ($p) { return $p }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $p = Test-Python "python"
        if ($p) { return $p }
    }
    # Freshly installed: not on this window's PATH yet.
    $roots = @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python*", "${env:ProgramFiles(x86)}\Python*")
    $candidates = foreach ($root in $roots) {
        Get-ChildItem -Path $root -Filter python.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue
    }
    foreach ($exe in $candidates | Sort-Object FullName -Descending) {
        $p = Test-Python $exe.FullName
        if ($p) { return $p }
    }
    return $null
}

function Install-Python {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Info "Instalez Python 3.13 cu winget (doar pentru utilizatorul curent)..."
        & winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
        $p = Find-Python
        if ($p) { return $p }
        Info "winget nu a reusit; incerc installerul oficial de pe python.org."
    }
    $setup = Join-Path $env:TEMP "python-setup-screenpilot.exe"
    Info "Descarc $PythonFallback ..."
    Invoke-WebRequest -Uri $PythonFallback -OutFile $setup -UseBasicParsing
    $signature = Get-AuthenticodeSignature $setup
    if ($signature.Status -ne "Valid") { throw "Installerul Python descarcat nu are o semnatura valida ($($signature.Status))." }
    Info "Instalez Python (fara drepturi de administrator)..."
    $arguments = "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0 Include_doc=0 Shortcuts=0"
    $proc = Start-Process -FilePath $setup -ArgumentList $arguments -Wait -PassThru
    Remove-Item $setup -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) { throw "Instalarea Python a esuat (cod $($proc.ExitCode))." }
    $p = Find-Python
    if (-not $p) { throw "Python a fost instalat, dar nu a putut fi gasit. Reporneste Install.bat." }
    return $p
}

function Install-Ffmpeg {
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Info "ffmpeg este deja instalat."; return }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Info "winget lipseste: sar peste ffmpeg. Capturile, editorul si GIF-urile merg; pentru MP4 instaleaza ffmpeg mai tarziu."
        return
    }
    Info "Instalez ffmpeg cu winget (optional, pentru inregistrari MP4)..."
    & winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) { Info "ffmpeg nu s-a instalat (nu e obligatoriu). Restul aplicatiei merge." }
}

Write-Host "ScreenPilot - instalare" -ForegroundColor Green
Write-Host "Fara drepturi de administrator. Totul se instaleaza doar pentru utilizatorul curent."

Step "1/3  Python"
$python = Find-Python
if ($python) { Info "Gasit: $python" } else { Info "Python nu este instalat."; $python = Install-Python; Info "Instalat: $python" }

Step "2/3  ffmpeg (optional)"
if ($NoFfmpeg) { Info "Sarit (-NoFfmpeg)." } else {
    try { Install-Ffmpeg } catch { Info "ffmpeg nu s-a instalat: $($_.Exception.Message) (nu e obligatoriu)." }
}

Step "3/3  ScreenPilot si dependentele lui (Pillow, PySide6)"
$installer = Join-Path $PSScriptRoot "install.ps1"
$options = @{ Python = $python }
if (-not $NoAutostart) { $options.Autostart = $true }
if (-not $NoDesktop) { $options.Desktop = $true }
if ($NoLaunch) { $options.NoLaunch = $true }
if ($InstallDir) { $options.InstallDir = $InstallDir }
& $installer @options
