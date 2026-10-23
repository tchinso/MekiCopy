from __future__ import annotations

import unittest
from unittest import mock

import hytrans_main
from hytrans.api_selftest import run_api_self_test


class HytransSelfTestTests(unittest.TestCase):
    def test_offline_api_selftest_checks_all_providers(self):
        result = run_api_self_test()
        self.assertTrue(result["ok"])
        self.assertTrue(result["offline"])
        self.assertEqual(set(result["providers"]), {"cloudflare", "deepinfra", "groq"})

    def test_windowless_selftest_failures_are_logged(self):
        for kind, module in (("api", "api_selftest"), ("hdtrans", "hdtrans_selftest")):
            failure = RuntimeError("selftest diagnostic failure")
            with self.subTest(kind=kind), mock.patch.object(
                hytrans_main.sys, "argv", ["HYTrans.exe", f"--self-test-{kind}"]
            ), mock.patch.object(hytrans_main, "set_windows_app_id"), mock.patch.object(
                hytrans_main.sys, "stdout", None
            ), mock.patch.object(hytrans_main.sys, "stderr", None), mock.patch.object(
                hytrans_main, "error"
            ) as log, mock.patch(f"hytrans.{module}.run_{kind}_self_test", side_effect=failure):
                self.assertEqual(hytrans_main.main(), 1)
                log.assert_called_once_with(f"self_test_{kind}", failure)

    def test_windowless_selftest_success_returns_zero(self):
        with mock.patch.object(hytrans_main.sys, "argv", ["HYTrans.exe", "--self-test-api"]), mock.patch.object(
            hytrans_main, "set_windows_app_id"
        ), mock.patch.object(hytrans_main.sys, "stdout", None), mock.patch(
            "hytrans.api_selftest.run_api_self_test", return_value={"ok": True}
        ):
            self.assertEqual(hytrans_main.main(), 0)
