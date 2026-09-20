from __future__ import annotations

import ctypes
import unittest
from types import SimpleNamespace
from unittest import mock

import win32_overlay


class _FakeRoot:
    def __init__(self) -> None:
        self._next_after_id = 1
        self.callbacks: dict[str, tuple[int, object]] = {}
        self.cancelled: list[str] = []

    def winfo_id(self) -> int:
        return 123

    def after(self, delay_ms: int, callback: object) -> str:
        callback_id = f"after-{self._next_after_id}"
        self._next_after_id += 1
        self.callbacks[callback_id] = (delay_ms, callback)
        return callback_id

    def after_cancel(self, callback_id: str) -> None:
        self.cancelled.append(callback_id)
        self.callbacks.pop(callback_id, None)

    def fire(self, callback_id: str) -> None:
        _delay, callback = self.callbacks.pop(callback_id)
        assert callable(callback)
        callback()


class _WinFunction:
    def __init__(self, return_value: object) -> None:
        self.return_value = return_value
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> object:
        self.calls.append(args)
        return self.return_value


class Win32OverlayTests(unittest.TestCase):
    def _foreground_event(
        self,
        controller: win32_overlay.TopmostWindowController,
    ) -> None:
        controller._on_win_event(
            None,
            win32_overlay.EVENT_SYSTEM_FOREGROUND,
            None,
            0,
            0,
            0,
            0,
        )

    @staticmethod
    def _callback_id_for_delay(root: _FakeRoot, delay_ms: int) -> str:
        return next(
            callback_id
            for callback_id, (delay, _callback) in root.callbacks.items()
            if delay == delay_ms
        )

    def test_non_windows_controller_is_a_noop(self) -> None:
        root = _FakeRoot()
        with mock.patch.object(win32_overlay.os, "name", "posix"):
            controller = win32_overlay.TopmostWindowController(root)
            self.assertFalse(controller.refresh())
            controller.start()
            self.assertFalse(controller.refresh())
            self.assertEqual(root.callbacks, {})
            self.assertIsNone(win32_overlay.get_top_level_hwnd(root))

    def test_foreground_rechecks_are_coalesced_and_removed_after_running(self) -> None:
        root = _FakeRoot()
        with mock.patch.object(win32_overlay.os, "name", "posix"):
            controller = win32_overlay.TopmostWindowController(root)
            controller.start()
            controller.refresh = mock.Mock()  # type: ignore[method-assign]

            self._foreground_event(controller)
            self._foreground_event(controller)
            immediate_id = self._callback_id_for_delay(root, 0)
            self.assertEqual(len(root.callbacks), 1)

            root.fire(immediate_id)
            self.assertEqual(controller.refresh.call_count, 1)
            first_retry_ids = list(controller._retry_after_ids)
            self.assertEqual(
                [root.callbacks[callback_id][0] for callback_id in first_retry_ids],
                [500, 3000],
            )

            root.fire(first_retry_ids[0])
            root.fire(first_retry_ids[1])
            self.assertEqual(controller.refresh.call_count, 3)
            self.assertEqual(controller._retry_after_ids, [])

    def test_new_foreground_event_replaces_pending_rechecks(self) -> None:
        root = _FakeRoot()
        with mock.patch.object(win32_overlay.os, "name", "posix"):
            controller = win32_overlay.TopmostWindowController(root)
            controller.start()
            controller.refresh = mock.Mock()  # type: ignore[method-assign]

            self._foreground_event(controller)
            root.fire(self._callback_id_for_delay(root, 0))
            old_retry_ids = list(controller._retry_after_ids)

            self._foreground_event(controller)
            root.fire(self._callback_id_for_delay(root, 0))

            self.assertTrue(set(old_retry_ids).issubset(root.cancelled))
            self.assertEqual(
                [root.callbacks[callback_id][0] for callback_id in controller._retry_after_ids],
                [500, 3000],
            )

    def test_disabling_or_closing_cancels_event_triggered_callbacks(self) -> None:
        root = _FakeRoot()
        with mock.patch.object(win32_overlay.os, "name", "posix"):
            controller = win32_overlay.TopmostWindowController(root)
            controller.start()
            self._foreground_event(controller)
            pending_id = self._callback_id_for_delay(root, 0)

            controller.set_enabled(False)
            self.assertIn(pending_id, root.cancelled)
            self.assertEqual(root.callbacks, {})
            self._foreground_event(controller)
            self.assertEqual(root.callbacks, {})

            controller.set_enabled(True)
            self._foreground_event(controller)
            new_pending_id = self._callback_id_for_delay(root, 0)
            controller.close()
            self.assertIn(new_pending_id, root.cancelled)
            self.assertEqual(root.callbacks, {})

    def test_top_level_hwnd_uses_getancestor_and_refresh_is_noactivate(self) -> None:
        root = _FakeRoot()
        get_ancestor = _WinFunction(456)
        set_window_pos = _WinFunction(True)
        fake_user32 = SimpleNamespace(
            GetAncestor=get_ancestor,
            SetWindowPos=set_window_pos,
        )
        with (
            mock.patch.object(win32_overlay.os, "name", "nt"),
            mock.patch.object(win32_overlay, "_user32", return_value=fake_user32),
        ):
            self.assertEqual(win32_overlay.get_top_level_hwnd(root), 456)
            self.assertEqual(get_ancestor.calls, [(123, win32_overlay.GA_ROOT)])

            controller = win32_overlay.TopmostWindowController(root, enabled=False)
            controller.start()

        self.assertEqual(len(set_window_pos.calls), 1)
        hwnd, insert_after, _x, _y, _width, _height, flags = set_window_pos.calls[0]
        self.assertEqual(hwnd, 456)
        self.assertEqual(
            insert_after.value,
            ctypes.c_void_p(win32_overlay.HWND_NOTOPMOST).value,
        )
        self.assertEqual(
            flags,
            win32_overlay.SWP_NOMOVE
            | win32_overlay.SWP_NOSIZE
            | win32_overlay.SWP_NOACTIVATE,
        )

    def test_enabled_controller_installs_and_releases_one_foreground_hook(self) -> None:
        root = _FakeRoot()
        get_ancestor = _WinFunction(456)
        set_window_pos = _WinFunction(True)
        set_win_event_hook = _WinFunction(987)
        unhook_win_event = _WinFunction(True)
        fake_user32 = SimpleNamespace(
            GetAncestor=get_ancestor,
            SetWindowPos=set_window_pos,
            SetWinEventHook=set_win_event_hook,
            UnhookWinEvent=unhook_win_event,
        )

        def fake_winfunctype(*_args: object) -> object:
            return lambda callback: callback

        with (
            mock.patch.object(win32_overlay.os, "name", "nt"),
            mock.patch.object(win32_overlay, "_user32", return_value=fake_user32),
            mock.patch.object(win32_overlay.ctypes, "WINFUNCTYPE", fake_winfunctype),
        ):
            controller = win32_overlay.TopmostWindowController(root, enabled=True)
            controller.start()
            controller.set_enabled(True)
            controller.close()

        self.assertEqual(len(set_win_event_hook.calls), 1)
        self.assertEqual(
            set_win_event_hook.calls[0][-1],
            win32_overlay.WINEVENT_OUTOFCONTEXT
            | win32_overlay.WINEVENT_SKIPOWNPROCESS,
        )
        self.assertEqual(unhook_win_event.calls, [(987,)])


if __name__ == "__main__":
    unittest.main()
