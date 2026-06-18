from __future__ import annotations

import sys
from pathlib import Path

from runtime_paths import writable_app_data_dir


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
    try:
        external.mkdir(parents=True, exist_ok=True)
        return external
    except OSError:
        pass

    return resource_root() / "models"


def app_data_dir() -> Path:
    return writable_app_data_dir("HYTrans")


def chrome_profile_dir() -> Path:
    return app_data_dir() / "ChromeProfile"


def log_dir(kind: str) -> Path:
    directory = app_data_dir() / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory
