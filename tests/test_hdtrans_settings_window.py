from __future__ import annotations

import io
import gc
import json
import tempfile
import tkinter as tk
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import hdtrans_settings_window as ui
from hytrans.hdtrans_settings import HdTransSettings


class ImmediateTasks:
    """Deliver mocked service results on the Tk thread without real network."""
    def __init__(self, root):
        pass

    def submit(self, key, operation, *, on_success, on_error):
        try:
            result = operation()
        except Exception as exc:
            on_error(exc)
        else:
            on_success(result)
        return True

    def is_running(self, key):
        return False

    def close(self):
        pass


def http_error(detail: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://localhost/hdtrans/settings", 409, "Conflict", {},
                                  io.BytesIO(json.dumps({"detail": detail}).encode()))


class NativeRequestTests(unittest.TestCase):
    def test_http_detail_is_readable_and_cannot_trigger_offline_fallback(self):
        with mock.patch.object(ui, "_json_request", side_effect=http_error("hdTrans is not the selected engine")):
            with self.assertRaisesRegex(RuntimeError, "409: hdTrans is not the selected engine"):
                ui._native_request("url")

    def test_missing_server_stays_an_os_error_for_offline_settings(self):
        with mock.patch.object(ui, "_json_request", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(OSError):
                ui._native_request("url")


class HdTransSettingsUiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self._cleanup_tk)
        self.root._hytrans_base_url = lambda: "http://127.0.0.1:6996"
        self.root.settings = SimpleNamespace(hytrans_port=6996)
        self.root._begin_hytrans_restart = mock.Mock(return_value=False)
        self.root._launch_hytrans = mock.Mock()
        self.status = {"ready": True, "state": "READY", "version": "0.2.1",
                       "dictionaryVersion": "0.2.1+c9ab89c732ad", "userDictionaryError": None}
        self.request = mock.Mock(return_value=self.status)
        for patcher in (
            mock.patch.object(ui, "TkTaskRunner", ImmediateTasks),
            mock.patch.object(ui, "_json_request", self.request),
            mock.patch("hytrans.hdtrans_settings.load_hdtrans_settings", return_value=HdTransSettings()),
            mock.patch("hytrans.hdtrans_download.installation_status", return_value={"installed": True}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = ui.HdTransSettingsWindow(self.root)
        self.window.withdraw()
        self.request.reset_mock()

    def _cleanup_tk(self):
        window = getattr(self, "window", None)
        if window is not None and window.winfo_exists():
            window._close()
        self.window = None
        window = None
        root = getattr(self, "root", None)
        if root is not None:
            root.destroy()
        self.root = None
        root = None
        gc.collect()

    def test_editor_preserves_official_utf8_tabs_and_applies_changed_paths_once(self):
        dictionary = self.directory / "userdict-test.tsv"
        dictionary.write_text("名前\t이름\n", encoding="utf-8")
        self.window.paths = [str(dictionary)]
        self.window._refresh_paths()
        self.window.listbox.selection_set(0)
        self.window._select()
        self.assertEqual(self.window.editor.get("1.0", "end-1c"), "名前\t이름\n")
        self.window.editor.delete("1.0", tk.END)
        self.window.editor.insert("1.0", "灯台\t등대\n")
        self.window.preserve_var.set(False)
        self.window._apply()
        self.assertEqual(dictionary.read_text(encoding="utf-8"), "灯台\t등대\n")
        self.assertFalse(list(self.directory.glob("*.tmp")))
        self.request.assert_called_once()
        call = self.request.call_args
        self.assertTrue(call.args[0].endswith("/hdtrans/settings"))
        self.assertEqual(call.args[1], {"preserveContext": False, "userDictionaries": [str(dictionary)]})
        self.assertFalse(self.window.saved.preserve_context)

    def test_reapply_same_files_requests_reload_and_shows_engine_line_diagnostics(self):
        warning = {**self.status, "userDictionaryError": "userdict-names.tsv:3: missing tab"}
        self.request.side_effect = [self.status, warning]
        self.window._apply()
        self.assertEqual(len(self.request.call_args_list), 2)
        self.assertTrue(self.request.call_args_list[-1].args[0].endswith("/hdtrans/reload"))
        self.assertIn("사용자 사전 진단", self.window.error_var.get())
        self.assertIn(":3: missing tab", self.window.error_var.get())
        self.assertIn("엔진 0.2.1", self.window.status_var.get())

    def test_absent_server_saves_options_for_next_start(self):
        self.request.side_effect = urllib.error.URLError("server absent")
        self.window.preserve_var.set(False)
        with mock.patch("hytrans.hdtrans_settings.save_hdtrans_settings", return_value=True) as save:
            self.window._apply()
        self.assertFalse(save.call_args.args[0].preserve_context)
        self.assertIn("HYTrans 시작 시 적용", self.window.status_var.get())

    def test_live_http_failure_never_saves_offline_or_starts_download(self):
        self.request.side_effect = lambda *args, **kwargs: (_ for _ in ()).throw(http_error("engine is inactive"))
        with mock.patch("hytrans.hdtrans_settings.save_hdtrans_settings") as save, \
             mock.patch("hytrans.hdtrans_download.get_download_manager") as manager:
            self.window._apply()
            self.window._install()
        save.assert_not_called()
        manager.return_value.start.assert_not_called()
        self.assertIn("409: engine is inactive", self.window.error_var.get())

    def test_local_install_uses_manager_contract_then_restarts_server(self):
        self.request.side_effect = urllib.error.URLError("offline")
        manager = mock.Mock()
        manager.status.return_value = {"state": "ready", "ready": True, "busy": False}
        with mock.patch("hytrans.hdtrans_download.get_download_manager", return_value=manager):
            self.window._install()
        manager.start.assert_called_once_with()
        self.root._begin_hytrans_restart.assert_called_once()
        self.root._launch_hytrans.assert_called_once_with(notify=False)

    def test_uninstalled_and_inactive_status_do_not_claim_ready(self):
        self.window._show_result({"installed": False, "ready": False})
        self.assertIn("설치가 필요", self.window.status_var.get())
        self.assertIn("미준비", self.window.status_var.get())
        self.window._show_result({"state": "INACTIVE", "ready": False})
        self.assertIn("메인 설정을 저장", self.window.status_var.get())


if __name__ == "__main__":
    unittest.main()
