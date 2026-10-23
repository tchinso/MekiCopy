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


@unittest.skipUnless(os.name == "nt", "Tcl/Tk path preparation is Windows-specific")
class TkEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="meki-tk-"))
        self.root = Path(temporary)
        self.program = self.root / "program"
        self.python = self.root / "python"
        self.bundle = self.root / "bundle"
        self.enterContext(mock.patch.dict(os.environ))
        self.enterContext(mock.patch.object(runtime_paths, "app_root", return_value=self.program))
        self.enterContext(mock.patch.object(runtime_paths.sys, "base_prefix", str(self.python)))
        self.enterContext(
            mock.patch.object(runtime_paths.sys, "_MEIPASS", str(self.bundle), create=True)
        )
        self.enterContext(mock.patch.object(runtime_paths.sys, "frozen", False, create=True))
        self.enterContext(mock.patch.object(runtime_paths, "windows_short_path", return_value=None))

    def scripts(self, tcl: Path, tk: Path) -> None:
        tcl.mkdir(parents=True)
        tk.mkdir(parents=True)
        (tcl / "init.tcl").write_text("tcl script", encoding="ascii")
        (tk / "tk.tcl").write_text("tk script", encoding="ascii")

    def assert_environment(self, tcl: Path, tk: Path) -> None:
        self.assertEqual(os.environ["TCL_LIBRARY"], tcl.as_posix())
        self.assertEqual(os.environ["TK_LIBRARY"], tk.as_posix())

    def test_source_run_uses_scripts_matching_active_python(self) -> None:
        tcl = self.python / "tcl" / "tcl8.6"
        tk = self.python / "tcl" / "tk8.6"
        self.scripts(tcl, tk)
        runtime = self.program / "MekiCopyRuntime"
        self.scripts(runtime / "tcl8.6", runtime / "tk8.6")
        self.scripts(self.bundle / "_tcl_data", self.bundle / "_tk_data")

        self.assertTrue(runtime_paths.prepare_tk_environment())
        self.assert_environment(tcl, tk)

    def test_frozen_run_uses_bundled_scripts(self) -> None:
        self.scripts(self.python / "tcl" / "tcl8.6", self.python / "tcl" / "tk8.6")
        tcl, tk = self.bundle / "_tcl_data", self.bundle / "_tk_data"
        self.scripts(tcl, tk)
        with mock.patch.object(runtime_paths.sys, "frozen", True):
            self.assertTrue(runtime_paths.prepare_tk_environment())
        self.assert_environment(tcl, tk)

    def test_non_ascii_scripts_use_next_writable_ascii_runtime(self) -> None:
        source = self.root / "한국어"
        self.scripts(source / "tcl" / "tcl8.6", source / "tcl" / "tk8.6")
        blocked = self.root / "blocked"
        blocked.write_text("occupied", encoding="ascii")
        safe = self.root / "runtime"
        with (
            mock.patch.object(runtime_paths.sys, "base_prefix", str(source)),
            mock.patch.object(
                runtime_paths,
                "tk_runtime_roots",
                return_value=[source / "runtime", blocked, safe],
            ),
        ):
            self.assertTrue(runtime_paths.prepare_tk_environment())

        self.assert_environment(safe / "tcl8.6", safe / "tk8.6")
        self.assertEqual(
            (safe / "tcl8.6" / "init.tcl").read_text(encoding="ascii"), "tcl script"
        )
        self.assertEqual(
            (safe / "tk8.6" / "tk.tcl").read_text(encoding="ascii"), "tk script"
        )


if __name__ == "__main__":
    unittest.main()
