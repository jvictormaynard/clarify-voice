"""Provider credential storage with a Windows DPAPI backend.

Only this module knows how credential material is encoded at rest. Callers
identify entries by provider and receive deliberately generic errors so a
failure cannot echo a credential into logs or diagnostics.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import platform
import tempfile
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Mapping


SECRET_STORE_VERSION = 1
SUPPORTED_SECRET_PROVIDERS = ("gemini", "openai", "groq")


class SecretStoreError(OSError):
    """Base error whose message is always safe to show to a user."""


class SecretStoreUnavailableError(SecretStoreError):
    """Raised when the operating-system protection backend cannot be used."""


class SecretStoreCorruptedError(SecretStoreError):
    """Raised when a stored entry cannot be decoded safely."""


class SecretStore(ABC):
    """Minimal provider-keyed credential persistence contract."""

    @abstractmethod
    def get(self, provider: str) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def set(self, provider: str, secret: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, provider: str) -> None:
        raise NotImplementedError


def _provider(provider: str) -> str:
    value = str(provider).strip().lower()
    if value not in SUPPORTED_SECRET_PROVIDERS:
        raise ValueError("Unsupported secret-store provider")
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        if os.name != "nt":
            path.chmod(0o600)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


class _JsonSecretStore(SecretStore):
    """Shared atomic JSON container for encoded secret entries."""

    backend_name = "unknown"

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _load_entries(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raise SecretStoreCorruptedError(
                "The credential store cannot be read") from None
        if not isinstance(payload, Mapping):
            raise SecretStoreCorruptedError("The credential store is invalid")
        if payload.get("version") != SECRET_STORE_VERSION:
            raise SecretStoreCorruptedError(
                "The credential store version is unsupported")
        if payload.get("backend") != self.backend_name:
            raise SecretStoreCorruptedError(
                "The credential store backend is invalid")
        entries = payload.get("entries")
        if not isinstance(entries, Mapping):
            raise SecretStoreCorruptedError(
                "The credential store entries are invalid")
        return dict(entries)

    def _write_entries(self, entries: Mapping[str, object]) -> None:
        if not entries:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                raise SecretStoreUnavailableError(
                    "The credential store could not be updated") from None
            return
        try:
            _atomic_write_json(self.path, {
                "version": SECRET_STORE_VERSION,
                "backend": self.backend_name,
                "entries": dict(entries),
            })
        except OSError:
            raise SecretStoreUnavailableError(
                "The credential store could not be updated") from None

    @abstractmethod
    def _encode(self, secret: str) -> object:
        raise NotImplementedError

    @abstractmethod
    def _decode(self, encoded: object) -> str:
        raise NotImplementedError

    def get(self, provider: str) -> str | None:
        name = _provider(provider)
        with self._lock:
            entries = self._load_entries()
            if name not in entries:
                return None
            return self._decode(entries[name])

    def set(self, provider: str, secret: str) -> None:
        name = _provider(provider)
        value = str(secret)
        if not value:
            raise ValueError("A non-empty credential is required")
        with self._lock:
            entries = self._load_entries()
            entries[name] = self._encode(value)
            self._write_entries(entries)

    def delete(self, provider: str) -> None:
        name = _provider(provider)
        with self._lock:
            entries = self._load_entries()
            if name not in entries:
                return
            del entries[name]
            self._write_entries(entries)


class PlaintextFileSecretStore(_JsonSecretStore):
    """Explicit source-run fallback for experimental non-Windows platforms."""

    backend_name = "plaintext-fallback"

    def _encode(self, secret: str) -> object:
        return secret

    def _decode(self, encoded: object) -> str:
        if not isinstance(encoded, str) or not encoded:
            raise SecretStoreCorruptedError(
                "A credential-store entry is invalid")
        return encoded


class WindowsDpapiProtector:
    """Small ctypes wrapper around current-user Windows DPAPI."""

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise SecretStoreUnavailableError(
                "Windows credential protection is unavailable")
        try:
            self._crypt32 = ctypes.windll.crypt32
            self._kernel32 = ctypes.windll.kernel32
            blob_pointer = ctypes.POINTER(self._DataBlob)
            self._crypt32.CryptProtectData.argtypes = [
                blob_pointer, ctypes.c_wchar_p, blob_pointer,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_pointer,
            ]
            self._crypt32.CryptProtectData.restype = ctypes.c_bool
            self._crypt32.CryptUnprotectData.argtypes = [
                blob_pointer, ctypes.POINTER(ctypes.c_wchar_p), blob_pointer,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_pointer,
            ]
            self._crypt32.CryptUnprotectData.restype = ctypes.c_bool
            self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            self._kernel32.LocalFree.restype = ctypes.c_void_p
        except (AttributeError, OSError):
            raise SecretStoreUnavailableError(
                "Windows credential protection is unavailable") from None

    @classmethod
    def _input_blob(cls, value: bytes):
        buffer = ctypes.create_string_buffer(value)
        blob = cls._DataBlob(len(value), ctypes.cast(buffer, ctypes.c_void_p))
        return blob, buffer

    def protect(self, value: bytes) -> bytes:
        source, source_buffer = self._input_blob(value)
        protected = self._DataBlob()
        try:
            succeeded = self._crypt32.CryptProtectData(
                ctypes.byref(source), None, None, None, None,
                self._CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(protected))
            if not succeeded:
                raise SecretStoreUnavailableError(
                    "Windows credential protection failed")
            return ctypes.string_at(protected.pbData, protected.cbData)
        finally:
            del source_buffer
            if protected.pbData:
                self._kernel32.LocalFree(protected.pbData)

    def unprotect(self, value: bytes) -> bytes:
        source, source_buffer = self._input_blob(value)
        clear = self._DataBlob()
        try:
            succeeded = self._crypt32.CryptUnprotectData(
                ctypes.byref(source), None, None, None, None,
                self._CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(clear))
            if not succeeded:
                raise SecretStoreCorruptedError(
                    "A credential-store entry cannot be decrypted")
            return ctypes.string_at(clear.pbData, clear.cbData)
        finally:
            del source_buffer
            if clear.pbData:
                self._kernel32.LocalFree(clear.pbData)


class DpapiSecretStore(_JsonSecretStore):
    """DPAPI-encrypted credentials persisted as base64 ciphertext."""

    backend_name = "windows-dpapi"

    def __init__(
        self,
        path: str | os.PathLike[str],
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
    ) -> None:
        super().__init__(path)
        if (protect is None) != (unprotect is None):
            raise ValueError(
                "Both DPAPI callbacks must be supplied for an injected backend")
        self._protect = protect
        self._unprotect = unprotect

    def _callbacks(
        self,
    ) -> tuple[Callable[[bytes], bytes], Callable[[bytes], bytes]]:
        if self._protect is None or self._unprotect is None:
            protector = WindowsDpapiProtector()
            self._protect = protector.protect
            self._unprotect = protector.unprotect
        return self._protect, self._unprotect

    def _encode(self, secret: str) -> object:
        try:
            protect, _unprotect = self._callbacks()
            protected = protect(secret.encode("utf-8"))
            return base64.b64encode(protected).decode("ascii")
        except SecretStoreError:
            raise
        except Exception:
            raise SecretStoreUnavailableError(
                "Windows credential protection failed") from None

    def _decode(self, encoded: object) -> str:
        if not isinstance(encoded, str) or not encoded:
            raise SecretStoreCorruptedError(
                "A credential-store entry is invalid")
        try:
            protected = base64.b64decode(encoded, validate=True)
            _protect, unprotect = self._callbacks()
            return unprotect(protected).decode("utf-8")
        except SecretStoreError:
            raise
        except (ValueError, UnicodeError, TypeError):
            raise SecretStoreCorruptedError(
                "A credential-store entry is invalid") from None
        except Exception:
            raise SecretStoreCorruptedError(
                "A credential-store entry cannot be decrypted") from None


class MemorySecretStore(SecretStore):
    """In-memory implementation for isolated repository and UI tests."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.values = {
            _provider(name): str(value) for name, value in (values or {}).items()
        }

    def get(self, provider: str) -> str | None:
        return self.values.get(_provider(provider))

    def set(self, provider: str, secret: str) -> None:
        name = _provider(provider)
        value = str(secret)
        if not value:
            raise ValueError("A non-empty credential is required")
        self.values[name] = value

    def delete(self, provider: str) -> None:
        self.values.pop(_provider(provider), None)


def create_secret_store(
    data_directory: str | os.PathLike[str],
    system: str | None = None,
    filename_stem: str = "secrets",
) -> SecretStore:
    """Return the supported backend for the current source/runtime platform."""

    directory = Path(data_directory)
    platform_name = platform.system() if system is None else system
    if platform_name == "Windows":
        return DpapiSecretStore(directory / f"{filename_stem}.dpapi.json")
    return PlaintextFileSecretStore(directory / f"{filename_stem}.json")
