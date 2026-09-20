"""Small, event-driven Win32 enhancements for Tk overlay windows.

Tk's ``-topmost`` attribute remains the portable source of truth.  This
module only reinforces it on Windows after a foreground-window transition,
without activating the overlay or repeatedly competing for Z-order.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any

from system_logging import log_debug


GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002


def _handle_value(value: Any) -> int:
    """Return a Win32 handle value without assuming ctypes' return shape."""

    if value is None:
        return 0
    raw_value = getattr(value, "value", value)
    try:
        return int(raw_value or 0)
    except (TypeError, ValueError):
        return 0


def _user32() -> Any:
    """Load user32 lazily so importing this module is harmless off Windows."""

    if os.name != "nt":
        return None
    return ctypes.WinDLL("user32", use_last_error=True)


def get_top_level_hwnd(root: Any) -> int | None:
    """Find the real top-level HWND for a Tk widget.

    ``winfo_id()`` can identify Tk's child window, so use ``GetAncestor`` to
    obtain the native root window before applying a window-level API.
    """

    if os.name != "nt":
        return None
    try:
        widget_hwnd = _handle_value(root.winfo_id())
        if not widget_hwnd:
            return None
        user32 = _user32()
        if user32 is None:
            return None
        get_ancestor = user32.GetAncestor
        get_ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
        get_ancestor.restype = wintypes.HWND
        top_level_hwnd = _handle_value(get_ancestor(widget_hwnd, GA_ROOT))
        return top_level_hwnd or widget_hwnd
    except Exception:
        return None


class TopmostWindowController:
    """Reassert an overlay's native topmost state only after foreground changes.

    The controller deliberately does not use activation APIs.  It is a safe
    no-op on non-Windows platforms or whenever an optional Win32 call fails;
    Tk's existing ``-topmost`` handling continues to work in either case.
    """

    FOREGROUND_RECHECK_DELAYS_MS = (500, 3000)

    def __init__(
        self,
        root: Any,
        *,
        enabled: bool = True,
        no_activate: bool = False,
        debug_log: bool = False,
    ) -> None:
        self.root = root
        self._enabled = bool(enabled)
        self._no_activate = bool(no_activate)
        self._debug_log = bool(debug_log)
        self._started = False
        self._closed = False
        self._hook: int | None = None
        # Keep this CFUNCTYPE instance alive for the full hook lifetime.
        self._win_event_callback: Any = None
        self._last_hwnd: int | None = None
        self._foreground_dispatch_after_id: str | None = None
        self._retry_after_ids: list[str] = []

    def start(self) -> None:
        """Enable native reinforcement after Tk has created its window."""

        if self._closed or self._started:
            return
        self._started = True
        self._reconcile_foreground_hook()
        self.refresh()
        self._debug("started", "Topmost controller started")

    def refresh(self) -> bool:
        """Apply the current native topmost policy without changing foreground."""

        if self._closed or not self._started or os.name != "nt":
            return False
        hwnd = get_top_level_hwnd(self.root)
        if not hwnd:
            self._debug("refresh", "Topmost refresh skipped: native HWND unavailable")
            return False
        if hwnd != self._last_hwnd:
            self._last_hwnd = hwnd
            self._debug("hwnd", f"Native HWND changed: 0x{hwnd:X}")

        try:
            user32 = _user32()
            if user32 is None:
                return False
            if self._no_activate:
                self._apply_no_activate(user32, hwnd)

            set_window_pos = user32.SetWindowPos
            set_window_pos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            set_window_pos.restype = wintypes.BOOL
            insert_after = HWND_TOPMOST if self._enabled else HWND_NOTOPMOST
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            if not set_window_pos(
                hwnd,
                wintypes.HWND(insert_after),
                0,
                0,
                0,
                0,
                flags,
            ):
                error_code = ctypes.get_last_error()
                self._debug(
                    "refresh",
                    f"SetWindowPos failed (error={error_code})",
                )
                return False
            self._debug(
                "refresh",
                "Topmost reasserted" if self._enabled else "Topmost removed",
            )
            return True
        except Exception as exc:
            self._debug("refresh", f"Win32 topmost enhancement unavailable: {exc}")
            return False

    def set_enabled(self, enabled: bool) -> None:
        """Change topmost policy and apply it immediately once started."""

        if self._closed:
            return
        self._enabled = bool(enabled)
        if not self._started:
            return
        self._reconcile_foreground_hook()
        self.refresh()

    def set_debug_log(self, enabled: bool) -> None:
        """Update detailed native-event logging without changing behavior."""

        self._debug_log = bool(enabled)

    def close(self) -> None:
        """Cancel event-triggered callbacks and release the WinEvent hook."""

        if self._closed:
            return
        self._closed = True
        self._cancel_scheduled_refreshes()
        self._remove_foreground_hook()
        self._debug("closed", "Topmost controller closed")

    def _apply_no_activate(self, user32: Any, hwnd: int) -> None:
        """Add WS_EX_NOACTIVATE while preserving all existing extended styles."""

        try:
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                get_window_long = user32.GetWindowLongPtrW
                set_window_long = user32.SetWindowLongPtrW
                long_type = ctypes.c_ssize_t
            else:
                get_window_long = user32.GetWindowLongW
                set_window_long = user32.SetWindowLongW
                long_type = ctypes.c_long
            get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
            get_window_long.restype = long_type
            set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, long_type]
            set_window_long.restype = long_type

            current_style = int(get_window_long(hwnd, GWL_EXSTYLE))
            if current_style & WS_EX_NOACTIVATE:
                return
            updated_style = current_style | WS_EX_NOACTIVATE
            ctypes.set_last_error(0)
            previous_style = set_window_long(hwnd, GWL_EXSTYLE, updated_style)
            error_code = ctypes.get_last_error()
            if not previous_style and error_code:
                self._debug(
                    "no_activate",
                    f"SetWindowLong failed (error={error_code})",
                )
                return

            set_window_pos = user32.SetWindowPos
            set_window_pos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            set_window_pos.restype = wintypes.BOOL
            if not set_window_pos(
                hwnd,
                wintypes.HWND(
                    HWND_TOPMOST if self._enabled else HWND_NOTOPMOST
                ),
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            ):
                self._debug(
                    "no_activate",
                    f"SetWindowPos style refresh failed (error={ctypes.get_last_error()})",
                )
                return
            self._debug("no_activate", "WS_EX_NOACTIVATE applied")
        except Exception as exc:
            self._debug("no_activate", f"WS_EX_NOACTIVATE unavailable: {exc}")

    def _reconcile_foreground_hook(self) -> None:
        if self._closed:
            return
        if not self._enabled:
            self._cancel_scheduled_refreshes()
            self._remove_foreground_hook()
        elif os.name == "nt":
            self._install_foreground_hook()

    def _install_foreground_hook(self) -> None:
        if self._hook is not None:
            return
        try:
            user32 = _user32()
            if user32 is None:
                return
            callback_type = ctypes.WINFUNCTYPE(
                None,
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.HWND,
                wintypes.LONG,
                wintypes.LONG,
                wintypes.DWORD,
                wintypes.DWORD,
            )
            self._win_event_callback = callback_type(self._on_win_event)
            set_win_event_hook = user32.SetWinEventHook
            set_win_event_hook.argtypes = [
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HMODULE,
                callback_type,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            set_win_event_hook.restype = wintypes.HANDLE
            hook = _handle_value(
                set_win_event_hook(
                    EVENT_SYSTEM_FOREGROUND,
                    EVENT_SYSTEM_FOREGROUND,
                    None,
                    self._win_event_callback,
                    0,
                    0,
                    WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
                )
            )
            if not hook:
                self._win_event_callback = None
                self._debug(
                    "hook",
                    f"SetWinEventHook failed (error={ctypes.get_last_error()})",
                )
                return
            self._hook = hook
            self._debug("hook", "WinEventHook installed")
        except Exception as exc:
            self._win_event_callback = None
            self._debug("hook", f"WinEventHook unavailable: {exc}")

    def _remove_foreground_hook(self) -> None:
        hook = self._hook
        self._hook = None
        if hook is None:
            self._win_event_callback = None
            return
        try:
            user32 = _user32()
            if user32 is not None:
                unhook_win_event = user32.UnhookWinEvent
                unhook_win_event.argtypes = [wintypes.HANDLE]
                unhook_win_event.restype = wintypes.BOOL
                if not unhook_win_event(hook):
                    self._debug(
                        "hook",
                        f"UnhookWinEvent failed (error={ctypes.get_last_error()})",
                    )
                else:
                    self._debug("hook", "WinEventHook removed")
        except Exception as exc:
            self._debug("hook", f"UnhookWinEvent unavailable: {exc}")
        finally:
            self._win_event_callback = None

    def _on_win_event(
        self,
        _hook: Any,
        event: int,
        _hwnd: Any,
        _object_id: int,
        _child_id: int,
        _event_thread: int,
        _event_time: int,
    ) -> None:
        """Schedule, rather than perform, Tk work from the WinEvent callback."""

        if (
            event != EVENT_SYSTEM_FOREGROUND
            or self._closed
            or not self._started
            or not self._enabled
        ):
            return
        self._debug("foreground", "Foreground changed")
        # WINEVENT_OUTOFCONTEXT delivers this callback on the hook-installing
        # UI thread.  ``after`` still keeps native callback work free of Tk
        # mutations and coalesces a burst of foreground notifications.
        if self._foreground_dispatch_after_id is not None:
            return
        try:
            self._foreground_dispatch_after_id = self.root.after(
                0,
                self._run_foreground_refresh,
            )
        except Exception as exc:
            self._debug("foreground", f"Unable to schedule topmost refresh: {exc}")

    def _run_foreground_refresh(self) -> None:
        self._foreground_dispatch_after_id = None
        if self._closed or not self._enabled:
            return
        self._cancel_retry_callbacks()
        self.refresh()
        for delay_ms in self.FOREGROUND_RECHECK_DELAYS_MS:
            try:
                self._schedule_retry(delay_ms)
            except Exception as exc:
                self._debug("foreground", f"Unable to schedule recheck: {exc}")
                continue

    def _schedule_retry(self, delay_ms: int) -> str:
        callback_id: str | None = None

        def run_retry() -> None:
            if callback_id is not None:
                try:
                    self._retry_after_ids.remove(callback_id)
                except ValueError:
                    pass
            if not self._closed and self._enabled:
                self.refresh()

        callback_id = self.root.after(delay_ms, run_retry)
        self._retry_after_ids.append(callback_id)
        return callback_id

    def _cancel_scheduled_refreshes(self) -> None:
        dispatch_id = self._foreground_dispatch_after_id
        self._foreground_dispatch_after_id = None
        if dispatch_id is not None:
            self._cancel_after(dispatch_id)
        self._cancel_retry_callbacks()

    def _cancel_retry_callbacks(self) -> None:
        callback_ids = self._retry_after_ids
        self._retry_after_ids = []
        for callback_id in callback_ids:
            self._cancel_after(callback_id)

    def _cancel_after(self, callback_id: str) -> None:
        try:
            self.root.after_cancel(callback_id)
        except Exception:
            pass

    def _debug(self, stage: str, message: str) -> None:
        log_debug(
            f"win32_overlay.{stage}",
            message,
            enabled=self._debug_log,
        )
