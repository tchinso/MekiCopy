from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import meki_audio_capture as capture
from meki_subtitle_vad import segments_from_probabilities


class FakeProbabilityModel:
    def __init__(self, probabilities: list[float]) -> None:
        self._probabilities = iter(probabilities)
        self.windows: list[np.ndarray] = []

    def probability(self, frame: np.ndarray) -> float:
        self.windows.append(np.array(frame, dtype=np.float32, copy=True))
        return next(self._probabilities)


class ProbabilityRealtimeVadTests(unittest.TestCase):
    def session(self, probabilities: list[float], preset: str = "FAST"):
        session = capture.RealtimeTranslationSession(
            {"vad": Path("vad.onnx")}, "parakeet", "int8", preset,
            "http://script", "http://hytrans", "fast-test", lambda _status: None,
        )
        # Keep these tests on the recorder-side boundary/queue path. Separate
        # tests cover STT and HTTP workers; neither should consume this queue.
        session._stt_thread = mock.Mock(spec=threading.Thread)
        session._translation_thread = mock.Mock(spec=threading.Thread)
        model = FakeProbabilityModel(probabilities)
        with mock.patch.object(capture, "SileroProbabilityModel", return_value=model) as create:
            session.start()
        create.assert_called_once_with(Path("vad.onnx"), num_threads=capture.REALTIME_VAD_NUM_THREADS)
        return session, model

    @staticmethod
    def drain(session):
        return [session._stt_queue.get_nowait() for _ in range(session._stt_queue.qsize())]

    def test_streaming_matches_batch_through_stereo_resampling_and_partial_eof(self) -> None:
        probabilities = ([0.] * 50 + [.06] * 2 + [0.] * 30 + [.9] * 160
                         + [.06] * 4 + [.9] * 250 + [0.] * 12
                         + [.06] * 2 + [0.] * 58 + [.9] * 5)
        source = np.arange(len(probabilities) * 512 - 137, dtype=np.float32)
        for preset in ("FAST", "BALANCED", "LONG"):
            with self.subTest(preset=preset):
                expected = segments_from_probabilities(source, probabilities, preset_name=preset)
                session, _model = self.session(probabilities, preset)
                stereo_48k = np.repeat(source, 3).repeat(2).reshape(-1, 2)
                position = 0
                sizes = (1, 119, 1537, 70_000, 2049)
                index = 0
                while position < len(stereo_48k):
                    end = min(len(stereo_48k), position + sizes[index % len(sizes)])
                    session.accept_capture_block(stereo_48k[position:end])
                    position = end
                    index += 1
                session.finish_input()
                actual = self.drain(session)
                self.assertEqual(len(actual), len(expected))
                for live, batch in zip(actual, expected, strict=True):
                    self.assertEqual(
                        (live.start_time, live.end_time, live.previous_overlap, live.is_forced_cut),
                        (batch.start_time, batch.end_time, batch.previous_overlap, batch.is_forced_cut),
                    )
                    np.testing.assert_array_equal(live.audio, batch.audio)
                self.assertTrue(session._stt_input_closed.is_set())
                self.assertEqual(session._live_source_end, len(source))

    def test_weak_onset_joins_clear_speech_and_emits_without_native_merge_wait(self) -> None:
        probabilities = [0.] * 50 + [.06] + [0.] * 20 + [.9] * 4 + [0.] * 7
        source = np.arange(len(probabilities) * 512, dtype=np.float32)
        session, _model = self.session(probabilities)
        session._feed_vad(source)
        # Seven silent frames close clear speech immediately. The original
        # weak onset remains in this request with its longer isolated padding.
        self.assertEqual(session._stt_queue.qsize(), 1)
        segment = session._stt_queue.get_nowait()
        self.assertEqual(segment.start_time, (50 * 512 - 19_456) / 16_000)
        self.assertEqual(segment.end_time, len(source) / 16_000)
        session.finish_input()
        self.assertTrue(session._stt_queue.empty())

    def test_live_history_is_bounded_and_queued_audio_survives_ring_reuse(self) -> None:
        probabilities = ([.9] * 10 + [0.] * 24) * 200
        for preset in ("FAST", "BALANCED", "LONG"):
            with self.subTest(preset=preset):
                session, _model = self.session(probabilities, preset)
                emitted = []
                session._enqueue_segment = emitted.append
                capacity = session._live_history_capacity
                source = np.arange(len(probabilities) * 512, dtype=np.float32)
                session._feed_vad(source)
                session.finish_input()
                self.assertEqual(len(emitted), 200)
                self.assertEqual(session._live_history.size, capacity)
                self.assertGreater(len(source), capacity * 10)
                self.assertLessEqual(len(session._vad_tracker.frames), capacity // 512 + 1)
                first = emitted[0]
                expected = source[round(first.start_time * 16_000):round(first.end_time * 16_000)].copy()
                self.assertFalse(np.shares_memory(first.audio, session._live_history))
                self.assertFalse(np.shares_memory(first.audio, source))
                session._live_history.fill(-1)
                source.fill(-2)
                np.testing.assert_array_equal(first.audio, expected)

    def test_final_partial_frame_does_not_extend_audio_or_repeat_cut_context(self) -> None:
        # The padded model frame crosses the 6-second forced-cut boundary;
        # its output must still stop at the exact real sample count.
        source = np.arange(95_900, dtype=np.float32)
        probabilities = [.9] * ((len(source) + 511) // 512)
        session, model = self.session(probabilities)
        session._feed_vad(source)
        session.finish_input()
        actual = self.drain(session)
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0].end_time, len(source) / 16_000)
        np.testing.assert_array_equal(actual[0].audio, source)
        np.testing.assert_array_equal(model.windows[-1][len(source) % 512:], 0.)
        session.finish_input()
        self.assertTrue(session._stt_queue.empty())

    def test_subminimum_noise_expires_before_later_valid_speech(self) -> None:
        for preset in ("BALANCED", "LONG"):
            with self.subTest(preset=preset):
                probabilities = [0.] * 10 + [.9] + [0.] * 100 + [.9] * 10 + [0.] * 24
                source = np.arange(len(probabilities) * 512, dtype=np.float32)
                session, _model = self.session(probabilities, preset)
                session._feed_vad(source)
                session.finish_input()
                actual = self.drain(session)
                self.assertEqual(len(actual), 1)
                self.assertGreater(actual[0].start_time, 3.0)
                self.assertLessEqual(actual[0].end_time, len(source) / 16_000)


class ProbabilityBatchRoutingTests(unittest.TestCase):
    def test_stopped_recordings_use_shared_final_segment_path_for_selected_preset(self) -> None:
        for preset in ("FAST", "BALANCED", "LONG"):
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as temporary:
                controller = capture.CaptureController(
                    "int8", preset, "http://script", "http://hytrans",
                    prepare_models_on_start=False,
                )
                root = Path(temporary)
                controller.state = "STOPPING"
                controller.wav_path = root / "capture.wav"
                controller.session_work_dir = root
                audio = np.zeros(512, dtype=np.float32)
                models = {"vad": Path("vad.onnx")}
                with (
                    mock.patch.object(capture, "wav_to_mono_16k", return_value=audio),
                    mock.patch.object(controller, "_models_for_processing", return_value=models),
                    mock.patch.object(capture, "collect_subtitle_segments", return_value=(0, [])) as collect,
                    mock.patch.object(capture, "create_recognizer") as recognizer,
                ):
                    controller._finish_and_process()
                collect.assert_called_once()
                self.assertIs(collect.call_args.args[0], audio)
                self.assertEqual(collect.call_args.args[1], models["vad"])
                self.assertEqual(collect.call_args.kwargs, {"preset_name": preset})
                recognizer.assert_not_called()
                self.assertEqual(controller.state, "READY")
                self.assertEqual(controller.status, "완료: 인식할 음성이 없습니다.")


if __name__ == "__main__":
    unittest.main()
