from __future__ import annotations

import os
import sys
import threading
import uuid
from pathlib import Path

from runtime_paths import fallback_app_data_dirs, writable_app_data_dir

_models_dir_cache: Path | None = None
_models_dir_lock = threading.RLock()

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
    probe = path / (
        f".write-test-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}"
    )
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="ascii")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def models_dir() -> Path:
    """Return HYTrans' durable, executable-adjacent model directory.

    A frozen HYTrans therefore always uses ``HYTrans/models`` when that
    directory is writable.  LocalAppData is only a last-resort fallback for
    read-only installations (for example, Program Files).  Most importantly,
    an existing LocalAppData cache must never make a writable executable-
    adjacent directory lose priority.
    """
    global _models_dir_cache
    with _models_dir_lock:
        if _models_dir_cache is not None:
            return _models_dir_cache

        external = app_root() / "models"
        if _ensure_writable_directory(external):
            _models_dir_cache = external
            return external

        # app_data_dir() normally selects the executable root first. If only
        # its child ``models`` is unusable (for example, a regular file with
        # that name), do not accidentally select the same broken path again.
        fallback_roots = [app_data_dir(), *fallback_app_data_dirs("HYTrans")]
        last_candidate = external
        seen: set[str] = {os.path.normcase(str(external))}
        for root in fallback_roots:
            candidate = root / "models"
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            last_candidate = candidate
            if _ensure_writable_directory(candidate):
                _models_dir_cache = candidate
                return candidate

        _models_dir_cache = last_candidate
        return last_candidate


def app_data_dir() -> Path:
    return writable_app_data_dir("HYTrans")


def chrome_profile_dir() -> Path:
    return app_data_dir() / "ChromeProfile"


def log_dir(kind: str) -> Path:
    directory = app_data_dir() / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory
