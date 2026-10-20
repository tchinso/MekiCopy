from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from hytrans.api_settings import ApiSettings, fingerprint
import mekicopy
from mekicopy_capture import Region
from mekicopy_realtime import RealtimeSession
from mekicopy_settings import AppSettings
import mekicopy_companions as companions
from meki_subtitle_window import MekiSubtitleWindow


class ApiTranslationIntegrationTests(unittest.TestCase):
    def test_manual_translation_dispatch_keeps_settings_from_task_submission(self) -> None:
        for app in (mekicopy.MainWindow, mekicopy.DetachedOcrButtonApp):
            with self.subTest(app=app.__name__):
                settings = AppSettings(
                    hytrans_port=7996, overlayer_port=7997,
                    overlayer_bg_color="#123456", overlayer_hide_titlebar=True,
                    debug_logging=True,
                )
                window = SimpleNamespace(settings=settings, _task_runner=mock.Mock())
                image = object()
                if app is mekicopy.MainWindow:
                    app._start_ocr_task(
                        window, image, source_button=mock.Mock(),
                        simple_feedback=True, translate=True,
                    )
                else:
                    app._start_ocr_task(window, image, translate=True)
                operation = window._task_runner.submit.call_args.args[1]
                window.settings = replace(
                    settings, hytrans_port=8996, overlayer_port=8997,
                    overlayer_bg_color="#abcdef", overlayer_hide_titlebar=False,
                )
                with (
                    mock.patch.object(mekicopy, "recognize_image", return_value="こんにちは") as recognize,
                    mock.patch.object(mekicopy, "detached_translation_locked", return_value=False),
                    mock.patch.object(mekicopy, "request_translation_and_show") as translate,
                ):
                    self.assertEqual(operation(), "こんにちは")
                recognize.assert_called_once_with(image)
                self.assertEqual(translate.call_args.args, ("こんにちは",))
                request = translate.call_args.kwargs
                self.assertEqual(request["hytrans_url"], "http://127.0.0.1:7996")
                self.assertEqual(request["overlayer_url"], "http://127.0.0.1:7997")
                self.assertEqual(request["overlayer_config"]["bg_color"], "#123456")
                self.assertTrue(request["overlayer_config"]["hide_titlebar"])
                if app is mekicopy.MainWindow:
                    self.assertTrue(request["overlayer_config"]["debug_log"])
                else:
                    self.assertNotIn("debug_log", request["overlayer_config"])

    def test_realtime_translation_keeps_config_and_display_contract_from_preparation(self) -> None:
        settings = AppSettings(
            hytrans_port=7996, overlayer_port=7997,
            overlayer_text_color="#123456", debug_logging=True,
        )
        region = Region(10, 20, 100, 80)
        session = RealtimeSession(1)
        window = SimpleNamespace(
            settings=settings, active_region=region, active_monitor=None,
            active_region_ratio=None, _realtime_block_reason=lambda: "",
        )
        operation = mekicopy.MainWindow._prepare_realtime_operation(window, session, None)
        window.settings = replace(
            settings, hytrans_port=8996, overlayer_port=8997,
            overlayer_text_color="#abcdef", debug_logging=False,
        )
        manager = mock.Mock()
        manager.resolve_region.return_value = region, None, None, False
        with (
            mock.patch.object(mekicopy, "_probe_service"),
            mock.patch.object(mekicopy, "get_capture_manager", return_value=manager),
            mock.patch.object(mekicopy, "capture_ocr_image", return_value=object()),
            mock.patch.object(mekicopy, "recognize_image", return_value="こんにちは"),
            mock.patch.object(
                mekicopy, "request_translation_and_show",
                return_value={"displayedMonotonic": 123.0},
            ) as translate,
        ):
            result = operation()
        self.assertEqual(result.displayed_at, 123.0)
        self.assertEqual(result.capture_target, (region, None, None))
        request = translate.call_args.kwargs
        self.assertEqual(request["hytrans_url"], "http://127.0.0.1:7996")
        self.assertEqual(request["overlayer_url"], "http://127.0.0.1:7997")
        self.assertEqual(request["overlayer_config"]["text_color"], "#123456")
        self.assertTrue(request["overlayer_config"]["debug_log"])
        self.assertTrue(request["wait_for_display"])
        self.assertEqual(request["context_owner"], session.owner)
        self.assertTrue(request["request_id"].startswith(session.owner + ":"))
        self.assertFalse(request["is_cancelled"]())
        session.cancelled.set()
        self.assertTrue(request["is_cancelled"]())

    def test_service_probe_accepts_api_readiness_without_a_browser(self) -> None:
        health = {"ok": True, "app": "HYTrans", "state": "READY", "backend": "groq"}
        ready = {"ready": True, "workerConnected": False, "backend": "groq", "model": "custom-model"}
        with mock.patch.object(companions, "_json_request", side_effect=[health, ready]):
            detail = companions._probe_service("HYTrans", "http://127.0.0.1:6996")
        self.assertIn("API=groq", detail)
        self.assertIn("custom-model", detail)

    def test_service_probe_still_requires_local_worker(self) -> None:
        health = {"ok": True, "app": "HYTrans", "state": "READY"}
        with mock.patch.object(companions, "_json_request", side_effect=[health, {"ready": True}]):
            with self.assertRaisesRegex(RuntimeError, "HYTransWorker"):
                companions._probe_service("HYTrans", "http://127.0.0.1:6996")

    def test_subtitle_accepts_api_readiness_without_reopening_worker(self) -> None:
        window = SimpleNamespace(
            _hytrans_url=lambda: "http://127.0.0.1:6996",
            _cancel_event=threading.Event(),
            _HYTRANS_READY_TIMEOUT_SECONDS=600,
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"ready": True, "workerConnected": False, "backend": "cloudflare"}
        ).encode()
        with mock.patch("urllib.request.urlopen", return_value=response) as open_url:
            self.assertEqual(MekiSubtitleWindow._wait_for_hytrans_ready(window), "http://127.0.0.1:6996")
        self.assertEqual(open_url.call_count, 1)

    def test_subtitle_reports_missing_api_settings_immediately(self) -> None:
        window = SimpleNamespace(
            _hytrans_url=lambda: "http://127.0.0.1:6996",
            _cancel_event=threading.Event(),
            _HYTRANS_READY_TIMEOUT_SECONDS=600,
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"ready": False, "backend": "groq", "state": "ERROR", "error": "API key required"}
        ).encode()
        with mock.patch("urllib.request.urlopen", return_value=response) as open_url:
            with self.assertRaisesRegex(RuntimeError, "API key required"):
                MekiSubtitleWindow._wait_for_hytrans_ready(window)
        self.assertEqual(open_url.call_count, 1)

    def test_launch_passes_only_provider_and_config_path(self) -> None:
        window = SimpleNamespace(
            hytrans_process=None,
            settings=AppSettings(hytrans_backend="groq"),
            _overlayer_show_url=lambda: "http://127.0.0.1:6997/show",
            _hytrans_base_url=lambda: "http://127.0.0.1:6996",
            _watch_started_process=mock.Mock(),
            _track_owned_companion=mock.Mock(),
        )
        with (
            mock.patch.object(mekicopy, "_is_process_alive", return_value=False),
            mock.patch.object(mekicopy, "_find_companion_executable", return_value=["HYTrans.exe"]),
            mock.patch.object(mekicopy, "api_settings_path", return_value=Path("C:/settings/translation_api.json")),
            mock.patch.object(mekicopy, "_startupinfo_for_background"),
            mock.patch.object(mekicopy, "_log_runtime_message"),
            mock.patch.object(mekicopy.subprocess, "Popen") as spawn,
        ):
            self.assertTrue(mekicopy.MainWindow._launch_hytrans(window, notify=False))
        command = spawn.call_args.args[0]
        self.assertEqual(command[command.index("--backend") + 1], "groq")
        self.assertEqual(command[command.index("--api-config") + 1], str(Path("C:/settings/translation_api.json")))
        self.assertNotIn("--api-key", command)

    @staticmethod
    def _settings_window(settings: AppSettings, api: ApiSettings) -> SimpleNamespace:
        window = SimpleNamespace(
            settings=settings,
            settings_window=None,
            _hytrans_config_fingerprint=fingerprint(api, settings.hytrans_backend),
            _global_hotkey_paused=False,
            _global_hotkey_configuration=lambda value: (value.global_hotkey_enabled, value.global_hotkey),
            _configure_global_hotkey=mock.Mock(return_value=(True, "")),
            subtitle_window=None,
            attributes=mock.Mock(),
            _apply_overlay_mode_ui=mock.Mock(),
            _sync_realtime_ocr=mock.Mock(),
            _send_overlayer_config=mock.Mock(),
            _send_script_config=mock.Mock(),
            _send_audio_capture_config=mock.Mock(),
            _send_hytrans_logging_config=mock.Mock(),
            _begin_hytrans_restart=mock.Mock(return_value=True),
        )
        return window

    def test_api_prompt_only_change_restarts_shared_service(self) -> None:
        settings = AppSettings(hytrans_backend="groq")
        old = ApiSettings()
        old.profiles["groq"].api_key = "fake-test-secret"
        draft = deepcopy(old)
        draft.profiles["groq"].prompt = "번역만 출력: {text}"
        window = self._settings_window(settings, old)
        with (
            mock.patch.object(mekicopy, "load_api_settings", return_value=old),
            mock.patch.object(mekicopy, "save_settings", return_value=True),
            mock.patch.object(mekicopy, "save_api_settings", return_value=True),
            mock.patch.object(mekicopy, "set_debug_enabled"),
        ):
            result = mekicopy.MainWindow.apply_settings(window, replace(settings), True, api_settings=draft)
        self.assertTrue(result)
        window._begin_hytrans_restart.assert_called_once_with(6996, "api-config")
        self.assertEqual(window._hytrans_config_fingerprint, fingerprint(draft, "groq"))

    def test_failed_api_save_preserves_active_settings_and_rolls_back_file(self) -> None:
        previous = AppSettings()
        api = ApiSettings()
        api.profiles["groq"].api_key = "fake-test-secret"
        window = self._settings_window(previous, api)
        settings = replace(previous, hytrans_backend="groq")
        with (
            mock.patch.object(mekicopy, "load_api_settings", return_value=api),
            mock.patch.object(mekicopy, "save_settings", return_value=True) as save,
            mock.patch.object(mekicopy, "save_api_settings", return_value=False),
            mock.patch.object(mekicopy.messagebox, "showerror"),
        ):
            result = mekicopy.MainWindow.apply_settings(window, settings, True, api_settings=api)
        self.assertIsNone(result)
        self.assertIs(window.settings, previous)
        self.assertEqual(save.call_args_list, [mock.call(settings), mock.call(previous)])
        window._begin_hytrans_restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
