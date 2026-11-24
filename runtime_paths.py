from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

DATA_DIR_ENV = "MEKICOPY_DATA_DIR"
FORCE_DATA_DIR_ENV = "MEKICOPY_FORCE_DATA_DIR"
_DATA_DIR_CACHE: dict[tuple[str, str, str, str], Path] = {}
_DATA_SUBDIR_CACHE: dict[tuple[str, str, str, str, str], Path] = {}
_DATA_DIR_LOCK = threading.RLock()


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


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


def fallback_app_data_dirs(app_name: str) -> list[Path]:
    """Return secondary data locations in their normal fallback order."""
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
    return _dedupe(candidates)


def _app_data_candidates(app_name: str) -> list[Path]:
    override = os.environ.get(DATA_DIR_ENV, "")
    force_override = os.environ.get(FORCE_DATA_DIR_ENV, "").strip().lower()
    fallback_candidates = fallback_app_data_dirs(app_name)
    forced = force_override in {"1", "true", "yes", "on"} and bool(override)
    return _dedupe(
        fallback_candidates + [app_root()]
        if forced
        else [app_root(), *fallback_candidates]
    )


def writable_app_data_dir(app_name: str) -> Path:
    """Choose one stable writable data root for the lifetime of this process.

    Portable data beside the executable is the normal first choice.  The
    LocalAppData, ProgramData and temporary locations are consulted only when
    that directory is not writable.  Build/smoke-test harnesses can opt into
    an isolated forced override by setting both ``MEKICOPY_DATA_DIR`` and
    ``MEKICOPY_FORCE_DATA_DIR``.
    """
    override = os.environ.get(DATA_DIR_ENV, "")
    force_override = os.environ.get(FORCE_DATA_DIR_ENV, "").strip().lower()
    cache_key = (app_name, str(app_root()), override, force_override)
    with _DATA_DIR_LOCK:
        cached = _DATA_DIR_CACHE.get(cache_key)
        if cached:
            return cached

        for candidate in _app_data_candidates(app_name):
            if _can_write_directory(candidate):
                _DATA_DIR_CACHE[cache_key] = candidate
                return candidate

        fallback = app_root()
        _DATA_DIR_CACHE[cache_key] = fallback
        return fallback


def writable_app_subdir(app_name: str, *relative_parts: str) -> Path:
    """Choose a writable child directory while preserving portable priority.

    Testing only the executable directory is insufficient when the desired
    child name is occupied by a file or has a narrower ACL. Probe the actual
    child at each candidate and fall back only after that concrete write fails.
    """
    relative = Path(*relative_parts)
    if relative.anchor or relative.drive or ".." in relative.parts:
        raise ValueError(f"relative application path required: {relative}")

    override = os.environ.get(DATA_DIR_ENV, "")
    force_override = os.environ.get(FORCE_DATA_DIR_ENV, "").strip().lower()
    cache_key = (app_name, str(app_root()), override, force_override, str(relative))
    with _DATA_DIR_LOCK:
        cached = _DATA_SUBDIR_CACHE.get(cache_key)
        if cached:
            return cached
        candidates = [root / relative for root in _app_data_candidates(app_name)]
        for candidate in _dedupe(candidates):
            if _can_write_directory(candidate):
                _DATA_SUBDIR_CACHE[cache_key] = candidate
                return candidate

        fallback = app_root() / relative
        _DATA_SUBDIR_CACHE[cache_key] = fallback
        return fallback


def state_data_dir(app_name: str = "MekiCopy") -> Path:
    """Return the durable directory used for user-editable application state.

    Both source and frozen runs prefer their program directory.  Protected
    installations (for example Program Files) transparently fall back to a
    writable per-user or machine location.
    """
    return writable_app_data_dir(app_name)


def log_dir(app_name: str, kind: str) -> Path:
    return writable_app_subdir(app_name, kind)


@contextmanager
def exclusive_file_lock(
    path: str | Path, timeout: float = 30.0, *,
    check_cancel: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Acquire a small cross-process lock file.

    The operating system releases the lock if a process exits unexpectedly.
    The lock file can be reused by future processes.
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
            if check_cancel is not None:
                check_cancel()
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
