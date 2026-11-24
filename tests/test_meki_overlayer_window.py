from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

from meki_overlayer import OverlayConfig, OverlayerApp


class OverlayerConfigWindowTests(unittest.TestCase):
    def app(self) -> OverlayerApp:
        from mekicopy_theme import ensure_application

        ensure_application()
        with mock.patch("meki_overlayer.TopmostWindowController"):
            app = OverlayerApp(OverlayConfig())
        self.addCleanup(app.deleteLater)
        self.addCleanup(app.hide)
        app.setGeometry(400, 300, 730, 200)
        app._apply_capture_exclusion = mock.Mock()
        return app

    def test_repeated_translation_config_does_not_remap_or_touch_the_window(self) -> None:
        app = self.app()
        with mock.patch.object(app, "setWindowFlag") as flags, mock.patch.object(app.label, "setFont") as font:
            for _ in range(100):
                app.config.update_from_dict(asdict(app.config))
                app.apply_config()
        flags.assert_not_called()
        font.assert_not_called()
        app._apply_capture_exclusion.assert_not_called()

    def test_text_appearance_change_updates_label_without_remapping(self) -> None:
        app = self.app()
        with mock.patch.object(app, "setWindowFlag") as flags:
            app.config.update_from_dict({"text_size": 32})
            app.apply_config()
        self.assertEqual(app.label.font().pointSize(), 32)
        self.assertTrue(app.label.font().bold())
        flags.assert_not_called()
        app._apply_capture_exclusion.assert_not_called()

    def test_titlebar_change_preserves_user_position_and_size(self) -> None:
        from PySide6.QtCore import Qt

        app = self.app()
        app.show()
        previous = app.geometry()
        app.config.update_from_dict({"hide_titlebar": True})
        app.apply_config()
        self.assertEqual(app.geometry(), previous)
        self.assertTrue(app.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(app.isVisible())
        app._apply_capture_exclusion.assert_called_once_with()


@unittest.skipUnless(os.name == "nt", "requires native Windows foreground events")
class NativeOverlayerWindowTests(unittest.TestCase):
    def test_foreground_events_and_translation_updates_keep_process_and_geometry(self) -> None:
        # Isolate native aborts so the test runner can report a failure.
        script = textwrap.dedent("""\
            import faulthandler
            import json
            from mekicopy_theme import ensure_application
            import meki_overlayer
            from win32_overlay import TopmostWindowController, get_top_level_hwnd
            faulthandler.enable()
            class CountingController(TopmostWindowController):
                events_seen = 0
                def _on_win_event(self, *args):
                    self.events_seen += 1
                    super()._on_win_event(*args)
            meki_overlayer.TopmostWindowController = CountingController
            application = ensure_application()
            app = meki_overlayer.OverlayerApp(meki_overlayer.OverlayConfig())
            app.setGeometry(400, 300, 730, 200)
            app.show()
            application.processEvents()
            print(json.dumps({'hwnd': get_top_level_hwnd(app)}), flush=True)
            count = 0
            def tick():
                global count
                count += 1
                app.enqueue_config({'topmost': True, 'text_font': 'Malgun Gothic'})
                app.enqueue_text('translation ' + str(count))
                if count >= 120:
                    print(json.dumps({'geometry': app.geometry().getRect(),
                        'events': app._topmost_controller.events_seen}), flush=True)
                    app._topmost_controller.close()
                    application.exit(0)
                else:
                    app.after(20, tick)
            app.after(100, tick)
            application.exec()
            """)
        process = subprocess.Popen(
            [sys.executable, "-X", "faulthandler", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        try:
            ready = process.stdout.readline()
            self.assertTrue(ready, "overlay exited before publishing its native HWND")
            hwnd = json.loads(ready)["hwnd"]
            self.assertTrue(hwnd)
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            create_window = user32.CreateWindowExW
            create_window.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
            create_window.restype = wintypes.HWND
            # A message-only stock window belongs to the producer process and
            # does not move focus or put another visible test window on screen.
            producer_hwnd = create_window(0, "STATIC", "overlay regression producer",
                0, 0, 0, 0, 0, wintypes.HWND(-3), None, None, None)
            self.assertTrue(producer_hwnd, ctypes.get_last_error())
            destroy_window = user32.DestroyWindow
            destroy_window.argtypes = [wintypes.HWND]
            destroy_window.restype = wintypes.BOOL
            notify = user32.NotifyWinEvent
            notify.argtypes = [wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG]
            notify.restype = None
            try:
                for _ in range(40):
                    # SKIPOWNPROCESS checks the source window's owning process.
                    notify(0x0003, producer_hwnd, 0, 0)
                    time.sleep(0.02)
            finally:
                destroy_window(producer_hwnd)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            result = json.loads(stdout.strip().splitlines()[-1])
            self.assertGreaterEqual(result["events"], 20)
            self.assertEqual(result["geometry"], [400, 300, 730, 200])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
