from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"


class RuntimeAssetManifestTests(unittest.TestCase):
    def test_manifest_versions_and_file_hashes(self) -> None:
        manifest = json.loads((ASSETS / "runtime_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["transformers"]["version"], "4.2.0")
        self.assertEqual(manifest["transformers"]["buildOverride"]["onnxRuntimeWeb"], "1.27.0")
        self.assertEqual(manifest["onnxRuntimeWeb"]["version"], "1.27.0")

        for entry in manifest["files"]:
            path = ASSETS / entry["path"]
            self.assertTrue(path.is_file(), entry["path"])
            self.assertEqual(path.stat().st_size, entry["size"], entry["path"])
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            self.assertEqual(digest.hexdigest(), entry["sha256"], entry["path"])

    def test_custom_bundle_contains_latest_ort(self) -> None:
        bundle = (ASSETS / "transformers.min.js").read_bytes()
        self.assertIn(b'1.27.0', bundle)
        self.assertNotIn(b'1.26.0-dev.20260416-b7804b056c', bundle)


if __name__ == "__main__":
    unittest.main()
