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

    def test_changed_dependency_cannot_reuse_a_lite_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "MekiCopy.exe").write_bytes(b"runtime")
            with mock.patch.object(packaging, "source_fingerprint", return_value="source"):
                with mock.patch.object(packaging, "installed_dependencies", return_value={"package": "1"}):
                    packaging.write_build_manifest(root, root, "Lite")
                with mock.patch.object(packaging, "installed_dependencies", return_value={"package": "2"}):
                    with self.assertRaisesRegex(ValueError, "dependencies changed"):
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

    def test_hdtrans_and_realtime_user_state_are_rejected(self) -> None:
        for filename in ("hdtrans_settings.json", "realtime_ocr_state.json"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / filename).write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Saved user settings"):
                    packaging.write_build_manifest(root, root, "Lite")
                self.assertFalse((root / packaging.MANIFEST_NAME).exists())


class ReleaseFlavorParityTests(unittest.TestCase):
    def test_full_may_add_only_optional_runtime_and_model_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lite, full = root / "Lite", root / "Full"
            lite.mkdir()
            full.mkdir()
            for directory in (lite, full):
                (directory / "MekiCopy.exe").write_bytes(b"runtime")
            for relative in (
                "HYTrans/models/owner/model/model.onnx",
                "HYTrans/hdtrans/installations/0.4.4-windows-x64/hdTransEngine.dll",
                "MekiAudioCapture/models/vad/vad.onnx",
                "MekiCopy/_internal/runtime_models/meikiocr/ocr.onnx",
                "MekiCopy/_internal/nvidia/cudnn/bin/cudnn64_9.dll",
                "MekiCopy/_internal/assets/ffmpeg/ffmpeg.exe",
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


class BrowserRuntimeValidationTests(unittest.TestCase):
    def test_browser_packaging_ignores_unlisted_optional_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).resolve().parents[1] / "assets/runtime_manifest.json"
            manifest = json.loads(source.read_text(encoding="utf-8"))
            for item in manifest["files"]:
                path = root / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
                item.update(size=path.stat().st_size, sha256=packaging.sha256_file(path))
            (root / "runtime_manifest.json").write_text(json.dumps(manifest))
            (root / "ffmpeg").mkdir()
            (root / "ffmpeg/ffmpeg.exe").write_bytes(b"optional")
            selected = packaging.browser_runtime_data(root)
            self.assertEqual(len(selected), len(manifest["files"]) + 1)
            self.assertFalse(any("ffmpeg" in path for path, _ in selected))
            self.assertIn((str(root / "wasm/ort-wasm-simd-threaded.wasm"), "assets/wasm"), selected)

    def test_checked_in_runtime_assets_match_manifest(self) -> None:
        packaging.verify_runtime_assets(Path(__file__).resolve().parents[1] / "assets")

    def test_mixed_runtime_versions_are_rejected_before_reading_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = {
                "transformers": {"buildOverride": {"onnxRuntimeWeb": "1.30.0"}},
                "onnxRuntimeWeb": {"version": "1.29.0"},
            }
            (root / "runtime_manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "versions disagree"):
                packaging.verify_runtime_assets(root)

    def test_missing_browser_entry_point_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = {
                "transformers": {"buildOverride": {"onnxRuntimeWeb": "1.30.0"}},
                "onnxRuntimeWeb": {"version": "1.30.0"}, "files": [],
            }
            (root / "runtime_manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "missing or duplicate"):
                packaging.verify_runtime_assets(root)


class LiteOptionalPayloadTests(unittest.TestCase):
    def test_optional_payloads_are_rejected_in_every_companion(self) -> None:
        for relative in (
            "HYTrans/_internal/assets/ffmpeg/ffmpeg.exe",
            "MekiAudioCapture/_internal/ffprobe.exe",
            "MekiDisplay/_internal/nvidia/licenses/license.txt",
            "HYTrans/_internal/cudart64_13.dll",
        ):
            with self.subTest(relative=relative):
                with self.assertRaisesRegex(ValueError, "Full-only payload"):
                    packaging.verify_lite_optional_payloads({relative: Path(relative)})

    def test_common_onnx_provider_and_opencv_media_helper_are_allowed(self) -> None:
        names = (
            "MekiCopy/_internal/onnxruntime/capi/onnxruntime_providers_cuda.dll",
            "MekiCopy/_internal/cv2/opencv_videoio_ffmpeg500_64.dll",
        )
        packaging.verify_lite_optional_payloads({name: Path(name) for name in names})


class MediaPayloadTests(unittest.TestCase):
    def test_media_probe_requires_matching_executables_and_notices(self) -> None:
        import media_tools_download
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {}
            for name in ("ffmpeg.exe", "ffprobe.exe", "LICENSE", "README.txt"):
                path = root / name
                path.write_bytes(name.encode("ascii"))
                files[name] = (path.stat().st_size, packaging.sha256_file(path))
            with mock.patch.object(media_tools_download, "FILES", files):
                packaging.verify_media_tools(root)
                (root / "LICENSE").unlink()
                with self.assertRaisesRegex(ValueError, "license files"):
                    packaging.verify_media_tools(root)
                (root / "LICENSE").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "license files"):
                    packaging.verify_media_tools(root)


if __name__ == "__main__":
    unittest.main()
