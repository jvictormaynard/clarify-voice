"""Small, testable Windows clipboard adapter used by ClarifyVoice.

The adapter deliberately handles only clipboard formats that can be copied as
owned global-memory blocks.  This keeps restoration deterministic and avoids
trying to duplicate arbitrary application-owned clipboard objects.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import platform
import time


CF_TEXT = 1
CF_DIB = 8
CF_UNICODETEXT = 13
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002
MAX_FORMAT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ClipboardFormat:
    format_id: int
    data: bytes


@dataclass(frozen=True)
class ClipboardSnapshot:
    formats: tuple[ClipboardFormat, ...]
    sequence: int
    restorable: bool = True

    @property
    def text(self) -> str | None:
        for item in self.formats:
            if item.format_id == CF_UNICODETEXT:
                try:
                    return item.data.decode("utf-16-le").rstrip("\x00")
                except UnicodeDecodeError:
                    return None
        for item in self.formats:
            if item.format_id == CF_TEXT:
                encoding = "mbcs" if platform.system() == "Windows" else "latin-1"
                return item.data.split(b"\x00", 1)[0].decode(
                    encoding, errors="replace")
        return None


class WindowsClipboardAdapter:
    """Read, write, and restore supported clipboard formats on Windows."""

    def __init__(self, *, is_windows: bool | None = None):
        self.is_windows = platform.system() == "Windows" if is_windows is None else is_windows

    def _user32(self):
        return ctypes.windll.user32

    def _kernel32(self):
        return ctypes.windll.kernel32

    def _registered_formats(self) -> set[int]:
        if not self.is_windows:
            return set()
        user32 = self._user32()
        user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
        user32.RegisterClipboardFormatW.restype = ctypes.c_uint
        return {
            int(user32.RegisterClipboardFormatW("HTML Format")),
            int(user32.RegisterClipboardFormatW("Rich Text Format")),
        }

    def _supported_formats(self) -> set[int]:
        return {
            CF_TEXT, CF_UNICODETEXT, CF_DIB, CF_DIBV5,
            *self._registered_formats(),
        }

    def _open(self, timeout: float = 0.25) -> None:
        user32 = self._user32()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if user32.OpenClipboard(None):
                return
            time.sleep(0.01)
        raise OSError("clipboard is busy")

    def sequence(self) -> int:
        if not self.is_windows:
            return 0
        user32 = self._user32()
        user32.GetClipboardSequenceNumber.restype = ctypes.c_uint
        return int(user32.GetClipboardSequenceNumber())

    def _read_global_memory(self, handle) -> bytes | None:
        if not handle:
            return None
        kernel32 = self._kernel32()
        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        size = int(kernel32.GlobalSize(handle) or 0)
        if not size or size > MAX_FORMAT_BYTES:
            return None
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, size)
        finally:
            kernel32.GlobalUnlock(handle)

    def snapshot(self) -> ClipboardSnapshot | None:
        if not self.is_windows:
            return None
        user32 = self._user32()
        user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
        user32.EnumClipboardFormats.restype = ctypes.c_uint
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        supported = self._supported_formats()
        self._open()
        formats = []
        restorable = True
        try:
            format_id = 0
            while True:
                format_id = int(user32.EnumClipboardFormats(format_id))
                if not format_id:
                    break
                if format_id not in supported:
                    # We cannot safely recreate arbitrary clipboard objects.
                    restorable = False
                    continue
                data = self._read_global_memory(user32.GetClipboardData(format_id))
                if data is not None:
                    formats.append(ClipboardFormat(format_id, data))
                else:
                    # A supported format that cannot be copied (for example,
                    # an oversized DIB) makes the snapshot incomplete.
                    restorable = False
        finally:
            user32.CloseClipboard()
        return ClipboardSnapshot(tuple(formats), self.sequence(), restorable)

    def text(self) -> str | None:
        snapshot = self.snapshot()
        return snapshot.text if snapshot is not None else None

    def _allocate_global_memory(self, data: bytes):
        kernel32 = self._kernel32()
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise OSError("could not allocate clipboard memory")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            raise OSError("could not lock clipboard memory")
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        return handle

    def _restore_open_clipboard(self, snapshot: ClipboardSnapshot, user32) -> bool:
        """Restore a snapshot while ``user32.OpenClipboard`` is held."""
        if not snapshot.restorable:
            return False
        user32.EmptyClipboard.restype = ctypes.c_int
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        handles = []
        if not user32.EmptyClipboard():
            raise OSError("could not clear clipboard")
        try:
            for item in snapshot.formats:
                handle = self._allocate_global_memory(item.data)
                handles.append(handle)
                if not user32.SetClipboardData(item.format_id, handle):
                    raise OSError("could not restore clipboard format")
                handles.pop()
            return True
        finally:
            for handle in handles:
                self._kernel32().GlobalFree(handle)

    def write_text(self, text: str) -> bool:
        if not self.is_windows:
            return False
        data = (str(text) + "\x00").encode("utf-16-le")
        handle = self._allocate_global_memory(data)
        user32 = self._user32()
        user32.EmptyClipboard.restype = ctypes.c_int
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        transferred = False
        self._open()
        try:
            if not user32.EmptyClipboard():
                raise OSError("could not clear clipboard")
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                raise OSError("could not set clipboard text")
            transferred = True
            return True
        finally:
            user32.CloseClipboard()
            if not transferred:
                self._kernel32().GlobalFree(handle)

    def restore(self, snapshot: ClipboardSnapshot | None) -> bool:
        if (not self.is_windows or snapshot is None
                or not snapshot.restorable):
            return False
        user32 = self._user32()
        self._open()
        try:
            return self._restore_open_clipboard(snapshot, user32)
        finally:
            user32.CloseClipboard()

    def restore_if_owned(self, snapshot: ClipboardSnapshot | None,
            expected_sequence: int, expected_text: str) -> bool:
        """Compare ownership and restore atomically under one clipboard lock."""
        if (not self.is_windows or snapshot is None
                or not snapshot.restorable):
            return False
        user32 = self._user32()
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        self._open()
        try:
            if self.sequence() != expected_sequence:
                return False
            format_id = CF_UNICODETEXT
            data = self._read_global_memory(user32.GetClipboardData(format_id))
            if data is None:
                format_id = CF_TEXT
                data = self._read_global_memory(user32.GetClipboardData(format_id))
            if expected_text is not None:
                if data is None:
                    return False
                current_text = ClipboardSnapshot(
                    (ClipboardFormat(format_id, data),), 0).text
                if current_text != expected_text:
                    return False
            return self._restore_open_clipboard(snapshot, user32)
        finally:
            user32.CloseClipboard()
