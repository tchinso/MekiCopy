from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hytrans import config, paths
from hytrans.model_files import MODEL_FILE_SPECS, MODEL_ID, ModelFileSpec


SMALL_MODEL_SPECS = {
    "config.json": ModelFileSpec(size=3, sha256="unused"),
    "onnx/model_q4.onnx": ModelFileSpec(size=5, sha256="unused"),
}


def write_model_files(root: Path, specs: dict[str, ModelFileSpec]) -> Path:
    model_root = root.joinpath(*MODEL_ID.split("/"))
    for relative_path, spec in specs.items():
        target = model_root.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * spec.size)
    return model_root


class HytransModelPathTests(unittest.TestCase):
    def test_existing_exe_adjacent_model_files_are_used_before_appdata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_root = root / "app"
            app_data = root / "data"
            write_model_files(app_root / "models", SMALL_MODEL_SPECS)

            with patch.object(paths, "app_root", return_value=app_root), patch.object(
                paths,
                "app_data_dir",
                return_value=app_data,
            ):
                self.assertEqual(paths.models_dir(), app_root / "models")

    def test_fresh_build_uses_stable_appdata_models_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_root = root / "app"
            app_data = root / "data"

            with patch.object(paths, "app_root", return_value=app_root), patch.object(
                paths,
                "app_data_dir",
                return_value=app_data,
            ):
                self.assertEqual(paths.models_dir(), app_data / "models")

    def test_exact_size_model_files_disable_remote_download_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            models_root = Path(temporary)
            write_model_files(models_root, SMALL_MODEL_SPECS)

            with patch.dict(MODEL_FILE_SPECS, SMALL_MODEL_SPECS, clear=True), patch.object(
                config,
                "models_dir",
                return_value=models_root,
            ):
                self.assertEqual(config.detect_model_mode(), "local")


if __name__ == "__main__":
    unittest.main()
