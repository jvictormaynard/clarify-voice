[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string[]]$InputCsv,
    [string]$OutputCsv = "measurements-summary.csv"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-Median {
    param([double[]]$Values)
    $sorted = @($Values | Sort-Object)
    if ($sorted.Count -eq 0) { return $null }
    $middle = [int][math]::Floor($sorted.Count / 2)
    if (($sorted.Count % 2) -eq 1) { return $sorted[$middle] }
    return ($sorted[$middle - 1] + $sorted[$middle]) / 2
}

function Resolve-OutputPath {
    param([string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Path))
}

function ConvertTo-ValidMeasurement {
    param([object]$Row, [string]$Path)

    $seen = $false
    if (-not [bool]::TryParse([string]$Row.MainWindowSeen, [ref]$seen) -or -not $seen) {
        throw "Rejected unsuccessful launch in $Path (MainWindowSeen is not true)."
    }
    foreach ($required in @("Target", "RunId", "BootId", "Round", "WindowProcessId")) {
        if ([string]::IsNullOrWhiteSpace([string]$Row.$required)) {
            throw "Rejected incomplete measurement in $Path (missing $required)."
        }
    }
    if (@("CustomTkinter", "PySide6") -notcontains [string]$Row.Target) {
        throw "Rejected unknown target in $Path."
    }
    $round = 0
    if (-not [int]::TryParse([string]$Row.Round, [ref]$round) -or $round -lt 1) {
        throw "Rejected invalid round in $Path."
    }
    $positiveMetrics = @(
        "ColdStartMs", "WorkingSetMB", "PrivateMemoryMB", "ProcessCount",
        "ThreadCount", "PackageSizeMB", "WindowProcessId"
    )
    foreach ($metric in $positiveMetrics) {
        $value = 0.0
        if (-not [double]::TryParse([string]$Row.$metric, [ref]$value)) {
            throw "Rejected non-numeric $metric in $Path."
        }
        if ([double]::IsNaN($value) -or [double]::IsInfinity($value) -or $value -le 0) {
            throw "Rejected invalid $metric in $Path."
        }
    }
    return $Row
}

$destination = Resolve-OutputPath $OutputCsv
$destinationKey = $destination.ToLowerInvariant()
$rows = @()
foreach ($path in $InputCsv) {
    if (-not (Test-Path $path)) { throw "Missing measurement CSV: $path" }
    $resolvedInput = (Resolve-Path $path).ProviderPath
    if ($resolvedInput.ToLowerInvariant() -eq $destinationKey) {
        # The documented measurements/*.csv wildcard also sees summary.csv on
        # reruns. Exclude this output before importing or validating rows.
        continue
    }
    foreach ($row in @(Import-Csv $resolvedInput)) {
        $rows += ConvertTo-ValidMeasurement $row $resolvedInput
    }
}
if ($rows.Count -eq 0) { throw "No valid measurement rows were provided." }

$summaries = @()
foreach ($target in @("CustomTkinter", "PySide6")) {
    $targetRows = @($rows | Where-Object { $_.Target -eq $target })
    $bootIds = @($targetRows | Select-Object -ExpandProperty BootId -Unique)
    $rounds = @($targetRows | Select-Object -ExpandProperty Round -Unique)
    if ($targetRows.Count -lt 3 -or $bootIds.Count -lt 3 -or $rounds.Count -lt 3) {
        throw "$target needs at least three independent post-reboot rounds before comparison."
    }
    $summaries += [pscustomobject]@{
        Target = $target
        Samples = $targetRows.Count
        IndependentBoots = $bootIds.Count
        Rounds = $rounds.Count
        MedianColdStartMs = Get-Median @([double[]]($targetRows | ForEach-Object { $_.ColdStartMs }))
        MedianWorkingSetMB = Get-Median @([double[]]($targetRows | ForEach-Object { $_.WorkingSetMB }))
        MedianPrivateMemoryMB = Get-Median @([double[]]($targetRows | ForEach-Object { $_.PrivateMemoryMB }))
        MedianProcessCount = Get-Median @([double[]]($targetRows | ForEach-Object { $_.ProcessCount }))
        MedianThreadCount = Get-Median @([double[]]($targetRows | ForEach-Object { $_.ThreadCount }))
        MedianPackageSizeMB = Get-Median @([double[]]($targetRows | ForEach-Object { $_.PackageSizeMB }))
    }
}

$parent = Split-Path -Parent $destination
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
$summaries | Export-Csv -NoTypeInformation -Encoding UTF8 -Path $destination
Write-Host "Summary uses only successful, valid rows; no fixed target order was assumed."
Write-Host "Measurement summary written to $destination"
