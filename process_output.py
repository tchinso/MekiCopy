"""Bounded subprocess output delivery with responsive cancellation."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterator
from typing import TextIO


def iter_process_lines(stream: TextIO, check_cancel: Callable[[], None]) -> Iterator[str]:
    """Read a pipe without blocking the owning worker's cancellation checks.

    The caller owns process termination and pipe closure. Output buffering is
    bounded so a noisy child cannot exhaust memory while its consumer is busy.
    """
    pending: queue.Queue[str | Exception | None] = queue.Queue(maxsize=64)
    stopped = threading.Event()

    def send(item: str | Exception | None) -> None:
        while not stopped.is_set():
            try:
                pending.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def read() -> None:
        try:
            for line in stream:
                if stopped.is_set():
                    break
                send(line)
        except Exception as exc:
            send(exc)
        finally:
            send(None)

    reader = threading.Thread(target=read, name="process-output", daemon=True)
    reader.start()
    try:
        while True:
            check_cancel()
            try:
                item = pending.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stopped.set()
        reader.join(timeout=0.2)
