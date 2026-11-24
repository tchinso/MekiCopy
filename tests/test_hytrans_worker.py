from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import hytrans_main
from hytrans import app as hytrans_app
from hytrans import config, model_files, paths
from hytrans.browser import BrowserManager
from hytrans.queue import TranslationQueue, TranslationQueueOverloadedError


class HytransDefaultsTests(unittest.TestCase):
    def test_local_wasm_requires_both_cpu_and_webgpu_runtime_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary)
            wasm = assets / "wasm"
            wasm.mkdir()
            for suffix in (".asyncify.mjs", ".asyncify.wasm"):
                (wasm / f"ort-wasm-simd-threaded{suffix}").touch()
            with mock.patch.object(config, "assets_dir", return_value=assets):
                self.assertFalse(config.has_local_wasm_files())
                for suffix in (".mjs", ".wasm"):
                    (wasm / f"ort-wasm-simd-threaded{suffix}").touch()
                self.assertTrue(config.has_local_wasm_files())

    def test_mt15_remains_the_default_among_supported_local_models(self) -> None:
        self.assertEqual(model_files.DEFAULT_MODEL_ID, "mt1.5")
        self.assertEqual(config.DTYPE, "q4")
        self.assertEqual(
            model_files.get_model_profile().model_id,
            "onnx-community/HY-MT1.5-1.8B-ONNX",
        )
        self.assertEqual(model_files.SUPPORTED_MODEL_IDS, ("mt1.5", "plamo3"))
        self.assertEqual(
            model_files.normalize_model_id("TeamFlaxia/pLaMo-3-translate-0.8b-onnx"),
            "plamo3",
        )
        self.assertEqual(model_files.normalize_model_id("mt2"), "mt1.5")
        self.assertEqual(
            model_files.normalize_model_id("tchinso/Hy-MT2-1.8B-onnx-q4f16"),
            "mt1.5",
        )

    def test_cli_uses_mt15_by_default_and_keeps_no_browser_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {
            "MEKICOPY_DATA_DIR": temporary, "MEKICOPY_FORCE_DATA_DIR": "1",
        }), mock.patch.object(sys, "argv", ["HYTrans.exe"]):
            defaults = hytrans_main.parse_args()
        self.assertEqual(defaults.model_id, "mt1.5")
        with mock.patch.object(sys, "argv", ["HYTrans.exe", "--no-worker"]):
            alias = hytrans_main.parse_args()
        self.assertTrue(alias.no_browser)
        with mock.patch.object(sys, "argv", ["HYTrans.exe", "--model", "plamo3"]):
            self.assertEqual(hytrans_main.parse_args().model_id, "plamo3")

    def test_direct_launch_reads_controller_settings_and_honors_cli_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary)
            controller = suite / "MekiCopy"
            controller.mkdir()
            (controller / "settings.cfg").write_text(
                "[settings]\nhytrans_model_id = plamo-3\n", encoding="utf-8"
            )
            for frozen, root in ((True, suite / "HYTrans"), (False, controller)):
                with (
                    self.subTest(frozen=frozen),
                    mock.patch.object(sys, "frozen", frozen, create=True),
                    mock.patch.object(hytrans_main, "app_root", return_value=root),
                    mock.patch.dict(os.environ, {"MEKICOPY_FORCE_DATA_DIR": ""}),
                    mock.patch.object(sys, "argv", ["HYTrans"]),
                ):
                    self.assertEqual(hytrans_main.parse_args().model_id, "plamo3")
                    for flag in ("--model", "--model-id"):
                        with mock.patch.object(sys, "argv", ["HYTrans", flag, "mt1.5"]):
                            self.assertEqual(hytrans_main.parse_args().model_id, "mt1.5")
                    with mock.patch.dict(os.environ, {
                        "MEKICOPY_DATA_DIR": str(suite / "isolated"), "MEKICOPY_FORCE_DATA_DIR": "1",
                    }):
                        self.assertEqual(hytrans_main.parse_args().model_id, "mt1.5")

    def test_direct_launch_reads_fallback_when_portable_settings_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fallback = Path(temporary) / "MekiCopy"
            fallback.mkdir()
            (fallback / "settings.cfg").write_text("[settings]\nhytrans_model_id=plamo3\n", encoding="utf-8")
            with (
                mock.patch.object(hytrans_main, "app_root", return_value=Path(temporary) / "source"),
                mock.patch.dict(os.environ, {"MEKICOPY_DATA_DIR": temporary, "MEKICOPY_FORCE_DATA_DIR": ""}),
                mock.patch.object(sys, "argv", ["HYTrans"]),
            ):
                self.assertEqual(hytrans_main.parse_args().model_id, "plamo3")
                with mock.patch.dict(os.environ, {"MEKICOPY_FORCE_DATA_DIR": "1"}):
                    self.assertEqual(hytrans_main.parse_args().model_id, "plamo3")

    def test_plamo_runtime_config_selects_pfml3_and_q4f16(self) -> None:
        with (
            mock.patch.object(config, "selected_model_profile", return_value=model_files.PLAMO3_PROFILE),
            mock.patch.object(config, "is_complete_model", return_value=True),
        ):
            runtime = config.runtime_config()
        self.assertEqual(runtime.modelKey, "plamo3")
        self.assertEqual(runtime.dtype, "q4f16")
        self.assertEqual(runtime.targetLang, "ko")
        self.assertFalse(runtime.promptTemplate)
        self.assertIn("onnx/model_q4f16.onnx", runtime.modelFiles)


class RealtimeTranslationTuningTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_uses_a_shorter_per_request_generation_budget(self) -> None:
        original_ready = hytrans_app.state.worker_ready
        original_state = hytrans_app.state.state
        original_error = hytrans_app.state.error
        hytrans_app.state.worker_ready = True
        try:
            with mock.patch.object(
                hytrans_app.translation_queue,
                "submit",
                new=mock.AsyncMock(return_value="번역"),
            ) as submit:
                self.assertEqual(
                    await hytrans_app._translate_text("こんにちは", realtime=True),
                    "번역",
                )
                self.assertEqual(
                    await hytrans_app._translate_text("こんにちは"),
                    "번역",
                )
        finally:
            hytrans_app.state.worker_ready = original_ready
            hytrans_app.state.state = original_state
            hytrans_app.state.error = original_error

        realtime_kwargs = submit.await_args_list[0].kwargs
        standard_kwargs = submit.await_args_list[1].kwargs
        self.assertEqual(
            realtime_kwargs["max_new_tokens"],
            config.realtime_max_new_tokens(len("こんにちは")),
        )
        self.assertLess(realtime_kwargs["max_new_tokens"], config.MAX_NEW_TOKENS)
        self.assertEqual(standard_kwargs["max_new_tokens"], config.MAX_NEW_TOKENS)

    def test_realtime_generation_budget_scales_without_reaching_bulk_default(self) -> None:
        self.assertEqual(config.realtime_max_new_tokens(0), 128)
        self.assertEqual(config.realtime_max_new_tokens(100), 264)
        self.assertEqual(config.realtime_max_new_tokens(1_000), 768)

    async def test_queue_overload_is_reported_as_retryable(self) -> None:
        original_ready = hytrans_app.state.worker_ready
        original_state = hytrans_app.state.state
        original_error = hytrans_app.state.error
        hytrans_app.state.worker_ready = True
        try:
            with mock.patch.object(
                hytrans_app.translation_queue,
                "submit",
                new=mock.AsyncMock(
                    side_effect=TranslationQueueOverloadedError("translation queue is busy")
                ),
            ):
                with self.assertRaises(hytrans_app.HTTPException) as raised:
                    await hytrans_app._translate_text("こんにちは", realtime=True)
        finally:
            hytrans_app.state.worker_ready = original_ready
            hytrans_app.state.state = original_state
            hytrans_app.state.error = original_error

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.detail, "translation queue is busy; retry shortly")


class HytransLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_replaces_a_stopped_queue_for_a_new_server_lifetime(self) -> None:
        original_queue = hytrans_app.translation_queue
        original_task = hytrans_app.queue_task
        original_state = dict(vars(hytrans_app.state))
        stale_queue = TranslationQueue()
        stale_queue.stop()
        hytrans_app.translation_queue = stale_queue
        hytrans_app.queue_task = None
        try:
            await hytrans_app.on_startup()
            fresh_queue = hytrans_app.translation_queue
            self.assertIsNot(fresh_queue, stale_queue)
            worker = object()
            fresh_queue.set_worker(worker)
            self.assertTrue(fresh_queue.worker_available)
            self.assertIsNotNone(hytrans_app.queue_task)
            self.assertFalse(hytrans_app.queue_task.done())
        finally:
            await hytrans_app.on_shutdown()
            hytrans_app.translation_queue = original_queue
            hytrans_app.queue_task = original_task
            for key, value in original_state.items():
                setattr(hytrans_app.state, key, value)


class PrivateWorkerTests(unittest.TestCase):
    def test_local_worker_is_isolated_for_wasm_threads(self) -> None:
        client = TestClient(hytrans_app.app)
        local_worker = client.get("/worker.html")
        self.assertEqual(local_worker.status_code, 200)
        self.assertEqual(local_worker.headers["cross-origin-opener-policy"], "same-origin")
        self.assertEqual(local_worker.headers["cross-origin-embedder-policy"], "require-corp")
        translator_worker = client.get("/translator_api_worker.html")
        self.assertEqual(translator_worker.status_code, 200)
        self.assertNotIn("cross-origin-embedder-policy", translator_worker.headers)

    def test_worker_command_is_headless_and_has_no_closeable_app_window(self) -> None:
        command = BrowserManager._worker_command(
            "edge.exe",
            "http://127.0.0.1:6996/worker.html",
            Path("C:/temporary/profile"),
        )
        self.assertIn("--headless=new", command)
        self.assertIn("--enable-unsafe-webgpu", command)
        self.assertIn("--disable-software-rasterizer", command)
        self.assertNotIn("--app=http://127.0.0.1:6996/worker.html", command)
        self.assertEqual(command[-1], "http://127.0.0.1:6996/worker.html")

    def test_worker_reports_a_software_webgpu_adapter_as_cpu_fallback(self) -> None:
        worker = (
            Path(__file__).resolve().parents[1] / "assets" / "worker.js"
        ).read_text(encoding="utf-8")
        self.assertIn("forceFallbackAdapter: false", worker)
        self.assertIn("isFallbackAdapter", worker)
        self.assertIn("swiftshader|software|warp", worker)
        self.assertIn("webgpu.adapter = selectedWebGpuAdapter", worker)
        self.assertIn("deviceDetail: activeDeviceDetail", worker)

    def test_configured_models_dir_is_used_as_the_shared_cache_root(self) -> None:
        original_override = paths._models_dir_override
        original_cache = paths._models_dir_cache
        try:
            with tempfile.TemporaryDirectory(prefix="meki-hytrans-models-") as temporary:
                expected = Path(temporary).resolve()
                paths.configure_models_dir(expected)
                self.assertEqual(paths.models_dir(), expected)
        finally:
            with paths._models_dir_lock:
                paths._models_dir_override = original_override
                paths._models_dir_cache = original_cache


if __name__ == "__main__":
    unittest.main()
