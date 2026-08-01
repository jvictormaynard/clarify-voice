import threading
import unittest
from unittest.mock import patch

import app
from windows_clipboard import (
    CF_DIB,
    CF_UNICODETEXT,
    ClipboardFormat,
    ClipboardSnapshot,
    WindowsClipboardAdapter,
)


class FakeClipboard:
    def __init__(self):
        self.sequence_value = 10
        self.state = ClipboardSnapshot((
            ClipboardFormat(CF_UNICODETEXT, "old\x00".encode("utf-16-le")),
            ClipboardFormat(49301, b"<html>old</html>"),
            ClipboardFormat(49302, b"{\\rtf1 old}"),
            ClipboardFormat(CF_DIB, b"dib-bytes"),
        ), self.sequence_value)

    def sequence(self):
        return self.sequence_value

    def snapshot(self):
        return self.state

    def text(self):
        return self.state.text

    def write_text(self, text):
        self.sequence_value += 1
        self.state = ClipboardSnapshot((
            ClipboardFormat(CF_UNICODETEXT, (str(text) + "\x00").encode("utf-16-le")),
        ), self.sequence_value)
        return True

    def restore(self, snapshot):
        self.sequence_value += 1
        self.state = ClipboardSnapshot(snapshot.formats, self.sequence_value)
        return True

    def external_write(self, text):
        self.write_text(text)


class ClipboardPasteTests(unittest.TestCase):
    def setUp(self):
        self.clipboard = FakeClipboard()

    def test_successful_paste_restores_all_supported_rich_formats(self):
        with patch.object(app, "_WINDOWS_CLIPBOARD", self.clipboard), \
                patch.object(app, "_send_key_chord", return_value=True), \
                patch.object(app.time, "sleep"):
            self.assertTrue(app._paste_generated_text("result", restore_delay=0))

        self.assertEqual(self.clipboard.state.text, "old")
        self.assertEqual(
            {item.format_id for item in self.clipboard.state.formats},
            {CF_UNICODETEXT, CF_DIB, 49301, 49302},
        )

    def test_later_user_clipboard_write_wins_over_restore(self):
        def user_write(_delay):
            self.clipboard.external_write("user wins")

        with patch.object(app, "_WINDOWS_CLIPBOARD", self.clipboard), \
                patch.object(app, "_send_key_chord", return_value=True), \
                patch.object(app.time, "sleep", side_effect=user_write):
            self.assertTrue(app._paste_generated_text("result"))

        self.assertEqual(self.clipboard.text(), "user wins")

    def test_failed_paste_keeps_generated_result_available(self):
        with patch.object(app, "_WINDOWS_CLIPBOARD", self.clipboard), \
                patch.object(app, "_send_key_chord", return_value=False), \
                patch.object(app.time, "sleep"):
            self.assertFalse(app._paste_generated_text("result"))

        self.assertEqual(self.clipboard.text(), "result")

    def test_injected_ctrl_v_without_consumption_keeps_generated_result(self):
        with patch.object(app, "_WINDOWS_CLIPBOARD", self.clipboard), \
                patch.object(app, "_send_key_chord", return_value=None), \
                patch.object(app.time, "sleep"):
            self.assertFalse(app._paste_generated_text("result"))

        self.assertEqual(self.clipboard.text(), "result")

    def test_uncapturable_snapshot_never_empties_clipboard_on_restore(self):
        self.clipboard.state = ClipboardSnapshot(
            (ClipboardFormat(9001, b"unsupported"),), 10, restorable=False)
        with patch.object(app, "_WINDOWS_CLIPBOARD", self.clipboard), \
                patch.object(app, "_send_key_chord", return_value=True), \
                patch.object(app.time, "sleep"):
            self.assertFalse(app._paste_generated_text("result"))

        self.assertEqual(self.clipboard.text(), "result")

    def test_overlapping_operations_are_serialized(self):
        entered = threading.Event()
        release = threading.Event()
        original_sleep = app.time.sleep

        def blocking_sleep(delay):
            if not entered.is_set():
                entered.set()
                release.wait(timeout=1)
            else:
                original_sleep(0)

        with patch.object(app, "_WINDOWS_CLIPBOARD", self.clipboard), \
                patch.object(app, "_send_key_chord", return_value=True), \
                patch.object(app.time, "sleep", side_effect=blocking_sleep):
            first = threading.Thread(target=app._paste_generated_text, args=("first",))
            second = threading.Thread(target=app._paste_generated_text, args=("second",))
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second.start()
            self.assertTrue(second.is_alive())
            release.set()
            first.join(timeout=1)
            second.join(timeout=1)

        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(self.clipboard.text(), "old")


class ClipboardSnapshotTests(unittest.TestCase):
    def test_unicode_snapshot_text_is_decoded_without_terminal_nul(self):
        snapshot = ClipboardSnapshot(
            (ClipboardFormat(CF_UNICODETEXT, "rich text\x00".encode("utf-16-le")),), 1)
        self.assertEqual(snapshot.text, "rich text")

    def test_unsupported_only_clipboard_is_not_a_restorable_empty_snapshot(self):
        adapter = _EnumeratingAdapter([9001], {})
        snapshot = adapter.snapshot()
        self.assertEqual(snapshot.formats, ())
        self.assertFalse(snapshot.restorable)

    def test_oversized_supported_format_is_not_restorable(self):
        adapter = _EnumeratingAdapter([CF_DIB], {CF_DIB: None})
        snapshot = adapter.snapshot()
        self.assertEqual(snapshot.formats, ())
        self.assertFalse(snapshot.restorable)


class _FakeFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class _FakeUser32:
    def __init__(self, formats):
        self.formats = formats
        self.EnumClipboardFormats = _FakeFunction(self._enum)
        self.GetClipboardData = _FakeFunction(lambda format_id: format_id)
        self.CloseClipboard = _FakeFunction(lambda: None)

    def _enum(self, previous):
        if previous == 0:
            return self.formats[0] if self.formats else 0
        index = self.formats.index(previous) + 1
        return self.formats[index] if index < len(self.formats) else 0


class _EnumeratingAdapter(WindowsClipboardAdapter):
    def __init__(self, formats, data):
        super().__init__(is_windows=True)
        self.user32 = _FakeUser32(formats)
        self.data = data

    def _user32(self):
        return self.user32

    def _supported_formats(self):
        return {CF_DIB}

    def _open(self, timeout=0.25):
        return None

    def _read_global_memory(self, handle):
        return self.data.get(handle)

    def sequence(self):
        return 1
