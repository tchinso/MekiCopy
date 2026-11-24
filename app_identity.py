"""Windows application identity and icons shared by companion apps."""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path


ICON_FILENAME = "MekiCopy.ico"


def resource_file(filename: str = ICON_FILENAME) -> Path | None:
    source_root = Path(__file__).resolve().parent
    roots = (
        [
            Path(sys.executable).resolve().parent,
            Path(getattr(sys, "_MEIPASS", source_root)),
            source_root,
        ]
        if getattr(sys, "frozen", False)
        else [source_root, Path(sys.executable).resolve().parent]
    )
    for root in roots:
        candidate = root / filename
        if candidate.is_file():
            return candidate
    return None


def set_windows_app_id(app_name: str) -> None:
    """Give each executable an explicit taskbar identity before windows exist."""
    if os.name != "nt":
        return
    try:
        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [wintypes.LPCWSTR]
        setter.restype = ctypes.c_long
        setter(f"MekiCopy.{app_name}")
    except Exception:
        pass


def apply_qt_icon(window) -> None:
    """Apply the bundled multi-size ICO to a window and its taskbar button."""
    from PySide6.QtGui import QIcon

    icon_path = resource_file()
    if icon_path is not None:
        window.setWindowIcon(QIcon(str(icon_path)))
