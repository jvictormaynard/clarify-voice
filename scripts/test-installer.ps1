[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BaselineInstaller,
    [Parameter(Mandatory = $true)]
    [string]$CurrentInstaller,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$BaselinePayloadSha256,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$CurrentPayloadSha256
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot

function Assert-DisposableHostedRunner {
    $requiredEnvironment = @{
        CI = "true"
        GITHUB_ACTIONS = "true"
        RUNNER_ENVIRONMENT = "github-hosted"
        RUNNER_OS = "Windows"
    }
    foreach ($name in $requiredEnvironment.Keys) {
        $actual = [Environment]::GetEnvironmentVariable($name)
        if ($actual -cne $requiredEnvironment[$name]) {
            throw "Refusing destructive installer smoke test: $name must equal '$($requiredEnvironment[$name])'."
        }
    }

    foreach ($name in @("GITHUB_WORKSPACE", "RUNNER_TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA")) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
            throw "Refusing destructive installer smoke test: $name is required."
        }
    }

    $workspace = [IO.Path]::GetFullPath($env:GITHUB_WORKSPACE).TrimEnd('\')
    $actualRepoRoot = [IO.Path]::GetFullPath($repoRoot).TrimEnd('\')
    if (-not $workspace.Equals($actualRepoRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing destructive installer smoke test: repository root must equal GITHUB_WORKSPACE."
    }
    if (-not (Test-Path $env:RUNNER_TEMP -PathType Container)) {
        throw "Refusing destructive installer smoke test: RUNNER_TEMP must be an existing directory."
    }

    $expectedAppData = Join-Path $env:USERPROFILE "AppData\Roaming"
    $expectedLocalAppData = Join-Path $env:USERPROFILE "AppData\Local"
    foreach ($pair in @(
        @($env:APPDATA, $expectedAppData, "APPDATA"),
        @($env:LOCALAPPDATA, $expectedLocalAppData, "LOCALAPPDATA")
    )) {
        $actualRoot = [IO.Path]::GetFullPath($pair[0]).TrimEnd('\')
        $expectedRoot = [IO.Path]::GetFullPath($pair[1]).TrimEnd('\')
        if (-not $actualRoot.Equals($expectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing destructive installer smoke test: $($pair[2]) is outside the hosted runner profile."
        }
    }
}

Assert-DisposableHostedRunner

$baseline = (Resolve-Path $BaselineInstaller).Path
$current = (Resolve-Path $CurrentInstaller).Path
$baselinePayloadHash = $BaselinePayloadSha256.ToLowerInvariant()
$currentPayloadHash = $CurrentPayloadSha256.ToLowerInvariant()
if ($baselinePayloadHash -ceq $currentPayloadHash) {
    throw "Baseline and current payload digests must be different."
}
$installDirectory = Join-Path $env:LOCALAPPDATA "Programs\ClarifyVoice"
$installedExe = Join-Path $installDirectory "ClarifyVoice.exe"
$configDirectory = Join-Path $env:APPDATA "ClarifyVoice"
$sentinel = Join-Path $configDirectory "config.json"
$sentinelContent = '{"schema_version":1,"openai_api_key":"test-only-sentinel"}'
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "ClarifyVoice.lnk"
$menuShortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\ClarifyVoice\ClarifyVoice.lnk"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$metadataKey = "HKCU:\Software\ClarifyVoice"

function Assert-CleanSmokeTarget {
    foreach ($path in @($installDirectory, $configDirectory, $desktopShortcut, $menuShortcut)) {
        if (Test-Path $path) {
            throw "Refusing destructive installer smoke test: pre-existing ClarifyVoice state at $path"
        }
    }
    if (Test-Path $metadataKey) {
        throw "Refusing destructive installer smoke test: pre-existing ClarifyVoice installer metadata."
    }
    if (Get-ItemProperty -Path $runKey -Name ClarifyVoice -ErrorAction SilentlyContinue) {
        throw "Refusing destructive installer smoke test: pre-existing ClarifyVoice autostart entry."
    }
}

function Invoke-MsiExec {
    param([string[]]$Arguments, [string]$Operation)

    $process = Start-Process msiexec.exe -ArgumentList $Arguments `
        -Wait -PassThru -NoNewWindow
    if ($process.ExitCode -notin @(0, 1641, 3010)) {
        throw "$Operation failed with Windows Installer exit code $($process.ExitCode)."
    }
}

function Assert-Installed([string]$Operation, [string]$ExpectedPayloadHash) {
    foreach ($path in @($installedExe, $desktopShortcut, $menuShortcut, $sentinel)) {
        if (-not (Test-Path $path -PathType Leaf)) {
            throw "$Operation did not preserve the expected path: $path"
        }
    }
    if ((Get-Content -LiteralPath $sentinel -Raw).Trim() -cne $sentinelContent) {
        throw "$Operation changed user configuration or credentials."
    }
    $actualPayloadHash = (Get-FileHash $installedExe -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualPayloadHash -cne $ExpectedPayloadHash) {
        throw "$Operation installed payload $actualPayloadHash instead of $ExpectedPayloadHash."
    }
}

function Assert-AutostartPreserved([string]$Operation) {
    $entry = Get-ItemPropertyValue -Path $runKey -Name ClarifyVoice `
        -ErrorAction SilentlyContinue
    if (-not $entry -or $entry -notlike "*$installedExe*") {
        throw "$Operation did not preserve the user-controlled autostart entry."
    }
}

Assert-CleanSmokeTarget
New-Item $configDirectory -ItemType Directory -Force | Out-Null
$sentinelContent | Set-Content -LiteralPath $sentinel -Encoding utf8

try {
    Invoke-MsiExec @('/i', $baseline, '/qn', '/norestart') "clean install"
    Assert-Installed "clean install" $baselinePayloadHash
    New-Item $runKey -Force | Out-Null
    Set-ItemProperty -Path $runKey -Name ClarifyVoice `
        -Value ('"' + $installedExe + '" --hidden')

    Invoke-MsiExec @('/i', $current, '/qn', '/norestart') "upgrade"
    Assert-Installed "upgrade" $currentPayloadHash
    Assert-AutostartPreserved "upgrade"

    Invoke-MsiExec @('/fa', $current, '/qn', '/norestart') "repair"
    Assert-Installed "repair" $currentPayloadHash
    Assert-AutostartPreserved "repair"

    Invoke-MsiExec @('/i', $baseline, '/qn', '/norestart') "manual rollback"
    Assert-Installed "manual rollback" $baselinePayloadHash
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
    if (Test-Path $sentinel -PathType Leaf) {
        $finalSentinelContent = (Get-Content -LiteralPath $sentinel -Raw).Trim()
        if ($finalSentinelContent -ceq $sentinelContent) {
            Remove-Item $sentinel -Force
        } else {
            Write-Warning "Refusing to remove config.json because it is no longer the smoke-test sentinel."
        }
    }
}

Write-Host "Installer install/upgrade/repair/rollback/uninstall smoke test passed."
