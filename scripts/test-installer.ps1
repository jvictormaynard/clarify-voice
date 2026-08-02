[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BaselineInstaller,
    [Parameter(Mandatory = $true)]
    [string]$CurrentInstaller
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$baseline = (Resolve-Path $BaselineInstaller).Path
$current = (Resolve-Path $CurrentInstaller).Path
$installDirectory = Join-Path $env:LOCALAPPDATA "Programs\ClarifyVoice"
$installedExe = Join-Path $installDirectory "ClarifyVoice.exe"
$configDirectory = Join-Path $env:APPDATA "ClarifyVoice"
$sentinel = Join-Path $configDirectory "config.json"
$sentinelContent = '{"schema_version":1,"openai_api_key":"test-only-sentinel"}'
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "ClarifyVoice.lnk"
$menuShortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\ClarifyVoice\ClarifyVoice.lnk"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

function Invoke-MsiExec {
    param([string[]]$Arguments, [string]$Operation)

    $process = Start-Process msiexec.exe -ArgumentList $Arguments `
        -Wait -PassThru -NoNewWindow
    if ($process.ExitCode -notin @(0, 1641, 3010)) {
        throw "$Operation failed with Windows Installer exit code $($process.ExitCode)."
    }
}

function Assert-Installed([string]$Operation) {
    foreach ($path in @($installedExe, $desktopShortcut, $menuShortcut, $sentinel)) {
        if (-not (Test-Path $path -PathType Leaf)) {
            throw "$Operation did not preserve the expected path: $path"
        }
    }
    if ((Get-Content -LiteralPath $sentinel -Raw).Trim() -cne $sentinelContent) {
        throw "$Operation changed user configuration or credentials."
    }
}

function Assert-AutostartPreserved([string]$Operation) {
    $entry = Get-ItemPropertyValue -Path $runKey -Name ClarifyVoice `
        -ErrorAction SilentlyContinue
    if (-not $entry -or $entry -notlike "*$installedExe*") {
        throw "$Operation did not preserve the user-controlled autostart entry."
    }
}

New-Item $configDirectory -ItemType Directory -Force | Out-Null
$sentinelContent | Set-Content -LiteralPath $sentinel -Encoding utf8

try {
    Invoke-MsiExec @('/i', $baseline, '/qn', '/norestart') "clean install"
    Assert-Installed "clean install"
    New-Item $runKey -Force | Out-Null
    Set-ItemProperty -Path $runKey -Name ClarifyVoice `
        -Value ('"' + $installedExe + '" --hidden')

    Invoke-MsiExec @('/i', $current, '/qn', '/norestart') "upgrade"
    Assert-Installed "upgrade"
    Assert-AutostartPreserved "upgrade"

    Invoke-MsiExec @('/fa', $current, '/qn', '/norestart') "repair"
    Assert-Installed "repair"
    Assert-AutostartPreserved "repair"

    Invoke-MsiExec @('/i', $baseline, '/qn', '/norestart') "manual rollback"
    Assert-Installed "manual rollback"
    Assert-AutostartPreserved "manual rollback"

    Invoke-MsiExec @('/x', $baseline, '/qn', '/norestart') "uninstall"

    if (Test-Path $installedExe) {
        throw "Uninstall left the installed executable behind."
    }
    if ((Test-Path $desktopShortcut) -or (Test-Path $menuShortcut)) {
        throw "Uninstall left a ClarifyVoice shortcut behind."
    }
    if (-not (Test-Path $sentinel -PathType Leaf)) {
        throw "Uninstall removed user configuration."
    }
    if (Get-ItemProperty -Path $runKey -Name ClarifyVoice -ErrorAction SilentlyContinue) {
        throw "Uninstall left the autostart entry behind."
    }
} finally {
    foreach ($installer in @($current, $baseline)) {
        try {
            Invoke-MsiExec @('/x', $installer, '/qn', '/norestart') "cleanup"
        } catch {
            Write-Warning $_
        }
    }
    Remove-Item $sentinel -Force -ErrorAction SilentlyContinue
}

Write-Host "Installer install/upgrade/repair/rollback/uninstall smoke test passed."
