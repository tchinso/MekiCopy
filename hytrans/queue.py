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


class TranslationQueue:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[TranslationJob] = asyncio.Queue()
        self.pending: dict[str, TranslationJob] = {}
        self.worker_ws: Any = None
        self.running = False

    def set_worker(self, websocket: Any) -> None:
        self.worker_ws = websocket

    def clear_worker(self) -> None:
        self.worker_ws = None
        for job in list(self.pending.values()):
            if not job.future.done():
                job.future.set_exception(RuntimeError("worker disconnected"))
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
            return await asyncio.wait_for(job.future, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending.pop(job.id, None)
            if not job.future.done():
                job.future.cancel()
            error("translation_timeout", f"id: {job.id}, chars: {len(text)}")
            raise

    async def run(self, max_new_tokens: int) -> None:
        self.running = True
        while self.running:
            job = await self.queue.get()
            try:
                if self.worker_ws is None:
                    raise RuntimeError("worker is not connected")
                self.pending[job.id] = job
                payload = {
                    "type": "translate",
                    "id": job.id,
                    "text": job.text,
                    "max_new_tokens": max_new_tokens,
                }
                await self.worker_ws.send_text(json.dumps(payload, ensure_ascii=False))
                await job.future
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error("queue_run", exc)
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                self.pending.pop(job.id, None)
                self.queue.task_done()

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
        for job in list(self.pending.values()):
            if not job.future.done():
                job.future.set_exception(RuntimeError("server is stopping"))
        self.pending.clear()

