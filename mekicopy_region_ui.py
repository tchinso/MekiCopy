from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

import mss
from PySide6.QtCore import QEventLoop, QRectF, Qt
from PySide6.QtGui import QColor, QCursor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QVBoxLayout, QWidget

from mekicopy_capture import MIN_SIZE_PX, Region, enable_dpi_awareness
from mekicopy_ocr import ocr_and_translate
from mekicopy_runtime import _set_window_icon
from mekicopy_settings import Bookmark, Rect, load_bookmarks
from mekicopy_theme import (
    ACCENT_INK,
    BG,
    OUTLINE,
    RoundedButton,
    configure_window_theme,
    ensure_application,
    make_font,
    messagebox,
)

EDGE_GRAB_PX = 8


class _DesktopPane(QWidget):
    """Draw a single screen in Qt coordinates while the selection stays in pixels."""

    def __init__(self, owner: _DesktopOverlay, screen, bounds: Rect, parent: QWidget | None) -> None:
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        super().__init__(parent, flags)
        self.owner = owner
        self.bounds = bounds
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if owner.selecting:
            self.setMouseTracking(True)
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())

    def _rect_on_screen(self, rect: Rect) -> QRectF:
        sx = self.width() / self.bounds.width
        sy = self.height() / self.bounds.height
        return QRectF(
            (rect.left - self.bounds.left) * sx,
            (rect.top - self.bounds.top) * sy,
            rect.width * sx,
            rect.height * sy,
        )

    def _physical_pointer(self, event) -> tuple[int, int]:
        if os.name == "nt":
            point = wintypes.POINT()
            # Qt logical desktop coordinates can have gaps on mixed-DPI
            # Windows desktops. Capture coordinates are always physical.
            if ctypes.windll.user32.GetPhysicalCursorPos(ctypes.byref(point)):
                return point.x, point.y
        pos = event.position()
        return (
            self.bounds.left + round(pos.x() * self.bounds.width / self.width()),
            self.bounds.top + round(pos.y() * self.bounds.height / self.height()),
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(24, 12, 24, 95)
        painter.fillRect(event.rect(), color)

        selected = self.owner.selection
        if self.owner.selecting and selected is not None:
            selected_rect = self._rect_on_screen(selected.normalized())
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            # Keep a nearly transparent pixel so Windows still delivers mouse
            # input inside the selected area of this layered window.
            painter.fillRect(selected_rect, QColor(0, 0, 0, 3))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            self._outline(painter, selected_rect, "#ffbad0", 2)
            for point in (
                (selected_rect.left(), selected_rect.center().y()),
                (selected_rect.right(), selected_rect.center().y()),
                (selected_rect.center().x(), selected_rect.top()),
                (selected_rect.center().x(), selected_rect.bottom()),
            ):
                x, y = point
                painter.setBrush(QColor(BG))
                painter.drawEllipse(QRectF(x - 4, y - 4, 8, 8))

        for region, outline, width in self.owner.annotations:
            rect = Rect(region.left, region.top, region.left + region.width, region.top + region.height)
            self._outline(painter, self._rect_on_screen(rect), outline, width)

        font = make_font(("Malgun Gothic", 11, "bold"))
        metrics = QFontMetrics(font)
        box = QRectF(14, 14, metrics.horizontalAdvance(self.owner.instruction) + 24, 33)
        painter.setPen(QPen(QColor(OUTLINE), 1.0))
        painter.setBrush(QColor(BG))
        painter.drawRoundedRect(box, 12, 12)
        painter.setFont(font)
        painter.setPen(QColor(ACCENT_INK))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self.owner.instruction)
        painter.end()

    @staticmethod
    def _outline(painter: QPainter, rectangle: QRectF, outline: str, width: int) -> None:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#59213a"), width + 2))
        painter.drawRect(rectangle)
        painter.setPen(QPen(QColor(outline), width))
        painter.drawRect(rectangle)

    def mousePressEvent(self, event) -> None:
        if self.owner.selecting and event.button() == Qt.MouseButton.LeftButton:
            self.owner.start_drag(*self._physical_pointer(event))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.owner.selecting:
            x, y = self._physical_pointer(event)
            if event.buttons() & Qt.MouseButton.LeftButton:
                self.owner.drag_to(x, y)
            else:
                hit = self.owner.edge_at(x, y)
                cursor = {
                    "left": Qt.CursorShape.SizeHorCursor,
                    "right": Qt.CursorShape.SizeHorCursor,
                    "top": Qt.CursorShape.SizeVerCursor,
                    "bottom": Qt.CursorShape.SizeVerCursor,
                    "move": Qt.CursorShape.SizeAllCursor,
                }.get(hit, Qt.CursorShape.CrossCursor)
                self.setCursor(cursor)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self.owner.selecting and event.button() == Qt.MouseButton.LeftButton:
            self.owner.drag_to(*self._physical_pointer(event))
            self.owner.end_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.owner.finish()
        elif self.owner.selecting and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.owner.confirm()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self.owner.finish()
        super().closeEvent(event)


class _DesktopOverlay:
    def __init__(
        self,
        parent: QWidget | None,
        instruction: str,
        *,
        selecting: bool = False,
        initial_region: Region | Bookmark | None = None,
        annotations: tuple[tuple[Region, str, int], ...] = (),
    ) -> None:
        app = ensure_application()
        with mss.mss() as capture:
            monitors = list(capture.monitors[1:])

        self.annotations = annotations
        self.instruction = instruction
        self.selecting = selecting
        self.selection = (
            Rect(initial_region.left, initial_region.top,
                 initial_region.left + initial_region.width, initial_region.top + initial_region.height)
            if initial_region is not None else None
        )
        self.drawn_selection = self.selection.normalized() if self.selection is not None else None
        self.result: Region | None = None
        self.drag_mode: str | None = None
        self.start_point: tuple[int, int] | None = None
        self.loop = QEventLoop()
        self.panes = []
        screens = app.screens()
        for screen in screens:
            geometry = screen.geometry()
            ratio = screen.devicePixelRatio()
            monitor = min(
                monitors,
                key=lambda item: (
                    abs(item["left"] - geometry.x()) + abs(item["top"] - geometry.y()),
                    abs(item["width"] - geometry.width() * ratio) +
                    abs(item["height"] - geometry.height() * ratio),
                ),
                default=None,
            )
            if monitor is None:
                left, top = geometry.x(), geometry.y()
                width, height = round(geometry.width() * ratio), round(geometry.height() * ratio)
            else:
                left, top = monitor["left"], monitor["top"]
                width, height = monitor["width"], monitor["height"]
                monitors.remove(monitor)
            self.panes.append(_DesktopPane(self, screen, Rect(left, top, left + width, top + height), parent))

    def exec(self) -> Region | None:
        if not self.panes:
            return None
        for pane in self.panes:
            pane.show()
            pane.raise_()
        pointer = QCursor.pos()
        active_pane = next((pane for pane in self.panes if pane.geometry().contains(pointer)), self.panes[0])
        active_pane.activateWindow()
        active_pane.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.loop.exec()
        for pane in self.panes:
            pane.deleteLater()
        ensure_application().processEvents()
        return self.result

    def finish(self) -> None:
        for pane in self.panes:
            pane.hide()
        self.loop.quit()

    def confirm(self) -> None:
        if self.selection is None:
            return
        rect = self.selection.normalized()
        if rect.width < MIN_SIZE_PX or rect.height < MIN_SIZE_PX:
            return
        self.result = Region(rect.left, rect.top, rect.width, rect.height)
        self.finish()

    def edge_at(self, x: int, y: int) -> str | None:
        if self.selection is None:
            return None
        rect = self.selection.normalized()
        if rect.top <= y <= rect.bottom:
            if abs(x - rect.left) <= EDGE_GRAB_PX:
                return "left"
            if abs(x - rect.right) <= EDGE_GRAB_PX:
                return "right"
        if rect.left <= x <= rect.right:
            if abs(y - rect.top) <= EDGE_GRAB_PX:
                return "top"
            if abs(y - rect.bottom) <= EDGE_GRAB_PX:
                return "bottom"
            if rect.top <= y <= rect.bottom:
                return "move"
        return None

    def start_drag(self, x: int, y: int) -> None:
        self.drag_mode = self.edge_at(x, y) or "new"
        self.start_point = (x, y)
        if self.drag_mode == "new":
            self.selection = Rect(x, y, x, y)
            self._redraw()

    def drag_to(self, x: int, y: int) -> None:
        if self.start_point is None or self.selection is None:
            return
        rect = self.selection
        if self.drag_mode == "new":
            rect.right, rect.bottom = x, y
        elif self.drag_mode == "move":
            dx, dy = x - self.start_point[0], y - self.start_point[1]
            rect.left += dx
            rect.right += dx
            rect.top += dy
            rect.bottom += dy
            self.start_point = (x, y)
        elif self.drag_mode == "left":
            rect.left = x
        elif self.drag_mode == "right":
            rect.right = x
        elif self.drag_mode == "top":
            rect.top = y
        elif self.drag_mode == "bottom":
            rect.bottom = y
        self._redraw()

    def end_drag(self) -> None:
        self.start_point = None
        self.drag_mode = None
        if self.selection is not None:
            rect = self.selection.normalized()
            self.selection = rect if rect.width >= MIN_SIZE_PX and rect.height >= MIN_SIZE_PX else None
        self._redraw()

    def _redraw(self) -> None:
        before = self.drawn_selection
        after = self.selection.normalized() if self.selection is not None else None
        self.drawn_selection = after
        changed = [rect for rect in (before, after) if rect is not None]
        for pane in self.panes:
            for rect in changed:
                dirty = pane._rect_on_screen(rect).adjusted(-8, -8, 8, 8).toAlignedRect()
                pane.update(dirty.intersected(pane.rect()))


class BookmarkPicker(QDialog):
    def __init__(self, bookmarks: dict[str, Bookmark], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        configure_window_theme(self)
        _set_window_icon(self)
        self.setWindowTitle("MekiCopy 북마크 선택")
        self.resize(334, min(285, 102 + 24 * len(bookmarks)))
        self.bookmarks = bookmarks
        self.selected: Bookmark | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        layout.addWidget(QLabel("북마크를 선택하세요", self))
        self.listbox = QListWidget(self)
        self.listbox.setStyleSheet("QListWidget::item { padding: 3px 5px; border-radius: 4px; }")
        self.listbox.addItems(sorted(bookmarks))
        self.listbox.setCurrentRow(0)
        self.listbox.itemDoubleClicked.connect(lambda _item: self._on_select())
        layout.addWidget(self.listbox)

        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(RoundedButton(self, "취소", self.reject, height=31, radius=11, variant="subtle"))
        choose = RoundedButton(self, "선택", self._on_select, height=31, radius=11, variant="primary")
        choose.setDefault(True)
        actions.addWidget(choose)
        layout.addLayout(actions)
        self.listbox.setFocus()

    def _on_select(self) -> None:
        item = self.listbox.currentItem()
        if item is not None:
            self.selected = self.bookmarks[item.text()]
            self.accept()


def pick_bookmark(parent: QWidget | None = None) -> Bookmark | None:
    bookmarks = load_bookmarks()
    if not bookmarks:
        messagebox.showerror("MekiCopy", "저장된 북마크가 없습니다.", parent=parent)
        return None
    ensure_application()
    dialog = BookmarkPicker(bookmarks, parent)
    dialog.exec()
    return dialog.selected


def run_picker_and_capture(parent: QWidget | None = None) -> None:
    bookmark = pick_bookmark(parent=parent)
    if bookmark:
        ocr_and_translate(bookmark.left, bookmark.top, bookmark.width, bookmark.height, parent=parent)


def run_selection(
    initial_region: Region | Bookmark | None = None,
    capture_on_enter: bool = True,
    parent: QWidget | None = None,
) -> Region | None:
    enable_dpi_awareness()
    instruction = "드래그로 영역 조정  ·  Enter 번역 후 표시  ·  Esc 취소" if capture_on_enter else (
        "드래그로 영역 조정  ·  Enter 선택  ·  Esc 취소"
    )
    region = _DesktopOverlay(parent, instruction, selecting=True, initial_region=initial_region).exec()
    if region is not None and capture_on_enter:
        if os.name == "nt":
            ctypes.windll.dwmapi.DwmFlush()
        ocr_and_translate(region.left, region.top, region.width, region.height, parent=parent)
    return region


def run_region_view(
    draft_region: Region | None,
    active_region: Region | None,
    parent: QWidget | None = None,
) -> None:
    enable_dpi_awareness()
    annotations = tuple(
        (region, color, width) for region, color, width in (
            (draft_region, "#82dcff", 3), (active_region, "#ff9ab8", 3),
        ) if region is not None
    )
    _DesktopOverlay(parent, "하늘색 임시 영역  ·  분홍색 확정 영역  ·  Esc 닫기", annotations=annotations).exec()
