[CmdletBinding()]
param(
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

function ConvertTo-ProcessArgument {
    param([string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-CheckedProcess {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$FailureMessage
    )

    $argumentLine = ($Arguments | ForEach-Object {
        ConvertTo-ProcessArgument ([string]$_)
    }) -join " "
    $process = Start-Process -FilePath $FilePath -ArgumentList $argumentLine `
        -Wait -PassThru -NoNewWindow
    if ($process.ExitCode -ne 0) {
        throw $FailureMessage
    }
}

function Resolve-SystemPython {
    $candidates = @()
    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($python -and $python.Source -notlike "*\WindowsApps\*") {
        $candidates += $python.Source
    }

    foreach ($root in @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python"),
        (Join-Path $env:ProgramFiles "Python")
    )) {
        if (Test-Path $root) {
            $candidates += Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path $_.FullName "python.exe" } |
                Where-Object { Test-Path $_ }
        }
    }

    $supported = $candidates | Sort-Object -Unique | ForEach-Object {
        $file = Get-Item $_ -ErrorAction SilentlyContinue
        if ($file) {
            try {
                $version = [Version]$file.VersionInfo.ProductVersion
                if ($version -ge [Version]"3.11") {
                    [PSCustomObject]@{ Path = $file.FullName; Version = $version }
                }
            } catch {
                # Ignore launchers that do not expose a parseable Python version.
            }
        }
    } | Sort-Object Version -Descending

    if ($supported) {
        return $supported[0].Path
    }

    throw "Python 3.11 or newer is required. Install it from https://python.org/downloads/windows/."
}

if (-not (Test-Path $venvPython)) {
    $systemPython = Resolve-SystemPython
    Write-Host "Creating isolated environment at $venvDir..."
    Invoke-CheckedProcess $systemPython @("-m", "venv", $venvDir) `
        "Could not create the Python virtual environment."
    if (-not (Test-Path $venvPython)) {
        throw "Could not create the Python virtual environment."
    }
}

Write-Host "Installing ClarifyVoice dependencies..."
Invoke-CheckedProcess $venvPython @(
    "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"
) "Could not update pip in the ClarifyVoice environment."

$requirements = if ($Dev) {
    Join-Path $repoRoot "requirements-dev.txt"
} else {
    Join-Path $repoRoot "requirements.txt"
}
Invoke-CheckedProcess $venvPython @(
    "-m", "pip", "install", "--disable-pip-version-check", "-r", $requirements
) "Could not install ClarifyVoice dependencies."

Write-Host "ClarifyVoice environment is ready."
