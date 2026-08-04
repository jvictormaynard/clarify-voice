import threading
import json
import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path

from hotkey_config import (
    ActivationMode,
    ActivationRaceState,
    HotkeyAction,
    HotkeyConflictError,
    HotkeyDefinition,
    HotkeySettings,
    HotkeySettingsController,
    HotkeyValidationError,
    RecordingActivationController,
)
from windows_hotkeys import (
    HotkeyRegistrationError,
    WindowsHotkeyRegistration,
    register_global_hotkeys,
)
from repositories import AppConfig, LocalConfigRepository


class _Function:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class _User32:
    def __init__(self, fail_keys=()):
        self.fail_keys = set(fail_keys)
        self.registered = []
        self.unregistered = []
        self.RegisterHotKey = _Function(self._register)
        self.UnregisterHotKey = _Function(self._unregister)

    def _register(self, _hwnd, hotkey_id, _modifiers, key):
        if key in self.fail_keys:
            return False
        self.registered.append((hotkey_id, key))
        return True

    def _unregister(self, _hwnd, hotkey_id):
        self.unregistered.append(hotkey_id)
        self.registered = [item for item in self.registered if item[0] != hotkey_id]
        return True


class HotkeySettingsTests(unittest.TestCase):
    def test_legacy_installation_gets_original_defaults(self):
        settings = HotkeySettings.from_mapping({"ui_language": "pt"})

        self.assertEqual(settings.definition(HotkeyAction.RECORDING).display, "Alt+L")
        self.assertEqual(settings.definition(HotkeyAction.REWRITE).display, "Alt+K")
        self.assertEqual(settings.activation_mode, ActivationMode.TOGGLE)

    def test_flat_pre_release_bindings_are_migrated(self):
        settings = HotkeySettings.from_mapping({
            "recording_hotkey": "Ctrl+Shift+L",
            "rewrite_hotkey": {"modifiers": ["alt"], "key": "F2"},
        })

        self.assertEqual(settings.definition("recording_hotkey").display, "Ctrl+Shift+L")
        self.assertEqual(settings.definition("rewrite_hotkey").display, "Alt+F2")
        self.assertEqual(settings.definition("translation_hotkey").display, "Alt+T")

    def test_invalid_entry_reverts_only_that_entry(self):
        settings = HotkeySettings.from_mapping({
            "bindings": {
                "recording_hotkey": {"modifiers": ["alt"], "key": "NotAKey"},
                "rewrite_hotkey": {"modifiers": ["ctrl"], "key": "K"},
            },
        })

        self.assertEqual(settings.definition("recording_hotkey").display, "Alt+L")
        self.assertEqual(settings.definition("rewrite_hotkey").display, "Ctrl+K")

    def test_conflicts_are_rejected_before_registration(self):
        settings = HotkeySettings.defaults()

        with self.assertRaises(HotkeyConflictError):
            settings.with_hotkey("rewrite_hotkey", "Alt+L")

    def test_capture_and_reset_form_settings_facing_api(self):
        controller = HotkeySettingsController()
        controller.capture("recording_hotkey", "Ctrl+L")
        self.assertEqual(controller.settings.definition("recording_hotkey").display, "Ctrl+L")
        controller.reset("recording_hotkey")
        self.assertEqual(controller.settings.definition("recording_hotkey").display, "Alt+L")

    def test_push_to_talk_requires_key_release_capability(self):
        with self.assertRaises(HotkeyValidationError):
            HotkeySettings.defaults().with_activation_mode(
                ActivationMode.PUSH_TO_TALK)

        supported = HotkeySettings.defaults().with_activation_mode(
            ActivationMode.PUSH_TO_TALK, push_to_talk_supported=True)
        self.assertEqual(supported.activation_mode, ActivationMode.PUSH_TO_TALK)

    def test_repository_round_trip_writes_typed_nested_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            repository = LocalConfigRepository(path)
            repository.save(AppConfig.from_mapping({
                "hotkeys": HotkeySettings.defaults()
                    .with_hotkey("recording_hotkey", "Ctrl+L")
                    .to_mapping(),
            }))
            payload = json.loads(path.read_text(encoding="utf-8"))
            loaded = repository.load()

        self.assertEqual(
            payload["hotkeys"]["bindings"]["recording_hotkey"]["key"], "L")
        self.assertEqual(loaded.hotkeys.definition("recording_hotkey").display, "Ctrl+L")


class RegistrationTests(unittest.TestCase):
    def test_strict_registration_cleans_up_partial_set(self):
        user32 = _User32(fail_keys={ord("T")})

        with self.assertRaises(HotkeyRegistrationError) as raised:
            register_global_hotkeys(user32, 1, strict=True)

        self.assertIn("translation_hotkey", raised.exception.failed_actions)
        self.assertEqual(user32.registered, [])
        self.assertGreaterEqual(len(user32.unregistered), 2)

    def test_transactional_replace_restores_previous_set_on_failure(self):
        user32 = _User32()
        manager = WindowsHotkeyRegistration(user32, 1)
        manager.register()
        original = manager.settings
        user32.fail_keys.add(ord("Q"))

        with self.assertRaises(HotkeyRegistrationError):
            manager.replace(original.with_hotkey("rewrite_hotkey", "Alt+Q"))

        self.assertEqual(manager.settings, original)
        self.assertEqual(len(manager.registered), 5)
        self.assertEqual(len(user32.registered), 5)


class ActivationControllerTests(unittest.TestCase):
    def test_toggle_press_during_start_stops_once_after_start(self):
        started = threading.Event()
        release_start = threading.Event()
        starts = []
        stops = []

        def start():
            starts.append(True)
            started.set()
            release_start.wait(1)
            return True

        controller = RecordingActivationController(start, lambda: stops.append(True))
        thread = threading.Thread(target=controller.press)
        thread.start()
        self.assertTrue(started.wait(1))
        controller.press()
        release_start.set()
        thread.join(1)

        self.assertEqual(len(starts), 1)
        self.assertEqual(len(stops), 1)
        self.assertEqual(controller.state, ActivationRaceState.IDLE)

    def test_push_to_talk_release_during_start_cannot_stick(self):
        started = threading.Event()
        release_start = threading.Event()
        stops = []

        def start():
            started.set()
            release_start.wait(1)
            return True

        controller = RecordingActivationController(
            start, lambda: stops.append(True), mode=ActivationMode.PUSH_TO_TALK)
        thread = threading.Thread(target=controller.press)
        thread.start()
        self.assertTrue(started.wait(1))
        self.assertTrue(controller.release())
        release_start.set()
        thread.join(1)

        self.assertEqual(stops, [True])
        self.assertEqual(controller.state, ActivationRaceState.IDLE)

    def test_shutdown_cancels_active_edge_and_ignores_late_start(self):
        started = threading.Event()
        release_start = threading.Event()
        starts = []
        cancels = []

        def start():
            starts.append(True)
            started.set()
            release_start.wait(1)
            return True

        controller = RecordingActivationController(
            start, Mock(), on_cancel=lambda: cancels.append(True))
        thread = threading.Thread(target=controller.press)
        thread.start()
        self.assertTrue(started.wait(1))
        controller.shutdown()
        release_start.set()
        thread.join(1)

        self.assertEqual(len(starts), 1)
        self.assertEqual(cancels, [True])
        self.assertEqual(controller.state, ActivationRaceState.CLOSED)


if __name__ == "__main__":
    unittest.main()
