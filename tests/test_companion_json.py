from __future__ import annotations

import io
import unittest
from types import SimpleNamespace

from companion_json import read_json


class CompanionJsonTests(unittest.TestCase):
    def handler(self, raw, **headers):
        return SimpleNamespace(headers={"Content-Length": str(len(raw)), **headers},
                               rfile=io.BytesIO(raw), close_connection=False)

    def test_fully_consumed_invalid_json_keeps_connection_available(self):
        for raw in (b"null", b"[]", b"1", b'"text"', b"invalid", b"\xff"):
            with self.subTest(raw=raw):
                handler = self.handler(raw)
                with self.assertRaises(ValueError):
                    read_json(handler)
                self.assertFalse(handler.close_connection)

    def test_incomplete_oversized_and_chunked_bodies_close_connection(self):
        for headers in ({"Content-Length": "20"}, {"Content-Length": "-1"},
                        {"Content-Length": "1048577"}, {"Content-Length": "bad"},
                        {"Transfer-Encoding": "chunked"}):
            with self.subTest(headers=headers):
                handler = self.handler(b"{}", **headers)
                with self.assertRaises(ValueError):
                    read_json(handler)
                self.assertTrue(handler.close_connection)

    def test_valid_and_empty_requests(self):
        self.assertEqual(read_json(self.handler(b'{"text":"hello"}')), {"text": "hello"})
        self.assertEqual(read_json(self.handler(b"")), {})
