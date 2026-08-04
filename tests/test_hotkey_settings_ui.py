"""Headless contracts for the user-facing hotkey Settings flow (#48)."""

from copy import deepcopy
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
from windows_hotkeys import HotkeyRegistrationError


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


class _RejectingTray:
    def __init__(self, rejected):
        self.rejected = rejected
        self.calls = []
        self.active = HotkeySettings.defaults()

    def reconfigure_hotkeys(self, settings):
        self.calls.append(settings)
        if settings == self.rejected:
            raise HotkeyRegistrationError(
                (HotkeyAction.RECORDING.value,), reason="already registered")
        self.active = settings


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
                "_apply_settings_with_hotkeys_transaction(",
                "restore_after_failed_apply",
                "restore_hotkeys_after_failed_apply",
                "_recording_hotkey_hint",
                "self.apply_hotkey_settings(hotkey_settings_controller.settings)"):
            with self.subTest(expected=expected):
                self.assertIn(expected, source)
        self.assertLess(
            source.index("_apply_settings_transaction("),
            source.index(
                "self.apply_hotkey_settings(hotkey_settings_controller.settings)"),
        )

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

    def test_full_apply_rolls_back_general_changes_when_hotkeys_fail(self):
        previous = HotkeySettings.defaults()
        selected = previous.with_hotkey(HotkeyAction.RECORDING, "Ctrl+L")
        baseline = {
            "transcription_provider": "gemini",
            "history_enabled": False,
            "autostart": False,
            "workflows": {
                "transcription": {"prompt": "original"},
            },
            "hotkeys": previous.to_mapping(),
        }
        config = deepcopy(baseline)
        persisted = []
        saved_settings = deepcopy(baseline)
        ui_state = {
            "transcription_provider": "gemini",
            "history_enabled": False,
            "workflows": {"transcription": {"prompt": "original"}},
        }
        tray = _RejectingTray(selected)

        def save_config(_repositories=None):
            persisted.append(deepcopy(config))

        def apply_general():
            config["transcription_provider"] = "openai"
            config["history_enabled"] = True
            config["autostart"] = True
            config["workflows"]["transcription"]["prompt"] = "draft"
            ui_state["transcription_provider"] = "openai"
            ui_state["history_enabled"] = True
            ui_state["workflows"]["transcription"]["prompt"] = "draft"
            app._save_app_config()

        def apply_hotkeys():
            return app._apply_hotkey_settings_transaction(
                selected, tray_icon=tray)

        def restore_ui():
            ui_state["transcription_provider"] = saved_settings[
                "transcription_provider"]
            ui_state["history_enabled"] = saved_settings["history_enabled"]
            ui_state["workflows"] = deepcopy(saved_settings["workflows"])

        with patch.object(app, "APP_CONFIG", config), \
                patch.object(app, "_save_app_config", side_effect=save_config):
            with self.assertRaises(HotkeyRegistrationError):
                app._apply_settings_with_hotkeys_transaction(
                    apply_general, apply_hotkeys,
                    restore_hotkeys=lambda: tray.reconfigure_hotkeys(previous),
                    on_rollback=restore_ui)

        self.assertEqual(config, baseline)
        self.assertEqual(persisted[-1], baseline)
        self.assertEqual(saved_settings, baseline)
        self.assertEqual(ui_state, {
            "transcription_provider": "gemini",
            "history_enabled": False,
            "workflows": {"transcription": {"prompt": "original"}},
        })
        self.assertEqual(tray.active, previous)
        self.assertEqual(tray.calls, [selected, previous, previous])

    def test_full_apply_commits_general_and_hotkey_changes(self):
        previous = HotkeySettings.defaults()
        selected = previous.with_hotkey(HotkeyAction.RECORDING, "Ctrl+L")
        config = {
            "transcription_provider": "gemini",
            "hotkeys": previous.to_mapping(),
        }
        tray = _Tray()

        def save_config(_repositories=None):
            return None

        def apply_general():
            config["transcription_provider"] = "openai"
            app._save_app_config()

        with patch.object(app, "APP_CONFIG", config), \
                patch.object(app, "_save_app_config", side_effect=save_config):
            result = app._apply_settings_with_hotkeys_transaction(
                apply_general,
                lambda: app._apply_hotkey_settings_transaction(
                    selected, tray_icon=tray))

        self.assertEqual(result, selected)
        self.assertEqual(config["transcription_provider"], "openai")
        self.assertEqual(config["hotkeys"], selected.to_mapping())
        self.assertEqual(tray.applied, [selected])

    def test_recording_hint_uses_effective_binding_in_every_locale(self):
        selected = HotkeySettings.defaults().with_hotkey(
            HotkeyAction.RECORDING, "Ctrl+L")
        harness = object.__new__(app.App)
        expected_stop = {
            "en": "Ctrl+L stop",
            "pt": "Ctrl+L parar",
            "es": "Ctrl+L detener",
            "de": "Ctrl+L stoppen",
            "ru": "Ctrl+L — остановить",
        }

        with patch.object(app, "APP_CONFIG", {
                "hotkeys": selected.to_mapping()}):
            for language, stop_hint in expected_stop.items():
                with self.subTest(language=language):
                    harness.lang = language
                    self.assertEqual(
                        app.App._recording_hotkey_hint(harness), "Ctrl+L")
                    self.assertEqual(
                        app.App._recording_hotkey_hint(harness, stopping=True),
                        stop_hint)

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
