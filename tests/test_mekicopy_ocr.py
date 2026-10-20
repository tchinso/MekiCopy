from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import mekicopy_ocr
from mekicopy_settings import AppSettings


class _Session:
    def __init__(self, providers: list[str]) -> None:
        self._providers = providers

    def get_providers(self) -> list[str]:
        return self._providers


def _engine(active_provider: str, session_provider: str) -> SimpleNamespace:
    session = _Session([session_provider])
    return SimpleNamespace(
        active_provider=active_provider,
        det_session=session,
        rec_session=session,
        vrec_session=session,
    )


class MeikiOcrGpuSelectionTests(unittest.TestCase):
    def test_cuda_is_attempted_when_onnxruntime_advertises_it(self) -> None:
        factory = mock.Mock(
            return_value=_engine("CUDAExecutionProvider", "CUDAExecutionProvider")
        )
        fake_meikiocr = SimpleNamespace(MeikiOCR=factory)

        with (
            mock.patch.object(
                mekicopy_ocr,
                "_get_available_ort_providers",
                return_value=["CUDAExecutionProvider", "CPUExecutionProvider"],
            ),
            mock.patch.object(mekicopy_ocr, "_log_runtime_error"),
        ):
            engine = mekicopy_ocr._create_best_meikiocr_engine(fake_meikiocr)

        self.assertEqual(engine.active_provider, "CUDAExecutionProvider")
        factory.assert_called_once_with(provider="CUDAExecutionProvider")

    def test_complete_cuda_fallback_reuses_the_first_cpu_engine(self) -> None:
        def create(provider: str) -> SimpleNamespace:
            active = "CPUExecutionProvider"
            return _engine(active, active)

        factory = mock.Mock(side_effect=create)
        fake_meikiocr = SimpleNamespace(MeikiOCR=factory)

        with (
            mock.patch.object(
                mekicopy_ocr,
                "_get_available_ort_providers",
                return_value=["CUDAExecutionProvider", "CPUExecutionProvider"],
            ),
            mock.patch.object(mekicopy_ocr, "_log_runtime_error"),
        ):
            engine = mekicopy_ocr._create_best_meikiocr_engine(fake_meikiocr)

        self.assertEqual(engine.active_provider, "CPUExecutionProvider")
        factory.assert_called_once_with(provider="CUDAExecutionProvider")

    def test_partial_cuda_fallback_recreates_a_clean_cpu_engine(self) -> None:
        partially_accelerated = SimpleNamespace(
            active_provider="CUDAExecutionProvider",
            det_session=_Session(["CUDAExecutionProvider"]),
            rec_session=_Session(["CPUExecutionProvider"]),
            vrec_session=_Session(["CUDAExecutionProvider"]),
        )
        clean_cpu_engine = _engine("CPUExecutionProvider", "CPUExecutionProvider")
        factory = mock.Mock(side_effect=[partially_accelerated, clean_cpu_engine])
        fake_meikiocr = SimpleNamespace(MeikiOCR=factory)

        with (
            mock.patch.object(
                mekicopy_ocr,
                "_get_available_ort_providers",
                return_value=["CUDAExecutionProvider", "CPUExecutionProvider"],
            ),
            mock.patch.object(mekicopy_ocr, "_log_runtime_error"),
        ):
            engine = mekicopy_ocr._create_best_meikiocr_engine(fake_meikiocr)

        self.assertIs(engine, clean_cpu_engine)
        self.assertEqual(
            factory.call_args_list,
            [
                mock.call(provider="CUDAExecutionProvider"),
                mock.call(provider="CPUExecutionProvider"),
            ],
        )

    def test_ort_gpu_preloader_runs_without_path_based_dll_gate(self) -> None:
        fake_ort = SimpleNamespace(preload_dlls=mock.Mock())
        original_preload_ready = mekicopy_ocr._ORT_PRELOAD_READY
        try:
            mekicopy_ocr._ORT_PRELOAD_READY = False
            with mock.patch.dict(sys.modules, {"onnxruntime": fake_ort}):
                mekicopy_ocr._preload_onnxruntime_gpu_dlls()
        finally:
            mekicopy_ocr._ORT_PRELOAD_READY = original_preload_ready

        fake_ort.preload_dlls.assert_called_once_with(
            cuda=True,
            cudnn=True,
            msvc=True,
        )


class OcrTranslationTests(unittest.TestCase):
    def test_capture_translates_with_saved_service_and_display_settings(self) -> None:
        settings = AppSettings(
            hytrans_port=7996,
            overlayer_port=7997,
            overlayer_text_color="#123456",
            simple_copy_complete=False,
        )
        complete = mock.Mock()
        parent = mock.Mock()
        with (
            mock.patch.object(mekicopy_ocr, "_prepare_tk_library_paths"),
            mock.patch.object(mekicopy_ocr, "load_settings", return_value=settings),
            mock.patch.object(mekicopy_ocr, "ocr_region", return_value="原文") as capture,
            mock.patch.object(mekicopy_ocr, "request_translation_and_show") as translate,
            mock.patch.object(mekicopy_ocr.messagebox, "showinfo") as feedback,
        ):
            mekicopy_ocr.ocr_and_translate(
                10, 20, 300, 100, parent=parent, on_translation_complete=complete
            )
        capture.assert_called_once_with(10, 20, 300, 100, parent=parent)
        request = translate.call_args
        self.assertEqual(request.args, ("原文",))
        self.assertEqual(request.kwargs["hytrans_url"], "http://127.0.0.1:7996")
        self.assertEqual(request.kwargs["overlayer_url"], "http://127.0.0.1:7997")
        self.assertEqual(request.kwargs["overlayer_config"]["text_color"], "#123456")
        complete.assert_called_once_with()
        feedback.assert_called_once()

    def test_empty_or_failed_capture_never_translates(self) -> None:
        for text in (None, "", "  "):
            with self.subTest(text=text):
                with (
                    mock.patch.object(mekicopy_ocr, "_prepare_tk_library_paths"),
                    mock.patch.object(mekicopy_ocr, "load_settings", return_value=AppSettings()),
                    mock.patch.object(mekicopy_ocr, "ocr_region", return_value=text),
                    mock.patch.object(mekicopy_ocr, "request_translation_and_show") as translate,
                    mock.patch.object(mekicopy_ocr.messagebox, "showwarning"),
                ):
                    mekicopy_ocr.ocr_and_translate(10, 20, 300, 100)
                translate.assert_not_called()

    def test_failed_translation_reports_failure_without_success_callback(self) -> None:
        complete = mock.Mock()
        with (
            mock.patch.object(mekicopy_ocr, "_prepare_tk_library_paths"),
            mock.patch.object(mekicopy_ocr, "load_settings", return_value=AppSettings()),
            mock.patch.object(mekicopy_ocr, "ocr_region", return_value="原文"),
            mock.patch.object(
                mekicopy_ocr, "request_translation_and_show", side_effect=RuntimeError("연결 실패")
            ),
            mock.patch.object(mekicopy_ocr, "_log_runtime_error"),
            mock.patch.object(mekicopy_ocr.messagebox, "showerror") as feedback,
        ):
            mekicopy_ocr.ocr_and_translate(10, 20, 300, 100, on_translation_complete=complete)
        complete.assert_not_called()
        feedback.assert_called_once()
        self.assertIn("연결 실패", feedback.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
