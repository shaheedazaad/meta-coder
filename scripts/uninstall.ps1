# Uninstall a release installed by install.ps1. User data and Pixi are kept.
# Usage:
#   irm https://github.com/shaheedazaad/meta-coder/releases/latest/download/uninstall.ps1 | iex
# or, from a source checkout: .\scripts\uninstall.ps1
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw "LOCALAPPDATA must be set."
}

$DataRoot = Join-Path $env:LOCALAPPDATA "Meta-Coder"
$AppRoot = Join-Path $DataRoot "app"
$BinDir = Join-Path $DataRoot "bin"
$Launcher = Join-Path $BinDir "meta-coder.cmd"

# A launcher is ours if it runs a manifest under $AppRoot, either behind
# install.ps1's REM marker line or as the `"<pixi.exe>" run ...` line written
# before the marker existed. Anything else is left alone.
function Test-OwnLauncher([string]$Path) {
    $Lines = @(Get-Content -LiteralPath $Path)
    $AppLines = @($Lines | Where-Object { $_.Contains("--manifest-path `"$AppRoot\") })
    if ($AppLines.Count -eq 0) { return $false }
    if ($Lines -ceq "REM meta-coder launcher") { return $true }
    return [bool]($AppLines | Where-Object { $_ -match '^"[^"]+" run ' })
}

if (Test-Path -LiteralPath $Launcher -PathType Leaf) {
    if (Test-OwnLauncher $Launcher) {
        Remove-Item -LiteralPath $Launcher -Force
    } else {
        Write-Host "Keeping launcher not recognized as this release installation: $Launcher"
    }
}
if (Test-Path -LiteralPath $AppRoot) {
    Remove-Item -LiteralPath $AppRoot -Recurse -Force
}

# Remove only the installer's directory, preserving all other PATH entries.
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -ne $UserPath) {
    $NewPath = (($UserPath -split ";" | Where-Object {
        $_.Trim().Trim('"').TrimEnd('\', '/') -ine $BinDir
    }) -join ";")
    if ($NewPath -cne $UserPath) {
        [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        Write-Host "Removed $BinDir from your user PATH. Open a new terminal for it to take effect."
    }
}
$env:Path = (($env:Path -split ";" | Where-Object {
    $_.Trim().Trim('"').TrimEnd('\', '/') -ine $BinDir
}) -join ";")

Write-Host "MetaCoder release installation removed."
Write-Host "Projects, settings, saved API keys, and Pixi have been preserved."
Write-Host "User data location: $DataRoot (or your META_CODER_HOME override)."
