[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("CustomTkinter", "PySide6")]
    [string]$Target,
    [Parameter(Mandatory = $true)] [string]$Executable,
    [Parameter(Mandatory = $true)] [string]$RunId,
    [Parameter(Mandatory = $true)] [ValidateRange(1, 10)] [int]$Round,
    [string]$OutputCsv = "measurements-windows.csv"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-ProcessTree {
    param([int]$RootId)
    $all = @(Get-CimInstance Win32_Process)
    $ids = [System.Collections.Generic.HashSet[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    $ids.Add($RootId) | Out-Null
    $queue.Enqueue($RootId)
    while ($queue.Count -gt 0) {
        $parent = $queue.Dequeue()
        foreach ($candidate in $all | Where-Object { $_.ParentProcessId -eq $parent }) {
            if ($ids.Add([int]$candidate.ProcessId)) {
                $queue.Enqueue([int]$candidate.ProcessId)
            }
        }
    }
    return @($ids)
}

function Get-TreeProcesses {
    param([int]$RootId)
    $ids = @(Get-ProcessTree $RootId)
    return @($ids | ForEach-Object {
        try { Get-Process -Id $_ -ErrorAction Stop } catch { $null }
    } | Where-Object { $_ })
}

function Get-TreeWindowProcess {
    param([int]$RootId)
    foreach ($candidate in @(Get-TreeProcesses $RootId)) {
        try {
            $candidate.Refresh()
            if ($candidate.MainWindowHandle -ne 0) {
                return $candidate
            }
        } catch {
            # A short-lived bootloader process can disappear between snapshots.
        }
    }
    return $null
}

function Get-BootId {
    $os = Get-CimInstance Win32_OperatingSystem
    return ([DateTime]$os.LastBootUpTime).ToUniversalTime().ToString("o")
}

function Get-HostId {
    $machineGuid = (Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Cryptography" -Name MachineGuid -ErrorAction Stop).MachineGuid
    if ([string]::IsNullOrWhiteSpace([string]$machineGuid)) {
        throw "Windows MachineGuid is unavailable; cannot identify benchmark host."
    }
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$machineGuid)
        $digest = $sha256.ComputeHash($bytes)
        return "sha256:" + (-join ($digest | ForEach-Object { $_.ToString("x2") }))
    } finally {
        $sha256.Dispose()
    }
}

function Measure-Executable {
    param([string]$Path)
    if (-not (Test-Path $Path)) { throw "Missing executable: $Path" }
    $resolved = (Resolve-Path $Path).Path
    $sizeMb = [math]::Round((Get-Item $resolved).Length / 1MB, 2)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $process = Start-Process -FilePath $resolved -PassThru
    $mainWindowSeen = $false
    $windowProcessId = $null
    while ($watch.Elapsed.TotalSeconds -lt 20) {
        Start-Sleep -Milliseconds 100
        $windowProcess = Get-TreeWindowProcess $process.Id
        if ($null -ne $windowProcess) {
            $mainWindowSeen = $true
            $windowProcessId = $windowProcess.Id
            break
        }
    }
    $coldStartMs = [math]::Round($watch.Elapsed.TotalMilliseconds, 0)
    Start-Sleep -Seconds 5
    $processes = @(Get-TreeProcesses $process.Id)
    $workingSetMb = [math]::Round((($processes | Measure-Object WorkingSet64 -Sum).Sum) / 1MB, 2)
    $privateMb = [math]::Round((($processes | Measure-Object PrivateMemorySize64 -Sum).Sum) / 1MB, 2)
    $threads = ($processes | ForEach-Object { $_.Threads.Count } | Measure-Object -Sum).Sum
    $result = [pscustomobject]@{
        Target = $Target
        Round = $Round
        RunId = $RunId
        BootId = Get-BootId
        HostId = Get-HostId
        Executable = $resolved
        ColdStartMs = $coldStartMs
        MainWindowSeen = $mainWindowSeen
        WindowProcessId = $windowProcessId
        WorkingSetMB = $workingSetMb
        PrivateMemoryMB = $privateMb
        ProcessCount = $processes.Count
        ThreadCount = $threads
        PackageSizeMB = $sizeMb
        MeasuredAtUtc = [DateTime]::UtcNow.ToString("o")
    }
    foreach ($runningProcess in $processes) {
        try { Stop-Process -Id $runningProcess.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
    return $result
}

$result = Measure-Executable $Executable
$destination = if ([System.IO.Path]::IsPathRooted($OutputCsv)) { $OutputCsv } else { Join-Path (Get-Location) $OutputCsv }
$parent = Split-Path -Parent $destination
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
$result | Export-Csv -NoTypeInformation -Encoding UTF8 -Path $destination
Write-Host "One-target measurement written to $destination"
