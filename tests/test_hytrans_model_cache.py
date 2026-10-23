from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from fastapi import HTTPException

from hytrans import model_cache
from hytrans.model_files import MT15_PROFILE, ModelFileSpec


class CacheUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_content_length_returns_client_error(self):
        request = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            model_cache, "model_dir", return_value=Path(directory)
        ):
            for value in ("invalid", "-1"):
                request.headers = {"content-length": value}
                with self.subTest(value=value), self.assertRaises(HTTPException) as raised:
                    await model_cache.save_model_cache_file(
                        f"/models/{MT15_PROFILE.model_id}/config.json", request
                    )
                self.assertEqual(raised.exception.status_code, 400)

    async def test_cancelled_upload_waits_for_disk_write_before_closing_handle(self):
        payload = b"model"
        profile = replace(MT15_PROFILE, files={
            "config.json": ModelFileSpec(len(payload), hashlib.sha256(payload).hexdigest())
        })
        started, release = threading.Event(), threading.Event()
        handles = []

        def write(handle, digest, chunk):
            handles.append(handle)
            started.set()
            if not release.wait(2):
                raise RuntimeError("write was not released")
            handle.write(chunk)
            digest.update(chunk)

        class Request:
            headers = {}

            async def stream(self):
                yield payload

        with tempfile.TemporaryDirectory() as directory, mock.patch.multiple(
            model_cache,
            active_model_profile=mock.Mock(return_value=profile),
            model_dir=mock.Mock(return_value=Path(directory)),
            is_verified_model_file=mock.Mock(return_value=False),
            _write_and_hash_chunk=write,
        ):
            task = asyncio.create_task(model_cache.save_model_cache_file(
                f"/models/{profile.model_id}/config.json", Request()
            ))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(handles[0].closed)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(handles[0].closed)
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                release.set()

