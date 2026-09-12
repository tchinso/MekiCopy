from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

import audio_capture_core
import meki_subtitle_paths
import meki_subtitle_pipeline as pipeline
import mekicopy
import meki_subtitle_window as subtitle_window_module
from meki_subtitle_window import MekiSubtitleWindow


class MekiSubtitlePathTests(unittest.TestCase):
    def test_frozen_style_layout_uses_sibling_audio_capture_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary)
            copy_root = suite / "MekiCopy"
            capture_root = suite / "MekiAudioCapture"
            copy_root.mkdir()
            capture_root.mkdir()
            with mock.patch.object(meki_subtitle_paths, "app_root", return_value=copy_root):
                actual = meki_subtitle_paths.shared_stt_model_root()
        self.assertEqual(actual, capture_root / "models")

    def test_explicit_ffmpeg_directory_is_resolved_as_a_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            suffix = ".exe" if os.name == "nt" else ""
            ffmpeg = directory / f"ffmpeg{suffix}"
            ffprobe = directory / f"ffprobe{suffix}"
            ffmpeg.touch()
            ffprobe.touch()
            with mock.patch.dict(
                os.environ,
                {"MEKI_SUBTITLE_FFMPEG_DIR": str(directory)},
                clear=False,
            ):
                tools = meki_subtitle_paths.resolve_media_tools()
        self.assertEqual(tools.ffmpeg, ffmpeg)
        self.assertEqual(tools.ffprobe, ffprobe)

    def test_frozen_resource_root_is_checked_for_bundled_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary)
            copy_root = suite / "MekiCopy"
            resource_root = copy_root / "_internal"
            directory = resource_root / "assets" / "ffmpeg"
            directory.mkdir(parents=True)
            suffix = ".exe" if os.name == "nt" else ""
            ffmpeg = directory / f"ffmpeg{suffix}"
            ffprobe = directory / f"ffprobe{suffix}"
            ffmpeg.touch()
            ffprobe.touch()
            with (
                mock.patch.object(meki_subtitle_paths, "app_root", return_value=copy_root),
                mock.patch.object(meki_subtitle_paths.sys, "_MEIPASS", resource_root, create=True),
                mock.patch.dict(
                    os.environ,
                    {"MEKI_SUBTITLE_FFMPEG_DIR": ""},
                    clear=False,
                ),
            ):
                tools = meki_subtitle_paths.resolve_media_tools()
        self.assertEqual(tools.ffmpeg, ffmpeg)
        self.assertEqual(tools.ffprobe, ffprobe)


class MekiSubtitlePipelineTests(unittest.TestCase):
    def test_prepare_stt_models_passes_the_shared_capture_root(self) -> None:
        owner = Path("C:/suite/MekiAudioCapture")
        model_root = owner / "models"
        expected = {"vad": model_root / "vad" / "silero_vad.onnx"}
        with (
            mock.patch.object(
                pipeline, "shared_audio_capture_app_root", return_value=owner
            ),
            mock.patch.object(pipeline, "shared_stt_model_root", return_value=model_root),
            mock.patch.object(audio_capture_core, "normalize_stt_model", return_value="parakeet"),
            mock.patch.object(audio_capture_core, "ensure_models", return_value=expected) as ensure,
        ):
            actual = pipeline.prepare_stt_models(stt_model="parakeet")

        self.assertEqual(actual, expected)
        self.assertEqual(ensure.call_args.args[:2], (owner, owner))
        self.assertEqual(ensure.call_args.kwargs["model_key"], "parakeet")
        self.assertEqual(ensure.call_args.kwargs["model_root"], model_root)

    def test_injected_translator_receives_supported_keywords(self) -> None:
        calls: list[tuple[str, float, object]] = []
        event = object()

        def translate(text: str, *, timeout: float, cancel_event: object) -> str:
            calls.append((text, timeout, cancel_event))
            return "번역"

        actual = pipeline._call_translation(
            translate,
            "日本語",
            timeout=12.5,
            cancel_event=event,
        )
        self.assertEqual(actual, "번역")
        self.assertEqual(calls, [("日本語", 12.5, event)])

    def test_process_video_uses_shared_pipeline_and_writes_srt(self) -> None:
        class FakeRecognizer:
            @staticmethod
            def create_stream():
                return SimpleNamespace(
                    result=SimpleNamespace(text="日本語"),
                    accept_waveform=lambda *_args: None,
                )

            @staticmethod
            def decode_stream(_stream):
                return None

        segment = SimpleNamespace(
            start_time=1.25,
            end_time=2.5,
            previous_overlap=0.0,
            audio=np.zeros(16_000, dtype=np.float32),
        )
        translated: list[str] = []

        def translate(text: str, **_kwargs) -> str:
            translated.append(text)
            return "한국어"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "video.mp4"
            destination = root / "result.srt"
            source.touch()
            tools = pipeline.MediaTools(root / "ffmpeg.exe", root / "ffprobe.exe")
            with (
                mock.patch.object(pipeline, "prepare_stt_models", return_value={"vad": root / "vad.onnx"}),
                mock.patch.object(pipeline, "probe_media", return_value=pipeline.MediaInfo(3.0, 0, "aac")),
                mock.patch.object(pipeline, "extract_original_audio"),
                mock.patch.object(pipeline, "writable_app_subdir", return_value=root),
                mock.patch.object(audio_capture_core, "wav_to_mono_16k", return_value=np.zeros(16_000, dtype=np.float32)),
                mock.patch.object(audio_capture_core, "collect_vad_intervals", return_value=[(0, 16_000)]),
                mock.patch.object(audio_capture_core, "build_segments", return_value=[segment]),
                mock.patch.object(audio_capture_core, "create_recognizer", return_value=FakeRecognizer()),
            ):
                summary = pipeline.process_video(
                    source,
                    destination,
                    translate=translate,
                    media_tools=tools,
                )

            text = destination.read_text(encoding="utf-8-sig")

        self.assertEqual(translated, ["日本語"])
        self.assertEqual(summary.recognized, 1)
        self.assertEqual(summary.translated, 1)
        self.assertIn("00:00:01,250 --> 00:00:02,500", text)
        self.assertIn("한국어", text)

    def test_stt_download_progress_honors_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "video.mp4"
            destination = root / "result.srt"
            source.touch()
            cancelled = threading.Event()
            tools = pipeline.MediaTools(root / "ffmpeg.exe", root / "ffprobe.exe")

            def prepare(**kwargs):
                cancelled.set()
                kwargs["progress"]("모델 다운로드 중... 5%")
                raise AssertionError("cancellation callback should have raised")

            with mock.patch.object(pipeline, "prepare_stt_models", side_effect=prepare):
                with self.assertRaises(pipeline.SubtitleCancelledError):
                    pipeline.process_video(
                        source,
                        destination,
                        media_tools=tools,
                        translate=lambda text: text,
                        cancel_event=cancelled,
                    )

    def test_write_srt_atomic_keeps_multiline_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subtitle.srt"
            count = pipeline.write_srt_atomic(
                [pipeline.SubtitleEntry(0, 1, " 첫 줄\n둘째 줄 ")],
                path,
            )
            contents = path.read_text(encoding="utf-8-sig")
        self.assertEqual(count, 1)
        self.assertIn("00:00:00,000 --> 00:00:01,000", contents)
        self.assertIn("첫 줄\n둘째 줄", contents)


class MekiSubtitleIntegrationTests(unittest.TestCase):
    def test_window_job_uses_fast_vad_shared_stt_and_existing_hytrans(self) -> None:
        shared_root = Path("C:/suite/MekiAudioCapture/models")
        summary = pipeline.SubtitleProcessSummary(
            output_path=Path("C:/suite/output.srt"),
            media_duration=1.0,
            vad_intervals=1,
            recognized=1,
            translated=1,
            translation_failures=0,
            elapsed=0.1,
        )
        translator = mock.Mock(return_value="번역")
        events = mock.Mock()
        window = SimpleNamespace(
            _translate=translator,
            _events=events,
            _emit_status=mock.Mock(),
            _cancel_event=threading.Event(),
        )
        with (
            mock.patch.object(subtitle_window_module, "shared_stt_model_root", return_value=shared_root),
            mock.patch.object(subtitle_window_module, "process_video", return_value=summary) as process,
        ):
            MekiSubtitleWindow._run_job(
                window,
                Path("C:/suite/input.mp4"),
                Path("C:/suite/output.srt"),
                "parakeet",
                "int8",
            )
        self.assertEqual(process.call_args.kwargs["vad_preset"], "FAST")
        self.assertIs(process.call_args.kwargs["translate"], translator)
        self.assertEqual(process.call_args.kwargs["stt_model_root"], shared_root)
        events.put.assert_called_once_with(("done", summary))

    def test_subtitle_job_does_not_start_when_hytrans_launch_failed(self) -> None:
        class Value:
            def __init__(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "video.mp4"
            source.touch()
            start_hytrans = mock.Mock(return_value=False)
            window = SimpleNamespace(
                input_var=Value(str(source)),
                output_var=Value(str(root / "output.srt")),
                _start_hytrans=start_hytrans,
            )
            MekiSubtitleWindow._start(window)
        start_hytrans.assert_called_once_with()

    def test_hytrans_port_conflict_reports_unsuccessful_start(self) -> None:
        window = SimpleNamespace(
            _hytrans_restart_after_id=None,
            hytrans_process=None,
            settings=SimpleNamespace(hytrans_port=6996),
            _hytrans_base_url=lambda: "http://127.0.0.1:6996",
            _tracked_process_is_starting=mock.Mock(return_value=False),
            _warn_if_port_busy=mock.Mock(return_value=True),
        )
        with mock.patch.object(mekicopy, "_json_request", side_effect=OSError):
            started = mekicopy.MainWindow._on_start_hytrans(window)
        self.assertFalse(started)
        window._warn_if_port_busy.assert_called_once_with("HYTrans", 6996)

    def test_inflight_hytrans_launch_is_not_reported_as_a_failure(self) -> None:
        window = SimpleNamespace(
            hytrans_process=object(),
            _tracked_process_is_starting=mock.Mock(return_value=True),
        )
        started = mekicopy.MainWindow._launch_hytrans(window)
        self.assertTrue(started)
        window._tracked_process_is_starting.assert_called_once_with(
            "HYTrans", window.hytrans_process
        )

    def test_parent_shutdown_marks_subtitle_window_closing_and_hides_it(self) -> None:
        window = SimpleNamespace(
            _closing=False,
            _cancel=mock.Mock(),
            withdraw=mock.Mock(),
        )
        MekiSubtitleWindow.cancel_for_parent_shutdown(window)
        self.assertTrue(window._closing)
        window._cancel.assert_called_once_with()
        window.withdraw.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
