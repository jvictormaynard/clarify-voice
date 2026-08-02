[CmdletBinding()]
param(
    [string]$InstallPath = $env:CLARIFYVOICE_INSTALL_PATH
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$repoExtra = Join-Path $repoRoot "extra"
$repoAssets = Join-Path $repoRoot "assets"
$repoDistribution = Join-Path $repoRoot "distribution"
$buildRoot = Join-Path $env:TEMP "clarify-voice-build"
$venvDir = Join-Path $buildRoot "venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$requirementsDir = Join-Path $buildRoot "requirements"
$requirementsFile = Join-Path $requirementsDir "requirements-dev.txt"
$lockFile = Join-Path $requirementsDir "requirements-lock-windows.txt"
$sourceDir = Join-Path $buildRoot "source"
$distDir = Join-Path $buildRoot "dist"
$workDir = Join-Path $buildRoot "work"
$specDir = Join-Path $buildRoot "spec"
$builtExe = Join-Path $distDir "ClarifyVoice.exe"
$pipOutLog = Join-Path $buildRoot "pip.stdout.log"
$pipErrLog = Join-Path $buildRoot "pip.stderr.log"
$versionsOutLog = Join-Path $buildRoot "versions.stdout.log"
$versionsErrLog = Join-Path $buildRoot "versions.stderr.log"
$buildOutLog = Join-Path $buildRoot "build.stdout.log"
$buildErrLog = Join-Path $buildRoot "build.stderr.log"

function ConvertTo-ProcessArgument {
    param([string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-LoggedProcess {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$StandardOutput,
        [string]$StandardError,
        [string]$FailureMessage
    )

    $argumentLine = ($Arguments | ForEach-Object {
        ConvertTo-ProcessArgument ([string]$_)
    }) -join " "
    $process = Start-Process -FilePath $FilePath -ArgumentList $argumentLine `
        -Wait -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $StandardOutput `
        -RedirectStandardError $StandardError
    if ($process.ExitCode -ne 0) {
        Get-Content $StandardOutput, $StandardError -ErrorAction SilentlyContinue
        throw $FailureMessage
    }
}

function Resolve-InstallPath {
    param([string]$ExplicitPath)

    if ($ExplicitPath) {
        return [Environment]::ExpandEnvironmentVariables($ExplicitPath)
    }

    $shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Clarify.lnk"
    if (Test-Path $shortcut) {
        $shell = New-Object -ComObject WScript.Shell
        $target = $shell.CreateShortcut($shortcut).TargetPath
        if ($target) {
            return $target
        }
    }

    return Join-Path $env:LOCALAPPDATA "ClarifyVoice\ClarifyVoice.exe"
}

function Resolve-Python {
    $commands = @("py.exe", "python.exe")
    foreach ($command in $commands) {
        $resolved = Get-Command $command -ErrorAction SilentlyContinue
        if ($resolved -and $resolved.Source -notlike "*\WindowsApps\*") {
            return $resolved.Source
        }
    }

    $installRoot = Join-Path $env:LOCALAPPDATA "Programs\Python"
    $installed = Get-ChildItem $installRoot -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notlike "*\WindowsApps\*" } |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if ($installed) {
        return $installed.FullName
    }

    throw "A Windows Python installation is required to build ClarifyVoice."
}

$targetExe = Resolve-InstallPath $InstallPath
$python = Resolve-Python
$targetDir = Split-Path -Parent $targetExe
$backupExe = "$targetExe.backup"

Write-Host "Building ClarifyVoice..."
New-Item $buildRoot, $requirementsDir -ItemType Directory -Force | Out-Null
Copy-Item (Join-Path $repoRoot "requirements.txt") $requirementsDir -Force
Copy-Item (Join-Path $repoRoot "requirements-dev.txt") $requirementsDir -Force
Copy-Item (Join-Path $repoRoot "requirements-lock-windows.txt") $requirementsDir -Force

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating isolated build environment at $venvDir..."
    Invoke-LoggedProcess $python @("-m", "venv", $venvDir) `
        $pipOutLog $pipErrLog `
        "Could not create the isolated Windows build environment."
    if (-not (Test-Path $venvPython)) {
        throw "Could not create the isolated Windows build environment."
    }
}

Invoke-LoggedProcess $venvPython @(
    "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
    "-r", $requirementsFile, "-c", $lockFile
) $pipOutLog $pipErrLog "Could not install the Windows build dependencies."

$versionScript = (
    "from importlib.metadata import version; " +
    "names=('requests','sounddevice','customtkinter','Pillow','pyinstaller'); " +
    "print(', '.join(name + ' ' + version(name) for name in names))"
)
$versionArguments = @("-c", $versionScript)
Invoke-LoggedProcess $venvPython $versionArguments `
    $versionsOutLog $versionsErrLog `
    "Could not inspect the isolated Windows build dependencies."
$dependencyVersions = (Get-Content $versionsOutLog -Raw).Trim()
Write-Host "Build dependencies: $dependencyVersions"

# Keep PyInstaller's work directory so subsequent deployments can reuse its
# dependency-analysis cache. Only refresh source inputs and final output.
Remove-Item $sourceDir, $distDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item $sourceDir, $distDir, $workDir, $specDir -ItemType Directory -Force | Out-Null

# PyInstaller cannot reliably analyze source files over a WSL UNC path, so
# stage the required inputs on the Windows filesystem before building.
$source = Join-Path $sourceDir "app.py"
$extra = Join-Path $sourceDir "extra"
$assets = Join-Path $sourceDir "assets"
$distribution = Join-Path $sourceDir "distribution"
Copy-Item (Join-Path $repoRoot "*.py") $sourceDir -Force
Copy-Item $repoExtra $extra -Recurse -Force
Copy-Item $repoAssets $assets -Recurse -Force
Copy-Item $repoDistribution $distribution -Recurse -Force
# Keep the linked SoX runtime intact, but omit files unused by the app.
Remove-Item (Join-Path $extra "sox.zip") -Force -ErrorAction SilentlyContinue
$soxDir = Join-Path $extra "sox-14.4.2"
Remove-Item (Join-Path $soxDir "*.pdf"),
    (Join-Path $soxDir "ChangeLog.txt"),
    (Join-Path $soxDir "README.txt"),
    (Join-Path $soxDir "README.win32.txt"),
    (Join-Path $soxDir "batch-example.bat"),
    (Join-Path $soxDir "wget.exe"),
    (Join-Path $soxDir "wget.ini") -Force -ErrorAction SilentlyContinue

$pyinstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--onefile", "--windowed",
    "--name", "ClarifyVoice",
    "--icon", (Join-Path $assets "branding\clarify.ico"),
    "--distpath", $distDir,
    "--workpath", $workDir,
    "--specpath", $specDir,
    "--add-data", "${extra};extra",
    "--add-data", "${assets};assets",
    "--add-data", "${distribution};distribution",
    "--hidden-import", "sounddevice",
    "--hidden-import", "_sounddevice_data",
    "--exclude-module", "numpy",
    # Windows hotkeys and Ctrl+C/V are implemented with Win32 APIs. Keep the
    # cross-platform source fallback out of the packaged Windows executable.
    "--exclude-module", "keyboard"
)

# Never copy or bundle the repository .env. Public and local executables read
# provider credentials from the user's ClarifyVoice config directory instead.
$pyinstallerArgs += $source

Invoke-LoggedProcess $venvPython $pyinstallerArgs $buildOutLog $buildErrLog `
    "ClarifyVoice build failed. The installed version was not changed."
if (-not (Test-Path $builtExe)) {
    throw "ClarifyVoice build completed without producing an executable."
}

Write-Host "Updating $targetExe..."
Get-Process ClarifyVoice -ErrorAction SilentlyContinue | Stop-Process -Force
New-Item $targetDir -ItemType Directory -Force | Out-Null

try {
    Remove-Item $backupExe -Force -ErrorAction SilentlyContinue
    if (Test-Path $targetExe) {
        Move-Item $targetExe $backupExe -Force
    }
    Copy-Item $builtExe $targetExe -Force
} catch {
    if (-not (Test-Path $targetExe) -and (Test-Path $backupExe)) {
        Move-Item $backupExe $targetExe -Force
    }
    throw
}

Start-Process $targetExe -WorkingDirectory $targetDir
Write-Host "ClarifyVoice was updated and restarted successfully."
