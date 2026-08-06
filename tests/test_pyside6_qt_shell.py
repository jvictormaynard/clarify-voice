"""Focused tests for the UI-free native Qt desktop shell."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from hotkey_config import HotkeyAction, HotkeySettings
from windows_hotkeys import ESCAPE_HOTKEY_ID

try:
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
    from PySide6.QtWidgets import QSystemTrayIcon
    from spikes.pyside6.qt_shell import (
        QtShell,
        QtSingleInstanceGuard,
        WindowsGlobalHotkeyBackend,
        WindowsHotkeyEventFilter,
    )

    PYSIDE6_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    PYSIDE6_AVAILABLE = False
    QObject = object

    def Signal(*_args, **_kwargs):
        return None


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "spikes" / "pyside6" / "qt_shell.py"


class FakeLock:
    locks: dict[str, "FakeLock"] = {}

    def __init__(self, path: str):
        self.path = path
        self.stale_lock_time = None
        self.locked = False

    def setStaleLockTime(self, value: int) -> None:
        self.stale_lock_time = value

    def tryLock(self, _timeout: int) -> bool:
        owner = self.locks.get(self.path)
        if owner is not None and owner is not self:
            return False
        self.locks[self.path] = self
        self.locked = True
        return True

    def unlock(self) -> None:
        if self.locks.get(self.path) is self:
            del self.locks[self.path]
        self.locked = False


class FakeWindow:
    def __init__(self, *, visible: bool = False, handle: int = 123):
        self.visible = visible
        self.handle = handle
        self.calls: list[str] = []
        self.event_filters = []
        self.removed_event_filters = []

    def installEventFilter(self, filter_object) -> None:
        self.event_filters.append(filter_object)

    def removeEventFilter(self, filter_object) -> None:
        self.removed_event_filters.append(filter_object)

    def winId(self) -> int:
        return self.handle

    def show(self) -> None:
        self.visible = True
        self.calls.append("show")

    def hide(self) -> None:
        self.visible = False
        self.calls.append("hide")

    def isVisible(self) -> bool:
        return self.visible

    def raise_(self) -> None:
        self.calls.append("raise")

    def requestActivate(self) -> None:
        self.calls.append("activate")


class FakeNativeEventTarget:
    def __init__(self):
        self.installed = []
        self.removed = []

    def installNativeEventFilter(self, filter_object) -> None:
        self.installed.append(filter_object)

    def removeNativeEventFilter(self, filter_object) -> None:
        self.removed.append(filter_object)


class FakeAction:
    def __init__(self):
        self.triggered = SignalSpy()


class FakeMenu:
    def __init__(self, _parent, *, fail_on_add_action: bool = False):
        self.actions = []
        self.fail_on_add_action = fail_on_add_action
        self.delete_later_calls = 0

    def addAction(self, text: str) -> FakeAction:
        if self.fail_on_add_action:
            raise RuntimeError("menu configuration failed")
        action = FakeAction()
        self.actions.append((text, action))
        return action

    def deleteLater(self) -> None:
        self.delete_later_calls += 1


class FakeTray:
    def __init__(self, _icon, _parent):
        self.activated = SignalSpy()
        self.context_menu = None
        self.tooltip = None
        self.visible = False
        self.hide_calls = 0
        self.delete_later_calls = 0
        self.fail_on_tooltip = False

    def setToolTip(self, value: str) -> None:
        if self.fail_on_tooltip:
            raise RuntimeError("tray configuration failed")
        self.tooltip = value

    def setContextMenu(self, menu) -> None:
        self.context_menu = menu

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False
        self.hide_calls += 1

    def deleteLater(self) -> None:
        self.delete_later_calls += 1


class SignalSpy:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in tuple(self.callbacks):
            callback(*args)


class FakeHotkeys(QObject):
    triggered = Signal(str)

    def __init__(self):
        super().__init__()
        self.started_with = None
        self.stopped = False
        self.stop_calls = 0
        self.is_running = False

    def start(self, window) -> set[int]:
        self.started_with = window
        self.is_running = True
        return {1}

    def stop(self) -> None:
        self.stopped = True
        self.stop_calls += 1
        self.is_running = False


class FailingHotkeys(FakeHotkeys):
    def start(self, window) -> set[int]:
        super().start(window)
        raise RuntimeError("hotkey setup failed")


class FakeApplication:
    def __init__(self):
        self.quit_calls = 0

    def quit(self) -> None:
        self.quit_calls += 1


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional QML dependency")
class QtSingleInstanceGuardTests(unittest.TestCase):
    def setUp(self):
        FakeLock.locks.clear()

    def test_only_one_guard_can_own_a_name(self):
        first = QtSingleInstanceGuard(
            "clarifyvoice-test",
            lock_path=Path(tempfile.gettempdir()) / "clarifyvoice-test.lock",
            lock_factory=FakeLock,
        )
        second = QtSingleInstanceGuard(
            "clarifyvoice-test",
            lock_path=Path(tempfile.gettempdir()) / "clarifyvoice-test.lock",
            lock_factory=FakeLock,
        )

        self.assertTrue(first.acquire())
        self.assertTrue(first.is_primary)
        self.assertFalse(second.acquire())
        self.assertFalse(second.is_primary)

        first.release()
        self.assertTrue(second.acquire())
        second.release()


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional QML dependency")
class WindowsHotkeyEventFilterTests(unittest.TestCase):
    def test_filter_routes_decoded_hotkey_without_consuming_event(self):
        received = []
        event_filter = WindowsHotkeyEventFilter(
            received.append,
            message_decoder=lambda _event_type, _message: 0x5101,
            action_for_id=lambda hotkey_id: (
                "recording_hotkey" if hotkey_id == 0x5101 else None
            ),
        )

        result = event_filter.nativeEventFilter(b"windows_generic_MSG", object())

        self.assertEqual(received, [0x5101])
        self.assertEqual(result, (False, 0))


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional QML dependency")
class WindowsGlobalHotkeyBackendTests(unittest.TestCase):
    def test_backend_registers_filters_and_emits_canonical_action(self):
        target = FakeNativeEventTarget()
        window = FakeWindow()
        registrations = []
        unregistrations = []

        def register(user32, hwnd, settings, *, strict):
            registrations.append((user32, hwnd, settings, strict))
            return {0x5101}

        def unregister(user32, hwnd, registered):
            unregistrations.append((user32, hwnd, set(registered)))

        backend = WindowsGlobalHotkeyBackend(
            target,
            user32="fake-user32",
            register_hotkeys=register,
            unregister_hotkeys=unregister,
            message_decoder=lambda _event_type, _message: 0x5101,
        )
        received = []
        backend.triggered.connect(received.append)

        self.assertEqual(backend.start(window), {0x5101})
        self.assertEqual(registrations[0][0], "fake-user32")
        self.assertEqual(registrations[0][1], 123)
        self.assertTrue(registrations[0][3])
        self.assertIsInstance(registrations[0][2], HotkeySettings)
        self.assertNotIn(HotkeyAction.VOICE_TRANSLATION, registrations[0][2].hotkeys)
        self.assertEqual(len(target.installed), 1)

        target.installed[0].nativeEventFilter(b"windows_generic_MSG", object())
        self.assertEqual(received, ["recording_hotkey"])

        backend.stop()
        self.assertEqual(target.removed, target.installed)
        self.assertEqual(unregistrations, [("fake-user32", 123, {0x5101})])
        self.assertFalse(backend.is_running)

    def test_backend_registers_escape_only_while_recording(self):
        target = FakeNativeEventTarget()
        escape_registrations = []
        escape_unregistrations = []

        def register_escape(user32, hwnd):
            escape_registrations.append((user32, hwnd))
            return True

        def unregister_escape(user32, hwnd):
            escape_unregistrations.append((user32, hwnd))

        backend = WindowsGlobalHotkeyBackend(
            target,
            user32="fake-user32",
            register_hotkeys=lambda *_args, **_kwargs: {0x5101},
            unregister_hotkeys=lambda *_args, **_kwargs: None,
            register_escape=register_escape,
            unregister_escape=unregister_escape,
            message_decoder=lambda _event_type, _message: ESCAPE_HOTKEY_ID,
        )
        received = []
        backend.triggered.connect(received.append)

        backend.start(FakeWindow())
        self.assertEqual(escape_registrations, [])
        self.assertNotIn(ESCAPE_HOTKEY_ID, backend.registered_ids)

        backend.set_recording_active(True)
        backend.set_recording_active(True)
        self.assertEqual(escape_registrations, [("fake-user32", 123)])
        self.assertIn(ESCAPE_HOTKEY_ID, backend.registered_ids)

        target.installed[0].nativeEventFilter(b"windows_generic_MSG", object())
        self.assertEqual(received, ["escape"])

        backend.set_recording_active(False)
        self.assertEqual(escape_unregistrations, [("fake-user32", 123)])
        self.assertNotIn(ESCAPE_HOTKEY_ID, backend.registered_ids)
        target.installed[0].nativeEventFilter(b"windows_generic_MSG", object())
        self.assertEqual(received, ["escape"])

        backend.stop()

    def test_backend_stop_cleans_escape_registration_during_recording(self):
        escape_unregistrations = []

        backend = WindowsGlobalHotkeyBackend(
            FakeNativeEventTarget(),
            user32="fake-user32",
            register_hotkeys=lambda *_args, **_kwargs: {0x5101},
            unregister_hotkeys=lambda *_args, **_kwargs: None,
            register_escape=lambda *_args, **_kwargs: True,
            unregister_escape=lambda user32, hwnd: escape_unregistrations.append(
                (user32, hwnd)
            ),
        )

        backend.start(FakeWindow())
        backend.set_recording_active(True)
        backend.stop()

        self.assertEqual(escape_unregistrations, [("fake-user32", 123)])
        self.assertFalse(backend.registered_ids)

    def test_backend_does_not_register_unsupported_voice_translation_action(self):
        target = FakeNativeEventTarget()
        registration_settings = []
        unregistrations = []

        def register(_user32, _hwnd, settings, *, strict):
            self.assertTrue(strict)
            registration_settings.append(settings)
            return {0x5101, 0x5104}

        def unregister(_user32, _hwnd, registered):
            unregistrations.append(set(registered))

        backend = WindowsGlobalHotkeyBackend(
            target,
            settings=HotkeySettings.defaults(),
            user32="fake-user32",
            register_hotkeys=register,
            unregister_hotkeys=unregister,
        )

        self.assertEqual(
            backend.start(FakeWindow()),
            {0x5101, 0x5104},
        )
        self.assertEqual(len(registration_settings), 1)
        self.assertEqual(
            set(registration_settings[0].hotkeys),
            {HotkeyAction.RECORDING, HotkeyAction.VISIBILITY},
        )
        self.assertNotIn(HotkeyAction.REWRITE, registration_settings[0].hotkeys)
        self.assertNotIn(HotkeyAction.TRANSLATION, registration_settings[0].hotkeys)
        self.assertNotIn(
            HotkeyAction.VOICE_TRANSLATION,
            registration_settings[0].hotkeys,
        )
        for unsupported_action in (
            HotkeyAction.REWRITE,
            HotkeyAction.TRANSLATION,
            HotkeyAction.VOICE_TRANSLATION,
        ):
            with self.assertRaises(KeyError):
                registration_settings[0].definition(unsupported_action)
        self.assertEqual(backend.registered_ids, {0x5101, 0x5104})

        backend.stop()
        self.assertEqual(unregistrations, [{0x5101, 0x5104}])

    def test_event_filter_factory_failure_unregisters_every_registered_id(self):
        target = FakeNativeEventTarget()
        registrations = {0x5101, 0x5102, 0x5103}
        unregistrations = []

        def register(_user32, _hwnd, _settings, *, strict):
            self.assertTrue(strict)
            return registrations

        def unregister(user32, hwnd, registered):
            unregistrations.append((user32, hwnd, set(registered)))

        def fail_factory(*_args, **_kwargs):
            raise RuntimeError("event filter construction failed")

        backend = WindowsGlobalHotkeyBackend(
            target,
            user32="fake-user32",
            register_hotkeys=register,
            unregister_hotkeys=unregister,
            event_filter_factory=fail_factory,
        )

        with self.assertRaisesRegex(RuntimeError, "event filter construction"):
            backend.start(FakeWindow())

        self.assertEqual(
            unregistrations,
            [("fake-user32", 123, registrations)],
        )
        self.assertEqual(backend.registered_ids, frozenset())
        self.assertFalse(backend.is_running)
        self.assertEqual(target.installed, [])

    def test_linux_requires_an_injected_user32_seam(self):
        backend = WindowsGlobalHotkeyBackend(FakeNativeEventTarget())
        with self.assertRaisesRegex(RuntimeError, "user32 seam"):
            backend.start(FakeWindow())


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 is an optional QML dependency")
class QtShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        FakeLock.locks.clear()

    def _shell(
        self,
        *,
        guard=None,
        hotkeys=None,
        application=None,
        tray_icon_factory=FakeTray,
        menu_factory=FakeMenu,
    ):
        return QtShell(
            FakeWindow(),
            instance_guard=guard
            or QtSingleInstanceGuard(
                lock_path=Path(tempfile.gettempdir()) / "clarifyvoice-shell.lock",
                lock_factory=FakeLock,
            ),
            hotkeys=hotkeys,
            application=application,
            tray_icon_factory=tray_icon_factory,
            menu_factory=menu_factory,
        )

    def test_secondary_process_does_not_create_tray_or_hotkeys(self):
        guard = QtSingleInstanceGuard(
            lock_path=Path(tempfile.gettempdir()) / "clarifyvoice-shell.lock",
            lock_factory=FakeLock,
        )
        owner = QtSingleInstanceGuard(
            lock_path=Path(tempfile.gettempdir()) / "clarifyvoice-shell.lock",
            lock_factory=FakeLock,
        )
        self.assertTrue(owner.acquire())
        shell = self._shell(guard=guard, hotkeys=FakeHotkeys())

        self.assertFalse(shell.start())
        self.assertFalse(shell.is_running)
        self.assertIsNone(shell.tray)

        owner.release()

    def test_primary_process_acquires_guard_without_a_tray(self):
        hotkeys = FakeHotkeys()
        shell = self._shell(hotkeys=hotkeys)

        self.assertTrue(shell.start(tray_available=False))
        self.assertTrue(shell.is_running)
        self.assertTrue(shell._instance_guard.is_primary)
        self.assertIsNone(shell.tray)
        self.assertTrue(hotkeys.is_running)

        shell.stop()
        self.assertFalse(shell._instance_guard.is_primary)

    def test_primary_shell_owns_tray_hotkeys_and_window_actions(self):
        hotkeys = FakeHotkeys()
        application = FakeApplication()
        shell = self._shell(hotkeys=hotkeys, application=application)
        visibility_events = []
        shell.hotkeyTriggered.connect(visibility_events.append)

        self.assertTrue(shell.start())
        self.assertTrue(shell.is_running)
        self.assertTrue(shell.tray.visible)
        self.assertEqual(hotkeys.started_with, shell._window)

        hotkeys.triggered.emit("toggle_visibility")
        self.assertEqual(visibility_events, ["toggle_visibility"])
        self.assertEqual(shell._window.calls, ["show", "raise", "activate"])

        shell.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        self.assertEqual(shell._window.calls[-1], "hide")

        shell.stop()
        self.assertTrue(shell.start())
        hotkeys.triggered.emit("toggle_visibility")
        self.assertEqual(visibility_events, ["toggle_visibility", "toggle_visibility"])

        shell.quit()
        self.assertTrue(hotkeys.stopped)
        self.assertEqual(application.quit_calls, 1)
        self.assertFalse(shell.is_running)

    def test_tray_close_event_hides_window_and_preserves_native_handle(self):
        shell = self._shell()
        self.assertTrue(shell.start())
        shell.show_window()
        close_event = QEvent(QEvent.Type.Close)

        self.assertTrue(shell.eventFilter(shell._window, close_event))
        self.assertFalse(close_event.isAccepted())
        self.assertFalse(shell._window.visible)
        self.assertEqual(shell._window.handle, 123)
        self.assertEqual(shell._window.event_filters, [shell])

        shell.stop()
        self.assertEqual(shell._window.removed_event_filters, [shell])

    def test_explicit_quit_removes_close_filter_before_quitting(self):
        application = FakeApplication()
        shell = self._shell(application=application)
        self.assertTrue(shell.start())

        shell.quit()

        self.assertEqual(application.quit_calls, 1)
        self.assertEqual(shell._window.removed_event_filters, [shell])
        self.assertFalse(shell.is_running)

    def test_tray_configuration_failure_hides_and_releases_partial_tray(self):
        tray = FakeTray(None, None)
        tray.fail_on_tooltip = True
        hotkeys = FakeHotkeys()

        shell = self._shell(
            hotkeys=hotkeys,
            tray_icon_factory=lambda _icon, _parent: tray,
        )

        with self.assertRaisesRegex(RuntimeError, "tray configuration"):
            shell.start()

        self.assertFalse(shell.is_running)
        self.assertTrue(shell._instance_guard.is_primary)
        self.assertIsNone(shell.tray)
        self.assertFalse(tray.visible)
        self.assertEqual(tray.hide_calls, 1)
        self.assertEqual(tray.delete_later_calls, 1)
        self.assertFalse(hotkeys.stopped)

        shell.stop()
        self.assertFalse(shell._instance_guard.is_primary)

    def test_partial_menu_failure_cleans_both_tray_objects(self):
        tray = FakeTray(None, None)
        menu = FakeMenu(None, fail_on_add_action=True)

        shell = self._shell(
            tray_icon_factory=lambda _icon, _parent: tray,
            menu_factory=lambda _parent: menu,
        )

        with self.assertRaisesRegex(RuntimeError, "menu configuration"):
            shell.start()

        self.assertFalse(shell.is_running)
        self.assertTrue(shell._instance_guard.is_primary)
        self.assertIsNone(shell.tray)
        self.assertFalse(tray.visible)
        self.assertEqual(tray.hide_calls, 1)
        self.assertEqual(tray.delete_later_calls, 1)
        self.assertEqual(menu.delete_later_calls, 1)

        shell.stop()
        self.assertFalse(shell._instance_guard.is_primary)

    def test_hotkey_setup_failure_cleans_backend_but_retains_guard(self):
        hotkeys = FailingHotkeys()
        shell = self._shell(hotkeys=hotkeys)

        with self.assertRaisesRegex(RuntimeError, "hotkey setup failed"):
            shell.start(tray_available=False)

        self.assertFalse(shell.is_running)
        self.assertTrue(shell._instance_guard.is_primary)
        self.assertFalse(hotkeys.is_running)
        self.assertEqual(hotkeys.stop_calls, 1)

        shell.stop()
        self.assertFalse(shell._instance_guard.is_primary)

    def test_stop_is_idempotent_after_started_shell(self):
        hotkeys = FakeHotkeys()
        shell = self._shell(hotkeys=hotkeys)
        tray = None

        self.assertTrue(shell.start())
        tray = shell.tray
        self.assertIsNotNone(tray)

        shell.stop()
        shell.stop()

        self.assertFalse(shell.is_running)
        self.assertFalse(shell._instance_guard.is_primary)
        self.assertEqual(hotkeys.stop_calls, 1)
        self.assertEqual(tray.hide_calls, 1)
        self.assertEqual(tray.delete_later_calls, 1)


class QtShellSourceTests(unittest.TestCase):
    def test_module_has_no_legacy_ui_imports(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

        self.assertNotIn("app", imported_roots)
        self.assertNotIn("customtkinter", imported_roots)
        self.assertNotIn("tkinter", imported_roots)
        self.assertIn("PySide6", imported_roots)

        source = MODULE.read_text(encoding="utf-8")
        self.assertIn("_UNSUPPORTED_SHELL_HOTKEY_ACTIONS", source)
        self.assertIn("HotkeyAction.REWRITE", source)
        self.assertIn("HotkeyAction.TRANSLATION", source)
        self.assertIn("voice", source.lower())
        self.assertIn("translation in this slice", source.lower())


if __name__ == "__main__":
    unittest.main()
