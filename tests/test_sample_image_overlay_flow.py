from __future__ import annotations

import asyncio
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hytrans import app as hytrans_app
from meki_overlayer import make_handler
from mekicopy_ocr import run_meikiocr


SAMPLE_IMAGE = Path(__file__).resolve().parent.parent / "sampleimage.jpeg"


class FakeOverlayerApp:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.configs: list[dict] = []
        self.config = type("Config", (), {"debug_log": False})()

    def enqueue_text(self, text: str) -> None:
        self.texts.append(text)

    def enqueue_config(self, data: dict) -> None:
        self.configs.append(data)


class SampleImageOverlayFlowTests(unittest.TestCase):
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

    def test_sample_image_ocr_reads_japanese_text(self) -> None:
        text = run_meikiocr(str(SAMPLE_IMAGE))

        self.assertIn("真っ青", text)
        self.assertIn("錬金術", text)

    def test_sample_image_translation_is_posted_to_overlayer_http_handler(self) -> None:
        source_text = run_meikiocr(str(SAMPLE_IMAGE))
        translated = "브레세일섬에는 여러 학문이 발전하고 있습니다."
        fake_overlayer = FakeOverlayerApp()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(fake_overlayer),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            overlay_url = f"http://127.0.0.1:{server.server_port}/show"
            submit = AsyncMock(return_value=translated)
            with patch.object(hytrans_app.translation_queue, "submit", submit):
                response = asyncio.run(
                    hytrans_app.translate_and_show(
                        hytrans_app.TranslateBody(
                            text=source_text,
                            overlayUrl=overlay_url,
                        )
                    )
                )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.assertTrue(response["ok"])
        self.assertEqual(response["text"], translated)
        self.assertEqual(fake_overlayer.texts, [translated])


if __name__ == "__main__":
    unittest.main()
