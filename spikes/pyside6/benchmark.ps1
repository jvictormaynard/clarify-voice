[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$CustomTkinterExecutable,
    [Parameter(Mandatory = $true)] [string]$PySide6Executable,
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

function Measure-Executable {
    param([string]$Label, [string]$Path)
    if (-not (Test-Path $Path)) { throw "Missing executable: $Path" }
    $resolved = (Resolve-Path $Path).Path
    $sizeMb = [math]::Round((Get-Item $resolved).Length / 1MB, 2)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $process = Start-Process -FilePath $resolved -PassThru
    $mainWindowSeen = $false
    while ($watch.Elapsed.TotalSeconds -lt 20) {
        Start-Sleep -Milliseconds 100
        try { $process.Refresh() } catch { break }
        if ($process.HasExited) { break }
        if ($process.MainWindowHandle -ne 0) { $mainWindowSeen = $true; break }
    }
    $coldStartMs = [math]::Round($watch.Elapsed.TotalMilliseconds, 0)
    Start-Sleep -Seconds 5
    $ids = @(Get-ProcessTree $process.Id)
    $processes = @($ids | ForEach-Object {
        try { Get-Process -Id $_ -ErrorAction Stop } catch { $null }
    } | Where-Object { $_ })
    $workingSetMb = [math]::Round((($processes | Measure-Object WorkingSet64 -Sum).Sum) / 1MB, 2)
    $privateMb = [math]::Round((($processes | Measure-Object PrivateMemorySize64 -Sum).Sum) / 1MB, 2)
    $threads = ($processes | ForEach-Object { $_.Threads.Count } | Measure-Object -Sum).Sum
    $result = [pscustomobject]@{
        Target = $Label
        Executable = $resolved
        ColdStartMs = $coldStartMs
        MainWindowSeen = $mainWindowSeen
        WorkingSetMB = $workingSetMb
        PrivateMemoryMB = $privateMb
        ProcessCount = $processes.Count
        ThreadCount = $threads
        PackageSizeMB = $sizeMb
        MeasuredAtUtc = [DateTime]::UtcNow.ToString("o")
    }
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    return $result
}

$results = @(
    Measure-Executable "CustomTkinter" $CustomTkinterExecutable
    Measure-Executable "PySide6" $PySide6Executable
)
$destination = if ([System.IO.Path]::IsPathRooted($OutputCsv)) { $OutputCsv } else { Join-Path (Get-Location) $OutputCsv }
$results | Export-Csv -NoTypeInformation -Encoding UTF8 -Path $destination
Write-Host "Measurements written to $destination"
