from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import numpy as np

from meki_subtitle_vad import (
    FRAME_SIZE,
    SAMPLE_RATE,
    SUBTITLE_VAD_SETTINGS,
    VadBoundaryTracker,
    collect_subtitle_segments,
    segments_from_probabilities,
    settings_for_fast,
    settings_for_preset,
)


class SubtitleVadTests(unittest.TestCase):
    @staticmethod
    def segment(*runs: tuple[int, float], trim: int = 0):
        probabilities = [probability for count, probability in runs for _ in range(count)]
        audio = np.arange(len(probabilities) * FRAME_SIZE - trim, dtype=np.float32)
        return audio, segments_from_probabilities(audio, probabilities)

    def test_fast_settings_are_frozen_snapshots_of_the_global_preset(self):
        with self.assertRaises(FrozenInstanceError):
            SUBTITLE_VAD_SETTINGS.speech_threshold = 0.6
        from audio_capture_core import VAD_PRESETS

        snapshot = settings_for_fast()
        self.assertEqual(snapshot, SUBTITLE_VAD_SETTINGS)
        with mock.patch.dict(VAD_PRESETS["FAST"], {"threshold": 1.0}):
            self.assertEqual(settings_for_fast().speech_threshold, 1.0)
            _, segments = self.segment((1, 0.9), (7, 0.0))
        self.assertEqual(segments, [])
        self.assertEqual(snapshot.speech_threshold, 0.04)

    def test_boundary_tracker_holds_no_pcm_or_accumulated_results(self):
        tracker = VadBoundaryTracker(SUBTITLE_VAD_SETTINGS)
        emitted = 0
        largest_history = 0
        for _ in range(5000):
            emitted += len(tracker.push(0.9))
            largest_history = max(largest_history, len(tracker.frames))
        self.assertGreater(emitted, 1)
        self.assertLessEqual(largest_history, 189)
        self.assertFalse(any(isinstance(value, np.ndarray) for value in vars(tracker).values()))
        self.assertNotIn("segments", vars(tracker))
        self.assertNotIn("audio", vars(tracker))

    def test_boundary_tracker_returns_only_new_outputs_and_clips_eof(self):
        tracker = VadBoundaryTracker(SUBTITLE_VAD_SETTINGS)
        self.assertEqual(tracker.push(0.9), [])
        self.assertEqual(tracker.push(0.9), [])
        boundaries = tracker.finish(600)
        self.assertEqual(len(boundaries), 1)
        self.assertEqual((boundaries[0].start, boundaries[0].end), (0, 600))
        self.assertFalse(boundaries[0].forced)
        self.assertEqual(boundaries[0].previous_overlap, 0)
        self.assertEqual(tracker.finish(600), [])

    def test_every_preset_reads_all_global_thresholds_and_sample_durations(self):
        from audio_capture_core import VAD_PRESETS

        for name in ("FAST", "BALANCED", "LONG"):
            with self.subTest(preset=name):
                settings = settings_for_preset(name)
                preset = VAD_PRESETS[name]
                self.assertEqual(settings.speech_threshold, preset["threshold"])
                self.assertEqual(settings.release_threshold, preset["release_threshold"])
                self.assertEqual(settings.weak_speech_threshold, preset["weak_speech_threshold"])
                self.assertEqual(settings.clear_speech_threshold, preset["clear_speech_threshold"])
                for field, key in (
                    ("min_speech_samples", "min_speech_duration"),
                    ("end_silence_samples", "min_silence_duration"),
                    ("weak_end_silence_samples", "weak_min_silence_duration"),
                    ("pre_padding_samples", "pre_padding"),
                    ("weak_pre_padding_samples", "weak_pre_padding"),
                    ("post_padding_samples", "post_padding"),
                    ("max_chunk_samples", "max_segment_duration"),
                    ("cut_lookback_samples", "cut_lookback"),
                    ("cut_pause_samples", "cut_pause"),
                    ("overlap_samples", "forced_cut_overlap"),
                ):
                    self.assertEqual(getattr(settings, field), round(preset[key] * SAMPLE_RATE))

    def test_subminimum_candidates_expire_with_bounded_history(self):
        for name, voiced_probability, quiet_frames in (("BALANCED", 0.30, 47), ("LONG", 0.6, 19)):
            with self.subTest(preset=name):
                tracker = VadBoundaryTracker(settings_for_preset(name))
                largest_history = 0
                for _ in range(100):
                    self.assertEqual(tracker.push(voiced_probability), [])
                    for _ in range(quiet_frames):
                        self.assertEqual(tracker.push(0.0), [])
                        largest_history = max(largest_history, len(tracker.frames))
                    self.assertIsNone(tracker.start)
                self.assertLess(largest_history, 100)
                self.assertEqual(tracker.finish(tracker.position), [])

    def test_long_uses_fixed_limit_without_pause_search_or_padding_merge(self):
        probabilities = [0.9] * 630 + [0.0] * 19
        audio = np.arange(len(probabilities) * FRAME_SIZE, dtype=np.float32)
        segments = segments_from_probabilities(audio, probabilities, preset_name="LONG")
        self.assertEqual(len(segments), 2)
        self.assertEqual(len(segments[0].audio), 20 * SAMPLE_RATE)
        self.assertEqual(segments[1].previous_overlap, 1.5)
        np.testing.assert_array_equal(segments[1].audio[:24000], segments[0].audio[-24000:])

    def test_one_weak_frame_at_threshold_survives_with_padding(self):
        audio, segments = self.segment((20, 0.039), (1, 0.04), (58, 0.039))
        self.assertEqual(len(segments), 1)
        segment = segments[0]
        self.assertEqual(segment.start_time * SAMPLE_RATE, 20 * FRAME_SIZE - 3584)
        self.assertEqual(len(segment.audio), 3584 + FRAME_SIZE + 3584)
        self.assertTrue(np.shares_memory(segment.audio, audio))
        self.assertFalse(segment.is_forced_cut)

    def test_clear_speech_keeps_short_gaps_and_weak_tails(self):
        _, segments = self.segment((1, 0.9), (6, 0.009), (1, 0.9), (7, 0.009))
        self.assertEqual(len(segments), 1)
        self.assertEqual(len(segments[0].audio), 15 * FRAME_SIZE)
        _, weak_tail = self.segment((1, 0.9), (7, 0.044))
        self.assertEqual(len(weak_tail), 1)
        self.assertEqual(len(weak_tail[0].audio), 8 * FRAME_SIZE)

    def test_weak_onset_joins_clear_speech_before_shorter_release(self):
        _, segments = self.segment((1, 0.06), (56, 0.009), (1, 0.15), (7, 0.009))
        self.assertEqual(len(segments), 1)
        self.assertEqual(len(segments[0].audio), 65 * FRAME_SIZE)

    def test_isolated_weak_onset_keeps_long_pre_padding(self):
        _, segments = self.segment((1, 0.9), (7, 0.009), (38, 0.009), (1, 0.06), (58, 0.009))
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[1].start_time, segments[0].end_time)
        self.assertEqual(len(segments[1].audio), 46 * FRAME_SIZE)

    def test_forced_cut_keeps_half_second_of_actual_overlap(self):
        _, segments = self.segment((188, 0.9), (7, 0.009))
        self.assertEqual(len(segments), 2)
        first, second = segments
        self.assertEqual(len(first.audio), 96000)
        self.assertTrue(first.is_forced_cut)
        self.assertTrue(second.is_forced_cut)
        self.assertEqual(first.previous_overlap, 0)
        self.assertEqual(second.previous_overlap, 0.5)
        np.testing.assert_array_equal(second.audio[:8000], first.audio[-8000:])

    def test_latest_completed_pause_sets_the_forced_cut(self):
        _, segments = self.segment(
            (160, 0.9), (3, 0.07), (9, 0.9), (3, 0.06),
            (4, 0.9), (2, 0.07), (7, 0.9), (7, 0.009),
        )
        self.assertEqual(len(segments), 2)
        self.assertEqual(len(segments[0].audio), (172 * FRAME_SIZE + 768))
        np.testing.assert_array_equal(segments[1].audio[:8000], segments[0].audio[-8000:])

    def test_eof_flush_clips_partial_frame_and_preserves_memmap_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            audio = np.memmap(Path(temporary) / "audio.f32", dtype=np.float32, mode="w+", shape=(600,))
            audio[:] = np.arange(600, dtype=np.float32)
            segments = segments_from_probabilities(audio, [0.9, 0.9])
            self.assertEqual(len(segments), 1)
            self.assertEqual(segments[0].end_time, 600 / SAMPLE_RATE)
            self.assertEqual(len(segments[0].audio), 600)
            self.assertTrue(np.shares_memory(segments[0].audio, audio))
            del segments
            audio._mmap.close()

    def test_overlap_only_tail_is_discarded_at_eof_and_on_silence(self):
        for silence in (9, 58):
            _, segments = self.segment((179, 0.06), (silence, 0.009))
            self.assertEqual(len(segments), 1)
            self.assertTrue(segments[0].is_forced_cut)

    def test_silence_and_empty_audio_produce_no_segments(self):
        _, segments = self.segment((100, 0.039))
        self.assertEqual(segments, [])
        self.assertEqual(segments_from_probabilities(np.empty(0, dtype=np.float32), []), [])

    def test_wrong_probability_count_is_rejected(self):
        for probabilities in ([], [0.0, 0.0]):
            with self.assertRaises(ValueError):
                segments_from_probabilities(np.zeros(FRAME_SIZE, dtype=np.float32), probabilities)

    def test_inference_preserves_recurrent_state_and_zero_pads_last_frame(self):
        inputs = [SimpleNamespace(name=name, shape=shape) for name, shape in
                  (("x", [1, 512]), ("h", [2, 1, 64]), ("c", [2, 1, 64]))]
        session = mock.Mock()
        session.get_inputs.return_value = inputs
        feeds = []

        def run(_outputs, feed):
            feeds.append(feed)
            return np.asarray([[0.9]], dtype=np.float32), feed["h"] + 1, feed["c"] + 2

        session.run.side_effect = run
        ort = SimpleNamespace(SessionOptions=mock.Mock(), InferenceSession=mock.Mock(return_value=session))
        progress = mock.Mock()
        with mock.patch.dict("sys.modules", {"onnxruntime": ort}):
            count, segments = collect_subtitle_segments(
                np.ones(600, dtype=np.float32), Path("shared-vad.onnx"), progress=progress,
            )
        self.assertEqual(count, 1)
        self.assertEqual(len(segments[0].audio), 600)
        np.testing.assert_array_equal(feeds[0]["h"], np.zeros((2, 1, 64), dtype=np.float32))
        np.testing.assert_array_equal(feeds[1]["h"], np.ones((2, 1, 64), dtype=np.float32))
        np.testing.assert_array_equal(feeds[1]["c"], np.full((2, 1, 64), 2, dtype=np.float32))
        np.testing.assert_array_equal(feeds[1]["x"][0, :88], np.ones(88, dtype=np.float32))
        np.testing.assert_array_equal(feeds[1]["x"][0, 88:], np.zeros(424, dtype=np.float32))
        self.assertEqual(set(feeds[0]), {"x", "h", "c"})
        progress.assert_called_with(1.0)
        self.assertEqual(ort.InferenceSession.call_args.kwargs["providers"], ["CPUExecutionProvider"])

    def test_cancellation_stops_before_creating_a_model_session(self):
        cancelled = RuntimeError("cancelled")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            collect_subtitle_segments(
                np.ones(512, dtype=np.float32), Path("missing.onnx"),
                check_cancel=mock.Mock(side_effect=cancelled),
            )


if __name__ == "__main__":
    unittest.main()
