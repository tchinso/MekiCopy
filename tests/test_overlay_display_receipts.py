from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import mekicopy_companions as companions
from companion_watchdog import default_health_evaluator
from meki_overlayer import OverlayerApp
from overlay_receipts import DisplayReceipts


class OverlayDisplayReceiptTests(unittest.TestCase):
    def app(self):
        app = object.__new__(OverlayerApp)
        app.events = queue.Queue()
        app._text_lock = threading.Lock()
        app._pending_text = None
        app._pending_request_id = None
        app.display_receipts = DisplayReceipts()
        app.label = SimpleNamespace(configure=mock.Mock())
        app.root = SimpleNamespace(deiconify=mock.Mock(), lift=mock.Mock(),
                                   update_idletasks=mock.Mock(), after=mock.Mock())
        return app

    def test_receipt_waits_until_tk_redraw_has_completed(self):
        app = self.app()
        receipt = app.display_receipts.create("one")
        app.enqueue_text("translation", "one")
        self.assertFalse(receipt.event.is_set())
        app.root.update_idletasks.side_effect = lambda: self.assertFalse(receipt.event.is_set())
        app._drain_events()
        self.assertTrue(receipt.displayed)
        self.assertIsNotNone(receipt.completed_at)
        app.label.configure.assert_called_once_with(text="translation")

    def test_superseded_text_fails_receipt_instead_of_claiming_display(self):
        app = self.app()
        first = app.display_receipts.create("one")
        second = app.display_receipts.create("two")
        app.enqueue_text("old", "one")
        app.enqueue_text("new", "two")
        self.assertTrue(first.event.is_set())
        self.assertFalse(first.displayed)
        app._drain_events()
        self.assertTrue(second.displayed)

    def test_cancelled_pending_text_never_touches_tk(self):
        app = self.app()
        receipt = app.display_receipts.create("one")
        app.enqueue_text("stale", "one")
        app.cancel_display("one")
        app._drain_events()
        app.label.configure.assert_not_called()
        self.assertFalse(receipt.displayed)
        self.assertTrue(receipt.event.is_set())

    def test_display_failure_does_not_acknowledge_success(self):
        app = self.app()
        receipt = app.display_receipts.create("one")
        app.enqueue_text("text", "one")
        app.label.configure.side_effect = RuntimeError("destroyed")
        with mock.patch("meki_overlayer.log_error"):
            app._drain_events()
        self.assertEqual(receipt.error, "destroyed")
        self.assertFalse(receipt.displayed)

    def test_receipt_storage_is_bounded_and_evicted_waiters_unblock(self):
        receipts = DisplayReceipts(limit=2)
        old = receipts.create("one")
        receipts.create("two")
        receipts.create("three")
        self.assertEqual(len(receipts.items), 2)
        self.assertTrue(old.event.is_set())
        self.assertFalse(old.displayed)

    def test_cancel_before_post_keeps_a_tombstone(self):
        receipts = DisplayReceipts()
        receipts.cancel("one")
        with self.assertRaises(ValueError):
            receipts.create("one")


class RealtimeCompanionTests(unittest.TestCase):
    def test_native_timeout_uses_existing_watchdog_recovery_without_restarting_loading(self):
        evaluate = default_health_evaluator("HYTrans")
        payload = {"ok": True, "app": "HYTrans", "backend": "hdtrans", "state": "NATIVE_TIMEOUT"}
        self.assertIn("timeout", evaluate(payload))
        self.assertIsNone(evaluate({**payload, "state": "LOADING"}))
        self.assertIsNone(evaluate({**payload, "state": "ERROR"}))

    def test_cancel_after_translation_suppresses_display_request(self):
        cancelled = threading.Event()
        def respond(url, *_args, **_kwargs):
            self.assertIn("/translate?format=json", url)
            cancelled.set()
            return {"ok": True, "text": "번역"}
        with mock.patch.object(companions, "_probe_service"), mock.patch.object(companions, "_json_request", side_effect=respond) as post:
            with self.assertRaisesRegex(RuntimeError, "취소"):
                companions.request_translation_and_show("原文", hytrans_url="http://h",
                    overlayer_url="http://o", is_cancelled=cancelled.is_set,
                    wait_for_display=True)
        self.assertEqual(post.call_count, 1)

    def test_display_time_is_measured_after_receipt_not_translation(self):
        with mock.patch.object(companions, "_probe_service"), \
             mock.patch.object(companions, "_json_request", side_effect=[
                 {"ok": True, "text": "번역"}, {"ok": True, "displayed": True, "displayedMonotonic": 42}
             ]) as post, mock.patch.object(companions.time, "monotonic", return_value=42):
            result = companions.request_translation_and_show("原文", hytrans_url="http://h",
                overlayer_url="http://o", wait_for_display=True, context_owner="ocr:one")
        self.assertEqual(result, {"text": "번역", "displayedMonotonic": 42})
        self.assertEqual(post.call_args_list[0].args[1]["contextOwner"], "ocr:one")
        self.assertTrue(post.call_args_list[1].args[1]["waitForDisplay"])

    def test_display_receipt_uses_paint_timestamp_despite_a_late_http_response(self):
        with mock.patch.object(companions, "_probe_service"), \
             mock.patch.object(companions, "_json_request", side_effect=[
                 {"ok": True, "text": "번역"},
                 {"ok": True, "displayed": True, "displayedMonotonic": 10.0},
             ]), mock.patch.object(companions.time, "monotonic", return_value=15):
            result = companions.request_translation_and_show("原文", hytrans_url="http://h",
                overlayer_url="http://o", wait_for_display=True)
        self.assertEqual(result["displayedMonotonic"], 10.0)

    def test_queue_success_without_display_ack_is_rejected(self):
        with mock.patch.object(companions, "_probe_service"), \
             mock.patch.object(companions, "_json_request", side_effect=[
                 {"ok": True, "text": "번역"}, {"ok": True}
             ]):
            with self.assertRaisesRegex(RuntimeError, "표시 완료"):
                companions.request_translation_and_show("原文", hytrans_url="http://h",
                    overlayer_url="http://o", wait_for_display=True)


if __name__ == "__main__":
    unittest.main()
