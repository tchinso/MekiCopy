from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .logging_setup import debug, error


@dataclass
class TranslationJob:
    id: str
    text: str
    future: asyncio.Future[str]
    created_at: float
    abandoned: bool = False


class TranslationQueue:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[TranslationJob] = asyncio.Queue()
        self.pending: dict[str, TranslationJob] = {}
        self.worker_ws: Any = None
        self.running = False

    def set_worker(self, websocket: Any) -> None:
        if self.worker_ws is websocket:
            return
        if self.worker_ws is not None:
            self._fail_pending("worker replaced")
        self.worker_ws = websocket

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
        self._fail_pending(reason)
        return True

    def _fail_pending(self, reason: str) -> None:
        for job in list(self.pending.values()):
            if not job.future.done():
                job.future.set_exception(RuntimeError(reason))
        self.pending.clear()

    async def submit(self, text: str, timeout: int) -> str:
        if self.worker_ws is None:
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
            # wait_for cancels its awaitable on timeout.  Shielding lets the
            # queue worker finish (or reject) the in-flight job instead of
            # propagating CancelledError into TranslationQueue.run().
            return await asyncio.wait_for(asyncio.shield(job.future), timeout=timeout)
        except asyncio.TimeoutError:
            # A job that has already reached the worker must remain pending so
            # a late result can release the serialized worker loop.  A queued
            # job has no consumer yet and can be safely skipped when run() sees
            # it.
            if job.id not in self.pending:
                job.abandoned = True
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
                    if self.worker_ws is None:
                        raise RuntimeError("worker is not connected")
                    worker = self.worker_ws
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
        self._fail_pending("server is stopping")
