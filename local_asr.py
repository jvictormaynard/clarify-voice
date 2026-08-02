"""Optional, checksummed whisper.cpp sidecar for local transcription.

Importing this module never downloads assets or starts a process. Installation
is an explicit operation and all installed files live outside the one-file app.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import secrets
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Protocol, runtime_checkable

import requests


PROVIDER_ID = "local_asr"
MODEL_ID = "ggml-small"
MANIFEST_FILENAME = "local_asr_manifest.json"
ROOT_MARKER = ".clarifyvoice-local-asr-root"
ProgressCallback = Callable[[str, int, int], None]


@runtime_checkable
class LocalTranscriptionBackend(Protocol):
    """Narrow boundary for the future provider-registry/lifecycle adapters."""

    def transcribe(
        self,
        audio_path: Path,
        language: str = "en",
        cancel_event: threading.Event | None = None,
    ) -> str:
        ...

    def cancel(self) -> None:
        ...

    def shutdown(self) -> None:
        ...


class LocalASRError(RuntimeError):
    """Base error with a message suitable for a local failure state."""


class LocalASRInstallRequiredError(LocalASRError):
    """The optional assets are absent or no longer pass verification."""


class LocalASRIntegrityError(LocalASRError):
    """A downloaded or installed file did not match the pinned manifest."""


class LocalASRCancelledError(LocalASRError):
    """The caller cancelled an in-flight local transcription."""


class LocalASRSidecarError(LocalASRError):
    """The verified sidecar could not start or complete inference."""


@dataclass(frozen=True)
class ManifestAsset:
    name: str
    filename: str
    url: str
    size: int
    sha256: str
    license: str
    source_url: str
    archive: str = ""


def _resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def default_manifest_path() -> Path:
    return _resource_root() / MANIFEST_FILENAME


def default_install_root() -> Path:
    if platform.system() == "Windows":
        parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return parent / "ClarifyVoice" / "local-asr"
    parent = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return parent / "ClarifyVoice" / "local-asr"


def _sha256(
    path: Path,
    chunk_size: int = 1024 * 1024,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            if cancel_check is not None and cancel_check():
                raise LocalASRCancelledError("Local ASR startup was cancelled")
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: str) -> Path:
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise LocalASRIntegrityError(f"Unsafe manifest path: {value}")
    return Path(*pure.parts)


def _valid_digest(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in string.hexdigits for character in value))


def load_manifest(path: Path | None = None) -> dict:
    manifest_path = Path(path or default_manifest_path())
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise LocalASRIntegrityError(f"Cannot read local-ASR manifest: {error}") from error
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise LocalASRIntegrityError("Unsupported local-ASR manifest schema")
    if payload.get("provider") != PROVIDER_ID:
        raise LocalASRIntegrityError("Manifest provider is not local_asr")
    for section in ("engine", "recommended_model"):
        value = payload.get(section)
        if (not isinstance(value, Mapping)
                or not str(value.get("license", "")).strip()
                or not str(value.get("source_url", "")).startswith("https://")):
            raise LocalASRIntegrityError(f"Manifest section is invalid: {section}")
    requirements = payload.get("requirements")
    if (not isinstance(requirements, Mapping)
            or not str(requirements.get("platform", "")).strip()
            or not str(requirements.get("compute", "")).strip()
            or any(not isinstance(requirements.get(key), int)
                   or requirements[key] <= 0
                   for key in ("memory_bytes", "disk_bytes", "download_bytes"))):
        raise LocalASRIntegrityError("Manifest requirements are invalid")
    license_files = payload.get("license_files")
    if not isinstance(license_files, list) or not license_files:
        raise LocalASRIntegrityError("Manifest license_files are incomplete")
    for value in license_files:
        _safe_relative_path(str(value))
    assets = payload.get("assets")
    extracted = payload.get("extracted_files")
    if not isinstance(assets, Mapping) or not isinstance(extracted, list):
        raise LocalASRIntegrityError("Manifest assets are incomplete")
    for name in ("runtime", "model"):
        value = assets.get(name)
        if not isinstance(value, Mapping):
            raise LocalASRIntegrityError(f"Manifest asset is missing: {name}")
        if (not isinstance(value.get("size"), int) or value["size"] <= 0
                or not _valid_digest(value.get("sha256"))
                or not str(value.get("url", "")).startswith("https://")
                or not str(value.get("source_url", "")).startswith("https://")
                or not str(value.get("license", "")).strip()):
            raise LocalASRIntegrityError(f"Manifest asset is invalid: {name}")
    seen_paths: set[str] = set()
    for value in extracted:
        if (not isinstance(value, Mapping)
                or not isinstance(value.get("size"), int) or value["size"] <= 0
                or not _valid_digest(value.get("sha256"))):
            raise LocalASRIntegrityError("Manifest extracted-file entry is invalid")
        _safe_relative_path(str(value.get("archive_path", "")))
        destination = str(value.get("path", ""))
        _safe_relative_path(destination)
        if destination in seen_paths:
            raise LocalASRIntegrityError(f"Duplicate extracted path: {destination}")
        seen_paths.add(destination)
    return dict(payload)


class LocalASRInstaller:
    """Install, verify, report, and remove the pinned optional assets."""

    def __init__(
        self,
        root: Path | None = None,
        manifest_path: Path | None = None,
        session=None,
    ):
        self.root = Path(root or default_install_root())
        self.manifest_path = Path(manifest_path or default_manifest_path())
        self.manifest = load_manifest(self.manifest_path)
        self._session = session or requests

    @property
    def installation_id(self) -> str:
        engine = self.manifest["engine"]
        model = self.manifest["recommended_model"]
        return f"{engine['name']}-{engine['version']}-{model['id']}"

    @property
    def install_dir(self) -> Path:
        return self.root / self.installation_id

    @property
    def executable_path(self) -> Path:
        return self.install_dir / "runtime" / "whisper-server.exe"

    @property
    def model_path(self) -> Path:
        model_name = self.manifest["assets"]["model"]["filename"]
        return self.install_dir / "models" / model_name

    @property
    def process_record_path(self) -> Path:
        return self.root / "sidecar-process.json"

    def asset(self, name: str) -> ManifestAsset:
        value = self.manifest["assets"][name]
        return ManifestAsset(
            name=name,
            filename=str(value["filename"]),
            url=str(value["url"]),
            size=int(value["size"]),
            sha256=str(value["sha256"]).lower(),
            license=str(value["license"]),
            source_url=str(value["source_url"]),
            archive=str(value.get("archive", "")),
        )

    def requirements(self) -> dict:
        return dict(self.manifest["requirements"])

    def _expected_installed_files(self):
        for entry in self.manifest["extracted_files"]:
            yield (
                self.install_dir / _safe_relative_path(str(entry["path"])),
                int(entry["size"]),
                str(entry["sha256"]).lower(),
            )
        model = self.asset("model")
        yield self.model_path, model.size, model.sha256
        for value in self.manifest.get("license_files", []):
            relative = _safe_relative_path(str(value))
            source = _resource_root() / relative
            try:
                size = source.stat().st_size
                source_digest = _sha256(source)
            except OSError as error:
                raise LocalASRIntegrityError(
                    f"Bundled license notice is missing: {relative}") from error
            yield self.install_dir / relative, size, source_digest

    def verify(self, cancel_check: Callable[[], bool] | None = None) -> Path:
        if not self.install_dir.is_dir():
            raise LocalASRInstallRequiredError(
                "Local ASR is not installed. Run the explicit installer first.")
        for path, expected_size, expected_digest in self._expected_installed_files():
            try:
                actual_size = path.stat().st_size
            except FileNotFoundError as error:
                raise LocalASRInstallRequiredError(
                    f"Local ASR is incomplete; missing {path.name}. Reinstall it.") from error
            except OSError as error:
                raise LocalASRIntegrityError(
                    f"Cannot read installed {path.name}. Remove and reinstall Local ASR."
                ) from error
            if actual_size != expected_size:
                raise LocalASRIntegrityError(
                    f"Integrity check failed for {path.name}. Remove and reinstall Local ASR.")
            try:
                actual_digest = _sha256(path, cancel_check=cancel_check)
            except LocalASRCancelledError:
                raise
            except FileNotFoundError as error:
                raise LocalASRInstallRequiredError(
                    f"Local ASR is incomplete; missing {path.name}. Reinstall it.") from error
            except OSError as error:
                raise LocalASRIntegrityError(
                    f"Cannot read installed {path.name}. Remove and reinstall Local ASR."
                ) from error
            if actual_digest != expected_digest:
                raise LocalASRIntegrityError(
                    f"Integrity check failed for {path.name}. Remove and reinstall Local ASR.")
        return self.install_dir

    def status(self) -> dict:
        try:
            self.verify()
        except LocalASRInstallRequiredError as error:
            state = "not_installed"
            detail = str(error)
        except LocalASRIntegrityError as error:
            state = "invalid"
            detail = str(error)
        else:
            state = "installed"
            detail = "All installed files match the published SHA-256 digests."
        return {
            "state": state,
            "detail": detail,
            "path": str(self.install_dir),
            "engine": self.manifest["engine"]["version"],
            "model": self.manifest["recommended_model"]["id"],
            "requirements": self.requirements(),
        }

    @staticmethod
    def _report(callback: ProgressCallback | None, stage: str, current: int, total: int):
        if callback is not None:
            callback(stage, current, total)

    def _download(
        self,
        asset: ManifestAsset,
        destination: Path,
        callback: ProgressCallback | None,
    ) -> None:
        self._report(callback, f"download:{asset.name}", 0, asset.size)
        digest = hashlib.sha256()
        downloaded = 0
        try:
            response = self._session.get(
                asset.url, stream=True, timeout=(10, 60),
                headers={"User-Agent": "ClarifyVoice-local-asr/1"})
            response.raise_for_status()
            with destination.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > asset.size:
                        raise LocalASRIntegrityError(
                            f"{asset.filename} exceeded its published size")
                    stream.write(chunk)
                    digest.update(chunk)
                    self._report(
                        callback, f"download:{asset.name}", downloaded, asset.size)
        except LocalASRError:
            raise
        except Exception as error:
            raise LocalASRError(f"Could not download {asset.filename}: {error}") from error
        if downloaded != asset.size or digest.hexdigest() != asset.sha256:
            raise LocalASRIntegrityError(
                f"SHA-256 or size mismatch for {asset.filename}; nothing was installed")
        self._report(callback, f"verify:{asset.name}", downloaded, asset.size)

    def _claim_root(self) -> None:
        marker = self.root / ROOT_MARKER
        if self.root.is_symlink():
            raise LocalASRError(f"Refusing symlinked asset root: {self.root}")
        if self.root.exists() and not marker.is_file():
            try:
                has_contents = next(self.root.iterdir(), None) is not None
            except OSError as error:
                raise LocalASRError(f"Cannot inspect asset root {self.root}: {error}") from error
            if has_contents:
                raise LocalASRError(
                    f"Refusing to use non-empty unowned asset root: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        if marker.exists():
            try:
                owner = marker.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise LocalASRError(f"Cannot verify asset-root ownership: {error}") from error
            if owner != PROVIDER_ID:
                raise LocalASRError(f"Asset root has an unknown owner: {self.root}")
        else:
            marker.write_text(f"{PROVIDER_ID}\n", encoding="utf-8")

    def _extract_runtime(self, archive_path: Path, staging: Path) -> None:
        expected = {
            str(entry["archive_path"]): entry
            for entry in self.manifest["extracted_files"]
        }
        try:
            with zipfile.ZipFile(archive_path) as archive:
                archive_names = set(archive.namelist())
                missing = set(expected) - archive_names
                if missing:
                    raise LocalASRIntegrityError(
                        f"Runtime archive is missing {sorted(missing)[0]}")
                for archive_name, entry in expected.items():
                    destination = staging / _safe_relative_path(str(entry["path"]))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(archive_name) as source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    if (destination.stat().st_size != int(entry["size"])
                            or _sha256(destination) != str(entry["sha256"]).lower()):
                        raise LocalASRIntegrityError(
                            f"Extracted runtime file failed verification: {destination.name}")
        except LocalASRError:
            raise
        except (OSError, zipfile.BadZipFile, KeyError) as error:
            raise LocalASRIntegrityError(f"Cannot extract verified runtime: {error}") from error

    def _copy_license_notices(self, staging: Path) -> None:
        notices = self.manifest.get("license_files", [])
        if not isinstance(notices, list):
            raise LocalASRIntegrityError("Manifest license_files must be a list")
        for value in notices:
            relative = _safe_relative_path(str(value))
            source = _resource_root() / relative
            destination = staging / relative
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            except OSError as error:
                raise LocalASRIntegrityError(
                    f"Cannot preserve license notice {relative}: {error}") from error

    def install(self, callback: ProgressCallback | None = None) -> dict:
        existing = self.status()
        if existing["state"] == "installed":
            self._report(callback, "complete", 1, 1)
            return existing

        self._claim_root()
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=self.root))
        runtime_archive = staging / self.asset("runtime").filename
        try:
            self._download(self.asset("runtime"), runtime_archive, callback)
            self._report(callback, "extract:runtime", 0, 1)
            self._extract_runtime(runtime_archive, staging)
            runtime_archive.unlink(missing_ok=True)
            self._report(callback, "extract:runtime", 1, 1)

            model = self.asset("model")
            model_path = staging / "models" / model.filename
            model_path.parent.mkdir(parents=True, exist_ok=True)
            self._download(model, model_path, callback)
            self._copy_license_notices(staging)

            receipt = {
                "schema_version": 1,
                "installation_id": self.installation_id,
                "engine": self.manifest["engine"],
                "model": self.manifest["recommended_model"],
                "installed_at": int(time.time()),
            }
            (staging / "receipt.json").write_text(
                json.dumps(receipt, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")

            if self.install_dir.exists():
                cleanup_recorded_sidecar(
                    self.process_record_path, self.executable_path)
                shutil.rmtree(self.install_dir)
            os.replace(staging, self.install_dir)
            result = self.status()
            if result["state"] != "installed":
                raise LocalASRIntegrityError(result["detail"])
            self._report(callback, "complete", 1, 1)
            return result
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def remove(self) -> bool:
        cleanup_recorded_sidecar(self.process_record_path, self.executable_path)
        existed = self.root.exists()
        if not existed:
            return False
        if self.root.is_symlink():
            raise LocalASRError(f"Refusing to remove symlinked asset root: {self.root}")
        marker = self.root / ROOT_MARKER
        owned = False
        try:
            owned = marker.read_text(encoding="utf-8").strip() == PROVIDER_ID
        except OSError:
            pass
        if owned:
            shutil.rmtree(self.root)
            return True

        # A missing marker must never turn a custom --root into an arbitrary
        # recursive delete. Remove only paths whose ownership is unambiguous.
        shutil.rmtree(self.install_dir, ignore_errors=True)
        try:
            self.process_record_path.unlink(missing_ok=True)
        except OSError:
            pass
        for staging in self.root.glob(".install-*"):
            if staging.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
        try:
            self.root.rmdir()
        except OSError:
            pass
        return existed


def _process_image_path(pid: int) -> Path | None:
    if platform.system() != "Windows" or pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                    process, 0, buffer, ctypes.byref(size)):
                return None
            return Path(buffer.value)
        finally:
            kernel32.CloseHandle(process)
    except Exception:
        return None


def _terminate_pid(pid: int) -> bool:
    if platform.system() != "Windows" or pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process_terminate = 0x0001
        synchronize = 0x00100000
        wait_object_0 = 0
        process = kernel32.OpenProcess(
            process_terminate | synchronize, False, pid)
        if not process:
            return False
        try:
            if not kernel32.TerminateProcess(process, 1):
                return False
            return kernel32.WaitForSingleObject(process, 2000) == wait_object_0
        finally:
            kernel32.CloseHandle(process)
    except Exception:
        return False


def cleanup_recorded_sidecar(record_path: Path, expected_executable: Path) -> None:
    """Terminate only a recorded PID whose image still matches our sidecar."""
    record_path = Path(record_path)
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        pid = int(record.get("pid", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        pid = 0
    actual = _process_image_path(pid)
    try:
        matches = actual is not None and actual.resolve() == Path(expected_executable).resolve()
    except OSError:
        matches = False
    if matches and not _terminate_pid(pid):
        raise LocalASRSidecarError(
            "Could not confirm that the recorded local-ASR sidecar terminated")
    try:
        record_path.unlink(missing_ok=True)
    except OSError:
        pass


class LocalASRSidecarManager:
    """Own one loopback-only whisper-server process and its request lifecycle."""

    def __init__(
        self,
        installer: LocalASRInstaller | None = None,
        *,
        idle_seconds: float = 60.0,
        startup_timeout: float = 45.0,
        request_timeout: float = 120.0,
        session=None,
        popen_factory=None,
    ):
        self.installer = installer or LocalASRInstaller()
        self.idle_seconds = float(idle_seconds)
        self.startup_timeout = float(startup_timeout)
        self.request_timeout = float(request_timeout)
        self._session = session or requests.Session()
        if hasattr(self._session, "trust_env"):
            self._session.trust_env = False
        self._popen = popen_factory or subprocess.Popen
        self._process = None
        self._port = 0
        self._request_path = ""
        self._lock = threading.RLock()
        self._startup_lock = threading.Lock()
        self._transcribe_lock = threading.Lock()
        self._startup_cancel: threading.Event | None = None
        self._shutdown_event = threading.Event()
        self._active_cancellations: set[threading.Event] = set()
        self._idle_timer: threading.Timer | None = None

    @property
    def process_id(self) -> int | None:
        process = self._process
        return int(process.pid) if process is not None and process.poll() is None else None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}{self._request_path}"

    @staticmethod
    def _reserve_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            return int(server.getsockname()[1])

    def _record_process(self) -> None:
        path = self.installer.process_record_path
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "pid": self.process_id,
            "executable": str(self.installer.executable_path),
            "port": self._port,
            "started_at": int(time.time()),
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=".sidecar-process-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _health(self, expected_process=None) -> bool:
        with self._lock:
            process = self._process
            if (process is None or process.poll() is not None
                    or (expected_process is not None and process is not expected_process)):
                return False
            url = f"http://127.0.0.1:{self._port}{self._request_path}/health"
        try:
            response = self._session.get(url, timeout=(0.25, 0.25))
            healthy = response.status_code == 200 and response.json().get("status") == "ok"
        except Exception:
            return False
        with self._lock:
            return bool(
                healthy
                and self._process is process
                and process.poll() is None
            )

    @staticmethod
    def _cancelled(*events) -> bool:
        return any(event is not None and event.is_set() for event in events)

    def _raise_if_cancelled(self, cancel_event=None) -> None:
        if self._cancelled(cancel_event, self._shutdown_event):
            raise LocalASRCancelledError("Local ASR startup was cancelled")

    def start(self, cancel_event=None) -> float:
        while not self._startup_lock.acquire(timeout=0.05):
            self._raise_if_cancelled(cancel_event)
        try:
            startup_cancel = threading.Event()
            with self._lock:
                self._raise_if_cancelled(cancel_event)
                self._startup_cancel = startup_cancel
            try:
                if self._health():
                    with self._lock:
                        self._raise_if_cancelled(cancel_event)
                        self._schedule_idle_shutdown_locked()
                    return 0.0
                with self._lock:
                    self._stop_locked()
                self._raise_if_cancelled(cancel_event)
                self.installer.verify(cancel_check=lambda: self._cancelled(
                    cancel_event, startup_cancel, self._shutdown_event))
                self._raise_if_cancelled(startup_cancel)
                self._raise_if_cancelled(cancel_event)
                cleanup_recorded_sidecar(
                    self.installer.process_record_path, self.installer.executable_path)
                self._raise_if_cancelled(startup_cancel)
                self._raise_if_cancelled(cancel_event)

                port = self._reserve_port()
                request_path = f"/{secrets.token_urlsafe(24)}"
                threads = max(1, min(8, (os.cpu_count() or 4) // 2))
                command = [
                    str(self.installer.executable_path),
                    "--model", str(self.installer.model_path),
                    "--host", "127.0.0.1",
                    "--port", str(port),
                    "--request-path", request_path,
                    "--threads", str(threads),
                    "--language", "auto",
                    "--no-context",
                    "--no-timestamps",
                    "--no-gpu",
                ]
                flags = 0x08000000 if platform.system() == "Windows" else 0
                started = time.perf_counter()
                process = None
                try:
                    with self._lock:
                        self._raise_if_cancelled(startup_cancel)
                        self._raise_if_cancelled(cancel_event)
                        self._port = port
                        self._request_path = request_path
                        process = self._popen(
                            command,
                            cwd=str(self.installer.executable_path.parent),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=flags,
                        )
                        self._process = process
                        self._record_process()
                except LocalASRCancelledError:
                    raise
                except Exception as error:
                    with self._lock:
                        self._stop_locked(expected_process=process)
                    raise LocalASRSidecarError(
                        f"Could not start whisper.cpp: {error}") from error

                deadline = time.monotonic() + self.startup_timeout
                while time.monotonic() < deadline:
                    if self._cancelled(
                            cancel_event, startup_cancel, self._shutdown_event):
                        with self._lock:
                            self._stop_locked(expected_process=process)
                        raise LocalASRCancelledError("Local ASR startup was cancelled")
                    if self._health(expected_process=process):
                        elapsed = time.perf_counter() - started
                        with self._lock:
                            self._raise_if_cancelled(startup_cancel)
                            self._raise_if_cancelled(cancel_event)
                            if self._process is not process:
                                raise LocalASRCancelledError(
                                    "Local ASR startup was cancelled")
                            self._schedule_idle_shutdown_locked()
                        return elapsed
                    if process.poll() is not None:
                        code = process.returncode
                        with self._lock:
                            self._stop_locked(expected_process=process)
                        raise LocalASRSidecarError(
                            f"whisper.cpp exited during startup (code {code})")
                    startup_cancel.wait(0.05)
                with self._lock:
                    self._stop_locked(expected_process=process)
                raise LocalASRSidecarError(
                    "whisper.cpp did not become healthy within "
                    f"{self.startup_timeout:g}s")
            finally:
                with self._lock:
                    if self._startup_cancel is startup_cancel:
                        self._startup_cancel = None
        finally:
            self._startup_lock.release()

    def _cancel_idle_shutdown_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _schedule_idle_shutdown_locked(self) -> None:
        self._cancel_idle_shutdown_locked()
        if self.idle_seconds <= 0:
            return
        if (self._active_cancellations or self._shutdown_event.is_set()
                or self._process is None or self._process.poll() is not None):
            return

        def idle_shutdown() -> None:
            with self._lock:
                if self._idle_timer is not timer:
                    return
                self._idle_timer = None
                if not self._active_cancellations:
                    self._stop_locked()

        timer = threading.Timer(self.idle_seconds, idle_shutdown)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _stop_locked(self, expected_process=None) -> None:
        if expected_process is not None and self._process is not expected_process:
            return
        self._cancel_idle_shutdown_locked()
        process = self._process
        self._process = None
        termination_confirmed = process is not None and process.poll() is not None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    pass
            termination_confirmed = process.poll() is not None
        if termination_confirmed:
            try:
                self.installer.process_record_path.unlink(missing_ok=True)
            except OSError:
                pass
        self._port = 0
        self._request_path = ""

    def stop(self) -> None:
        with self._lock:
            startup_cancel = self._startup_cancel
        if startup_cancel is not None:
            startup_cancel.set()
        with self._lock:
            self._stop_locked()

    def cancel(self) -> None:
        with self._lock:
            cancellations = tuple(self._active_cancellations)
            startup_cancel = self._startup_cancel
        for event in cancellations:
            event.set()
        if startup_cancel is not None:
            startup_cancel.set()
        self.stop()

    def shutdown(self) -> None:
        self._shutdown_event.set()
        self.cancel()

    def _post_inference(self, audio_path: Path, language: str, result: queue.Queue) -> None:
        try:
            with Path(audio_path).open("rb") as audio_file:
                response = self._session.post(
                    f"{self.base_url}/inference",
                    files={"file": (Path(audio_path).name, audio_file, "audio/wav")},
                    data={
                        "response_format": "json",
                        "temperature": "0.0",
                        "language": language,
                    },
                    timeout=(5, self.request_timeout),
                )
            response.raise_for_status()
            payload = response.json()
            text = str(payload.get("text", "")).strip()
            if not text:
                raise LocalASRSidecarError("whisper.cpp returned an empty transcription")
            result.put((text, None))
        except Exception as error:
            result.put((None, error))

    def _transcribe_once(
        self,
        audio_path: Path,
        language: str,
        cancel_event: threading.Event | None,
    ) -> str:
        self.start(cancel_event=cancel_event)
        result: queue.Queue = queue.Queue(maxsize=1)
        worker = threading.Thread(
            target=self._post_inference,
            args=(audio_path, language, result),
            daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + self.request_timeout + 5
        while worker.is_alive():
            if cancel_event is not None and cancel_event.is_set():
                self.stop()
                worker.join(timeout=2)
                raise LocalASRCancelledError("Local transcription was cancelled")
            if time.monotonic() >= deadline:
                self.stop()
                worker.join(timeout=2)
                raise LocalASRSidecarError(
                    f"Local transcription exceeded {self.request_timeout:g}s")
            worker.join(timeout=0.05)
        if cancel_event is not None and cancel_event.is_set():
            self.stop()
            raise LocalASRCancelledError("Local transcription was cancelled")
        text, error = result.get_nowait()
        if error is not None:
            if isinstance(error, LocalASRError):
                raise error
            raise LocalASRSidecarError(f"Local inference failed: {error}") from error
        return str(text)

    def transcribe(
        self,
        audio_path: Path,
        language: str = "en",
        cancel_event: threading.Event | None = None,
    ) -> str:
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise LocalASRError(f"Audio file does not exist: {audio_path}")
        operation_cancel = threading.Event()
        with self._lock:
            if self._cancelled(cancel_event, self._shutdown_event):
                raise LocalASRCancelledError("Local transcription was cancelled")
            self._active_cancellations.add(operation_cancel)
            self._cancel_idle_shutdown_locked()
        combined_cancel = _CancellationView(
            operation_cancel, cancel_event, self._shutdown_event)
        owns_transcription = False
        try:
            while not self._transcribe_lock.acquire(timeout=0.05):
                if combined_cancel.is_set():
                    raise LocalASRCancelledError(
                        "Local transcription was cancelled")
            owns_transcription = True
            if combined_cancel.is_set():
                raise LocalASRCancelledError("Local transcription was cancelled")
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    return self._transcribe_once(
                        audio_path, language, combined_cancel)
                except (LocalASRCancelledError, LocalASRInstallRequiredError,
                        LocalASRIntegrityError):
                    raise
                except LocalASRError as error:
                    if combined_cancel.is_set():
                        raise LocalASRCancelledError(
                            "Local transcription was cancelled") from error
                    last_error = error
                    self.stop()
                    if attempt == 0:
                        continue
            raise LocalASRSidecarError(
                "whisper.cpp failed after one automatic restart: "
                f"{last_error}")
        finally:
            if owns_transcription:
                self._transcribe_lock.release()
            with self._lock:
                self._active_cancellations.discard(operation_cancel)
                self._schedule_idle_shutdown_locked()


class _CancellationView:
    """Read-only union of caller, lifecycle, and operation cancellation."""

    def __init__(self, *events):
        self._events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)
