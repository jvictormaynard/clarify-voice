[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [ValidatePattern('^[a-z0-9][a-z0-9.-]{0,63}$')]
    [string]$PayloadIdentity
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repoRoot "dist"
} elseif (-not [System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot $OutputDirectory
}

& (Join-Path $PSScriptRoot "setup.ps1") -Dev
if (-not (Test-Path (Join-Path $repoRoot ".venv\Scripts\python.exe"))) {
    throw "Could not prepare the build environment."
}

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$entryPoint = Join-Path $repoRoot "app.py"
$versionSource = Join-Path $repoRoot "version.py"
$repoExtra = Join-Path $repoRoot "extra"
$assets = Join-Path $repoRoot "assets"
$distribution = Join-Path $repoRoot "distribution"
$icon = Join-Path $assets "branding\clarify.ico"
$workDir = Join-Path $repoRoot "build\pyinstaller"
$specDir = Join-Path $repoRoot "build\spec"
$packageInput = Join-Path $repoRoot "build\package-input"
$payloadIdentityFile = Join-Path $packageInput "clarifyvoice-build-identity.txt"
$soxManifestPath = Join-Path $PSScriptRoot "sox-runtime-manifest.json"
$extra = Join-Path $packageInput "extra"
$packageSox = Join-Path $extra "sox-14.4.2"

function ConvertTo-ProcessArgument {
    param([string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

foreach ($requiredPath in @(
    $entryPoint, $versionSource, $repoExtra, $assets, $distribution, $icon,
    $soxManifestPath
)) {
    if (-not (Test-Path $requiredPath)) {
        throw "Required build input is missing: $requiredPath"
    }
}

New-Item $OutputDirectory, $workDir, $specDir -ItemType Directory -Force | Out-Null
Remove-Item $packageInput -Recurse -Force -ErrorAction SilentlyContinue
New-Item $packageSox -ItemType Directory -Force | Out-Null

if ($PayloadIdentity) {
    $PayloadIdentity | Set-Content -LiteralPath $payloadIdentityFile -Encoding ascii
}

$repoSox = Join-Path $repoExtra "sox-14.4.2"
$soxManifest = Get-Content $soxManifestPath -Raw | ConvertFrom-Json
Copy-Item (Join-Path $repoSox $soxManifest.runtime_glob) $packageSox -Force
foreach ($runtimeFile in @($soxManifest.runtime_files + "LICENSE.GPL.txt", "README.txt", "README.win32.txt")) {
    Copy-Item (Join-Path $repoSox $runtimeFile) $packageSox -Force
}

$pyInstallerArgs = @(
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "ClarifyVoice",
    "--icon", $icon,
    "--distpath", $OutputDirectory,
    "--workpath", $workDir,
    "--specpath", $specDir,
    "--paths", $repoRoot,
    "--add-data", "${extra};extra",
    "--add-data", "${assets};assets",
    "--add-data", "${distribution};distribution",
    "--hidden-import", "version",
    "--hidden-import", "sounddevice",
    "--hidden-import", "_sounddevice_data",
    "--exclude-module", "numpy",
    # Windows uses native hotkeys and clipboard events. The cross-platform
    # fallback must not become a global hook in the packaged executable.
    "--exclude-module", "keyboard"
)
if ($PayloadIdentity) {
    # This test-only build marker is bundled inside the one-file archive, so CI
    # gets a valid but byte-distinct historical payload without mutating source.
    $pyInstallerArgs += @("--add-data", "${payloadIdentityFile};.")
}
$pyInstallerArgs += $entryPoint

Write-Host "Building portable ClarifyVoice executable..."
$arguments = @("-m", "PyInstaller") + $pyInstallerArgs
$argumentLine = ($arguments | ForEach-Object {
    ConvertTo-ProcessArgument ([string]$_)
}) -join " "
$builder = Start-Process -FilePath $python -ArgumentList $argumentLine `
    -Wait -PassThru -NoNewWindow
if ($builder.ExitCode -ne 0) {
    throw "ClarifyVoice build failed."
}

$executable = Join-Path $OutputDirectory "ClarifyVoice.exe"
if (-not (Test-Path $executable)) {
    throw "PyInstaller completed without producing $executable."
}

Write-Host "Build complete: $executable"
