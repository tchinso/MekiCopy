"""Bounded display receipts shared by the HTTP threads and the Tk consumer."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import threading
import time


@dataclass
class DisplayReceipt:
    event: threading.Event = field(default_factory=threading.Event)
    displayed: bool = False
    error: str = ""
    completed_at: float | None = None


class DisplayReceipts:
    def __init__(self, limit: int = 128) -> None:
        self.lock = threading.RLock()
        self.items: OrderedDict[str, DisplayReceipt] = OrderedDict()
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("receipt limit must be a positive integer")
        self.limit = limit

    def create(self, request_id: str) -> DisplayReceipt:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 160:
            raise ValueError("requestId must contain 1 to 160 characters")
        with self.lock:
            if request_id in self.items:
                raise ValueError("requestId has already been used")
            receipt = DisplayReceipt()
            self.items[request_id] = receipt
            while len(self.items) > self.limit:
                _, old = self.items.popitem(last=False)
                if not old.event.is_set():
                    old.error = "display receipt expired"
                    old.event.set()
            return receipt

    def complete(self, request_id: str | None, *, error: str = "") -> None:
        if request_id is None:
            return
        with self.lock:
            receipt = self.items.get(request_id)
            if receipt is None or receipt.event.is_set():
                return
            receipt.error = error
            receipt.displayed = not error
            receipt.completed_at = time.monotonic() if not error else None
            receipt.event.set()

    def cancelled(self, request_id: str | None) -> bool:
        if request_id is None:
            return False
        with self.lock:
            receipt = self.items.get(request_id)
            return receipt is None or (receipt.event.is_set() and not receipt.displayed)

    def cancel(self, request_id: str) -> None:
        # Keep a tombstone so a cancelled request arriving late cannot display.
        with self.lock:
            if request_id not in self.items:
                self.create(request_id)
            self.complete(request_id, error="display cancelled")
