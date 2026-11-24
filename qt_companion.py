"""Coalesce companion worker notifications onto the Qt event loop."""

from __future__ import annotations

from collections.abc import Callable
import threading

from PySide6.QtCore import QObject, Qt, Signal, Slot


class UiWakeup(QObject):
    """Schedule at most one GUI callback while producers fill their buffers."""

    _ready = Signal()

    def __init__(self, parent: QObject, drain: Callable[[], None]) -> None:
        super().__init__(parent)
        self._drain = drain
        self._lock = threading.Lock()
        self._scheduled = False
        self._ready.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)

    def schedule(self) -> None:
        with self._lock:
            if self._scheduled:
                return
            self._scheduled = True
        self._ready.emit()

    @Slot()
    def _dispatch(self) -> None:
        with self._lock:
            self._scheduled = False
        self._drain()
