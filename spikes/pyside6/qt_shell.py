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
import threading
from collections.abc import Callable, Mapping
from ctypes import wintypes
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from PySide6.QtCore import (
    QAbstractNativeEventFilter,
    QEvent,
    QLockFile,
    QObject,
    Signal,
    Slot,
)
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from hotkey_config import HotkeyAction, HotkeySettings
from windows_hotkeys import (
    ESCAPE_HOTKEY_ID,
    WM_HOTKEY,
    action_for_hotkey_id,
    register_escape_hotkey,
    register_global_hotkeys,
    unregister_escape_hotkey,
    unregister_global_hotkeys,
)


DEFAULT_INSTANCE_NAME = "clarifyvoice"
ACTIVATION_EVENT_NAME = r"Local\ClarifyVoice.ShowExisting.v1"
_WINDOWS_NATIVE_EVENT_TYPES = frozenset(
    {"windows_generic_MSG", "windows_dispatcher_MSG"}
)
_UNSUPPORTED_SHELL_HOTKEY_ACTIONS = frozenset(
    {
        HotkeyAction.REWRITE,
        HotkeyAction.TRANSLATION,
        HotkeyAction.VOICE_TRANSLATION,
    }
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

    def installEventFilter(self, filter_object: QObject) -> None: ...

    def removeEventFilter(self, filter_object: QObject) -> None: ...

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


class _WindowsSingleInstanceActivationApi:
    """Small Win32 named-event boundary shared by primary and secondary runs."""

    WAIT_OBJECT_0 = 0
    INFINITE = 0xFFFFFFFF

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32

    def create_event(self, name: str) -> Any:
        return self._kernel32.CreateEventW(None, False, False, name)

    def set_event(self, handle: Any) -> bool:
        return bool(self._kernel32.SetEvent(handle))

    def wait_for_event(self, handle: Any) -> int:
        return int(self._kernel32.WaitForSingleObject(handle, self.INFINITE))

    def close(self, handle: Any) -> None:
        if handle:
            self._kernel32.CloseHandle(handle)


def _supported_shell_hotkey_settings(
    settings: HotkeySettings | Mapping[str, Any] | None,
) -> HotkeySettings:
    """Filter actions with no real QML runtime handler before registration.

    ``QtClipboardGateway`` cannot capture a selection yet, and the QML runtime
    does not implement voice translation in this slice.  These actions are
    deliberately unsupported here, so the Windows shell must not reserve
    their bindings until their boundaries exist.
    """

    configured = (
        settings
        if isinstance(settings, HotkeySettings)
        else HotkeySettings.from_mapping(settings)
    )
    supported = {
        action: definition
        for action, definition in configured.hotkeys.items()
        if action not in _UNSUPPORTED_SHELL_HOTKEY_ACTIONS
    }
    filtered = HotkeySettings(supported, configured.activation_mode)
    # HotkeySettings normally fills omitted actions with compatibility
    # defaults.  The shell boundary needs a closed set instead: passing those
    # defaults downstream would reserve unsupported global shortcuts again.
    object.__setattr__(filtered, "hotkeys", MappingProxyType(dict(supported)))
    return filtered


class QtSingleInstanceGuard:
    """Own one native Qt lock for the lifetime of a desktop process."""

    def __init__(
        self,
        name: str = DEFAULT_INSTANCE_NAME,
        *,
        lock_path: str | Path | None = None,
        lock_factory: Callable[[str], Any] = QLockFile,
        stale_lock_time_ms: int = 30_000,
        activation_api: Any | None = None,
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
        self._activation_api = activation_api
        self._activation_event: Any | None = None
        self._activation_listener_thread: threading.Thread | None = None
        self._activation_listening = False

    @property
    def is_primary(self) -> bool:
        """Whether this process currently owns the instance lock."""

        return self._acquired

    def acquire(self) -> bool:
        """Try to become the only process for this shell."""

        if self._acquired:
            return True
        activation_api = self._activation_event_api()
        activation_event = None
        if activation_api is not None:
            activation_event = activation_api.create_event(ACTIVATION_EVENT_NAME)
            if not activation_event:
                raise QtShellError(
                    "could not create the single-instance activation event"
                )

        try:
            acquired = bool(self._lock.tryLock(0))
        except BaseException:
            if activation_event is not None:
                activation_api.close(activation_event)
            raise

        if not acquired:
            if activation_event is not None:
                try:
                    activation_api.set_event(activation_event)
                finally:
                    activation_api.close(activation_event)
            return False

        self._activation_event = activation_event
        self._acquired = True
        return True

    def release(self) -> None:
        """Release the lock owned by this process."""

        if not self._acquired:
            return
        self.stop_activation_listener()
        try:
            if self._activation_event is not None:
                self._activation_event_api().close(self._activation_event)
                self._activation_event = None
        finally:
            self._lock.unlock()
            self._acquired = False

    def start_activation_listener(self, callback: Callable[[], None]) -> None:
        """Notify a primary Qt shell when a later launch signals its event."""

        if (
            not self._acquired
            or self._activation_event is None
            or not callable(callback)
            or self._activation_listener_thread is not None
        ):
            return

        api = self._activation_event_api()
        handle = self._activation_event
        self._activation_listening = True

        def wait_loop() -> None:
            while self._activation_listening and self._activation_event == handle:
                result = api.wait_for_event(handle)
                if result != api.WAIT_OBJECT_0:
                    return
                if self._activation_listening:
                    callback()

        worker = threading.Thread(
            target=wait_loop,
            name="ClarifyVoiceSingleInstanceActivation",
            daemon=True,
        )
        self._activation_listener_thread = worker
        worker.start()

    def stop_activation_listener(self) -> None:
        """Stop dispatching activation callbacks for this primary process."""

        self._activation_listening = False
        worker = self._activation_listener_thread
        if worker is None:
            return
        if worker is threading.current_thread():
            self._activation_listener_thread = None
            return

        handle = self._activation_event
        if handle is not None:
            self._activation_event_api().set_event(handle)
        worker.join()
        self._activation_listener_thread = None

    def _activation_event_api(self) -> Any | None:
        if self._activation_api is None and sys.platform == "win32":
            self._activation_api = _WindowsSingleInstanceActivationApi()
        return self._activation_api

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
        register_escape: Callable[..., bool] = register_escape_hotkey,
        unregister_escape: Callable[..., None] = unregister_escape_hotkey,
        event_filter_factory: Callable[
            ..., WindowsHotkeyEventFilter
        ] = WindowsHotkeyEventFilter,
        message_decoder: Callable[[Any, Any], int | None] | None = None,
        action_for_id: Callable[[int], str | None] = action_for_hotkey_id,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._event_target = event_target
        self._settings = _supported_shell_hotkey_settings(settings)
        self._user32 = user32
        self._register_hotkeys = register_hotkeys
        self._unregister_hotkeys = unregister_hotkeys
        self._register_escape = register_escape
        self._unregister_escape = unregister_escape
        self._event_filter_factory = event_filter_factory
        self._message_decoder = message_decoder
        self._action_for_id = action_for_id
        self._event_filter: QAbstractNativeEventFilter | None = None
        self._registered: set[int] = set()
        self._hwnd: int | None = None
        self._recording_active = False
        self._escape_registered = False

    @property
    def is_running(self) -> bool:
        return self._event_filter is not None

    @property
    def registered_ids(self) -> frozenset[int]:
        registered = set(self._registered)
        if self._escape_registered:
            registered.add(ESCAPE_HOTKEY_ID)
        return frozenset(registered)

    def set_recording_active(self, active: bool) -> None:
        """Keep the global Escape binding aligned with recording state."""

        self._recording_active = bool(active)
        if self.is_running:
            self._set_escape_hotkey(self._recording_active)

    def start(self, window: WindowTarget) -> set[int]:
        """Register configured shortcuts against the QML window handle."""

        if self.is_running:
            return set(self._registered)

        hwnd = self._window_handle(window)
        user32 = self._user32 if self._user32 is not None else _load_user32()
        registered: set[int] = set()
        event_filter: QAbstractNativeEventFilter | None = None
        event_filter_installed = False
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
            event_filter_installed = True
            self._hwnd = hwnd
            self._registered = registered
            self._event_filter = event_filter
            self._set_escape_hotkey(self._recording_active)
        except BaseException:
            try:
                self._set_escape_hotkey(False)
            finally:
                try:
                    if event_filter_installed and event_filter is not None:
                        self._event_target.removeNativeEventFilter(event_filter)
                finally:
                    self._unregister_hotkeys(user32, hwnd, registered)
                    self._registered.clear()
                    self._hwnd = None
                    self._event_filter = None
            raise

        return set(self.registered_ids)

    def stop(self) -> None:
        """Remove the native filter and unregister every active shortcut."""

        event_filter = self._event_filter
        if event_filter is None:
            return

        user32 = self._user32 if self._user32 is not None else _load_user32()
        try:
            self._event_target.removeNativeEventFilter(event_filter)
        finally:
            try:
                self._set_escape_hotkey(False)
            finally:
                self._unregister_hotkeys(user32, self._hwnd, self._registered)
                self._registered.clear()
                self._recording_active = False
                self._hwnd = None
                self._event_filter = None

    def _set_escape_hotkey(self, enabled: bool) -> None:
        if self._hwnd is None:
            return
        user32 = self._user32 if self._user32 is not None else _load_user32()
        if enabled:
            if self._escape_registered:
                return
            self._escape_registered = bool(self._register_escape(user32, self._hwnd))
            return
        if not self._escape_registered:
            return
        try:
            self._unregister_escape(user32, self._hwnd)
        finally:
            self._escape_registered = False

    def _handle_hotkey_id(self, hotkey_id: int) -> None:
        if int(hotkey_id) not in self.registered_ids:
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
    activationRequested = Signal()
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
        self._hotkeys_started = False
        self._window_close_filter_installed = False
        self.activationRequested.connect(self.show_window)

    @property
    def is_running(self) -> bool:
        return self._started

    @property
    def tray(self) -> Any | None:
        return self._tray

    def start(self, *, tray_available: bool = True) -> bool:
        """Start the guard and optional tray/hotkey resources.

        The single-instance guard is intentionally acquired even when the
        desktop has no system tray.  Only tray construction is optional.
        """

        if self._started:
            return True
        if not self._instance_guard.acquire():
            return False

        hotkeys_start_attempted = False
        try:
            self._instance_guard.start_activation_listener(self._request_activation)
            if tray_available:
                self._create_tray()
                self._install_window_close_filter()
            if self._hotkeys is not None:
                if not self._hotkeys_connected:
                    self._hotkeys.triggered.connect(self._handle_hotkey)
                    self._hotkeys_connected = True
                hotkeys_start_attempted = True
                self._hotkeys.start(self._window)
                self._hotkeys_started = True
        except BaseException:
            try:
                if self._hotkeys is not None and hotkeys_start_attempted:
                    self._hotkeys.stop()
            finally:
                self._hotkeys_started = False
                self._remove_window_close_filter()
                self._cleanup_tray()
                self._started = False
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
            if self._hotkeys is not None and self._hotkeys_started:
                self._hotkeys.stop()
        finally:
            self._hotkeys_started = False
            self._remove_window_close_filter()
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

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Hide a tray-owned QWindow instead of destroying its native handle."""

        if (
            watched is self._window
            and self._tray is not None
            and event.type() == QEvent.Type.Close
        ):
            event.ignore()
            self.hide_window()
            return True
        return bool(super().eventFilter(watched, event))

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

    def _install_window_close_filter(self) -> None:
        if self._window_close_filter_installed:
            return
        self._window.installEventFilter(self)
        self._window_close_filter_installed = True

    def _remove_window_close_filter(self) -> None:
        if not self._window_close_filter_installed:
            return
        try:
            self._window.removeEventFilter(self)
        finally:
            self._window_close_filter_installed = False

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

    def _request_activation(self) -> None:
        """Queue a primary-window activation on Qt's GUI thread."""

        self.activationRequested.emit()


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
    "ACTIVATION_EVENT_NAME",
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
