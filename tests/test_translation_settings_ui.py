from __future__ import annotations

import configparser
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QLineEdit, QWidget

import mekicopy_settings
import mekicopy_settings_window as settings_ui
from hytrans.api_settings import ApiSettings, BACKEND_LABELS
from hytrans.model_files import PLAMO3_PROFILE
from mekicopy_settings import AppSettings, load_settings, save_settings
from mekicopy_theme import ensure_application


class TranslationSettingsPersistenceTests(unittest.TestCase):
    def test_realtime_mode_round_trips(self) -> None:
        settings = AppSettings(realtime_ocr_enabled=False)
        with mock.patch.object(mekicopy_settings, "_write_state_text", return_value=True) as write:
            self.assertTrue(save_settings(settings))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.cfg"
            path.write_text(write.call_args.args[1], encoding="utf-8")
            with mock.patch.object(mekicopy_settings, "SETTINGS_FILE", str(path)):
                self.assertFalse(load_settings().realtime_ocr_enabled)

    def test_unknown_model_defaults_to_hdtrans_mt15(self) -> None:
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
        self.app = ensure_application()
        self.root = QWidget()
        self.root.settings = AppSettings()
        self.root.apply_settings = mock.Mock(return_value=False)
        self.root.pause_global_hotkey = mock.Mock()
        self.root.resume_global_hotkey = mock.Mock()
        self.root._on_hdtrans_settings = mock.Mock()
        patches = (
            mock.patch.object(settings_ui, "load_api_settings", return_value=ApiSettings()),
            mock.patch.object(settings_ui, "load_detached_geometry", side_effect=lambda value: value),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.window = settings_ui.SettingsWindow(self.root)
        self.root.settings_window = self.window
        self.addCleanup(self._cleanup_qt)

    def _cleanup_qt(self) -> None:
        if self.root.settings_window is not None:
            self.window.reject()
        self.root.close()
        self.root.deleteLater()
        self.app.processEvents()

    def dialog(self) -> settings_ui.ApiSettingsDialog:
        self.window._open_api_settings()
        return self.window.api_settings_dialog

    def test_translation_mode_page_separates_realtime_and_manual_controls(self) -> None:
        tabs = self.window.tabs
        self.assertEqual(
            [tabs.tabText(index) for index in range(tabs.count())],
            ["일반", "실시간/수동 번역", "번역 오버레이", "음성인식"],
        )
        translation = tabs.widget(1)
        self.assertIn(self.window.realtime_ocr_enabled, translation.findChildren(QCheckBox))
        self.assertFalse(self.window.realtime_ocr_interval.isEditable())
        interval = self.window.realtime_ocr_interval
        self.assertEqual([interval.itemText(i) for i in range(interval.count())], ["0.5", "1", "1.5", "2", "2.5"])
        self.assertEqual(interval.currentText(), "1")
        self.assertTrue(self.window.realtime_ocr_enabled.isChecked())
        self.assertFalse(self.window.global_hotkey_enabled.isChecked())
        self.assertEqual(self.window.hytrans_backend.currentText(), "hdTrans")
        general = tabs.widget(0)
        self.assertTrue(general.isAncestorOf(self.window.hdtrans_settings_button))
        for control in (
            self.window.simple_copy_complete, self.window.global_hotkey_enabled,
            self.window.detached_topmost, self.window.detached_fixed_size,
        ):
            self.assertFalse(general.isAncestorOf(control))
            self.assertTrue(translation.isAncestorOf(control))

    def test_realtime_disables_manual_controls_and_retains_preferences(self) -> None:
        controls = (
            self.window.simple_copy_complete,
            self.window.global_hotkey_enabled,
            self.window.detached_topmost,
            self.window.detached_fixed_size,
            self.window.global_hotkey_entry,
            self.window.global_hotkey_default_button,
        )
        self.window.global_hotkey_enabled.setChecked(True)
        self.window.detached_fixed_size.setChecked(True)
        self.window.global_hotkey_entry.setText("Ctrl+Alt+B")
        self.assertTrue(all(not control.isEnabled() for control in controls))
        self.window.realtime_ocr_enabled.setChecked(False)
        self.assertTrue(all(control.isEnabled() for control in controls))
        self.window.realtime_ocr_enabled.setChecked(True)
        collected = self.window._collect_settings()
        self.assertTrue(collected.global_hotkey_enabled)
        self.assertTrue(collected.detached_fixed_size)
        self.assertEqual(collected.global_hotkey, "Ctrl+Alt+B")
        self.window.realtime_ocr_enabled.setChecked(False)
        self.window.global_hotkey_enabled.setChecked(False)
        self.assertFalse(self.window.global_hotkey_entry.isEnabled())

    def test_realtime_help_preserves_usage_and_requirements(self) -> None:
        help_text = self.window.realtime_ocr_enabled.toolTip()
        for term in ("OCR 영역", "HYTrans", "MekiOverlayer", "실시간 OCR 시작", "실시간 OCR 중지"):
            self.assertIn(term, help_text)

    def test_global_shortcut_field_captures_native_qt_key_events(self) -> None:
        self.window.realtime_ocr_enabled.setChecked(False)
        self.window.global_hotkey_enabled.setChecked(True)
        self.window.tabs.setCurrentIndex(1)
        self.window.show()
        self.window.global_hotkey_entry.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.global_hotkey_entry, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window.global_hotkey_entry.text(), "Ctrl+B")
        QTest.keyClick(self.window.global_hotkey_entry, Qt.Key.Key_Escape)
        self.assertEqual(self.window.global_hotkey_entry.text(), "Esc")
        self.assertIs(self.root.settings_window, self.window)
        self.window.global_hotkey_default_button.click()
        self.assertEqual(self.window.global_hotkey_entry.text(), "B")

    def test_realtime_settings_are_collected_with_overlay_settings(self) -> None:
        self.window.realtime_ocr_enabled.setChecked(True)
        self.window.realtime_ocr_interval.setCurrentText("1.5")
        collected = self.window._collect_settings()
        self.assertTrue(collected.realtime_ocr_enabled)
        self.assertEqual(collected.realtime_ocr_interval, 1.5)

    def test_provider_switch_retains_freeform_model_and_exact_prompt(self) -> None:
        dialog = self.dialog()
        prompt = 'Translate 100% accurately.\nKeep {literal} braces.\n{source} -> {target}\n{text}\n'
        dialog.key_entry.setText("cf-example-key")
        dialog.account_entry.setText("a" * 32)
        dialog.model_combo.setEditText("@cf/example/custom-model")
        dialog.models_text.setPlainText("model/a\nmodel/b\nmodel/a\n")
        dialog.prompt_text.setPlainText(prompt)
        self.assertEqual(dialog.key_entry.echoMode(), QLineEdit.EchoMode.Password)
        dialog.show_key.setChecked(True)
        dialog.provider_combo.setCurrentText(BACKEND_LABELS["groq"])
        dialog.key_entry.setText("groq-example-key")
        dialog.model_combo.setEditText("a-new-model-id")
        dialog.provider_combo.setCurrentText(BACKEND_LABELS["cloudflare"])
        self.assertEqual(dialog.key_entry.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(dialog.key_entry.text(), "cf-example-key")
        self.assertEqual(dialog.model_combo.currentText(), "@cf/example/custom-model")
        self.assertEqual(dialog.prompt_text.toPlainText(), prompt)
        dialog._on_apply()
        self.assertEqual(self.window.api_settings.profiles["cloudflare"].prompt, prompt)
        self.assertEqual(self.window.api_settings.profiles["cloudflare"].models, ["model/a", "model/b"])
        self.assertEqual(self.window.api_settings.profiles["groq"].model, "a-new-model-id")
        self.root.apply_settings.assert_not_called()

    def test_cancel_leaves_parent_draft_unchanged(self) -> None:
        before = deepcopy(self.window.api_settings)
        dialog = self.dialog()
        dialog.key_entry.setText("discard-me")
        dialog.reject()
        self.assertIsNone(self.window.api_settings_dialog)
        self.assertEqual(self.window.api_settings, before)
        self.root.apply_settings.assert_not_called()

    def test_local_allows_blank_credentials_but_selected_api_requires_them(self) -> None:
        for label, model in (("pLaMo-3 (로컬)", PLAMO3_PROFILE.key), (BACKEND_LABELS["local"], "mt1.5")):
            with self.subTest(engine=label):
                self.window.hytrans_backend.setCurrentText(label)
                collected = self.window._collect_settings()
                self.assertEqual(collected.hytrans_backend, "local")
                self.assertEqual(collected.hytrans_model_id, model)
        self.window.hytrans_backend.setCurrentText(BACKEND_LABELS["cloudflare"])
        with self.assertRaisesRegex(ValueError, "API"):
            self.window._collect_settings()
        profile = self.window.api_settings.profiles["cloudflare"]
        profile.api_key = "example-key"
        profile.account_id = "a" * 32
        self.assertEqual(self.window._collect_settings().hytrans_backend, "cloudflare")
        self.assertEqual(self.window.api_settings.profiles["groq"].api_key, "")

    def test_translation_engine_order_and_plamo_selection_restore(self) -> None:
        menu = self.window.hytrans_backend
        self.assertEqual([menu.itemText(i) for i in range(menu.count())], [
            BACKEND_LABELS["hdtrans"], BACKEND_LABELS["translator_api"], "pLaMo-3 (로컬)",
            BACKEND_LABELS["local"], BACKEND_LABELS["cloudflare"], BACKEND_LABELS["deepinfra"], BACKEND_LABELS["groq"],
        ])
        menu.setCurrentText("pLaMo-3 (로컬)")
        self.root.settings = self.window._collect_settings()
        reopened = settings_ui.SettingsWindow(self.root)
        try:
            self.assertEqual(reopened.hytrans_backend.currentText(), "pLaMo-3 (로컬)")
        finally:
            reopened.reject()

    def test_hdtrans_selection_needs_no_api_profile_and_enables_native_settings(self) -> None:
        profiles = deepcopy(self.window.api_settings)
        self.window.hytrans_backend.setCurrentText(BACKEND_LABELS["local"])
        self.assertFalse(self.window.hdtrans_settings_button.isEnabled())
        self.window.hytrans_backend.setCurrentText(BACKEND_LABELS["hdtrans"])
        self.assertEqual(self.window._collect_settings().hytrans_backend, "hdtrans")
        self.assertTrue(self.window.hdtrans_settings_button.isEnabled())
        self.window.hdtrans_settings_button.click()
        self.root._on_hdtrans_settings.assert_called_once()
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
        self.assertIsNone(self.root.settings_window)

    def test_failed_save_keeps_window_and_draft_for_retry(self) -> None:
        self.root.apply_settings.return_value = None
        self.window.api_settings.profiles["groq"].model = "future-model"
        self.window._on_save()
        self.assertEqual(self.window.api_settings.profiles["groq"].model, "future-model")
        self.assertIs(self.root.settings_window, self.window)

    def test_switching_to_parakeet_keeps_reazonspeech_precision_while_editing(self) -> None:
        model = self.window.audio_stt_model
        precision = self.window.audio_stt_precision
        model.setCurrentIndex(model.findData("reazonspeech"))
        precision.setCurrentText("fp32")
        model.setCurrentIndex(model.findData("parakeet"))
        self.assertFalse(precision.isEnabled())
        self.assertEqual(self.window._collect_settings().audio_stt_precision, "int8")
        model.setCurrentIndex(model.findData("reazonspeech"))
        self.assertTrue(precision.isEnabled())
        self.assertEqual(self.window._collect_settings().audio_stt_precision, "fp32")


if __name__ == "__main__":
    unittest.main()
