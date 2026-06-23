from __future__ import annotations

import datetime as dt
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import TextIO


_component = "MekiCopy"
_debug_enabled = False
_write_lock = threading.Lock()
_installed_hooks = False


def suite_root() -> Path:
    override = os.environ.get("MEKICOPY_LOG_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent

    executable_dir = Path(sys.executable).resolve().parent
    if executable_dir.name.casefold() == "mekicopy":
        return executable_dir
    sibling = executable_dir.parent / "MekiCopy"
    try:
        sibling.mkdir(parents=True, exist_ok=True)
        return sibling
    except OSError:
        return executable_dir


def log_directory(kind: str) -> Path:
    directory = suite_root() / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def configure_system_logging(component: str, debug_enabled: bool = False) -> None:
    global _component, _debug_enabled
    _component = str(component).strip() or "MekiCopy"
    _debug_enabled = bool(debug_enabled)
    os.environ["MEKICOPY_DEBUG_LOG"] = "1" if _debug_enabled else "0"


def set_debug_enabled(enabled: bool) -> None:
    global _debug_enabled
    _debug_enabled = bool(enabled)
    os.environ["MEKICOPY_DEBUG_LOG"] = "1" if _debug_enabled else "0"


def is_debug_enabled() -> bool:
    return _debug_enabled or os.environ.get("MEKICOPY_DEBUG_LOG", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _safe_component_name(component: str) -> str:
    return "".join(char for char in component if char.isalnum() or char in "-_") or "MekiCopy"


def _append(kind: str, component: str, text: str) -> None:
    filename = f"{_safe_component_name(component).lower()}.log"
    payload = (text.rstrip() + "\n").encode("utf-8", errors="replace")
    try:
        path = log_directory(kind) / filename
        with _write_lock, path.open("ab", buffering=0) as handle:
            handle.write(payload)
    except OSError:
        pass


def _header(level: str, component: str, stage: str) -> list[str]:
    return [
        "",
        f"=== {level} ===",
        f"time: {dt.datetime.now().astimezone().isoformat(timespec='milliseconds')}",
        f"component: {component}",
        f"stage: {stage}",
        f"pid: {os.getpid()}",
        f"thread: {threading.current_thread().name}",
    ]


def log_error(
    stage: str,
    value: BaseException | str,
    *,
    component: str | None = None,
    traceback_text: str | None = None,
) -> None:
    owner = component or _component
    lines = _header("ERROR", owner, stage)
    if isinstance(value, BaseException):
        lines.extend((f"type: {type(value).__name__}", f"message: {value}"))
        if traceback_text is None:
            traceback_text = "".join(
                traceback.format_exception(type(value), value, value.__traceback__)
            )
    else:
        lines.append(f"message: {value}")
    if traceback_text:
        lines.append(traceback_text.rstrip())
    _append("error_log", owner, "\n".join(lines))


def log_debug(
    stage: str,
    message: str,
    *,
    component: str | None = None,
    enabled: bool | None = None,
) -> None:
    if not (is_debug_enabled() if enabled is None else enabled):
        return
    owner = component or _component
    lines = _header("DEBUG", owner, stage)
    lines.append(str(message))
    _append("debug_log", owner, "\n".join(lines))


class _LogStream:
    def __init__(self, level: str, fallback: TextIO | None = None) -> None:
        self.level = level
        self.fallback = fallback
        self._buffer = ""
        self.encoding = "utf-8"

    def write(self, value: object) -> int:
        text = str(value)
        if self.fallback is not None:
            try:
                self.fallback.write(text)
            except Exception:
                pass
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                if self.level == "error":
                    log_error("stderr", line)
                else:
                    log_debug("stdout", line)
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            if self.level == "error":
                log_error("stderr", self._buffer)
            else:
                log_debug("stdout", self._buffer)
        self._buffer = ""
        if self.fallback is not None:
            try:
                self.fallback.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False


def capture_windowed_streams() -> None:
    if sys.stderr is None:
        sys.stderr = _LogStream("error")  # type: ignore[assignment]
    if sys.stdout is None:
        sys.stdout = _LogStream("debug")  # type: ignore[assignment]


def install_exception_hooks() -> None:
    global _installed_hooks
    if _installed_hooks:
        return
    _installed_hooks = True
    previous_sys_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def sys_hook(exc_type, exc_value, exc_tb) -> None:
        log_error(
            "unhandled_exception",
            exc_value,
            traceback_text="".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        if previous_sys_hook is not sys_hook:
            previous_sys_hook(exc_type, exc_value, exc_tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        log_error(
            "unhandled_thread_exception",
            args.exc_value,
            traceback_text="".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            ),
        )
        if previous_thread_hook is not thread_hook:
            previous_thread_hook(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook


def install_tk_exception_hook(root) -> None:
    def report_callback_exception(exc_type, exc_value, exc_tb) -> None:
        log_error(
            "tk_callback",
            exc_value,
            traceback_text="".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )

    root.report_callback_exception = report_callback_exception
