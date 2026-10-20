"""The pinned hdTrans C API, owned by one bounded, sequential worker.

All DLL entry points (including diagnostics and lifecycle calls) run on the
same thread. HTTP cancellation abandons a result; it never unloads a running
DLL or claims that a native call was cancelled. There is one process session,
and changing the caller's work boundary resets it rather than inventing
independent sessions.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import ctypes
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import platform
import queue
import struct
import threading
import uuid
from typing import Callable, Any

from .hdtrans_settings import HdTransSettings, load_hdtrans_settings, normalize_settings, save_hdtrans_settings
from .queue import TranslationQueueOverloadedError

_NATIVE_LIBRARY_LOCK = threading.Lock()


class HdTransError(RuntimeError):
    pass


def validate_dll_architecture(path: Path) -> None:
    """Reject a mismatched PE before Windows produces an opaque loader error."""
    with path.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise HdTransError("hdTransEngine.dll is not a valid Windows PE file")
        handle.seek(0x3C)
        offset = struct.unpack("<I", handle.read(4))[0]
        handle.seek(offset)
        if handle.read(4) != b"PE\0\0":
            raise HdTransError("hdTransEngine.dll has an invalid PE header")
        machine = struct.unpack("<H", handle.read(2))[0]
    bits = ctypes.sizeof(ctypes.c_void_p) * 8
    expected = 0x14C if bits == 32 else (0xAA64 if platform.machine().lower() in {"arm64", "aarch64"} else 0x8664)
    names = {0x14C: "x86 (32-bit)", 0x8664: "x64 (64-bit)", 0xAA64: "ARM64"}
    if machine != expected:
        raise HdTransError(f"hdTrans architecture mismatch: Python/EXE={names.get(expected)}, DLL={names.get(machine, hex(machine))}")


class NativeLibrary:
    """Cdecl, UTF-8 and original-pointer ownership from v0.2.1 hdtrans.h."""
    def __init__(self, path: Path, *, loader: Callable[..., Any] | None = None) -> None:
        validate_dll_architecture(path)
        if not _NATIVE_LIBRARY_LOCK.acquire(blocking=False):
            raise HdTransError("Only one hdTrans engine/session may be loaded in this process")
        self._owns_library = True
        self.initialized = False
        try:
            self.dll = (loader or ctypes.CDLL)(str(path))
        except BaseException:
            self._owns_library = False
            _NATIVE_LIBRARY_LOCK.release()
            raise
        signatures = {
            "hdTrans_Initialize": ([ctypes.c_char_p], ctypes.c_int),
            "hdTrans_Terminate": ([], None),
            "hdTrans_Translate": ([ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_size_t)], ctypes.c_int),
            "hdTrans_Reset": ([], ctypes.c_int),
            "hdTrans_Reload": ([], ctypes.c_int),
            "hdTrans_UserDictError": ([ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_size_t)], ctypes.c_int),
            "hdTrans_FreeMem": ([ctypes.c_void_p], None),
            "hdTrans_Version": ([], ctypes.c_char_p),
        }
        try:
            for name, (arguments, result) in signatures.items():
                function = getattr(self.dll, name)
                function.argtypes, function.restype = arguments, result
        except AttributeError as exc:
            self.close()
            raise HdTransError("hdTrans DLL does not provide the required v0.2.1 C API") from exc

    def initialize(self, dictionary: Path) -> None:
        # The native list is NUL separated with a double-NUL terminator.
        dirs = str(dictionary.resolve()).encode("utf-8") + b"\0\0"
        if self.dll.hdTrans_Initialize(dirs) != 0:
            raise HdTransError("hdTrans engine/base dictionary initialization failed; verify the compatible DLL and .hdsnap pair")
        self.initialized = True

    def _buffer(self, function: Any, *args: Any) -> str:
        pointer = ctypes.c_void_p()
        length = ctypes.c_size_t()
        try:
            result = function(*args, ctypes.byref(pointer), ctypes.byref(length))
            if result != 0:
                raise HdTransError("hdTrans C API call failed")
            if length.value and not pointer.value:
                raise HdTransError("hdTrans returned an invalid output buffer")
            return ctypes.string_at(pointer, length.value).decode("utf-8") if pointer.value else ""
        finally:
            if pointer.value:
                self.dll.hdTrans_FreeMem(pointer)

    def translate(self, text: str) -> str:
        encoded = text.encode("utf-8")
        return self._buffer(self.dll.hdTrans_Translate, encoded, len(encoded))

    def diagnostics(self) -> str:
        return self._buffer(self.dll.hdTrans_UserDictError)

    def reset(self) -> None:
        if self.dll.hdTrans_Reset() != 0:
            raise HdTransError("hdTrans context reset failed")

    def reload(self) -> None:
        if self.dll.hdTrans_Reload() != 0:
            raise HdTransError("hdTrans dictionary reload failed; the previous engine/dictionary remains active")

    def version(self) -> str:
        return self.dll.hdTrans_Version().decode("utf-8")

    def close(self) -> None:
        dll = getattr(self, "dll", None)
        if dll is None:
            return
        if getattr(self, "initialized", False):
            dll.hdTrans_Terminate()
            self.initialized = False
        # ctypes does not automatically close CDLL handles on Windows. All
        # calls, and Terminate's background reload join, have completed here.
        if os.name == "nt" and isinstance(dll, ctypes.CDLL):
            import _ctypes
            _ctypes.FreeLibrary(dll._handle)
            dll._handle = 0
        self.dll = None
        if getattr(self, "_owns_library", False):
            self._owns_library = False
            _NATIVE_LIBRARY_LOCK.release()


@dataclass
class NativeJob:
    operation: str
    arguments: tuple[Any, ...]
    future: Future = field(default_factory=Future)
    abandoned: threading.Event = field(default_factory=threading.Event)
    started: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)


class HdTransBackend:
    def __init__(self, *, max_queued_jobs: int = 8,
                 settings: HdTransSettings | None = None,
                 installer: Callable[..., Path] | None = None,
                 library_factory: Callable[[Path], NativeLibrary] = NativeLibrary) -> None:
        if max_queued_jobs < 1:
            raise ValueError("max_queued_jobs must be at least 1")
        self.queue: queue.Queue[NativeJob | None] = queue.Queue(maxsize=max_queued_jobs)
        self.settings = normalize_settings(settings or load_hdtrans_settings())
        self._installer = installer
        self._library_factory = library_factory
        self._native: NativeLibrary | None = None
        self._install: Path | None = None
        self._owner: str | None = None
        self._reset_needed = False
        self._timed_out_job: NativeJob | None = None
        self._closing = False
        self._lock = threading.RLock()
        self.cancel_install = threading.Event()
        self._status: dict[str, Any] = {"state": "STARTING", "phase": "설치 확인 대기", "ready": False,
                                        "complete": False, "error": None, "userDictionaryError": ""}
        self._thread = threading.Thread(target=self._run, name="HYTrans-hdTrans", daemon=True)
        self._thread.start()

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = {**self._status, "backend": "hdtrans", "workerConnected": False,
                    "settings": self.settings.as_payload(), "queued": self.queue.qsize(),
                    "contextOwner": self._owner, "singleSession": True, "reloadResetsContext": True}
            if self._timed_out_job is not None and not self._timed_out_job.finished.is_set():
                snapshot.update(ready=False, complete=False, state="NATIVE_TIMEOUT", nativeCallPending=True,
                                error="hdTrans native call timed out; waiting safely for completion. Restart HYTrans if it does not recover.")
            return snapshot

    def _update(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)

    def _progress(self, values: dict[str, Any]) -> None:
        self._update(**{**values, "ready": False, "complete": False})

    async def submit(self, operation: str, *arguments: Any, timeout: float | None = 120) -> Any:
        if self._closing:
            raise HdTransError("hdTrans is stopping")
        job = NativeJob(operation, arguments)
        try:
            self.queue.put_nowait(job)
        except queue.Full as exc:
            raise TranslationQueueOverloadedError("hdTrans native queue is busy; retry shortly") from exc
        waiter = asyncio.wrap_future(job.future)
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
            # An executing function continues to own all native resources.
            job.abandoned.set()
            if job.operation == "translate" and job.started.is_set():
                self._reset_needed = True
                if isinstance(exc, asyncio.TimeoutError) and not job.finished.is_set():
                    self._timed_out_job = job
            # Consume any late exception without changing the job's lifetime.
            waiter.add_done_callback(lambda future: None if future.cancelled() else future.exception())
            raise

    async def close(self) -> None:
        self._closing = True
        self.cancel_install.set()
        # Drain pending requests; the active call finishes before Terminate.
        while True:
            try:
                job = self.queue.get_nowait()
            except queue.Empty:
                break
            if job is not None:
                job.abandoned.set()
                if not job.future.done():
                    job.future.set_exception(HdTransError("hdTrans is stopping"))
            self.queue.task_done()
        self.queue.put_nowait(None)
        await asyncio.to_thread(self._thread.join)

    def _run(self) -> None:
        try:
            while True:
                job = self.queue.get()
                try:
                    if job is None:
                        return
                    job.started.set()
                    if job.abandoned.is_set():
                        job.future.cancel()
                        continue
                    try:
                        result = self._execute(job.operation, *job.arguments)
                        if job.operation == "translate" and job.abandoned.is_set():
                            self._reset_needed = True
                        if not job.future.done():
                            job.future.set_result(result)
                    except BaseException as exc:
                        if job.operation == "translate":
                            self._reset_needed = True
                        if job.operation == "initialize":
                            self._update(state="CANCELLED" if self.cancel_install.is_set() else "ERROR",
                                         phase=str(exc), ready=False, complete=False, error=str(exc))
                        if not job.future.done():
                            job.future.set_exception(exc)
                finally:
                    if job is not None:
                        job.finished.set()
                    self.queue.task_done()
        finally:
            if self._native is not None:
                self._native.close()
                self._native = None
            self._update(state="STOPPED", ready=False, complete=False)

    def _write_config(self, settings: HdTransSettings) -> None:
        assert self._install is not None
        user = self._install / "hdTrans" / "user"
        user.mkdir(parents=True, exist_ok=True)
        # A second, empty official TSV makes per-file reload diagnostics
        # observable even when the sole selected user file is malformed.
        sentinel = user / "userdict.mekicopy-empty.tsv"
        sentinel.write_text("# Managed empty hdTrans user dictionary.\n", encoding="utf-8")
        paths = [*settings.user_dictionaries, str(sentinel.resolve())]
        text = "# Managed by MekiCopy; user files remain outside this version.\nuserdict:\n"
        text += "".join("  - " + json.dumps(path.replace("\\", "/"), ensure_ascii=False) + "\n" for path in paths)
        text += "ehnd: false\n"
        destination = user / "hdtrans.config.yaml"
        temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _execute(self, operation: str, *arguments: Any) -> Any:
        if operation == "initialize":
            if self._native is not None:
                return self.status()
            self._update(state="LOADING", phase="hdTrans 설치 확인", ready=False, complete=False, error=None)
            installer = self._installer
            if installer is None:
                from .hdtrans_download import ensure_installation
                installer = ensure_installation
            self._install = Path(installer(cancel_event=self.cancel_install, progress=self._progress))
            if self.cancel_install.is_set():
                raise HdTransError("hdTrans 설치가 취소되었습니다.")
            self._write_config(self.settings)
            self._update(state="LOADING", phase="엔진 및 기본 사전 초기화")
            native = self._library_factory(self._install / "hdTransEngine.dll")
            try:
                native.initialize(self._install / "hdTrans" / "dicts.hdsnap")
                self._native = native
                dictionary_version_path = self._install / "hdTrans" / "dicts.txt"
                dictionary_version = dictionary_version_path.read_text(encoding="utf-8").strip() if dictionary_version_path.is_file() else None
                self._update(version=native.version(), dictionaryVersion=dictionary_version,
                             installationPath=str(self._install), userDictionaryError=native.diagnostics(),
                             state="READY", phase="준비 완료", ready=True, complete=True, error=None)
            except BaseException:
                native.close()
                self._native = None
                raise
            return self.status()
        native = self._native
        if operation == "settings" and native is None:
            settings = normalize_settings(arguments[0])
            if not save_hdtrans_settings(settings):
                raise HdTransError("hdTrans 설정을 저장하지 못했습니다.")
            self.settings = settings
            return self.status()
        if native is None:
            raise HdTransError("hdTrans engine is not ready")
        if operation == "translate":
            text, owner, reset = arguments
            if reset or self._reset_needed or not self.settings.preserve_context or owner != self._owner:
                native.reset()
                self._reset_needed = False
            self._owner = owner
            self._update(state="BUSY")
            try:
                result = native.translate(text).strip()
                if not result:
                    raise HdTransError("hdTrans returned an empty translation")
                self._update(userDictionaryError=native.diagnostics(), error=None)
                return result
            finally:
                self._update(state="READY")
        if operation == "reset":
            native.reset()
            self._owner = None
            self._reset_needed = False
        elif operation == "reload":
            self._write_config(self.settings)
            native.reload()
            self._owner = None
            self._update(userDictionaryError=native.diagnostics(), error=None)
        elif operation == "settings":
            settings = normalize_settings(arguments[0])
            old = self.settings
            changed_files = settings.user_dictionaries != old.user_dictionaries
            try:
                if changed_files:
                    self._write_config(settings)
                    native.reload()
                elif settings.preserve_context != old.preserve_context:
                    native.reset()
                if not save_hdtrans_settings(settings):
                    raise HdTransError("hdTrans 설정을 저장하지 못했습니다.")
            except BaseException:
                if changed_files:
                    self._write_config(old)
                    native.reload()
                raise
            self.settings = settings
            self._owner = None
            self._update(userDictionaryError=native.diagnostics(), error=None)
        else:
            raise HdTransError(f"Unknown hdTrans operation: {operation}")
        return self.status()
