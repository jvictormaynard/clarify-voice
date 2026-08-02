[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$spikeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $spikeRoot "../..")).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $spikeRoot "artifacts"
} elseif (-not [System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory = Join-Path $spikeRoot $OutputDirectory
}

$venv = Join-Path $spikeRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$buildRoot = Join-Path $spikeRoot "build"
$customOutput = Join-Path $OutputDirectory "customtkinter"
$qtOutput = Join-Path $OutputDirectory "pyside6"

New-Item -ItemType Directory -Force -Path $OutputDirectory, $buildRoot | Out-Null
if (-not (Test-Path $venvPython)) {
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated spike virtual environment." }
}

& $venvPython -m pip install --disable-pip-version-check --upgrade pip
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $repoRoot "requirements.txt")
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $spikeRoot "requirements.txt") pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Could not install spike packaging dependencies." }

function Invoke-PyInstaller {
    param(
        [string]$Name,
        [string]$EntryPoint,
        [string]$Destination,
        [string[]]$ExtraArguments = @()
    )

    $work = Join-Path $buildRoot $Name
    $spec = Join-Path $buildRoot "$Name-spec"
    New-Item -ItemType Directory -Force -Path $Destination, $work, $spec | Out-Null
    $arguments = @(
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", $Name, "--distpath", $Destination,
        "--workpath", $work, "--specpath", $spec,
        "--paths", $repoRoot
    ) + $ExtraArguments + @($EntryPoint)
    & $venvPython -m PyInstaller @arguments
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for $Name." }
    $executable = Join-Path $Destination "$Name.exe"
    if (-not (Test-Path $executable)) { throw "Missing packaged artifact: $executable" }
    Write-Host "Built $executable"
}

# Both builds use the same isolated environment, output root, and PyInstaller
# mode. The production scripts and dist/ directory are never called.
Invoke-PyInstaller "ClarifyVoice-customtkinter" (Join-Path $repoRoot "app.py") $customOutput @(
    "--add-data", "$(Join-Path $repoRoot 'extra');extra",
    "--add-data", "$(Join-Path $repoRoot 'assets');assets",
    "--hidden-import", "sounddevice",
    "--hidden-import", "_sounddevice_data",
    "--exclude-module", "numpy",
    "--exclude-module", "keyboard"
)
Invoke-PyInstaller "ClarifyVoice-pyside6" (Join-Path $spikeRoot "app.py") $qtOutput
Write-Host "Comparable spike artifacts are under $OutputDirectory"
