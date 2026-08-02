[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Path,
    [string]$PolicyPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# SignTool's verbose stdout is intentionally not parsed: Microsoft documents
# its verification result and exit codes, but not a timestamp table format.
# CryptoAPI reads the embedded PKCS#7 signer attributes instead.
$timestampInspectorSource = @'
using System;
using System.Runtime.InteropServices;

public static class ClarifyVoiceTimestampInspector
{
    private const uint CertQueryObjectFile = 1;
    private const uint CertQueryContentPkcs7SignedEmbed = 10;
    private const uint CertQueryContentFlagPkcs7SignedEmbed = 1u << 10;
    private const uint CertQueryFormatBinary = 1;
    private const uint CertQueryFormatFlagBinary = 1u << 1;
    private const uint Pkcs7AsnEncoding = 0x10000;
    private const uint CmsgSignerCountParam = 5;
    private const uint CmsgSignerUnauthAttrParam = 10;
    private const string Rfc3161SignatureTimeStampTokenOid =
        "1.2.840.113549.1.9.16.2.14";
    private const string LegacyCountersignatureOid = "1.2.840.113549.1.9.6";
    private const string LegacyAuthenticodeCountersignatureOid =
        "1.3.6.1.4.1.311.3.2.1";

    [StructLayout(LayoutKind.Sequential)]
    private struct CryptAttributes
    {
        public uint Count;
        public IntPtr Attributes;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct CryptAttribute
    {
        public IntPtr ObjectId;
        public uint ValueCount;
        public IntPtr Values;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct CryptAttributeBlob
    {
        public uint DataSize;
        public IntPtr Data;
    }

    [DllImport("crypt32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CryptQueryObject(
        uint objectType, string objectPath, uint contentFlags,
        uint formatFlags, uint flags, out uint encoding, out uint contentType,
        out uint formatType, out IntPtr certStore, out IntPtr message,
        out IntPtr context);

    [DllImport("crypt32.dll", SetLastError = true)]
    private static extern bool CryptMsgGetParam(
        IntPtr message, uint parameter, uint index, IntPtr data,
        ref uint dataSize);

    [DllImport("crypt32.dll", SetLastError = true)]
    private static extern bool CryptMsgClose(IntPtr message);

    [DllImport("crypt32.dll", SetLastError = true)]
    private static extern bool CertCloseStore(IntPtr store, uint flags);

    private static bool HasNonEmptyValue(CryptAttribute attribute)
    {
        if (attribute.ValueCount != 1 || attribute.Values == IntPtr.Zero)
        {
            return false;
        }
        CryptAttributeBlob blob = (CryptAttributeBlob)Marshal.PtrToStructure(
            attribute.Values, typeof(CryptAttributeBlob));
        return blob.DataSize > 0 && blob.Data != IntPtr.Zero;
    }

    private static string ObjectIdentifier(IntPtr objectId)
    {
        return objectId == IntPtr.Zero ? null : Marshal.PtrToStringAnsi(objectId);
    }

    public static string GetTimestampProtocol(string path)
    {
        IntPtr store = IntPtr.Zero;
        IntPtr message = IntPtr.Zero;
        uint encoding;
        uint contentType;
        uint formatType;
        IntPtr context;
        try
        {
            if (!CryptQueryObject(
                    CertQueryObjectFile, path,
                    CertQueryContentFlagPkcs7SignedEmbed,
                    CertQueryFormatFlagBinary, 0, out encoding,
                    out contentType, out formatType, out store,
                    out message, out context) || message == IntPtr.Zero)
            {
                return "Invalid";
            }
            if (contentType != CertQueryContentPkcs7SignedEmbed ||
                formatType != CertQueryFormatBinary ||
                (encoding & Pkcs7AsnEncoding) == 0)
            {
                return "Invalid";
            }

            uint signerCountSize = sizeof(uint);
            IntPtr signerCountBuffer = Marshal.AllocHGlobal((int)signerCountSize);
            try
            {
                if (!CryptMsgGetParam(
                        message, CmsgSignerCountParam, 0, signerCountBuffer,
                        ref signerCountSize))
                {
                    return "Invalid";
                }
                uint signerCount = unchecked(
                    (uint)Marshal.ReadInt32(signerCountBuffer));
                if (signerCount == 0)
                {
                    return "Missing";
                }
                if (signerCount > 64)
                {
                    return "Invalid";
                }

                int rfc3161Count = 0;
                int legacyCount = 0;
                for (uint signerIndex = 0; signerIndex < signerCount;
                     signerIndex++)
                {
                    uint attributeSize = 0;
                    CryptMsgGetParam(
                        message, CmsgSignerUnauthAttrParam, signerIndex,
                        IntPtr.Zero, ref attributeSize);
                    if (attributeSize == 0)
                    {
                        // No unauthenticated attributes is the documented
                        // no-timestamp shape; the caller still rejects it.
                        continue;
                    }
                    if (attributeSize > Int32.MaxValue)
                    {
                        return "Invalid";
                    }
                    IntPtr attributeBuffer = Marshal.AllocHGlobal(
                        checked((int)attributeSize));
                    try
                    {
                        if (!CryptMsgGetParam(
                                message, CmsgSignerUnauthAttrParam,
                                signerIndex, attributeBuffer,
                                ref attributeSize))
                        {
                            return "Invalid";
                        }
                        CryptAttributes attributes = (CryptAttributes)
                            Marshal.PtrToStructure(
                                attributeBuffer, typeof(CryptAttributes));
                        if (attributes.Count > 4096 ||
                            (attributes.Count > 0 &&
                             attributes.Attributes == IntPtr.Zero))
                        {
                            return "Invalid";
                        }
                        for (uint attributeIndex = 0;
                             attributeIndex < attributes.Count;
                             attributeIndex++)
                        {
                            IntPtr attributePointer = IntPtr.Add(
                                attributes.Attributes,
                                checked((int)(attributeIndex *
                                              (uint)Marshal.SizeOf(
                                                  typeof(CryptAttribute)))));
                            if (attributePointer == IntPtr.Zero)
                            {
                                return "Invalid";
                            }
                            CryptAttribute attribute = (CryptAttribute)
                                Marshal.PtrToStructure(
                                    attributePointer, typeof(CryptAttribute));
                            string oid = ObjectIdentifier(attribute.ObjectId);
                            if (oid == Rfc3161SignatureTimeStampTokenOid)
                            {
                                if (!HasNonEmptyValue(attribute))
                                {
                                    return "Invalid";
                                }
                                rfc3161Count++;
                            }
                            else if (oid == LegacyCountersignatureOid ||
                                     oid == LegacyAuthenticodeCountersignatureOid)
                            {
                                legacyCount++;
                            }
                        }
                    }
                    finally
                    {
                        Marshal.FreeHGlobal(attributeBuffer);
                    }
                }
                if (rfc3161Count == 1 && legacyCount == 0)
                {
                    return "RFC3161";
                }
                if (rfc3161Count == 0 && legacyCount > 0)
                {
                    return "Legacy";
                }
                if (rfc3161Count == 0)
                {
                    return "Missing";
                }
                return "Invalid";
            }
            finally
            {
                Marshal.FreeHGlobal(signerCountBuffer);
            }
        }
        catch
        {
            // A malformed/unrecognized PKCS#7 is never proof of a timestamp.
            return "Invalid";
        }
        finally
        {
            if (message != IntPtr.Zero)
            {
                CryptMsgClose(message);
            }
            if (store != IntPtr.Zero)
            {
                CertCloseStore(store, 0);
            }
        }
    }
}
'@
Add-Type -TypeDefinition $timestampInspectorSource -Language CSharp

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

$signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
if (-not $signtool) {
    throw "signtool.exe is required for independent RFC3161 verification."
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

    # SignTool's documented exit code is authoritative for Windows signature
    # and TSA-chain validation. /tw may return a warning for no timestamp;
    # every non-zero result is rejected. No stdout format is parsed.
    $null = @(& $signtool.Source verify /pa /all /tw $resolved 2>$null)
    $signToolExitCode = $LASTEXITCODE
    if ($signToolExitCode -ne 0) {
        throw "SignTool rejected the signature or TSA chain for $resolved (exit $signToolExitCode)."
    }

    $timestampProtocol = [ClarifyVoiceTimestampInspector]::GetTimestampProtocol(
        $resolved)
    $timestampStatus = if ($timestampProtocol -ceq "RFC3161") {
        "Valid"
    } elseif ($timestampProtocol -ceq "Missing") {
        "Missing"
    } else {
        "Invalid"
    }
    if ($timestampStatus -cne "Valid") {
        throw "A valid RFC3161 signatureTimeStampToken was not found for $resolved (status $timestampStatus, protocol $timestampProtocol)."
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
    Write-Host "Timestamp status: $timestampStatus"
    Write-Host "Timestamp protocol: $timestampProtocol"
    Write-Host "Timestamp signer: $timestampCommonName"
    Write-Host "Timestamp thumbprint: $($timestampCertificate.Thumbprint)"
}
