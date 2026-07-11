[CmdletBinding()]
param(
    [string]$InstallPath = $env:CLARIFYVOICE_INSTALL_PATH
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$repoSource = Join-Path $repoRoot "app.py"
$repoExtra = Join-Path $repoRoot "extra"
$repoAssets = Join-Path $repoRoot "assets"
$repoEnvFile = Join-Path $repoRoot ".env"
$buildRoot = Join-Path $env:TEMP "clarify-voice-build"
$sourceDir = Join-Path $buildRoot "source"
$distDir = Join-Path $buildRoot "dist"
$workDir = Join-Path $buildRoot "work"
$specDir = Join-Path $buildRoot "spec"
$builtExe = Join-Path $distDir "ClarifyVoice.exe"
$pipOutLog = Join-Path $buildRoot "pip.stdout.log"
$pipErrLog = Join-Path $buildRoot "pip.stderr.log"
$buildOutLog = Join-Path $buildRoot "build.stdout.log"
$buildErrLog = Join-Path $buildRoot "build.stderr.log"

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
New-Item $buildRoot -ItemType Directory -Force | Out-Null
$pip = Start-Process -FilePath $python -ArgumentList @(
    "-m", "pip", "install", "--quiet",
    "keyboard", "requests", "sounddevice", "customtkinter", "Pillow", "pyinstaller"
) -Wait -PassThru -WindowStyle Hidden -RedirectStandardOutput $pipOutLog -RedirectStandardError $pipErrLog
if ($pip.ExitCode -ne 0) {
    Get-Content $pipOutLog, $pipErrLog -ErrorAction SilentlyContinue
    throw "Could not install the Windows build dependencies."
}

# Keep PyInstaller's work directory so subsequent deployments can reuse its
# dependency-analysis cache. Only refresh source inputs and final output.
Remove-Item $sourceDir, $distDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item $sourceDir, $distDir, $workDir, $specDir -ItemType Directory -Force | Out-Null

# PyInstaller cannot reliably analyze source files over a WSL UNC path, so
# stage the required inputs on the Windows filesystem before building.
$source = Join-Path $sourceDir "app.py"
$extra = Join-Path $sourceDir "extra"
$assets = Join-Path $sourceDir "assets"
$envFile = Join-Path $sourceDir ".env"
Copy-Item $repoSource $source -Force
Copy-Item $repoExtra $extra -Recurse -Force
Copy-Item $repoAssets $assets -Recurse -Force
# Keep the linked SoX runtime intact, but omit files unused by the app.
Remove-Item (Join-Path $extra "sox.zip") -Force -ErrorAction SilentlyContinue
$soxDir = Join-Path $extra "sox-14.4.2"
Remove-Item (Join-Path $soxDir "*.pdf"),
    (Join-Path $soxDir "*.txt"),
    (Join-Path $soxDir "batch-example.bat"),
    (Join-Path $soxDir "wget.exe"),
    (Join-Path $soxDir "wget.ini") -Force -ErrorAction SilentlyContinue
if (Test-Path $repoEnvFile) {
    Copy-Item $repoEnvFile $envFile -Force
}

$pyinstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--onefile", "--windowed",
    "--name", "ClarifyVoice",
    "--distpath", $distDir,
    "--workpath", $workDir,
    "--specpath", $specDir,
    "--add-data", "${extra};extra",
    "--add-data", "${assets};assets",
    "--hidden-import", "sounddevice",
    "--hidden-import", "_sounddevice_data",
    "--exclude-module", "numpy"
)

if (Test-Path $envFile) {
    $pyinstallerArgs += @("--add-data", "${envFile};.")
}
$pyinstallerArgs += $source

$builder = Start-Process -FilePath $python -ArgumentList $pyinstallerArgs -Wait -PassThru `
    -WindowStyle Hidden -RedirectStandardOutput $buildOutLog -RedirectStandardError $buildErrLog
if ($builder.ExitCode -ne 0 -or -not (Test-Path $builtExe)) {
    Get-Content $buildOutLog, $buildErrLog -ErrorAction SilentlyContinue
    throw "ClarifyVoice build failed. The installed version was not changed."
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
