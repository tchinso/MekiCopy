from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .logging_setup import debug, error


@dataclass
class TranslationJob:
    id: str
    text: str
    future: asyncio.Future[str]
    created_at: float
    abandoned: bool = False
    worker_ws: Any = None


class TranslationQueue:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[TranslationJob] = asyncio.Queue()
        self.pending: dict[str, TranslationJob] = {}
        self.worker_ws: Any = None
        self._worker_usable = False
        self._worker_invalidated_callback: Callable[[str], None] | None = None
        self._last_worker_invalidation_reason: str | None = None
        self.running = False

    def set_worker(self, websocket: Any) -> None:
        if self.worker_ws is websocket:
            return
        if self.worker_ws is not None:
            self._fail_pending("worker replaced")
        self.worker_ws = websocket
        self._worker_usable = True

    @property
    def worker_available(self) -> bool:
        """Return whether new work may be dispatched to the active worker."""
        return self.worker_ws is not None and self._worker_usable

    def set_worker_invalidated_callback(
        self,
        callback: Callable[[str], None] | None,
    ) -> None:
        """Register a lightweight state hook for timeout-driven invalidation."""
        self._worker_invalidated_callback = callback

    def take_worker_invalidation_reason(self) -> str | None:
        """Return and clear the latest timeout-driven worker failure reason."""
        reason = self._last_worker_invalidation_reason
        self._last_worker_invalidation_reason = None
        return reason

    def is_current_worker(self, websocket: Any) -> bool:
        """Return whether a websocket still owns the active worker slot."""
        return self.worker_ws is websocket

    def clear_worker(
        self,
        websocket: Any | None = None,
        *,
        reason: str = "worker disconnected",
    ) -> bool:
        """Clear the active worker only when *websocket* still owns it.

        A replacement browser can connect before the older websocket receives
        its close notification.  The identity check prevents that stale close
        from disconnecting the replacement worker.
        """
        if websocket is not None and self.worker_ws is not websocket:
            return False
        if self.worker_ws is None:
            return False
        self.worker_ws = None
        self._worker_usable = False
        self._fail_pending(reason)
        return True

    def invalidate_worker(
        self,
        websocket: Any | None = None,
        *,
        reason: str,
    ) -> Any | None:
        """Quarantine a stuck worker and return it for asynchronous closing.

        Clearing the public worker slot is the state signal consumed by the
        application timeout handler.  The returned websocket is closed by the
        async caller, while the identity check in the websocket receive loop
        prevents that stale connection from disturbing a replacement worker.
        """
        if websocket is not None and self.worker_ws is not websocket:
            return None
        if self.worker_ws is None or not self._worker_usable:
            return None

        invalidated = self.worker_ws
        self.worker_ws = None
        self._worker_usable = False
        self._last_worker_invalidation_reason = reason

        # The only pending job is the serialized in-flight request whose HTTP
        # waiter has already timed out.  Cancellation wakes run() without
        # leaving an unobserved exception on that abandoned future.
        for job in list(self.pending.values()):
            job.abandoned = True
            if not job.future.done():
                job.future.cancel()
        self.pending.clear()

        callback = self._worker_invalidated_callback
        if callback is not None:
            try:
                callback(reason)
            except Exception as exc:
                error("worker_invalidation_callback", exc)
        return invalidated

    def _fail_pending(self, reason: str) -> None:
        for job in list(self.pending.values()):
            if not job.future.done():
                job.future.set_exception(RuntimeError(reason))
        self.pending.clear()

    def _fail_queued(self, reason: str) -> None:
        while True:
            try:
                job = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                job.abandoned = True
                if not job.future.done():
                    job.future.set_exception(RuntimeError(reason))
            finally:
                self.queue.task_done()

    async def _close_invalidated_worker(self, websocket: Any | None) -> None:
        if websocket is None:
            return
        close = getattr(websocket, "close", None)
        if not callable(close):
            return
        try:
            result = close(code=1011)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=2.0)
        except Exception as exc:
            debug("worker_close_after_timeout", str(exc))

    async def submit(self, text: str, timeout: int) -> str:
        if not self.worker_available:
            raise RuntimeError("worker is not connected")
        loop = asyncio.get_running_loop()
        job = TranslationJob(
            id=str(uuid.uuid4()),
            text=text,
            future=loop.create_future(),
            created_at=time.time(),
        )
        await self.queue.put(job)
        debug("queue_submit", f"id: {job.id}\nchars: {len(text)}")
        try:
            # wait_for cancels its awaitable on timeout. Shielding leaves the
            # job future under queue ownership so the timeout path can either
            # skip a queued request or cancel and quarantine an in-flight one.
            return await asyncio.wait_for(asyncio.shield(job.future), timeout=timeout)
        except asyncio.TimeoutError:
            if job.id in self.pending:
                reason = f"translation timed out after {timeout} seconds"
                websocket = self.invalidate_worker(
                    job.worker_ws,
                    reason=reason,
                )
                if not job.future.done():
                    job.future.cancel()
                await self._close_invalidated_worker(websocket)
            else:
                # This request has not reached a worker, so run() can skip it
                # without invalidating an otherwise healthy active session.
                job.abandoned = True
                if not job.future.done():
                    job.future.cancel()
            error("translation_timeout", f"id: {job.id}, chars: {len(text)}")
            raise

    async def run(self, max_new_tokens: int) -> None:
        self.running = True
        try:
            while self.running:
                job = await self.queue.get()
                try:
                    if job.abandoned:
                        if not job.future.done():
                            job.future.cancel()
                        continue
                    if not self.worker_available:
                        raise RuntimeError("worker is not connected")
                    worker = self.worker_ws
                    job.worker_ws = worker
                    self.pending[job.id] = job
                    payload = {
                        "type": "translate",
                        "id": job.id,
                        "text": job.text,
                        "max_new_tokens": max_new_tokens,
                    }
                    await worker.send_text(json.dumps(payload, ensure_ascii=False))
                    await asyncio.shield(job.future)
                except asyncio.CancelledError:
                    # A job future can be cancelled independently (for example, a
                    # timed-out queued request).  That must not stop the worker
                    # task.  Cancellation of the worker task itself still exits.
                    if job.future.cancelled():
                        debug("queue_job_cancelled", f"id: {job.id}")
                        continue
                    raise
                except Exception as exc:
                    error("queue_run", exc)
                    if not job.future.done():
                        job.future.set_exception(exc)
                finally:
                    self.pending.pop(job.id, None)
                    self.queue.task_done()
        finally:
            self.running = False

    def resolve(self, request_id: str, text: str) -> None:
        job = self.pending.get(request_id)
        if job and not job.future.done():
            job.future.set_result(text)

    def reject(self, request_id: str, message: str) -> None:
        job = self.pending.get(request_id)
        if job and not job.future.done():
            job.future.set_exception(RuntimeError(message))

    def stop(self) -> None:
        self.running = False
        self.worker_ws = None
        self._worker_usable = False
        self._fail_pending("server is stopping")
        self._fail_queued("server is stopping")
