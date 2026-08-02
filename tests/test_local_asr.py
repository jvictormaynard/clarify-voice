import ctypes
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import local_asr
from scripts import local_asr_harness
from provider_registry import ProviderRegistry, build_provider_registry
from provider_types import (
    ProviderCapability,
    ProviderConfigurationError,
    ProviderConnection,
    ProviderResponseError,
    TranscriptionRequest,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, *, content=b"", payload=None, status_code=200):
        self.content = content
        self._payload = payload or {}
        self.status_code = status_code

    def iter_content(self, chunk_size):
        for index in range(0, len(self.content), max(1, chunk_size // 2)):
            yield self.content[index:index + max(1, chunk_size // 2)]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class InstallerSession:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(content=self.responses[url])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def make_zip(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


class InstallerFixture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.runtime = b"verified-sidecar"
        self.model = b"verified-model"
        self.archive = make_zip({
            "Release/whisper-server.exe": self.runtime,
            "../../outside.exe": b"must-not-extract",
        })
        self.manifest = {
            "schema_version": 1,
            "provider": "local_asr",
            "engine": {
                "name": "whisper.cpp",
                "version": "v-test",
                "source_url": "https://example.test/source",
                "license": "MIT",
            },
            "recommended_model": {
                "id": "ggml-test",
                "display_name": "Test",
                "source_url": "https://example.test/model-source",
                "license": "MIT",
            },
            "requirements": {
                "platform": "Windows x64",
                "compute": "CPU only",
                "memory_bytes": 1,
                "disk_bytes": 1,
                "download_bytes": len(self.archive) + len(self.model),
            },
            "license_files": ["licenses/whisper.cpp-MIT.txt"],
            "assets": {
                "runtime": {
                    "filename": "runtime.zip",
                    "url": "https://example.test/runtime.zip",
                    "size": len(self.archive),
                    "sha256": digest(self.archive),
                    "license": "MIT",
                    "source_url": "https://example.test/runtime-source",
                    "archive": "zip",
                },
                "model": {
                    "filename": "model.bin",
                    "url": "https://example.test/model.bin",
                    "size": len(self.model),
                    "sha256": digest(self.model),
                    "license": "MIT",
                    "source_url": "https://example.test/model-source",
                },
            },
            "extracted_files": [{
                "archive_path": "Release/whisper-server.exe",
                "path": "runtime/whisper-server.exe",
                "size": len(self.runtime),
                "sha256": digest(self.runtime),
            }],
        }
        self.manifest_path = self.directory / "manifest.json"
        self.manifest_path.write_text(
            json.dumps(self.manifest), encoding="utf-8")
        self.root = self.directory / "assets"

    def installer(self, responses=None):
        responses = responses or {
            self.manifest["assets"]["runtime"]["url"]: self.archive,
            self.manifest["assets"]["model"]["url"]: self.model,
        }
        session = InstallerSession(responses)
        return local_asr.LocalASRInstaller(
            root=self.root, manifest_path=self.manifest_path, session=session), session


class LocalASRManifestTests(unittest.TestCase):
    def test_published_manifest_pins_real_assets_and_licenses(self):
        manifest = local_asr.load_manifest(ROOT / "local_asr_manifest.json")

        self.assertEqual(manifest["engine"]["name"], "whisper.cpp")
        self.assertEqual(manifest["engine"]["version"], "v1.9.1")
        self.assertEqual(manifest["engine"]["license"], "MIT")
        self.assertEqual(manifest["recommended_model"]["id"], "ggml-small")
        self.assertEqual(manifest["recommended_model"]["license"], "MIT")
        self.assertEqual(
            manifest["assets"]["runtime"]["sha256"],
            "7d8be46ecd31828e1eb7a2ecdd0d6b314feafd82163038ab6092594b0a063539",
        )
        self.assertEqual(manifest["assets"]["runtime"]["size"], 7982101)
        self.assertEqual(
            manifest["assets"]["model"]["sha256"],
            "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
        )
        self.assertEqual(manifest["assets"]["model"]["size"], 487601967)
        self.assertIn(
            "80da2d8bfee42b0e836fc3a9890373e5defc00a6",
            manifest["assets"]["model"]["url"],
        )
        self.assertGreaterEqual(len(manifest["extracted_files"]), 10)
        for relative in manifest["license_files"]:
            self.assertTrue((ROOT / relative).is_file())

    def test_manifest_rejects_unsafe_destination(self):
        for destination in ("../escape.exe", r"..\\escape.exe"):
            with self.subTest(destination=destination), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = InstallerFixture(directory)
                fixture.manifest["extracted_files"][0]["path"] = destination
                fixture.manifest_path.write_text(
                    json.dumps(fixture.manifest), encoding="utf-8")

                with self.assertRaises(local_asr.LocalASRIntegrityError):
                    local_asr.load_manifest(fixture.manifest_path)

    def test_manifest_rejects_unsafe_asset_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            fixture.manifest["assets"]["model"]["filename"] = "../model.bin"
            fixture.manifest_path.write_text(
                json.dumps(fixture.manifest), encoding="utf-8")

            with self.assertRaises(local_asr.LocalASRIntegrityError):
                local_asr.load_manifest(fixture.manifest_path)

    def test_manifest_rejects_unsafe_installation_components(self):
        cases = (
            ("engine", "name", "../victim"),
            ("engine", "version", "/absolute"),
            ("recommended_model", "id", "nested/model"),
            ("recommended_model", "id", r"C:\\victim"),
        )
        for section, field, value in cases:
            with self.subTest(section=section, field=field, value=value), \
                    tempfile.TemporaryDirectory() as directory:
                fixture = InstallerFixture(directory)
                fixture.manifest[section][field] = value
                fixture.manifest_path.write_text(
                    json.dumps(fixture.manifest), encoding="utf-8")

                with self.assertRaises(local_asr.LocalASRIntegrityError):
                    local_asr.load_manifest(fixture.manifest_path)

    def test_manifest_rejects_invalid_extracted_file_size(self):
        for size in (0, -1, "not-an-integer"):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                fixture = InstallerFixture(directory)
                fixture.manifest["extracted_files"][0]["size"] = size
                fixture.manifest_path.write_text(
                    json.dumps(fixture.manifest), encoding="utf-8")

                with self.assertRaises(local_asr.LocalASRIntegrityError):
                    local_asr.load_manifest(fixture.manifest_path)

    def test_repository_contains_no_sidecar_binary_or_model(self):
        names = [path.name.casefold() for path in ROOT.rglob("*") if path.is_file()]

        self.assertNotIn("whisper-server.exe", names)
        self.assertFalse(any(name.startswith("ggml-") and name.endswith(".bin")
                             for name in names))
        self.assertNotIn("whisper.cpp", (ROOT / "requirements.txt").read_text(
            encoding="utf-8").casefold())


class LocalASRInstallerTests(unittest.TestCase):
    def test_status_is_read_only_and_never_uses_network(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, session = fixture.installer()

            status = installer.status()

            self.assertEqual(status["state"], "not_installed")
            self.assertEqual(session.calls, [])
            self.assertFalse(fixture.root.exists())

    def test_explicit_install_verifies_downloads_and_allowlisted_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, session = fixture.installer()
            progress = []

            status = installer.install(lambda *values: progress.append(values))

            self.assertEqual(status["state"], "installed")
            self.assertEqual(len(session.calls), 2)
            self.assertEqual(installer.executable_path.read_bytes(), fixture.runtime)
            self.assertEqual(installer.model_path.read_bytes(), fixture.model)
            self.assertTrue(
                (installer.install_dir / "licenses" / "whisper.cpp-MIT.txt").is_file())
            self.assertFalse((fixture.root / "outside.exe").exists())
            self.assertFalse((Path(directory) / "outside.exe").exists())
            self.assertTrue(any(stage == "verify:model" for stage, _, _ in progress))
            self.assertEqual(progress[-1], ("complete", 1, 1))

    def test_digest_mismatch_leaves_no_runnable_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            responses = {
                fixture.manifest["assets"]["runtime"]["url"]: fixture.archive,
                fixture.manifest["assets"]["model"]["url"]: b"wrong-model!!",
            }
            installer, _session = fixture.installer(responses)

            with self.assertRaises(local_asr.LocalASRIntegrityError):
                installer.install()

            self.assertFalse(installer.install_dir.exists())
            self.assertFalse(installer.executable_path.exists())
            self.assertFalse(list(fixture.root.glob(".install-*")))

    def test_installed_file_tampering_blocks_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, _session = fixture.installer()
            installer.install()
            installer.executable_path.write_bytes(b"tampered-sidecar")

            with self.assertRaises(local_asr.LocalASRIntegrityError):
                installer.verify()

            self.assertEqual(installer.status()["state"], "invalid")

    def test_unreadable_installed_hash_is_an_invalid_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, _session = fixture.installer()
            installer.install()
            original_sha256 = local_asr._sha256

            def unreadable_hash(path, *args, **kwargs):
                if Path(path) == installer.model_path:
                    raise PermissionError("private ACL detail")
                return original_sha256(path, *args, **kwargs)

            with patch.object(local_asr, "_sha256", side_effect=unreadable_hash):
                with self.assertRaises(local_asr.LocalASRIntegrityError):
                    installer.verify()
                status = installer.status()

            self.assertEqual(status["state"], "invalid")
            self.assertIn("model.bin", status["detail"])
            self.assertIn("reinstall", status["detail"].casefold())
            self.assertNotIn("private ACL detail", status["detail"])

    def test_installed_stat_error_is_an_invalid_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, _session = fixture.installer()
            installer.install()
            original_stat = Path.stat
            executable_path = installer.executable_path

            def unreadable_stat(path, *args, **kwargs):
                if Path(path) == executable_path:
                    raise OSError("private filesystem detail")
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", new=unreadable_stat):
                status = installer.status()

            self.assertEqual(status["state"], "invalid")
            self.assertIn("whisper-server.exe", status["detail"])
            self.assertIn("reinstall", status["detail"].casefold())
            self.assertNotIn("private filesystem detail", status["detail"])

    def test_missing_installed_file_remains_not_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, _session = fixture.installer()
            installer.install()
            installer.model_path.unlink()

            status = installer.status()

            self.assertEqual(status["state"], "not_installed")
            self.assertIn("missing model.bin", status["detail"])

    def test_remove_deletes_every_asset_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, _session = fixture.installer()
            installer.install()

            self.assertTrue(installer.remove())

            self.assertFalse(fixture.root.exists())
            self.assertFalse(installer.executable_path.exists())
            self.assertFalse(installer.model_path.exists())

    def test_install_refuses_nonempty_unowned_custom_root(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            fixture.root.mkdir()
            unrelated = fixture.root / "keep.txt"
            unrelated.write_text("user data", encoding="utf-8")
            installer, session = fixture.installer()

            with self.assertRaises(local_asr.LocalASRError):
                installer.install()

            self.assertEqual(session.calls, [])
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "user data")

    def test_install_wraps_asset_root_creation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, session = fixture.installer()
            original_mkdir = Path.mkdir

            def fail_root_mkdir(path, *args, **kwargs):
                if Path(path) == fixture.root:
                    raise PermissionError("private ACL detail")
                return original_mkdir(path, *args, **kwargs)

            with patch.object(Path, "mkdir", new=fail_root_mkdir):
                with self.assertRaises(local_asr.LocalASRError) as raised:
                    installer.install()

            self.assertEqual(session.calls, [])
            self.assertIn("asset root", str(raised.exception))
            self.assertNotIn("private ACL detail", str(raised.exception))

    def test_install_wraps_asset_root_marker_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, session = fixture.installer()
            original_write_text = Path.write_text

            def fail_marker_write(path, *args, **kwargs):
                if Path(path) == fixture.root / local_asr.ROOT_MARKER:
                    raise OSError("private filesystem detail")
                return original_write_text(path, *args, **kwargs)

            with patch.object(Path, "write_text", new=fail_marker_write):
                with self.assertRaises(local_asr.LocalASRError) as raised:
                    installer.install()

            self.assertEqual(session.calls, [])
            self.assertIn("ownership", str(raised.exception))
            self.assertNotIn("private filesystem detail", str(raised.exception))

    def test_unowned_root_removal_preserves_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            fixture.root.mkdir()
            installer, _session = fixture.installer()
            installer.executable_path.parent.mkdir(parents=True)
            installer.executable_path.write_bytes(b"old owned runtime")
            unrelated = fixture.root / "keep.txt"
            unrelated.write_text("user data", encoding="utf-8")

            with self.assertRaises(local_asr.LocalASRError) as raised:
                installer.remove()

            self.assertIn("unowned", str(raised.exception))
            self.assertTrue(installer.install_dir.exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "user data")

    def test_install_wraps_publication_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, session = fixture.installer()
            original_replace = local_asr.os.replace

            def fail_publish(source, destination):
                if Path(destination) == installer.install_dir:
                    raise OSError("private sharing detail")
                return original_replace(source, destination)

            with patch.object(local_asr.os, "replace", new=fail_publish):
                with self.assertRaises(local_asr.LocalASRError) as raised:
                    installer.install()

            self.assertEqual(len(session.calls), 2)
            self.assertIn("publish", str(raised.exception))
            self.assertNotIn("private sharing detail", str(raised.exception))
            self.assertFalse(installer.install_dir.exists())

    def test_install_rejects_mutated_traversal_component_before_deleting(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, session = fixture.installer()
            victim = Path(directory) / "victim-v-test-ggml-test"
            victim.mkdir()
            keep = victim / "keep.txt"
            keep.write_text("external data", encoding="utf-8")
            installer.manifest["engine"]["name"] = "../victim"

            with self.assertRaises(local_asr.LocalASRIntegrityError):
                installer.install()

            self.assertEqual(session.calls, [])
            self.assertEqual(keep.read_text(encoding="utf-8"), "external data")
            self.assertFalse(fixture.root.exists())

    def test_remove_rejects_mutated_absolute_component_before_deleting(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            installer, _session = fixture.installer()
            fixture.root.mkdir()
            (fixture.root / local_asr.ROOT_MARKER).write_text(
                f"{local_asr.PROVIDER_ID}\n", encoding="utf-8")
            outside_prefix = Path(directory) / "external"
            victim = Path(f"{outside_prefix}-v-test-ggml-test")
            victim.mkdir()
            keep = victim / "keep.txt"
            keep.write_text("external data", encoding="utf-8")
            installer.manifest["engine"]["name"] = str(outside_prefix)

            with self.assertRaises(local_asr.LocalASRIntegrityError):
                installer.remove()

            self.assertEqual(keep.read_text(encoding="utf-8"), "external data")
            self.assertTrue(fixture.root.exists())


class FakeProcess:
    next_pid = 12000

    def __init__(self, terminated_event=None):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.returncode = None
        self.alive = True
        self.terminated_event = terminated_event
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return None if self.alive else self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.alive = False
        self.returncode = 0
        if self.terminated_event:
            self.terminated_event.set()

    def kill(self):
        self.kill_calls += 1
        self.alive = False
        self.returncode = -9
        if self.terminated_event:
            self.terminated_event.set()

    def wait(self, timeout=None):
        return self.returncode


class PopenFactory:
    def __init__(self, terminated_event=None):
        self.calls = []
        self.processes = []
        self.terminated_event = terminated_event

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        process = FakeProcess(self.terminated_event)
        self.processes.append(process)
        return process


class SidecarSession:
    def __init__(self, post_results=None):
        self.trust_env = True
        self.get_calls = []
        self.post_calls = []
        self.post_results = list(post_results or [FakeResponse(payload={"text": "local text"})])

    def get(self, url, timeout=None):
        self.get_calls.append((url, timeout))
        return FakeResponse(payload={"status": "ok"})

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        result = self.post_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result()
        return result


class FakeInstaller:
    def __init__(self, root):
        self.root = Path(root)
        self.install_dir = self.root / "installed"
        self.executable_path = self.install_dir / "runtime" / "whisper-server.exe"
        self.model_path = self.install_dir / "models" / "model.bin"
        self.process_record_path = self.root / "sidecar-process.json"
        self.verify_calls = 0
        self.executable_path.parent.mkdir(parents=True)
        self.model_path.parent.mkdir(parents=True)
        self.executable_path.write_bytes(b"exe")
        self.model_path.write_bytes(b"model")

    def verify(self, cancel_check=None):
        self.verify_calls += 1
        if cancel_check is not None and cancel_check():
            raise local_asr.LocalASRCancelledError("cancelled during verification")
        return self.install_dir


class LocalASRSidecarTests(unittest.TestCase):
    def _manager(self, directory, session=None, factory=None, **kwargs):
        installer = FakeInstaller(directory)
        session = session or SidecarSession()
        factory = factory or PopenFactory()
        manager = local_asr.LocalASRSidecarManager(
            installer,
            session=session,
            popen_factory=factory,
            elevation_checker=kwargs.get("elevation_checker", lambda: False),
            startup_timeout=kwargs.get("startup_timeout", 0.5),
            request_timeout=kwargs.get("request_timeout", 0.5),
            idle_seconds=kwargs.get("idle_seconds", 0),
        )
        return manager, installer, session, factory

    def test_manager_conforms_to_narrow_backend_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, *_ = self._manager(directory)
            self.assertIsInstance(manager, local_asr.LocalTranscriptionBackend)

    def test_start_verifies_then_uses_loopback_secret_and_cpu_only_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, installer, session, factory = self._manager(directory)

            manager.start()

            command, options = factory.calls[0]
            self.assertEqual(installer.verify_calls, 1)
            self.assertIn("--host", command)
            self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
            self.assertIn("--request-path", command)
            request_path = command[command.index("--request-path") + 1]
            self.assertTrue(request_path.startswith("/"))
            self.assertGreater(len(request_path), 20)
            self.assertIn("--no-gpu", command)
            self.assertIn("--no-context", command)
            self.assertEqual(options["stdin"], local_asr.subprocess.DEVNULL)
            self.assertFalse(session.trust_env)
            self.assertTrue(session.get_calls[0][0].endswith("/health"))
            manager.shutdown()

    def test_start_records_process_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, installer, _session, _factory = self._manager(directory)
            record_process = Mock(wraps=manager._record_process)
            manager._record_process = record_process

            manager.start()

            record_process.assert_called_once_with()
            self.assertTrue(installer.process_record_path.is_file())
            manager.shutdown()

    def test_windows_start_refuses_elevated_or_unknown_privileges(self):
        for elevation in (True, None):
            with self.subTest(elevation=elevation), \
                    tempfile.TemporaryDirectory() as directory:
                manager, installer, _session, factory = self._manager(
                    directory,
                    elevation_checker=lambda elevation=elevation: elevation,
                )

                with patch.object(
                        local_asr.platform, "system", return_value="Windows"):
                    with self.assertRaises(local_asr.LocalASRSidecarError):
                        manager.start()

                self.assertEqual(installer.verify_calls, 1)
                self.assertEqual(factory.calls, [])
                self.assertFalse(installer.process_record_path.exists())

    def test_unelevated_windows_start_uses_no_window_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, _installer, _session, factory = self._manager(directory)

            with patch.object(
                    local_asr.platform, "system", return_value="Windows"):
                manager.start()

            self.assertEqual(factory.calls[0][1]["creationflags"], 0x08000000)
            manager.shutdown()

    def test_transcribe_posts_only_to_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, _installer, session, _factory = self._manager(directory)
            audio = Path(directory) / "input.wav"
            audio.write_bytes(b"RIFF-audio")

            text = manager.transcribe(audio, "pt")

            self.assertEqual(text, "local text")
            url, options = session.post_calls[0]
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            self.assertTrue(url.endswith("/inference"))
            self.assertEqual(options["data"]["language"], "pt")
            self.assertEqual(options["files"]["file"][1], b"RIFF-audio")
            manager.shutdown()

    def test_transcribe_uses_session_snapshot_without_owning_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, _installer, session, _factory = self._manager(directory)
            released_path = Path(directory) / "recording-session.wav"
            snapshot = b"RIFF-session-snapshot"

            text = manager.transcribe(
                released_path,
                "en",
                audio_bytes=snapshot,
            )

            self.assertEqual(text, "local text")
            self.assertFalse(released_path.exists())
            file_part = session.post_calls[0][1]["files"]["file"]
            self.assertEqual(file_part, (
                "recording-session.wav", snapshot, "audio/wav"))
            manager.shutdown()

    def test_request_failure_restarts_sidecar_once(self):
        with tempfile.TemporaryDirectory() as directory:
            session = SidecarSession([
                ConnectionError("sidecar crashed"),
                FakeResponse(payload={"text": "recovered"}),
            ])
            manager, installer, _session, factory = self._manager(
                directory, session=session)
            audio = Path(directory) / "input.wav"
            audio.write_bytes(b"RIFF-audio")

            text = manager.transcribe(audio)

            self.assertEqual(text, "recovered")
            self.assertEqual(len(factory.processes), 2)
            self.assertEqual(installer.verify_calls, 2)
            self.assertEqual(factory.processes[0].terminate_calls, 1)
            manager.shutdown()

    def test_concurrent_requests_are_serialized_across_failure_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            first_request_started = threading.Event()
            release_first_request = threading.Event()

            def fail_first_request():
                first_request_started.set()
                release_first_request.wait(2)
                raise ConnectionError("first request failed")

            session = SidecarSession([
                fail_first_request,
                FakeResponse(payload={"text": "first recovered"}),
                FakeResponse(payload={"text": "second completed"}),
            ])
            manager, _installer, _session, factory = self._manager(
                directory, session=session)
            first_audio = Path(directory) / "first.wav"
            second_audio = Path(directory) / "second.wav"
            first_audio.write_bytes(b"RIFF-first")
            second_audio.write_bytes(b"RIFF-second")
            results = {}
            errors = {}

            def transcribe(name, audio):
                try:
                    results[name] = manager.transcribe(audio)
                except Exception as error:
                    errors[name] = error

            first = threading.Thread(
                target=transcribe, args=("first", first_audio))
            second = threading.Thread(
                target=transcribe, args=("second", second_audio))
            first.start()
            self.assertTrue(first_request_started.wait(0.5))
            second.start()
            time.sleep(0.1)

            self.assertEqual(len(session.post_calls), 1)
            release_first_request.set()
            first.join(1)
            second.join(1)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, {})
            self.assertEqual(results, {
                "first": "first recovered",
                "second": "second completed",
            })
            self.assertEqual(len(factory.processes), 2)
            self.assertEqual(factory.processes[0].terminate_calls, 1)
            self.assertEqual(factory.processes[1].terminate_calls, 0)
            manager.shutdown()

    def test_per_call_cancel_does_not_cancel_queued_request(self):
        with tempfile.TemporaryDirectory() as directory:
            first_request_started = threading.Event()
            terminated = threading.Event()

            def block_first_request():
                first_request_started.set()
                terminated.wait(2)
                raise ConnectionError("cancelled sidecar stopped")

            session = SidecarSession([
                block_first_request,
                FakeResponse(payload={"text": "queued completed"}),
            ])
            factory = PopenFactory(terminated)
            manager, _installer, _session, _factory = self._manager(
                directory, session=session, factory=factory)
            first_audio = Path(directory) / "first.wav"
            second_audio = Path(directory) / "second.wav"
            first_audio.write_bytes(b"RIFF-first")
            second_audio.write_bytes(b"RIFF-second")
            first_cancel = threading.Event()
            results = {}
            errors = {}

            def first_transcription():
                try:
                    results["first"] = manager.transcribe(
                        first_audio, cancel_event=first_cancel)
                except Exception as error:
                    errors["first"] = error

            def second_transcription():
                try:
                    results["second"] = manager.transcribe(second_audio)
                except Exception as error:
                    errors["second"] = error

            first = threading.Thread(target=first_transcription)
            second = threading.Thread(target=second_transcription)
            first.start()
            self.assertTrue(first_request_started.wait(0.5))
            second.start()
            time.sleep(0.1)

            self.assertEqual(len(session.post_calls), 1)
            first_cancel.set()
            first.join(1)
            second.join(1)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertIsInstance(
                errors.get("first"), local_asr.LocalASRCancelledError)
            self.assertNotIn("second", errors)
            self.assertEqual(results.get("second"), "queued completed")
            self.assertEqual(len(factory.processes), 2)
            self.assertEqual(factory.processes[0].terminate_calls, 1)
            self.assertEqual(factory.processes[1].terminate_calls, 0)
            manager.shutdown()

    def test_manager_cancel_cancels_active_and_queued_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            first_request_started = threading.Event()
            terminated = threading.Event()

            def block_first_request():
                first_request_started.set()
                terminated.wait(2)
                raise ConnectionError("manager stopped")

            session = SidecarSession([block_first_request])
            factory = PopenFactory(terminated)
            manager, _installer, _session, _factory = self._manager(
                directory, session=session, factory=factory)
            audio = Path(directory) / "input.wav"
            audio.write_bytes(b"RIFF-audio")
            errors = []

            def transcribe():
                try:
                    manager.transcribe(audio)
                except Exception as error:
                    errors.append(error)

            first = threading.Thread(target=transcribe)
            second = threading.Thread(target=transcribe)
            first.start()
            self.assertTrue(first_request_started.wait(0.5))
            second.start()
            time.sleep(0.1)

            manager.cancel()
            first.join(1)
            second.join(1)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(len(errors), 2)
            self.assertTrue(all(isinstance(
                error, local_asr.LocalASRCancelledError) for error in errors))
            self.assertEqual(len(factory.processes), 1)

    def test_cancellation_terminates_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            terminated = threading.Event()

            def blocking_post():
                terminated.wait(2)
                raise ConnectionError("server stopped")

            session = SidecarSession([blocking_post])
            factory = PopenFactory(terminated)
            manager, _installer, _session, _factory = self._manager(
                directory, session=session, factory=factory)
            audio = Path(directory) / "input.wav"
            audio.write_bytes(b"RIFF-audio")
            cancelled = threading.Event()
            threading.Timer(0.05, cancelled.set).start()

            with self.assertRaises(local_asr.LocalASRCancelledError):
                manager.transcribe(audio, cancel_event=cancelled)

            self.assertTrue(terminated.is_set())
            self.assertIsNone(manager.process_id)

    def test_cancel_during_request_does_not_retry_or_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            request_started = threading.Event()
            terminated = threading.Event()

            def blocking_post():
                request_started.set()
                terminated.wait(2)
                raise ConnectionError("server stopped")

            session = SidecarSession([blocking_post])
            factory = PopenFactory(terminated)
            manager, installer, _session, _factory = self._manager(
                directory, session=session, factory=factory)
            audio = Path(directory) / "input.wav"
            audio.write_bytes(b"RIFF-audio")
            errors = []

            def transcribe():
                try:
                    manager.transcribe(audio)
                except Exception as error:
                    errors.append(error)

            worker = threading.Thread(target=transcribe)
            worker.start()
            self.assertTrue(request_started.wait(0.5))

            manager.cancel()
            worker.join(0.75)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], local_asr.LocalASRCancelledError)
            self.assertEqual(len(factory.processes), 1)
            self.assertTrue(terminated.is_set())
            self.assertFalse(installer.process_record_path.exists())

    def test_cancel_during_health_startup_is_bounded_and_cleans_record(self):
        with tempfile.TemporaryDirectory() as directory:
            health_polled = threading.Event()

            class UnhealthySession(SidecarSession):
                def get(self, url, timeout=None):
                    self.get_calls.append((url, timeout))
                    health_polled.set()
                    return FakeResponse(status_code=503)

            session = UnhealthySession()
            manager, installer, _session, factory = self._manager(
                directory, session=session, startup_timeout=5)
            audio = Path(directory) / "input.wav"
            audio.write_bytes(b"RIFF-audio")
            cancelled = threading.Event()
            errors = []

            def transcribe():
                try:
                    manager.transcribe(audio, cancel_event=cancelled)
                except Exception as error:
                    errors.append(error)

            worker = threading.Thread(target=transcribe)
            worker.start()
            self.assertTrue(health_polled.wait(0.5))

            started = time.monotonic()
            cancelled.set()
            worker.join(0.75)

            self.assertFalse(worker.is_alive())
            self.assertLess(time.monotonic() - started, 0.75)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], local_asr.LocalASRCancelledError)
            self.assertEqual(factory.processes[0].terminate_calls, 1)
            self.assertFalse(installer.process_record_path.exists())

    def test_cancel_during_audio_snapshot_is_observed_before_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, installer, _session, factory = self._manager(directory)
            audio = Path(directory) / "input.wav"
            audio.write_bytes(b"RIFF-audio")
            read_started = threading.Event()
            release_read = threading.Event()
            original_read_bytes = Path.read_bytes
            errors = []

            def blocking_read(path):
                if Path(path) == audio:
                    read_started.set()
                    release_read.wait(1)
                return original_read_bytes(path)

            def transcribe():
                try:
                    manager.transcribe(audio)
                except Exception as error:
                    errors.append(error)

            with patch.object(Path, "read_bytes", new=blocking_read):
                worker = threading.Thread(target=transcribe)
                worker.start()
                self.assertTrue(read_started.wait(0.5))
                manager.cancel()
                release_read.set()
                worker.join(0.75)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], local_asr.LocalASRCancelledError)
            self.assertEqual(factory.processes, [])
            self.assertFalse(installer.process_record_path.exists())

    def test_shutdown_concurrent_with_startup_is_bounded_and_cleans_record(self):
        with tempfile.TemporaryDirectory() as directory:
            health_polled = threading.Event()

            class UnhealthySession(SidecarSession):
                def get(self, url, timeout=None):
                    self.get_calls.append((url, timeout))
                    health_polled.set()
                    return FakeResponse(status_code=503)

            manager, installer, _session, factory = self._manager(
                directory, session=UnhealthySession(), startup_timeout=5)
            errors = []

            def start():
                try:
                    manager.start()
                except Exception as error:
                    errors.append(error)

            worker = threading.Thread(target=start)
            worker.start()
            self.assertTrue(health_polled.wait(0.5))

            started = time.monotonic()
            manager.shutdown()
            worker.join(0.75)

            self.assertFalse(worker.is_alive())
            self.assertLess(time.monotonic() - started, 0.75)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], local_asr.LocalASRCancelledError)
            self.assertEqual(factory.processes[0].terminate_calls, 1)
            self.assertFalse(installer.process_record_path.exists())

    def test_idle_shutdown_stops_verified_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, _installer, _session, factory = self._manager(
                directory, idle_seconds=0.05)

            manager.start()
            time.sleep(0.12)

            self.assertEqual(factory.processes[0].terminate_calls, 1)
            self.assertIsNone(manager.process_id)

    def test_idle_shutdown_waits_until_active_request_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            request_started = threading.Event()
            release_request = threading.Event()

            def blocking_post():
                request_started.set()
                release_request.wait(2)
                return FakeResponse(payload={"text": "slow local text"})

            session = SidecarSession([blocking_post])
            manager, _installer, _session, factory = self._manager(
                directory, session=session, idle_seconds=0.05,
                request_timeout=1)
            audio = Path(directory) / "input.wav"
            audio.write_bytes(b"RIFF-audio")
            results = []
            worker = threading.Thread(
                target=lambda: results.append(manager.transcribe(audio)))
            worker.start()
            self.assertTrue(request_started.wait(0.5))

            time.sleep(0.12)

            self.assertEqual(factory.processes[0].terminate_calls, 0)
            self.assertTrue(worker.is_alive())
            release_request.set()
            worker.join(0.75)
            self.assertEqual(results, ["slow local text"])

            deadline = time.monotonic() + 0.5
            while factory.processes[0].terminate_calls == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            self.assertEqual(factory.processes[0].terminate_calls, 1)

    def test_process_record_failure_does_not_orphan_started_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, _installer, _session, factory = self._manager(directory)
            manager._record_process = Mock(side_effect=OSError("disk full"))

            with self.assertRaises(local_asr.LocalASRSidecarError):
                manager.start()

            self.assertEqual(factory.processes[0].terminate_calls, 1)
            self.assertIsNone(manager.process_id)

    def test_unconfirmed_managed_termination_keeps_process_record(self):
        with tempfile.TemporaryDirectory() as directory:
            class StubbornProcess(FakeProcess):
                def terminate(self):
                    self.terminate_calls += 1

                def kill(self):
                    self.kill_calls += 1

                def wait(self, timeout=None):
                    raise TimeoutError("still running")

            factory = PopenFactory()

            def create_process(command, **kwargs):
                factory.calls.append((command, kwargs))
                process = StubbornProcess()
                factory.processes.append(process)
                return process

            manager, installer, _session, _factory = self._manager(
                directory, factory=create_process)
            manager.start()

            manager.stop()

            self.assertTrue(installer.process_record_path.exists())


class LocalASRRecordedProcessTests(unittest.TestCase):
    def test_windows_working_set_reports_kernel_peak(self):
        def populate_counters(_process, pointer, _size):
            pointer._obj.PeakWorkingSetSize = 987654
            pointer._obj.WorkingSetSize = 123456
            return 1

        kernel32 = SimpleNamespace(
            OpenProcess=Mock(return_value=123),
            CloseHandle=Mock(return_value=1),
        )
        psapi = SimpleNamespace(
            GetProcessMemoryInfo=Mock(side_effect=populate_counters),
        )
        windll = SimpleNamespace(kernel32=kernel32, psapi=psapi)

        with patch.object(
                local_asr_harness.platform, "system", return_value="Windows"), \
                patch.object(ctypes, "windll", windll, create=True):
            peak = local_asr_harness._windows_working_set(4321)

        self.assertEqual(peak, 987654)
        kernel32.CloseHandle.assert_called_once_with(123)

    def test_windows_elevation_state_uses_admin_token_check(self):
        shell32 = SimpleNamespace(IsUserAnAdmin=Mock(return_value=1))
        windll = SimpleNamespace(shell32=shell32)

        with patch.object(local_asr.platform, "system", return_value="Windows"), \
                patch.object(ctypes, "windll", windll, create=True):
            self.assertTrue(local_asr._windows_elevation_state())

        shell32.IsUserAnAdmin.assert_called_once_with()
        shell32.IsUserAnAdmin.return_value = 0
        with patch.object(local_asr.platform, "system", return_value="Windows"), \
                patch.object(ctypes, "windll", windll, create=True):
            self.assertFalse(local_asr._windows_elevation_state())

    def test_pid_running_state_uses_synchronize_without_terminating(self):
        kernel32 = SimpleNamespace(
            OpenProcess=Mock(return_value=123),
            WaitForSingleObject=Mock(return_value=0x00000102),
            CloseHandle=Mock(return_value=1),
        )
        windll = SimpleNamespace(kernel32=kernel32)

        with patch.object(local_asr.platform, "system", return_value="Windows"), \
                patch.object(ctypes, "windll", windll, create=True):
            self.assertTrue(local_asr._pid_running_state(4321))

        kernel32.OpenProcess.assert_called_once_with(0x00100000, False, 4321)
        kernel32.WaitForSingleObject.assert_called_once_with(123, 0)
        kernel32.CloseHandle.assert_called_once_with(123)

        kernel32.WaitForSingleObject.return_value = 0
        with patch.object(local_asr.platform, "system", return_value="Windows"), \
                patch.object(ctypes, "windll", windll, create=True):
            self.assertFalse(local_asr._pid_running_state(4322))

    def test_terminate_pid_requests_synchronize_and_confirms_wait(self):
        kernel32 = SimpleNamespace(
            OpenProcess=Mock(return_value=123),
            TerminateProcess=Mock(return_value=1),
            WaitForSingleObject=Mock(return_value=0),
            CloseHandle=Mock(return_value=1),
        )
        windll = SimpleNamespace(kernel32=kernel32)

        with patch.object(local_asr.platform, "system", return_value="Windows"), \
                patch.object(ctypes, "windll", windll, create=True):
            self.assertTrue(local_asr._terminate_pid(4321))

        kernel32.OpenProcess.assert_called_once_with(0x00100001, False, 4321)
        kernel32.TerminateProcess.assert_called_once_with(123, 1)
        kernel32.WaitForSingleObject.assert_called_once_with(123, 2000)
        kernel32.CloseHandle.assert_called_once_with(123)

        kernel32.WaitForSingleObject.return_value = 0x00000102
        with patch.object(local_asr.platform, "system", return_value="Windows"), \
                patch.object(ctypes, "windll", windll, create=True):
            self.assertFalse(local_asr._terminate_pid(4322))

    def test_cleanup_keeps_record_when_termination_is_not_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / "sidecar-process.json"
            executable = Path(directory) / "whisper-server.exe"
            record_path.write_text(json.dumps({"pid": 4321}), encoding="utf-8")

            with patch.object(
                    local_asr, "_process_image_path", return_value=executable), \
                    patch.object(local_asr, "_terminate_pid", return_value=False):
                with self.assertRaises(local_asr.LocalASRSidecarError):
                    local_asr.cleanup_recorded_sidecar(record_path, executable)

            self.assertTrue(record_path.exists())

    def test_cleanup_keeps_record_when_image_lookup_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / "sidecar-process.json"
            executable = Path(directory) / "whisper-server.exe"
            record_path.write_text(json.dumps({"pid": 4321}), encoding="utf-8")

            for running in (True, None):
                with self.subTest(running=running), \
                        patch.object(
                            local_asr, "_process_image_path", return_value=None,
                        ), patch.object(
                            local_asr, "_pid_running_state", return_value=running,
                        ), patch.object(local_asr, "_terminate_pid") as terminate:
                    with self.assertRaises(local_asr.LocalASRSidecarError):
                        local_asr.cleanup_recorded_sidecar(
                            record_path, executable)

                self.assertTrue(record_path.exists())
                terminate.assert_not_called()

    def test_cleanup_discards_record_after_proving_pid_exited(self):
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / "sidecar-process.json"
            executable = Path(directory) / "whisper-server.exe"
            record_path.write_text(json.dumps({"pid": 4321}), encoding="utf-8")

            with patch.object(
                    local_asr, "_process_image_path", return_value=None), \
                    patch.object(
                        local_asr, "_pid_running_state", return_value=False,
                    ):
                local_asr.cleanup_recorded_sidecar(record_path, executable)

            self.assertFalse(record_path.exists())


class LocalASRProviderAdapterTests(unittest.TestCase):
    def test_adapter_registers_with_typed_audio_only_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "input.wav"
            audio.write_bytes(b"RIFF-audio")
            backend = Mock()
            backend.transcribe.return_value = "typed local transcript"
            registry = ProviderRegistry()
            registry.register(local_asr.LocalASRProviderAdapter(backend))

            result = registry.transcribe(
                local_asr.PROVIDER_ID,
                TranscriptionRequest(
                    audio, local_asr.MODEL_ID, "pt", "unused", "unused", 0.0,
                    audio_bytes=b"RIFF-owned-by-recording-session",
                ),
                ProviderConnection("", ""),
            )

            self.assertEqual(result.text, "typed local transcript")
            self.assertEqual(result.provider_id, local_asr.PROVIDER_ID)
            self.assertEqual(result.model, local_asr.MODEL_ID)
            self.assertTrue(registry.supports(
                local_asr.PROVIDER_ID,
                ProviderCapability.AUDIO_TRANSCRIPTION,
            ))
            self.assertFalse(registry.supports(
                local_asr.PROVIDER_ID,
                ProviderCapability.TEXT_GENERATION,
            ))
            backend.transcribe.assert_called_once_with(
                audio,
                "pt",
                audio_bytes=b"RIFF-owned-by-recording-session",
            )
            registry.adapter(local_asr.PROVIDER_ID).cancel()
            registry.adapter(local_asr.PROVIDER_ID).shutdown()
            backend.cancel.assert_called_once_with()
            backend.shutdown.assert_called_once_with()

    def test_adapter_maps_install_and_runtime_failures_to_provider_errors(self):
        request = TranscriptionRequest(
            Path("input.wav"), local_asr.MODEL_ID, "en", "", "", 0.0)
        connection = ProviderConnection("", "")
        cases = (
            (
                local_asr.LocalASRInstallRequiredError("Install Local ASR"),
                ProviderConfigurationError,
            ),
            (
                local_asr.LocalASRIntegrityError("Reinstall Local ASR"),
                ProviderConfigurationError,
            ),
            (
                local_asr.LocalASRSidecarError("Sidecar unavailable"),
                ProviderResponseError,
            ),
        )

        for local_error, provider_error in cases:
            with self.subTest(error=type(local_error).__name__):
                backend = Mock()
                backend.transcribe.side_effect = local_error
                adapter = local_asr.LocalASRProviderAdapter(backend)

                with self.assertRaises(provider_error) as raised:
                    adapter.transcribe(request, connection)

                self.assertEqual(raised.exception.provider_id, local_asr.PROVIDER_ID)
                self.assertEqual(
                    raised.exception.capability,
                    ProviderCapability.AUDIO_TRANSCRIPTION,
                )

    def test_adapter_rejects_unpinned_model_and_is_not_registered_by_default(self):
        adapter = local_asr.LocalASRProviderAdapter(Mock())
        request = TranscriptionRequest(
            Path("input.wav"), "another-model", "en", "", "", 0.0)

        with self.assertRaises(ProviderConfigurationError):
            adapter.transcribe(request, ProviderConnection("", ""))

        self.assertNotIn(local_asr.PROVIDER_ID, build_provider_registry().provider_ids)


if __name__ == "__main__":
    unittest.main()
