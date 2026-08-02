[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Path,
    [string]$PolicyPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $PolicyPath) {
    $PolicyPath = Join-Path $repoRoot "distribution\update-policy.json"
}
$policy = Get-Content -LiteralPath $PolicyPath -Raw | ConvertFrom-Json
$expectedPublisher = [string]$policy.publisher_common_name
if (-not $expectedPublisher) {
    throw "Update policy has no pinned publisher common name."
}

foreach ($candidate in $Path) {
    $resolved = (Resolve-Path $candidate).Path
    $signature = Get-AuthenticodeSignature -LiteralPath $resolved
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Invalid Authenticode signature for ${resolved}: $($signature.StatusMessage)"
    }
    if (-not $signature.SignerCertificate) {
        throw "Authenticode signer certificate is missing for $resolved."
    }
    $commonName = $signature.SignerCertificate.GetNameInfo(
        [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
        $false)
    if ($commonName -cne $expectedPublisher) {
        throw "Publisher mismatch for ${resolved}: expected '$expectedPublisher', got '$commonName'."
    }
    if (-not $signature.TimeStamperCertificate) {
        throw "RFC3161 timestamp is missing for $resolved."
    }
    Write-Host "Verified $resolved"
    Write-Host "Publisher: $commonName"
    Write-Host "Thumbprint: $($signature.SignerCertificate.Thumbprint)"
}
