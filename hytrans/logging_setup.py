from __future__ import annotations

import datetime as _dt
import traceback

from .paths import log_dir

_debug_enabled = False


def configure_logging(debug_enabled: bool) -> None:
    global _debug_enabled
    _debug_enabled = debug_enabled
    debug("logging", f"debug logging enabled: {debug_enabled}")


def _timestamp() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append(kind: str, filename: str, text: str) -> None:
    try:
        path = log_dir(kind) / filename
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")
    except OSError:
        pass


def error(stage: str, exc: BaseException | str) -> None:
    lines = [
        "",
        "=== ERROR ===",
        f"time: {_timestamp()}",
        f"stage: {stage}",
    ]
    if isinstance(exc, BaseException):
        lines.append(f"type: {type(exc).__name__}")
        lines.append(f"message: {exc}")
        lines.append(traceback.format_exc())
    else:
        lines.append(f"message: {exc}")
    _append("error_log", "hytrans_error.log", "\n".join(lines))


def debug(stage: str, message: str) -> None:
    if not _debug_enabled:
        return
    lines = [
        "",
        "=== DEBUG ===",
        f"time: {_timestamp()}",
        f"stage: {stage}",
        message,
    ]
    _append("debug_log", "hytrans_debug.log", "\n".join(lines))

