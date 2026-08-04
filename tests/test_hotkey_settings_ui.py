"""Headless contracts for the user-facing hotkey Settings flow (#48)."""

import inspect
import unittest
from unittest.mock import patch

import app
from hotkey_config import (
    ActivationMode,
    HotkeyAction,
    HotkeyConflictError,
    HotkeySettings,
    HotkeySettingsController,
    HotkeyValidationError,
)


class _KeyEvent:
    def __init__(self, state, keysym):
        self.state = state
        self.keysym = keysym


class _Tray:
    def __init__(self):
        self.applied = []

    def reconfigure_hotkeys(self, settings):
        self.applied.append(settings)


class _KeyboardHook:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_hotkey(self, chord, callback):
        handle = len(self.added) + 1
        self.added.append((handle, chord, callback))
        return handle

    def remove_hotkey(self, handle):
        self.removed.append(handle)


class HotkeySettingsControllerUiTests(unittest.TestCase):
    def test_capture_event_normalises_tk_modifiers_and_key(self):
        controller = HotkeySettingsController()

        selected = controller.capture_event(
            HotkeyAction.RECORDING,
            _KeyEvent(0x0004 | 0x0008, "F2"),
        )

        self.assertEqual(selected.definition(HotkeyAction.RECORDING).display,
                         "Ctrl+Alt+F2")

    def test_conflicting_capture_keeps_the_previous_draft(self):
        controller = HotkeySettingsController()

        with self.assertRaises(HotkeyConflictError):
            controller.capture_event(
                HotkeyAction.REWRITE, _KeyEvent(0x0008, "L"))

        self.assertEqual(
            controller.settings.definition(HotkeyAction.REWRITE).display,
            "Alt+K")

    def test_replace_restores_a_settings_baseline_for_undo(self):
        controller = HotkeySettingsController()
        baseline = HotkeySettings.defaults().with_hotkey(
            HotkeyAction.RECORDING, "Ctrl+L")

        controller.capture(HotkeyAction.RECORDING, "Ctrl+Shift+L")
        controller.replace(baseline)

        self.assertEqual(
            controller.settings.definition(HotkeyAction.RECORDING).display,
            "Ctrl+L")


class HotkeySettingsIntegrationTests(unittest.TestCase):
    def test_settings_source_wires_capture_reset_mode_and_atomic_apply(self):
        source = inspect.getsource(app.App._open_settings)

        for expected in (
                "HotkeySettingsController(",
                "capture_event(action, event)",
                "reset_hotkey(action)",
                "hotkey_mode_menu",
                "self.apply_hotkey_settings(hotkey_settings_controller.settings)"):
            with self.subTest(expected=expected):
                self.assertIn(expected, source)

    def test_apply_transaction_persists_the_selected_draft(self):
        previous = HotkeySettings.defaults()
        selected = previous.with_hotkey(HotkeyAction.RECORDING, "Ctrl+L")
        tray = _Tray()
        config = {"hotkeys": previous.to_mapping()}

        with patch.object(app, "APP_CONFIG", config), \
                patch.object(app, "_save_app_config") as save:
            result = app._apply_hotkey_settings_transaction(
                selected, repositories=None, tray_icon=tray)

        self.assertEqual(result, selected)
        self.assertEqual(config["hotkeys"], selected.to_mapping())
        self.assertEqual(tray.applied, [selected])
        save.assert_called_once_with(None)

    def test_apply_transaction_restores_native_and_persisted_state_on_save_error(self):
        previous = HotkeySettings.defaults()
        selected = previous.with_hotkey(HotkeyAction.RECORDING, "Ctrl+L")
        tray = _Tray()
        config = {"hotkeys": previous.to_mapping()}

        with patch.object(app, "APP_CONFIG", config), \
                patch.object(app, "_save_app_config", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                app._apply_hotkey_settings_transaction(
                    selected, repositories=None, tray_icon=tray)

        self.assertEqual(config["hotkeys"], previous.to_mapping())
        self.assertEqual(tray.applied, [selected, previous])

    def test_apply_rejects_push_to_talk_without_native_key_release(self):
        selected = HotkeySettings.defaults().with_activation_mode(
            ActivationMode.PUSH_TO_TALK, push_to_talk_supported=True)

        with patch.object(app, "supports_push_to_talk", return_value=False):
            with self.assertRaises(HotkeyValidationError) as raised:
                app._apply_hotkey_settings_transaction(selected)

        self.assertEqual(raised.exception.code, "unsupported_activation_mode")

    def test_keyboard_chord_format_preserves_modifier_order(self):
        selected = HotkeySettings.defaults().with_hotkey(
            HotkeyAction.RECORDING, "Shift+Ctrl+F2")

        self.assertEqual(
            app._keyboard_hotkey_chord(
                selected.definition(HotkeyAction.RECORDING)),
            "ctrl+shift+f2")

    def test_non_windows_hooks_replace_the_four_runtime_bindings(self):
        selected = HotkeySettings.defaults().with_hotkey(
            HotkeyAction.RECORDING, "Ctrl+L")
        hooks = _KeyboardHook()
        harness = object.__new__(app.App)
        harness._keyboard_hotkey_handles = {}

        with patch.object(app, "IS_WIN", False), patch.object(app, "keyboard", hooks):
            app.App._register_non_windows_hotkeys(harness, selected)

        self.assertEqual(
            [chord for _handle, chord, _callback in hooks.added],
            ["ctrl+l", "alt+k", "alt+t", "alt+r"])
        self.assertEqual(hooks.removed, [])

        replacement = selected.with_hotkey(HotkeyAction.REWRITE, "Ctrl+K")
        with patch.object(app, "IS_WIN", False), patch.object(app, "keyboard", hooks):
            app.App._register_non_windows_hotkeys(harness, replacement)

        self.assertEqual(hooks.removed, [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
