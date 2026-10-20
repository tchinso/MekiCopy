from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from hytrans import hdtrans_download as download


class HdTransInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / "hdtrans"
        self.archive = self.root / "release.zip"
        self.source = self.root / "source.zip"
        self.source.write_bytes(b"corresponding source")
        self.payload = {
            "hdTransEngine.dll": b"native C API DLL",
            "hdTrans/dicts.hdsnap": b"same release snapshot",
            "hdTrans/dicts.txt": (download.DICTIONARY_VERSION + "\n").encode(),
            "LICENSE": b"AGPL license",
            "THIRD-PARTY-NOTICES.txt": b"notices",
            "hdTrans/user/hdtrans.config.example.yaml": b"# config example",
            "hdTrans/user/userdict.example.txt": b"# dictionary example",
            download.SOURCE_NAME: self.source.read_bytes(),
        }
        with zipfile.ZipFile(self.archive, "w") as package:
            for name, content in self.payload.items():
                if name == download.SOURCE_NAME:
                    continue
                member = download.DLL_FILES[download.architecture()][0] if name == "hdTransEngine.dll" else name
                package.writestr(member, content)
            # Unlisted paths must never be extracted.
            package.writestr("../../escape.txt", b"unsafe")
            package.writestr("J2KEngine.dll", b"unused compatibility DLL")
        expected = {name: (len(value), hashlib.sha256(value).hexdigest()) for name, value in self.payload.items()}
        for patcher in (
            mock.patch.object(download, "expected_files", return_value=expected),
            mock.patch.object(download, "ARCHIVE_SIZE", self.archive.stat().st_size),
            mock.patch.object(download, "ARCHIVE_SHA256", download._hash(self.archive)),
            mock.patch.object(download, "SOURCE_SIZE", self.source.stat().st_size),
            mock.patch.object(download, "SOURCE_SHA256", download._hash(self.source)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def prepare(self, **kwargs) -> Path:
        return download.ensure_installation(root=self.install, archive_path=self.archive,
                                            source_path=self.source, **kwargs)

    def test_install_uses_verified_pair_and_never_extracts_arbitrary_paths(self) -> None:
        phases = []
        installed = self.prepare(progress=lambda payload: phases.append(payload["state"]))
        self.assertTrue(download.validate_installation(installed, rehash=True))
        self.assertEqual(download.resolve_installation(self.install), installed)
        self.assertEqual((installed / "hdTransEngine.dll").read_bytes(), self.payload["hdTransEngine.dll"])
        self.assertIn("verifying", phases)
        self.assertEqual(phases[-1], "ready")
        self.assertFalse((installed / "J2KEngine.dll").exists())
        self.assertFalse((self.root / "escape.txt").exists())

    def test_offline_reuse_does_not_contact_network(self) -> None:
        installed = self.prepare()
        with mock.patch.object(download, "_download", side_effect=AssertionError("network used")):
            self.assertEqual(download.ensure_installation(root=self.install), installed)

    def test_corrupted_file_and_mismatched_snapshot_version_are_rejected(self) -> None:
        installed = self.prepare()
        (installed / "hdTrans/dicts.hdsnap").write_bytes(b"damaged")
        self.assertFalse(download.validate_installation(installed))
        repaired = self.prepare()
        self.assertNotEqual(installed, repaired)
        manifest = json.loads((repaired / download.MANIFEST_NAME).read_text())
        manifest["dictionaryVersion"] = "incompatible"
        (repaired / download.MANIFEST_NAME).write_text(json.dumps(manifest))
        self.assertFalse(download.validate_installation(repaired))

    def test_force_stages_new_generation_preserving_old_and_user_files(self) -> None:
        installed = self.prepare()
        user = self.install / "user" / "userdict-names.txt"
        user.parent.mkdir()
        user.write_text("名前\t이름", encoding="utf-8")
        replacement = self.prepare(force=True)
        self.assertNotEqual(installed, replacement)
        self.assertTrue(download.validate_installation(installed))
        self.assertEqual(download.resolve_installation(self.install), replacement)
        self.assertEqual(user.read_text(encoding="utf-8"), "名前\t이름")

    def test_bad_download_and_cancel_leave_previous_active_install(self) -> None:
        installed = self.prepare()
        active = (self.install / "active.json").read_bytes()
        self.archive.write_bytes(b"failed download")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.prepare(force=True)
        event = threading.Event()
        event.set()
        with self.assertRaises(download.DownloadCancelled):
            self.prepare(force=True, cancel_event=event)
        self.assertEqual((self.install / "active.json").read_bytes(), active)
        self.assertEqual(download.resolve_installation(self.install), installed)
        self.assertFalse(list(self.install.glob(".staging-*")))

    def test_cancellation_after_validation_does_not_publish_partial_install(self) -> None:
        event = threading.Event()
        def progress(payload: dict) -> None:
            if payload["state"] == "extracting":
                event.set()
        with self.assertRaises(download.DownloadCancelled):
            self.prepare(cancel_event=event, progress=progress)
        self.assertIsNone(download.resolve_installation(self.install))
        self.assertFalse((self.install / "active.json").exists())

    def test_concurrent_first_install_serializes_and_reuses_one_generation(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            paths = list(executor.map(lambda _: self.prepare(), range(2)))
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(len(list((self.install / "installations").iterdir())), 1)

    def test_pointer_failure_preserves_old_active_generation(self) -> None:
        installed = self.prepare()
        with mock.patch.object(download, "_activate", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.prepare(force=True)
        self.assertEqual(download.resolve_installation(self.install), installed)

    def test_corrupt_pointer_still_finds_previous_verified_install(self) -> None:
        installed = self.prepare()
        (self.install / "active.json").write_text('{"installation":"../../outside"}')
        self.assertEqual(download.resolve_installation(self.install), installed)

    def test_retry_after_first_network_failure(self) -> None:
        installed = self.prepare()
        with mock.patch.object(download, "_download", side_effect=OSError("offline")):
            with self.assertRaisesRegex(OSError, "offline"):
                download.ensure_installation(root=self.install, force=True)
        self.assertEqual(download.resolve_installation(self.install), installed)
        replacement = self.prepare(force=True)
        self.assertTrue(download.validate_installation(replacement))

    def test_frozen_ui_and_server_share_config_owned_install_root(self) -> None:
        shared = self.root / "MekiCopy" / "hdtrans_settings.json"
        with mock.patch("hytrans.hdtrans_settings.hdtrans_settings_path", return_value=shared):
            with mock.patch.object(download, "app_root", return_value=self.root / "MekiCopy"):
                ui_root = download.install_root()
            with mock.patch.object(download, "app_root", return_value=self.root / "HYTrans"):
                server_root = download.install_root()
                user_root = download.user_root()
        self.assertEqual(ui_root, shared.parent / "hdtrans")
        self.assertEqual(server_root, ui_root)
        self.assertEqual(user_root, ui_root / "user")

    def test_full_seed_copies_to_shared_writable_store_without_network(self) -> None:
        seeded = self.prepare()
        executable_root = self.root / "Full" / "HYTrans"
        executable_root.mkdir(parents=True)
        target_seed = executable_root / "hdtrans"
        import shutil
        shutil.copytree(self.install, target_seed)
        shared_root = self.root / "shared" / "hdtrans"
        with mock.patch.object(download, "install_root", return_value=shared_root), \
             mock.patch.object(download, "app_root", return_value=executable_root), \
             mock.patch.object(download, "_download", side_effect=AssertionError("network used")):
            selected = download.ensure_installation()
        self.assertNotEqual(selected, seeded)
        self.assertTrue(selected.is_relative_to(shared_root))
        self.assertTrue(download.validate_installation(selected))

    def test_full_ui_first_install_uses_sibling_hytrans_seed_offline(self) -> None:
        self.prepare()
        import shutil
        native_root = self.root / "Full" / "HYTrans"
        native_root.mkdir(parents=True)
        shutil.copytree(self.install, native_root / "hdtrans")
        main_root = self.root / "Full" / "MekiCopy"
        main_root.mkdir()
        shared_root = main_root / "hdtrans"
        with mock.patch.object(download, "install_root", return_value=shared_root), \
             mock.patch.object(download, "app_root", return_value=main_root), \
             mock.patch.object(download, "_download", side_effect=AssertionError("network used")):
            selected = download.ensure_installation()
        self.assertTrue(selected.is_relative_to(shared_root))
        self.assertTrue(download.validate_installation(selected))


if __name__ == "__main__":
    unittest.main()
