[CmdletBinding()]
param(
    [ValidatePattern('^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')]
    [string]$Version,
    [string]$SourceExe,
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Version) {
    $versionSource = Get-Content (Join-Path $repoRoot "version.py") -Raw
    $match = [regex]::Match(
        $versionSource, '(?m)^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"$')
    if (-not $match.Success) {
        throw "Could not read the packaged version from version.py."
    }
    $Version = $match.Groups[1].Value
}
if (-not $SourceExe) {
    $SourceExe = Join-Path $repoRoot "dist\ClarifyVoice.exe"
}
if (-not $OutputPath) {
    $OutputPath = Join-Path $repoRoot "dist\ClarifyVoice-windows-x64.msi"
}
$source = (Resolve-Path $SourceExe).Path
$outputDirectory = Split-Path -Parent $OutputPath
$installerSource = Join-Path $repoRoot "installer\ClarifyVoice.wxs"
$license = Join-Path $repoRoot "LICENSE"
$notices = Join-Path $repoRoot "THIRD_PARTY_NOTICES.md"
$icon = Join-Path $repoRoot "assets\branding\clarify.ico"
$toolDirectory = Join-Path $repoRoot "build\tools\wix-6.0.2"
$wix = Join-Path $toolDirectory "wix.exe"

foreach ($requiredPath in @($source, $installerSource, $license, $notices, $icon)) {
    if (-not (Test-Path $requiredPath -PathType Leaf)) {
        throw "Required installer input is missing: $requiredPath"
    }
}

if (-not (Test-Path $wix -PathType Leaf)) {
    New-Item $toolDirectory -ItemType Directory -Force | Out-Null
    & dotnet tool install wix --tool-path $toolDirectory --version 6.0.2
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $wix -PathType Leaf)) {
        throw "Could not install the pinned WiX Toolset 6.0.2 compiler."
    }
}

New-Item $outputDirectory -ItemType Directory -Force | Out-Null
Remove-Item $OutputPath -Force -ErrorAction SilentlyContinue

& $wix build $installerSource `
    -arch x64 `
    -d "Version=$Version" `
    -d "SourceExe=$source" `
    -d "LicenseFile=$license" `
    -d "NoticesFile=$notices" `
    -d "IconFile=$icon" `
    -o $OutputPath
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $OutputPath -PathType Leaf)) {
    throw "WiX did not produce the ClarifyVoice MSI."
}

Write-Host "Installer complete: $OutputPath"
