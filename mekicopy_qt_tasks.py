from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import TypeVar

from PySide6.QtCore import QObject, Qt, Signal, Slot


T = TypeVar("T")


class _Completion(QObject):
    finished = Signal(str, object, object)


class QtTaskRunner(QObject):
    """Run bounded, uniquely keyed jobs and deliver results on the GUI thread."""

    MAX_WORKERS = 4
    MAX_WAITING = 8

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._closed = False
        self._callbacks: dict[str, tuple[Callable, Callable]] = {}
        self._waiting: deque[tuple[str, Callable]] = deque()
        # Workers retain this sender until they exit. Its lifetime is
        # independent of the receiving window, so late results can be dropped.
        self._completion = _Completion()
        self._completion.finished.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def submit(
        self,
        key: str,
        operation: Callable[[], T],
        *,
        on_success: Callable[[T], None],
        on_error: Callable[[Exception], None],
    ) -> bool:
        if (self._closed or key in self._callbacks
                or len(self._callbacks) >= self.MAX_WORKERS + self.MAX_WAITING):
            return False
        self._callbacks[key] = on_success, on_error
        if len(self._callbacks) > self.MAX_WORKERS:
            self._waiting.append((key, operation))
            return True
        try:
            self._start(key, operation)
        except Exception:
            self._callbacks.pop(key)
            raise
        return True

    def _start(self, key: str, operation: Callable) -> None:
        completion = self._completion

        def run() -> None:
            try:
                result = operation()
            except Exception as exc:
                completion.finished.emit(key, None, exc)
            else:
                completion.finished.emit(key, result, None)

        threading.Thread(target=run, name=f"MekiCopyTask-{key}", daemon=True).start()

    def is_running(self, key: str) -> bool:
        return key in self._callbacks

    def close(self) -> None:
        self._closed = True
        self._waiting.clear()
        self._callbacks.clear()

    @Slot(str, object, object)
    def _deliver(self, key: str, value: object, error: Exception | None) -> None:
        callbacks = self._callbacks.pop(key, None)
        if callbacks is None:
            return
        if self._waiting:
            next_key, operation = self._waiting.popleft()
            try:
                self._start(next_key, operation)
            except Exception as exc:
                self._completion.finished.emit(next_key, None, exc)
        on_success, on_error = callbacks
        if error is None:
            on_success(value)
        else:
            on_error(error)
