"""Native Windows global-hotkey and synthetic-key helpers for ClarifyVoice."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


WM_HOTKEY = 0x0312
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
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_MENU) & 0x8000)


def send_ctrl_key(letter: str) -> None:
    """Send Ctrl plus one ASCII letter with the lightweight Win32 API."""
    virtual_key = ord(str(letter).upper())
    user32 = ctypes.windll.user32
    key_up = 0x0002
    user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
    user32.keybd_event(virtual_key, 0, 0, 0)
    user32.keybd_event(virtual_key, 0, key_up, 0)
    user32.keybd_event(0x11, 0, key_up, 0)  # Ctrl up
