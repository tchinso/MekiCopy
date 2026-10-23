from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import gpu_runtime as runtime


class GpuRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.resources = self.root / "resources"
        self.cache = self.root / "cache"
        (self.resources / "assets").mkdir(parents=True)
        self.payload = {
            "nvidia/cu13/bin/x86_64/cudart64_13.dll": b"CUDA runtime",
            "nvidia/cudnn/bin/cudnn64_9.dll": b"cuDNN runtime",
            "licenses/nvidia-test/License.txt": b"License terms",
        }
        self.wheel = self.root / "test.whl"
        with zipfile.ZipFile(self.wheel, "w") as archive:
            for name, content in self.payload.items():
                archive.writestr(name, content)
            archive.writestr("../../escape.dll", b"must not extract")
        self.manifest = {
            "schemaVersion": 1, "platform": "win_amd64", "packages": [{
                "package": "nvidia-test", "version": "1",
                "url": "https://files.pythonhosted.org/test.whl",
                "size": self.wheel.stat().st_size, "sha256": runtime._sha256(self.wheel),
                "files": [{"member": name, "path": name, "size": len(content),
                           "sha256": hashlib.sha256(content).hexdigest()}
                          for name, content in self.payload.items()],
            }],
        }
        self.write_manifest()
        for patcher in (
            mock.patch.object(runtime, "writable_app_subdir", return_value=self.cache),
            mock.patch.object(runtime.sys, "frozen", True, create=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_manifest(self) -> None:
        (self.resources / "assets/gpu_runtime_manifest.json").write_text(json.dumps(self.manifest))

    def install(self) -> list[Path]:
        with mock.patch.object(runtime, "_download", side_effect=lambda _p, target, _progress:
                               shutil.copyfile(self.wheel, target)):
            return runtime.prepare_gpu_runtime(self.resources)

    def test_download_extracts_only_verified_files_and_reuses_offline(self) -> None:
        directories = self.install()
        self.assertEqual(len(directories), 2)
        self.assertFalse((self.cache / "escape.dll").exists())
        self.assertFalse(list(self.cache.glob(".staging-*")))
        with mock.patch.object(runtime, "_download", side_effect=AssertionError("network used")):
            self.assertEqual(runtime.prepare_gpu_runtime(self.resources), directories)
        self.assertEqual(len(list(self.cache.rglob("License.txt"))), 1)

    def test_full_bundle_does_not_download_or_create_cache(self) -> None:
        for name, content in self.payload.items():
            if name.endswith(".dll"):
                path = self.resources / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        with mock.patch.object(runtime, "_download", side_effect=AssertionError("network used")):
            directories = runtime.prepare_gpu_runtime(self.resources)
        self.assertTrue(all(path.is_relative_to(self.resources) for path in directories))
        self.assertFalse(self.cache.exists())
        self.assertTrue(runtime.validate_gpu_runtime(self.resources))

    def test_corrupt_cache_is_replaced_without_overwriting_loaded_generation(self) -> None:
        old = self.install()
        dll = old[0] / "cudart64_13.dll"
        dll.write_bytes(b"bad runtime")
        new = self.install()
        self.assertNotEqual(old, new)
        self.assertEqual(dll.read_bytes(), b"bad runtime")

    def test_failed_download_never_publishes_partial_runtime(self) -> None:
        with mock.patch.object(runtime, "_download", side_effect=OSError("offline")):
            with self.assertRaisesRegex(OSError, "offline"):
                runtime.prepare_gpu_runtime(self.resources)
        self.assertFalse(list(self.cache.glob(".staging-*")))
        self.assertFalse(list(self.cache.rglob("*.dll")))

    def test_abandoned_staging_is_removed_before_reusing_cache(self) -> None:
        directories = self.install()
        abandoned = self.cache / ".staging-interrupted" / "runtime"
        abandoned.mkdir(parents=True)
        (abandoned / "partial.dll").write_bytes(b"incomplete")
        with mock.patch.object(runtime, "_download", side_effect=AssertionError("network used")):
            self.assertEqual(runtime.prepare_gpu_runtime(self.resources), directories)
        self.assertFalse(abandoned.parent.exists())

    def test_staging_cleanup_does_not_follow_junctions(self) -> None:
        staging = self.cache / ".staging-junction"
        staging.mkdir(parents=True)
        protected = staging / "keep.txt"
        protected.write_bytes(b"external data")
        with mock.patch.object(Path, "is_junction", return_value=True):
            runtime._remove_abandoned_staging(self.cache)
        self.assertEqual(protected.read_bytes(), b"external data")

    def test_extraction_can_cancel_without_publishing_partial_runtime(self) -> None:
        def progress(message: str) -> None:
            if "압축 해제" in message:
                raise RuntimeError("user cancelled")
        with mock.patch.object(runtime, "_download", side_effect=lambda _p, target, _progress:
                               shutil.copyfile(self.wheel, target)):
            with self.assertRaisesRegex(RuntimeError, "user cancelled"):
                runtime.prepare_gpu_runtime(self.resources, progress)
        self.assertFalse(list(self.cache.glob(".staging-*")))
        self.assertFalse(list(self.cache.rglob("*.dll")))

    def test_traversal_manifest_is_rejected_before_network(self) -> None:
        self.manifest["packages"][0]["files"][0]["path"] = "../outside.dll"
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "Invalid GPU runtime path"):
            runtime.prepare_gpu_runtime(self.resources)

    def test_wrong_wheel_member_hash_is_rejected(self) -> None:
        self.manifest["packages"][0]["files"][0]["sha256"] = "0" * 64
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.install()
        self.assertFalse(list(self.cache.rglob("*.dll")))

    def test_concurrent_preparation_publishes_one_generation(self) -> None:
        with mock.patch.object(runtime, "_download", side_effect=lambda _p, target, _progress:
                               shutil.copyfile(self.wheel, target)) as download:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: runtime.prepare_gpu_runtime(self.resources), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(download.call_count, 1)

    def test_download_retries_connection_failure_and_removes_partial_file(self) -> None:
        target = self.root / "partial.whl"
        with mock.patch.object(runtime.urllib.request, "urlopen", side_effect=OSError("offline")) as request:
            with mock.patch.object(runtime.time, "sleep"):
                with self.assertRaisesRegex(OSError, "offline"):
                    runtime._download(self.manifest["packages"][0], target, None)
        self.assertEqual(request.call_count, 3)
        self.assertFalse(target.exists())

    def test_download_progress_can_cancel_without_retry(self) -> None:
        response = io.BytesIO(self.wheel.read_bytes())
        response.geturl = lambda: "https://files.pythonhosted.org/test.whl"
        target = self.root / "cancelled.whl"
        def progress(message: str) -> None:
            if "MiB" in message:
                raise OSError("user cancelled")
        with mock.patch.object(runtime.urllib.request, "urlopen", return_value=response) as request:
            with self.assertRaisesRegex(OSError, "user cancelled"):
                runtime._download(self.manifest["packages"][0], target, progress)
        self.assertEqual(request.call_count, 1)
        self.assertFalse(target.exists())

    def test_lock_wait_can_cancel_through_progress(self) -> None:
        lock = mock.MagicMock()
        lock.__enter__.side_effect = TimeoutError("busy")
        with mock.patch.object(runtime, "exclusive_file_lock", return_value=lock):
            with self.assertRaisesRegex(RuntimeError, "user cancelled"):
                with runtime._install_lock(self.cache, mock.Mock(side_effect=RuntimeError("user cancelled"))):
                    self.fail("lock was not acquired")


if __name__ == "__main__":
    unittest.main()
