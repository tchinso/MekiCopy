"""Small, Tk-safe background task bridge used by the desktop windows.

Tk widgets must only be accessed on the thread that owns the Tcl interpreter.
This module keeps slow work on daemon threads and delivers its result from the
window's ``after`` loop, so callers only need to supply normal UI callbacks.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


@dataclass
class _TaskCompletion(Generic[T]):
    key: str
    value: T | None = None
    error: Exception | None = None


class TkTaskRunner:
    """Run uniquely-keyed daemon tasks and return their results on Tk's thread."""

    # No worker touches Tk directly, so this bridge must poll from the Tk
    # thread.  Keep the check responsive while work is in flight, but do not
    # wake an otherwise idle visual-novel companion 25 times per second.
    POLL_INTERVAL_MS = 100
    IDLE_POLL_INTERVAL_MS = 500

    def __init__(
        self,
        root,
        *,
        poll_interval_ms: int = POLL_INTERVAL_MS,
        idle_poll_interval_ms: int = IDLE_POLL_INTERVAL_MS,
    ) -> None:
        self._root = root
        self._poll_interval_ms = max(10, int(poll_interval_ms))
        self._idle_poll_interval_ms = max(
            self._poll_interval_ms,
            int(idle_poll_interval_ms),
        )
        self._completions: queue.Queue[_TaskCompletion] = queue.Queue()
        self._running_keys: set[str] = set()
        self._callbacks: dict[
            str,
            tuple[Callable[[object], None], Callable[[Exception], None]],
        ] = {}
        self._closed = False
        self._poll_after_id: str | None = None
        self._schedule_poll(self._idle_poll_interval_ms)

    def submit(
        self,
        key: str,
        operation: Callable[[], T],
        *,
        on_success: Callable[[T], None],
        on_error: Callable[[Exception], None],
    ) -> bool:
        """Start a task unless one with the same key is already running."""
        if self._closed or key in self._running_keys:
            return False

        self._running_keys.add(key)
        self._callbacks[key] = (on_success, on_error)

        # ``submit`` is called from the Tk thread.  A previous idle callback
        # can therefore be safely replaced so a fast task is delivered without
        # waiting up to the idle interval.
        self._schedule_poll(0, replace=True)

        def run() -> None:
            try:
                completion: _TaskCompletion[T] = _TaskCompletion(
                    key=key,
                    value=operation(),
                )
            except Exception as exc:
                completion = _TaskCompletion(key=key, error=exc)
            self._completions.put(completion)

        thread = threading.Thread(
            target=run,
            name=f"MekiCopyTask-{key}",
            daemon=True,
        )
        thread.start()
        return True

    def close(self) -> None:
        """Stop delivering queued completions while the owning window closes."""
        if self._closed:
            return
        self._closed = True
        self._callbacks.clear()
        if self._poll_after_id:
            try:
                self._root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None

    def _schedule_poll(self, delay_ms: int, *, replace: bool = False) -> None:
        if self._closed:
            return
        if self._poll_after_id is not None:
            if not replace:
                return
            try:
                self._root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        try:
            self._poll_after_id = self._root.after(
                max(0, int(delay_ms)),
                self._drain,
            )
        except Exception:
            self._closed = True

    def _drain(self) -> None:
        self._poll_after_id = None
        try:
            while True:
                completion = self._completions.get_nowait()
                self._running_keys.discard(completion.key)
                if self._closed:
                    continue

                self._deliver(completion)
        except queue.Empty:
            pass
        finally:
            delay = (
                self._poll_interval_ms
                if self._running_keys or not self._completions.empty()
                else self._idle_poll_interval_ms
            )
            self._schedule_poll(delay)

    def _deliver(self, completion: _TaskCompletion) -> None:
        on_success, on_error = self._callbacks.pop(completion.key, (None, None))
        callback = on_error if completion.error is not None else on_success
        if callback is None:
            return
        try:
            if completion.error is not None:
                callback(completion.error)
            else:
                callback(completion.value)
        except Exception as exc:
            reporter = getattr(self._root, "report_callback_exception", None)
            if callable(reporter):
                reporter(type(exc), exc, exc.__traceback__)
