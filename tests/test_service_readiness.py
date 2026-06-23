from __future__ import annotations

import unittest
from unittest.mock import patch

from mekicopy_companions import (
    _probe_service,
    _validate_service_health,
    _validated_translation_text,
)


class ServiceReadinessTests(unittest.TestCase):
    def test_rejects_an_unexpected_service_on_the_configured_port(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "대신"):
            _validate_service_health(
                "MekiScript",
                {"ok": True, "app": "DifferentApp"},
            )

    @patch("mekicopy_companions._json_request")
    def test_hytrans_worker_disconnect_is_not_reported_as_normal(self, request) -> None:
        request.side_effect = [
            {"ok": True, "app": "HYTrans", "server": "running"},
            {"ready": False, "workerConnected": False, "state": "STARTING"},
        ]

        with self.assertRaisesRegex(RuntimeError, "HYTransWorker"):
            _probe_service("HYTrans", "http://127.0.0.1:6996")

    @patch("mekicopy_companions._json_request")
    def test_hytrans_requires_a_ready_model(self, request) -> None:
        request.side_effect = [
            {"ok": True, "app": "HYTrans", "server": "running"},
            {"ready": False, "workerConnected": True, "state": "WORKER_LOADING"},
        ]

        with self.assertRaisesRegex(RuntimeError, "준비되지"):
            _probe_service("HYTrans", "http://127.0.0.1:6996")

    @patch("mekicopy_companions._json_request")
    def test_ready_hytrans_is_accepted(self, request) -> None:
        request.side_effect = [
            {"ok": True, "app": "HYTrans", "server": "running"},
            {"ready": True, "workerConnected": True, "state": "READY"},
        ]

        self.assertEqual(
            _probe_service("HYTrans", "http://127.0.0.1:6996"),
            "READY",
        )

    def test_empty_translation_is_not_silently_accepted(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "빈 번역"):
            _validated_translation_text({"ok": True, "text": "  "})
        self.assertEqual(
            _validated_translation_text({"ok": True, "text": " 번역 결과 "}),
            "번역 결과",
        )


if __name__ == "__main__":
    unittest.main()
