[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$source = (Resolve-Path $Manifest).Path
if ((Split-Path $source -Leaf) -ne "release-manifest.json") {
    throw "The signed container must carry release-manifest.json."
}
$destinationDirectory = Split-Path -Parent $OutputPath
New-Item $destinationDirectory -ItemType Directory -Force | Out-Null
Remove-Item $OutputPath -Force -ErrorAction SilentlyContinue

& makecab.exe /D CompressionType=LZX $source $OutputPath | Out-Host
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $OutputPath -PathType Leaf)) {
    throw "makecab did not produce the release manifest container."
}

Write-Host "Manifest container complete: $OutputPath"
