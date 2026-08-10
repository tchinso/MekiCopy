from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mekicopy_settings
import system_logging
from hytrans import model_files


class StateFailoverTests(unittest.TestCase):
    def test_actual_file_failure_pins_all_state_to_fallback(self) -> None:
        original_directory = mekicopy_settings.STATE_DIR
        original_pinned = mekicopy_settings._STATE_ROOT_PINNED
        with tempfile.TemporaryDirectory(prefix="메키상태-") as temporary:
            root = Path(temporary)
            program = root / "program"
            fallback = root / "fallback"
            program.mkdir()
            # A directory occupying one fixed filename reproduces an actual
            # per-file failure even though the parent itself is writable.
            (program / "settings.cfg").mkdir()
            (program / "bookmarks.txt").write_text("기존\t0\t0\t10\t10\n", encoding="utf-8")
            try:
                mekicopy_settings._activate_state_directory(program)
                mekicopy_settings._STATE_ROOT_PINNED = False
                with (
                    mock.patch.object(mekicopy_settings, "_get_app_dir", return_value=str(program)),
                    mock.patch.object(
                        mekicopy_settings,
                        "fallback_app_data_dirs",
                        return_value=[fallback],
                    ),
                ):
                    self.assertTrue(
                        mekicopy_settings._write_state_text("settings.cfg", "[main]\n")
                    )
                    self.assertEqual(mekicopy_settings.STATE_DIR, fallback)
                    self.assertTrue(mekicopy_settings._STATE_ROOT_PINNED)
                    self.assertTrue((fallback / "bookmarks.txt").is_file())

                    (program / "settings.cfg").rmdir()
                    self.assertTrue(
                        mekicopy_settings._write_state_text(
                            "detached_button_geometry.json",
                            "{}",
                        )
                    )
                    self.assertTrue((fallback / "detached_button_geometry.json").is_file())
                    self.assertFalse((program / "detached_button_geometry.json").exists())
            finally:
                mekicopy_settings._activate_state_directory(original_directory)
                mekicopy_settings._STATE_ROOT_PINNED = original_pinned


class LoggingFailoverTests(unittest.TestCase):
    def test_fixed_log_failure_uses_next_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="메키로그-") as temporary:
            root = Path(temporary)
            program = root / "program"
            fallback = root / "fallback"
            program.mkdir()
            # The directory is writable, but the fixed log target is not a file.
            (program / "mekitest.log").mkdir()
            system_logging._log_directory_cache.clear()
            with mock.patch.object(
                system_logging,
                "_log_directory_candidates",
                return_value=[program, fallback],
            ):
                system_logging._append("debug_log", "MekiTest", "ok")
            self.assertEqual((fallback / "mekitest.log").read_text(encoding="utf-8"), "ok\n")


class HytransIntegrityFallbackTests(unittest.TestCase):
    def test_verified_manifest_can_live_outside_read_only_model_root(self) -> None:
        data = b"verified model"
        digest = hashlib.sha256(data).hexdigest()
        profile = model_files.ModelProfile(
            key="test",
            display_label="test",
            model_id="owner/test",
            revision="revision",
            dtype="q4",
            prompt="{text}",
            files={"model.bin": model_files.ModelFileSpec(len(data), digest)},
        )
        with tempfile.TemporaryDirectory(prefix="메키모델-") as temporary:
            root = Path(temporary)
            model_root = root / "model"
            model_root.mkdir()
            (model_root / "model.bin").write_bytes(data)
            blocked_manifest = root / "blocked"
            blocked_manifest.mkdir()
            fallback_manifest = root / "cache" / "manifest.json"
            with mock.patch.object(
                model_files,
                "_manifest_paths",
                return_value=[blocked_manifest, fallback_manifest],
            ):
                self.assertTrue(
                    model_files.is_verified_model_file(model_root, "model.bin", profile)
                )
                self.assertTrue(fallback_manifest.is_file())
                with mock.patch.object(
                    model_files,
                    "sha256_file",
                    side_effect=AssertionError("unexpected rehash"),
                ):
                    self.assertTrue(
                        model_files.is_verified_model_file(model_root, "model.bin", profile)
                    )

    def test_stale_fallback_does_not_shadow_new_local_manifest(self) -> None:
        data = b"verified model"
        digest = hashlib.sha256(data).hexdigest()
        profile = model_files.ModelProfile(
            key="test",
            display_label="test",
            model_id="owner/test",
            revision="revision",
            dtype="q4",
            prompt="{text}",
            files={"model.bin": model_files.ModelFileSpec(len(data), digest)},
        )
        with tempfile.TemporaryDirectory(prefix="메키모델병합-") as temporary:
            root = Path(temporary)
            model_root = root / "model"
            model_root.mkdir()
            model_path = model_root / "model.bin"
            model_path.write_bytes(data)
            local_manifest = model_root / ".manifest.json"
            fallback_manifest = root / "cache" / "manifest.json"
            fallback_manifest.parent.mkdir()
            fallback_manifest.write_text(
                json.dumps(
                    {
                        "modelId": profile.model_id,
                        "revision": profile.revision,
                        "files": {
                            "model.bin": {
                                "size": len(data),
                                "mtimeNs": 0,
                                "sha256": digest,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            original_hash = model_files.sha256_file
            with (
                mock.patch.object(
                    model_files,
                    "_manifest_paths",
                    return_value=[local_manifest, fallback_manifest],
                ),
                mock.patch.object(
                    model_files,
                    "sha256_file",
                    wraps=original_hash,
                ) as hash_file,
            ):
                self.assertTrue(
                    model_files.is_verified_model_file(model_root, "model.bin", profile)
                )
                self.assertTrue(
                    model_files.is_verified_model_file(model_root, "model.bin", profile)
                )
            self.assertEqual(hash_file.call_count, 1)


if __name__ == "__main__":
    unittest.main()
