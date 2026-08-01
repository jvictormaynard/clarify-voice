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

$rows = @(
    foreach ($path in $InputCsv) {
        if (-not (Test-Path $path)) { throw "Missing measurement CSV: $path" }
        Import-Csv $path
    }
)
if ($rows.Count -eq 0) { throw "No measurement rows were provided." }

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

$destination = if ([System.IO.Path]::IsPathRooted($OutputCsv)) { $OutputCsv } else { Join-Path (Get-Location) $OutputCsv }
$parent = Split-Path -Parent $destination
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
$summaries | Export-Csv -NoTypeInformation -Encoding UTF8 -Path $destination
Write-Host "Summary uses independent post-reboot rounds; no fixed target order was assumed."
Write-Host "Measurement summary written to $destination"
