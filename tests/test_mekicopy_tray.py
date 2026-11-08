from __future__ import annotations

import ctypes
import os
import unittest
from unittest import mock

from mekicopy_tray import WindowsTrayIcon, _NotifyIconDataW


@unittest.skipUnless(os.name == "nt", "requires the Windows notification-area ABI")
class TrayShellTests(unittest.TestCase):
    def test_notify_data_matches_windows_sdk_layout(self):
        # Values from the Windows SDK NOTIFYICONDATAW ABI, including fields
        # after szTip even when balloon notifications are not requested.
        is_x64 = ctypes.sizeof(ctypes.c_void_p) == 8
        self.assertEqual(ctypes.sizeof(_NotifyIconDataW), 976 if is_x64 else 956)
        self.assertEqual(_NotifyIconDataW.guidItem.offset, 952 if is_x64 else 936)
        self.assertEqual(_NotifyIconDataW.hBalloonIcon.offset, 968 if is_x64 else 952)

    def test_shell_receives_complete_data_and_preserves_error_code(self):
        tray = WindowsTrayIcon(mock.Mock(), "MekiCopy", mock.Mock())
        tray._hwnd, tray._hicon = 123, 456
        calls = []

        def reject(message, pointer):
            data = ctypes.cast(pointer, ctypes.POINTER(_NotifyIconDataW)).contents
            calls.append((message, data.cbSize, data.hWnd, data.hIcon, data.uFlags))
            ctypes.set_last_error(5)
            return False

        notify = mock.Mock(side_effect=reject)
        with mock.patch("mekicopy_tray.ctypes.WinDLL", return_value=mock.Mock(Shell_NotifyIconW=notify)):
            with self.assertRaises(OSError) as raised:
                tray._notify_icon(tray.NIM_ADD, tray.NIF_ICON)
        self.assertEqual(raised.exception.winerror, 5)
        self.assertEqual(calls, [(tray.NIM_ADD, ctypes.sizeof(_NotifyIconDataW), 123, 456, tray.NIF_ICON)])

    def test_rejected_icon_is_logged_and_does_not_become_active(self):
        tray = WindowsTrayIcon(mock.Mock(), "MekiCopy", mock.Mock())
        tray._hwnd, tray._hicon = 123, 456
        failure = ctypes.WinError(5)
        with mock.patch.object(tray, "_notify_icon", side_effect=failure), \
             mock.patch.object(tray, "_destroy_message_window") as destroy, \
             mock.patch("mekicopy_tray._log_runtime_error") as log:
            self.assertFalse(tray.show())
        self.assertFalse(tray._active)
        destroy.assert_called_once_with()
        log.assert_called_once_with("tray_show", failure)


if __name__ == "__main__":
    unittest.main()
