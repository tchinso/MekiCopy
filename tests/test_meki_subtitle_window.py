from __future__ import annotations

import io
import json
import queue
import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import meki_subtitle_window as subtitle_ui
from meki_subtitle_pipeline import SubtitleCancelledError
from meki_subtitle_window import MekiSubtitleWindow


class SubtitleJobOrderingTests(unittest.TestCase):
    @staticmethod
    def _window() -> SimpleNamespace:
        return SimpleNamespace(
            _wait_for_hytrans_ready=mock.Mock(return_value="http://localhost:6996"),
            _translate=mock.Mock(return_value="한국어"),
            _emit_status=mock.Mock(),
            _emit_log=mock.Mock(),
            _cancel_event=threading.Event(),
            _events=queue.Queue(),
        )

    def test_local_model_readiness_precedes_any_video_processing(self) -> None:
        window = self._window()
        calls = mock.Mock()
        calls.attach_mock(window._wait_for_hytrans_ready, "ready")
        summary = object()
        with mock.patch.object(subtitle_ui, "process_video", return_value=summary) as process:
            calls.attach_mock(process, "process")
            MekiSubtitleWindow._run_job(
                window, Path("video.mp4"), Path("video.ko.srt"), "parakeet", "int8",
                translate_to_korean=True, wait_for_hytrans_before_stt=True,
            )
        self.assertEqual([call[0] for call in calls.mock_calls], ["ready", "process"])
        window._wait_for_hytrans_ready.assert_called_once_with(progress_ratio=0.0)
        self.assertTrue(process.call_args.kwargs["translate_to_korean"])
        self.assertEqual(window._events.get_nowait(), ("done", summary))

    def test_japanese_only_never_waits_for_or_calls_a_translator(self) -> None:
        window = self._window()
        summary = object()
        with mock.patch.object(subtitle_ui, "process_video", return_value=summary) as process:
            MekiSubtitleWindow._run_job(
                window, Path("video.mp4"), Path("video.ja.srt"), "parakeet", "int8",
                translate_to_korean=False, wait_for_hytrans_before_stt=True,
            )
        window._wait_for_hytrans_ready.assert_not_called()
        window._translate.assert_not_called()
        self.assertIsNone(process.call_args.kwargs["translate"])
        self.assertFalse(process.call_args.kwargs["translate_to_korean"])
        self.assertEqual(window._events.get_nowait(), ("done", summary))

    def test_cancel_during_model_loading_does_not_start_recognition(self) -> None:
        window = self._window()
        window._wait_for_hytrans_ready.side_effect = SubtitleCancelledError("취소됨")
        with mock.patch.object(subtitle_ui, "process_video") as process:
            MekiSubtitleWindow._run_job(
                window, Path("video.mp4"), Path("video.ko.srt"), "parakeet", "int8",
                wait_for_hytrans_before_stt=True,
            )
        process.assert_not_called()
        self.assertEqual(window._events.get_nowait(), ("cancelled", "취소됨"))

    def test_model_loading_timeout_does_not_start_recognition(self) -> None:
        window = self._window()
        window._wait_for_hytrans_ready.side_effect = TimeoutError("모델 준비 시간 초과")
        with mock.patch.object(subtitle_ui, "process_video") as process:
            MekiSubtitleWindow._run_job(
                window, Path("video.mp4"), Path("video.ko.srt"), "parakeet", "int8",
                wait_for_hytrans_before_stt=True,
            )
        process.assert_not_called()
        event = window._events.get_nowait()
        self.assertEqual(event[:2], ("error", "모델 준비 시간 초과"))


class SubtitleReadinessTests(unittest.TestCase):
    @staticmethod
    def _window() -> SimpleNamespace:
        cancel_event = mock.Mock()
        cancel_event.is_set.return_value = False
        return SimpleNamespace(
            _hytrans_url=lambda: "http://localhost:6996/",
            _HYTRANS_READY_TIMEOUT_SECONDS=600,
            _cancel_event=cancel_event,
            _emit_status=mock.Mock(),
        )

    def test_local_ready_requires_connected_and_fully_loaded_worker(self) -> None:
        window = self._window()
        payloads = (
            {"backend": "local", "state": "LOADING", "ready": False, "workerConnected": True},
            {"backend": "local", "state": "READY", "ready": True, "workerConnected": False},
            {"backend": "local", "state": "READY", "ready": True, "workerConnected": True},
        )
        responses = [io.BytesIO(json.dumps(payload).encode()) for payload in payloads]
        with mock.patch.object(subtitle_ui, "urlopen", side_effect=responses) as request:
            result = MekiSubtitleWindow._wait_for_hytrans_ready(window, progress_ratio=0.0)
        self.assertEqual(result, "http://localhost:6996")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(window._cancel_event.wait.call_count, 2)
        self.assertTrue(window._emit_status.call_args_list)
        self.assertTrue(all(call.args[0] == 0.0 for call in window._emit_status.call_args_list))

    def test_cancelling_readiness_wait_does_not_contact_hytrans(self) -> None:
        window = self._window()
        window._cancel_event.is_set.return_value = True
        with mock.patch.object(subtitle_ui, "urlopen") as request:
            with self.assertRaises(SubtitleCancelledError):
                MekiSubtitleWindow._wait_for_hytrans_ready(window, progress_ratio=0.0)
        request.assert_not_called()

    def test_native_backend_readiness_does_not_require_browser_worker(self) -> None:
        window = self._window()
        response = io.BytesIO(json.dumps({
            "backend": "hdtrans", "state": "READY", "ready": True, "workerConnected": False,
        }).encode())
        with mock.patch.object(subtitle_ui, "urlopen", return_value=response):
            self.assertEqual(
                MekiSubtitleWindow._wait_for_hytrans_ready(window), "http://localhost:6996"
            )
        window._cancel_event.wait.assert_not_called()


class SubtitleOptionsUiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.start_hytrans = mock.Mock(return_value=True)
        self.settings = SimpleNamespace(
            audio_stt_precision="int8", audio_stt_model="parakeet",
            hytrans_backend="local", hytrans_model_id="mt1.5",
        )
        self.window = MekiSubtitleWindow(
            self.root, settings=self.settings,
            hytrans_url=lambda: "http://localhost:6996", start_hytrans=self.start_hytrans,
        )
        self.window.withdraw()
        self.addCleanup(self.window.destroy)

    def _start(self, *, japanese_only: bool, backend: str = "local"):
        self.settings.hytrans_backend = backend
        self.window.skip_translation_var.set(japanese_only)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "video.mp4"
            source.touch()
            self.window.input_var.set(str(source))
            self.window.output_var.set(str(root / ("video.ja.srt" if japanese_only else "video.ko.srt")))
            with mock.patch.object(subtitle_ui.threading, "Thread") as thread:
                thread.return_value.is_alive.return_value = False
                self.window._start()
            self.assertEqual(thread.call_count, 1)
            thread.return_value.start.assert_called_once_with()
            return thread.call_args.kwargs["args"]

    def test_japanese_only_start_skips_hytrans_launch(self) -> None:
        args = self._start(japanese_only=True)
        self.start_hytrans.assert_not_called()
        self.assertEqual(args[4:], (False, False))

    def test_korean_local_start_waits_for_model_before_recognition(self) -> None:
        args = self._start(japanese_only=False)
        self.start_hytrans.assert_called_once_with()
        self.assertEqual(args[4:], (True, True))

    def test_korean_api_start_keeps_readiness_at_translation_stage(self) -> None:
        args = self._start(japanese_only=False, backend="groq")
        self.start_hytrans.assert_called_once_with()
        self.assertEqual(args[4:], (True, False))

    def test_translation_option_updates_output_language_and_action(self) -> None:
        self.assertFalse(self.window.skip_translation_var.get())
        self.window.output_var.set("video.ko.srt")
        self.window.skip_translation_var.set(True)
        self.window._on_translation_changed()
        self.assertEqual(self.window.output_var.get(), "video.ja.srt")
        self.assertIn("일본어", self.window.start_button.cget("text"))
        self.assertEqual(self.window.translation_model_var.get(), "사용하지 않음 · 일본어 SRT만 생성")
        self.window.skip_translation_var.set(False)
        self.window._on_translation_changed()
        self.assertEqual(self.window.output_var.get(), "video.ko.srt")
        self.assertIn("한국어", self.window.start_button.cget("text"))


if __name__ == "__main__":
    unittest.main()
