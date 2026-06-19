from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from pathlib import Path

DATA_DIR_ENV = "MEKICOPY_DATA_DIR"
_DATA_DIR_CACHE: dict[str, Path] = {}


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def is_ascii_path(path: str | Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def windows_short_path(path: str | Path) -> str | None:
    if os.name != "nt":
        return None
    try:
        source = str(Path(path).resolve())
    except OSError:
        source = str(path)

    get_short = ctypes.windll.kernel32.GetShortPathNameW
    required = get_short(source, None, 0)
    if required <= 0:
        return None
    buffer = ctypes.create_unicode_buffer(required)
    written = get_short(source, buffer, required)
    if written <= 0:
        return None
    short = buffer.value
    if short and is_ascii_path(short):
        return short
    return None


def path_for_tcl(path: str | Path) -> str:
    if is_ascii_path(path):
        return str(path)
    short = windows_short_path(path)
    if short:
        return short
    return str(path)


def _program_data_root() -> Path | None:
    if os.name != "nt":
        return None
    base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return Path(base) / "MekiCopy"


def _local_app_data_root() -> Path | None:
    if os.name != "nt":
        return None
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / "MekiCopy"


def _can_write_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def writable_app_data_dir(app_name: str) -> Path:
    cached = _DATA_DIR_CACHE.get(app_name)
    if cached:
        return cached

    candidates: list[Path] = []
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        candidates.append(Path(override).expanduser() / app_name)

    local_app_data = _local_app_data_root()
    if local_app_data:
        candidates.append(local_app_data / app_name)

    program_data = _program_data_root()
    if program_data:
        candidates.append(program_data / app_name)

    candidates.append(app_root() / "runtime_data" / app_name)

    if os.name != "nt":
        candidates.append(Path.home() / ".local" / "share" / "MekiCopy" / app_name)

    candidates.append(Path(tempfile.gettempdir()) / "MekiCopy" / app_name)

    for candidate in _dedupe(candidates):
        if _can_write_directory(candidate):
            _DATA_DIR_CACHE[app_name] = candidate
            return candidate

    fallback = app_root()
    _DATA_DIR_CACHE[app_name] = fallback
    return fallback


def log_dir(app_name: str, kind: str) -> Path:
    directory = writable_app_data_dir(app_name) / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def tk_runtime_roots(runtime_dirname: str) -> list[Path]:
    candidates: list[Path] = []
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        candidates.append(Path(override).expanduser() / runtime_dirname)

    local_app_data = _local_app_data_root()
    if local_app_data:
        candidates.append(local_app_data / runtime_dirname)

    program_data = _program_data_root()
    if program_data:
        candidates.append(program_data / runtime_dirname)

    candidates.append(app_root() / runtime_dirname)
    candidates.append(Path(tempfile.gettempdir()) / "MekiCopy" / runtime_dirname)
    return _dedupe(candidates)
