from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runtime_paths


class WritablePathPolicyTests(unittest.TestCase):
    def test_file_lock_wait_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "busy.lock"
            with runtime_paths.exclusive_file_lock(path):
                cancel = mock.Mock(side_effect=[None, None, InterruptedError("cancelled")])
                with self.assertRaises(InterruptedError):
                    with runtime_paths.exclusive_file_lock(path, check_cancel=cancel):
                        self.fail("second lock acquired while first owner holds it")
                self.assertEqual(cancel.call_count, 3)
            with runtime_paths.exclusive_file_lock(path):
                pass

    @unittest.skipUnless(os.name == "nt", "Windows path anchors")
    def test_subdirectory_rejects_rooted_and_drive_relative_paths(self):
        for path in ("C:escape", "\\escape", "C:\\escape", "..\\escape"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                runtime_paths.writable_app_subdir("TestApp", path)

    def setUp(self) -> None:
        runtime_paths._DATA_DIR_CACHE.clear()
        runtime_paths._DATA_SUBDIR_CACHE.clear()

    def test_program_subdirectory_has_priority(self) -> None:
        program = Path("C:/portable/MekiCopy")
        fallback = Path("C:/fallback/MekiCopy/TestApp")
        with (
            mock.patch.dict(
                os.environ,
                {runtime_paths.DATA_DIR_ENV: "", runtime_paths.FORCE_DATA_DIR_ENV: ""},
            ),
            mock.patch.object(runtime_paths, "app_root", return_value=program),
            mock.patch.object(runtime_paths, "fallback_app_data_dirs", return_value=[fallback]),
            mock.patch.object(runtime_paths, "_can_write_directory", return_value=True),
        ):
            selected = runtime_paths.writable_app_subdir("TestApp", "work")
        self.assertEqual(selected, program / "work")

    def test_failed_program_subdirectory_uses_fallback(self) -> None:
        program = Path("C:/portable/MekiCopy")
        fallback = Path("C:/fallback/MekiCopy/TestApp")

        def writable(path: Path) -> bool:
            return path != program / "work"

        with (
            mock.patch.dict(
                os.environ,
                {runtime_paths.DATA_DIR_ENV: "", runtime_paths.FORCE_DATA_DIR_ENV: ""},
            ),
            mock.patch.object(runtime_paths, "app_root", return_value=program),
            mock.patch.object(runtime_paths, "fallback_app_data_dirs", return_value=[fallback]),
            mock.patch.object(runtime_paths, "_can_write_directory", side_effect=writable),
        ):
            selected = runtime_paths.writable_app_subdir("TestApp", "work")
        self.assertEqual(selected, fallback / "work")


if __name__ == "__main__":
    unittest.main()
