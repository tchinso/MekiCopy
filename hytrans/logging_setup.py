from __future__ import annotations

from system_logging import (
    configure_system_logging,
    log_debug,
    log_error,
    set_debug_enabled,
)


def configure_logging(debug_enabled: bool) -> None:
    configure_system_logging("HYTrans", debug_enabled)
    set_debug_enabled(debug_enabled)
    debug("logging", f"debug logging enabled: {debug_enabled}")


def error(stage: str, exc: BaseException | str) -> None:
    log_error(stage, exc, component="HYTrans")


def debug(stage: str, message: str) -> None:
    log_debug(stage, message, component="HYTrans")
