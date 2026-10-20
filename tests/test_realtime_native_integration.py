"""Opt-in native payload test; no download, user files or cloud credentials.

OCR strings and HYTrans's socket transport are mocked. The overlay uses the
production HTTP handler and client. The Windows DLL,
FastAPI routes, native queue, TkTaskRunner, realtime scheduler, overlay HTTP
server, Tk label redraw and display acknowledgement all execute for real.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock


@unittest.skipUnless(os.name == "nt", "requires the actual Windows hdTrans DLL")
class NativeRealtimeIntegrationTests(unittest.TestCase):
    def test_real_native_translation_tk_ack_dedup_and_late_result_cancellation(self):
        installed = (Path(__file__).resolve().parents[1] / ".cache" / "hdtrans" / "full"
                     / "installations" / "0.2.1-windows-x64")
        if not (installed / "hdTransEngine.dll").is_file():
            self.skipTest("validated local hdTrans v0.2.1 payload is not present; no downloads in tests")
        import tkinter as tk
        from http.server import ThreadingHTTPServer
        from urllib.parse import urlsplit
        from fastapi.testclient import TestClient
        from hytrans import app as hytrans_server
        from hytrans.hdtrans_backend import HdTransBackend, NativeLibrary
        from hytrans.hdtrans_settings import HdTransSettings
        from hytrans.state import AppState
        from meki_overlayer import OverlayConfig, OverlayerApp, make_handler
        import mekicopy_companions as companions
        from mekicopy_realtime import RealtimeOcrController, perform_cycle
        from mekicopy_tasks import TkTaskRunner

        first = "今日はとてもいい天気ですね。"
        second = "明日は雨になるかもしれません。"
        obsolete = "この結果は表示しないでください。"
        calls, delivered, errors, requests = [], [], [], []
        translations = {}
        native_started, native_release, native_completed = (threading.Event() for _ in range(3))

        class RecordingNative(NativeLibrary):
            def translate(self, text):
                calls.append(text)
                translated = super().translate(text)
                translations[text] = translated.strip()
                if text == obsolete:
                    native_started.set()
                    if not native_release.wait(5):
                        raise RuntimeError("test did not release the native operation")
                    native_completed.set()
                return translated

        with tempfile.TemporaryDirectory(prefix="mekicopy-native-ocr-") as directory:
            isolated = Path(directory)
            (isolated / "hdTrans").mkdir()
            shutil.copy2(installed / "hdTransEngine.dll", isolated / "hdTransEngine.dll")
            snapshot = installed / "hdTrans" / "dicts.hdsnap"
            try:
                os.link(snapshot, isolated / "hdTrans" / "dicts.hdsnap")
            except OSError:
                shutil.copy2(snapshot, isolated / "hdTrans" / "dicts.hdsnap")
            backend = HdTransBackend(settings=HdTransSettings(preserve_context=True),
                                     installer=lambda **kwargs: isolated,
                                     library_factory=RecordingNative)
            try:
                asyncio.run(backend.submit("initialize", timeout=30))
                self.assertTrue(backend.status()["ready"])
                root = tk.Tk()
                overlay = OverlayerApp(root, OverlayConfig(topmost=False))
                server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(overlay))
                server.daemon_threads = True
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                runner = TkTaskRunner(root)
                overlay_url = f"http://127.0.0.1:{server.server_port}"
                overlay_request = companions._json_request

                source = iter((first, first, second))
                client = TestClient(hytrans_server.app, client=("127.0.0.1", 12345))
                try:
                    self.assertTrue(overlay_request(overlay_url + "/health")["ok"])
                    with (mock.patch.object(hytrans_server.options, "backend", "hdtrans"),
                          mock.patch.object(hytrans_server, "hdtrans_backend", backend),
                          mock.patch.object(hytrans_server, "state", AppState())):

                        def dispatch(url, payload=None, *, timeout=3, method=None):
                            requests.append((method or "GET", url))
                            if url.startswith("http://hytrans.test"):
                                path = urlsplit(url).path
                                if urlsplit(url).query:
                                    path += "?" + urlsplit(url).query
                                response = client.request(method or ("POST" if payload is not None else "GET"),
                                                          path, json=payload)
                                response.raise_for_status()
                                return response.json()
                            return overlay_request(url, payload, timeout=timeout, method=method)

                        def prepare(session, previous):
                            return lambda: perform_cycle(
                                lambda: next(source), session=session, previous=previous,
                                translate=lambda text: companions.request_translation_and_show(
                                    text, hytrans_url="http://hytrans.test", overlayer_url=overlay_url,
                                    request_id=f"{session.owner}:{time.monotonic_ns()}",
                                    is_cancelled=session.cancelled.is_set, wait_for_display=True,
                                    context_owner=session.owner,
                                ),
                            )

                        def success(result):
                            delivered.append(result)
                            return True

                        controller = RealtimeOcrController(root, runner, prepare_operation=prepare,
                                                          on_result=success, on_error=errors.append)
                        with mock.patch.object(companions, "_json_request", side_effect=dispatch):
                            self.assertFalse(client.get("/ready").json()["workerConnected"])
                            controller.configure(enabled=True, interval=1, translate=True)
                            deadline = time.monotonic() + 15
                            while len(delivered) < 2 and not errors and time.monotonic() < deadline:
                                root.update()
                                time.sleep(0.005)
                            controller.configure(enabled=False, interval=1, translate=True)
                            self.assertFalse(errors, (errors, requests, calls))
                            self.assertEqual(calls, [first, second])
                            self.assertEqual(len(delivered), 2)
                            self.assertTrue(all(result.displayed_at for result in delivered))
                            self.assertEqual(overlay.label.cget("text"), translations[second])
                            old_label = overlay.label.cget("text")
                            source = iter((obsolete,))
                            controller.configure(enabled=True, interval=1, translate=True,
                                                 fingerprint="cancellation session")
                            deadline = time.monotonic() + 8
                            while not native_started.is_set() and time.monotonic() < deadline:
                                root.update()
                                time.sleep(0.005)
                            self.assertTrue(native_started.is_set())
                            controller.configure(enabled=False, interval=1, translate=True)
                            native_release.set()
                            deadline = time.monotonic() + 5
                            while runner.is_running("main_ocr") and time.monotonic() < deadline:
                                root.update()
                                time.sleep(0.005)
                            self.assertTrue(native_completed.is_set())
                            self.assertEqual(overlay.label.cget("text"), old_label)
                            self.assertEqual(len(delivered), 2)
                            self.assertIsNone(controller.previous)
                            controller.close()
                finally:
                    native_release.set()
                    client.close()
                    runner.close()
                    if overlay._topmost_controller:
                        overlay._topmost_controller.close()
                    server.shutdown()
                    server.server_close()
                    root.destroy()
            finally:
                native_release.set()
                asyncio.run(backend.close())


if __name__ == "__main__":
    unittest.main()
