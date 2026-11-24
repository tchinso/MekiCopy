from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QWidget,
)

PINK = "#ff7da4"
ROSE = "#f86e83"
OUTLINE = "#feb3c7"
BORDER = "#ffd3de"
SOFT = "#ffe6ec"
BG = "#fff4f7"
SURFACE = "#ffffff"
INK = "#2d292a"
MUTED = "#725964"
LILAC = "#f7ecff"
SUCCESS = "#31b66b"
ACCENT_INK = "#982b53"
DISABLED_BG = "#f6edf1"
DISABLED_FG = "#826d77"

DEFAULT_FONT = ("Malgun Gothic", 10)
TITLE_FONT = ("Malgun Gothic", 13, "bold")
BUTTON_FONT = ("Malgun Gothic", 10, "bold")
SMALL_FONT = ("Malgun Gothic", 9)

_STYLESHEET = f"""
QWidget {{ color: {INK}; }}
QWidget#MekiWindow, QDialog {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="accent"] {{ color: {ACCENT_INK}; }}
QFrame[role="card"] {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 23px; }}
QFrame[role="note"] {{ background: {LILAC}; border: 1px solid #eaddf4; border-radius: 14px; }}
QFrame[role="inset"] {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 14px; }}
QPushButton, QToolButton {{
    background: {SOFT}; color: {INK}; border: 1px solid {BORDER};
    border-radius: 14px; padding: 7px 13px; font-weight: bold;
}}
QPushButton:hover, QToolButton:hover {{ background: {LILAC}; border-color: {OUTLINE}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {BORDER}; }}
QPushButton:focus, QToolButton:focus {{ border: 2px solid {ACCENT_INK}; padding: 6px 12px; }}
QPushButton:disabled, QToolButton:disabled {{
    background: {DISABLED_BG}; color: {DISABLED_FG}; border-color: {DISABLED_BG};
}}
QPushButton[variant="primary"] {{ background: {PINK}; color: #351522; border-color: #ea6d95; }}
QPushButton[variant="primary"]:hover {{ background: #ff9bbb; border-color: #ed719a; }}
QPushButton[variant="primary"]:pressed {{ background: {ROSE}; }}
QPushButton[variant="primary"]:disabled {{ background: {DISABLED_BG}; color: {DISABLED_FG}; border-color: {DISABLED_BG}; }}
QPushButton[variant="subtle"] {{ background: {SURFACE}; border-color: {BORDER}; }}
QPushButton[variant="subtle"]:hover {{ background: {SOFT}; }}
QPushButton[variant="tab-active"] {{ background: {SURFACE}; color: {ACCENT_INK}; border-color: {OUTLINE}; }}
QPushButton[variant="success"] {{ background: #d8f5e4; color: #175636; border-color: #a1dbb7; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QListWidget {{
    background: {SURFACE}; color: {INK}; border: 1px solid {BORDER};
    border-radius: 10px; padding: 5px 8px;
    selection-background-color: {SOFT}; selection-color: {ACCENT_INK};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:focus, QListWidget:focus {{ border-color: {ACCENT_INK}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    background: {DISABLED_BG}; color: {DISABLED_FG};
}}
QComboBox::drop-down {{ border: 0; width: 25px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER}; }}
QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {DISABLED_FG}; }}
QGroupBox {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 16px;
    margin-top: 12px; padding: 13px 10px 9px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 5px; color: {ACCENT_INK}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; background: {SURFACE}; border-radius: 14px; }}
QTabBar::tab {{
    background: {SOFT}; border: 1px solid {BORDER}; border-radius: 10px;
    padding: 8px 12px; margin: 2px;
}}
QTabBar::tab:selected {{ background: {SURFACE}; color: {ACCENT_INK}; font-weight: bold; }}
QTabBar::tab:hover {{ background: {LILAC}; }}
QScrollArea {{ background: transparent; border: 0; }}
QScrollBar:vertical {{ border: 0; background: transparent; width: 11px; margin: 3px; }}
QScrollBar::handle:vertical {{ background: {OUTLINE}; min-height: 28px; border-radius: 3px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QProgressBar {{
    background: {SOFT}; border: 0; border-radius: 7px;
    text-align: center; color: {INK}; min-height: 14px;
}}
QProgressBar::chunk {{ background: {PINK}; border-radius: 7px; }}
QToolTip {{ background: {SURFACE}; color: {INK}; border: 1px solid {OUTLINE}; padding: 6px; }}
"""

_application: QApplication | None = None


def ensure_application() -> QApplication:
    global _application
    if _application is None:
        _application = QApplication.instance() or QApplication([])
        _application.setApplicationName("MekiCopy")
        _application.setStyle("Fusion")
        _application.setStyleSheet(_STYLESHEET)
        _application.setFont(make_font(DEFAULT_FONT))
    return _application


def configure_window_theme(window: QWidget) -> None:
    ensure_application()
    if not window.objectName():
        window.setObjectName("MekiWindow")


def make_font(spec: tuple[str, int] | tuple[str, int, str]) -> QFont:
    font = QFont(spec[0], spec[1])
    font.setBold(len(spec) > 2 and spec[2] == "bold")
    return font


class RoundedButton(QPushButton):
    def __init__(
        self,
        parent: QWidget | None = None,
        text: str = "",
        command: Callable[[], None] | None = None,
        *,
        height: int = 40,
        radius: int = 14,
        variant: str = "normal",
        width: int | None = None,
        anchor: str = "center",
        font: tuple[str, int] | tuple[str, int, str] = BUTTON_FONT,
        focus_on_click: bool = True,
    ) -> None:
        super().__init__(text, parent)
        self.setMinimumHeight(height)
        if width is not None:
            self.setMinimumWidth(width)
        self.setFont(make_font(font))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if not focus_on_click:
            self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setStyleSheet(
            f"QPushButton {{ border-radius: {radius}px; text-align: "
            f"{'left' if anchor == 'w' else 'center'}; }}"
        )
        self.set_variant(variant)
        if command is not None:
            self.clicked.connect(lambda: command())

    def set_variant(self, variant: str) -> None:
        if self.property("variant") != variant:
            self.setProperty("variant", variant)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()


def style_color_button(button: QPushButton, color: str) -> None:
    from PySide6.QtGui import QColor

    selected = QColor(color)
    foreground = INK if selected.lightnessF() > 0.55 else SURFACE
    button.setStyleSheet(
        f"QPushButton {{ background: {selected.name()}; color: {foreground}; border-color: {OUTLINE}; }}"
    )


class TimerHost:
    """Provide cancellable, GUI-thread callbacks for scheduled controllers."""

    def after(self, delay_ms: int, callback: Callable, *args) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)

        def run() -> None:
            timer.deleteLater()
            callback(*args)

        timer.timeout.connect(run)
        timer.start(max(0, int(delay_ms)))
        return timer

    def after_cancel(self, timer: QTimer) -> None:
        try:
            timer.stop()
            timer.deleteLater()
        except RuntimeError:
            pass


class _Messages:
    @staticmethod
    def _show(icon, title: str, text: str, parent: QWidget | None, *, question: bool = False):
        ensure_application()
        buttons = (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                   if question else QMessageBox.StandardButton.Ok)
        box = QMessageBox(icon, title, text, buttons, parent)
        box.setTextFormat(Qt.TextFormat.PlainText)
        if question:
            box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec()

    def showerror(self, title: str, text: str, *, parent: QWidget | None = None) -> None:
        self._show(QMessageBox.Icon.Critical, title, text, parent)

    def showwarning(self, title: str, text: str, *, parent: QWidget | None = None) -> None:
        self._show(QMessageBox.Icon.Warning, title, text, parent)

    def showinfo(self, title: str, text: str, *, parent: QWidget | None = None) -> None:
        self._show(QMessageBox.Icon.Information, title, text, parent)

    def askyesno(self, title: str, text: str, *, parent: QWidget | None = None) -> bool:
        return self._show(QMessageBox.Icon.Question, title, text, parent, question=True) == QMessageBox.StandardButton.Yes


class _Input:
    @staticmethod
    def askstring(title: str, text: str, *, parent: QWidget | None = None) -> str | None:
        ensure_application()
        value, accepted = QInputDialog.getText(parent, title, text)
        return value if accepted else None


messagebox = _Messages()
simpledialog = _Input()
