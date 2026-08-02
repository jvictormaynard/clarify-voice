import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from update_security import (
    DowngradeError,
    IntegrityError,
    ManifestError,
    ReleaseManifest,
    ReleaseAsset,
    SignatureError,
    SignatureIdentity,
    UpdatePolicy,
    _AUTHENTICODE_TIMESTAMP_INSPECTOR,
    download_atomic,
    parse_release_manifest,
    prepare_update,
    require_newer_version,
    verify_authenticode,
    launch_prepared_update,
    PreparedUpdate,
)


POLICY = UpdatePolicy(
    repository="jvictormaynard/clarify-voice",
    channel="stable",
    manifest_asset="ClarifyVoice-release-manifest.cab",
    installer_asset="ClarifyVoice-windows-x64.msi",
    publisher_common_name="Joao Victor Maynard Mota",
    require_rfc3161_timestamp=True,
    maximum_download_bytes=1024 * 1024,
)


def manifest_bytes(version="0.2.0", **asset_overrides):
    asset = {
        "name": POLICY.installer_asset,
        "url": (
            "https://github.com/jvictormaynard/clarify-voice/"
            f"releases/download/v{version}/{POLICY.installer_asset}"
        ),
        "sha256": "a" * 64,
        "size": 3,
        "publisher_common_name": POLICY.publisher_common_name,
    }
    asset.update(asset_overrides)
    return json.dumps({
        "schema_version": 1,
        "version": version,
        "release_tag": f"v{version}",
        "channel": "stable",
        "asset": asset,
    }, separators=(",", ":")).encode()


class ManifestValidationTests(unittest.TestCase):
    def test_accepts_exact_expected_release_identity(self):
        manifest = parse_release_manifest(manifest_bytes(), POLICY)
        self.assertEqual(manifest.version, "0.2.0")
        self.assertEqual(manifest.asset.name, POLICY.installer_asset)

    def test_rejects_mismatched_asset_url(self):
        with self.assertRaises(ManifestError):
            parse_release_manifest(
                manifest_bytes(url="https://example.com/update.msi"), POLICY)

    def test_rejects_mismatched_publisher(self):
        with self.assertRaises(ManifestError):
            parse_release_manifest(
                manifest_bytes(publisher_common_name="Someone Else"), POLICY)

    def test_rejects_duplicate_json_fields(self):
        payload = manifest_bytes().decode().replace(
            '"schema_version":1', '"schema_version":1,"schema_version":1')
        with self.assertRaises(ManifestError):
            parse_release_manifest(payload.encode(), POLICY)

    def test_refuses_downgrade_and_ignores_same_version(self):
        manifest = parse_release_manifest(manifest_bytes("0.2.0"), POLICY)
        self.assertFalse(require_newer_version("0.2.0", manifest))
        with self.assertRaises(DowngradeError):
            require_newer_version("0.3.0", manifest)
        self.assertTrue(require_newer_version("0.1.2", manifest))


class AtomicDownloadTests(unittest.TestCase):
    class Response:
        def __init__(self, content, *, content_length=None):
            self.content = content
            self.url = "https://objects.githubusercontent.com/signed"
            self.headers = {}
            if content_length is not None:
                self.headers["Content-Length"] = str(content_length)

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            del chunk_size
            yield self.content[:1]
            yield self.content[1:]

    def test_download_is_published_only_after_size_and_checksum_match(self):
        data = b"msi"
        session = SimpleNamespace(get=Mock(return_value=self.Response(data, content_length=3)))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "update.msi"
            download_atomic(
                POLICY.manifest_url,
                destination,
                maximum_bytes=10,
                expected_size=3,
                expected_sha256=hashlib.sha256(data).hexdigest(),
                session=session,
            )
            self.assertEqual(destination.read_bytes(), data)
            self.assertFalse(Path(str(destination) + ".part").exists())

    def test_interrupted_or_tampered_download_removes_partial_file(self):
        session = SimpleNamespace(get=Mock(return_value=self.Response(b"bad")))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "update.msi"
            with self.assertRaises(IntegrityError):
                download_atomic(
                    POLICY.manifest_url,
                    destination,
                    maximum_bytes=10,
                    expected_size=3,
                    expected_sha256="0" * 64,
                    session=session,
                )
            self.assertFalse(destination.exists())
            self.assertFalse(Path(str(destination) + ".part").exists())


class SignatureValidationTests(unittest.TestCase):
    def _verify(self, artifact, payload, *, signtool_result=None):
        powershell_result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        if signtool_result is None:
            signtool_result = SimpleNamespace(
                returncode=0,
                # SignTool stdout is deliberately not parsed. Its documented
                # exit status is the only accepted chain-verification signal.
                stdout="unstructured SignTool output",
                stderr="",
            )
        runner = Mock(side_effect=[powershell_result, signtool_result])
        identity = verify_authenticode(
            artifact,
            POLICY.publisher_common_name,
            require_timestamp=POLICY.require_rfc3161_timestamp,
            runner=runner,
            powershell="powershell.exe",
            signtool="signtool.exe",
        )
        powershell_script = runner.call_args_list[0].args[0][-1]
        for field in (
            "CryptQueryObject",
            "CryptMsgGetParam",
            "CmsgSignerUnauthAttrParam",
            "Marshal.SizeOf",
            "IntPtr.Add",
            "CertQueryContentPkcs7SignedEmbed",
            "CertQueryFormatBinary",
            "1.2.840.113549.1.9.16.2.14",
            "1.2.840.113549.1.9.6",
            "TimeStamperCertificate",
            "TimestampStatus",
            "TimestampProtocol",
            "TimestampCommonName",
            "TimestampThumbprint",
        ):
            self.assertIn(field, powershell_script)
        self.assertNotIn("Index  Algorithm  Timestamp", powershell_script)
        self.assertNotIn("/v", powershell_script)
        self.assertNotIn("Marshal.ReadIntPtr", powershell_script)
        signtool_command = runner.call_args_list[1].args[0]
        self.assertEqual(
            signtool_command,
            [
                "signtool.exe",
                "verify",
                "/pa",
                "/all",
                "/tw",
                str(artifact.resolve()),
            ],
        )
        return identity

    def _valid_payload(self, **overrides):
        payload = {
            "Status": "Valid",
            "StatusMessage": "Signature verified.",
            "CommonName": POLICY.publisher_common_name,
            "Thumbprint": "AB" * 20,
            # The PowerShell side structurally identifies the RFC3161 OID;
            # SignTool separately validates the Windows/TSA chain.
            "TimestampStatus": "Valid",
            "TimestampProtocol": "RFC3161",
            "TimestampCommonName": "Trusted RFC3161 TSA",
            "TimestampThumbprint": "CD" * 20,
        }
        payload.update(overrides)
        return payload

    def test_accepts_valid_signature_publisher_and_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed")
            identity = self._verify(artifact, self._valid_payload())
            self.assertEqual(identity.thumbprint, "AB" * 20)
            self.assertEqual(identity.timestamp_common_name, "Trusted RFC3161 TSA")
            self.assertEqual(identity.timestamp_thumbprint, "CD" * 20)

    def test_rejects_missing_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed-without-timestamp")
            with self.assertRaises(SignatureError):
                self._verify(artifact, self._valid_payload(
                    TimestampStatus="Missing",
                    TimestampProtocol="Missing",
                    TimestampCommonName="",
                    TimestampThumbprint="",
                ))

    def test_rejects_invalid_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed-with-invalid-timestamp")
            with self.assertRaises(SignatureError):
                self._verify(artifact, self._valid_payload(
                    TimestampStatus="Invalid",
                    TimestampProtocol="Invalid",
                ))

    def test_rejects_legacy_authenticode_countersignature(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed-with-legacy-timestamp")
            with self.assertRaises(SignatureError):
                self._verify(artifact, self._valid_payload(
                    TimestampStatus="Invalid",
                    TimestampProtocol="Legacy",
                ))

    def test_rejects_malformed_rfc3161_attribute(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed-with-malformed-rfc3161")
            with self.assertRaises(SignatureError):
                self._verify(artifact, self._valid_payload(
                    TimestampStatus="Invalid",
                    TimestampProtocol="Invalid",
                ))

    def test_rejects_untrusted_or_invalid_timestamp_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed-with-untrusted-tsa")
            invalid_tsa = SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=(
                    "SignTool Error: A certificate chain processed, but "
                    "terminated in an untrusted root."
                ),
            )
            with self.assertRaises(SignatureError):
                self._verify(artifact, self._valid_payload(),
                             signtool_result=invalid_tsa)

    def test_rejects_signtool_warning_even_with_valid_structural_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed-with-timestamp-warning")
            warning = SimpleNamespace(
                returncode=2,
                stdout="Signature verification completed with warnings",
                stderr="",
            )
            with self.assertRaises(SignatureError):
                self._verify(artifact, self._valid_payload(),
                             signtool_result=warning)

    def test_ignores_undocumented_signtool_stdout_when_exit_status_is_success(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed-with-rfc3161-timestamp")
            identity = self._verify(
                artifact,
                self._valid_payload(),
                signtool_result=SimpleNamespace(
                    returncode=0,
                    stdout="any future SignTool diagnostic format",
                    stderr="",
                ),
            )
            self.assertEqual(identity.timestamp_common_name, "Trusted RFC3161 TSA")

    def test_rejects_unavailable_signtool(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed-with-rfc3161-timestamp")
            powershell_result = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(self._valid_payload()),
                stderr="",
            )
            runner = Mock(side_effect=[powershell_result, OSError("not found")])
            with self.assertRaises(SignatureError):
                verify_authenticode(
                    artifact,
                    POLICY.publisher_common_name,
                    require_timestamp=True,
                    runner=runner,
                    powershell="powershell.exe",
                    signtool="signtool.exe",
                )

    @unittest.skipUnless(
        sys.platform == "win32",
        "requires Windows CryptoAPI and Authenticode cmdlets",
    )
    def test_windows_inspector_reads_real_embedded_pkcs7_without_timestamp(self):
        """Exercise CryptoAPI against a real temporary Authenticode fixture."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "clarifyvoice-inspector-fixture.ps1"
            fixture.write_text("Write-Output signed-fixture\n", encoding="utf-16")
            command = (
                "$ErrorActionPreference='Stop';"
                "$env:PSModulePath=(($env:WINDIR + "
                "'\\System32\\WindowsPowerShell\\v1.0\\Modules') + ';' + "
                "($env:ProgramFiles + '\\WindowsPowerShell\\Modules'));"
                "$source=@'\n"
                + _AUTHENTICODE_TIMESTAMP_INSPECTOR
                + "\n'@;"
                "Add-Type -TypeDefinition $source -Language CSharp;"
                "$setSignatureCommand=Get-Command "
                "Set-AuthenticodeSignature -ErrorAction Stop;"
                "$newCertificateCommand=Get-Command "
                "New-SelfSignedCertificate -ErrorAction Stop;"
                "if(-not (Get-PSDrive -Name Cert -ErrorAction SilentlyContinue)){"
                "throw 'Certificate provider is unavailable'};"
                "$fixture=$env:CLARIFYVOICE_INSPECTOR_FIXTURE;"
                "$certificate=$null;"
                "try{"
                "$certificate=& $newCertificateCommand.Name "
                "-Subject 'CN=ClarifyVoice Inspector Fixture' "
                "-Type CodeSigningCert -CertStoreLocation Cert:\\CurrentUser\\My;"
                "$signed=& $setSignatureCommand.Name -LiteralPath $fixture "
                "-Certificate $certificate;"
                "if($signed.Status -eq 'NotSigned'){throw 'fixture was not signed'};"
                "$protocol=[ClarifyVoiceTimestampInspector]::"
                "GetTimestampProtocol($fixture);"
                "if($protocol -cne 'Missing'){throw "
                "('unexpected protocol: ' + $protocol)};"
                "Write-Output $protocol;"
                "}finally{"
                "if($certificate){Remove-Item -LiteralPath "
                "('Cert:\\CurrentUser\\My\\' + $certificate.Thumbprint) "
                "-ErrorAction SilentlyContinue};"
                "Remove-Item -LiteralPath $fixture -Force -ErrorAction SilentlyContinue"
                "}"
            )
            environment = os.environ.copy()
            environment["CLARIFYVOICE_INSPECTOR_FIXTURE"] = str(fixture)
            # PowerShell 7 is the default GitHub Actions shell, but the
            # Authenticode cmdlets used by this fixture are provided by the
            # Windows PowerShell 5.1 Security module.  Invoke that binary
            # explicitly while retaining -NoProfile so module autoloading is
            # exercised deterministically.
            powershell_executable = str(
                Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            result = subprocess.run(
                [
                    powershell_executable,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "Missing")

    def test_rejects_invalid_timestamp_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed-with-invalid-timestamp-certificate")
            with self.assertRaises(SignatureError):
                self._verify(artifact, self._valid_payload(
                    TimestampThumbprint="not-a-thumbprint",
                ))

    def test_rejects_mismatched_authenticode_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed-by-wrong-publisher")
            with self.assertRaises(SignatureError):
                self._verify(artifact, self._valid_payload(
                    CommonName="Someone Else",
                ))

    def test_rejects_invalid_primary_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"unsigned")
            with self.assertRaises(SignatureError):
                self._verify(artifact, self._valid_payload(
                    Status="NotSigned",
                    StatusMessage="Unsigned",
                    CommonName="",
                    Thumbprint="",
                    TimestampStatus="Missing",
                    TimestampProtocol="Missing",
                    TimestampCommonName="",
                    TimestampThumbprint="",
                ))


class PrepareUpdateTests(unittest.TestCase):
    def test_verifies_container_before_manifest_and_installer_after_download(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            calls = []

            def downloader(url, destination, **kwargs):
                calls.append(("download", url, destination.name, kwargs))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"cab" if destination.suffix == ".cab" else b"msi")
                return destination

            def verifier(path, publisher, *, require_timestamp):
                calls.append(("verify", path.name, publisher))
                self.assertTrue(require_timestamp)
                return SignatureIdentity(
                    publisher, "AB" * 20, "Trusted TSA", "CD" * 20)

            def extractor(cab, output):
                calls.append(("extract", cab.name))
                output.mkdir(parents=True)
                manifest = output / "release-manifest.json"
                manifest.write_bytes(manifest_bytes())
                return manifest

            prepared = prepare_update(
                "0.1.2",
                cache,
                policy=POLICY,
                signature_verifier=verifier,
                manifest_extractor=extractor,
                downloader=downloader,
            )
            self.assertEqual(prepared.manifest.version, "0.2.0")
            self.assertEqual(prepared.installer_path.read_bytes(), b"msi")
            self.assertEqual(
                prepared.installer_path,
                cache / "0.2.0" / POLICY.installer_asset,
            )
            self.assertEqual(list(cache.glob(".update-staging-*")), [])
            self.assertEqual([call[0] for call in calls], [
                "download", "verify", "extract", "download", "verify"])

    def test_current_version_does_not_download_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            downloads = []

            def downloader(url, destination, **kwargs):
                downloads.append(destination.name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"cab")
                return destination

            def extractor(cab, output):
                del cab
                output.mkdir(parents=True)
                manifest = output / "release-manifest.json"
                manifest.write_bytes(manifest_bytes("0.1.2"))
                return manifest

            prepared = prepare_update(
                "0.1.2", cache, policy=POLICY,
                signature_verifier=lambda path, publisher, require_timestamp: (
                    SignatureIdentity(
                        publisher, "AB" * 20, "Trusted TSA", "CD" * 20)),
                manifest_extractor=extractor,
                downloader=downloader,
            )
            self.assertIsNone(prepared)
            self.assertEqual(downloads, [POLICY.manifest_asset])
            self.assertEqual(list(cache.rglob(POLICY.manifest_asset)), [])
            self.assertEqual(list(cache.glob(".update-staging-*")), [])

    def test_cab_verification_failure_cleans_staging_and_preserves_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            verified_installer = cache / "0.2.0" / POLICY.installer_asset
            verified_installer.parent.mkdir(parents=True)
            verified_installer.write_bytes(b"previously-verified")

            def downloader(url, destination, **kwargs):
                del url, kwargs
                destination.write_bytes(b"untrusted-cab")
                return destination

            def reject_signature(path, publisher, *, require_timestamp):
                del path, publisher, require_timestamp
                raise SignatureError("publisher mismatch")

            with self.assertRaises(SignatureError):
                prepare_update(
                    "0.1.2", cache, policy=POLICY,
                    signature_verifier=reject_signature,
                    downloader=downloader,
                )
            self.assertEqual(verified_installer.read_bytes(), b"previously-verified")
            self.assertEqual(list(cache.rglob(POLICY.manifest_asset)), [])
            self.assertEqual(list(cache.glob(".update-staging-*")), [])

    def test_manifest_extraction_or_parse_failure_cleans_staged_cab(self):
        for failure in ("extract", "parse"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                cache = Path(directory)

                def downloader(url, destination, **kwargs):
                    del url, kwargs
                    destination.write_bytes(b"signed-cab")
                    return destination

                def verifier(path, publisher, *, require_timestamp):
                    del path, require_timestamp
                    return SignatureIdentity(
                        publisher, "AB" * 20, "Trusted TSA", "CD" * 20)

                def extractor(cab, output):
                    del cab
                    if failure == "extract":
                        raise ManifestError("cannot extract")
                    output.mkdir(parents=True)
                    manifest = output / "release-manifest.json"
                    manifest.write_bytes(b"not-json")
                    return manifest

                with self.assertRaises(ManifestError):
                    prepare_update(
                        "0.1.2", cache, policy=POLICY,
                        signature_verifier=verifier,
                        manifest_extractor=extractor,
                        downloader=downloader,
                    )
                self.assertEqual(list(cache.rglob(POLICY.manifest_asset)), [])
                self.assertEqual(list(cache.glob(".update-staging-*")), [])

    def test_msi_signature_failure_preserves_previous_verified_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            verified_installer = cache / "0.2.0" / POLICY.installer_asset
            verified_installer.parent.mkdir(parents=True)
            verified_installer.write_bytes(b"previously-verified")

            def downloader(url, destination, **kwargs):
                del url, kwargs
                destination.write_bytes(
                    b"cab" if destination.suffix == ".cab" else b"untrusted-msi")
                return destination

            def verifier(path, publisher, *, require_timestamp):
                del require_timestamp
                if path.suffix == ".msi":
                    raise SignatureError("publisher mismatch")
                return SignatureIdentity(
                    publisher, "AB" * 20, "Trusted TSA", "CD" * 20)

            def extractor(cab, output):
                del cab
                output.mkdir(parents=True)
                manifest = output / "release-manifest.json"
                manifest.write_bytes(manifest_bytes())
                return manifest

            with self.assertRaises(SignatureError):
                prepare_update(
                    "0.1.2", cache, policy=POLICY,
                    signature_verifier=verifier,
                    manifest_extractor=extractor,
                    downloader=downloader,
                )
            self.assertEqual(verified_installer.read_bytes(), b"previously-verified")
            self.assertEqual(list(cache.rglob("*.msi")), [verified_installer])
            self.assertEqual(list(cache.glob(".update-staging-*")), [])


class ReleaseManifestScriptTests(unittest.TestCase):
    def test_generated_manifest_round_trips_through_strict_parser(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            installer = Path(directory) / POLICY.installer_asset
            output = Path(directory) / "release-manifest.json"
            installer.write_bytes(b"msi")
            result = subprocess.run([
                sys.executable,
                str(root / "scripts" / "create_release_manifest.py"),
                "--version", "0.2.0",
                "--installer", str(installer),
                "--output", str(output),
                "--policy", str(root / "distribution" / "update-policy.json"),
            ], check=False, capture_output=True, text=True, cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = parse_release_manifest(output.read_bytes(), POLICY)
            self.assertEqual(parsed.asset.sha256, hashlib.sha256(b"msi").hexdigest())


class LaunchPreparedUpdateTests(unittest.TestCase):
    def test_revalidates_bytes_and_signature_immediately_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = Path(directory) / POLICY.installer_asset
            installer.write_bytes(b"msi")
            manifest = parse_release_manifest(manifest_bytes(
                sha256=hashlib.sha256(b"msi").hexdigest()), POLICY)
            prepared = PreparedUpdate(
                manifest=manifest,
                installer_path=installer,
                signer=SignatureIdentity(
                    POLICY.publisher_common_name,
                    "AB" * 20,
                    "Trusted TSA",
                    "CD" * 20,
                ),
            )
            verifier = Mock(return_value=prepared.signer)
            runner = Mock()
            with patch("update_security.platform.system", return_value="Windows"):
                launch_prepared_update(
                    prepared, signature_verifier=verifier, runner=runner)
            verifier.assert_called_once_with(
                installer,
                POLICY.publisher_common_name,
                require_timestamp=True,
            )
            runner.assert_called_once()

            installer.write_bytes(b"bad")
            with patch("update_security.platform.system", return_value="Windows"):
                with self.assertRaises(IntegrityError):
                    launch_prepared_update(
                        prepared, signature_verifier=verifier, runner=runner)


if __name__ == "__main__":
    unittest.main()
