from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import unittest

from hytrans.queue import TranslationQueue, TranslationQueueOverloadedError


class _ImmediateWorker:
    def __init__(self, translation_queue: TranslationQueue) -> None:
        self.translation_queue = translation_queue
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        self.translation_queue.resolve(str(payload["id"]), "번역")


class _BlockingWorker:
    def __init__(self) -> None:
        self.sent = asyncio.Event()
        self.release = asyncio.Event()

    async def send_text(self, raw: str) -> None:
        del raw
        self.sent.set()
        await self.release.wait()


class TranslationQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_specific_generation_budget_reaches_the_worker(self) -> None:
        translation_queue = TranslationQueue()
        worker = _ImmediateWorker(translation_queue)
        translation_queue.set_worker(worker)
        worker_task = asyncio.create_task(
            translation_queue.run(default_max_new_tokens=2_048)
        )
        try:
            self.assertEqual(
                await translation_queue.submit(
                    "실시간 발화",
                    timeout=1,
                    max_new_tokens=128,
                ),
                "번역",
            )
            self.assertEqual(
                await translation_queue.submit("일반 번역", timeout=1),
                "번역",
            )
        finally:
            translation_queue.stop()
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task

        self.assertEqual(worker.payloads[0]["max_new_tokens"], 128)
        self.assertEqual(worker.payloads[1]["max_new_tokens"], 2_048)

    async def test_full_queue_rejects_a_burst_without_waiting(self) -> None:
        translation_queue = TranslationQueue(max_queued_jobs=1)
        worker = _BlockingWorker()
        translation_queue.set_worker(worker)
        worker_task = asyncio.create_task(
            translation_queue.run(default_max_new_tokens=2_048)
        )
        first = asyncio.create_task(translation_queue.submit("first", timeout=10))
        try:
            await asyncio.wait_for(worker.sent.wait(), timeout=1)
            second = asyncio.create_task(translation_queue.submit("second", timeout=10))
            await asyncio.sleep(0)

            with self.assertRaisesRegex(
                TranslationQueueOverloadedError,
                "translation queue is busy",
            ):
                await translation_queue.submit("third", timeout=10)

            self.assertEqual(translation_queue.queue.qsize(), 1)
            translation_queue.stop()
            worker.release.set()
            with self.assertRaisesRegex(RuntimeError, "server is stopping"):
                await first
            with self.assertRaisesRegex(RuntimeError, "server is stopping"):
                await second
            await asyncio.wait_for(worker_task, timeout=1)
        finally:
            worker.release.set()
            translation_queue.stop()
            if not worker_task.done():
                worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task

    async def test_stop_wakes_an_idle_worker_without_cancellation(self) -> None:
        translation_queue = TranslationQueue()
        worker_task = asyncio.create_task(
            translation_queue.run(default_max_new_tokens=2_048)
        )
        try:
            await asyncio.sleep(0)
            translation_queue.stop()
            await asyncio.wait_for(worker_task, timeout=1)
            self.assertFalse(worker_task.cancelled())
        finally:
            translation_queue.stop()
            if not worker_task.done():
                worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task


if __name__ == "__main__":
    unittest.main()
