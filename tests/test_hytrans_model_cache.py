from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from hytrans import model_cache
from hytrans.model_files import MODEL_FILE_SPECS, ModelFileSpec, is_verified_model_file


class FakeRequest:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.headers = {"content-length": str(len(body))}

    async def stream(self):
        midpoint = max(1, len(self.body) // 2)
        yield self.body[:midpoint]
        yield self.body[midpoint:]


class ModelCacheTests(unittest.TestCase):
    url = (
        "https://huggingface.co/onnx-community/HY-MT1.5-1.8B-ONNX/"
        "resolve/test-revision/config.json"
    )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.models_root = Path(self.temporary.name)
        self.model_root = (
            self.models_root / "onnx-community" / "HY-MT1.5-1.8B-ONNX"
        )
        self.body = "정상 모델 파일".encode("utf-8")
        self.spec = ModelFileSpec(
            size=len(self.body),
            sha256=hashlib.sha256(self.body).hexdigest(),
        )
        self.models_patch = patch.object(
            model_cache,
            "models_dir",
            return_value=self.models_root,
        )
        self.models_patch.start()
        self.spec_patch = patch.dict(
            MODEL_FILE_SPECS,
            {"config.json": self.spec},
            clear=True,
        )
        self.spec_patch.start()

    def tearDown(self) -> None:
        self.spec_patch.stop()
        self.models_patch.stop()
        self.temporary.cleanup()

    def test_complete_upload_is_verified_and_reused(self) -> None:
        first = asyncio.run(
            model_cache.save_model_cache_file(self.url, FakeRequest(self.body))
        )
        self.assertEqual(first["bytes"], len(self.body))
        self.assertTrue(is_verified_model_file(self.model_root, "config.json"))
        self.assertTrue(model_cache.model_cache_status(self.url)["exists"])

        second = asyncio.run(
            model_cache.save_model_cache_file(self.url, FakeRequest(b"ignored"))
        )
        self.assertTrue(second["reused"])
        self.assertEqual(
            (self.model_root / "config.json").read_bytes(),
            self.body,
        )

    def test_partial_upload_is_rejected_without_publishing_file(self) -> None:
        with self.assertRaises(HTTPException):
            asyncio.run(
                model_cache.save_model_cache_file(
                    self.url,
                    FakeRequest(self.body[:-1]),
                )
            )
        self.assertFalse((self.model_root / "config.json").exists())
        self.assertFalse(model_cache.model_cache_status(self.url)["exists"])

    def test_same_sized_corrupt_file_is_not_reused(self) -> None:
        target = self.model_root / "config.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x" * len(self.body))

        status = model_cache.model_cache_status(self.url)

        self.assertFalse(status["exists"])
        self.assertTrue(status["invalid"])


if __name__ == "__main__":
    unittest.main()
