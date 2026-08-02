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
if ($policy.require_rfc3161_timestamp -cne $true) {
    throw "Release policy must require RFC3161 timestamps."
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

    # Get-AuthenticodeSignature exposes a timestamp certificate, but that
    # property alone does not distinguish RFC3161 from a legacy Authenticode
    # countersignature or validate the TSA chain.  SignTool performs the
    # independent Windows trust check and prints the timestamp protocol.
    $signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if (-not $signtool) {
        throw "signtool.exe is required for independent RFC3161 verification."
    }
    $signtoolOutput = @(& $signtool.Source verify /pa /all /tw /v $resolved 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "SignTool rejected the signature or TSA chain for $resolved."
    }
    $timestampProtocols = @()
    $inTimestampTable = $false
    foreach ($line in $signtoolOutput) {
        $text = [string]$line
        if ($text -match '^\s*Index\s+Algorithm\s+Timestamp\s*$') {
            $inTimestampTable = $true
            continue
        }
        if ($inTimestampTable -and
            ($text -match '^\s*0\s+\S+\s+(?<protocol>\S+)\s*$')) {
            $timestampProtocols += [string]$Matches.protocol
        }
    }
    if ($timestampProtocols.Count -ne 1 -or
        $timestampProtocols[0] -cne "RFC3161") {
        throw "A valid RFC3161 timestamp token was not reported for $resolved."
    }
    $timestampCertificate = $signature.TimeStamperCertificate
    if (-not $timestampCertificate) {
        throw "RFC3161 timestamp signer certificate is missing for $resolved."
    }
    $timestampCommonName = $timestampCertificate.GetNameInfo(
        [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
        $false)
    Write-Host "Verified $resolved"
    Write-Host "Publisher: $commonName"
    Write-Host "Thumbprint: $($signature.SignerCertificate.Thumbprint)"
    Write-Host "Timestamp status: RFC3161"
    Write-Host "Timestamp signer: $timestampCommonName"
    Write-Host "Timestamp thumbprint: $($timestampCertificate.Thumbprint)"
}
