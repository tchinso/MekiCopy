from __future__ import annotations

import http.client
import json
import tempfile
import sys
import threading
import time
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

import audio_capture_core
import meki_audio_capture
from audio_capture_core import (
    DEFAULT_STT_MODEL,
    MODEL_FILE_HASHES,
    MODEL_FILE_SIZES,
    PARAKEET_MODEL_DIRECTORY,
    STT_MODELS,
    VAD_PRESETS,
    build_segments,
    create_recognizer,
    effective_stt_precision,
    model_root_candidates,
    validate_tokens_file,
)


class TokenTableTests(unittest.TestCase):
    def test_accepts_contiguous_ids_in_unicode_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="메키토큰-") as temporary:
            tokens = Path(temporary) / "tokens.txt"
            tokens.write_text("<blk> 0\n<sos/eos> 1\n테스트 2\n", encoding="utf-8")
            self.assertEqual(validate_tokens_file(tokens), 3)

    def test_rejects_missing_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tokens = Path(temporary) / "tokens.txt"
            tokens.write_text("<blk> 0\n테스트 2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ID 1"):
                validate_tokens_file(tokens)


class VadPresetTests(unittest.TestCase):
    def test_chunk_boundary_settings(self) -> None:
        expected = {
            "FAST": {
                "threshold": 0.60,
                "min_speech_duration": 0.20,
                "min_silence_duration": 0.10,
                "max_segment_duration": 10.0,
                "pre_padding": 0.05,
                "post_padding": 0.10,
                "merge_gap": 0.05,
                "short_segment_max_duration": 0.60,
                "forced_cut_overlap": 0.25,
            },
            "BALANCED": {
                "threshold": 0.55,
                "min_speech_duration": 0.20,
                "min_silence_duration": 0.30,
                "max_segment_duration": 15.0,
                "pre_padding": 0.10,
                "post_padding": 0.20,
                "merge_gap": 0.15,
                "short_segment_max_duration": 1.00,
                "forced_cut_overlap": 0.75,
            },
            "LONG": {
                "threshold": 0.55,
                "min_speech_duration": 0.20,
                "min_silence_duration": 0.60,
                "max_segment_duration": 20.0,
                "pre_padding": 0.15,
                "post_padding": 0.30,
                "merge_gap": 0.30,
                "short_segment_max_duration": 1.50,
                "forced_cut_overlap": 1.50,
            },
        }

        for preset_name, values in expected.items():
            with self.subTest(preset=preset_name):
                self.assertEqual(VAD_PRESETS[preset_name], values)

    def test_fast_keeps_a_short_dialogue_boundary(self) -> None:
        audio = np.zeros(32_000, dtype=np.float32)
        # A 0.16-second turn gap survives FAST's short padding but used to be
        # rejoined by the former 0.50-second combined padding.
        intervals = [(3_200, 6_400), (8_960, 12_160)]
        segments = build_segments(audio, intervals, "FAST")
        self.assertEqual(len(segments), 2)
        self.assertLess(segments[0].end_time, segments[1].start_time)

    def test_short_segment_limit_marks_metadata_without_merging(self) -> None:
        audio = np.zeros(40_000, dtype=np.float32)
        # The raw gap is larger than FAST's 0.05 s merge gap. The first
        # padded segment is <= 0.60 s, while the second is longer.
        intervals = [(3_200, 9_600), (14_400, 28_800)]

        segments = build_segments(audio, intervals, "FAST")

        self.assertEqual(len(segments), 2)
        self.assertEqual([segment.is_short for segment in segments], [True, False])


class SttModelTests(unittest.TestCase):
    def test_parakeet_is_the_default_and_reazonspeech_is_selectable(self) -> None:
        self.assertEqual(DEFAULT_STT_MODEL, "parakeet")
        self.assertIn(DEFAULT_STT_MODEL, STT_MODELS)
        self.assertIn("reazonspeech", STT_MODELS)
        self.assertEqual(effective_stt_precision("parakeet", "fp32"), "int8")
        self.assertEqual(effective_stt_precision("reazonspeech", "fp32"), "fp32")

    def test_parakeet_archive_asset_manifest_is_complete(self) -> None:
        model = STT_MODELS["parakeet"]
        self.assertTrue(model.archive_url.endswith(f"{PARAKEET_MODEL_DIRECTORY}.tar.bz2"))
        self.assertEqual(model.required_files, ("tokens.txt", "model.int8.onnx"))
        for filename in model.required_files:
            key = f"{PARAKEET_MODEL_DIRECTORY}/{filename}"
            self.assertIn(key, MODEL_FILE_SIZES)
            self.assertIn(key, MODEL_FILE_HASHES)

    def test_recognizer_factory_uses_nemo_ctc_for_parakeet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parakeet = root / PARAKEET_MODEL_DIRECTORY
            parakeet.mkdir()
            parakeet_model = parakeet / "model.int8.onnx"
            parakeet_model.touch()
            parakeet_tokens = parakeet / "tokens.txt"
            parakeet_tokens.write_text("<blk> 0\n", encoding="utf-8")

            reazon = root / "reazonspeech-ja"
            reazon.mkdir()
            reazon_tokens = reazon / "tokens.txt"
            reazon_tokens.write_text("<blk> 0\n", encoding="utf-8")
            reazon_paths = {
                "tokens": reazon_tokens,
                "encoder": reazon / "encoder.int8.onnx",
                "decoder": reazon / "decoder.onnx",
                "joiner": reazon / "joiner.int8.onnx",
            }
            for path in reazon_paths.values():
                if path != reazon_tokens:
                    path.touch()

            offline_recognizer = types.SimpleNamespace(
                from_nemo_ctc=mock.Mock(return_value="parakeet-recognizer"),
                from_transducer=mock.Mock(return_value="reazon-recognizer"),
            )
            fake_sherpa = types.SimpleNamespace(OfflineRecognizer=offline_recognizer)
            with mock.patch.dict(sys.modules, {"sherpa_onnx": fake_sherpa}):
                self.assertEqual(
                    create_recognizer(
                        {"tokens": parakeet_tokens, "model": parakeet_model},
                        model_key="parakeet",
                        num_threads=2,
                    ),
                    "parakeet-recognizer",
                )
                self.assertEqual(
                    create_recognizer(reazon_paths, model_key="reazonspeech"),
                    "reazon-recognizer",
                )

            parakeet_call = offline_recognizer.from_nemo_ctc.call_args.kwargs
            self.assertEqual(parakeet_call["model"], str(parakeet_model))
            self.assertEqual(parakeet_call["tokens"], str(parakeet_tokens))
            self.assertEqual(parakeet_call["sample_rate"], 16_000)
            self.assertEqual(parakeet_call["feature_dim"], 80)
            self.assertEqual(parakeet_call["decoding_method"], "greedy_search")
            self.assertEqual(parakeet_call["provider"], "cpu")
            self.assertEqual(
                offline_recognizer.from_transducer.call_args.kwargs["encoder"],
                str(reazon_paths["encoder"]),
            )

    def test_explicit_shared_model_root_prevents_subtitle_model_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            portable = temporary_root / "MekiAudioCapture" / "models"
            cache_parent = temporary_root / "shared-cache" / "MekiAudioCapture"
            with mock.patch.object(
                audio_capture_core,
                "fallback_app_data_dirs",
                return_value=[cache_parent],
            ):
                candidates = model_root_candidates(
                    temporary_root / "MekiSubtitle",
                    temporary_root,
                    model_root=portable,
                )
            self.assertEqual(candidates, [portable, cache_parent / "models"])


class TranslationRequestTests(unittest.TestCase):
    def test_realtime_request_marks_the_hytrans_payload(self) -> None:
        with mock.patch.object(
            audio_capture_core,
            "_post_json",
            return_value={"text": "안녕하세요"},
        ) as post_json:
            actual = audio_capture_core.translate_text(
                "http://hytrans",
                "こんにちは",
                timeout=123,
                realtime=True,
            )

        self.assertEqual(actual, "안녕하세요")
        self.assertEqual(
            post_json.call_args.args,
            ("http://hytrans/translate?format=json", {"text": "こんにちは", "realtime": True}),
        )
        self.assertEqual(post_json.call_args.kwargs, {"timeout": 123})


class CaptureControllerTests(unittest.TestCase):
    @staticmethod
    def _controller() -> meki_audio_capture.CaptureController:
        return meki_audio_capture.CaptureController(
            "int8",
            "BALANCED",
            "http://127.0.0.1:1",
            "http://127.0.0.1:2",
            prepare_models_on_start=False,
        )

    def test_header_only_recording_skips_model_loading(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session"
            session.mkdir()
            wav_path = session / "capture.wav"
            with wave.open(str(wav_path), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(48_000)

            controller.state = "STOPPING"
            controller.wav_path = wav_path
            controller.session_work_dir = session
            controller._models_for_processing = mock.Mock(side_effect=AssertionError("model load"))
            controller._finish_and_process()

        self.assertEqual(controller.state, "READY")
        self.assertEqual(controller.status, "완료: 녹음된 오디오가 없습니다.")
        controller._models_for_processing.assert_not_called()

    def test_no_speech_skips_recognizer(self) -> None:
        controller = self._controller()
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session"
            session.mkdir()
            controller.state = "STOPPING"
            controller.wav_path = session / "capture.wav"
            controller.session_work_dir = session

            with (
                mock.patch.object(meki_audio_capture, "wav_to_mono_16k", return_value=np.zeros(16_000)),
                mock.patch.object(controller, "_models_for_processing", return_value={"vad": Path("vad.onnx")}),
                mock.patch.object(meki_audio_capture, "collect_vad_intervals", return_value=[]),
                mock.patch.object(meki_audio_capture, "create_recognizer") as create_recognizer,
            ):
                controller._finish_and_process()

        self.assertEqual(controller.state, "READY")
        self.assertEqual(controller.status, "완료: 인식할 음성이 없습니다.")
        create_recognizer.assert_not_called()

    def test_stale_recording_cannot_overwrite_current_session(self) -> None:
        controller = self._controller()
        controller._session_generation = 2
        controller.state = "RECORDING"
        changed = controller._set_state_for_session(1, "ERROR", "stale")
        self.assertFalse(changed)
        self.assertEqual(controller.state, "RECORDING")

    def test_configure_is_rejected_after_start_reserves_state(self) -> None:
        controller = self._controller()
        controller.state = "STARTING"
        with self.assertRaisesRegex(RuntimeError, "설정을 바꿀 수 없습니다"):
            controller.configure({"precision": "fp32"})

    def test_default_controller_uses_parakeet_int8_and_allows_reazon_fp32(self) -> None:
        controller = self._controller()
        self.assertEqual(controller.stt_model, "parakeet")
        self.assertEqual(controller.precision, "int8")
        with mock.patch.object(controller, "prepare_models") as prepare_models:
            controller.configure({"sttModel": "reazonspeech", "precision": "fp32"})
        self.assertEqual(controller.stt_model, "reazonspeech")
        self.assertEqual(controller.precision, "fp32")
        prepare_models.assert_called_once()

    def test_cli_defaults_select_parakeet_int8(self) -> None:
        with mock.patch.object(sys, "argv", ["MekiAudioCapture"]):
            args = meki_audio_capture.parse_args()
        self.assertEqual(args.stt_model, "parakeet")
        self.assertEqual(args.precision, "int8")

    def test_realtime_start_preparation_does_not_block_the_calling_thread(self) -> None:
        controller = self._controller()
        entered = threading.Event()
        release = threading.Event()

        def hold_start(*_args: object) -> None:
            entered.set()
            release.wait(timeout=2)

        with mock.patch.object(controller, "_start_recording", side_effect=hold_start):
            started = time.monotonic()
            controller.start(realtime_translation=True)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.25)
            self.assertTrue(entered.wait(timeout=1))
            self.assertEqual(controller.state, "STARTING")
            start_thread = controller.start_thread
            self.assertIsNotNone(start_thread)
            release.set()
            assert start_thread is not None
            start_thread.join(timeout=1)
            self.assertFalse(start_thread.is_alive())

    def test_default_start_passes_a_disabled_realtime_flag_to_the_worker(self) -> None:
        controller = self._controller()
        entered = threading.Event()
        release = threading.Event()
        received: list[tuple[int, tuple[str, str, str, str, str, bool]]] = []

        def hold_start(
            generation: int,
            session_options: tuple[str, str, str, str, str, bool],
        ) -> None:
            received.append((generation, session_options))
            entered.set()
            release.wait(timeout=2)

        with mock.patch.object(controller, "_start_recording", side_effect=hold_start):
            controller.start()
            self.assertTrue(entered.wait(timeout=1))
            start_thread = controller.start_thread
            self.assertIsNotNone(start_thread)
            release.set()
            assert start_thread is not None
            start_thread.join(timeout=1)

        self.assertEqual(len(received), 1)
        self.assertFalse(received[0][1][-1])

    def test_standard_start_unpacks_the_realtime_flag_before_using_it(self) -> None:
        controller = self._controller()
        controller._session_generation = 1
        controller.state = "STARTING"
        session_options = (
            controller.stt_model,
            controller.precision,
            controller.preset,
            controller.script_url,
            controller.hytrans_url,
            False,
        )

        with tempfile.TemporaryDirectory() as temporary:
            record_thread = mock.Mock()
            with (
                mock.patch.object(meki_audio_capture, "work_dir", return_value=Path(temporary)),
                mock.patch.object(
                    meki_audio_capture.shutil,
                    "disk_usage",
                    return_value=types.SimpleNamespace(
                        free=meki_audio_capture.MIN_RECORDING_FREE_BYTES
                    ),
                ),
                mock.patch.object(meki_audio_capture.threading, "Thread", return_value=record_thread),
            ):
                controller._start_recording(1, session_options)

        record_thread.start.assert_called_once_with()
        self.assertEqual(controller.state, "RECORDING")
        self.assertIn("컴퓨터 소리를 녹음", controller.status)

    def test_live_start_skips_batch_wav_workspace_and_disk_gate(self) -> None:
        controller = self._controller()
        controller._session_generation = 1
        controller.state = "STARTING"
        session_options = (
            controller.stt_model,
            controller.precision,
            controller.preset,
            controller.script_url,
            controller.hytrans_url,
            True,
        )
        live_session = mock.Mock()
        record_thread = mock.Mock()

        with (
            mock.patch.object(
                controller,
                "_models_for_processing",
                return_value={"vad": Path("vad.onnx")},
            ),
            mock.patch.object(
                meki_audio_capture,
                "RealtimeTranslationSession",
                return_value=live_session,
            ),
            mock.patch.object(
                meki_audio_capture,
                "work_dir",
                side_effect=AssertionError("live mode must not allocate a batch workspace"),
            ) as work_dir,
            mock.patch.object(
                meki_audio_capture.shutil,
                "disk_usage",
                side_effect=AssertionError("live mode must not apply the batch disk gate"),
            ) as disk_usage,
            mock.patch.object(
                meki_audio_capture.threading,
                "Thread",
                return_value=record_thread,
            ) as new_thread,
        ):
            controller._start_recording(1, session_options)

        work_dir.assert_not_called()
        disk_usage.assert_not_called()
        live_session.start.assert_called_once_with()
        self.assertEqual(controller.session_work_dir, None)
        self.assertEqual(controller.wav_path, None)
        self.assertEqual(new_thread.call_args.kwargs["args"][2:], (None, None, live_session))
        record_thread.start.assert_called_once_with()

    def test_new_start_waits_for_an_aborted_live_session_to_exit(self) -> None:
        controller = self._controller()
        previous_session = mock.Mock()
        previous_session.has_active_workers.return_value = True
        controller.realtime_session = previous_session

        controller.start(realtime_translation=True)

        self.assertEqual(controller.state, "READY")
        self.assertIn("이전 실시간 번역 작업", controller.status)

    def test_record_stop_timeout_aborts_and_reaps_live_workers(self) -> None:
        controller = self._controller()
        controller._session_generation = 1
        controller.state = "STOPPING"
        record_thread = mock.Mock()
        record_thread.is_alive.return_value = True
        live_session = mock.Mock()
        session_options = (
            controller.stt_model,
            controller.precision,
            controller.preset,
            controller.script_url,
            controller.hytrans_url,
            True,
        )
        with mock.patch.object(controller, "_reap_unresponsive_recording") as reaper:
            controller._finish_and_process(
                generation=1,
                record_thread=record_thread,
                wav_path=Path("capture.wav"),
                session_work_dir=Path("session"),
                session_id="stalled-live-session",
                session_options=session_options,
                realtime_session=live_session,
            )

        record_thread.join.assert_called_once_with(timeout=5)
        live_session.abort.assert_called_once_with()
        reaper.assert_called_once_with(1, record_thread, Path("session"), live_session)
        self.assertEqual(controller.state, "ERROR")

    def test_stop_during_live_drain_cancels_remaining_translations(self) -> None:
        controller = self._controller()
        controller._session_generation = 1
        controller.state = "PROCESSING"
        live_session = mock.Mock()
        controller.realtime_session = live_session

        controller.stop()

        live_session.abort.assert_called_once_with()
        self.assertEqual(controller.state, "CANCELLING")
        self.assertIn("취소", controller.status)


class ModelTestWavTests(unittest.TestCase):
    def test_model_sample_wav_is_downmixed_and_downsampled_to_16k(self) -> None:
        raw_samples = np.array([0, 32767, -32768, 3000, -3000, 6000], dtype="<i2")
        with tempfile.TemporaryDirectory() as temporary:
            wav_path = Path(temporary) / "sample.wav"
            with wave.open(str(wav_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(48_000)
                output.writeframes(raw_samples.tobytes())
            samples = meki_audio_capture._load_model_test_wav(wav_path)

        expected = raw_samples.astype(np.float32).reshape(-1, 3).mean(axis=1) / 32768.0
        np.testing.assert_allclose(samples, expected)


class RealtimeTranslationSessionTests(unittest.TestCase):
    @staticmethod
    def _session() -> meki_audio_capture.RealtimeTranslationSession:
        return meki_audio_capture.RealtimeTranslationSession(
            {"vad": Path("vad.onnx")},
            "parakeet",
            "int8",
            "BALANCED",
            "http://script",
            "http://hytrans",
            "live-session",
            lambda _status: None,
        )

    @staticmethod
    def _segment(segment_id: int = 1) -> object:
        return meki_audio_capture.SpeechSegment(
            id=segment_id,
            start_time=0.0,
            end_time=0.032,
            duration=0.032,
            audio=np.zeros(512, dtype=np.float32),
            is_forced_cut=False,
            is_short=True,
            previous_overlap=0.0,
        )

    @staticmethod
    def _result(segment_id: int = 1) -> object:
        return meki_audio_capture.STTResult(
            segment_id=segment_id,
            start_time=0.0,
            end_time=0.032,
            duration=0.032,
            text_ja="こんにちは",
            is_forced_cut=False,
            is_short=True,
            stt_latency=0.01,
        )

    def test_translation_queue_has_a_bounded_live_backlog(self) -> None:
        session = self._session()

        self.assertEqual(meki_audio_capture.REALTIME_TRANSLATION_QUEUE_MAX_SEGMENTS, 2048)
        for index in range(32):
            session._enqueue_translation(self._result(index + 1), f"entry-{index + 1}")

        self.assertEqual(session._translation_queue.qsize(), 32)
        self.assertEqual(session._translation_failures, 0)
        self.assertEqual(session._max_translation_backlog, 32)

    def test_finalized_live_vad_chunk_is_appended_then_translated_and_flushed(self) -> None:
        class FakeVad:
            def __init__(self) -> None:
                self.accepted_windows: list[np.ndarray] = []
                self._items: list[object] = []
                self.flush_calls = 0
                self._emitted = False

            def accept_waveform(self, samples: np.ndarray) -> None:
                self.accepted_windows.append(np.array(samples, dtype=np.float32, copy=True))
                if not self._emitted:
                    self._emitted = True
                    self._items.append(
                        types.SimpleNamespace(
                            samples=np.full(640, 0.25, dtype=np.float32),
                            start=320,
                        )
                    )

            def flush(self) -> None:
                self.flush_calls += 1

            def empty(self) -> bool:
                return not self._items

            @property
            def front(self) -> object:
                return self._items[0]

            def pop(self) -> None:
                self._items.pop(0)

        class FakeStream:
            def __init__(self) -> None:
                self.result = types.SimpleNamespace(text="")
                self.accepted: tuple[int, np.ndarray] | None = None

            def accept_waveform(self, sample_rate: int, samples: np.ndarray) -> None:
                self.accepted = (sample_rate, np.array(samples, copy=True))

        class FakeRecognizer:
            def __init__(self) -> None:
                self.streams: list[FakeStream] = []

            def create_stream(self) -> FakeStream:
                stream = FakeStream()
                self.streams.append(stream)
                return stream

            @staticmethod
            def decode_stream(stream: FakeStream) -> None:
                stream.result.text = "こんにちは"

        fake_vad = FakeVad()
        fake_recognizer = FakeRecognizer()
        deliveries: list[tuple[str, str, str]] = []
        statuses: list[str] = []
        realtime_flags: list[bool] = []
        append_seen = threading.Event()

        def append(script_url: str, result: object, *, entry_id: str | None = None) -> None:
            deliveries.append(("append", str(entry_id), result.text_ja))
            append_seen.set()

        def translate(
            hytrans_url: str,
            text: str,
            timeout: float,
            *,
            realtime: bool = False,
        ) -> str:
            realtime_flags.append(realtime)
            deliveries.append(("translate", hytrans_url, text))
            return "안녕하세요"

        def set_translation(
            script_url: str,
            result: object,
            text: str,
            *,
            entry_id: str | None = None,
        ) -> None:
            deliveries.append(("translation", str(entry_id), text))

        session = meki_audio_capture.RealtimeTranslationSession(
            {"vad": Path("vad.onnx")},
            "parakeet",
            "int8",
            "BALANCED",
            "http://script",
            "http://hytrans",
            "live-session",
            statuses.append,
        )
        # Live padding waits through the native VAD's minimum speech/silence
        # finalization tail as well as the padded-boundary merge window. Feed
        # enough source PCM for BALANCED's bounded lookahead to expire before
        # checking that the utterance is published.
        capture_frames = 28 * 512 * (
            audio_capture_core.CAPTURE_SAMPLE_RATE // audio_capture_core.INTERNAL_SAMPLE_RATE
        )
        with (
            mock.patch.object(
                meki_audio_capture,
                "create_voice_activity_detector",
                return_value=fake_vad,
            ) as create_vad,
            mock.patch.object(
                meki_audio_capture,
                "create_recognizer",
                return_value=fake_recognizer,
            ) as create_recognizer,
            mock.patch.object(meki_audio_capture, "append_script_text", side_effect=append),
            mock.patch.object(meki_audio_capture, "translate_text", side_effect=translate),
            mock.patch.object(meki_audio_capture, "set_script_translation", side_effect=set_translation),
        ):
            session.start()
            session.accept_capture_block(np.full((capture_frames, 2), 0.25, dtype=np.float32))
            self.assertTrue(append_seen.wait(timeout=2), "VAD-finalized speech was not published live")
            session.finish_input()
            summary = session.wait_for_completion()

        self.assertEqual(create_vad.call_args.args, (Path("vad.onnx"), "BALANCED"))
        self.assertEqual(
            create_vad.call_args.kwargs,
            {"num_threads": meki_audio_capture.REALTIME_VAD_NUM_THREADS},
        )
        create_recognizer.assert_called_once_with(
            {"vad": Path("vad.onnx")},
            model_key="parakeet",
            precision="int8",
            num_threads=meki_audio_capture.REALTIME_STT_NUM_THREADS,
        )
        self.assertEqual([len(window) for window in fake_vad.accepted_windows], [512] * 28)
        self.assertEqual(fake_vad.flush_calls, 1)
        self.assertEqual(
            deliveries,
            [
                ("append", "live-session-1", "こんにちは"),
                ("translate", "http://hytrans", "こんにちは"),
                ("translation", "live-session-1", "안녕하세요"),
            ],
        )
        self.assertEqual(summary.recognized, 1)
        self.assertEqual(summary.translation_failures, 0)
        self.assertEqual(summary.delivery_failures, 0)
        self.assertFalse(session._stt_thread.is_alive())
        self.assertFalse(session._translation_thread.is_alive())
        self.assertEqual(realtime_flags, [True])
        self.assertTrue(any("번역 1개" in status for status in statuses))

    def test_live_vad_merges_intervals_when_their_padding_overlaps(self) -> None:
        class FakeVad:
            def __init__(self, items: list[object]) -> None:
                self._items = items

            def empty(self) -> bool:
                return not self._items

            @property
            def front(self) -> object:
                return self._items[0]

            def pop(self) -> None:
                self._items.pop(0)

        session = self._session()
        source = np.arange(20_000, dtype=np.float32)
        session._append_live_history(source)
        # The raw 0.25-second gap exceeds BALANCED's merge_gap (0.15 s), but
        # is covered by its 0.10 s pre- plus 0.20 s post-padding.  Batch and
        # live segmentation must therefore form one contiguous request.
        session._vad = FakeVad(
            [
                types.SimpleNamespace(samples=source[3_200:6_400], start=3_200),
                types.SimpleNamespace(samples=source[10_400:13_600], start=10_400),
            ]
        )

        session._drain_vad()
        session._flush_live_pending_segments(force=True)

        segment = session._stt_queue.get_nowait()
        self.assertTrue(session._stt_queue.empty())
        self.assertEqual((segment.start_time, segment.end_time), (0.1, 1.05))
        np.testing.assert_array_equal(segment.audio, source[1_600:16_800])

    def test_live_vad_waits_for_a_delayed_nearby_interval_before_flushing(self) -> None:
        class FakeVad:
            def __init__(self, items: list[object]) -> None:
                self._items = items

            def empty(self) -> bool:
                return not self._items

            @property
            def front(self) -> object:
                return self._items[0]

            def pop(self) -> None:
                self._items.pop(0)

        session = self._session()
        source = np.arange(25_600, dtype=np.float32)
        # The first interval is finalized after BALANCED's 0.30 s silence.
        # At that point the second, nearby interval has not been emitted by
        # native VAD yet. Its padded range overlaps the first, so batch and
        # live processing must still produce one STT request.
        session._append_live_history(source[:11_200])
        session._vad = FakeVad(
            [types.SimpleNamespace(samples=source[3_200:6_400], start=3_200)]
        )
        session._drain_vad()
        session._flush_live_pending_segments()
        self.assertTrue(session._stt_queue.empty())

        session._append_live_history(source[11_200:16_000])
        session._vad = FakeVad(
            [types.SimpleNamespace(samples=source[8_000:11_200], start=8_000)]
        )
        session._drain_vad()
        session._append_live_history(source[16_000:])
        session._flush_live_pending_segments()

        segment = session._stt_queue.get_nowait()
        self.assertTrue(session._stt_queue.empty())
        self.assertEqual((segment.start_time, segment.end_time), (0.1, 0.9))
        np.testing.assert_array_equal(segment.audio, source[1_600:14_400])

    def test_live_vad_applies_padding_and_forced_cut_overlap(self) -> None:
        class FakeVad:
            def __init__(self, item: object) -> None:
                self._items = [item]

            def empty(self) -> bool:
                return not self._items

            @property
            def front(self) -> object:
                return self._items[0]

            def pop(self) -> None:
                self._items.pop(0)

        session = meki_audio_capture.RealtimeTranslationSession(
            {"vad": Path("vad.onnx")},
            "parakeet",
            "int8",
            "FAST",
            "http://script",
            "http://hytrans",
            "live-session",
            lambda _status: None,
        )
        # FAST: 0.05 s pre-padding, 0.10 s post-padding, a 10-second
        # maximum, and a 0.25-second forced-cut overlap.
        source = np.arange(165_600, dtype=np.float32)
        session._append_live_history(source)
        session._vad = FakeVad(
            types.SimpleNamespace(samples=source[3_200:163_200], start=3_200)
        )

        session._drain_vad()
        session._flush_live_pending_segments(force=True)

        first = session._stt_queue.get_nowait()
        second = session._stt_queue.get_nowait()
        self.assertTrue(session._stt_queue.empty())
        self.assertEqual((first.start_time, first.end_time), (0.15, 10.15))
        self.assertEqual((second.start_time, second.end_time), (9.9, 10.3))
        self.assertTrue(first.is_forced_cut)
        self.assertTrue(second.is_forced_cut)
        self.assertEqual(second.previous_overlap, 0.25)
        np.testing.assert_array_equal(first.audio, source[2_400:162_400])
        np.testing.assert_array_equal(second.audio, source[158_400:164_800])

    def test_failed_translator_with_full_queue_cannot_deadlock_stt_shutdown(self) -> None:
        session = self._session()
        # Simulate a translator that already died after leaving its bounded
        # queue full.  The old terminal-item design blocked forever here.
        session._started = True
        session._translation_thread = threading.Thread(target=lambda: None, daemon=True)
        session._translation_thread.start()
        session._translation_thread.join(timeout=1)
        session._translation_queue = meki_audio_capture.queue.Queue(maxsize=1)
        session._translation_queue.put_nowait((self._result(), "entry-1"))
        session._stt_input_closed.set()
        session._stt_thread = threading.Thread(target=session._run_stt, daemon=True)

        with mock.patch.object(meki_audio_capture, "create_recognizer", return_value=object()):
            started = time.monotonic()
            session._stt_thread.start()
            summary = session.wait_for_completion()
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertFalse(session._stt_thread.is_alive())
        self.assertFalse(session._translation_thread.is_alive())
        self.assertEqual(summary.recognized, 0)

    def test_abort_after_decode_suppresses_late_script_publish(self) -> None:
        class FakeStream:
            def __init__(self) -> None:
                self.result = types.SimpleNamespace(text="")

            def accept_waveform(self, _sample_rate: int, _samples: np.ndarray) -> None:
                return

        decode_entered = threading.Event()
        release_decode = threading.Event()

        class FakeRecognizer:
            def create_stream(self) -> FakeStream:
                return FakeStream()

            @staticmethod
            def decode_stream(stream: FakeStream) -> None:
                decode_entered.set()
                release_decode.wait(timeout=2)
                stream.result.text = "こんにちは"

        session = self._session()
        session._started = True
        session._stt_thread = threading.Thread(target=session._run_stt, daemon=True)
        session._translation_thread = threading.Thread(target=session._run_translation, daemon=True)
        with (
            mock.patch.object(meki_audio_capture, "create_recognizer", return_value=FakeRecognizer()),
            mock.patch.object(meki_audio_capture, "append_script_text") as append_script_text,
            mock.patch.object(meki_audio_capture, "set_script_translation") as set_script_translation,
        ):
            session._translation_thread.start()
            session._stt_thread.start()
            session._stt_queue.put_nowait(self._segment())
            try:
                self.assertTrue(decode_entered.wait(timeout=1))
                session.abort()
                release_decode.set()
                session.wait_for_completion()
            finally:
                release_decode.set()
                session.abort()
                session.wait_for_completion()

        append_script_text.assert_not_called()
        set_script_translation.assert_not_called()

    def test_live_translation_has_no_pre_stop_aggregate_deadline(self) -> None:
        session = self._session()
        session._started = True
        session._translation_thread = threading.Thread(target=session._run_translation, daemon=True)
        translated = threading.Event()

        def translate(*_args: object, **_kwargs: object) -> str:
            translated.set()
            return "안녕하세요"

        with (
            mock.patch.object(meki_audio_capture, "translate_text", side_effect=translate) as translate_text,
            mock.patch.object(meki_audio_capture, "set_script_translation"),
        ):
            session._translation_thread.start()
            session._translation_queue.put_nowait((self._result(), "entry-1"))
            self.assertTrue(translated.wait(timeout=1))
            self.assertFalse(hasattr(session, "_translation_deadline"))
            session._translation_input_closed.set()
            session.wait_for_completion()

        translate_text.assert_called_once()

    def test_stopped_live_session_drains_after_the_legacy_deadline(self) -> None:
        session = self._session()
        session._started = True
        # A former live session set this value during Stop and skipped every
        # remaining item after 30 minutes. The live path must now drain a
        # responsive translator instead.
        session._translation_deadline = time.monotonic() - 1
        delivered: list[str] = []

        def translate(*_args: object, **kwargs: object) -> str:
            self.assertTrue(kwargs.get("realtime"))
            return "안녕하세요"

        def set_translation(
            _script_url: str,
            _result: object,
            text: str,
            *,
            entry_id: str | None = None,
        ) -> None:
            delivered.append(f"{entry_id}:{text}")

        with (
            mock.patch.object(meki_audio_capture, "translate_text", side_effect=translate),
            mock.patch.object(
                meki_audio_capture,
                "set_script_translation",
                side_effect=set_translation,
            ),
        ):
            session._translation_queue.put_nowait((self._result(), "entry-1"))
            session._translation_input_closed.set()
            session._translation_thread.start()
            summary = session.wait_for_completion()

        self.assertEqual(delivered, ["entry-1:안녕하세요"])
        self.assertEqual(summary.translation_failures, 0)

    def test_repeated_translation_failures_skip_remaining_items_and_finish(self) -> None:
        session = self._session()
        session._started = True
        deliveries: list[tuple[str, str]] = []

        def set_translation(
            _script_url: str,
            _result: object,
            text: str,
            *,
            entry_id: str | None = None,
        ) -> None:
            deliveries.append((str(entry_id), text))

        with (
            mock.patch.object(
                meki_audio_capture,
                "translate_text",
                side_effect=RuntimeError("HYTrans unavailable"),
            ) as translate_text,
            mock.patch.object(
                meki_audio_capture,
                "set_script_translation",
                side_effect=set_translation,
            ),
        ):
            for index in range(3):
                session._translation_queue.put_nowait((self._result(index + 1), f"entry-{index + 1}"))
            session._translation_input_closed.set()
            session._translation_thread.start()
            summary = session.wait_for_completion()

        self.assertEqual(translate_text.call_count, 2)
        self.assertEqual(
            deliveries,
            [
                ("entry-1", "[번역 실패] HYTrans unavailable"),
                ("entry-2", "[번역 실패] HYTrans unavailable"),
                (
                    "entry-3",
                    "[번역 건너뜀] HYTrans가 연속으로 응답하지 않아 나머지 요청을 중단했습니다.",
                ),
            ],
        )
        self.assertEqual(summary.translation_failures, 3)
        self.assertFalse(session._translation_thread.is_alive())

    def test_live_finish_waits_for_session_without_entering_batch_pipeline(self) -> None:
        controller = CaptureControllerTests._controller()
        controller._session_generation = 1
        controller.state = "STOPPING"
        live_session = mock.Mock()
        live_session.wait_for_completion.return_value = meki_audio_capture.RealtimeTranslationSummary(
            recognized=2,
            delivery_failures=0,
            translation_failures=0,
            dropped_segments=0,
        )
        controller.realtime_session = live_session
        session_options = (
            controller.stt_model,
            controller.precision,
            controller.preset,
            controller.script_url,
            controller.hytrans_url,
            True,
        )

        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary) / "session"
            session_dir.mkdir()
            with (
                mock.patch.object(
                    controller,
                    "_models_for_processing",
                    side_effect=AssertionError("live finish must not load batch models"),
                ) as models_for_processing,
                mock.patch.object(
                    meki_audio_capture,
                    "wav_to_mono_16k",
                    side_effect=AssertionError("live finish must not convert recorded WAV"),
                ) as wav_to_mono_16k,
                mock.patch.object(
                    meki_audio_capture,
                    "collect_vad_intervals",
                    side_effect=AssertionError("live finish must not run batch VAD"),
                ) as collect_intervals,
                mock.patch.object(
                    meki_audio_capture,
                    "build_segments",
                    side_effect=AssertionError("live finish must not rebuild batch segments"),
                ) as build_segments,
                mock.patch.object(
                    meki_audio_capture,
                    "create_recognizer",
                    side_effect=AssertionError("live finish must not create a batch recognizer"),
                ) as create_recognizer,
                mock.patch.object(
                    meki_audio_capture,
                    "recognize_segments",
                    side_effect=AssertionError("live finish must not run batch recognition"),
                ) as recognize_segments,
            ):
                controller._finish_and_process(
                    generation=1,
                    record_thread=None,
                    wav_path=session_dir / "capture.wav",
                    session_work_dir=session_dir,
                    session_id="live-session",
                    session_options=session_options,
                    realtime_session=live_session,
                )

        live_session.wait_for_completion.assert_called_once_with()
        for batch_mock in (
            models_for_processing,
            wav_to_mono_16k,
            collect_intervals,
            build_segments,
            create_recognizer,
            recognize_segments,
        ):
            batch_mock.assert_not_called()
        self.assertEqual(controller.state, "READY")
        self.assertIn("실시간으로 일본어 2개", controller.status)
        self.assertIsNone(controller.realtime_session)


class RealtimeTranslationConfigTests(unittest.TestCase):
    def test_config_endpoint_rejects_realtime_translation_setting(self) -> None:
        controller = CaptureControllerTests._controller()
        before = controller.health()
        server = meki_audio_capture.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            meki_audio_capture.make_handler(controller),
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request(
                "POST",
                "/config",
                body=json.dumps({"realtimeTranslation": True}).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertEqual(response.status, 409)
        self.assertFalse(payload["ok"])
        self.assertIn("MekiAudioCapture 창의 체크박스", payload["error"])
        self.assertEqual(controller.health(), before)


if __name__ == "__main__":
    unittest.main()
