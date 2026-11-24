from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import os

from PySide6.QtCore import QEvent, QLocale, QPointF, QSignalBlocker, Qt
from PySide6.QtGui import QColor, QFontDatabase, QIcon, QIntValidator, QKeyEvent, QKeySequence, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionComboBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from hytrans.api_settings import (
    BACKEND_LABELS,
    PROVIDERS,
    ApiSettings,
    ProviderSettings,
    load_api_settings,
    normalize_backend,
    validate_provider_settings,
)
from hytrans.model_files import MT15_PROFILE, PLAMO3_PROFILE, get_model_profile
from mekicopy_hotkey import DEFAULT_GLOBAL_HOTKEY, parse_hotkey
from mekicopy_realtime import REALTIME_INTERVALS, normalize_interval
from mekicopy_settings import AppSettings, _geometry_size, _normalize_font_name, load_detached_geometry
from mekicopy_theme import ACCENT_INK, BG, BORDER, DISABLED_FG, RoundedButton, configure_window_theme
from service_ports import validate_unique_ports

TRANSLATION_ENGINES = {
    BACKEND_LABELS["hdtrans"]: ("hdtrans", MT15_PROFILE),
    BACKEND_LABELS["translator_api"]: ("translator_api", MT15_PROFILE),
    "pLaMo-3 (로컬)": ("local", PLAMO3_PROFILE),
    BACKEND_LABELS["local"]: ("local", MT15_PROFILE),
    **{BACKEND_LABELS[provider]: (provider, MT15_PROFILE) for provider in PROVIDERS},
}
STT_MODEL_LABELS = {
    "parakeet": "Parakeet TDT-CTC 0.6B 일본어 (기본, INT8)",
    "reazonspeech": "ReazonSpeech 일본어 (INT8 / FP32)",
}


def _configure_dialog(dialog: QDialog, title: str, width: int, height: int) -> QVBoxLayout:
    dialog.setWindowTitle(title)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    configure_window_theme(dialog)
    screen = dialog.screen().availableGeometry()
    available_width = max(320, screen.width() - 48)
    available_height = max(300, screen.height() - 72)
    dialog.setMinimumSize(min(520, available_width), min(420, available_height))
    dialog.resize(min(width, available_width), min(height, available_height))
    body = QVBoxLayout(dialog)
    body.setContentsMargins(15, 13, 15, 13)
    body.setSpacing(9)
    heading = QLabel(title)
    font = heading.font()
    font.setPointSize(16)
    font.setBold(True)
    heading.setFont(font)
    body.addWidget(heading)
    return body


def _scroll_content() -> tuple[QScrollArea, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QWidget()
    content.setObjectName("settingsScrollBody")
    content.setStyleSheet(f"QWidget#settingsScrollBody {{ background: {BG}; }}")
    body = QVBoxLayout(content)
    body.setContentsMargins(3, 6, 7, 2)
    body.setSpacing(8)
    scroll.setWidget(content)
    return scroll, body


def _section(parent: QVBoxLayout, title: str) -> QVBoxLayout:
    card = QFrame()
    card.setProperty("role", "card")
    card.setAccessibleName(title)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(12, 9, 12, 11)
    layout.setSpacing(6)
    label = QLabel(title)
    font = label.font()
    font.setPointSize(11)
    font.setBold(True)
    label.setFont(font)
    layout.addWidget(label)
    parent.addWidget(card)
    return layout


def _description(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setProperty("role", "muted")
    return label


def _action(text, callback, *, variant="normal") -> RoundedButton:
    button = RoundedButton(None, text=text, command=callback, height=36, radius=12, variant=variant)
    button.setAutoDefault(False)
    return button


def _show_dialog(dialog: QDialog) -> None:
    if dialog.isMinimized():
        dialog.showNormal()
    else:
        dialog.show()
    dialog.raise_()
    dialog.activateWindow()


def _footer(body: QVBoxLayout, dismiss, confirm, *, confirm_text="저장") -> RoundedButton:
    row = QHBoxLayout()
    row.addStretch()
    save = _action(confirm_text, confirm, variant="primary")
    for button in (_action("닫기", dismiss), save):
        button.setMinimumWidth(104)
        button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        row.addWidget(button)
    body.addLayout(row)
    return save


def _form(section: QVBoxLayout) -> QFormLayout:
    form = QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setHorizontalSpacing(13)
    form.setVerticalSpacing(6)
    section.addLayout(form)
    return form


def _checkbox(section: QVBoxLayout, title: str, checked: bool) -> QCheckBox:
    checkbox = QCheckBox(title)
    checkbox.setChecked(checked)
    section.addWidget(checkbox)
    return checkbox


class _NumberField(QLineEdit):
    def __init__(self, label: str, value: int, minimum: int, maximum: int) -> None:
        super().__init__(str(value))
        self.setAccessibleName(label)
        self.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        locale = QLocale.c()
        locale.setNumberOptions(QLocale.NumberOption.RejectGroupSeparator)
        validator = QIntValidator(minimum, maximum, self)
        validator.setLocale(locale)
        self.setValidator(validator)

    def value(self) -> int:
        if not self.hasAcceptableInput():
            validator = self.validator()
            raise ValueError(
                f"{self.accessibleName()} 값은 {validator.bottom()}부터 {validator.top()} 사이의 숫자여야 합니다."
            )
        return int(self.text())


def _number_field(form: QFormLayout, label: str, value: int, minimum: int, maximum: int) -> _NumberField:
    field = _NumberField(label, value, minimum, maximum)
    form.addRow(label, field)
    return field


def _opacity_slider(form: QFormLayout, label: str, opacity: float) -> QSlider:
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(10, 100)
    slider.setSingleStep(1)
    slider.setValue(round(opacity * 100))
    slider.setAccessibleName(label)
    amount = QLabel(f"{slider.value()}%")
    amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    amount.setMinimumWidth(42)
    slider.valueChanged.connect(lambda value: amount.setText(f"{value}%"))
    row = QHBoxLayout()
    row.addWidget(slider, 1)
    row.addWidget(amount)
    form.addRow(label, row)
    return slider


class _SettingsComboBox(QComboBox):
    def __init__(self) -> None:
        super().__init__()
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(14)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        center = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxArrow, self
        ).center()
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor(ACCENT_INK if self.isEnabled() else DISABLED_FG), 1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawPolyline(QPolygonF([
                QPointF(center.x() - 4, center.y() - 2),
                QPointF(center.x(), center.y() + 2),
                QPointF(center.x() + 4, center.y() - 2),
            ]))


def _font_picker(writing_system: QFontDatabase.WritingSystem, selected: str) -> QComboBox:
    combo = _SettingsComboBox()
    selected = _normalize_font_name(selected)
    families = QFontDatabase.families(writing_system)
    combo.addItems(sorted({selected, *(name for name in families if not name.startswith("@"))}, key=str.casefold))
    combo.setCurrentText(selected)
    return combo


class _ColorButton(RoundedButton):
    def __init__(self, owner: QDialog, label: str, initial: str) -> None:
        super().__init__(None, text="", command=self._choose, height=36, radius=12)
        self.owner = owner
        self.label = label
        self.setAutoDefault(False)
        self._set_color(QColor(initial))

    def _set_color(self, color: QColor) -> None:
        self._color = color
        self.setText(color.name().upper())
        swatch = QPixmap(18, 18)
        swatch.fill(Qt.GlobalColor.transparent)
        with QPainter(swatch) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor(BORDER))
            painter.setBrush(color)
            painter.drawRoundedRect(1, 1, 15, 15, 4, 4)
        self.setIcon(QIcon(swatch))
        self.setAccessibleName(f"{self.label} {color.name().upper()}")

    def color(self) -> str:
        return self._color.name()

    def _choose(self) -> None:
        color = QColorDialog.getColor(self._color, self.owner, self.label)
        if color.isValid():
            self._set_color(color)


_SHORTCUT_MODIFIERS = (
    (Qt.KeyboardModifier.ControlModifier, "Ctrl"),
    (Qt.KeyboardModifier.AltModifier, "Alt"),
    (Qt.KeyboardModifier.ShiftModifier, "Shift"),
    (Qt.KeyboardModifier.MetaModifier, "Win"),
)
_SHIFTED_PUNCTUATION = dict(zip("!@#$%^&*()_+{}|:\"<>?~", "1234567890-=[]\\;',./`"))
_WINDOWS_PUNCTUATION = {
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}


def _shortcut_from_event(event: QKeyEvent) -> str | None:
    key = event.key()
    modifiers = event.modifiers()
    native_key = event.nativeVirtualKey() if os.name == "nt" else 0
    if Qt.Key.Key_0 <= key <= Qt.Key.Key_9 and modifiers & Qt.KeyboardModifier.KeypadModifier:
        key_name = f"Num{key - Qt.Key.Key_0}"
    elif 0x60 <= native_key <= 0x69:
        key_name = f"Num{native_key - 0x60}"
    elif native_key in _WINDOWS_PUNCTUATION:
        key_name = _WINDOWS_PUNCTUATION[native_key]
    elif 0x30 <= native_key <= 0x39:
        key_name = chr(native_key)
    elif key == Qt.Key.Key_Backtab:
        key_name = "Tab"
    else:
        key_name = QKeySequence(key).toString(QKeySequence.SequenceFormat.PortableText)
        key_name = _SHIFTED_PUNCTUATION.get(key_name, key_name)
    parts = [name for flag, name in _SHORTCUT_MODIFIERS if modifiers & flag]
    shortcut = parse_hotkey("+".join([*parts, key_name]))
    return shortcut.text if shortcut else None


class _HotkeyField(QLineEdit):
    def __init__(self, value: str, owner: QWidget) -> None:
        super().__init__(value)
        self.owner = owner
        self.setReadOnly(True)
        self.setAccessibleName("전역 단축키 설정")

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        if event.type() == QEvent.Type.KeyPress:
            shortcut = _shortcut_from_event(event)
            if shortcut is not None:
                self.setText(shortcut)
            return True
        return super().event(event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        pause = getattr(self.owner, "pause_global_hotkey", None)
        if pause is not None:
            pause()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        resume = getattr(self.owner, "resume_global_hotkey", None)
        if resume is not None:
            resume()


class ApiSettingsDialog(QDialog):
    """Edit provider drafts for the parent settings window."""

    def __init__(self, owner: SettingsWindow) -> None:
        super().__init__(owner)
        self.owner = owner
        self.settings = deepcopy(owner.api_settings)
        backend = owner.selected_engine()[0]
        self.provider = backend if backend in PROVIDERS else PROVIDERS[0]
        body = _configure_dialog(self, "번역 API 설정", 590, 650)
        self.provider_combo = _SettingsComboBox()
        for provider in PROVIDERS:
            self.provider_combo.addItem(BACKEND_LABELS[provider], provider)
        self.provider_combo.setCurrentIndex(self.provider_combo.findData(self.provider))
        provider_form = _form(body)
        provider_form.addRow("API 서비스", self.provider_combo)

        scroll, fields = _scroll_content()
        body.addWidget(scroll, 1)
        connection = _section(fields, "연결 정보")
        connection_form = _form(connection)
        self.key_entry = QLineEdit()
        connection_form.addRow("API 키", self.key_entry)
        self.account_entry = QLineEdit()
        connection_form.addRow("Cloudflare Account ID", self.account_entry)
        self.show_key = _checkbox(connection, "API 키 표시", False)
        self.show_key.toggled.connect(self._update_key_visibility)
        self._connection_form = connection_form

        model = _section(fields, "모델")
        self.model_combo = _SettingsComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        _form(model).addRow("사용할 모델 ID", self.model_combo)
        model.addWidget(_description("모델 ID를 직접 입력할 수 있습니다. 아래 목록에는 한 줄에 하나씩 입력하세요."))
        self.models_text = QPlainTextEdit()
        self.models_text.setAccessibleName("모델 목록")
        self.models_text.setFixedHeight(82)
        model.addWidget(self.models_text)

        prompt = _section(fields, "번역 프롬프트")
        prompt.addWidget(_description(
            "{source} 원문 언어 · {target} 번역 언어 · {text} 원문\n"
            "프롬프트와 모델 목록은 서비스별로 따로 보관됩니다."
        ))
        self.prompt_text = QPlainTextEdit()
        self.prompt_text.setAccessibleName("번역 프롬프트")
        self.prompt_text.setMinimumHeight(112)
        prompt.addWidget(self.prompt_text)
        fields.addStretch()
        body.addWidget(_description("적용한 뒤 MekiCopy 설정을 저장하면 반영됩니다."))
        _footer(body, self.reject, self._on_apply, confirm_text="적용")
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.models_text.textChanged.connect(self._refresh_models)
        self._load_provider()

    def _models(self) -> list[str]:
        return list(dict.fromkeys(
            line.strip() for line in self.models_text.toPlainText().splitlines() if line.strip()
        ))

    def _store_provider(self) -> None:
        self.settings.profiles[self.provider] = ProviderSettings(
            api_key=self.key_entry.text().strip(),
            account_id=self.account_entry.text().strip(),
            model=self.model_combo.currentText().strip(),
            models=self._models(),
            prompt=self.prompt_text.toPlainText(),
        )

    def _load_provider(self) -> None:
        profile = self.settings.profiles[self.provider]
        self.key_entry.setText(profile.api_key)
        self.account_entry.setText(profile.account_id)
        with QSignalBlocker(self.models_text):
            self.models_text.setPlainText("\n".join(profile.models))
        self.prompt_text.setPlainText(profile.prompt)
        self._refresh_models(profile.model)
        self.show_key.setChecked(False)
        self._update_key_visibility()
        self._connection_form.setRowVisible(self.account_entry, self.provider == "cloudflare")

    def _on_provider_changed(self) -> None:
        self._store_provider()
        self.provider = self.provider_combo.currentData()
        self._load_provider()

    def _refresh_models(self, selected: str | None = None) -> None:
        if selected is None:
            selected = self.model_combo.currentText()
        self.model_combo.clear()
        self.model_combo.addItems(self._models())
        self.model_combo.setEditText(selected)

    def _update_key_visibility(self) -> None:
        echo_mode = QLineEdit.EchoMode.Normal if self.show_key.isChecked() else QLineEdit.EchoMode.Password
        self.key_entry.setEchoMode(echo_mode)

    def _on_apply(self) -> None:
        self._store_provider()
        self.owner.api_settings = deepcopy(self.settings)
        self.owner._update_translation_controls()
        self.accept()

    def done(self, result: int) -> None:
        if self.owner.api_settings_dialog is self:
            self.owner.api_settings_dialog = None
        super().done(result)


class SettingsWindow(QDialog):
    def __init__(self, owner: QWidget) -> None:
        super().__init__(owner)
        self.owner = owner
        self.api_settings: ApiSettings = load_api_settings()
        self.api_settings_dialog: ApiSettingsDialog | None = None
        settings = owner.settings
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, settings.main_always_on_top)
        body = _configure_dialog(self, "MekiCopy 설정", 590, 590)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        body.addWidget(self.tabs, 1)
        for title, build in (
            ("일반", self._build_general_page),
            ("실시간/수동 번역", self._build_translation_page),
            ("번역 오버레이", self._build_overlay_page),
            ("음성인식", self._build_audio_page),
        ):
            scroll, page = _scroll_content()
            build(page, settings)
            page.addStretch()
            self.tabs.addTab(scroll, title)
        _footer(body, self.reject, self._on_save)
        self._update_translation_controls()
        self._update_manual_translation_controls()
        self._on_audio_stt_model_changed()

    def _build_general_page(self, page: QVBoxLayout, settings: AppSettings) -> None:
        windows = _section(page, "앱 동작")
        self.minimize_to_tray = _checkbox(windows, "최소화할 때 시스템 트레이로 이동", settings.minimize_to_tray)
        self.main_topmost = _checkbox(windows, "MekiCopy를 항상 위로", settings.main_always_on_top)
        self.detached_hide_titlebar = _checkbox(
            windows, "분리된 '번역 후 표시' 버튼의 제목표시줄 숨김", settings.detached_hide_titlebar
        )
        self.suppress_magpie_notice = _checkbox(
            windows, "MagPie 실행 안내 숨김", settings.suppress_magpie_launch_notice
        )
        self.debug_logging = _checkbox(windows, "오류 분석을 위한 디버그 로그 켜기", settings.debug_logging)

        translation = _section(page, "번역 엔진")
        translation.addWidget(_description("화면 번역, 음성인식, 자막에서 함께 사용하는 설정입니다."))
        self.hytrans_backend = _SettingsComboBox()
        for label, engine in TRANSLATION_ENGINES.items():
            self.hytrans_backend.addItem(label, engine[0])
        backend = normalize_backend(settings.hytrans_backend)
        profile = get_model_profile(settings.hytrans_model_id) if backend == "local" else MT15_PROFILE
        selected_engine = next(label for label, engine in TRANSLATION_ENGINES.items() if engine == (backend, profile))
        self.hytrans_backend.setCurrentText(selected_engine)
        connection = _form(translation)
        connection.addRow("번역 서비스", self.hytrans_backend)
        self.hytrans_port = _number_field(connection, "HYTrans 포트", settings.hytrans_port, 1, 65535)
        self.translation_model_label = _description("")
        translation.addWidget(self.translation_model_label)
        actions = QHBoxLayout()
        actions.addWidget(_action("API 키 · 모델 · 프롬프트", self._open_api_settings))
        self.hdtrans_settings_button = _action("hdTrans 설치 · 사전 · 문맥", self._open_hdtrans_settings)
        actions.addWidget(self.hdtrans_settings_button)
        translation.addLayout(actions)
        self.hytrans_backend.currentIndexChanged.connect(self._update_translation_controls)

    def _build_translation_page(self, page: QVBoxLayout, settings: AppSettings) -> None:
        realtime = _section(page, "실시간 OCR")
        self.realtime_ocr_enabled = _checkbox(realtime, "실시간 번역 모드 사용", settings.realtime_ocr_enabled)
        self.realtime_ocr_enabled.setToolTip(
            "설정을 저장한 뒤 OCR 영역을 확정하고 HYTrans와 MekiOverlayer를 실행하세요.\n"
            "화면 번역 화면의 '실시간 OCR 시작' 버튼으로 시작할 수 있습니다.\n"
            "조건을 만족하지 않게 되거나 '실시간 OCR 중지'를 누르면 중지됩니다.\n"
            "실시간 모드를 끄면 '번역 후 표시' 버튼으로 수동 번역할 수 있습니다."
        )
        self.realtime_ocr_interval = _SettingsComboBox()
        self.realtime_ocr_interval.addItems([f"{value:g}" for value in REALTIME_INTERVALS])
        self.realtime_ocr_interval.setCurrentText(f"{settings.realtime_ocr_interval:g}")
        _form(realtime).addRow("최소 인식 간격(초)", self.realtime_ocr_interval)

        manual = _section(page, "수동 번역")
        self.manual_controls = QWidget()
        controls = QVBoxLayout(self.manual_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        manual.addWidget(self.manual_controls)
        self.simple_copy_complete = _checkbox(controls, "번역 완료를 간단하게 표시", settings.simple_copy_complete)
        self.detached_topmost = _checkbox(controls, "분리된 '번역 후 표시' 버튼을 항상 위로", settings.detached_always_on_top)
        self.detached_fixed_size = _checkbox(controls, "분리된 '번역 후 표시' 버튼의 크기 고정", settings.detached_fixed_size)
        self.global_hotkey_enabled = _checkbox(controls, "전역 단축키로 '번역 후 표시' 실행", settings.global_hotkey_enabled)

        hotkey_row = QHBoxLayout()
        self.global_hotkey_entry = _HotkeyField(settings.global_hotkey, self.owner)
        hotkey_row.addWidget(self.global_hotkey_entry, 1)
        self.global_hotkey_default_button = _action("기본값 B", self._restore_default_global_hotkey)
        hotkey_row.addWidget(self.global_hotkey_default_button)
        _form(controls).addRow("전역 단축키 설정", hotkey_row)
        controls.addWidget(_description("입력칸을 클릭한 뒤 키를 누르세요. B 같은 일반 키도 단독으로 사용할 수 있습니다."))
        self.realtime_ocr_enabled.toggled.connect(self._update_manual_translation_controls)
        self.global_hotkey_enabled.toggled.connect(self._update_manual_translation_controls)

    def _build_overlay_page(self, page: QVBoxLayout, settings: AppSettings) -> None:
        window = _section(page, "번역결과 표시기")
        self.overlayer_port = _number_field(_form(window), "MekiOverlayer 포트", settings.overlayer_port, 1, 65535)
        self.overlayer_topmost = _checkbox(window, "MekiOverlayer를 항상 위로", settings.overlayer_always_on_top)
        self.overlayer_hide_titlebar = _checkbox(window, "제목표시줄 숨김", settings.overlayer_hide_titlebar)
        self.overlayer_fixed_size = _checkbox(window, "창 크기 고정", settings.overlayer_fixed_size)
        self.overlayer_exclude_capture = _checkbox(window, "화면 캡처에서 오버레이 제외", settings.overlayer_exclude_from_capture)

        appearance = _form(_section(page, "오버레이 모양"))
        self.overlayer_bg_color = _ColorButton(self, "배경색", settings.overlayer_bg_color)
        appearance.addRow("배경색", self.overlayer_bg_color)
        self.overlayer_opacity = _opacity_slider(appearance, "배경 불투명도", settings.overlayer_bg_opacity)
        self.overlayer_text_color = _ColorButton(self, "글자색", settings.overlayer_text_color)
        appearance.addRow("글자색", self.overlayer_text_color)
        self.overlayer_text_size = _number_field(appearance, "글자 크기", settings.overlayer_text_size, 8, 96)
        self.overlayer_text_font = _font_picker(QFontDatabase.WritingSystem.Korean, settings.overlayer_text_font)
        appearance.addRow("글꼴", self.overlayer_text_font)

    def _build_audio_page(self, page: QVBoxLayout, settings: AppSettings) -> None:
        audio = _form(_section(page, "음성인식"))
        self.audio_stt_model = _SettingsComboBox()
        for model, label in STT_MODEL_LABELS.items():
            self.audio_stt_model.addItem(label, model)
        self.audio_stt_model.setCurrentIndex(max(0, self.audio_stt_model.findData(settings.audio_stt_model)))
        audio.addRow("STT 모델", self.audio_stt_model)
        self.audio_stt_precision = _SettingsComboBox()
        self.audio_stt_precision.addItems(["int8", "fp32"])
        self.audio_stt_precision.setCurrentText(settings.audio_stt_precision)
        self._reazonspeech_precision = self.audio_stt_precision.currentText()
        audio.addRow("정밀도", self.audio_stt_precision)
        self.audio_capture_port = _number_field(audio, "MekiAudioCapture 포트", settings.audio_capture_port, 1, 65535)
        self.audio_chunk_preset = _SettingsComboBox()
        self.audio_chunk_preset.addItems(["FAST", "BALANCED", "LONG"])
        self.audio_chunk_preset.setCurrentText(settings.audio_chunk_preset)
        audio.addRow("음성 구간 길이", self.audio_chunk_preset)
        self.audio_stt_model.currentIndexChanged.connect(self._on_audio_stt_model_changed)

        script = _section(page, "MekiScript")
        self.script_topmost = _checkbox(script, "MekiScript를 항상 위로", settings.script_always_on_top)
        appearance = _form(script)
        self.script_port = _number_field(appearance, "MekiScript 포트", settings.script_port, 1, 65535)
        self.script_bg_color = _ColorButton(self, "자막 배경색", settings.script_bg_color)
        appearance.addRow("배경색", self.script_bg_color)
        self.script_opacity = _opacity_slider(appearance, "배경 불투명도", settings.script_bg_opacity)

        original = _form(_section(page, "원문 자막"))
        self.script_original_color = _ColorButton(self, "원문 자막 글자색", settings.script_original_text_color)
        original.addRow("글자색", self.script_original_color)
        self.script_original_size = _number_field(original, "글자 크기", settings.script_original_text_size, 8, 96)
        self.script_original_font = _font_picker(QFontDatabase.WritingSystem.Japanese, settings.script_original_text_font)
        original.addRow("글꼴", self.script_original_font)

        translated = _form(_section(page, "번역 자막"))
        self.script_translated_color = _ColorButton(self, "번역 자막 글자색", settings.script_translated_text_color)
        translated.addRow("글자색", self.script_translated_color)
        self.script_translated_size = _number_field(translated, "글자 크기", settings.script_translated_text_size, 8, 96)
        self.script_translated_font = _font_picker(QFontDatabase.WritingSystem.Korean, settings.script_translated_text_font)
        translated.addRow("글꼴", self.script_translated_font)

    def selected_engine(self):
        return TRANSLATION_ENGINES[self.hytrans_backend.currentText()]

    def _open_api_settings(self) -> None:
        if self.api_settings_dialog is None:
            self.api_settings_dialog = ApiSettingsDialog(self)
        _show_dialog(self.api_settings_dialog)

    def _open_hdtrans_settings(self) -> None:
        self.owner._on_hdtrans_settings()

    def _update_translation_controls(self) -> None:
        backend, model = self.selected_engine()
        if backend == "local":
            text = model.display_label
            if model == PLAMO3_PROFILE:
                text += " · Built with PLaMo"
        elif backend == "translator_api":
            text = "브라우저 내장 Translator API · 일본어 → 한국어 (첫 사용 시 모델 다운로드)"
        elif backend == "hdtrans":
            text = "hdTrans · 일본어 → 한국어 · 브라우저/API 키 불필요 · Lite 첫 사용 시 다운로드"
        else:
            profile = self.api_settings.profiles[backend]
            text = f"API 모델 {profile.model or '(모델 ID를 입력하세요)'}"
        self.translation_model_label.setText(text)
        self.hdtrans_settings_button.setEnabled(backend == "hdtrans")

    def _update_manual_translation_controls(self) -> None:
        manual_enabled = not self.realtime_ocr_enabled.isChecked()
        self.manual_controls.setEnabled(manual_enabled)
        hotkey_enabled = manual_enabled and self.global_hotkey_enabled.isChecked()
        self.global_hotkey_entry.setEnabled(hotkey_enabled)
        self.global_hotkey_default_button.setEnabled(hotkey_enabled)

    def _restore_default_global_hotkey(self) -> None:
        self.global_hotkey_entry.setText(DEFAULT_GLOBAL_HOTKEY)

    def _on_audio_stt_model_changed(self) -> None:
        is_parakeet = self.audio_stt_model.currentData() == "parakeet"
        if is_parakeet and self.audio_stt_precision.isEnabled():
            self._reazonspeech_precision = self.audio_stt_precision.currentText()
        self.audio_stt_precision.setCurrentText("int8" if is_parakeet else self._reazonspeech_precision)
        self.audio_stt_precision.setEnabled(not is_parakeet)

    def _collect_settings(self) -> AppSettings:
        current = self.owner.settings
        backend, model = self.selected_engine()
        if backend in PROVIDERS:
            error = validate_provider_settings(self.api_settings.profiles[backend], backend)
            if error:
                raise ValueError(f"{BACKEND_LABELS[backend]} API 설정을 확인하세요.\n{error}")
        global_hotkey = parse_hotkey(self.global_hotkey_entry.text())
        if global_hotkey is None:
            raise ValueError("전역 단축키는 B 또는 Ctrl+Alt+B처럼 키 하나를 포함해 입력하세요.")
        ports = (self.hytrans_port.value(), self.overlayer_port.value(),
                 self.audio_capture_port.value(), self.script_port.value())
        validate_unique_ports(dict(zip(("HYTrans", "MekiOverlayer", "MekiAudioCapture", "MekiScript"), ports)))
        detached_geometry = load_detached_geometry(current.detached_geometry)
        detached_fixed_size = self.detached_fixed_size.isChecked()
        detached_size = _geometry_size(detached_geometry) if detached_fixed_size else None
        return replace(
            current,
            minimize_to_tray=self.minimize_to_tray.isChecked(),
            main_always_on_top=self.main_topmost.isChecked(),
            detached_always_on_top=self.detached_topmost.isChecked(),
            detached_hide_titlebar=self.detached_hide_titlebar.isChecked(),
            detached_fixed_size=detached_fixed_size,
            simple_copy_complete=self.simple_copy_complete.isChecked(),
            global_hotkey_enabled=self.global_hotkey_enabled.isChecked(),
            global_hotkey=global_hotkey.text,
            detached_geometry=detached_geometry,
            detached_fixed_width=detached_size[0] if detached_size else current.detached_fixed_width,
            detached_fixed_height=detached_size[1] if detached_size else current.detached_fixed_height,
            realtime_ocr_enabled=self.realtime_ocr_enabled.isChecked(),
            realtime_ocr_interval=normalize_interval(self.realtime_ocr_interval.currentText()),
            hytrans_backend=backend,
            hytrans_model_id=model.key,
            hytrans_port=ports[0],
            overlayer_port=ports[1],
            audio_capture_port=ports[2],
            script_port=ports[3],
            overlayer_always_on_top=self.overlayer_topmost.isChecked(),
            overlayer_hide_titlebar=self.overlayer_hide_titlebar.isChecked(),
            overlayer_fixed_size=self.overlayer_fixed_size.isChecked(),
            overlayer_exclude_from_capture=self.overlayer_exclude_capture.isChecked(),
            overlayer_bg_color=self.overlayer_bg_color.color(),
            overlayer_bg_opacity=self.overlayer_opacity.value() / 100,
            overlayer_text_color=self.overlayer_text_color.color(),
            overlayer_text_size=self.overlayer_text_size.value(),
            overlayer_text_font=self.overlayer_text_font.currentText(),
            audio_stt_model=self.audio_stt_model.currentData(),
            audio_stt_precision=self.audio_stt_precision.currentText(),
            audio_chunk_preset=self.audio_chunk_preset.currentText(),
            script_always_on_top=self.script_topmost.isChecked(),
            script_bg_color=self.script_bg_color.color(),
            script_bg_opacity=self.script_opacity.value() / 100,
            script_original_text_color=self.script_original_color.color(),
            script_original_text_size=self.script_original_size.value(),
            script_original_text_font=self.script_original_font.currentText(),
            script_translated_text_color=self.script_translated_color.color(),
            script_translated_text_size=self.script_translated_size.value(),
            script_translated_text_font=self.script_translated_font.currentText(),
            suppress_magpie_launch_notice=self.suppress_magpie_notice.isChecked(),
            debug_logging=self.debug_logging.isChecked(),
        )

    def _on_save(self) -> None:
        if self.api_settings_dialog is not None:
            self.api_settings_dialog._on_apply()
        try:
            settings = self._collect_settings()
        except ValueError as exc:
            QMessageBox.critical(self, "MekiCopy", str(exc))
            return
        result = self.owner.apply_settings(settings, persist=True, api_settings=self.api_settings)
        if result is not None:
            self.accept()

    def done(self, result: int) -> None:
        if self.api_settings_dialog is not None:
            self.api_settings_dialog.reject()
        if getattr(self.owner, "settings_window", None) is self:
            self.owner.settings_window = None
        super().done(result)
