from __future__ import annotations

import configparser
from copy import deepcopy
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import mekicopy_settings
import mekicopy_settings_window as settings_ui
from hytrans.api_settings import ApiSettings, BACKEND_LABELS
from mekicopy_settings import AppSettings, load_settings, save_settings


class TranslationSettingsPersistenceTests(unittest.TestCase):
    def test_legacy_settings_default_to_local_mt15(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.cfg"
            path.write_text("[settings]\nhytrans_model_id = mt2\n", encoding="utf-8")
            with mock.patch.object(mekicopy_settings, "SETTINGS_FILE", str(path)):
                settings = load_settings()
        self.assertEqual(settings.hytrans_backend, "local")
        self.assertEqual(settings.hytrans_model_id, "mt1.5")

    def test_backend_is_persisted_and_invalid_values_fall_back(self) -> None:
        written: dict[str, str] = {}

        def write(filename: str, text: str) -> bool:
            written[filename] = text
            return True

        with mock.patch.object(mekicopy_settings, "_write_state_text", side_effect=write):
            self.assertTrue(save_settings(AppSettings(hytrans_backend=" Groq ")))
        parser = configparser.ConfigParser()
        parser.read_string(written["settings.cfg"])
        self.assertEqual(parser.get("settings", "hytrans_backend"), "groq")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.cfg"
            for raw, expected in ((" DeepInfra ", "deepinfra"), ("unknown", "local")):
                with self.subTest(raw=raw):
                    path.write_text(f"[settings]\nhytrans_backend = {raw}\n", encoding="utf-8")
                    with mock.patch.object(mekicopy_settings, "SETTINGS_FILE", str(path)):
                        self.assertEqual(load_settings().hytrans_backend, expected)


class TranslationSettingsUiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.root.settings = AppSettings()
        self.root.apply_settings = mock.Mock(return_value=False)
        self.root.settings_window = None
        patches = (
            mock.patch.object(settings_ui, "load_api_settings", return_value=ApiSettings()),
            mock.patch.object(settings_ui, "_korean_font_families", return_value=["Malgun Gothic"]),
            mock.patch.object(settings_ui, "_japanese_font_families", return_value=["Yu Gothic UI"]),
            mock.patch.object(settings_ui, "load_detached_geometry", side_effect=lambda value: value),
            mock.patch.object(settings_ui, "_set_window_icon"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.window = settings_ui.SettingsWindow(self.root)
        self.window.withdraw()
        self.root.settings_window = self.window

    def dialog(self) -> settings_ui.ApiSettingsDialog:
        self.window._open_api_settings()
        dialog = self.window.api_settings_dialog
        dialog.withdraw()
        return dialog

    def test_provider_switch_retains_freeform_model_and_exact_prompt(self) -> None:
        dialog = self.dialog()
        prompt = 'Translate 100% accurately.\nKeep {literal} braces.\n{source} -> {target}\n{text}\n'
        dialog.api_key_var.set("cf-example-key")
        dialog.account_id_var.set("a" * 32)
        dialog.model_var.set("@cf/example/custom-model")
        dialog.models_text.delete("1.0", tk.END)
        dialog.models_text.insert("1.0", "model/a\nmodel/b\nmodel/a\n")
        dialog.prompt_text.delete("1.0", tk.END)
        dialog.prompt_text.insert("1.0", prompt)
        self.assertNotEqual(dialog.key_entry.cget("show"), "")
        dialog._on_provider_changed(BACKEND_LABELS["groq"])
        dialog.api_key_var.set("groq-example-key")
        dialog.model_var.set("a-new-model-id")
        dialog._on_provider_changed(BACKEND_LABELS["cloudflare"])
        self.assertEqual(dialog.api_key_var.get(), "cf-example-key")
        self.assertEqual(dialog.model_var.get(), "@cf/example/custom-model")
        self.assertEqual(dialog.prompt_text.get("1.0", "end-1c"), prompt)
        dialog._on_apply()
        self.assertEqual(self.window.api_settings.profiles["cloudflare"].prompt, prompt)
        self.assertEqual(self.window.api_settings.profiles["cloudflare"].models, ["model/a", "model/b"])
        self.assertEqual(self.window.api_settings.profiles["groq"].model, "a-new-model-id")
        self.root.apply_settings.assert_not_called()

    def test_cancel_leaves_parent_draft_unchanged(self) -> None:
        before = deepcopy(self.window.api_settings)
        dialog = self.dialog()
        dialog.api_key_var.set("discard-me")
        dialog._on_close()
        self.assertEqual(self.window.api_settings, before)
        self.root.apply_settings.assert_not_called()

    def test_local_allows_blank_credentials_but_selected_api_requires_them(self) -> None:
        self.assertEqual(self.window._collect_settings().hytrans_backend, "local")
        self.window.hytrans_backend_var.set(BACKEND_LABELS["cloudflare"])
        with self.assertRaisesRegex(ValueError, "API"):
            self.window._collect_settings()
        profile = self.window.api_settings.profiles["cloudflare"]
        profile.api_key = "example-key"
        profile.account_id = "a" * 32
        self.assertEqual(self.window._collect_settings().hytrans_backend, "cloudflare")
        self.assertEqual(self.window.api_settings.profiles["groq"].api_key, "")

    def test_api_only_save_passes_draft_through_existing_apply_callback(self) -> None:
        self.window.api_settings.profiles["groq"].models.append("future-model")
        draft = self.window.api_settings
        self.window._on_save()
        self.root.apply_settings.assert_called_once()
        args, kwargs = self.root.apply_settings.call_args
        self.assertEqual(args[0].hytrans_backend, "local")
        self.assertTrue(kwargs["persist"])
        self.assertIs(kwargs["api_settings"], draft)
        self.assertFalse(self.window.winfo_exists())

    def test_failed_save_keeps_window_and_draft_for_retry(self) -> None:
        self.root.apply_settings.return_value = None
        self.window.api_settings.profiles["groq"].model = "future-model"
        self.window._on_save()
        self.assertTrue(self.window.winfo_exists())
        self.assertEqual(self.window.api_settings.profiles["groq"].model, "future-model")
        self.assertIs(self.root.settings_window, self.window)


if __name__ == "__main__":
    unittest.main()
