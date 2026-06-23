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


def _ensure_writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def models_dir() -> Path:
    """Return HYTrans' durable, executable-adjacent model directory.

    A frozen HYTrans therefore always uses ``HYTrans/models`` when that
    directory is writable.  LocalAppData is only a last-resort fallback for
    read-only installations (for example, Program Files).  Most importantly,
    an existing LocalAppData cache must never make a writable executable-
    adjacent directory lose priority.
    """
    external = app_root() / "models"
    if _ensure_writable_directory(external):
        return external

    stable = app_data_dir() / "models"
    if _ensure_writable_directory(stable):
        return stable

    return stable


def app_data_dir() -> Path:
    return writable_app_data_dir("HYTrans")


def chrome_profile_dir() -> Path:
    return app_data_dir() / "ChromeProfile"


def log_dir(kind: str) -> Path:
    directory = app_data_dir() / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory
