from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

import meki_audio_capture
from audio_capture_core import validate_tokens_file


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


if __name__ == "__main__":
    unittest.main()
