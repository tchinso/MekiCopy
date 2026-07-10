from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DATA_DIR_ENV = "MEKICOPY_DATA_DIR"
_DATA_DIR_CACHE: dict[str, Path] = {}
_TK_RUNTIME_SYNC_LOCK = threading.RLock()


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


def state_data_dir(app_name: str = "MekiCopy") -> Path:
    """Return the durable directory used for user-editable application state.

    Source runs keep their checked-out settings beside the source files.  Frozen
    releases instead use a writable per-user location, so installing below
    Program Files or another read-only directory cannot prevent clean shutdown
    or saving settings.  ``MEKICOPY_DATA_DIR`` intentionally opts into the same
    durable-location behavior for development and test runs.
    """
    if getattr(sys, "frozen", False) or os.environ.get(DATA_DIR_ENV):
        return writable_app_data_dir(app_name)
    return app_root()


def log_dir(app_name: str, kind: str) -> Path:
    directory = writable_app_data_dir(app_name) / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@contextmanager
def exclusive_file_lock(path: str | Path, timeout: float = 30.0) -> Iterator[None]:
    """Acquire a small cross-process lock file.

    Tcl/Tk's script folders are copied lazily when the executable lives below a
    non-ASCII path.  Several companion executables can start simultaneously,
    so a process-local lock alone is insufficient.  The lock is released by
    the operating system if a process exits unexpectedly; the lock file itself
    is deliberately retained and harmless.
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, timeout)

    with lock_path.open("a+b") as handle:
        # msvcrt.locking locks the byte at the current file position.  Ensure
        # that byte exists before trying to lock it.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)

        locked = False
        while not locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for lock: {lock_path}") from None
                time.sleep(0.05)

        try:
            yield
        finally:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                # The operating system will release a process-owned lock on
                # close even if an explicit unlock fails.
                pass


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


def sync_tk_runtime(
    source_tcl: str | Path,
    source_tk: str | Path,
    target_root: str | Path,
) -> tuple[Path, Path]:
    source_tcl_path = Path(source_tcl)
    source_tk_path = Path(source_tk)
    target_root_path = Path(target_root)
    target_tcl = target_root_path / "tcl8.6"
    target_tk = target_root_path / "tk8.6"
    marker = target_root_path / ".runtime_signature"

    digest = hashlib.sha256()
    for script in (source_tcl_path / "init.tcl", source_tk_path / "tk.tcl"):
        digest.update(script.read_bytes())
    expected_signature = digest.hexdigest()

    # Copying both script trees must be a single transaction from every
    # companion process's perspective.  Without this lock, two frozen
    # executables can interleave ``copytree`` calls and leave Tcl seeing a
    # half-populated directory on startup.
    target_root_path.mkdir(parents=True, exist_ok=True)
    lock_path = target_root_path / ".runtime_sync.lock"
    with _TK_RUNTIME_SYNC_LOCK, exclusive_file_lock(lock_path):
        current_signature = ""
        try:
            current_signature = marker.read_text(encoding="ascii").strip()
        except OSError:
            pass

        runtime_is_current = (
            current_signature == expected_signature
            and (target_tcl / "init.tcl").is_file()
            and (target_tk / "tk.tcl").is_file()
        )
        if not runtime_is_current:
            shutil.copytree(source_tcl_path, target_tcl, dirs_exist_ok=True)
            shutil.copytree(source_tk_path, target_tk, dirs_exist_ok=True)
            temporary_marker = marker.with_name(
                f"{marker.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                temporary_marker.write_text(expected_signature, encoding="ascii")
                os.replace(temporary_marker, marker)
            finally:
                try:
                    temporary_marker.unlink(missing_ok=True)
                except OSError:
                    pass

    return target_tcl, target_tk


def prepare_tk_environment(runtime_dirname: str = "MekiCopyRuntime") -> bool:
    """Set ASCII-safe Tcl/Tk paths before tkinter imports _tkinter."""
    if os.name != "nt":
        return True

    resource_root = Path(getattr(sys, "_MEIPASS", app_root()))
    application_root = app_root()
    base_root = Path(sys.base_prefix)
    if getattr(sys, "frozen", False):
        tcl_candidates = [
            resource_root / "_tcl_data",
            resource_root / "tcl" / "tcl8.6",
            resource_root / runtime_dirname / "tcl8.6",
            application_root / runtime_dirname / "tcl8.6",
            base_root / "tcl" / "tcl8.6",
        ]
        tk_candidates = [
            resource_root / "_tk_data",
            resource_root / "tcl" / "tk8.6",
            resource_root / runtime_dirname / "tk8.6",
            application_root / runtime_dirname / "tk8.6",
            base_root / "tcl" / "tk8.6",
        ]
    else:
        # Source runs must use the Tcl scripts matching the active Python DLL.
        # A checked-in runtime may have been produced by a different Python/Tcl.
        tcl_candidates = [
            base_root / "tcl" / "tcl8.6",
            application_root / runtime_dirname / "tcl8.6",
        ]
        tk_candidates = [
            base_root / "tcl" / "tk8.6",
            application_root / runtime_dirname / "tk8.6",
        ]
    source_tcl = next((path for path in tcl_candidates if (path / "init.tcl").is_file()), None)
    source_tk = next((path for path in tk_candidates if (path / "tk.tcl").is_file()), None)
    if source_tcl is None or source_tk is None:
        return False

    def activate(tcl_path: str | Path, tk_path: str | Path) -> bool:
        tcl_env = path_for_tcl(tcl_path)
        tk_env = path_for_tcl(tk_path)
        if not is_ascii_path(tcl_env) or not is_ascii_path(tk_env):
            return False
        if not (Path(tcl_env) / "init.tcl").is_file() or not (Path(tk_env) / "tk.tcl").is_file():
            return False
        os.environ["TCL_LIBRARY"] = tcl_env.replace("\\", "/")
        os.environ["TK_LIBRARY"] = tk_env.replace("\\", "/")
        return True

    if activate(source_tcl, source_tk):
        return True

    for safe_root in tk_runtime_roots(runtime_dirname):
        safe_root_text = path_for_tcl(safe_root)
        if not is_ascii_path(safe_root_text):
            continue
        try:
            safe_tcl, safe_tk = sync_tk_runtime(source_tcl, source_tk, safe_root_text)
        except OSError:
            continue
        if activate(safe_tcl, safe_tk):
            return True
    return False
