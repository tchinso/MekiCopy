from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mekicopy_settings
from mekicopy_realtime import (
    REALTIME_INTERVALS, REALTIME_LOCK_LABEL, RealtimeOcrController,
    RealtimeResult, RealtimeSession, comparison_text, detached_translation_locked,
    is_duplicate, japanese_character_count, next_start, normalize_interval, perform_cycle,
)
from mekicopy_settings import AppSettings, load_settings, save_settings


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeRoot:
    def __init__(self, clock):
        self.clock, self.pending, self.sequence = clock, {}, 0

    def after(self, delay, callback):
        self.sequence += 1
        key = str(self.sequence)
        self.pending[key] = (self.clock() + delay / 1000, callback)
        return key

    def after_cancel(self, key):
        self.pending.pop(key, None)

    def fire(self):
        key = min(self.pending, key=lambda item: self.pending[item][0])
        deadline, callback = self.pending.pop(key)
        self.clock.now = max(self.clock(), deadline)
        callback()

    @property
    def deadline(self):
        return min(item[0] for item in self.pending.values())


class FakeRunner:
    def __init__(self):
        self.pending = None
        self.submissions = 0
        self.manual_busy = False

    def is_running(self, key):
        return self.manual_busy or self.pending is not None

    def submit(self, key, operation, *, on_success, on_error):
        if self.is_running(key):
            return False
        self.pending = operation, on_success, on_error
        self.submissions += 1
        return True

    def finish(self):
        operation, on_success, on_error = self.pending
        self.pending = None
        try:
            result = operation()
        except Exception as exc:
            on_error(exc)
        else:
            on_success(result)


class DeduplicationTests(unittest.TestCase):
    def test_first_result_and_short_change(self):
        self.assertFalse(is_duplicate(None, "はい"))
        self.assertTrue(is_duplicate("はい", "はい"))
        self.assertFalse(is_duplicate("はい", "いい"))

    def test_whitespace_only_is_normalized(self):
        self.assertEqual(comparison_text("  金額  123円。\n次の行！ "), "金額 123円。 次の行！")
        self.assertTrue(is_duplicate("金額 123円。\n次の行！", "金額\t123円。 次の行！"))
        self.assertFalse(is_duplicate("123!", "124!"))
        self.assertFalse(is_duplicate("123!", "123?"))

    def test_japanese_character_count_excludes_space_ascii_punctuation(self):
        self.assertEqual(japanese_character_count("あア漢字 123abc。！？"), 4)
        self.assertEqual(japanese_character_count("ｶﾅー々〆〇"), 6)

    def test_both_originals_require_fifteen_japanese_characters(self):
        self.assertFalse(is_duplicate("あ" * 14 + "abc", "あ" * 15 + "abc"))
        self.assertFalse(is_duplicate("あ" * 15 + "abc", "あ" * 14 + "abc"))
        self.assertTrue(is_duplicate("あ" * 15 + "abc", "あ" * 15 + "abd"))

    def test_exact_92_percent_boundary_is_included(self):
        self.assertTrue(is_duplicate("あ" * 25, "あ" * 23 + "いう"))
        self.assertFalse(is_duplicate("あ" * 24, "あ" * 22 + "いう"))

    def test_numeric_and_punctuation_changes_use_only_required_distance(self):
        self.assertTrue(is_duplicate("あ" * 25 + "123!", "あ" * 25 + "124?"))

    def test_dedup_happens_before_native_translation(self):
        translate = mock.Mock()
        result = perform_cycle(lambda: "あ" * 23 + "いう", session=RealtimeSession(1),
                               previous="あ" * 25, translate=translate)
        self.assertTrue(result.skipped)
        translate.assert_not_called()

    def test_empty_and_cancelled_results_never_translate(self):
        translate = mock.Mock()
        session = RealtimeSession(1)
        result = perform_cycle(lambda: " \n", session=session, previous=None, translate=translate)
        self.assertTrue(result.skipped)
        session.cancelled.set()
        recognize = mock.Mock()
        self.assertTrue(perform_cycle(recognize, session=session, previous=None,
                                      translate=translate).skipped)
        recognize.assert_not_called()
        translate.assert_not_called()

    def test_original_text_is_preserved_for_copy_and_translation(self):
        original = "  日本語\n123！？  "
        translate = mock.Mock(return_value={"displayedMonotonic": 101.5})
        result = perform_cycle(lambda: original, session=RealtimeSession(1), previous=None,
                               translate=translate)
        self.assertEqual(result.text, original)
        translate.assert_called_once_with(original)

    def test_stop_after_ocr_discards_result_before_translation(self):
        session = RealtimeSession(1)
        def recognize():
            session.cancelled.set()
            return "日本語"
        translate = mock.Mock()
        self.assertTrue(perform_cycle(recognize, session=session, previous=None,
                                      translate=translate).skipped)
        translate.assert_not_called()

    def test_queue_acceptance_is_not_display_completion(self):
        for receipt in ({"ok": True}, {"displayedMonotonic": float("nan")}, "text"):
            with self.subTest(receipt=receipt), self.assertRaises(RuntimeError):
                perform_cycle(lambda: "日本語", session=RealtimeSession(1), previous=None,
                              translate=lambda text: receipt)


class RealtimeSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.root, self.runner = FakeRoot(self.clock), FakeRunner()
        self.outputs, self.errors, self.states = [], [], []
        self.result = RealtimeResult("原文")
        self.prepare = mock.Mock(side_effect=lambda session, previous: lambda: self.result)
        self.controller = RealtimeOcrController(
            self.root, self.runner, prepare_operation=self.prepare,
            on_result=lambda result: self.outputs.append(result) or True,
            on_error=self.errors.append,
            on_state=lambda: self.states.append(self.controller.translation_locked),
            clock=self.clock,
        )

    def start(self, translate=False, fingerprint="initial"):
        self.controller.configure(enabled=True, interval=2, translate=translate,
                                  fingerprint=fingerprint)
        self.root.fire()

    def test_fast_display_uses_interval_not_an_extra_interval(self):
        self.start(translate=True)
        self.clock.now = 100.5
        self.result = RealtimeResult("原文", displayed_at=100.5)
        self.runner.finish()
        self.assertEqual(self.root.deadline, 102)

    def test_slow_display_waits_one_second_after_actual_ack(self):
        self.start(translate=True)
        self.clock.now = 103
        self.result = RealtimeResult("原文", displayed_at=103)
        self.runner.finish()
        self.assertEqual(self.root.deadline, 104)

    def test_elapsed_deadlines_do_not_add_delay(self):
        self.start(translate=True)
        self.clock.now = 110
        self.result = RealtimeResult("原文", displayed_at=103)
        self.runner.finish()
        self.assertEqual(self.root.deadline, 110)

    def test_translation_wait_creates_no_timers_or_queued_requests(self):
        self.start(translate=True)
        self.assertFalse(self.root.pending)
        self.clock.now += 30
        self.assertEqual(self.runner.submissions, 1)
        self.assertTrue(self.controller.translation_locked)

    def test_lock_persists_during_next_cycle_wait_and_unlocks_on_mode_change(self):
        self.start(translate=True)
        self.result = RealtimeResult("原文", displayed_at=100)
        self.runner.finish()
        self.assertTrue(self.controller.translation_locked)
        self.controller.configure(enabled=True, interval=2, translate=False)
        self.assertFalse(self.controller.translation_locked)

    def test_copy_and_skipped_results_add_no_display_wait(self):
        self.start()
        self.runner.finish()
        self.assertEqual(self.root.deadline, 102)
        self.root.fire()
        self.result = RealtimeResult("原文", skipped=True)
        self.runner.finish()
        self.assertEqual(self.root.deadline, 104)

    def test_skipped_and_empty_results_keep_last_successful_baseline(self):
        self.start()
        self.runner.finish()
        self.root.fire()
        self.result = RealtimeResult("似た文", skipped=True)
        self.runner.finish()
        self.assertEqual(self.controller.previous, "原文")
        self.root.fire()
        self.result = RealtimeResult("", skipped=True)
        self.runner.finish()
        self.assertEqual(self.controller.previous, "原文")

    def test_similar_skips_do_not_chain_drift(self):
        original = "あ" * 25
        self.prepare.side_effect = lambda session, previous: lambda: perform_cycle(
            lambda: self.result.text, session=session, previous=previous)
        self.result = RealtimeResult(original)
        self.start()
        self.runner.finish()
        self.root.fire()
        self.result = RealtimeResult("あ" * 23 + "いう")
        self.runner.finish()
        self.assertEqual(self.controller.previous, original)
        self.root.fire()
        self.result = RealtimeResult("あ" * 21 + "いうえお")
        self.runner.finish()
        self.assertEqual(len(self.outputs), 2)

    def test_copy_failure_keeps_baseline_for_same_sentence_retry(self):
        self.controller.on_result = mock.Mock(side_effect=RuntimeError("clipboard failure"))
        self.start()
        self.runner.finish()
        self.assertIsNone(self.controller.previous)
        self.assertEqual(self.root.deadline, 102)
        self.controller.on_result = lambda result: True
        self.root.fire()
        self.runner.finish()
        self.assertEqual(self.controller.previous, "原文")

    def test_ocr_translation_and_display_errors_retry_at_minimum_interval(self):
        for reason in ("ocr failure", "translation failure", "display failure"):
            with self.subTest(reason=reason):
                self.prepare.side_effect = RuntimeError(reason)
                self.controller.configure(enabled=True, interval=2, translate=True,
                                          fingerprint=reason)
                started = self.clock()
                self.root.fire()
                self.assertEqual(self.root.deadline, self.clock() + 2)
                self.assertIsNone(self.controller.previous)
                self.assertFalse(self.outputs)

    def test_stop_cancels_pending_timer(self):
        self.start()
        self.runner.finish()
        self.controller.configure(enabled=False, interval=2, translate=False)
        self.assertFalse(self.root.pending)
        self.assertFalse(self.controller.active)

    def test_stop_and_start_wait_for_obsolete_worker_before_new_session(self):
        self.start(translate=True)
        old_session = self.controller.session
        self.controller.configure(enabled=False, interval=2, translate=True)
        self.controller.configure(enabled=True, interval=2, translate=True,
                                  fingerprint="new")
        self.assertTrue(old_session.cancelled.is_set())
        self.assertFalse(self.root.pending)
        self.assertEqual(self.runner.submissions, 1)
        self.runner.finish()
        self.assertIsNone(self.controller.previous)
        self.assertFalse(self.outputs)
        self.root.fire()
        self.assertEqual(self.runner.submissions, 2)

    def test_region_and_settings_change_invalidate_comparison_and_late_result(self):
        self.start()
        self.runner.finish()
        self.assertEqual(self.controller.previous, "原文")
        self.root.fire()
        self.controller.configure(enabled=True, interval=2, translate=False,
                                  fingerprint="new region or settings")
        self.assertIsNone(self.controller.previous)
        self.runner.finish()
        self.assertIsNone(self.controller.previous)

    def test_preexisting_manual_task_is_never_overlapped(self):
        self.runner.manual_busy = True
        self.start()
        self.assertEqual(self.runner.submissions, 0)
        self.assertEqual(self.root.deadline, 102)
        self.runner.manual_busy = False
        self.root.fire()
        self.assertEqual(self.runner.submissions, 1)

    def test_close_cancels_worker_and_discards_late_result(self):
        self.start()
        session = self.controller.session
        self.controller.close()
        self.assertTrue(session.cancelled.is_set())
        self.runner.finish()
        self.assertFalse(self.outputs)
        self.assertFalse(self.root.pending)

    def test_clipboard_update_reentrancy_cannot_commit_old_session(self):
        def copy_and_change_settings(result):
            self.controller.configure(enabled=True, interval=2, translate=False,
                                      fingerprint="changed inside clipboard update")
            return True
        self.controller.on_result = copy_and_change_settings
        self.start()
        self.runner.finish()
        self.assertIsNone(self.controller.previous)


class RealtimeSettingsTests(unittest.TestCase):
    def test_default_is_off_and_supported_intervals_roundtrip(self):
        self.assertFalse(AppSettings().realtime_ocr_enabled)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.cfg"
            for interval in REALTIME_INTERVALS:
                for enabled in (False, True):
                    def write(filename, text):
                        path.write_text(text, encoding="utf-8")
                        return True
                    with mock.patch.object(mekicopy_settings, "_write_state_text", write):
                        self.assertTrue(save_settings(AppSettings(
                            realtime_ocr_enabled=enabled, realtime_ocr_interval=interval)))
                    with mock.patch.object(mekicopy_settings, "SETTINGS_FILE", str(path)):
                        loaded = load_settings()
                    self.assertEqual(loaded.realtime_ocr_enabled, enabled)
                    self.assertEqual(loaded.realtime_ocr_interval, interval)

    def test_legacy_settings_and_invalid_intervals_remain_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.cfg"
            path.write_text("[settings]\nmain_always_on_top = true\n", encoding="utf-8")
            with mock.patch.object(mekicopy_settings, "SETTINGS_FILE", str(path)):
                loaded = load_settings()
            self.assertTrue(loaded.main_always_on_top)
            self.assertFalse(loaded.realtime_ocr_enabled)
        for value in (None, "wrong", float("nan"), -1, 4):
            self.assertEqual(normalize_interval(value), 2)

    def test_detached_lock_uses_running_lease_and_expires_after_main_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "realtime.json"
            self.assertFalse(detached_translation_locked(path, now=100))
            for active, translation, updated, expected in (
                (True, True, 99, True), (False, True, 99, False),
                (True, False, 99, False), (True, True, 90, False),
            ):
                path.write_text(json.dumps({"active": active, "translation": translation,
                                           "updatedAt": updated}), encoding="utf-8")
                self.assertEqual(detached_translation_locked(path, now=100), expected)


class RealtimeUiEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import mekicopy
        cls.app = mekicopy

    def test_manual_main_entry_is_blocked_without_capturing(self):
        window = SimpleNamespace(_realtime_ocr=SimpleNamespace(translation_locked=True))
        with mock.patch.object(self.app, "capture_ocr_image") as capture:
            self.assertFalse(self.app.MainWindow._on_ocr_copy(window))
            capture.assert_not_called()

    def test_actual_main_and_detached_labels_restore_on_mode_change(self):
        window = SimpleNamespace(_realtime_ocr=SimpleNamespace(translation_locked=True),
                                 settings=AppSettings())
        self.assertEqual(self.app.MainWindow.ocr_action_label(window), REALTIME_LOCK_LABEL)
        window._realtime_ocr.translation_locked = False
        self.assertEqual(self.app.MainWindow.ocr_action_label(window), "번역 후 표시")
        detached = SimpleNamespace(_realtime_locked=True, settings=AppSettings())
        self.assertEqual(self.app.DetachedOcrButtonApp.ocr_action_label(detached), REALTIME_LOCK_LABEL)
        detached.settings.overlay_translation_mode = False
        self.assertEqual(self.app.DetachedOcrButtonApp.ocr_action_label(detached), "인식 후 복사")

    def test_global_hotkey_is_blocked_for_entire_active_session(self):
        window = SimpleNamespace(
            _closing=False, _global_hotkey=SimpleNamespace(drain_activations=lambda: 1),
            settings=AppSettings(), _global_hotkey_paused=False,
            _global_hotkey_action_active=False,
            _realtime_ocr=SimpleNamespace(translation_locked=True),
            _on_ocr_copy=mock.Mock(), _schedule_global_hotkey_poll=mock.Mock(),
        )
        self.app.MainWindow._poll_global_hotkey(window)
        window._on_ocr_copy.assert_not_called()

    def test_capture_uses_mss_owned_by_current_worker(self):
        import mekicopy_capture
        manager = mekicopy_capture.CaptureManager.__new__(mekicopy_capture.CaptureManager)
        manager.sct = mock.Mock()
        shot = SimpleNamespace(size=(2, 2), rgb=b"\x00" * 12)
        local_capture = mock.MagicMock()
        local_capture.__enter__.return_value.grab.return_value = shot
        with mock.patch.object(mekicopy_capture.mss, "mss", return_value=local_capture):
            image = manager._capture_with_mss(mekicopy_capture.Region(0, 0, 2, 2))
        self.assertEqual(image.size, (2, 2))
        manager.sct.grab.assert_not_called()
        local_capture.__exit__.assert_called_once()

    def test_monitor_target_correction_processes_first_result_and_updates_profile(self):
        from mekicopy_capture import Region
        old, corrected = Region(0, 0, 100, 100), Region(100, 0, 100, 100)
        window = SimpleNamespace(
            active_region=old, active_monitor=None, active_region_ratio=None,
            settings=AppSettings(overlay_translation_mode=False), _closing=False,
            _update_status=mock.Mock(), _set_capture_status=mock.Mock(),
        )
        manager = SimpleNamespace(resolve_region=mock.Mock(return_value=(corrected, "monitor", "ratio", [])))
        with (mock.patch.object(self.app, "get_capture_manager", return_value=manager),
              mock.patch.object(self.app, "capture_ocr_image"),
              mock.patch.object(self.app, "recognize_image", return_value="同じ原文")):
            operation = self.app.MainWindow._prepare_realtime_operation(window, RealtimeSession(1), "同じ原文")
            result = operation()
        self.assertFalse(result.skipped)
        with (mock.patch.object(self.app, "save_detached_region"),
              mock.patch.object(self.app, "copy_text_to_clipboard") as copy):
            self.assertTrue(self.app.MainWindow._on_realtime_result(window, result))
        self.assertEqual(window.active_region, corrected)
        self.assertEqual(window.active_monitor, "monitor")
        copy.assert_called_once_with("同じ原文", notify=False, parent=window)


if __name__ == "__main__":
    unittest.main()
