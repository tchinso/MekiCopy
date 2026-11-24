from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from build_runtime_data import ssl_runtime_data, without_windows_icu


class SslRuntimeDataTests(unittest.TestCase):
    def test_patched_interpreter_policy_is_collected_at_frozen_module_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = root / "codex-rejected-cas.pem"
            policy.write_text("policy", encoding="ascii")
            self.assertEqual(ssl_runtime_data(root / "ssl.py"), [(str(policy.resolve()), ".")])

    def test_standard_interpreter_needs_no_policy_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(ssl_runtime_data(Path(temporary) / "ssl.py"), [])

    def test_policy_directory_is_not_a_bundle_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "codex-rejected-cas.pem").mkdir()
            self.assertEqual(ssl_runtime_data(root / "ssl.py"), [])


class QtRuntimeDataTests(unittest.TestCase):
    def test_external_icu_cannot_shadow_the_windows_runtime(self):
        qt = (r"PySide6\Qt6Core.dll", "qt/Qt6Core.dll", "BINARY")
        binaries = [
            ("ICUUC.dll", "poppler/icuuc.dll", "BINARY"),
            ("icudt78.dll", "poppler/icudt78.dll", "BINARY"),
            qt,
        ]
        self.assertEqual(without_windows_icu(binaries), [qt])
