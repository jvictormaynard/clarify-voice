import hashlib
import json
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
    def test_requires_valid_signature_and_exact_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.msi"
            artifact.write_bytes(b"signed")
            runner = Mock(return_value=SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "Status": "Valid",
                    "StatusMessage": "Signature verified.",
                    "CommonName": POLICY.publisher_common_name,
                    "Thumbprint": "AB" * 20,
                }),
                stderr="",
            ))
            identity = verify_authenticode(
                artifact, POLICY.publisher_common_name,
                runner=runner, powershell="powershell.exe")
            self.assertEqual(identity.thumbprint, "AB" * 20)

            runner.return_value = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "Status": "NotSigned", "StatusMessage": "Unsigned",
                    "CommonName": "", "Thumbprint": "",
                }),
                stderr="",
            )
            with self.assertRaises(SignatureError):
                verify_authenticode(
                    artifact, POLICY.publisher_common_name,
                    runner=runner, powershell="powershell.exe")


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

            def verifier(path, publisher):
                calls.append(("verify", path.name, publisher))
                return SignatureIdentity(publisher, "AB" * 20)

            def extractor(cab, output):
                calls.append(("extract", cab.name))
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
                manifest = output / "release-manifest.json"
                manifest.write_bytes(manifest_bytes("0.1.2"))
                return manifest

            prepared = prepare_update(
                "0.1.2", cache, policy=POLICY,
                signature_verifier=lambda path, publisher: SignatureIdentity(
                    publisher, "AB" * 20),
                manifest_extractor=extractor,
                downloader=downloader,
            )
            self.assertIsNone(prepared)
            self.assertEqual(downloads, [POLICY.manifest_asset])


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
                signer=SignatureIdentity(POLICY.publisher_common_name, "AB" * 20),
            )
            verifier = Mock(return_value=prepared.signer)
            runner = Mock()
            with patch("update_security.platform.system", return_value="Windows"):
                launch_prepared_update(
                    prepared, signature_verifier=verifier, runner=runner)
            verifier.assert_called_once_with(installer, POLICY.publisher_common_name)
            runner.assert_called_once()

            installer.write_bytes(b"bad")
            with patch("update_security.platform.system", return_value="Windows"):
                with self.assertRaises(IntegrityError):
                    launch_prepared_update(
                        prepared, signature_verifier=verifier, runner=runner)


if __name__ == "__main__":
    unittest.main()
