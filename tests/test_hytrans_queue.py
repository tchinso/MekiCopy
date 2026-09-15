from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import unittest

from hytrans.queue import TranslationQueue


class _ImmediateWorker:
    def __init__(self, translation_queue: TranslationQueue) -> None:
        self.translation_queue = translation_queue
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        self.translation_queue.resolve(str(payload["id"]), "번역")


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


if __name__ == "__main__":
    unittest.main()
