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

function Resolve-ExistingPath {
    param([string]$Path)
    return (Get-Item -LiteralPath $Path -ErrorAction Stop).FullName
}

function ConvertTo-PositiveInteger {
    param([string]$Value, [string]$Metric, [string]$Path)

    $parsed = [long]0
    $integerStyle = [System.Globalization.NumberStyles]::Integer
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    if (-not [long]::TryParse($Value, $integerStyle, $invariant, [ref]$parsed) -or $parsed -le 0) {
        throw "Rejected invalid integer $Metric in $Path."
    }
    return $parsed
}

function ConvertTo-CanonicalBootId {
    param([string]$Value, [string]$Path)

    $pattern = "^(?<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{7})?)(?<zone>Z|[+-]\d{2}:\d{2})$"
    if ($Value -cnotmatch $pattern) {
        throw "Rejected invalid BootId in $Path."
    }
    $stamp = $Matches.stamp
    $zone = $Matches.zone
    $parseValue = $stamp + $(if ($zone -ceq "Z") { "+00:00" } else { $zone })
    $format = if ($stamp.Contains(".")) {
        "yyyy-MM-dd'T'HH:mm:ss.fffffffzzz"
    } else {
        "yyyy-MM-dd'T'HH:mm:sszzz"
    }
    $parsed = [DateTimeOffset]::MinValue
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    $styles = [System.Globalization.DateTimeStyles]::None
    if (-not [DateTimeOffset]::TryParseExact($parseValue, $format, $invariant, $styles, [ref]$parsed)) {
        throw "Rejected invalid BootId in $Path."
    }
    return $parsed.ToUniversalTime().ToString("o", $invariant)
}

function ConvertTo-CanonicalHostId {
    param([string]$Value, [string]$Path)

    $pattern = "^sha256:[0-9a-f]{64}$"
    if ($Value -cnotmatch $pattern) {
        throw "Rejected invalid HostId in $Path."
    }
    return $Value.ToLowerInvariant()
}

function ConvertTo-CanonicalSha256 {
    param([string]$Value, [string]$Metric, [string]$Path)

    if ($Value -cnotmatch "^[0-9a-fA-F]{64}$") {
        throw "Rejected invalid SHA-256 $Metric in $Path."
    }
    return $Value.ToLowerInvariant()
}

function ConvertTo-ValidMeasurement {
    param([object]$Row, [string]$Path)

    $seen = $false
    if (-not [bool]::TryParse([string]$Row.MainWindowSeen, [ref]$seen) -or -not $seen) {
        throw "Rejected unsuccessful launch in $Path (MainWindowSeen is not true)."
    }
    foreach ($required in @("Target", "RunId", "BootId", "HostId", "Round", "WindowProcessId")) {
        if ([string]::IsNullOrWhiteSpace([string]$Row.$required)) {
            throw "Rejected incomplete measurement in $Path (missing $required)."
        }
    }
    if (@("CustomTkinter", "PySide6") -notcontains [string]$Row.Target) {
        throw "Rejected unknown target in $Path."
    }
    $Row.BootId = ConvertTo-CanonicalBootId ([string]$Row.BootId) $Path
    $Row.HostId = ConvertTo-CanonicalHostId ([string]$Row.HostId) $Path
    $hashProperties = @(
        "ExecutableSHA256",
        "ArtifactManifestSHA256",
        "ManifestArtifactSHA256"
    )
    $presentHashes = @($hashProperties | Where-Object {
        $Row.PSObject.Properties.Name -contains $_ -and
        -not [string]::IsNullOrWhiteSpace([string]$Row.$_)
    })
    if ($presentHashes.Count -gt 0 -and $presentHashes.Count -ne $hashProperties.Count) {
        throw "Measurement in $Path must include all artifact hash fields together."
    }
    foreach ($hashProperty in $presentHashes) {
        $Row.$hashProperty = ConvertTo-CanonicalSha256 ([string]$Row.$hashProperty) $hashProperty $Path
    }
    $round = 0
    $integerStyle = [System.Globalization.NumberStyles]::Integer
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    if (-not [int]::TryParse([string]$Row.Round, $integerStyle, $invariant, [ref]$round) -or $round -lt 1) {
        throw "Rejected invalid round in $Path."
    }
    $Row.Round = $round
    foreach ($metric in @("WindowProcessId", "ProcessCount", "ThreadCount")) {
        [void](ConvertTo-PositiveInteger ([string]$Row.$metric) $metric $Path)
    }
    $positiveMetrics = @("ColdStartMs", "WorkingSetMB", "PrivateMemoryMB", "PackageSizeMB")
    foreach ($metric in $positiveMetrics) {
        $value = 0.0
        $floatStyle = [System.Globalization.NumberStyles]::Float
        if (-not [double]::TryParse([string]$Row.$metric, $floatStyle, $invariant, [ref]$value)) {
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
if (Test-Path -LiteralPath $destination) {
    $destinationKey = (Resolve-ExistingPath $destination).ToLowerInvariant()
}
$rows = @()
foreach ($path in $InputCsv) {
    if (-not (Test-Path $path)) { throw "Missing measurement CSV: $path" }
    $resolvedInput = Resolve-ExistingPath $path
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
$hostIds = @($rows | Select-Object -ExpandProperty HostId -Unique)
if ($hostIds.Count -ne 1) {
    throw "Measurements must come from exactly one benchmark host."
}

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
