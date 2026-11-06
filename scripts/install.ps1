# MetaCoder installer (Windows) — todo.md step 20 / plan.md "A1. Shape,
# install, and local-only security". Mirrors install.sh's logic; see that
# file for the full rationale on each step. Downloads a versioned release
# bundle, installs its locked Pixi environment, and puts a `meta-coder`
# launcher on the current user's PATH (no admin rights required).
#
# Usage:
#   irm https://github.com/shaheedazaad/meta-coder/releases/latest/download/install.ps1 | iex
# Configuration (environment variables):
#   META_CODER_RELEASE_BASE_URL  Where release bundles + latest.txt live.
#                                 Optional override for mirrors/testing.
#   META_CODER_VERSION           Install this exact version instead of latest.

$ErrorActionPreference = "Stop"

$AppName = "MetaCoder"
$DataDirName = "Meta-Coder"

# --- 1. Public GitHub releases (or an explicitly configured mirror) --------
$BaseUrl = $env:META_CODER_RELEASE_BASE_URL
if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    $BaseUrl = "https://github.com/shaheedazaad/meta-coder/releases/latest/download"
}
function Get-ReleaseAsset([string]$Name, [string]$OutputPath) {
    $DownloadBase = $BaseUrl.TrimEnd('/')
    if ([string]::IsNullOrWhiteSpace($env:META_CODER_RELEASE_BASE_URL) -and $Version) {
        $DownloadBase = "https://github.com/shaheedazaad/meta-coder/releases/download/v$Version"
    }
    Invoke-WebRequest -UseBasicParsing "$DownloadBase/$Name" -OutFile $OutputPath
}

# --- 2. Which version? ------------------------------------------------------
$Version = $env:META_CODER_VERSION
if ([string]::IsNullOrWhiteSpace($Version)) {
    $VersionFile = [System.IO.Path]::GetTempFileName()
    try {
        Get-ReleaseAsset "latest.txt" $VersionFile
        $Version = (Get-Content -Raw $VersionFile).Trim()
    } finally { Remove-Item $VersionFile -Force }
}
if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
    Write-Error "Expected a stable version like 0.1.0."
    exit 1
}
Write-Host "Installing $AppName $Version..."

# --- 3. Where does it go? (mirrors meta_coder/paths.py's app_data_dir) ----
$DataRoot = Join-Path $env:LOCALAPPDATA $DataDirName
$AppRoot = Join-Path $DataRoot "app"
$InstallDir = Join-Path $AppRoot $Version
$BinDir = Join-Path $DataRoot "bin"
$Launcher = Join-Path $BinDir "meta-coder.cmd"

# --- 4. Download + extract --------------------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$TmpTarball = Join-Path $env:TEMP "meta-coder-$Version.tar.gz"
Get-ReleaseAsset "meta-coder-$Version.tar.gz" $TmpTarball
# tar.exe ships built in since Windows 10 1803 — no separate archive tool needed.
tar -xzf $TmpTarball -C $InstallDir --strip-components=1
if ($LASTEXITCODE -ne 0) { throw "Release extraction failed." }
Remove-Item $TmpTarball -Force

# --- 5. Bootstrap Pixi if this machine doesn't have it yet -----------------
if (-not (Get-Command pixi -ErrorAction SilentlyContinue)) {
    Write-Host "Pixi not found — installing it (see https://pixi.sh)..."
    Invoke-Expression (Invoke-WebRequest -UseBasicParsing "https://pixi.sh/install.ps1").Content
    $env:Path = "$env:LOCALAPPDATA\pixi\bin;$env:Path"
}
if (-not (Get-Command pixi -ErrorAction SilentlyContinue)) {
    Write-Error "Pixi installation did not put 'pixi' on PATH. Open a new terminal and re-run."
    exit 1
}

# --- 6. Materialize the locked environment ----------------------------------
# --locked (not --frozen) so a bundle whose pixi.lock doesn't actually match
# its own manifest fails loudly here rather than silently resolving fresh.
pixi install --manifest-path (Join-Path $InstallDir "pyproject.toml") --locked
if ($LASTEXITCODE -ne 0) { throw "Locked environment installation failed." }

# --- 7. Launcher shim --------------------------------------------------------
$ManifestPath = Join-Path $InstallDir "pyproject.toml"
$PixiPath = (Get-Command pixi).Source
# The REM marker line lets uninstall.ps1 recognise launchers it may remove.
Set-Content -Path $Launcher -Value "@echo off`r`nREM meta-coder launcher`r`n`"$PixiPath`" run --locked --manifest-path `"$ManifestPath`" start %*"

# --- 8. Drop stale versions — "re-running the installer updates in place" --
Get-ChildItem -Path $AppRoot -Directory | Where-Object { $_.Name -ne $Version } | Remove-Item -Recurse -Force

# --- 9. Make sure $BinDir is on this user's PATH ----------------------------
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not ($UserPath -split ";" -contains $BinDir)) {
    [Environment]::SetEnvironmentVariable("Path", "$UserPath;$BinDir", "User")
    Write-Host "Added $BinDir to your user PATH — open a new terminal for it to take effect."
}

Write-Host ""
Write-Host "$AppName $Version installed."
Write-Host "Run it with: meta-coder"
