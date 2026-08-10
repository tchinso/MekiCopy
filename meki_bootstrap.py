from __future__ import annotations

"""Small stdlib-first launcher that can report import-time native failures."""

import ctypes
import importlib
import sys
from pathlib import Path

from system_logging import (
    capture_windowed_streams,
    configure_system_logging,
    install_exception_hooks,
    log_directory,
    log_error,
)


_TARGETS = {
    "mekicopy": ("MekiCopy", "mekicopy"),
    "hytrans": ("HYTrans", "hytrans_main"),
    "mekiaudiocapture": ("MekiAudioCapture", "meki_audio_capture"),
    "mekioverlayer": ("MekiOverlayer", "meki_overlayer"),
    "mekiscript": ("MekiScript", "meki_script"),
}


def _show_fatal_error(component: str, exc: BaseException) -> None:
    message = (
        "프로그램을 시작하는 중 치명적인 오류가 발생했습니다.\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        f"로그: {log_directory('error_log', component)}"
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, message, component, 0x10)
    except Exception:
        pass


def main() -> int:
    executable_name = Path(sys.executable).stem.casefold()
    target = _TARGETS.get(executable_name)
    if target is None:
        raise RuntimeError(f"알 수 없는 MekiCopy 실행 파일입니다: {executable_name}")
    component, module_name = target
    configure_system_logging(component, "--debug-log" in sys.argv[1:])
    install_exception_hooks()
    capture_windowed_streams()
    try:
        module = importlib.import_module(module_name)
        result = module.main()
        return int(result) if result is not None else 0
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    except BaseException as exc:
        log_error("bootstrap", exc, component=component)
        if not any(argument.startswith("--self-test") for argument in sys.argv[1:]):
            _show_fatal_error(component, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
