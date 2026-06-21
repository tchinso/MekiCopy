from __future__ import annotations

import ctypes
import os
import threading
import tkinter as tk
from ctypes import wintypes
from typing import Callable

from mekicopy_ocr import _log_runtime_error
from mekicopy_runtime import _get_icon_path

_HICON = getattr(wintypes, "HICON", wintypes.HANDLE)
_LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _WndClassW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", _HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _NotifyIconDataW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", _HICON),
        ("szTip", wintypes.WCHAR * 128),
    ]


class WindowsTrayIcon:
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    WM_USER = 0x0400
    WM_TRAY_CALLBACK = WM_USER + 20
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    LR_DEFAULTSIZE = 0x00000040
    IDI_APPLICATION = 32512
    RESTORE_POLL_MS = 25

    def __init__(
        self,
        root: tk.Tk,
        tooltip: str,
        on_restore: Callable[[], None],
    ) -> None:
        self.root = root
        self.tooltip = tooltip[:127]
        self.on_restore = on_restore
        self._hwnd: int | None = None
        self._hicon: int | None = None
        self._hinstance: int | None = None
        self._window_class_name: str | None = None
        self._window_class_atom: int | None = None
        self._wndproc = None
        self._active = False
        self._restore_requested = threading.Event()
        self._closed = False
        self._poll_after_id = self.root.after(
            self.RESTORE_POLL_MS,
            self._poll_restore_requests,
        )

    def show(self) -> bool:
        if os.name != "nt" or self._active:
            return False
        try:
            if not self._hwnd:
                self._create_message_window()
            if not self._hicon:
                self._hicon = self._load_icon()
            data = self._build_notify_data(self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP)
            shell32 = ctypes.windll.shell32
            if not shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(data)):
                return False
            self._active = True
            return True
        except Exception as exc:
            _log_runtime_error("tray_show", exc)
            self._destroy_message_window()
            return False

    def hide(self) -> None:
        if os.name != "nt":
            return
        if self._active and self._hwnd:
            try:
                data = self._build_notify_data(0)
                ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(data))
            except Exception:
                pass
        self._active = False
        self._restore_requested.clear()

    def close(self) -> None:
        """Remove the icon and native message window during application shutdown."""
        if self._closed:
            return
        self._closed = True
        self.hide()
        if self._poll_after_id:
            try:
                self.root.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        self._destroy_message_window()

    def _destroy_message_window(self) -> None:
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        if self._hwnd:
            try:
                user32.DestroyWindow.argtypes = [wintypes.HWND]
                user32.DestroyWindow.restype = wintypes.BOOL
                user32.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None
        if self._window_class_name and self._hinstance:
            try:
                user32.UnregisterClassW.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.HINSTANCE,
                ]
                user32.UnregisterClassW.restype = wintypes.BOOL
                user32.UnregisterClassW(self._window_class_name, self._hinstance)
            except Exception:
                pass
        self._window_class_name = None
        self._window_class_atom = None
        self._hinstance = None
        self._wndproc = None

    def _poll_restore_requests(self) -> None:
        self._poll_after_id = None
        if self._closed:
            return
        if self._restore_requested.is_set():
            self._restore_requested.clear()
            if self._active:
                try:
                    self.on_restore()
                except Exception as exc:
                    _log_runtime_error("tray_restore", exc)
        try:
            self._poll_after_id = self.root.after(
                self.RESTORE_POLL_MS,
                self._poll_restore_requests,
            )
        except tk.TclError:
            self._closed = True

    def _build_notify_data(self, flags: int) -> _NotifyIconDataW:
        data = _NotifyIconDataW()
        data.cbSize = ctypes.sizeof(_NotifyIconDataW)
        data.hWnd = self._hwnd or 0
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = self.WM_TRAY_CALLBACK
        data.hIcon = self._hicon or 0
        data.szTip = self.tooltip
        return data

    def _load_icon(self) -> int:
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = _HICON
        icon_path = _get_icon_path()
        if icon_path:
            handle = user32.LoadImageW(
                None,
                icon_path,
                self.IMAGE_ICON,
                0,
                0,
                self.LR_LOADFROMFILE | self.LR_DEFAULTSIZE,
            )
            if handle:
                return int(handle)
        user32.LoadIconW.restype = _HICON
        return int(user32.LoadIconW(None, self.IDI_APPLICATION))

    def _create_message_window(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.DefWindowProcW.restype = _LRESULT
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]

        def _window_proc(hwnd, msg, wparam, lparam):
            if msg == self.WM_TRAY_CALLBACK and lparam in (
                self.WM_LBUTTONDBLCLK,
                self.WM_RBUTTONUP,
            ):
                # A ctypes WndProc must remain tiny. Calling into Tk from here can
                # re-enter Tcl while Windows is dispatching the native message and
                # has caused hard process crashes. The Tk thread polls this event.
                self._restore_requested.set()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = _WNDPROC(_window_proc)
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._hinstance = kernel32.GetModuleHandleW(None)
        self._window_class_name = (
            f"MekiCopy.TrayMessageWindow.{os.getpid()}.{id(self):x}"
        )
        window_class = _WndClassW()
        window_class.lpfnWndProc = self._wndproc
        window_class.hInstance = self._hinstance
        window_class.lpszClassName = self._window_class_name
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.RegisterClassW.argtypes = [ctypes.POINTER(_WndClassW)]
        self._window_class_atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not self._window_class_atom:
            raise ctypes.WinError(ctypes.get_last_error())

        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        self._hwnd = user32.CreateWindowExW(
            0,
            self._window_class_name,
            self.tooltip,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            self._hinstance,
            None,
        )
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
