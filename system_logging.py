from __future__ import annotations

import datetime as dt
import faulthandler
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
_fault_handle: TextIO | None = None
_log_directory_cache: dict[tuple[str, str], Path] = {}
_MAX_LOG_BYTES = 20 * 1024 * 1024
_LOG_BACKUPS = 3
_NATIVE_LOG_RETENTION = 8


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


def _log_directory_candidates(kind: str, component: str) -> list[Path]:
    override = os.environ.get("MEKICOPY_LOG_ROOT")
    candidates: list[Path] = []
    cached = _log_directory_cache.get((component, kind))
    if cached is not None:
        candidates.append(cached)
    if override:
        candidates.append(Path(override).expanduser().resolve() / kind)
    try:
        from runtime_paths import app_root, fallback_app_data_dirs, writable_app_subdir

        candidates.extend(
            [
                writable_app_subdir(component, kind),
                app_root() / kind,
                *(root / kind for root in fallback_app_data_dirs(component)),
            ]
        )
    except Exception:
        candidates.append(suite_root() / kind)
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def log_directory(kind: str, component: str | None = None) -> Path:
    owner = component or _component
    cache_key = (owner, kind)
    cached = _log_directory_cache.get(cache_key)
    if cached is not None:
        return cached
    candidates = _log_directory_candidates(kind, owner)
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _log_directory_cache[cache_key] = directory
            return directory
        except OSError:
            continue
    return candidates[0] if candidates else suite_root() / kind


def configure_system_logging(component: str, debug_enabled: bool = False) -> None:
    global _component, _debug_enabled
    _component = str(component).strip() or "MekiCopy"
    _debug_enabled = bool(debug_enabled)
    os.environ["MEKICOPY_DEBUG_LOG"] = "1" if _debug_enabled else "0"
    _enable_fault_diagnostics()


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


def _rotate_log(path: Path, incoming_size: int) -> None:
    try:
        if not path.is_file() or path.stat().st_size + incoming_size <= _MAX_LOG_BYTES:
            return
        oldest = path.with_name(f"{path.name}.{_LOG_BACKUPS}")
        oldest.unlink(missing_ok=True)
        for index in range(_LOG_BACKUPS - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                os.replace(source, path.with_name(f"{path.name}.{index + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        # Logging must never be able to terminate the application.
        pass


def _append(kind: str, component: str, text: str) -> None:
    filename = f"{_safe_component_name(component).lower()}.log"
    payload = (text.rstrip() + "\n").encode("utf-8", errors="replace")
    with _write_lock:
        for directory in _log_directory_candidates(kind, component):
            path = directory / filename
            try:
                directory.mkdir(parents=True, exist_ok=True)
                try:
                    from runtime_paths import exclusive_file_lock

                    with exclusive_file_lock(
                        directory / f".{filename}.lock",
                        timeout=0.5,
                    ):
                        _rotate_log(path, len(payload))
                        with path.open("ab", buffering=0) as handle:
                            handle.write(payload)
                except TimeoutError:
                    # Do not split logs across roots merely because another
                    # process is rotating the same file; append is still safe.
                    with path.open("ab", buffering=0) as handle:
                        handle.write(payload)
                _log_directory_cache[(component, kind)] = directory
                return
            except (OSError, RuntimeError):
                if _log_directory_cache.get((component, kind)) == directory:
                    _log_directory_cache.pop((component, kind), None)
                continue


def _enable_fault_diagnostics() -> None:
    """Persist Python/native fault traces that would otherwise look like a silent exit."""
    global _fault_handle
    if _fault_handle is not None:
        try:
            faulthandler.disable()
            _fault_handle.close()
        except (OSError, RuntimeError):
            pass
        _fault_handle = None
    filename = f"{_safe_component_name(_component).lower()}-native-{os.getpid()}.log"
    for directory in _log_directory_candidates("error_log", _component):
        handle: TextIO | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            native_pattern = f"{_safe_component_name(_component).lower()}-native-*.log*"
            try:
                native_logs = sorted(
                    directory.glob(native_pattern),
                    key=lambda item: item.stat().st_mtime_ns,
                    reverse=True,
                )
            except OSError:
                native_logs = []
            for index, old_log in enumerate(native_logs):
                try:
                    if old_log.stat().st_size == 0 or index >= _NATIVE_LOG_RETENTION:
                        old_log.unlink(missing_ok=True)
                except OSError:
                    pass
            path = directory / filename
            _rotate_log(path, 0)
            handle = path.open("a", encoding="utf-8", buffering=1)
            faulthandler.enable(file=handle, all_threads=True)
            _fault_handle = handle
            _log_directory_cache[(_component, "error_log")] = directory
            return
        except (OSError, RuntimeError):
            if _log_directory_cache.get((_component, "error_log")) == directory:
                _log_directory_cache.pop((_component, "error_log"), None)
            if handle is not None:
                try:
                    handle.close()
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
