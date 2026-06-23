from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from hytrans import app as hytrans_app


class TranslationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        hytrans_app.state.worker_connected = True
        hytrans_app.state.worker_ready = True
        hytrans_app.state.state = "READY"
        hytrans_app.state.error = None

    def tearDown(self) -> None:
        hytrans_app.state.worker_connected = False
        hytrans_app.state.worker_ready = False
        hytrans_app.state.state = "STARTING"
        hytrans_app.state.error = None

    def test_japanese_translation_is_forwarded_to_overlayer(self) -> None:
        submit = AsyncMock(return_value="한국어 번역")
        overlay = AsyncMock()
        with patch.object(hytrans_app.translation_queue, "submit", submit), patch.object(
            hytrans_app,
            "_send_to_overlay",
            overlay,
        ):
            response = asyncio.run(
                hytrans_app.translate_and_show(
                    hytrans_app.TranslateBody(
                        text="日本語の文章です。",
                        overlayUrl="http://127.0.0.1:6997/show",
                    )
                )
            )

        self.assertTrue(response["ok"])
        self.assertEqual(response["text"], "한국어 번역")
        overlay.assert_awaited_once_with(
            "한국어 번역",
            "http://127.0.0.1:6997/show",
        )

    def test_empty_worker_result_fails_before_overlay(self) -> None:
        submit = AsyncMock(return_value="  ")
        overlay = AsyncMock()
        with patch.object(hytrans_app.translation_queue, "submit", submit), patch.object(
            hytrans_app,
            "_send_to_overlay",
            overlay,
        ):
            with self.assertRaises(HTTPException):
                asyncio.run(
                    hytrans_app.translate_and_show(
                        hytrans_app.TranslateBody(text="日本語")
                    )
                )
        overlay.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
