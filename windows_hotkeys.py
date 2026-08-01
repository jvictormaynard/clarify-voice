"""Native Windows global-hotkey and synthetic-key helpers for ClarifyVoice."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


WM_HOTKEY = 0x0312
WM_PASTE = 0x0302
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
EM_GETSEL = 0x00B0
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002
MOD_ALT = 0x0001
MOD_NOREPEAT = 0x4000
VK_MENU = 0x12
VK_ESCAPE = 0x1B
ESCAPE_HOTKEY_ID = 0x5105

HOTKEY_SPECS = {
    0x5101: ("recording_hotkey", ord("L")),
    0x5102: ("rewrite_hotkey", ord("K")),
    0x5103: ("translation_hotkey", ord("T")),
    0x5104: ("toggle_visibility", ord("R")),
}

_CONFIRMABLE_PASTE_CLASSES = frozenset({
    "Edit",
    "RichEdit20A",
    "RichEdit20W",
    "RICHEDIT50A",
    "RICHEDIT50W",
    "RICHEDIT60A",
    "RICHEDIT60W",
})
_MAX_CONFIRMABLE_CONTROL_TEXT = 4 * 1024 * 1024


class _GUIThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
    ]


def action_for_hotkey_id(hotkey_id: int) -> str | None:
    if int(hotkey_id) == ESCAPE_HOTKEY_ID:
        return "escape"
    spec = HOTKEY_SPECS.get(int(hotkey_id))
    return spec[0] if spec else None


def register_global_hotkeys(user32, hwnd) -> set[int]:
    """Register ClarifyVoice's Alt shortcuts and return successful IDs."""
    user32.RegisterHotKey.argtypes = [
        wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    registered: set[int] = set()
    modifiers = MOD_ALT | MOD_NOREPEAT
    for hotkey_id, (_action, virtual_key) in HOTKEY_SPECS.items():
        if user32.RegisterHotKey(hwnd, hotkey_id, modifiers, virtual_key):
            registered.add(hotkey_id)
    return registered


def unregister_global_hotkeys(user32, hwnd, registered: set[int]) -> None:
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    for hotkey_id in tuple(registered):
        user32.UnregisterHotKey(hwnd, hotkey_id)


def register_escape_hotkey(user32, hwnd) -> bool:
    """Capture Escape without modifiers for the active recording session only."""
    user32.RegisterHotKey.argtypes = [
        wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    return bool(user32.RegisterHotKey(
        hwnd, ESCAPE_HOTKEY_ID, MOD_NOREPEAT, VK_ESCAPE))


def unregister_escape_hotkey(user32, hwnd) -> None:
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey(hwnd, ESCAPE_HOTKEY_ID)


def is_alt_pressed() -> bool:
    """Read the physical Alt state without installing a keyboard hook."""
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_MENU) & 0x8000)  # type: ignore[attr-defined]


def _focused_control_for_foreground(user32):
    """Return the focused child of the current foreground window, if any."""
    user32.GetForegroundWindow.restype = wintypes.HWND
    foreground = user32.GetForegroundWindow()
    if not foreground:
        return None

    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    thread_id = user32.GetWindowThreadProcessId(foreground, None)
    if not thread_id:
        return None

    info = _GUIThreadInfo()
    info.cbSize = ctypes.sizeof(info)
    user32.GetGUIThreadInfo.argtypes = [
        wintypes.DWORD, ctypes.POINTER(_GUIThreadInfo)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        return None
    focused = info.hwndFocus or info.hwndActive
    if not focused:
        return None
    if focused != foreground:
        user32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
        user32.IsChild.restype = wintypes.BOOL
        if not user32.IsChild(foreground, focused):
            return None
    return focused


def _window_class_name(user32, hwnd) -> str | None:
    user32.GetClassNameW.argtypes = [
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(256)
    length = user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value if length else None


def _send_message_timeout(user32, hwnd, message, wparam, lparam, timeout_ms):
    """Send a control query with the same bounded timeout as WM_PASTE."""
    result = ctypes.c_size_t()
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = wintypes.BOOL
    try:
        delivered = user32.SendMessageTimeoutW(
            hwnd, message, wparam, lparam,
            SMTO_BLOCK | SMTO_ABORTIFHUNG, int(timeout_ms),
            ctypes.byref(result))
    except (AttributeError, OSError, TypeError):
        return None
    return result.value if delivered else None


def _control_state(user32, hwnd, timeout_ms):
    """Read plain text and selection bounds from a standard text control."""
    length_result = _send_message_timeout(
        user32, hwnd, WM_GETTEXTLENGTH, 0, 0, timeout_ms)
    if length_result is None:
        return None
    length = int(length_result)
    if length < 0 or length > _MAX_CONFIRMABLE_CONTROL_TEXT:
        return None
    buffer = ctypes.create_unicode_buffer(length + 1)
    copied = _send_message_timeout(
        user32, hwnd, WM_GETTEXT, length + 1,
        ctypes.addressof(buffer), timeout_ms)
    if copied is None or copied < 0:
        return None

    start = ctypes.c_int()
    end = ctypes.c_int()
    selection_result = _send_message_timeout(
        user32, hwnd, EM_GETSEL,
        ctypes.addressof(start), ctypes.addressof(end), timeout_ms)
    if selection_result is None:
        return None
    if start.value < 0 or end.value < start.value or end.value > length:
        return None
    return buffer.value, start.value, end.value


def _paste_result_matches(before, after, expected_text: str) -> bool:
    if before is None or after is None:
        return False
    before_text, start, end = before
    after_text, after_start, after_end = after
    # EM_GETSEL counts UTF-16 code units; avoid guessing around surrogate
    # pairs when Python's string indexes use Unicode code points.
    if len(before_text.encode("utf-16-le")) // 2 != len(before_text):
        return False
    expected_variants = {
        str(expected_text),
        str(expected_text).replace("\n", "\r\n"),
    }
    suffix = before_text[end:]
    prefix = before_text[:start]
    for inserted in expected_variants:
        if after_text != prefix + inserted + suffix:
            continue
        expected_caret = start + len(inserted.encode("utf-16-le")) // 2
        if after_start == after_end == expected_caret:
            return True
    return False


def paste_focused_control(expected_text: str | None = None,
        timeout_ms: int = 250, *, user32=None) -> bool | None:
    """Paste synchronously into a compatible focused control when possible.

    ``SendMessageTimeoutW`` only proves that a standard text control returned
    from its window procedure. When the expected text is supplied, confirmation
    additionally requires the control's text and caret to match the predicted
    insertion. Custom, read-only, limited, and otherwise unobservable controls
    fall back to key injection without claiming consumption.
    """
    if user32 is None:
        user32 = ctypes.windll.user32
    try:
        focused = _focused_control_for_foreground(user32)
        class_name = _window_class_name(user32, focused) if focused else None
        if (expected_text is not None and focused is not None
                and class_name in _CONFIRMABLE_PASTE_CLASSES):
            before = _control_state(user32, focused, timeout_ms)
            if before is None:
                return send_ctrl_key("v")
            try:
                delivered = _send_message_timeout(
                    user32, focused, WM_PASTE, 0, 0, timeout_ms)
            except (AttributeError, OSError, TypeError):
                # The message may have crossed the process boundary before
                # the API reported an error; never inject a second paste.
                return None
            if delivered is not None:
                try:
                    after = _control_state(user32, focused, timeout_ms)
                except (AttributeError, OSError, TypeError):
                    return None
                if _paste_result_matches(before, after, expected_text):
                    return True
            # WM_PASTE was attempted. A timeout or an unverifiable/malformed
            # state must not be followed by Ctrl+V, which could double-paste
            # if the original message was delivered late.
            return None
    except (AttributeError, OSError, TypeError):
        pass
    return send_ctrl_key("v")


def send_ctrl_key(letter: str) -> bool | None:
    """Inject Ctrl plus one ASCII letter without claiming paste consumption."""
    virtual_key = ord(str(letter).upper())
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    key_up = 0x0002
    try:
        user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
        user32.keybd_event(virtual_key, 0, 0, 0)
        user32.keybd_event(virtual_key, 0, key_up, 0)
        user32.keybd_event(0x11, 0, key_up, 0)  # Ctrl up
        # keybd_event is void; successful injection is not proof that the
        # target consumed Ctrl+V. Returning None keeps the result available.
        return None
    except OSError:
        return False
