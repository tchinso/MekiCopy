from __future__ import annotations

import configparser
import gc
from copy import deepcopy
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from pathlib import Path
from unittest import mock

import mekicopy_settings
import mekicopy_settings_window as settings_ui
from hytrans.api_settings import ApiSettings, BACKEND_LABELS
from mekicopy_settings import AppSettings, load_settings, save_settings


class TranslationSettingsPersistenceTests(unittest.TestCase):
    def test_legacy_clipboard_mode_loads_as_translation_and_saves_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.cfg"
            path.write_text(
                "[settings]\noverlay_translation_mode = false\n"
                "realtime_ocr_enabled = false\n",
                encoding="utf-8",
            )
            with mock.patch.object(mekicopy_settings, "SETTINGS_FILE", str(path)):
                settings = load_settings()
        self.assertTrue(settings.overlay_translation_mode)
        self.assertFalse(settings.realtime_ocr_enabled)
        settings.overlay_translation_mode = False
        with mock.patch.object(mekicopy_settings, "_write_state_text", return_value=True) as write:
            self.assertTrue(save_settings(settings))
        parser = configparser.ConfigParser()
        parser.read_string(write.call_args.args[1])
        self.assertTrue(parser.getboolean("settings", "overlay_translation_mode"))

    def test_legacy_settings_default_to_hdtrans_mt15(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.cfg"
            path.write_text("[settings]\nhytrans_model_id = mt2\n", encoding="utf-8")
            with mock.patch.object(mekicopy_settings, "SETTINGS_FILE", str(path)):
                settings = load_settings()
        self.assertEqual(settings.hytrans_backend, "hdtrans")
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
        self.addCleanup(self._cleanup_tk)
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

    def _cleanup_tk(self) -> None:
        root = getattr(self, "root", None)
        if root is not None:
            root.update_idletasks()
        dialog = None
        window = getattr(self, "window", None)
        if window is not None and window.winfo_exists():
            dialog = getattr(window, "hdtrans_settings_dialog", None)
            if dialog is not None and dialog.winfo_exists():
                dialog._close()
            window.destroy()
        self.window = None
        window = None
        dialog = None
        if root is not None:
            root.settings_window = None
            root.destroy()
        self.root = None
        root = None
        gc.collect()

    def dialog(self) -> settings_ui.ApiSettingsDialog:
        self.window._open_api_settings()
        dialog = self.window.api_settings_dialog
        dialog.withdraw()
        return dialog

    @staticmethod
    def descendants(widget: tk.Misc):
        for child in widget.winfo_children():
            yield child
            yield from TranslationSettingsUiTests.descendants(child)

    def notebook(self) -> ttk.Notebook:
        notebooks = [
            widget for widget in self.descendants(self.window)
            if isinstance(widget, ttk.Notebook)
        ]
        self.assertEqual(len(notebooks), 1)
        return notebooks[0]

    def test_translation_mode_page_separates_realtime_and_manual_controls(self) -> None:
        main_notebook = self.notebook()
        top_tabs = {main_notebook.tab(tab, "text"): tab for tab in main_notebook.tabs()}
        self.assertEqual(list(top_tabs), ["일반", "실시간/수동 번역", "번역 오버레이", "음성인식"])
        translation_page = self.window.nametowidget(top_tabs["실시간/수동 번역"])
        sections = translation_page.interior.pack_slaves()
        self.assertEqual([section.cget("text") for section in sections], ["실시간 OCR", "수동 번역"])
        controls = list(self.descendants(sections[0]))
        enabled = next(
            widget for widget in controls
            if isinstance(widget, tk.Checkbutton)
            and widget.cget("text") == "실시간 번역 모드 사용"
        )
        self.assertEqual(str(enabled.cget("variable")), str(self.window.realtime_ocr_enabled_var))
        interval = next(widget for widget in controls if isinstance(widget, ttk.Combobox))
        self.assertEqual(str(interval.cget("textvariable")), str(self.window.realtime_ocr_interval_var))
        self.assertEqual(str(interval.cget("state")), "readonly")
        self.assertEqual(interval.cget("values"), ("0.5", "1", "1.5", "2", "2.5"))
        self.assertEqual(self.window.realtime_ocr_interval_var.get(), "1")
        self.assertTrue(self.window.realtime_ocr_enabled_var.get())
        self.assertFalse(self.window.global_hotkey_enabled_var.get())
        self.assertEqual(self.window.hytrans_backend_var.get(), "hdTrans")
        labels = [
            str(widget.cget("text")) for widget in controls
            if isinstance(widget, tk.Label)
        ]
        self.assertEqual(labels, ["?", "최소 인식 간격(초)"])
        self.assertIsNone(self.window.realtime_ocr_tooltip.window)
        all_labels = [
            str(widget.cget("text")) for widget in self.descendants(self.window)
            if isinstance(widget, tk.Label)
        ]
        self.assertFalse(any("설정 저장 후" in label for label in all_labels))

        general_page = self.window.nametowidget(top_tabs["일반"])
        general_controls = list(self.descendants(general_page))
        self.assertIn(self.window.hdtrans_settings_button, general_controls)
        general_variables = {
            str(widget.cget("variable")) for widget in general_controls
            if isinstance(widget, tk.Checkbutton)
        }
        for variable in (
            self.window.simple_copy_complete_var, self.window.global_hotkey_enabled_var,
            self.window.detached_topmost_var, self.window.detached_fixed_size_var,
        ):
            self.assertNotIn(str(variable), general_variables)
        all_checkboxes = [
            str(widget.cget("text")) for widget in self.descendants(self.window)
            if isinstance(widget, tk.Checkbutton)
        ]
        self.assertNotIn("오버레이어 번역 모드 사용", all_checkboxes)

    def test_realtime_disables_manual_controls_and_restores_manual_preferences(self) -> None:
        manual_variables = (
            self.window.simple_copy_complete_var,
            self.window.global_hotkey_enabled_var,
            self.window.detached_topmost_var,
            self.window.detached_fixed_size_var,
        )
        controls = [
            widget for widget in self.descendants(self.window)
            if isinstance(widget, tk.Checkbutton)
            and str(widget.cget("variable")) in {str(variable) for variable in manual_variables}
        ]
        self.assertEqual(len(controls), 4)
        self.window.global_hotkey_enabled_var.set(True)
        self.window.detached_fixed_size_var.set(True)
        self.window.global_hotkey_var.set("Ctrl+Alt+B")
        for widget in [*controls, self.window.global_hotkey_entry, self.window.global_hotkey_default_button]:
            self.assertEqual(str(widget.cget("state")), "disabled")
        self.window.realtime_ocr_enabled_var.set(False)
        for widget in [*controls, self.window.global_hotkey_entry, self.window.global_hotkey_default_button]:
            self.assertEqual(str(widget.cget("state")), "normal")
        self.window.realtime_ocr_enabled_var.set(True)
        collected = self.window._collect_settings()
        self.assertTrue(collected.global_hotkey_enabled)
        self.assertTrue(collected.detached_fixed_size)
        self.assertEqual(collected.global_hotkey, "Ctrl+Alt+B")
        self.window.realtime_ocr_enabled_var.set(False)
        self.window.global_hotkey_enabled_var.set(False)
        self.assertEqual(str(self.window.global_hotkey_entry.cget("state")), "disabled")

    def _show_realtime_help(self):
        notebook = self.notebook()
        self.root.deiconify()
        self.window.deiconify()
        notebook.select(1)
        self.root.update()
        tooltip = self.window.realtime_ocr_tooltip
        tooltip.widget.event_generate("<Enter>")
        self.root.update()
        self.assertIsNotNone(tooltip.window)
        self.assertTrue(tooltip.window.winfo_ismapped())
        return tooltip

    def test_realtime_help_only_appears_on_hover_and_hides_on_leave(self) -> None:
        tooltip = self._show_realtime_help()
        popup = tooltip.window
        help_text = "\n".join(
            str(widget.cget("text")) for widget in self.descendants(popup)
            if isinstance(widget, tk.Label)
        )
        for requirement in (
            "확정된 OCR 영역", "번역 엔진(HyTrans)", "번역결과 표시기(MekiOverlayer)",
            "실시간 번역 모드 사용",
            "실시간 OCR 시작", "실시간 OCR 중지",
        ):
            self.assertIn(requirement, help_text)
        tooltip.widget.event_generate("<Enter>")
        self.assertIs(tooltip.window, popup)
        tooltip.widget.event_generate("<Leave>")
        self.root.update()
        self.assertIsNone(tooltip.window)
        self.assertFalse(popup.winfo_exists())

    def test_realtime_help_closes_when_changing_tabs_or_hiding_settings(self) -> None:
        tooltip = self._show_realtime_help()
        self.notebook().select(0)
        self.root.update()
        self.assertIsNone(tooltip.window)
        tooltip = self._show_realtime_help()
        self.window.withdraw()
        self.root.update()
        self.assertIsNone(tooltip.window)

    def test_destroying_help_icon_removes_popup_and_owner_bindings(self) -> None:
        tooltip = self._show_realtime_help()
        popup = tooltip.window
        tooltip.widget.destroy()
        self.root.update()
        self.assertIsNone(tooltip.window)
        self.assertFalse(popup.winfo_exists())
        self.assertEqual(tooltip._bindings, [])
        self.window.withdraw()
        self.root.update()

    def test_realtime_settings_are_collected_with_overlay_settings(self) -> None:
        self.root.settings.overlay_translation_mode = False
        self.window.realtime_ocr_enabled_var.set(True)
        self.window.realtime_ocr_interval_var.set("1.5")
        collected = self.window._collect_settings()
        self.assertTrue(collected.overlay_translation_mode)
        self.assertTrue(collected.realtime_ocr_enabled)
        self.assertEqual(collected.realtime_ocr_interval, 1.5)

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
        self.window.hytrans_backend_var.set(BACKEND_LABELS["local"])
        self.assertEqual(self.window._collect_settings().hytrans_backend, "local")
        self.window.hytrans_backend_var.set(BACKEND_LABELS["cloudflare"])
        with self.assertRaisesRegex(ValueError, "API"):
            self.window._collect_settings()
        profile = self.window.api_settings.profiles["cloudflare"]
        profile.api_key = "example-key"
        profile.account_id = "a" * 32
        self.assertEqual(self.window._collect_settings().hytrans_backend, "cloudflare")
        self.assertEqual(self.window.api_settings.profiles["groq"].api_key, "")

    def test_hdtrans_selection_needs_no_api_profile_and_enables_native_settings(self) -> None:
        profiles = deepcopy(self.window.api_settings)
        self.window.hytrans_backend_var.set(BACKEND_LABELS["hdtrans"])
        self.window._update_translation_controls()
        self.assertEqual(self.window._collect_settings().hytrans_backend, "hdtrans")
        self.assertEqual(str(self.window.hdtrans_settings_button.cget("state")), "normal")
        self.assertEqual(self.window.api_settings, profiles)

    def test_api_only_save_passes_draft_through_existing_apply_callback(self) -> None:
        self.window.api_settings.profiles["groq"].models.append("future-model")
        draft = self.window.api_settings
        self.window._on_save()
        self.root.apply_settings.assert_called_once()
        args, kwargs = self.root.apply_settings.call_args
        self.assertEqual(args[0].hytrans_backend, "hdtrans")
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
