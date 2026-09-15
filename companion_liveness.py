"""Small, dependency-free UI heartbeat shared by desktop companion apps.

Their HTTP servers run on background threads, so a successful ``/health``
request alone does not prove that the Tk event loop can still redraw or handle
input.  The parent process uses this heartbeat to distinguish a responsive
desktop app from one whose UI thread has stopped pumping messages.
"""

from __future__ import annotations

import threading
import time
from typing import Any


DEFAULT_MAX_UI_HEARTBEAT_AGE_SECONDS = 7.0


class UiHeartbeat:
    """Expose recent Tk event-loop activity safely to an HTTP worker thread."""

    def __init__(self, *, max_age_seconds: float = DEFAULT_MAX_UI_HEARTBEAT_AGE_SECONDS) -> None:
        self._max_age_seconds = max(1.0, float(max_age_seconds))
        self._last_tick = time.monotonic()
        self._lock = threading.Lock()

    def tick(self) -> None:
        with self._lock:
            self._last_tick = time.monotonic()

    def schedule(self, root: Any, *, interval_ms: int = 250) -> None:
        """Refresh on Tk's event loop until that loop is no longer alive."""

        delay = max(25, int(interval_ms))

        def pulse() -> None:
            self.tick()
            try:
                root.after(delay, pulse)
            except Exception:
                # Tk raises during teardown.  A stale heartbeat is precisely
                # what a still-running parent needs to see in that case.
                return

        try:
            root.after(0, pulse)
        except Exception:
            # ``schedule`` is normally called during startup, but callers can
            # also race a window teardown.  Health reporting must never turn a
            # harmless late registration into a second shutdown failure.
            return

    def health_payload(self) -> dict[str, Any]:
        with self._lock:
            age = max(0.0, time.monotonic() - self._last_tick)
        return {
            "uiResponsive": age <= self._max_age_seconds,
            "uiHeartbeatAgeSeconds": round(age, 3),
        }
