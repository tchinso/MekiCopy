from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import release_packaging as packaging


class ReleaseReuseTests(unittest.TestCase):
    def test_unchanged_metadata_reuses_completed_content_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "MekiCopy.exe").write_bytes(b"runtime")
            with mock.patch.object(packaging, "source_fingerprint", return_value="source"):
                packaging.write_build_manifest(root, root, "Lite")
                with mock.patch.object(packaging, "sha256_file", side_effect=AssertionError("unexpected rehash")):
                    packaging.verify_lite_build(root, root)

    def test_changed_source_cannot_reuse_an_older_lite_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "MekiCopy.exe").write_bytes(b"runtime")
            with mock.patch.object(packaging, "source_fingerprint", return_value="old"):
                packaging.write_build_manifest(root, root, "Lite")
            with mock.patch.object(packaging, "source_fingerprint", return_value="new"):
                with self.assertRaisesRegex(ValueError, "source changed"):
                    packaging.verify_lite_build(root, root)

    def test_same_length_mutation_with_changed_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "MekiCopy.exe").write_bytes(b"runtime")
            with mock.patch.object(packaging, "source_fingerprint", return_value="source"):
                packaging.write_build_manifest(root, root, "Lite")
                manifest_path = root / packaging.MANIFEST_NAME
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["files"]["MekiCopy.exe"]["mtimeNs"] = 0
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                (root / "MekiCopy.exe").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "file changed"):
                    packaging.verify_lite_build(root, root)

    def test_additional_unverified_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "MekiCopy.exe").write_bytes(b"runtime")
            with mock.patch.object(packaging, "source_fingerprint", return_value="source"):
                packaging.write_build_manifest(root, root, "Lite")
                (root / "settings.cfg").write_text("secret", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "file list changed"):
                    packaging.verify_lite_build(root, root)

    def test_skipped_executable_checks_cannot_be_reused_as_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "MekiCopy.exe").write_bytes(b"runtime")
            with mock.patch.object(packaging, "source_fingerprint", return_value="source"):
                packaging.write_build_manifest(root, root, "Lite", smoke_tested=False)
                with self.assertRaisesRegex(ValueError, "smoke tests were skipped"):
                    packaging.verify_lite_build(root, root)

    def test_saved_provider_credentials_are_rejected_before_packaging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "translation_api.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Saved API settings"):
                packaging.write_build_manifest(root, root, "Lite")
            self.assertFalse((root / packaging.MANIFEST_NAME).exists())


class ReleaseFlavorParityTests(unittest.TestCase):
    def test_full_may_add_only_model_and_magpie_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lite, full = root / "Lite", root / "Full"
            lite.mkdir()
            full.mkdir()
            for directory in (lite, full):
                (directory / "MekiCopy.exe").write_bytes(b"runtime")
            for relative in (
                "HYTrans/models/owner/model/model.onnx",
                "MekiAudioCapture/models/vad/vad.onnx",
                "MekiCopy/_internal/runtime_models/meikiocr/ocr.onnx",
                "MagPie/MagPie.exe",
            ):
                path = full / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"payload")
            packaging.verify_flavor_parity(lite, full)
            (full / "unexpected.dll").write_bytes(b"extra")
            with self.assertRaisesRegex(ValueError, "file lists differ"):
                packaging.verify_flavor_parity(lite, full)

    def test_changed_shared_runtime_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lite, full = root / "Lite", root / "Full"
            lite.mkdir()
            full.mkdir()
            (lite / "MekiCopy.exe").write_bytes(b"runtime")
            (full / "MekiCopy.exe").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "contents differ"):
                packaging.verify_flavor_parity(lite, full)


if __name__ == "__main__":
    unittest.main()
