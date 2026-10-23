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



class OcrGpuRuntimePreparationTests(unittest.TestCase):
    def setUp(self):
        self.ready = mock.patch.object(mekicopy_ocr, "_ORT_PRELOAD_READY", False)
        self.available = mock.patch.object(mekicopy_ocr, "_ORT_GPU_RUNTIME_AVAILABLE", False)
        self.ready.start()
        self.available.start()
        self.addCleanup(self.ready.stop)
        self.addCleanup(self.available.stop)
        self.addCleanup(mekicopy_ocr.take_ocr_progress)

    def test_cpu_machine_never_downloads_or_preloads_gpu_runtime(self):
        prepare = mock.Mock()
        ort = SimpleNamespace(preload_dlls=mock.Mock())
        with mock.patch.object(mekicopy_ocr, "_cuda_driver_is_usable", return_value=False), \
             mock.patch.dict(sys.modules, {"gpu_runtime": SimpleNamespace(prepare_gpu_runtime=prepare),
                                          "onnxruntime": ort}):
            self.assertFalse(mekicopy_ocr._preload_onnxruntime_gpu_dlls())
        prepare.assert_not_called()
        ort.preload_dlls.assert_not_called()

    def test_cached_or_bundled_directories_are_registered_and_preloaded_once(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            cuda = Path(temporary) / "cuda"
            cudnn = Path(temporary) / "cudnn"
            cuda.mkdir()
            cudnn.mkdir()
            (cuda / "cudart64_13.dll").touch()
            (cudnn / "cudnn64_9.dll").touch()
            prepare = mock.Mock(return_value=[cuda, cudnn])
            ort = SimpleNamespace(preload_dlls=mock.Mock(),
                                  get_available_providers=lambda: ["CUDAExecutionProvider"])
            with mock.patch.object(mekicopy_ocr, "_cuda_driver_is_usable", return_value=True), \
                 mock.patch.object(mekicopy_ocr, "_register_native_dll_directories") as register, \
                 mock.patch.dict(sys.modules, {"gpu_runtime": SimpleNamespace(prepare_gpu_runtime=prepare),
                                              "onnxruntime": ort}):
                self.assertTrue(mekicopy_ocr._preload_onnxruntime_gpu_dlls())
                self.assertTrue(mekicopy_ocr._preload_onnxruntime_gpu_dlls())
            prepare.assert_called_once()
            register.assert_called_once_with([cuda, cudnn])
            self.assertEqual(ort.preload_dlls.call_args_list, [
                mock.call(cuda=True, cudnn=False, msvc=True, directory=str(cuda)),
                mock.call(cuda=False, cudnn=True, msvc=True, directory=str(cudnn)),
            ])

    def test_network_failure_preserves_cpu_ocr(self):
        prepare = mock.Mock(side_effect=OSError("offline"))
        factory = mock.Mock(return_value=_engine("CPUExecutionProvider", "CPUExecutionProvider"))
        ort = SimpleNamespace(preload_dlls=mock.Mock(),
                              get_available_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"])
        with mock.patch.object(mekicopy_ocr, "_cuda_driver_is_usable", return_value=True), \
             mock.patch.object(mekicopy_ocr, "_log_runtime_error"), \
             mock.patch.dict(sys.modules, {"gpu_runtime": SimpleNamespace(prepare_gpu_runtime=prepare),
                                          "onnxruntime": ort}):
            allow_cuda = mekicopy_ocr._preload_onnxruntime_gpu_dlls()
            engine = mekicopy_ocr._create_best_meikiocr_engine(SimpleNamespace(MeikiOCR=factory),
                                                              allow_cuda=allow_cuda)
        self.assertFalse(allow_cuda)
        self.assertEqual(engine.active_provider, "CPUExecutionProvider")
        factory.assert_called_once_with(provider="CPUExecutionProvider")
        ort.preload_dlls.assert_not_called()

    def test_progress_keeps_only_latest_status(self):
        mekicopy_ocr._publish_ocr_progress("first")
        mekicopy_ocr._publish_ocr_progress("second")
        self.assertEqual(mekicopy_ocr.take_ocr_progress(), "second")
        self.assertIsNone(mekicopy_ocr.take_ocr_progress())


class CudaDriverProbeTests(unittest.TestCase):
    def test_download_requires_initialized_driver_cuda13_and_a_device(self):
        import mekicopy_runtime as runtime
        for init, version, devices, expected in ((0, 13000, 1, True), (100, 13000, 1, False),
                                                 (0, 12000, 1, False), (0, 13000, 0, False)):
            with self.subTest(init=init, version=version, devices=devices):
                driver = mock.Mock()
                driver.cuInit.return_value = init
                def write_version(pointer):
                    pointer._obj.value = version
                    return 0
                def write_count(pointer):
                    pointer._obj.value = devices
                    return 0
                driver.cuDriverGetVersion.side_effect = write_version
                driver.cuDeviceGetCount.side_effect = write_count
                with mock.patch.object(runtime.ctypes, "WinDLL", return_value=driver), \
                     mock.patch.object(runtime.os, "name", "nt"):
                    self.assertEqual(runtime._cuda_driver_is_usable(), expected)

    def test_missing_driver_is_cpu_only(self):
        import mekicopy_runtime as runtime
        with mock.patch.object(runtime.ctypes, "WinDLL", side_effect=OSError("missing")), \
             mock.patch.object(runtime.os, "name", "nt"):
            self.assertFalse(runtime._cuda_driver_is_usable())


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


class OcrCancellationTests(unittest.TestCase):
    def tearDown(self):
        mekicopy_ocr.take_ocr_progress()

    def test_cancelled_progress_is_not_delivered_to_ui(self):
        import threading
        cancelled = threading.Event()
        mekicopy_ocr._publish_ocr_progress("downloading", cancelled.is_set)
        cancelled.set()
        self.assertIsNone(mekicopy_ocr.take_ocr_progress())

    def test_runtime_download_cancellation_does_not_stick_cpu_fallback(self):
        import threading
        cancelled = threading.Event()
        def prepare(_root, progress):
            cancelled.set()
            progress("downloading")
        ort = SimpleNamespace(preload_dlls=mock.Mock(),
                              get_available_providers=lambda: ["CUDAExecutionProvider"])
        with mock.patch.object(mekicopy_ocr, "_ORT_PRELOAD_READY", False), \
             mock.patch.object(mekicopy_ocr, "_ORT_GPU_RUNTIME_AVAILABLE", False), \
             mock.patch.object(mekicopy_ocr, "_cuda_driver_is_usable", return_value=True), \
             mock.patch.dict(sys.modules, {"gpu_runtime": SimpleNamespace(prepare_gpu_runtime=prepare),
                                          "onnxruntime": ort}):
            with self.assertRaises(mekicopy_ocr.OcrCancelledError):
                mekicopy_ocr._preload_onnxruntime_gpu_dlls(cancelled.is_set)
            self.assertFalse(mekicopy_ocr._ORT_PRELOAD_READY)
            self.assertFalse(mekicopy_ocr._ORT_GPU_RUNTIME_AVAILABLE)
        ort.preload_dlls.assert_not_called()

    def test_waiting_for_engine_lock_can_be_cancelled_without_releasing_owner(self):
        import threading
        lock = threading.Lock()
        lock.acquire()
        cancelled = threading.Event()
        finished = threading.Event()
        errors = []
        def wait():
            try:
                with mekicopy_ocr._ocr_lock(lock, cancelled.is_set):
                    self.fail("cancelled waiter entered critical section")
            except mekicopy_ocr.OcrCancelledError:
                errors.append("cancelled")
            finally:
                finished.set()
        thread = threading.Thread(target=wait)
        thread.start()
        cancelled.set()
        try:
            self.assertTrue(finished.wait(1))
            self.assertEqual(errors, ["cancelled"])
            self.assertTrue(lock.locked())
        finally:
            lock.release()
            thread.join(timeout=1)

    def test_realtime_ocr_passes_cancellation_to_initialization(self):
        from PIL import Image
        cancelled = mock.Mock(return_value=False)
        engine = mock.Mock()
        engine.run_ocr.return_value = [{"text": "hello"}]
        with mock.patch.object(mekicopy_ocr, "_get_ocr_engine", return_value=engine) as get_engine, \
             mock.patch.object(mekicopy_ocr, "_prepare_native_runtime_paths"):
            self.assertEqual(mekicopy_ocr.recognize_image(Image.new("RGB", (10, 10)),
                                                       is_cancelled=cancelled), "hello")
        get_engine.assert_called_once_with(cancelled)


class DetachedOcrProgressTests(unittest.TestCase):
    def test_settings_poll_consumes_download_status_on_ui_thread(self):
        import mekicopy
        window = SimpleNamespace(_closing=False, _settings_file_state=("file", 1, 1),
                                 _current_settings_file_state=lambda: ("file", 1, 1),
                                 _refresh_realtime_lock=mock.Mock(),
                                 _schedule_settings_poll=mock.Mock())
        with mock.patch.object(mekicopy, "take_ocr_progress", return_value="GPU runtime 45%"):
            mekicopy.DetachedOcrButtonApp._poll_settings(window)
        self.assertEqual(window._ocr_progress_text, "GPU runtime 45%")
        window._refresh_realtime_lock.assert_called_once()
        window._schedule_settings_poll.assert_called_once()

    def test_busy_detached_button_displays_progress_and_restores_action_after_work(self):
        import mekicopy
        runner = mock.Mock()
        button = mock.Mock()
        window = SimpleNamespace(_task_runner=runner, button=button,
                                 _ocr_progress_text="GPU runtime 45%", settings=AppSettings(realtime_ocr_enabled=False),
                                 ocr_action_label=lambda: "translate")
        with mock.patch.object(mekicopy, "detached_translation_locked", return_value=False):
            runner.is_running.return_value = True
            mekicopy.DetachedOcrButtonApp._refresh_realtime_lock(window)
            button.config.assert_called_with(text="GPU runtime 45%", state="disabled")
            runner.is_running.return_value = False
            mekicopy.DetachedOcrButtonApp._refresh_realtime_lock(window)
            button.config.assert_called_with(text="translate", state="normal")
