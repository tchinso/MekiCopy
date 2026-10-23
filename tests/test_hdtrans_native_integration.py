"""Opt-in Windows tests against the verified official DLL, never a mock.

HYTRANS_NATIVE_TEST_INSTALLATION must point at a verified installation. The
suite copies its DLL/data into a temporary directory and leaves user settings
and the reusable install untouched.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from hytrans.hdtrans_backend import HdTransBackend
from hytrans.hdtrans_download import VERSION
from hytrans.hdtrans_settings import HdTransSettings


@unittest.skipUnless(os.name == "nt" and os.environ.get("HYTRANS_NATIVE_TEST_INSTALLATION"), "requires opt-in official Windows DLL")
class RealHdTransTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="meki-hdtrans-native-")
        self.root = Path(self.temporary.name)
        installation = Path(os.environ["HYTRANS_NATIVE_TEST_INSTALLATION"])
        shutil.copy2(installation / "hdTransEngine.dll", self.root)
        shutil.copytree(installation / "hdTrans", self.root / "hdTrans")
        self.save = mock.patch("hytrans.hdtrans_backend.save_hdtrans_settings", return_value=True)
        self.save.start()
        self.backend = HdTransBackend(settings=HdTransSettings(), installer=lambda **kwargs: self.root)
        await self.backend.submit("initialize")

    async def asyncTearDown(self):
        await self.backend.close()
        self.save.stop()
        self.temporary.cleanup()

    async def translate(self, text, owner="test"):
        return await self.backend.submit("translate", text, owner, False)

    async def test_utf8_translation_version_and_native_ready(self):
        self.assertEqual(self.backend.status()["version"], VERSION)
        self.assertTrue(self.backend.status()["ready"])
        self.assertFalse(self.backend.status()["workerConnected"])
        self.assertTrue(await self.translate("今日はいい天気ですね。"))
        for _ in range(100):
            self.assertTrue(await self.translate("日本語の翻訳テストです。"))

    async def test_context_on_off_manual_reset_and_work_boundary(self):
        baseline = await self.translate("お兄様！")
        await self.translate("「あたしが行くわよ」")
        contextual = await self.translate("お兄様！")
        self.assertNotEqual(contextual, baseline, (baseline, contextual))
        await self.backend.submit("reset")
        self.assertEqual(await self.translate("お兄様！"), baseline)
        await self.translate("「あたしが行くわよ」")
        self.assertEqual(await self.translate("お兄様！", owner="subtitle:new-file"), baseline)
        await self.backend.submit("settings", HdTransSettings(False))
        await self.translate("「あたしが行くわよ」")
        self.assertEqual(await self.translate("お兄様！"), baseline)

    async def test_multiple_user_dictionaries_errors_reload_and_context_reset(self):
        first = self.root / "first.tsv"
        second = self.root / "second.tsv"
        invalid = self.root / "invalid.tsv"
        first.write_text("蒼月\t메키사전첫째\n", encoding="utf-8")
        second.write_text("蒼月\t메키사전둘째\n", encoding="utf-8")
        invalid.write_text("bad line without a tab\n", encoding="utf-8")
        await self.backend.submit("settings", HdTransSettings(True, [str(first), str(second), str(invalid)]))
        self.assertIn("메키사전둘째", await self.translate("蒼月"))
        self.assertIn("invalid.tsv", self.backend.status()["userDictionaryError"])
        self.assertTrue(self.backend.status()["ready"])
        baseline = await self.translate("お兄様！")
        await self.translate("「あたしが行くわよ」")
        self.assertNotEqual(await self.translate("お兄様！"), baseline)
        second.write_text("蒼月\t메키변경\n", encoding="utf-8")
        await self.backend.submit("reload")
        self.assertEqual(await self.translate("お兄様！"), baseline)
        self.assertIn("메키변경", await self.translate("蒼月"))

    async def test_real_concurrent_requests_serialized(self):
        results = await asyncio.gather(*(self.translate(f"日本語のテスト{i}です。") for i in range(5)))
        self.assertEqual(len(results), 5)
        self.assertTrue(all(results))


if __name__ == "__main__":
    unittest.main()
