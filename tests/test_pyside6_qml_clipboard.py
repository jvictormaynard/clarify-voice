"""Focused tests for the standalone QML clipboard boundary."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from spikes.pyside6.qml_clipboard import QmlClipboardGateway
from voice_translation import VoiceTranslationPublication
from windows_clipboard import CF_UNICODETEXT, ClipboardFormat, ClipboardSnapshot
from workflows import SelectionDisposition, SelectionTarget


class FakeClipboardAdapter:
    """Clipboard adapter double with sequence ownership semantics."""

    is_windows = True

    def __init__(self, text="previous"):
        self.sequence_value = 10
        self.state = self._snapshot(text, self.sequence_value)
        self.writes = []
        self.restores = []

    @staticmethod
    def _snapshot(text, sequence):
        return ClipboardSnapshot(
            (
                ClipboardFormat(
                    CF_UNICODETEXT,
                    (str(text) + "\x00").encode("utf-16-le"),
                ),
            ),
            sequence,
        )

    def sequence(self):
        return self.sequence_value

    def snapshot(self):
        return self.state

    def text(self):
        return self.state.text

    def write_text(self, text):
        self.sequence_value += 1
        self.state = self._snapshot(text, self.sequence_value)
        self.writes.append(str(text))
        return True

    def restore(self, snapshot):
        self.sequence_value += 1
        self.state = ClipboardSnapshot(snapshot.formats, self.sequence_value)
        self.restores.append(snapshot)
        return True

    def restore_if_owned(self, snapshot, expected_sequence, expected_text):
        if (
            self.sequence() != expected_sequence
            or self.text() != expected_text
            or not snapshot.restorable
        ):
            return False
        return self.restore(snapshot)

    def external_write(self, text):
        self.write_text(text)


class QmlClipboardGatewayTests(unittest.TestCase):
    def setUp(self):
        self.foreground = {"window": 77}
        self.clipboard = FakeClipboardAdapter()
        self.target = SelectionTarget(77, "editor.exe")
        self.sleep = Mock()

        def foreground_window():
            return self.foreground["window"]

        def copy_selected():
            self.clipboard.sequence_value += 1
            self.clipboard.state = self.clipboard._snapshot(
                "selected", self.clipboard.sequence_value
            )
            return True

        self.gateway = QmlClipboardGateway(
            adapter=self.clipboard,
            is_windows=True,
            platform_name="Windows",
            foreground_window=foreground_window,
            executable_for_window=lambda _window: "editor.exe",
            send_ctrl_c=copy_selected,
            send_ctrl_v=lambda _text: True,
            sleep=self.sleep,
            monotonic=self._monotonic,
            restore_delay=0,
            copy_timeout=0.1,
        )

    def _monotonic(self):
        # The fake Ctrl+C changes the sequence immediately, so the polling
        # loop exits on its first iteration without a real clock or sleep.
        return 0.0

    def test_capture_target_and_currentness_use_foreground_hwnd_and_executable(self):
        self.assertEqual(self.gateway.capture_target(), self.target)
        self.assertTrue(self.gateway.is_target_current(self.target))

        self.foreground["window"] = 88
        self.assertFalse(self.gateway.is_target_current(self.target))

    def test_selection_capture_and_restore_only_when_sequence_is_owned(self):
        capture = self.gateway.capture_selection(self.target)

        self.assertIsNotNone(capture)
        self.assertEqual(capture.text, "selected")
        self.assertEqual(capture.context["copy_observed_sequence"], 11)

        self.gateway.restore(capture)
        self.assertEqual(self.clipboard.text(), "previous")
        self.assertEqual(len(self.clipboard.restores), 1)

        capture = self.gateway.capture_selection(self.target)
        self.clipboard.external_write("user wins")
        self.gateway.restore(capture)
        self.assertEqual(self.clipboard.text(), "user wins")
        self.assertEqual(len(self.clipboard.restores), 1)

    def test_unsafe_paste_downgrades_to_copy_only_and_keeps_result_available(self):
        self.gateway._send_ctrl_v = lambda _text: None
        capture = self.gateway.capture_selection(self.target)

        disposition = self.gateway.apply_result(capture, "generated")

        self.assertEqual(disposition, SelectionDisposition.COPIED)
        self.assertEqual(self.clipboard.text(), "generated")
        self.assertEqual(self.clipboard.writes[-1], "generated")

    def test_injected_paste_boundary_is_called_with_generated_text(self):
        paste = Mock(return_value=None)

        def copy_selected():
            self.clipboard.sequence_value += 1
            self.clipboard.state = self.clipboard._snapshot(
                "selected", self.clipboard.sequence_value
            )
            return True

        gateway = QmlClipboardGateway(
            adapter=self.clipboard,
            is_windows=True,
            platform_name="Windows",
            foreground_window=lambda: self.foreground["window"],
            executable_for_window=lambda _window: "editor.exe",
            send_ctrl_c=copy_selected,
            send_ctrl_v=paste,
            sleep=self.sleep,
            monotonic=self._monotonic,
            restore_delay=0,
            copy_timeout=0.1,
        )
        capture = gateway.capture_selection(self.target)

        gateway.apply_result(capture, "generated")

        paste.assert_called_once_with("generated")

    def test_focus_change_downgrades_without_sending_ctrl_v(self):
        paste = Mock(return_value=True)
        self.gateway._send_ctrl_v = paste
        capture = self.gateway.capture_selection(self.target)
        self.foreground["window"] = 88

        disposition = self.gateway.apply_result(capture, "generated")

        self.assertEqual(disposition, SelectionDisposition.COPIED)
        paste.assert_not_called()
        self.assertEqual(self.clipboard.text(), "generated")

    def test_voice_publication_reuses_owned_paste_transaction(self):
        self.assertTrue(self.gateway.owns_clipboard())

        disposition = self.gateway.publish(
            "translated voice",
            self.target,
            VoiceTranslationPublication.PASTED,
        )

        self.assertEqual(disposition, VoiceTranslationPublication.PASTED)
        self.assertEqual(self.clipboard.text(), "previous")
        self.assertEqual(self.clipboard.writes[-1], "translated voice")

    def test_voice_copy_only_publication_keeps_generated_text_visible(self):
        disposition = self.gateway.publish(
            "translated voice",
            self.target,
            VoiceTranslationPublication.COPY_ONLY,
        )

        self.assertEqual(disposition, VoiceTranslationPublication.COPY_ONLY)
        self.assertEqual(self.clipboard.text(), "translated voice")


class QmlClipboardNonWindowsTests(unittest.TestCase):
    def test_visible_result_uses_xclip_and_selection_is_copy_only(self):
        run = Mock()
        gateway = QmlClipboardGateway(
            adapter=FakeClipboardAdapter(),
            is_windows=False,
            platform_name="Linux",
            run=run,
        )
        target = SelectionTarget(77, "editor.exe")

        self.assertIsNone(gateway.capture_target())
        self.assertFalse(gateway.is_target_current(target))
        self.assertIsNone(gateway.capture_selection(target))
        self.assertEqual(
            gateway.write_dictation_result(target, "visible"),
            SelectionDisposition.COPIED,
        )
        self.assertEqual(
            gateway.apply_result(None, "visible"), SelectionDisposition.COPIED
        )
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[0].args[0],
            [
                "xclip",
                "-selection",
                "clipboard",
            ],
        )
        self.assertFalse(gateway.alt_pressed())


if __name__ == "__main__":
    unittest.main()
