from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

import mekicopy
from mekicopy_capture import Region
from mekicopy_settings import AppSettings


class TranslationActionTests(unittest.TestCase):
    def test_unified_button_dispatches_to_selected_translation_mode(self) -> None:
        for realtime in (False, True):
            with self.subTest(realtime=realtime):
                window = SimpleNamespace(
                    settings=AppSettings(realtime_ocr_enabled=realtime),
                    _workflow_mode="screen", ocr_button=mock.Mock(),
                    _on_ocr_copy=mock.Mock(), _on_toggle_realtime_ocr=mock.Mock(),
                )
                mekicopy.MainWindow._on_translation_action(window)
                self.assertEqual(window._on_toggle_realtime_ocr.call_count, int(realtime))
                self.assertEqual(window._on_ocr_copy.call_count, int(not realtime))

    def test_manual_entry_is_blocked_outside_manual_screen_workflow(self) -> None:
        for workflow, realtime in ((None, False), ("audio", False), ("subtitle", False), ("screen", True)):
            with self.subTest(workflow=workflow, realtime=realtime):
                runner = mock.Mock()
                runner.is_running.return_value = False
                window = SimpleNamespace(
                    settings=AppSettings(realtime_ocr_enabled=realtime),
                    _workflow_mode=workflow, _task_runner=runner,
                    _realtime_ocr=SimpleNamespace(active=False, translation_locked=False),
                    active_region=Region(10, 20, 100, 80),
                    _prepare_active_region_for_capture=mock.Mock(return_value=True),
                    _start_ocr_task=mock.Mock(),
                )
                with mock.patch.object(mekicopy, "capture_ocr_image") as capture:
                    self.assertFalse(mekicopy.MainWindow._on_ocr_copy(window))
                capture.assert_not_called()
                window._start_ocr_task.assert_not_called()

    def test_detached_manual_entry_is_blocked_even_before_realtime_starts(self) -> None:
        window = SimpleNamespace(
            settings=AppSettings(realtime_ocr_enabled=True),
            _suppress_next_click=False, _realtime_locked=False,
            _refresh_realtime_lock=mock.Mock(),
        )
        with (
            mock.patch.object(mekicopy, "load_settings", return_value=window.settings),
            mock.patch.object(mekicopy, "load_detached_region") as region,
            mock.patch.object(mekicopy, "capture_ocr_image") as capture,
        ):
            mekicopy.DetachedOcrButtonApp._on_button_command(window)
        region.assert_not_called()
        capture.assert_not_called()

    def test_legacy_copy_flag_always_runs_translation_and_display(self) -> None:
        for app_type in (mekicopy.MainWindow, mekicopy.DetachedOcrButtonApp):
            with self.subTest(app=app_type.__name__):
                runner = mock.Mock()
                runner.submit.return_value = True
                window = SimpleNamespace(
                    settings=AppSettings(realtime_ocr_enabled=False, overlay_translation_mode=False),
                    _task_runner=runner,
                )
                image = object()
                if app_type is mekicopy.MainWindow:
                    app_type._start_ocr_task(
                        window, image, source_button=mock.Mock(),
                        simple_feedback=True, translate=False,
                    )
                else:
                    app_type._start_ocr_task(window, image, translate=False)
                operation = runner.submit.call_args.args[1]
                with (
                    mock.patch.object(mekicopy, "recognize_image", return_value="こんにちは"),
                    mock.patch.object(mekicopy, "detached_translation_locked", return_value=False),
                    mock.patch.object(mekicopy, "request_translation_and_show") as translate,
                ):
                    self.assertEqual(operation(), "こんにちは")
                self.assertEqual(translate.call_args.args, ("こんにちは",))


class WorkflowConnectionTests(unittest.TestCase):
    def test_screen_connection_check_always_checks_both_tools(self) -> None:
        window = SimpleNamespace(
            _hytrans_base_url=lambda: "http://127.0.0.1:5000",
            _overlayer_base_url=lambda: "http://127.0.0.1:6000",
            _overlayer_show_url=lambda: "http://127.0.0.1:6000/show",
            _send_overlayer_config=mock.Mock(),
        )
        for failed_service in (None, "HYTrans", "MekiOverlayer"):
            with self.subTest(failed_service=failed_service):
                def probe(name, _url):
                    if name == failed_service:
                        raise RuntimeError("offline")
                    return "ready"

                with (
                    mock.patch.object(mekicopy, "_probe_service", side_effect=probe) as checked,
                    mock.patch.object(mekicopy, "_json_request"),
                    mock.patch.object(mekicopy.messagebox, "showinfo") as info,
                    mock.patch.object(mekicopy.messagebox, "showerror") as error,
                ):
                    mekicopy.MainWindow._on_test_overlay_connection(window)
                self.assertEqual([call.args[0] for call in checked.call_args_list], ["HYTrans", "MekiOverlayer"])
                self.assertEqual(error.call_count, int(failed_service is not None))
                self.assertEqual(info.call_count, int(failed_service is None))

    def test_screen_connection_check_stops_when_overlay_config_fails(self) -> None:
        parent = object()
        window = SimpleNamespace(
            _hytrans_base_url=lambda: "http://127.0.0.1:5000",
            _overlayer_base_url=lambda: "http://127.0.0.1:6000",
            _overlayer_show_url=lambda: "http://127.0.0.1:6000/show",
            _send_overlayer_config=mock.Mock(return_value=False),
        )
        with (
            mock.patch.object(mekicopy, "_probe_service", return_value="ready"),
            mock.patch.object(mekicopy, "_json_request") as request,
            mock.patch.object(mekicopy.messagebox, "showinfo") as info,
            mock.patch.object(mekicopy.messagebox, "showerror") as error,
        ):
            mekicopy.MainWindow._on_test_overlay_connection(window, parent=parent)
        request.assert_not_called()
        info.assert_not_called()
        error.assert_called_once()
        self.assertIs(error.call_args.kwargs["parent"], parent)

    def test_audio_connection_check_always_checks_all_three_tools(self) -> None:
        window = SimpleNamespace(
            _audio_capture_base_url=lambda: "http://127.0.0.1:7000",
            _hytrans_base_url=lambda: "http://127.0.0.1:5000",
            _script_base_url=lambda: "http://127.0.0.1:6001",
            _send_script_config=mock.Mock(),
            _send_audio_capture_config=mock.Mock(),
        )
        services = ["MekiAudioCapture", "HYTrans", "MekiScript"]
        for failed_service in (None, *services):
            with self.subTest(failed_service=failed_service):
                def probe(name, _url):
                    if name == failed_service:
                        raise RuntimeError("offline")
                    return "ready"

                with (
                    mock.patch.object(mekicopy, "_probe_service", side_effect=probe) as checked,
                    mock.patch.object(mekicopy.messagebox, "showinfo") as info,
                    mock.patch.object(mekicopy.messagebox, "showerror") as error,
                ):
                    mekicopy.MainWindow._on_test_audio_connection(window)
                self.assertEqual([call.args[0] for call in checked.call_args_list], services)
                self.assertEqual(error.call_count, int(failed_service is not None))
                self.assertEqual(info.call_count, int(failed_service is None))


class CompanionActionFeedbackTests(unittest.TestCase):
    @staticmethod
    def _hytrans_window(backend: str) -> SimpleNamespace:
        return SimpleNamespace(
            settings=SimpleNamespace(
                hytrans_backend=backend, hytrans_model_id="test", hytrans_port=5000,
            ),
            _hytrans_restart_after_id=None,
            hytrans_process=object(),
            _hytrans_base_url=lambda: "http://127.0.0.1:5000",
            _send_hytrans_logging_config=mock.Mock(),
            _tracked_process_is_starting=mock.Mock(return_value=False),
            _warn_if_port_busy=mock.Mock(return_value=True),
            _launch_hytrans=mock.Mock(),
            _begin_hytrans_restart=mock.Mock(),
        )

    def test_hytrans_readiness_error_reports_failure_without_launching(self) -> None:
        window = self._hytrans_window("hdtrans")
        health = {"ok": True, "app": "HYTrans", "backend": "hdtrans", "model": "hdTrans"}
        failure = TimeoutError("timed out")
        with (
            mock.patch.object(mekicopy, "get_model_profile", return_value=object()),
            mock.patch.object(mekicopy, "_json_request", side_effect=[health, failure]) as request,
            mock.patch.object(mekicopy, "_log_runtime_error") as log,
            mock.patch.object(mekicopy.messagebox, "showinfo") as info,
            mock.patch.object(mekicopy.messagebox, "showerror") as error,
        ):
            self.assertFalse(mekicopy.MainWindow._on_start_hytrans(window))
        self.assertEqual(request.call_args_list[-1].args[0], "http://127.0.0.1:5000/ready")
        log.assert_called_once_with("check_hytrans_ready", failure)
        error.assert_called_once()
        info.assert_not_called()
        window._launch_hytrans.assert_not_called()
        window._warn_if_port_busy.assert_not_called()
        window._tracked_process_is_starting.assert_not_called()

    def test_hytrans_worker_reopen_error_reports_failure_without_launching(self) -> None:
        window = self._hytrans_window("local")
        profile = SimpleNamespace(model_id="test", dtype="int8")
        health = {
            "ok": True, "app": "HYTrans", "backend": "local", "model": "test", "dtype": "int8",
        }
        failure = ConnectionResetError("connection reset")
        with (
            mock.patch.object(mekicopy, "get_model_profile", return_value=profile),
            mock.patch.object(
                mekicopy, "_json_request",
                side_effect=[health, {"ready": False, "workerConnected": False}, failure],
            ) as request,
            mock.patch.object(mekicopy, "_log_runtime_error") as log,
            mock.patch.object(mekicopy.messagebox, "showinfo") as info,
            mock.patch.object(mekicopy.messagebox, "showerror") as error,
        ):
            self.assertFalse(mekicopy.MainWindow._on_start_hytrans(window))
        self.assertEqual(request.call_args_list[-1].args[0], "http://127.0.0.1:5000/worker/reopen")
        log.assert_called_once_with("restart_hytrans_worker", failure)
        error.assert_called_once()
        info.assert_not_called()
        window._launch_hytrans.assert_not_called()
        window._warn_if_port_busy.assert_not_called()
        window._tracked_process_is_starting.assert_not_called()

    def test_running_overlayer_config_error_does_not_claim_settings_applied(self) -> None:
        window = SimpleNamespace(
            _overlayer_base_url=lambda: "http://127.0.0.1:6000",
            _send_overlayer_config=mock.Mock(return_value=False),
            _warn_if_port_busy=mock.Mock(),
            _tracked_process_is_starting=mock.Mock(),
            _launch_overlayer=mock.Mock(),
        )
        with (
            mock.patch.object(mekicopy, "_probe_service", return_value="ready"),
            mock.patch.object(mekicopy.messagebox, "showinfo") as info,
            mock.patch.object(mekicopy.messagebox, "showerror") as error,
        ):
            mekicopy.MainWindow._on_start_overlayer(window)
        error.assert_called_once()
        info.assert_not_called()
        window._launch_overlayer.assert_not_called()
        window._warn_if_port_busy.assert_not_called()
        window._tracked_process_is_starting.assert_not_called()


if __name__ == "__main__":
    unittest.main()
