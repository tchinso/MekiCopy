from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from build_runtime_data import ssl_runtime_data


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
