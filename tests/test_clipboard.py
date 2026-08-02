import threading
import unittest
from unittest.mock import patch

import app
from windows_clipboard import (
    CF_DIB,
    CF_UNICODETEXT,
    ClipboardFormat,
    ClipboardSnapshot,
    MAX_FORMAT_BYTES,
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

    def restore_if_owned(self, snapshot, expected_sequence, expected_text):
        if (not snapshot.restorable
                or self.sequence() != expected_sequence
                or self.text() != expected_text):
            return False
        return self.restore(snapshot)

    def external_write(self, text):
        self.write_text(text)


class FailingRestoreClipboard(FakeClipboard):
    def __init__(self, failure):
        super().__init__()
        self.failure = failure

    def restore_if_owned(self, snapshot, expected_sequence, expected_text):
        raise OSError(self.failure)


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
            self.assertFalse(app._paste_generated_text("result"))

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

    def test_focus_change_after_write_skips_ctrl_v_and_keeps_result_copied(self):
        focused = {"value": True}

        def write_result(text):
            self.clipboard.write_text(text)
            focused["value"] = False

        with patch.object(app, "_WINDOWS_CLIPBOARD", self.clipboard), \
                patch.object(app, "_set_windows_clipboard_text",
                             side_effect=write_result), \
                patch.object(app, "_send_key_chord") as send_key, \
                patch.object(app.time, "sleep"):
            pasted = app._paste_generated_text(
                "result", paste_predicate=lambda: focused["value"])

        self.assertFalse(pasted)
        send_key.assert_not_called()
        self.assertEqual(self.clipboard.text(), "result")

    def test_uncapturable_snapshot_never_empties_clipboard_on_restore(self):
        self.clipboard.state = ClipboardSnapshot(
            (ClipboardFormat(9001, b"unsupported"),), 10, restorable=False)
        with patch.object(app, "_WINDOWS_CLIPBOARD", self.clipboard), \
                patch.object(app, "_send_key_chord", return_value=True), \
                patch.object(app.time, "sleep"):
            self.assertFalse(app._paste_generated_text("result"))

        self.assertEqual(self.clipboard.text(), "result")

    def test_restore_failure_republishes_generated_text_without_second_paste(self):
        for failure in ("allocation failed", "SetClipboardData failed"):
            with self.subTest(failure=failure):
                clipboard = FailingRestoreClipboard(failure)
                with patch.object(app, "_WINDOWS_CLIPBOARD", clipboard), \
                        patch.object(app, "_send_key_chord", return_value=True) as send_key, \
                        patch.object(app.time, "sleep"):
                    self.assertFalse(app._paste_generated_text("result"))

                self.assertEqual(clipboard.text(), "result")
                send_key.assert_called_once_with(
                    "ctrl+v", expected_text="result")

    def test_new_clipboard_write_wins_during_atomic_compare_and_restore(self):
        clipboard = _AtomicRaceClipboard()
        result = []

        def paste():
            result.append(app._paste_generated_text("result"))

        with patch.object(app, "_WINDOWS_CLIPBOARD", clipboard), \
                patch.object(app, "_send_key_chord", return_value=True), \
                patch.object(app.time, "sleep"):
            worker = threading.Thread(target=paste)
            worker.start()
            self.assertTrue(clipboard.checked.wait(timeout=1))

            writer = threading.Thread(
                target=clipboard.external_write, args=("new user text",))
            writer.start()
            self.assertTrue(clipboard.writer_started.wait(timeout=1))
            clipboard.allow_restore.set()
            worker.join(timeout=1)
            writer.join(timeout=1)

        self.assertEqual(result, [True])
        self.assertEqual(clipboard.text(), "new user text")

    def test_selection_snapshot_restore_is_atomic(self):
        clipboard = _AtomicRaceClipboard()
        original = clipboard.snapshot()
        result = []

        def restore_selection():
            result.append(app._restore_clipboard_snapshot_if_owned(
                original, clipboard.sequence(), clipboard.text()))

        with patch.object(app, "_WINDOWS_CLIPBOARD", clipboard):
            worker = threading.Thread(target=restore_selection)
            worker.start()
            self.assertTrue(clipboard.checked.wait(timeout=1))

            writer = threading.Thread(
                target=clipboard.external_write, args=("new user text",))
            writer.start()
            self.assertTrue(clipboard.writer_started.wait(timeout=1))
            clipboard.allow_restore.set()
            worker.join(timeout=1)
            writer.join(timeout=1)

        self.assertEqual(result, [True])
        self.assertEqual(clipboard.text(), "new user text")

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

    def test_unicode_snapshot_ignores_capacity_bytes_after_terminator(self):
        data = "rich text\x00".encode("utf-16-le") + b"\xff\xfe\x01\x02"
        snapshot = ClipboardSnapshot((ClipboardFormat(CF_UNICODETEXT, data),), 1)
        self.assertEqual(snapshot.text, "rich text")

    def test_unicode_snapshot_ignores_false_unaligned_terminator(self):
        data = b"A\x00\x00\x00" + b"capacity\x00\x00"
        snapshot = ClipboardSnapshot((ClipboardFormat(CF_UNICODETEXT, data),), 1)
        self.assertEqual(snapshot.text, "A")

    def test_unicode_snapshot_decodes_surrogate_pair(self):
        data = "A \U0001f600\x00".encode("utf-16-le")
        snapshot = ClipboardSnapshot((ClipboardFormat(CF_UNICODETEXT, data),), 1)
        self.assertEqual(snapshot.text, "A \U0001f600")

    def test_unicode_snapshot_rejects_odd_length_buffer(self):
        data = "valid\x00".encode("utf-16-le") + b"\x01"
        snapshot = ClipboardSnapshot((ClipboardFormat(CF_UNICODETEXT, data),), 1)
        self.assertIsNone(snapshot.text)

    def test_unicode_snapshot_rejects_missing_terminator(self):
        data = "missing terminator".encode("utf-16-le")
        snapshot = ClipboardSnapshot((ClipboardFormat(CF_UNICODETEXT, data),), 1)
        self.assertIsNone(snapshot.text)

    def test_unicode_snapshot_large_buffer_uses_native_code_unit_search(self):
        code_units = (MAX_FORMAT_BYTES // 2) - 1
        data = (b"A\x00" * code_units) + b"\x00\x00"
        snapshot = ClipboardSnapshot((ClipboardFormat(CF_UNICODETEXT, data),), 1)
        self.assertEqual(len(snapshot.text), code_units)

    def test_unsupported_only_clipboard_is_not_a_restorable_empty_snapshot(self):
        adapter = _EnumeratingAdapter([9001], {})
        snapshot = adapter.snapshot()
        self.assertEqual(snapshot.formats, ())
        self.assertFalse(snapshot.restorable)

    def test_supported_format_survives_auxiliary_clipboard_format(self):
        adapter = _EnumeratingAdapter(
            [CF_DIB, 9001], {CF_DIB: b"dib-bytes"})
        snapshot = adapter.snapshot()
        self.assertEqual(
            snapshot.formats, (ClipboardFormat(CF_DIB, b"dib-bytes"),))
        self.assertTrue(snapshot.restorable)

    def test_oversized_supported_format_is_not_restorable(self):
        adapter = _EnumeratingAdapter([CF_DIB], {CF_DIB: None})
        snapshot = adapter.snapshot()
        self.assertEqual(snapshot.formats, ())
        self.assertFalse(snapshot.restorable)

    def test_restore_preallocates_before_emptying_clipboard(self):
        adapter = _RestoreAdapter(allocation_failure_at=2)
        snapshot = ClipboardSnapshot((
            ClipboardFormat(CF_UNICODETEXT, b"one"),
            ClipboardFormat(CF_DIB, b"two"),
        ), 1)

        with self.assertRaises(OSError):
            adapter.restore(snapshot)

        self.assertEqual(adapter.user32.empty_calls, 0)

    def test_set_data_failure_repairs_generated_text_before_close(self):
        adapter = _RestoreAdapter(set_failure_at=1)
        snapshot = ClipboardSnapshot(
            (ClipboardFormat(CF_UNICODETEXT, b"old"),), 1)

        with self.assertRaises(OSError):
            adapter._restore_open_clipboard(
                snapshot, adapter.user32, fallback_text="generated")

        self.assertEqual(len(adapter.user32.set_calls), 2)
        self.assertEqual(adapter.user32.empty_calls, 2)
        self.assertEqual(adapter.allocation_empty_counts, [0, 0])


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


class _RestoreUser32:
    def __init__(self, set_failure_at=None):
        self.set_failure_at = set_failure_at
        self.empty_calls = 0
        self.set_calls = []
        self.failure_triggered = False
        self.EmptyClipboard = _FakeFunction(self._empty)
        self.SetClipboardData = _FakeFunction(self._set)
        self.CloseClipboard = _FakeFunction(lambda: None)

    def _empty(self):
        self.empty_calls += 1
        return True

    def _set(self, format_id, handle):
        self.set_calls.append((format_id, handle))
        if (self.set_failure_at is not None
                and not self.failure_triggered
                and len(self.set_calls) == self.set_failure_at):
            self.failure_triggered = True
            return False
        return True


class _RestoreAdapter(WindowsClipboardAdapter):
    def __init__(self, allocation_failure_at=None, set_failure_at=None):
        super().__init__(is_windows=True)
        self.user32 = _RestoreUser32(set_failure_at)
        self.allocation_failure_at = allocation_failure_at
        self.allocations = 0
        self.allocation_empty_counts = []

    def _user32(self):
        return self.user32

    def _open(self, timeout=0.25):
        return None

    def _allocate_global_memory(self, data):
        self.allocations += 1
        self.allocation_empty_counts.append(self.user32.empty_calls)
        if self.allocations == self.allocation_failure_at:
            raise OSError("allocation failed")
        return self.allocations

    def _kernel32(self):
        return _RestoreKernel32()


class _RestoreKernel32:
    def __init__(self):
        self.GlobalFree = _FakeFunction(lambda _handle: None)


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


class _AtomicRaceClipboard(FakeClipboard):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.checked = threading.Event()
        self.allow_restore = threading.Event()
        self.writer_started = threading.Event()

    def restore_if_owned(self, snapshot, expected_sequence, expected_text):
        with self.lock:
            if (not snapshot.restorable
                    or self.sequence() != expected_sequence
                    or self.text() != expected_text):
                return False
            self.checked.set()
            self.allow_restore.wait(timeout=1)
            return self.restore(snapshot)

    def external_write(self, text):
        self.writer_started.set()
        with self.lock:
            self.write_text(text)
