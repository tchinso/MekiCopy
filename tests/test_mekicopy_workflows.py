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


if __name__ == "__main__":
    unittest.main()
