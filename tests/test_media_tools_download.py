from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock
import zipfile
import uuid

import media_tools_download as media
import meki_subtitle_paths as paths


class MediaToolsDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = {"ffmpeg.exe": b"test-encoder", "ffprobe.exe": b"test-probe", "LICENSE": b"license"}
        self.files = {name: (len(value), hashlib.sha256(value).hexdigest()) for name, value in self.payload.items()}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, value in self.payload.items():
                relative = "bin/" + name if name.endswith(".exe") else name
                archive.writestr(f"ffmpeg-{media.VERSION}-essentials_build/{relative}", value)
            archive.writestr("../../outside.txt", "must not be extracted")
        self.archive = stream.getvalue()
        self.patch = mock.patch.multiple(media, FILES=self.files,
            ARCHIVE_SIZE=len(self.archive), ARCHIVE_SHA256=hashlib.sha256(self.archive).hexdigest())
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def cached_archive(self):
        (self.root / media.ARCHIVE_NAME).write_bytes(self.archive)

    def test_download_then_reuse_verified_install_offline(self):
        with mock.patch.object(media.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)) as network:
            installed = media.ensure_media_tools(cache_root=self.root)
            self.assertEqual(network.call_count, 1)
        with mock.patch.object(media.urllib.request, "urlopen", side_effect=AssertionError("offline")):
            self.assertEqual(media.ensure_media_tools(cache_root=self.root), installed)
        self.assertEqual((installed / "ffmpeg.exe").read_bytes(), self.payload["ffmpeg.exe"])
        self.assertFalse((self.root.parent / "outside.txt").exists())
        self.assertEqual({path.name for path in installed.iterdir()}, set(self.files))

    def test_offline_archive_repairs_corrupt_install(self):
        self.cached_archive()
        installed = media.ensure_media_tools(cache_root=self.root)
        (installed / "ffprobe.exe").write_bytes(b"damaged")
        with mock.patch.object(media.urllib.request, "urlopen", side_effect=AssertionError("offline")):
            media.ensure_media_tools(cache_root=self.root)
        self.assertEqual((installed / "ffprobe.exe").read_bytes(), self.payload["ffprobe.exe"])
        self.assertFalse(list(self.root.glob(".ffmpeg-*")))

    def test_failed_publication_restores_previous_generation(self):
        self.cached_archive()
        installed = media.ensure_media_tools(cache_root=self.root)
        (installed / "ffprobe.exe").write_bytes(b"previous")
        replace = media.os.replace
        def fail_publish(source, destination):
            if Path(source).name == "payload":
                raise PermissionError("publication failure")
            return replace(source, destination)
        with mock.patch.object(media.os, "replace", side_effect=fail_publish):
            with self.assertRaisesRegex(PermissionError, "publication"):
                media.ensure_media_tools(cache_root=self.root)
        self.assertEqual((installed / "ffprobe.exe").read_bytes(), b"previous")

    def test_cancelled_download_cleans_partial_and_publishes_nothing(self):
        count = 0
        def check():
            nonlocal count
            count += 1
            if count > 3:
                raise InterruptedError("cancelled")
        with mock.patch.object(media.urllib.request, "urlopen", return_value=io.BytesIO(self.archive)):
            with self.assertRaises(InterruptedError):
                media.ensure_media_tools(cache_root=self.root, check_cancel=check)
        self.assertFalse(list(self.root.glob("*.part")))
        self.assertFalse((self.root / f"ffmpeg-{media.VERSION}-windows-x64").exists())

    def test_hash_verification_cancellation_is_not_treated_as_cache_miss(self):
        self.cached_archive()
        with self.assertRaises(InterruptedError):
            media._matches(self.root / media.ARCHIVE_NAME, len(self.archive), "irrelevant",
                           lambda: (_ for _ in ()).throw(InterruptedError("cancelled")))

    def test_bad_download_cannot_publish_archive(self):
        with mock.patch.object(media.urllib.request, "urlopen", return_value=io.BytesIO(b"broken")):
            with self.assertRaisesRegex(RuntimeError, "무결성"):
                media.ensure_media_tools(cache_root=self.root)
        self.assertFalse((self.root / media.ARCHIVE_NAME).exists())
        self.assertFalse(list(self.root.glob("*.part")))

    def test_concurrent_installers_share_one_download(self):
        start = threading.Barrier(2)
        def install():
            start.wait(timeout=2)
            return media.ensure_media_tools(cache_root=self.root)
        with mock.patch.object(media.urllib.request, "urlopen", side_effect=lambda *a, **k: io.BytesIO(self.archive)) as network:
            with ThreadPoolExecutor(max_workers=2) as workers:
                first, second = workers.submit(install), workers.submit(install)
                self.assertEqual(first.result(timeout=5), second.result(timeout=5))
            self.assertEqual(network.call_count, 1)

    def test_missing_archive_member_preserves_existing_generation(self):
        self.cached_archive()
        installed = media.ensure_media_tools(cache_root=self.root)
        (installed / "ffprobe.exe").write_bytes(b"previous")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../../ffprobe.exe", "invalid path")
        data = archive.getvalue()
        (self.root / media.ARCHIVE_NAME).write_bytes(data)
        with mock.patch.multiple(media, ARCHIVE_SIZE=len(data), ARCHIVE_SHA256=hashlib.sha256(data).hexdigest()):
            with self.assertRaisesRegex(RuntimeError, "필수 항목"):
                media.ensure_media_tools(cache_root=self.root)
        self.assertEqual((installed / "ffprobe.exe").read_bytes(), b"previous")

    def test_cached_install_cleans_only_abandoned_owned_temporary_paths(self):
        self.cached_archive()
        installed = media.ensure_media_tools(cache_root=self.root)
        staging = Path(tempfile.mkdtemp(prefix=".ffmpeg-install-", dir=self.root))
        (staging / "partial.exe").write_bytes(b"unfinished")
        partial = self.root / f"{media.ARCHIVE_NAME}.{uuid.uuid4().hex}.part"
        partial.write_bytes(b"unfinished download")
        unrelated = self.root / "user.part"
        unrelated.write_bytes(b"keep")
        backup = self.root / f".ffmpeg-damaged-{uuid.uuid4().hex}"
        backup.mkdir()
        with mock.patch.object(media.urllib.request, "urlopen", side_effect=AssertionError("offline")):
            self.assertEqual(media.ensure_media_tools(cache_root=self.root), installed)
        self.assertFalse(staging.exists())
        self.assertFalse(partial.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(backup.exists())
        self.assertTrue((self.root / media.ARCHIVE_NAME).exists())
        self.assertTrue((installed / "ffmpeg.exe").exists())

    def test_cleanup_refuses_nested_junctions(self):
        staging = Path(tempfile.mkdtemp(prefix=".ffmpeg-install-", dir=self.root))
        junction = staging / "external"
        junction.mkdir()
        sentinel = junction / "untouched.txt"
        sentinel.write_bytes(b"keep")
        original = Path.is_junction
        with mock.patch.object(Path, "is_junction", lambda path: path == junction or original(path)):
            media._remove_abandoned_downloads(self.root)
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_cleanup_refuses_a_top_level_symlink(self):
        partial = self.root / f"{media.ARCHIVE_NAME}.{uuid.uuid4().hex}.part"
        partial.write_bytes(b"keep")
        original = Path.is_symlink
        with mock.patch.object(Path, "is_symlink", lambda path: path == partial or original(path)):
            media._remove_abandoned_downloads(self.root)
        self.assertEqual(partial.read_bytes(), b"keep")

    def test_full_pair_never_downloads(self):
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            (self.root / name).touch()
        with mock.patch.object(paths, "_ffmpeg_directory_candidates", return_value=[self.root]), mock.patch.object(
            media, "ensure_media_tools", side_effect=AssertionError("Full must not download")
        ):
            self.assertEqual(paths.resolve_media_tools(download=True).ffmpeg, self.root / "ffmpeg.exe")

    def test_lite_missing_pair_downloads_only_when_requested(self):
        with mock.patch.object(paths, "_ffmpeg_directory_candidates", return_value=[]), mock.patch.object(
            paths.shutil, "which", return_value=None
        ), mock.patch.object(media, "ensure_media_tools", return_value=self.root) as installer:
            with self.assertRaises(FileNotFoundError):
                paths.resolve_media_tools()
            installer.assert_not_called()
            result = paths.resolve_media_tools(download=True)
            self.assertEqual(result.ffprobe, self.root / "ffprobe.exe")
            installer.assert_called_once()
