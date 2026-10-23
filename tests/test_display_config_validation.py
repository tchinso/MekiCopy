import unittest
from dataclasses import asdict

from meki_script import ScriptConfig
from meki_overlayer import OverlayConfig
from overlay_receipts import DisplayReceipts


class DisplayValidationTests(unittest.TestCase):
    def test_invalid_numeric_configuration_is_atomic(self):
        for config, method, size_key in ((ScriptConfig(), "update", "original_size"),
                                         (OverlayConfig(), "update_from_dict", "text_size")):
            before = asdict(config)
            for values in ({"topmost": False, size_key: "bad"},
                           {"topmost": False, "opacity": float("nan")}):
                with self.subTest(config=type(config).__name__, values=values):
                    with self.assertRaises(ValueError):
                        getattr(config, method)(values)
                    self.assertEqual(asdict(config), before)

    def test_receipt_capacity_rejects_invalid_values(self):
        for limit in (-1, 0, True, 1.5):
            with self.assertRaises(ValueError):
                DisplayReceipts(limit)

    def test_receipt_eviction_wakes_waiter(self):
        receipts = DisplayReceipts(1)
        first = receipts.create("first")
        receipts.create("second")
        self.assertTrue(first.event.is_set())
        self.assertFalse(first.displayed)
        self.assertTrue(receipts.cancelled("first"))


class DisplayEventRecoveryTests(unittest.TestCase):
    def test_bad_configuration_does_not_stop_transcript_events(self):
        import queue
        from unittest import mock
        from meki_script import ScriptWindow
        window = object.__new__(ScriptWindow)
        window.root = mock.Mock()
        window.config = ScriptConfig()
        window.events = queue.Queue()
        window.events.put(("config", {"original_size": "invalid"}))
        window.events.put(("append", {"id": "one", "text": "hello"}))
        window._append = mock.Mock()
        window.apply_config = mock.Mock()
        with mock.patch("meki_script.log_error") as log:
            window.drain()
        log.assert_called_once()
        window._append.assert_called_once_with({"id": "one", "text": "hello"})
        window.root.after.assert_called_once()
        window.apply_config.assert_not_called()
