from __future__ import annotations

from collections import deque
from contextlib import contextmanager
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
    @contextmanager
    def _recognized_video(self, root: Path, results=None):
        source = root / "video.mp4"
        source.touch()
        if results is None:
            results = [(0.0, 1.0, "最初"), (1.0, 2.0, "次")]
        with (
            mock.patch.object(pipeline, "prepare_stt_models", return_value={"vad": root / "vad.onnx"}),
            mock.patch.object(pipeline, "probe_media", return_value=pipeline.MediaInfo(2.0, 0, "aac")),
            mock.patch.object(pipeline, "extract_original_audio"),
            mock.patch.object(pipeline, "writable_app_subdir", return_value=root),
            mock.patch.object(audio_capture_core, "wav_to_mono_16k", return_value=np.zeros(16_000, dtype=np.float32)),
            mock.patch.object(pipeline, "collect_subtitle_segments", return_value=(1, [object()])),
            mock.patch.object(audio_capture_core, "create_recognizer"),
            mock.patch.object(pipeline, "_recognize_segments", return_value=results),
        ):
            yield source, pipeline.MediaTools(root / "ffmpeg.exe", root / "ffprobe.exe")

    def test_japanese_subtitle_paths_and_conflicting_korean_destination(self) -> None:
        for output, expected in (
            ("video.ko.srt", "video.ja.srt"),
            ("video.KO.SRT", "video.ja.srt"),
            ("video.ja.srt", "video.ja.srt"),
            ("custom.srt", "custom.ja.srt"),
        ):
            with self.subTest(output=output):
                self.assertEqual(pipeline.japanese_subtitle_path(output), Path(expected))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self._recognized_video(root) as (source, tools):
                with self.assertRaises(ValueError):
                    pipeline.process_video(source, root / "video.ja.srt", media_tools=tools)

    def test_complete_japanese_srt_precedes_translation_and_is_removed_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "video.ko.srt"
            japanese_path = root / "video.ja.srt"

            def translate(text, **_kwargs):
                japanese = japanese_path.read_text(encoding="utf-8-sig")
                self.assertIn("最初", japanese)
                self.assertIn("次", japanese)
                self.assertFalse(destination.exists())
                return "한국어"

            with self._recognized_video(root) as (source, tools):
                summary = pipeline.process_video(source, destination, media_tools=tools, translate=translate)
            self.assertEqual(summary.translated, 2)
            self.assertTrue(destination.is_file())
            self.assertFalse(japanese_path.exists())

    def test_translation_cancellation_preserves_japanese_and_existing_korean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "custom.srt"
            destination.write_text("previous Korean", encoding="utf-8")
            translate = mock.Mock(side_effect=pipeline.SubtitleCancelledError())
            with self._recognized_video(root) as (source, tools):
                with self.assertRaises(pipeline.SubtitleCancelledError):
                    pipeline.process_video(source, destination, media_tools=tools, translate=translate)
            japanese = (root / "custom.ja.srt").read_text(encoding="utf-8-sig")
            self.assertIn("最初", japanese)
            self.assertIn("次", japanese)
            self.assertEqual(destination.read_text(encoding="utf-8"), "previous Korean")
            translate.assert_called_once()

    def test_failed_translation_keeps_complete_japanese_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "video.ko.srt"
            translate = mock.Mock(side_effect=RuntimeError("translation failed"))
            with self._recognized_video(root) as (source, tools), mock.patch.object(pipeline.time, "sleep"):
                summary = pipeline.process_video(source, destination, media_tools=tools, translate=translate)
            self.assertEqual(summary.translation_failures, 2)
            self.assertEqual(summary.translated, 0)
            self.assertTrue((root / "video.ja.srt").is_file())
            self.assertIn("[번역 실패]", destination.read_text(encoding="utf-8-sig"))

    def test_failed_korean_publication_preserves_japanese_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "video.ko.srt"
            destination.write_text("previous Korean", encoding="utf-8")
            original_write = pipeline.write_srt_atomic

            def write(entries, path):
                if path == destination:
                    raise PermissionError("output is open")
                return original_write(entries, path)

            with self._recognized_video(root) as (source, tools), mock.patch.object(pipeline, "write_srt_atomic", side_effect=write):
                with self.assertRaises(PermissionError):
                    pipeline.process_video(source, destination, media_tools=tools, translate=lambda _: "한국어")
            self.assertTrue((root / "video.ja.srt").is_file())
            self.assertEqual(destination.read_text(encoding="utf-8"), "previous Korean")

    def test_japanese_only_never_creates_or_calls_a_translator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "video.ko.srt"
            translator = mock.Mock(side_effect=AssertionError("must not translate"))
            with self._recognized_video(root) as (source, tools), mock.patch.object(pipeline, "_translation_callable") as create_translator:
                summary = pipeline.process_video(
                    source, destination, media_tools=tools, translate=translator,
                    translation_url="http://127.0.0.1:6996", translate_to_korean=False,
                )
            self.assertEqual(summary.output_path, root / "video.ja.srt")
            self.assertEqual(summary.recognized, 2)
            self.assertEqual(summary.translated, 0)
            self.assertFalse(destination.exists())
            self.assertIn("次", summary.output_path.read_text(encoding="utf-8-sig"))
            create_translator.assert_not_called()
            translator.assert_not_called()

    def test_japanese_only_empty_recognition_still_publishes_japanese_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self._recognized_video(root, []) as (source, tools):
                summary = pipeline.process_video(
                    source, root / "video.ko.srt", media_tools=tools, translate_to_korean=False,
                )
            self.assertEqual(summary.output_path, root / "video.ja.srt")
            self.assertEqual(summary.output_path.read_text(encoding="utf-8-sig"), "")
            self.assertFalse((root / "video.ko.srt").exists())

    def test_silent_video_replaces_stale_japanese_checkpoint_and_finishes_without_translation(self) -> None:
        for translate_to_korean in (True, False):
            with self.subTest(translate_to_korean=translate_to_korean), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                korean = root / "video.ko.srt"
                japanese = root / "video.ja.srt"
                japanese.write_text("stale Japanese", encoding="utf-8")
                translator = mock.Mock(side_effect=AssertionError("must not translate"))
                with self._recognized_video(root) as (source, tools), mock.patch.object(
                    pipeline, "collect_subtitle_segments", return_value=(0, [])
                ):
                    summary = pipeline.process_video(
                        source, korean, media_tools=tools, translate=translator,
                        translate_to_korean=translate_to_korean,
                    )
                expected = korean if translate_to_korean else japanese
                self.assertEqual(summary.output_path, expected)
                self.assertEqual(expected.read_text(encoding="utf-8-sig"), "")
                self.assertEqual(summary.translated, 0)
                self.assertEqual(summary.recognized, 0)
                self.assertEqual(japanese.exists(), not translate_to_korean)
                self.assertEqual(korean.exists(), translate_to_korean)
                translator.assert_not_called()

    def test_padded_segments_keep_repeated_dialogue_and_nonoverlapping_cues(self) -> None:
        recognizer = SimpleNamespace(
            create_stream=lambda: SimpleNamespace(
                result=SimpleNamespace(text="はい"), accept_waveform=lambda *_args: None
            ),
            decode_stream=lambda _stream: None,
        )
        segments = [
            SimpleNamespace(start_time=start, end_time=end, previous_overlap=0.0,
                            audio=np.zeros(512, dtype=np.float32))
            for start, end in ((0.0, 1.5), (1.3, 2.0))
        ]
        results = pipeline._recognize_segments(
            recognizer, segments, model_name="Parakeet", status=None, cancel_event=None
        )
        self.assertEqual(results, [(0.0, 1.5, "はい"), (1.5, 2.0, "はい")])

    def test_positional_only_translator_options_are_not_passed_as_keywords(self):
        def translate(text, timeout=7, /):
            return f"{text}:{timeout}"

        self.assertEqual(
            pipeline._call_translation(translate, "test", timeout=12, cancel_event=None),
            "test:7",
        )

    def test_failed_srt_publication_keeps_destination_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.srt"
            path.write_text("original", encoding="utf-8")
            with mock.patch.object(pipeline.os, "replace", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    pipeline.write_srt_atomic([pipeline.SubtitleEntry(0, 1, "new")], path)
            self.assertEqual(path.read_text(encoding="utf-8"), "original")
            self.assertEqual(list(path.parent.iterdir()), [path])

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
                mock.patch.object(pipeline, "collect_subtitle_segments", return_value=(1, [segment])) as collect,
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
        self.assertNotIn("vad_preset", collect.call_args.kwargs)
        self.assertEqual(collect.call_args.kwargs["preset_name"], "FAST")
        self.assertIn("check_cancel", collect.call_args.kwargs)
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
            _emit_log=mock.Mock(),
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
        self.assertNotIn("vad_preset", process.call_args.kwargs)
        translate_cue = process.call_args.kwargs["translate"]
        self.assertEqual(translate_cue("最初"), "번역")
        self.assertEqual(translate_cue("次"), "번역")
        first_owner = translator.call_args_list[0].kwargs["context_owner"]
        self.assertTrue(first_owner.startswith("subtitle:"))
        self.assertEqual(translator.call_args_list[1].kwargs["context_owner"], first_owner)
        self.assertEqual(process.call_args.kwargs["stt_model_root"], shared_root)
        self.assertIs(process.call_args.kwargs["log"], window._emit_log)
        events.put.assert_called_once_with(("done", summary))

    def test_subtitle_job_does_not_start_when_hytrans_launch_failed(self) -> None:
        class Value:
            def __init__(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "video.mp4"
            source.touch()
            start_hytrans = mock.Mock(return_value=False)
            window = SimpleNamespace(
                input_var=Value(str(source)),
                output_var=Value(str(root / "output.srt")),
                skip_translation_var=SimpleNamespace(get=lambda: False),
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


class MekiSubtitleUiBufferTests(unittest.TestCase):
    @staticmethod
    def _buffer_window() -> SimpleNamespace:
        return SimpleNamespace(
            _event_lock=threading.Lock(),
            _latest_status=None,
            _pending_logs=deque(),
            _dropped_log_count=0,
        )

    def test_status_updates_are_coalesced_to_the_latest_value(self) -> None:
        window = self._buffer_window()
        MekiSubtitleWindow._emit_status(window, 0.1, "첫 상태")
        MekiSubtitleWindow._emit_status(window, 0.9, "최신 상태")
        status, logs = MekiSubtitleWindow._take_pending_updates(window)
        self.assertEqual(status, (0.9, "최신 상태"))
        self.assertEqual(logs, [])
        self.assertIsNone(window._latest_status)

    def test_log_buffer_keeps_recent_entries_and_reports_drops(self) -> None:
        window = self._buffer_window()
        window._PENDING_LOG_MAX_ENTRIES = 2
        for text in ("첫 로그", "둘째 로그", "셋째 로그", "넷째 로그"):
            MekiSubtitleWindow._emit_log(window, text)
        status, logs = MekiSubtitleWindow._take_pending_updates(window)
        self.assertIsNone(status)
        self.assertEqual(
            logs,
            [
                "… 이전 처리 기록 2개는 생략했습니다.",
                "셋째 로그",
                "넷째 로그",
            ],
        )

    def test_visible_log_trim_removes_excess_lines_in_one_batch(self) -> None:
        log_text = mock.Mock()
        log_text.index.return_value = "2002.0"
        window = SimpleNamespace(
            log_text=log_text,
            _VISIBLE_LOG_MAX_LINES=2_000,
        )
        MekiSubtitleWindow._trim_visible_log(window)
        log_text.delete.assert_called_once_with("1.0", "3.0")


if __name__ == "__main__":
    unittest.main()
