from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import mekicopy_ocr


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


if __name__ == "__main__":
    unittest.main()
