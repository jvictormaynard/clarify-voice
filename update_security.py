"""Fail-closed Windows release discovery and update preparation.

The release manifest is carried inside an Authenticode-signed CAB.  The CAB
authenticates the manifest with the same managed publisher identity used for
the executable and MSI, avoiding a second private release key.  This module
never installs without an explicit caller decision; it only returns a fully
verified local MSI path.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests


POLICY_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILE_NAME = "release-manifest.json"
SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class UpdateSecurityError(RuntimeError):
    """Base class for update failures that must not be bypassed."""


class UpdatePolicyError(UpdateSecurityError):
    pass


class ManifestError(UpdateSecurityError):
    pass


class SignatureError(UpdateSecurityError):
    pass


class IntegrityError(UpdateSecurityError):
    pass


class DowngradeError(UpdateSecurityError):
    pass


@dataclass(frozen=True)
class UpdatePolicy:
    repository: str
    channel: str
    manifest_asset: str
    installer_asset: str
    publisher_common_name: str
    require_rfc3161_timestamp: bool
    maximum_download_bytes: int

    @property
    def manifest_url(self) -> str:
        return (
            f"https://github.com/{self.repository}/releases/latest/download/"
            f"{self.manifest_asset}"
        )


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    sha256: str
    size: int
    publisher_common_name: str


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    release_tag: str
    channel: str
    asset: ReleaseAsset


@dataclass(frozen=True)
class SignatureIdentity:
    common_name: str
    thumbprint: str
    timestamp_common_name: str
    timestamp_thumbprint: str


@dataclass(frozen=True)
class PreparedUpdate:
    manifest: ReleaseManifest
    installer_path: Path
    signer: SignatureIdentity
    require_rfc3161_timestamp: bool = True


def _resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(data: bytes, *, label: str, maximum_bytes: int) -> dict[str, Any]:
    if not data or len(data) > maximum_bytes:
        raise ManifestError(f"{label} has an invalid size")
    try:
        payload = json.loads(
            data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ManifestError(f"{label} is not canonical UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ManifestError(f"{label} must be a JSON object")
    return payload


def _require_exact_keys(
        payload: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ManifestError(
            f"{label} fields do not match the schema "
            f"(missing={missing}, extra={extra})")


def parse_version(value: str) -> tuple[int, int, int]:
    match = SEMVER_PATTERN.fullmatch(str(value))
    if not match:
        raise ManifestError("version must be stable SemVer X.Y.Z")
    return tuple(int(part) for part in match.groups())


def load_update_policy(path: Path | None = None) -> UpdatePolicy:
    policy_path = path or (_resource_root() / "distribution" / "update-policy.json")
    try:
        payload = _read_json(
            policy_path.read_bytes(), label="update policy", maximum_bytes=16_384)
    except OSError as error:
        raise UpdatePolicyError(f"cannot read update policy: {error}") from error
    _require_exact_keys(payload, {
        "schema_version", "repository", "channel", "manifest_asset",
        "installer_asset", "publisher_common_name", "require_rfc3161_timestamp",
        "maximum_download_bytes",
    }, label="update policy")
    if payload["schema_version"] != POLICY_SCHEMA_VERSION:
        raise UpdatePolicyError("unsupported update policy schema")
    repository = str(payload["repository"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise UpdatePolicyError("invalid update repository")
    channel = str(payload["channel"])
    if channel != "stable":
        raise UpdatePolicyError("only the stable channel is supported")
    manifest_asset = str(payload["manifest_asset"])
    installer_asset = str(payload["installer_asset"])
    if manifest_asset != "ClarifyVoice-release-manifest.cab":
        raise UpdatePolicyError("unexpected manifest asset identity")
    if installer_asset != "ClarifyVoice-windows-x64.msi":
        raise UpdatePolicyError("unexpected installer asset identity")
    publisher = str(payload["publisher_common_name"]).strip()
    if not publisher or len(publisher) > 256:
        raise UpdatePolicyError("publisher identity is not configured")
    require_timestamp = payload["require_rfc3161_timestamp"]
    if require_timestamp is not True:
        raise UpdatePolicyError("update policy must require RFC3161 timestamps")
    maximum = payload["maximum_download_bytes"]
    if not isinstance(maximum, int) or not 1_048_576 <= maximum <= 1_073_741_824:
        raise UpdatePolicyError("invalid maximum download size")
    return UpdatePolicy(
        repository=repository,
        channel=channel,
        manifest_asset=manifest_asset,
        installer_asset=installer_asset,
        publisher_common_name=publisher,
        require_rfc3161_timestamp=require_timestamp,
        maximum_download_bytes=maximum,
    )


def parse_release_manifest(data: bytes, policy: UpdatePolicy) -> ReleaseManifest:
    payload = _read_json(data, label="release manifest", maximum_bytes=65_536)
    _require_exact_keys(payload, {
        "schema_version", "version", "release_tag", "channel", "asset",
    }, label="release manifest")
    if payload["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestError("unsupported release manifest schema")
    version = str(payload["version"])
    parse_version(version)
    release_tag = str(payload["release_tag"])
    if release_tag != f"v{version}":
        raise ManifestError("release tag does not match version")
    channel = str(payload["channel"])
    if channel != policy.channel:
        raise ManifestError("release channel does not match policy")
    asset_data = payload["asset"]
    if not isinstance(asset_data, dict):
        raise ManifestError("release asset must be an object")
    _require_exact_keys(asset_data, {
        "name", "url", "sha256", "size", "publisher_common_name",
    }, label="release asset")
    name = str(asset_data["name"])
    if name != policy.installer_asset:
        raise ManifestError("release asset name does not match policy")
    expected_url = (
        f"https://github.com/{policy.repository}/releases/download/"
        f"{release_tag}/{name}"
    )
    url = str(asset_data["url"])
    if url != expected_url:
        raise ManifestError("release asset URL does not match identity")
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "github.com":
        raise ManifestError("release asset URL is not an approved HTTPS origin")
    digest = str(asset_data["sha256"])
    if not SHA256_PATTERN.fullmatch(digest):
        raise ManifestError("release asset checksum is not lowercase SHA-256")
    size = asset_data["size"]
    if not isinstance(size, int) or not 1 <= size <= policy.maximum_download_bytes:
        raise ManifestError("release asset size is outside policy")
    publisher = str(asset_data["publisher_common_name"])
    if publisher != policy.publisher_common_name:
        raise ManifestError("release asset publisher does not match policy")
    return ReleaseManifest(
        version=version,
        release_tag=release_tag,
        channel=channel,
        asset=ReleaseAsset(
            name=name,
            url=url,
            sha256=digest,
            size=size,
            publisher_common_name=publisher,
        ),
    )


def require_newer_version(current_version: str, manifest: ReleaseManifest) -> bool:
    current = parse_version(current_version)
    offered = parse_version(manifest.version)
    if offered < current:
        raise DowngradeError(
            f"refusing downgrade from {current_version} to {manifest.version}")
    return offered > current


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_atomic(
        url: str,
        destination: Path,
        *,
        maximum_bytes: int,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        session: Any = requests,
        timeout: tuple[int, int] = (10, 60)) -> Path:
    """Download to a sibling .part file and publish only verified bytes."""

    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise IntegrityError("download origin is not allowed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_name(destination.name + ".part")
    try:
        part_path.unlink(missing_ok=True)
        response = session.get(
            url, stream=True, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
        final_url = urlparse(str(getattr(response, "url", url)))
        if final_url.scheme != "https":
            raise IntegrityError("download redirect is not HTTPS")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                announced = int(content_length)
            except ValueError as error:
                raise IntegrityError("invalid Content-Length") from error
            if announced > maximum_bytes:
                raise IntegrityError("download exceeds maximum size")
            if expected_size is not None and announced != expected_size:
                raise IntegrityError("download size does not match manifest")
        digest = hashlib.sha256()
        total = 0
        with part_path.open("xb") as stream:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > maximum_bytes:
                    raise IntegrityError("download exceeds maximum size")
                stream.write(chunk)
                digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if expected_size is not None and total != expected_size:
            raise IntegrityError("download size does not match manifest")
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise IntegrityError("download checksum does not match manifest")
        os.replace(part_path, destination)
        return destination
    except Exception:
        part_path.unlink(missing_ok=True)
        raise


def _powershell_executable() -> str:
    return "powershell.exe" if platform.system() == "Windows" else "powershell"


def _signtool_executable() -> str:
    """Return the Windows SDK verifier used for independent timestamp checks."""

    return "signtool.exe"


_SIGNTOOL_TIMESTAMP_HEADER = re.compile(
    r"(?im)^\s*Index\s+Algorithm\s+Timestamp\s*$")
_SIGNTOOL_TIMESTAMP_ROW = re.compile(
    r"(?im)^\s*(?P<index>\d+)\s+\S+\s+(?P<timestamp>\S+)\s*$")


def _parse_signtool_timestamp_status(output: str) -> str:
    """Parse the primary timestamp protocol from verbose SignTool output.

    SignTool's verbose verification table labels RFC 3161 tokens explicitly.
    Requiring the table header and a single primary (index zero) row avoids
    treating a generic timestamp certificate or arbitrary diagnostic text as
    proof of an RFC 3161 token.  Unknown output is intentionally invalid.
    """

    header = _SIGNTOOL_TIMESTAMP_HEADER.search(output)
    if not header:
        return "Invalid"
    rows = [
        match
        for match in _SIGNTOOL_TIMESTAMP_ROW.finditer(output, header.end())
        if int(match.group("index")) == 0
    ]
    if len(rows) != 1:
        return "Invalid"
    return (
        "Valid"
        if rows[0].group("timestamp").upper() == "RFC3161"
        else "Invalid"
    )


def _verify_rfc3161_timestamp(
        path: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]],
        signtool: str | None = None) -> None:
    """Verify the timestamp token and TSA chain with Windows SignTool.

    ``Get-AuthenticodeSignature`` exposes the timestamp certificate but does
    not establish that the embedded token is RFC 3161 or that its TSA chain
    validates independently.  SignTool performs that Windows trust check; its
    explicit verbose protocol row is parsed separately and is required to be
    RFC3161.  Any missing tool, non-zero result, or unfamiliar output fails
    closed.
    """

    executable = signtool or _signtool_executable()
    try:
        result = runner(
            [
                executable,
                "verify",
                "/pa",
                "/all",
                "/tw",
                "/v",
                str(path.resolve()),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise SignatureError(
            "RFC3161 timestamp verifier is unavailable") from error
    output = "\n".join(
        str(part or "") for part in (result.stdout, result.stderr))
    if result.returncode:
        detail = output.strip().splitlines()
        suffix = f": {detail[-1][:240]}" if detail else ""
        raise SignatureError(
            "RFC3161 timestamp or TSA chain verification failed" + suffix)
    if _parse_signtool_timestamp_status(output) != "Valid":
        raise SignatureError(
            "SignTool did not confirm a valid RFC3161 timestamp token")


def verify_authenticode(
        path: Path,
        expected_common_name: str,
        *,
        require_timestamp: bool = True,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        powershell: str | None = None,
        signtool: str | None = None) -> SignatureIdentity:
    """Require a trusted signature, pinned publisher, and valid timestamp."""

    if not path.is_file():
        raise SignatureError(f"signed file is missing: {path}")
    executable = powershell or _powershell_executable()
    script = (
        "$ErrorActionPreference='Stop';"
        "$signature=Get-AuthenticodeSignature -LiteralPath "
        "$env:CLARIFYVOICE_SIGNATURE_PATH;"
        "$certificate=$signature.SignerCertificate;"
        "$timestampCertificate=$signature.TimeStamperCertificate;"
        "$timestampStatus=if(-not $timestampCertificate){'Missing'}"
        "else{'Present'};"
        "$result=[ordered]@{Status=[string]$signature.Status;"
        "StatusMessage=[string]$signature.StatusMessage;"
        "CommonName=if($certificate){$certificate.GetNameInfo("
        "[System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,"
        "$false)}else{''};"
        "Thumbprint=if($certificate){[string]$certificate.Thumbprint}else{''};"
        "TimestampStatus=$timestampStatus;"
        "TimestampCommonName=if($timestampCertificate){"
        "$timestampCertificate.GetNameInfo("
        "[System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,"
        "$false)}else{''};"
        "TimestampThumbprint=if($timestampCertificate){"
        "[string]$timestampCertificate.Thumbprint}else{''}};"
        "$result|ConvertTo-Json -Compress"
    )
    environment = os.environ.copy()
    environment["CLARIFYVOICE_SIGNATURE_PATH"] = str(path.resolve())
    result = runner(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode:
        raise SignatureError(
            result.stderr.strip() or "Authenticode verification failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SignatureError("Authenticode verifier returned invalid output") from error
    if not isinstance(payload, dict):
        raise SignatureError("Authenticode verifier returned invalid output")
    if payload.get("Status") != "Valid":
        message = payload.get("StatusMessage") or payload.get("Status") or "unknown"
        raise SignatureError(f"Authenticode signature is not valid: {message}")
    common_name = str(payload.get("CommonName", "")).strip()
    if common_name != expected_common_name:
        raise SignatureError("Authenticode publisher does not match update policy")
    thumbprint = str(payload.get("Thumbprint", "")).replace(" ", "").upper()
    if not re.fullmatch(r"[0-9A-F]{40,128}", thumbprint):
        raise SignatureError("Authenticode certificate thumbprint is missing")
    timestamp_status = str(payload.get("TimestampStatus", "Missing"))
    timestamp_common_name = str(payload.get("TimestampCommonName", "")).strip()
    timestamp_thumbprint = str(
        payload.get("TimestampThumbprint", "")).replace(" ", "").upper()
    if require_timestamp:
        if timestamp_status != "Present":
            raise SignatureError(
                "RFC3161 timestamp is missing or invalid: " + timestamp_status)
        if not timestamp_common_name:
            raise SignatureError("RFC3161 timestamp signer is missing")
        if not re.fullmatch(r"[0-9A-F]{40,128}", timestamp_thumbprint):
            raise SignatureError("RFC3161 timestamp certificate is missing")
        _verify_rfc3161_timestamp(
            path, runner=runner, signtool=signtool)
    return SignatureIdentity(
        common_name=common_name,
        thumbprint=thumbprint,
        timestamp_common_name=timestamp_common_name,
        timestamp_thumbprint=timestamp_thumbprint,
    )


def extract_manifest_from_cab(
        cab_path: Path,
        destination: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        expand_executable: str = "expand.exe") -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / MANIFEST_FILE_NAME
    manifest_path.unlink(missing_ok=True)
    result = runner(
        [expand_executable, str(cab_path), f"-F:{MANIFEST_FILE_NAME}", str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode or not manifest_path.is_file():
        raise ManifestError(
            result.stderr.strip() or result.stdout.strip()
            or "signed manifest container could not be extracted")
    if manifest_path.stat().st_size > 65_536:
        raise ManifestError("release manifest exceeds maximum size")
    return manifest_path


def prepare_update(
        current_version: str,
        cache_directory: Path,
        *,
        policy: UpdatePolicy | None = None,
        session: Any = requests,
        signature_verifier: Callable[..., SignatureIdentity] = verify_authenticode,
        manifest_extractor: Callable[[Path, Path], Path] = extract_manifest_from_cab,
        downloader: Callable[..., Path] = download_atomic) -> PreparedUpdate | None:
    """Return a verified MSI for a newer release, or None when already current."""

    active_policy = policy or load_update_policy()
    cache_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix=".update-staging-", dir=cache_directory) as temporary:
        staging_directory = Path(temporary)
        cab_path = staging_directory / active_policy.manifest_asset
        downloader(
            active_policy.manifest_url,
            cab_path,
            maximum_bytes=4 * 1024 * 1024,
            session=session,
        )
        signature_verifier(
            cab_path,
            active_policy.publisher_common_name,
            require_timestamp=active_policy.require_rfc3161_timestamp,
        )
        manifest_path = manifest_extractor(
            cab_path, staging_directory / "manifest")
        manifest = parse_release_manifest(manifest_path.read_bytes(), active_policy)
        if not require_newer_version(current_version, manifest):
            return None
        staged_installer = staging_directory / manifest.asset.name
        downloader(
            manifest.asset.url,
            staged_installer,
            maximum_bytes=active_policy.maximum_download_bytes,
            expected_size=manifest.asset.size,
            expected_sha256=manifest.asset.sha256,
            session=session,
        )
        signer = signature_verifier(
            staged_installer,
            manifest.asset.publisher_common_name,
            require_timestamp=active_policy.require_rfc3161_timestamp,
        )
        version_directory = cache_directory / manifest.version
        version_directory.mkdir(parents=True, exist_ok=True)
        installer_path = version_directory / manifest.asset.name
        os.replace(staged_installer, installer_path)
        return PreparedUpdate(
            manifest=manifest,
            installer_path=installer_path,
            signer=signer,
            require_rfc3161_timestamp=active_policy.require_rfc3161_timestamp,
        )


def launch_prepared_update(
        prepared: PreparedUpdate,
        *,
        signature_verifier: Callable[..., SignatureIdentity] = verify_authenticode,
        runner: Callable[..., subprocess.Popen[Any]] = subprocess.Popen) -> None:
    """Revalidate and launch a prepared MSI with visible Windows Installer UI."""

    if platform.system() != "Windows":
        raise UpdateSecurityError("updates can only be installed on Windows")
    installer_path = prepared.installer_path
    asset = prepared.manifest.asset
    if not installer_path.is_file() or installer_path.suffix.lower() != ".msi":
        raise UpdateSecurityError("verified MSI is missing")
    if installer_path.stat().st_size != asset.size:
        raise IntegrityError("prepared installer size changed before launch")
    if sha256_file(installer_path) != asset.sha256:
        raise IntegrityError("prepared installer checksum changed before launch")
    signature_verifier(
        installer_path,
        asset.publisher_common_name,
        require_timestamp=prepared.require_rfc3161_timestamp,
    )
    runner(["msiexec.exe", "/i", str(installer_path.resolve()), "/passive"])
