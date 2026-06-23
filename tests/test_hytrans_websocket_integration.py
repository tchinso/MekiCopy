from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from hytrans import app as hytrans_app


class HytransWebSocketIntegrationTests(unittest.TestCase):
    def test_japanese_request_reaches_worker_and_overlay(self) -> None:
        overlay = AsyncMock()
        with patch.object(hytrans_app, "_send_to_overlay", overlay), TestClient(
            hytrans_app.app
        ) as client, client.websocket_connect("/ws/worker") as worker:
            worker.send_json(
                {
                    "type": "ready",
                    "device": "wasm",
                    "model": "test-model",
                    "dtype": "q4",
                    "modelMode": "local",
                }
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                response_future = executor.submit(
                    client.post,
                    "/translate-and-show",
                    json={
                        "text": "日本語の文章です。",
                        "overlayUrl": "http://127.0.0.1:6997/show",
                    },
                )
                request = worker.receive_json()
                self.assertEqual(request["type"], "translate")
                self.assertEqual(request["text"], "日本語の文章です。")
                worker.send_json(
                    {
                        "type": "result",
                        "id": request["id"],
                        "text": "한국어 번역입니다.",
                    }
                )
                response = response_future.result(timeout=5)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "한국어 번역입니다.")
        overlay.assert_awaited_once_with(
            "한국어 번역입니다.",
            "http://127.0.0.1:6997/show",
        )


if __name__ == "__main__":
    unittest.main()
