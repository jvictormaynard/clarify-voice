[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$OutputDirectory = "",
    [string]$RuntimeRequirements = "",
    [string]$SpikeRequirements = "",
    [string]$EnvironmentManifest = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$spikeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $spikeRoot "../..")).Path

function Resolve-RepoPath {
    param([string]$Path)
    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) {
        $Path
    } else {
        Join-Path $repoRoot $Path
    }
    return (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
}

$runtimeRequirementsPath = if ($RuntimeRequirements) {
    Resolve-RepoPath $RuntimeRequirements
} else {
    Resolve-RepoPath "requirements-lock-runtime-windows.txt"
}
$spikeRequirementsPath = if ($SpikeRequirements) {
    Resolve-RepoPath $SpikeRequirements
} else {
    Resolve-RepoPath "spikes/pyside6/requirements.txt"
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $spikeRoot "artifacts"
} elseif (-not [System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory = Join-Path $spikeRoot $OutputDirectory
}
$outputRoot = (Resolve-Path (New-Item -ItemType Directory -Force -Path $OutputDirectory)).Path

$venv = Join-Path $spikeRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$buildRoot = Join-Path $spikeRoot "build"
$customOutput = Join-Path $outputRoot "customtkinter"
$qtOutput = Join-Path $outputRoot "pyside6"

New-Item -ItemType Directory -Force -Path $outputRoot, $buildRoot | Out-Null
if (-not (Test-Path $venvPython)) {
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated spike virtual environment." }
}

& $venvPython -m pip install --disable-pip-version-check -r $runtimeRequirementsPath
if ($LASTEXITCODE -ne 0) { throw "Could not install the locked runtime requirements." }
& $venvPython -m pip install --disable-pip-version-check -r $spikeRequirementsPath pyinstaller
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

if (-not $EnvironmentManifest) {
    $EnvironmentManifest = Join-Path $outputRoot "build-environment.txt"
} elseif (-not [System.IO.Path]::IsPathRooted($EnvironmentManifest)) {
    $EnvironmentManifest = Join-Path $outputRoot $EnvironmentManifest
}
$manifestParent = Split-Path -Parent $EnvironmentManifest
if ($manifestParent) { New-Item -ItemType Directory -Force -Path $manifestParent | Out-Null }
& $venvPython -m pip freeze | Out-File -Encoding utf8 -FilePath $EnvironmentManifest
if ($LASTEXITCODE -ne 0) { throw "Could not write the package environment manifest." }

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Get-ArtifactRecord {
    param([string]$Target, [string]$Path)
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    return [ordered]@{
        Target = $Target
        Path = $item.FullName
        SizeBytes = [int64]$item.Length
        SizeMB = [math]::Round($item.Length / 1MB, 2)
        SHA256 = Get-Sha256 $item.FullName
    }
}

$artifactManifest = [ordered]@{
    SchemaVersion = 1
    GeneratedAtUtc = [DateTime]::UtcNow.ToString("o")
    RepositoryCommit = (& git -C $repoRoot rev-parse HEAD).Trim()
    Python = (& $venvPython --version 2>&1).Trim()
    PyInstaller = (& $venvPython -m PyInstaller --version 2>&1).Trim()
    RuntimeRequirements = [ordered]@{
        Path = $runtimeRequirementsPath
        SHA256 = Get-Sha256 $runtimeRequirementsPath
    }
    SpikeRequirements = [ordered]@{
        Path = $spikeRequirementsPath
        SHA256 = Get-Sha256 $spikeRequirementsPath
    }
    EnvironmentManifest = [ordered]@{
        Path = (Resolve-Path $EnvironmentManifest).Path
        SHA256 = Get-Sha256 $EnvironmentManifest
    }
    Artifacts = @(
        Get-ArtifactRecord "CustomTkinter" (Join-Path $customOutput "ClarifyVoice-customtkinter.exe")
        Get-ArtifactRecord "PySide6" (Join-Path $qtOutput "ClarifyVoice-pyside6.exe")
    )
}
$artifactManifestPath = Join-Path $outputRoot "artifacts-manifest.json"
$artifactManifest | ConvertTo-Json -Depth 8 | Out-File -Encoding utf8 -FilePath $artifactManifestPath
Write-Host "Comparable spike artifacts are under $outputRoot"
Write-Host "Build provenance written to $artifactManifestPath"
