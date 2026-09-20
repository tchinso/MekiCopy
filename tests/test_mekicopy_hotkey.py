from __future__ import annotations

import configparser
import tempfile
import unittest
from pathlib import Path
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
