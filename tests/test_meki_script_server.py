from __future__ import annotations

import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from meki_script import ScriptWindow, make_handler


class MekiScriptServerTests(unittest.TestCase):
    def test_transcript_scrollback_keeps_the_requested_bounded_capacity(self) -> None:
        self.assertEqual(ScriptWindow.MAX_HISTORY_ENTRIES, 2048)
        self.assertEqual(ScriptWindow.MAX_HISTORY_CHARS, 512 * 1024)

    def _post_handler(self, window: SimpleNamespace, payload: dict) -> object:
        handler_type = make_handler(window)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler = object.__new__(handler_type)
        handler.path = "/append"
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

    def test_full_ui_queue_returns_retryable_failure_to_the_producer(self) -> None:
        window = SimpleNamespace(enqueue=mock.Mock(return_value=False))
        payload = {"id": "entry-1", "text": "こんにちは"}

        handler = self._post_handler(window, payload)

        window.enqueue.assert_called_once_with("append", payload)
        self.assertEqual(handler.statuses, [503])
        self.assertEqual(
            json.loads(handler.wfile.getvalue().decode("utf-8")),
            {
                "ok": False,
                "error": "MekiScript UI queue is busy; retry shortly",
            },
        )


if __name__ == "__main__":
    unittest.main()
