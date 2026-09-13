from __future__ import annotations

import tempfile
import sys
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
                "merge_short_under": 0.60,
                "forced_cut_overlap": 0.25,
            },
            "BALANCED": {
                "threshold": 0.55,
                "min_speech_duration": 0.225,
                "min_silence_duration": 0.525,
                "max_segment_duration": 18.5,
                "pre_padding": 0.15,
                "post_padding": 0.325,
                "merge_gap": 0.30,
                "merge_short_under": 1.05,
                "forced_cut_overlap": 0.425,
            },
            "LONG": {
                "threshold": 0.50,
                "min_speech_duration": 0.25,
                "min_silence_duration": 0.95,
                "max_segment_duration": 27.0,
                "pre_padding": 0.25,
                "post_padding": 0.55,
                "merge_gap": 0.55,
                "merge_short_under": 1.50,
                "forced_cut_overlap": 0.60,
            },
        }

        for preset_name, values in expected.items():
            with self.subTest(preset=preset_name):
                self.assertEqual(VAD_PRESETS[preset_name], values)

    def test_balanced_is_the_midpoint_of_fast_and_long(self) -> None:
        for key, fast_value in VAD_PRESETS["FAST"].items():
            with self.subTest(setting=key):
                self.assertAlmostEqual(
                    VAD_PRESETS["BALANCED"][key],
                    (fast_value + VAD_PRESETS["LONG"][key]) / 2,
                )

    def test_fast_keeps_a_short_dialogue_boundary(self) -> None:
        audio = np.zeros(32_000, dtype=np.float32)
        # A 0.16-second turn gap survives FAST's short padding but used to be
        # rejoined by the former 0.50-second combined padding.
        intervals = [(3_200, 6_400), (8_960, 12_160)]
        segments = build_segments(audio, intervals, "FAST")
        self.assertEqual(len(segments), 2)
        self.assertLess(segments[0].end_time, segments[1].start_time)


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


if __name__ == "__main__":
    unittest.main()
