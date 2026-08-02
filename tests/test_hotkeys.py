import ctypes
import unittest
from unittest.mock import patch

import windows_hotkeys


class _FakeFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class _FakeUser32:
    def __init__(self, *, class_name="Edit", delivered=True, focused=11,
            text="old", selection=(3, 3), mutate=True, paste_text="new",
            query_before_delivered=True, query_after_delivered=True):
        self.class_name = class_name
        self.delivered = delivered
        self.focused = focused
        self.text = text
        self.selection = selection
        self.mutate = mutate
        self.paste_text = paste_text
        self.query_before_delivered = query_before_delivered
        self.query_after_delivered = query_after_delivered
        self.paste_attempted = False
        self.paste_calls = []
        self.GetForegroundWindow = _FakeFunction(lambda: 10)
        self.GetWindowThreadProcessId = _FakeFunction(lambda _hwnd, _pid: 1)
        self.GetGUIThreadInfo = _FakeFunction(self._get_gui_thread_info)
        self.IsChild = _FakeFunction(lambda _parent, _child: True)
        self.GetClassNameW = _FakeFunction(self._get_class_name)
        self.SendMessageTimeoutW = _FakeFunction(self._send_message_timeout)

    def _get_gui_thread_info(self, _thread_id, info_pointer):
        info_pointer._obj.hwndFocus = self.focused
        return True

    def _get_class_name(self, _hwnd, buffer, _length):
        buffer.value = self.class_name
        return len(self.class_name)

    def _send_message_timeout(self, *args):
        _hwnd, message, wparam, lparam, _flags, _timeout, result = args
        if message == windows_hotkeys.WM_PASTE:
            self.paste_calls.append(args)
            self.paste_attempted = True
            if self.delivered and self.mutate:
                start, end = self.selection
                self.text = self.text[:start] + self.paste_text + self.text[end:]
                caret = start + len(self.paste_text)
                self.selection = (caret, caret)
            result._obj.value = 0
            return self.delivered

        delivered = (self.query_after_delivered if self.paste_attempted
                     else self.query_before_delivered)
        if not delivered:
            return False
        if message == windows_hotkeys.WM_GETTEXTLENGTH:
            result._obj.value = len(self.text)
            return True
        if message == windows_hotkeys.WM_GETTEXT:
            buffer = ctypes.create_unicode_buffer(self.text)
            ctypes.memmove(lparam, buffer, ctypes.sizeof(buffer))
            result._obj.value = len(self.text)
            return True
        if message == windows_hotkeys.EM_GETSEL:
            ctypes.cast(wparam, ctypes.POINTER(ctypes.c_int)).contents.value = self.selection[0]
            ctypes.cast(lparam, ctypes.POINTER(ctypes.c_int)).contents.value = self.selection[1]
            result._obj.value = 0
            return True
        return False


class PasteDispatchTests(unittest.TestCase):
    def test_compatible_control_confirms_synchronous_paste(self):
        user32 = _FakeUser32(paste_text="new")
        with patch.object(windows_hotkeys, "send_ctrl_key") as fallback:
            self.assertTrue(windows_hotkeys.paste_focused_control(
                expected_text="new", user32=user32))

        fallback.assert_not_called()
        self.assertEqual(len(user32.paste_calls), 1)
        self.assertEqual(user32.paste_calls[0][1], windows_hotkeys.WM_PASTE)

    def test_timeout_stays_unconfirmed_without_second_injection(self):
        user32 = _FakeUser32(delivered=False)
        with patch.object(windows_hotkeys, "send_ctrl_key", return_value=None) as fallback:
            self.assertIsNone(windows_hotkeys.paste_focused_control(
                expected_text="new", user32=user32))

        fallback.assert_not_called()
        self.assertEqual(len(user32.paste_calls), 1)

    def test_pre_paste_query_timeout_uses_unconfirmed_key_fallback(self):
        user32 = _FakeUser32(query_before_delivered=False)
        with patch.object(windows_hotkeys, "send_ctrl_key", return_value=None) as fallback:
            self.assertIsNone(windows_hotkeys.paste_focused_control(
                expected_text="new", user32=user32))

        fallback.assert_called_once_with("v")
        self.assertEqual(user32.paste_calls, [])

    def test_post_paste_query_timeout_never_injects_again(self):
        user32 = _FakeUser32(query_after_delivered=False)
        with patch.object(windows_hotkeys, "send_ctrl_key", return_value=None) as fallback:
            self.assertIsNone(windows_hotkeys.paste_focused_control(
                expected_text="new", user32=user32))

        fallback.assert_not_called()
        self.assertEqual(len(user32.paste_calls), 1)

    def test_ignored_wm_paste_keeps_response_unconfirmed(self):
        user32 = _FakeUser32(mutate=False)
        with patch.object(windows_hotkeys, "send_ctrl_key", return_value=None) as fallback:
            self.assertIsNone(windows_hotkeys.paste_focused_control(
                expected_text="new", user32=user32))

        fallback.assert_not_called()
        self.assertEqual(len(user32.paste_calls), 1)

    def test_partial_normalization_does_not_inject_a_second_paste(self):
        user32 = _FakeUser32(paste_text="new\n")
        with patch.object(windows_hotkeys, "send_ctrl_key", return_value=None) as fallback:
            self.assertIsNone(windows_hotkeys.paste_focused_control(
                expected_text="new", user32=user32))

        fallback.assert_not_called()
        self.assertEqual(len(user32.paste_calls), 1)

    def test_read_only_control_keeps_response_unconfirmed(self):
        user32 = _FakeUser32(mutate=False)
        with patch.object(windows_hotkeys, "send_ctrl_key", return_value=None) as fallback:
            self.assertIsNone(windows_hotkeys.paste_focused_control(
                expected_text="new", user32=user32))

        fallback.assert_not_called()

    def test_custom_control_keeps_injected_paste_unconfirmed(self):
        user32 = _FakeUser32(class_name="Chrome_RenderWidgetHostHWND")
        with patch.object(windows_hotkeys, "send_ctrl_key", return_value=None) as fallback:
            self.assertIsNone(windows_hotkeys.paste_focused_control(
                expected_text="new", user32=user32))

        fallback.assert_called_once_with("v")
        self.assertEqual(user32.paste_calls, [])


if __name__ == "__main__":
    unittest.main()
