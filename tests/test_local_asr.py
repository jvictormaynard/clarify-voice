import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock

import local_asr


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
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            fixture.manifest["extracted_files"][0]["path"] = "../escape.exe"
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

    def test_unowned_root_removal_preserves_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = InstallerFixture(directory)
            fixture.root.mkdir()
            installer, _session = fixture.installer()
            installer.executable_path.parent.mkdir(parents=True)
            installer.executable_path.write_bytes(b"old owned runtime")
            unrelated = fixture.root / "keep.txt"
            unrelated.write_text("user data", encoding="utf-8")

            self.assertTrue(installer.remove())

            self.assertFalse(installer.install_dir.exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "user data")


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

    def verify(self):
        self.verify_calls += 1
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
            startup_timeout=0.5,
            request_timeout=0.5,
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

    def test_idle_shutdown_stops_verified_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, _installer, _session, factory = self._manager(
                directory, idle_seconds=0.05)

            manager.start()
            time.sleep(0.12)

            self.assertEqual(factory.processes[0].terminate_calls, 1)
            self.assertIsNone(manager.process_id)

    def test_process_record_failure_does_not_orphan_started_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, _installer, _session, factory = self._manager(directory)
            manager._record_process = Mock(side_effect=OSError("disk full"))

            with self.assertRaises(local_asr.LocalASRSidecarError):
                manager.start()

            self.assertEqual(factory.processes[0].terminate_calls, 1)
            self.assertIsNone(manager.process_id)


if __name__ == "__main__":
    unittest.main()
