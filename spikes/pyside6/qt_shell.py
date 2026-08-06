"""Native Qt desktop-shell services for the QML frontend.

The module owns process-level shell concerns that do not belong in QML:
single-instance ownership, tray actions, and the Windows global-hotkey bridge.
It has no dependency on a frontend entrypoint, so the QML application can
compose it without importing another UI toolkit.
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QAbstractNativeEventFilter, QLockFile, QObject, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from hotkey_config import HotkeyAction, HotkeySettings
from windows_hotkeys import (
    WM_HOTKEY,
    action_for_hotkey_id,
    register_global_hotkeys,
    unregister_global_hotkeys,
)


DEFAULT_INSTANCE_NAME = "clarifyvoice"
_WINDOWS_NATIVE_EVENT_TYPES = frozenset(
    {"windows_generic_MSG", "windows_dispatcher_MSG"}
)


class QtShellError(RuntimeError):
    """The native Qt shell could not be started."""


class HotkeyPlatformError(QtShellError):
    """The Windows hotkey backend was started without a Windows API seam."""


class NativeEventTarget(Protocol):
    """Qt application surface that accepts native event filters."""

    def installNativeEventFilter(
        self, filter_object: QAbstractNativeEventFilter
    ) -> None: ...

    def removeNativeEventFilter(
        self, filter_object: QAbstractNativeEventFilter
    ) -> None: ...


class WindowTarget(Protocol):
    """Small window contract used by tray and hotkey shell actions."""

    def show(self) -> None: ...

    def hide(self) -> None: ...

    def isVisible(self) -> bool: ...

    def raise_(self) -> None: ...

    def requestActivate(self) -> None: ...


class GlobalHotkeyBackend(Protocol):
    """Injectable lifecycle boundary for shell global shortcuts."""

    triggered: Any

    def start(self, window: WindowTarget) -> set[int]: ...

    def stop(self) -> None: ...


class QtSingleInstanceGuard:
    """Own one native Qt lock for the lifetime of a desktop process."""

    def __init__(
        self,
        name: str = DEFAULT_INSTANCE_NAME,
        *,
        lock_path: str | Path | None = None,
        lock_factory: Callable[[str], Any] = QLockFile,
        stale_lock_time_ms: int = 30_000,
    ) -> None:
        normalized_name = str(name).strip()
        if not normalized_name:
            raise ValueError("The instance name cannot be empty")

        if lock_path is None:
            filename = "".join(
                character if character.isalnum() or character in "-_" else "_"
                for character in normalized_name
            )
            lock_path = Path(tempfile.gettempdir()) / f"{filename}.lock"

        self.name = normalized_name
        self.path = Path(lock_path)
        self._lock = lock_factory(str(self.path))
        self._lock.setStaleLockTime(int(stale_lock_time_ms))
        self._acquired = False

    @property
    def is_primary(self) -> bool:
        """Whether this process currently owns the instance lock."""

        return self._acquired

    def acquire(self) -> bool:
        """Try to become the only process for this shell."""

        if self._acquired:
            return True
        self._acquired = bool(self._lock.tryLock(0))
        return self._acquired

    def release(self) -> None:
        """Release the lock owned by this process."""

        if not self._acquired:
            return
        self._lock.unlock()
        self._acquired = False

    def __enter__(self) -> "QtSingleInstanceGuard":
        if not self.acquire():
            raise QtShellError(f"Another {self.name} instance is already running")
        return self

    def __exit__(self, _exception_type, _exception, _traceback) -> None:
        self.release()


class WindowsHotkeyEventFilter(QAbstractNativeEventFilter):
    """Decode WM_HOTKEY messages and forward their canonical action names."""

    def __init__(
        self,
        on_hotkey_id: Callable[[int], None],
        *,
        message_decoder: Callable[[Any, Any], int | None] | None = None,
        action_for_id: Callable[[int], str | None] = action_for_hotkey_id,
    ) -> None:
        super().__init__()
        self._on_hotkey_id = on_hotkey_id
        self._message_decoder = message_decoder or decode_windows_hotkey_message
        self._action_for_id = action_for_id

    def nativeEventFilter(self, event_type: Any, message: Any) -> tuple[bool, int]:
        """Observe a native event without consuming it from Qt."""

        hotkey_id = self._message_decoder(event_type, message)
        if hotkey_id is not None and self._action_for_id(int(hotkey_id)) is not None:
            self._on_hotkey_id(int(hotkey_id))
        return False, 0


class WindowsGlobalHotkeyBackend(QObject):
    """Native RegisterHotKey adapter with injectable OS and Qt seams."""

    triggered = Signal(str)

    def __init__(
        self,
        event_target: NativeEventTarget,
        *,
        settings: HotkeySettings | Mapping[str, Any] | None = None,
        user32: Any | None = None,
        register_hotkeys: Callable[..., set[int]] = register_global_hotkeys,
        unregister_hotkeys: Callable[..., None] = unregister_global_hotkeys,
        event_filter_factory: Callable[
            ..., WindowsHotkeyEventFilter
        ] = WindowsHotkeyEventFilter,
        message_decoder: Callable[[Any, Any], int | None] | None = None,
        action_for_id: Callable[[int], str | None] = action_for_hotkey_id,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._event_target = event_target
        self._settings = settings
        self._user32 = user32
        self._register_hotkeys = register_hotkeys
        self._unregister_hotkeys = unregister_hotkeys
        self._event_filter_factory = event_filter_factory
        self._message_decoder = message_decoder
        self._action_for_id = action_for_id
        self._event_filter: QAbstractNativeEventFilter | None = None
        self._registered: set[int] = set()
        self._hwnd: int | None = None

    @property
    def is_running(self) -> bool:
        return self._event_filter is not None

    @property
    def registered_ids(self) -> frozenset[int]:
        return frozenset(self._registered)

    def start(self, window: WindowTarget) -> set[int]:
        """Register configured shortcuts against the QML window handle."""

        if self.is_running:
            return set(self._registered)

        hwnd = self._window_handle(window)
        user32 = self._user32 if self._user32 is not None else _load_user32()
        registered: set[int] = set()
        try:
            registered = set(
                self._register_hotkeys(
                    user32,
                    hwnd,
                    self._settings,
                    strict=True,
                )
            )
            event_filter = self._event_filter_factory(
                self._handle_hotkey_id,
                message_decoder=self._message_decoder,
                action_for_id=self._action_for_id,
            )
            self._event_target.installNativeEventFilter(event_filter)
        except BaseException:
            self._unregister_hotkeys(user32, hwnd, registered)
            raise

        self._hwnd = hwnd
        self._registered = registered
        self._event_filter = event_filter
        return set(self._registered)

    def stop(self) -> None:
        """Remove the native filter and unregister every active shortcut."""

        event_filter = self._event_filter
        if event_filter is None:
            return

        user32 = self._user32 if self._user32 is not None else _load_user32()
        try:
            self._event_target.removeNativeEventFilter(event_filter)
        finally:
            self._unregister_hotkeys(user32, self._hwnd, self._registered)
            self._registered.clear()
            self._hwnd = None
            self._event_filter = None

    def _handle_hotkey_id(self, hotkey_id: int) -> None:
        if int(hotkey_id) not in self._registered:
            return
        action = self._action_for_id(int(hotkey_id))
        if action is not None:
            self.triggered.emit(action)

    @staticmethod
    def _window_handle(window: WindowTarget) -> int:
        handle = window.winId()  # type: ignore[attr-defined]
        if not handle:
            raise QtShellError("The QML window has no native handle")
        return int(handle)


class QtShell(QObject):
    """Compose the QML window with instance, tray, and hotkey ownership."""

    started = Signal()
    stopped = Signal()
    hotkeyTriggered = Signal(str)
    quitRequested = Signal()

    def __init__(
        self,
        window: WindowTarget,
        *,
        instance_guard: QtSingleInstanceGuard | None = None,
        hotkeys: GlobalHotkeyBackend | None = None,
        application: Any | None = None,
        tray_icon_factory: Callable[[QIcon, QObject | None], Any] = QSystemTrayIcon,
        menu_factory: Callable[[QObject | None], Any] = QMenu,
        icon: QIcon | None = None,
        title: str = "ClarifyVoice",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._window = window
        self._instance_guard = instance_guard or QtSingleInstanceGuard()
        self._hotkeys = hotkeys
        self._application = application
        self._tray_icon_factory = tray_icon_factory
        self._menu_factory = menu_factory
        self._icon = icon or QIcon()
        self._title = str(title)
        self._tray: Any | None = None
        self._menu: Any | None = None
        self._started = False
        self._hotkeys_connected = False

    @property
    def is_running(self) -> bool:
        return self._started

    @property
    def tray(self) -> Any | None:
        return self._tray

    def start(self) -> bool:
        """Start shell-owned resources if this process is the primary one."""

        if self._started:
            return True
        if not self._instance_guard.acquire():
            return False

        hotkeys_started = False
        try:
            self._create_tray()
            if self._hotkeys is not None:
                if not self._hotkeys_connected:
                    self._hotkeys.triggered.connect(self._handle_hotkey)
                    self._hotkeys_connected = True
                self._hotkeys.start(self._window)
                hotkeys_started = True
        except BaseException:
            try:
                if self._hotkeys is not None and hotkeys_started:
                    self._hotkeys.stop()
            finally:
                self._cleanup_tray()
                self._started = False
                self._instance_guard.release()
            raise

        self._started = True
        self.started.emit()
        return True

    def stop(self) -> None:
        """Release hotkeys, tray resources, and the single-instance lock."""

        if not self._started and not self._instance_guard.is_primary:
            return

        self._started = False
        try:
            if self._hotkeys is not None:
                self._hotkeys.stop()
        finally:
            self._cleanup_tray()
            self._instance_guard.release()
            self.stopped.emit()

    @Slot()
    def show_window(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.requestActivate()

    @Slot()
    def hide_window(self) -> None:
        self._window.hide()

    @Slot()
    def toggle_window(self) -> None:
        if self._window.isVisible():
            self.hide_window()
        else:
            self.show_window()

    @Slot()
    def quit(self) -> None:
        self.stop()
        self.quitRequested.emit()
        if self._application is not None:
            self._application.quit()

    def _create_tray(self) -> None:
        tray = self._tray_icon_factory(self._icon, self)
        self._tray = tray
        menu = self._menu_factory(self)
        self._menu = menu
        show_action = menu.addAction(f"Show {self._title}")
        quit_action = menu.addAction(f"Quit {self._title}")
        show_action.triggered.connect(lambda _checked=False: self.show_window())
        quit_action.triggered.connect(lambda _checked=False: self.quit())
        tray.setToolTip(self._title)
        tray.setContextMenu(menu)
        tray.activated.connect(self._handle_tray_activation)
        tray.show()

    def _cleanup_tray(self) -> None:
        """Hide and release tray resources, including partially built ones."""

        menu = self._menu
        tray = self._tray
        self._menu = None
        self._tray = None

        for resource in (tray, menu):
            if resource is None:
                continue
            hide = getattr(resource, "hide", None)
            if callable(hide):
                try:
                    hide()
                except BaseException:
                    pass
            delete_later = getattr(resource, "deleteLater", None)
            if callable(delete_later):
                try:
                    delete_later()
                except BaseException:
                    pass

    def _handle_tray_activation(self, reason: Any) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_window()

    def _handle_hotkey(self, action: str) -> None:
        normalized = str(action)
        self.hotkeyTriggered.emit(normalized)
        if normalized == HotkeyAction.VISIBILITY.value:
            self.toggle_window()


class _NativeMessage(ctypes.Structure):
    """Prefix-compatible Windows MSG structure for native event decoding."""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint32),
        ("w_param", ctypes.c_size_t),
        ("l_param", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("point_x", ctypes.c_int32),
        ("point_y", ctypes.c_int32),
        ("private", ctypes.c_uint32),
    ]


def decode_windows_hotkey_message(event_type: Any, message: Any) -> int | None:
    """Decode a Qt Windows native message into a RegisterHotKey ID."""

    if _event_type_name(event_type) not in _WINDOWS_NATIVE_EVENT_TYPES:
        return None
    if sys.platform != "win32":
        return None
    try:
        native_message = _NativeMessage.from_address(int(message))
    except (TypeError, ValueError, OSError):
        return None
    if int(native_message.message) != WM_HOTKEY:
        return None
    return int(native_message.w_param)


def _event_type_name(event_type: Any) -> str:
    if isinstance(event_type, bytes):
        return event_type.decode(errors="replace")
    try:
        return bytes(event_type).decode(errors="replace")
    except (TypeError, ValueError):
        return str(event_type)


def _load_user32() -> Any:
    if sys.platform != "win32":
        raise HotkeyPlatformError(
            "WindowsGlobalHotkeyBackend requires a Windows user32 seam"
        )
    return ctypes.windll.user32  # type: ignore[attr-defined]


__all__ = [
    "DEFAULT_INSTANCE_NAME",
    "GlobalHotkeyBackend",
    "HotkeyPlatformError",
    "NativeEventTarget",
    "QtShell",
    "QtShellError",
    "QtSingleInstanceGuard",
    "WindowsGlobalHotkeyBackend",
    "WindowsHotkeyEventFilter",
    "WindowTarget",
    "decode_windows_hotkey_message",
]
