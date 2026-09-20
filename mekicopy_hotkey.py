"""Native Windows global-hotkey support for MekiCopy.

The implementation deliberately uses ``RegisterHotKey`` instead of a keyboard
hook or a third-party package.  It is therefore available in the packaged
application without an extra runtime dependency, and accepts ordinary keys
such as ``B`` without requiring Ctrl, Alt, or a function key.

Windows delivers hotkey notifications to a thread message queue.  This module
keeps that queue on a small worker thread and exposes an activation queue for
the Tk main thread to drain.  No worker thread ever calls into Tk.
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable


DEFAULT_GLOBAL_HOTKEY = "B"

# RegisterHotKey modifier flags.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_SHIFT_MASK = 0x0001
_CONTROL_MASK = 0x0004
_ALT_MASK = 0x0008
_WINDOWS_MASK = 0x0040

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_WM_APP = 0x8000
_WM_CONFIGURE = _WM_APP + 0x4D
_HOTKEY_ID = 0x4D43


@dataclass(frozen=True)
class Hotkey:
    """A normalized RegisterHotKey-compatible shortcut."""

    key: str
    modifiers: int
    virtual_key: int

    @property
    def text(self) -> str:
        parts: list[str] = []
        if self.modifiers & MOD_CONTROL:
            parts.append("Ctrl")
        if self.modifiers & MOD_ALT:
            parts.append("Alt")
        if self.modifiers & MOD_SHIFT:
            parts.append("Shift")
        if self.modifiers & MOD_WIN:
            parts.append("Win")
        parts.append(self.key)
        return "+".join(parts)


@dataclass(frozen=True)
class HotkeyRegistration:
    """Result returned after a registration request reaches the message thread."""

    ok: bool
    enabled: bool
    hotkey: str
    active: bool
    error: str | None = None


_KEY_DEFINITIONS: dict[str, int] = {
    "Backspace": 0x08,
    "Tab": 0x09,
    "Enter": 0x0D,
    "Pause": 0x13,
    "CapsLock": 0x14,
    "Esc": 0x1B,
    "Space": 0x20,
    "PageUp": 0x21,
    "PageDown": 0x22,
    "End": 0x23,
    "Home": 0x24,
    "Left": 0x25,
    "Up": 0x26,
    "Right": 0x27,
    "Down": 0x28,
    "PrintScreen": 0x2C,
    "Insert": 0x2D,
    "Delete": 0x2E,
    "NumLock": 0x90,
    "ScrollLock": 0x91,
    "Semicolon": 0xBA,
    "Equal": 0xBB,
    "Comma": 0xBC,
    "Minus": 0xBD,
    "Period": 0xBE,
    "Slash": 0xBF,
    "Backtick": 0xC0,
    "BracketLeft": 0xDB,
    "Backslash": 0xDC,
    "BracketRight": 0xDD,
    "Quote": 0xDE,
}
_KEY_DEFINITIONS.update({chr(code): code for code in range(ord("A"), ord("Z") + 1)})
_KEY_DEFINITIONS.update({str(number): ord(str(number)) for number in range(10)})
_KEY_DEFINITIONS.update(
    {f"F{number}": 0x70 + number - 1 for number in range(1, 25)}
)
_KEY_DEFINITIONS.update(
    {f"Num{number}": 0x60 + number for number in range(10)}
)

_KEY_ALIASES = {
    "backspace": "Backspace",
    "backspacekey": "Backspace",
    "backspace_key": "Backspace",
    "tab": "Tab",
    "return": "Enter",
    "enter": "Enter",
    "pause": "Pause",
    "capslock": "CapsLock",
    "caps_lock": "CapsLock",
    "escape": "Esc",
    "esc": "Esc",
    "space": "Space",
    "spacebar": "Space",
    "prior": "PageUp",
    "pageup": "PageUp",
    "page_up": "PageUp",
    "pgup": "PageUp",
    "next": "PageDown",
    "pagedown": "PageDown",
    "page_down": "PageDown",
    "pgdn": "PageDown",
    "end": "End",
    "home": "Home",
    "left": "Left",
    "up": "Up",
    "right": "Right",
    "down": "Down",
    "print": "PrintScreen",
    "printscreen": "PrintScreen",
    "print_screen": "PrintScreen",
    "insert": "Insert",
    "delete": "Delete",
    "del": "Delete",
    "numlock": "NumLock",
    "num_lock": "NumLock",
    "scrolllock": "ScrollLock",
    "scroll_lock": "ScrollLock",
    "semicolon": "Semicolon",
    ";": "Semicolon",
    "equal": "Equal",
    "equals": "Equal",
    "=": "Equal",
    "comma": "Comma",
    ",": "Comma",
    "minus": "Minus",
    "-": "Minus",
    "period": "Period",
    ".": "Period",
    "slash": "Slash",
    "/": "Slash",
    "backtick": "Backtick",
    "grave": "Backtick",
    "`": "Backtick",
    "bracketleft": "BracketLeft",
    "bracket_left": "BracketLeft",
    "[": "BracketLeft",
    "backslash": "Backslash",
    "\\": "Backslash",
    "bracketright": "BracketRight",
    "bracket_right": "BracketRight",
    "]": "BracketRight",
    "quote": "Quote",
    "apostrophe": "Quote",
    "'": "Quote",
}
_MODIFIER_ALIASES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "meta": MOD_WIN,
}


def _normalize_key_token(value: str) -> str | None:
    token = value.strip()
    if not token:
        return None
    compact = token.casefold().replace(" ", "").replace("_", "")
    if compact in _KEY_ALIASES:
        return _KEY_ALIASES[compact]
    if len(token) == 1 and token.isascii() and token.isalpha():
        return token.upper()
    if len(token) == 1 and token.isascii() and token.isdigit():
        return token
    if compact.startswith("f") and compact[1:].isdigit():
        number = int(compact[1:])
        if 1 <= number <= 24:
            return f"F{number}"
    if compact.startswith(("num", "numpad")):
        number_text = compact.removeprefix("numpad").removeprefix("num")
        if len(number_text) == 1 and number_text.isdigit():
            return f"Num{number_text}"
    return None


def parse_hotkey(value: object) -> Hotkey | None:
    """Parse a user-facing shortcut such as ``B`` or ``Ctrl+Alt+B``.

    A bare ordinary key is valid by design.  A shortcut must contain exactly
    one non-modifier key, and repeated/unknown modifiers are rejected.
    """

    raw = str(value or "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split("+")]
    if not parts or any(not part for part in parts):
        return None

    modifiers = 0
    key: str | None = None
    for part in parts:
        modifier = _MODIFIER_ALIASES.get(part.casefold())
        if modifier is not None:
            if modifiers & modifier:
                return None
            modifiers |= modifier
            continue
        normalized_key = _normalize_key_token(part)
        if normalized_key is None or key is not None:
            return None
        key = normalized_key
    if key is None:
        return None
    virtual_key = _KEY_DEFINITIONS.get(key)
    if virtual_key is None:
        return None
    return Hotkey(key=key, modifiers=modifiers, virtual_key=virtual_key)


def normalize_hotkey(value: object, fallback: str = DEFAULT_GLOBAL_HOTKEY) -> str:
    """Return a canonical shortcut string, falling back to a safe default."""

    parsed = parse_hotkey(value)
    if parsed is not None:
        return parsed.text
    parsed_fallback = parse_hotkey(fallback)
    return parsed_fallback.text if parsed_fallback is not None else DEFAULT_GLOBAL_HOTKEY


def hotkey_from_tk_event(keysym: object, state: object) -> str | None:
    """Convert a Tk keypress into a canonical shortcut for the settings field."""

    parsed = parse_hotkey(keysym)
    if parsed is None:
        return None
    try:
        key_state = int(state)
    except (TypeError, ValueError):
        key_state = 0
    modifiers = 0
    if key_state & _CONTROL_MASK:
        modifiers |= MOD_CONTROL
    if key_state & _ALT_MASK:
        modifiers |= MOD_ALT
    if key_state & _SHIFT_MASK:
        modifiers |= MOD_SHIFT
    if key_state & _WINDOWS_MASK:
        modifiers |= MOD_WIN
    return Hotkey(parsed.key, modifiers, parsed.virtual_key).text


class HotkeyRegistrationState:
    """Testable registration/replacement policy independent from Win32 calls."""

    def __init__(self) -> None:
        self._registered: Hotkey | None = None

    @property
    def registered(self) -> Hotkey | None:
        return self._registered

    def configure(
        self,
        *,
        enabled: bool,
        hotkey: Hotkey,
        register: Callable[[Hotkey], str | None],
        unregister: Callable[[Hotkey], None],
    ) -> HotkeyRegistration:
        desired = hotkey if enabled else None
        if desired == self._registered:
            return HotkeyRegistration(
                ok=True,
                enabled=enabled,
                hotkey=hotkey.text,
                active=desired is not None,
            )

        previous = self._registered
        if previous is not None:
            unregister(previous)
            self._registered = None

        if desired is not None:
            error = register(desired)
            if error:
                rollback_error = register(previous) if previous is not None else None
                if previous is not None and rollback_error is None:
                    self._registered = previous
                return HotkeyRegistration(
                    ok=False,
                    enabled=enabled,
                    hotkey=hotkey.text,
                    active=self._registered is not None,
                    error=error,
                )

        self._registered = desired
        return HotkeyRegistration(
            ok=True,
            enabled=enabled,
            hotkey=hotkey.text,
            active=desired is not None,
        )


class _WindowsHotkeyApi:
    """Small typed wrapper around the specific User32 calls we need."""

    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._user32.RegisterHotKey.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        self._user32.UnregisterHotKey.restype = wintypes.BOOL
        self._user32.PeekMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.PeekMessageW.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.GetMessageW.restype = ctypes.c_int
        self._user32.PostThreadMessageW.argtypes = (
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self._user32.PostThreadMessageW.restype = wintypes.BOOL
        self._kernel32.GetCurrentThreadId.argtypes = ()
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def create_message_queue(self) -> None:
        message = wintypes.MSG()
        # PM_NOREMOVE creates a queue without removing a real message.
        self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)

    def current_thread_id(self) -> int:
        return int(self._kernel32.GetCurrentThreadId())

    def next_message(self) -> tuple[int, int]:
        message = wintypes.MSG()
        result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
        if result == -1:
            code = ctypes.get_last_error()
            raise OSError(code, ctypes.FormatError(code).strip())
        return int(message.message), int(message.wParam)

    def post_thread_message(self, thread_id: int, message: int) -> bool:
        return bool(self._user32.PostThreadMessageW(thread_id, message, 0, 0))

    def register(self, hotkey: Hotkey) -> str | None:
        ctypes.set_last_error(0)
        if self._user32.RegisterHotKey(
            None,
            _HOTKEY_ID,
            hotkey.modifiers | MOD_NOREPEAT,
            hotkey.virtual_key,
        ):
            return None
        code = ctypes.get_last_error()
        if code == 1409:
            return "다른 프로그램에서 이미 사용 중인 단축키입니다. 다른 키를 지정하세요."
        detail = ctypes.FormatError(code).strip() if code else "알 수 없는 Windows 오류"
        return f"Windows 오류 {code}: {detail}"

    def unregister(self, _hotkey: Hotkey) -> None:
        self._user32.UnregisterHotKey(None, _HOTKEY_ID)


@dataclass
class _ConfigureRequest:
    enabled: bool
    hotkey: Hotkey
    done: threading.Event
    result: HotkeyRegistration | None = None
    cancelled: bool = False


class GlobalHotkeyManager:
    """Own a Windows global hotkey and queue activations for Tk to consume."""

    START_TIMEOUT_SECONDS = 2.0
    CONFIGURE_TIMEOUT_SECONDS = 2.0

    def __init__(self) -> None:
        self._activation_queue: queue.Queue[None] = queue.Queue()
        self._request_queue: queue.Queue[_ConfigureRequest] = queue.Queue()
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._closed = False
        self._startup_error: str | None = None
        self._api: _WindowsHotkeyApi | None = None
        if os.name == "nt":
            try:
                self._api = _WindowsHotkeyApi()
            except Exception as exc:
                self._startup_error = str(exc)

    @property
    def supported(self) -> bool:
        return self._api is not None

    def configure(self, *, enabled: bool, hotkey: object) -> HotkeyRegistration:
        parsed = parse_hotkey(hotkey)
        if enabled and parsed is None:
            return HotkeyRegistration(
                ok=False,
                enabled=True,
                hotkey=str(hotkey or ""),
                active=False,
                error="전역 단축키 형식이 올바르지 않습니다.",
            )
        if parsed is None:
            parsed = parse_hotkey(DEFAULT_GLOBAL_HOTKEY)
        assert parsed is not None

        if self._api is None:
            # The desktop app is Windows-only, but retaining a harmless no-op
            # makes settings and parser tests portable.
            return HotkeyRegistration(
                ok=True,
                enabled=enabled,
                hotkey=parsed.text,
                active=False,
            )
        if not self._ensure_worker():
            return HotkeyRegistration(
                ok=False,
                enabled=enabled,
                hotkey=parsed.text,
                active=False,
                error=self._startup_error or "전역 단축키 처리 스레드를 시작하지 못했습니다.",
            )

        request = _ConfigureRequest(enabled, parsed, threading.Event())
        self._request_queue.put(request)
        with self._lock:
            thread_id = self._thread_id
            closed = self._closed
        if closed or not thread_id or not self._api.post_thread_message(
            thread_id, _WM_CONFIGURE
        ):
            request.cancelled = True
            return HotkeyRegistration(
                ok=False,
                enabled=enabled,
                hotkey=parsed.text,
                active=False,
                error="전역 단축키 처리 스레드와 통신하지 못했습니다.",
            )
        if not request.done.wait(self.CONFIGURE_TIMEOUT_SECONDS):
            request.cancelled = True
            return HotkeyRegistration(
                ok=False,
                enabled=enabled,
                hotkey=parsed.text,
                active=False,
                error="전역 단축키 등록 응답 시간이 초과되었습니다.",
            )
        return request.result or HotkeyRegistration(
            ok=False,
            enabled=enabled,
            hotkey=parsed.text,
            active=False,
            error="전역 단축키 등록 결과를 받지 못했습니다.",
        )

    def drain_activations(self) -> int:
        """Return queued hotkey presses; call this from the Tk thread only."""

        count = 0
        while True:
            try:
                self._activation_queue.get_nowait()
            except queue.Empty:
                return count
            count += 1

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread_id = self._thread_id
            thread = self._thread
        if self._api is not None and thread_id:
            self._api.post_thread_message(thread_id, _WM_QUIT)
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._fail_pending("전역 단축키 처리 스레드가 종료되었습니다.")

    def _ensure_worker(self) -> bool:
        with self._lock:
            if self._closed:
                self._startup_error = "프로그램이 종료 중입니다."
                return False
            if self._thread is not None and not self._thread.is_alive():
                # A transient User32/message-queue failure should not require
                # restarting all of MekiCopy before the user can try another
                # shortcut from Settings.
                self._thread = None
                self._thread_id = 0
                self._ready = threading.Event()
                self._startup_error = None
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="MekiCopyGlobalHotkey",
                    daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(self.START_TIMEOUT_SECONDS):
            self._startup_error = "전역 단축키 처리 스레드 시작 시간이 초과되었습니다."
            return False
        return self._startup_error is None

    def _run(self) -> None:
        api = self._api
        state = HotkeyRegistrationState()
        try:
            if api is None:
                raise RuntimeError("Windows User32 API를 사용할 수 없습니다.")
            api.create_message_queue()
            with self._lock:
                self._thread_id = api.current_thread_id()
                closed = self._closed
            self._ready.set()
            if closed:
                return
            while True:
                message, wparam = api.next_message()
                if message == _WM_QUIT:
                    break
                if message == _WM_CONFIGURE:
                    self._handle_configuration(state, api)
                elif message == _WM_HOTKEY and wparam == _HOTKEY_ID:
                    self._activation_queue.put(None)
        except Exception as exc:
            with self._lock:
                if self._startup_error is None:
                    self._startup_error = str(exc) or type(exc).__name__
            self._ready.set()
        finally:
            if state.registered is not None and api is not None:
                api.unregister(state.registered)
            with self._lock:
                self._thread_id = 0
            self._fail_pending(self._startup_error or "전역 단축키 처리 스레드가 종료되었습니다.")

    def _handle_configuration(
        self,
        state: HotkeyRegistrationState,
        api: _WindowsHotkeyApi,
    ) -> None:
        try:
            request = self._request_queue.get_nowait()
        except queue.Empty:
            return
        if request.cancelled:
            return
        request.result = state.configure(
            enabled=request.enabled,
            hotkey=request.hotkey,
            register=api.register,
            unregister=api.unregister,
        )
        request.done.set()

    def _fail_pending(self, error: str) -> None:
        while True:
            try:
                request = self._request_queue.get_nowait()
            except queue.Empty:
                return
            if request.done.is_set() or request.cancelled:
                continue
            request.result = HotkeyRegistration(
                ok=False,
                enabled=request.enabled,
                hotkey=request.hotkey.text,
                active=False,
                error=error,
            )
            request.done.set()
