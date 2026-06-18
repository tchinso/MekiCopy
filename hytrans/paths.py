from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root())).resolve()
    return app_root()


def assets_dir() -> Path:
    external = app_root() / "assets"
    if external.exists():
        return external
    return resource_root() / "assets"


def models_dir() -> Path:
    external = app_root() / "models"
    if external.exists():
        return external
    return resource_root() / "models"


def local_appdata_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "HYTrans"
    return Path.home() / "AppData" / "Local" / "HYTrans"


def chrome_profile_dir() -> Path:
    return local_appdata_dir() / "ChromeProfile"


def log_dir(kind: str) -> Path:
    directory = local_appdata_dir() / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory
