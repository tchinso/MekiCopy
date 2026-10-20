from __future__ import annotations

import configparser
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mekicopy_settings
from mekicopy_hotkey import (
    DEFAULT_GLOBAL_HOTKEY,
    HotkeyRegistrationState,
    hotkey_from_tk_event,
    parse_hotkey,
)
from mekicopy_settings import AppSettings, load_settings, save_settings


class HotkeyParserTests(unittest.TestCase):
    def test_bare_ordinary_key_is_supported(self) -> None:
        hotkey = parse_hotkey("b")

        self.assertIsNotNone(hotkey)
        self.assertEqual(hotkey.text, "B")
        self.assertEqual(hotkey.virtual_key, ord("B"))

    def test_modifier_shortcut_is_canonicalized(self) -> None:
        hotkey = parse_hotkey("shift + ctrl + f12")

        self.assertIsNotNone(hotkey)
        self.assertEqual(hotkey.text, "Ctrl+Shift+F12")

    def test_invalid_shortcuts_are_rejected(self) -> None:
        for value in ("", "Ctrl", "Ctrl+Ctrl+B", "Ctrl+B+C", "NotAKey"):
            with self.subTest(value=value):
                self.assertIsNone(parse_hotkey(value))

    def test_tk_event_capture_keeps_a_bare_key_bare(self) -> None:
        self.assertEqual(hotkey_from_tk_event("b", 0), "B")
        self.assertEqual(hotkey_from_tk_event("b", 0x0004), "Ctrl+B")


class HotkeyRegistrationStateTests(unittest.TestCase):
    def test_failed_replacement_restores_the_previous_shortcut(self) -> None:
        state = HotkeyRegistrationState()
        registered: list[str] = []
        unregistered: list[str] = []

        def register(hotkey):
            registered.append(hotkey.text)
            return "already registered" if hotkey.text == "C" else None

        def unregister(hotkey):
            unregistered.append(hotkey.text)

        first = state.configure(
            enabled=True,
            hotkey=parse_hotkey("B"),
            register=register,
            unregister=unregister,
        )
        failed = state.configure(
            enabled=True,
            hotkey=parse_hotkey("C"),
            register=register,
            unregister=unregister,
        )

        self.assertTrue(first.ok)
        self.assertFalse(failed.ok)
        self.assertTrue(failed.active)
        self.assertEqual(state.registered.text, "B")
        self.assertEqual(unregistered, ["B"])
        self.assertEqual(registered, ["B", "C", "B"])


class HotkeySettingsTests(unittest.TestCase):
    def test_save_normalizes_and_persists_hotkey_settings(self) -> None:
        writes: dict[str, str] = {}

        def capture_write(filename: str, text: str) -> bool:
            writes[filename] = text
            return True

        settings = AppSettings(
            global_hotkey_enabled=False,
            global_hotkey="ctrl + alt + b",
        )
        with mock.patch.object(mekicopy_settings, "_write_state_text", capture_write):
            self.assertTrue(save_settings(settings))

        parser = configparser.ConfigParser()
        parser.read_string(writes["settings.cfg"])
        self.assertFalse(parser.getboolean("settings", "global_hotkey_enabled"))
        self.assertEqual(parser.get("settings", "global_hotkey"), "Ctrl+Alt+B")

    def test_load_uses_b_for_an_invalid_saved_hotkey(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings_file = Path(temporary) / "settings.cfg"
            settings_file.write_text(
                "[settings]\nglobal_hotkey_enabled = true\nglobal_hotkey = not-a-key\n",
                encoding="utf-8",
            )
            with mock.patch.object(mekicopy_settings, "SETTINGS_FILE", str(settings_file)):
                settings = load_settings()

        self.assertTrue(settings.global_hotkey_enabled)
        self.assertEqual(settings.global_hotkey, DEFAULT_GLOBAL_HOTKEY)


class ManualTranslationHotkeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import mekicopy
        cls.app = mekicopy

    def test_realtime_mode_disables_registration_without_erasing_manual_preference(self) -> None:
        for realtime in (False, True):
            with self.subTest(realtime=realtime):
                settings = AppSettings(
                    realtime_ocr_enabled=realtime,
                    global_hotkey_enabled=True,
                    global_hotkey="Ctrl+B",
                )
                self.assertEqual(
                    self.app.MainWindow._global_hotkey_configuration(settings),
                    (not realtime, "Ctrl+B"),
                )
                self.assertTrue(settings.global_hotkey_enabled)

    def ready_window(self, *, workflow="screen", realtime=False):
        runner = mock.Mock()
        runner.is_running.return_value = False
        return SimpleNamespace(
            _workflow_mode=workflow,
            settings=AppSettings(realtime_ocr_enabled=realtime, global_hotkey_enabled=True),
            _closing=False,
            _global_hotkey=SimpleNamespace(drain_activations=lambda: 3),
            _global_hotkey_paused=False,
            _global_hotkey_action_active=False,
            _realtime_ocr=SimpleNamespace(active=False, translation_locked=False),
            _task_runner=runner,
            ocr_button=mock.Mock(),
            _on_ocr_copy=mock.Mock(return_value=True),
            _on_global_hotkey_action_finished=mock.Mock(),
            _schedule_global_hotkey_poll=mock.Mock(),
        )

    def test_hotkey_only_runs_in_manual_screen_translation(self) -> None:
        for workflow in (None, "screen", "audio", "subtitle"):
            for realtime in (False, True):
                with self.subTest(workflow=workflow, realtime=realtime):
                    window = self.ready_window(workflow=workflow, realtime=realtime)
                    self.app.MainWindow._poll_global_hotkey(window)
                    self.assertEqual(
                        window._on_ocr_copy.call_count,
                        int(workflow == "screen" and not realtime),
                    )
                    window._schedule_global_hotkey_poll.assert_called_once_with()

    def test_repeated_hotkey_events_share_one_inflight_manual_action(self) -> None:
        window = self.ready_window()
        self.app.MainWindow._poll_global_hotkey(window)
        self.app.MainWindow._poll_global_hotkey(window)
        window._on_ocr_copy.assert_called_once_with(
            source_button=window.ocr_button,
            on_finished=window._on_global_hotkey_action_finished,
        )
