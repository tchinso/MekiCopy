from __future__ import annotations

import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from meki_overlayer import make_handler


class MekiOverlayerServerTests(unittest.TestCase):
    def _config_post_handler(self, app_ref: SimpleNamespace, payload: dict) -> object:
        handler_type = make_handler(app_ref)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler = object.__new__(handler_type)
        handler.path = "/config"
        handler.headers = {"Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.wfile = io.BytesIO()
        statuses: list[int] = []
        handler.send_response = statuses.append
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler_type.do_POST(handler)
        handler.statuses = statuses
        return handler

    def test_full_config_queue_returns_retryable_failure_to_the_producer(self) -> None:
        app_ref = SimpleNamespace(
            config=SimpleNamespace(debug_log=False),
            enqueue_config=mock.Mock(return_value=False),
        )
        payload = {"text_color": "#ffffff"}

        handler = self._config_post_handler(app_ref, payload)

        app_ref.enqueue_config.assert_called_once_with(payload)
        self.assertEqual(handler.statuses, [503])
        self.assertEqual(
            json.loads(handler.wfile.getvalue().decode("utf-8")),
            {
                "ok": False,
                "error": "MekiOverlayer UI queue is busy; retry shortly",
            },
        )


if __name__ == "__main__":
    unittest.main()
