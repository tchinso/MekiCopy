from __future__ import annotations

import sys
from pathlib import Path

from runtime_paths import writable_app_data_dir

HYTRANS_MODEL_ID = "onnx-community/HY-MT1.5-1.8B-ONNX"


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


def _model_root_has_hytrans_files(root: Path) -> bool:
    model_root = root.joinpath(*HYTRANS_MODEL_ID.split("/"))
    if not model_root.exists():
        return False
    return any(path.is_file() for path in model_root.rglob("*"))


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
    external = app_root() / "models"
    stable = app_data_dir() / "models"

    if _model_root_has_hytrans_files(external):
        return external
    if _model_root_has_hytrans_files(stable):
        return stable
    if _ensure_writable_directory(stable):
        return stable
    if _ensure_writable_directory(external):
        return external

    return stable


def app_data_dir() -> Path:
    return writable_app_data_dir("HYTrans")


def chrome_profile_dir() -> Path:
    return app_data_dir() / "ChromeProfile"


def log_dir(kind: str) -> Path:
    directory = app_data_dir() / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory
