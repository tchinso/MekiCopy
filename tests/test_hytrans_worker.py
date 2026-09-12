from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hytrans_main
from hytrans import config, model_files, paths
from hytrans.browser import BrowserManager


class HytransDefaultsTests(unittest.TestCase):
    def test_mt15_is_the_default_and_mt2_remains_supported(self) -> None:
        self.assertEqual(model_files.DEFAULT_MODEL_ID, "mt1.5")
        self.assertEqual(config.DTYPE, "q4")
        self.assertEqual(
            model_files.get_model_profile().model_id,
            "onnx-community/HY-MT1.5-1.8B-ONNX",
        )
        self.assertIn("mt2", model_files.SUPPORTED_MODEL_IDS)

    def test_cli_uses_mt15_by_default_and_keeps_no_browser_alias(self) -> None:
        with mock.patch.object(sys, "argv", ["HYTrans.exe"]):
            defaults = hytrans_main.parse_args()
        self.assertEqual(defaults.model_id, "mt1.5")
        with mock.patch.object(sys, "argv", ["HYTrans.exe", "--no-worker"]):
            alias = hytrans_main.parse_args()
        self.assertTrue(alias.no_browser)


class PrivateWorkerTests(unittest.TestCase):
    def test_worker_command_is_headless_and_has_no_closeable_app_window(self) -> None:
        command = BrowserManager._worker_command(
            "edge.exe",
            "http://127.0.0.1:6996/worker.html",
            Path("C:/temporary/profile"),
        )
        self.assertIn("--headless=new", command)
        self.assertNotIn("--app=http://127.0.0.1:6996/worker.html", command)
        self.assertEqual(command[-1], "http://127.0.0.1:6996/worker.html")

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
