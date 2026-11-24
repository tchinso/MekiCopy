from __future__ import annotations

import argparse
from collections import deque
import os
import queue
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QCloseEvent, QFont, QPalette, QTextBlock, QTextBlockFormat, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QTextEdit, QVBoxLayout, QWidget

from app_identity import apply_qt_icon, set_windows_app_id
from mekicopy_theme import TimerHost, ensure_application, messagebox
from qt_companion import UiWakeup
from companion_json import MAX_REQUEST_BYTES, read_json as _read_json, write_json as _write_json
from display_config import apply_display_config, normalize_font_name
from companion_liveness import UiHeartbeat
from companion_manual_close import MANUAL_CLOSE_EXIT_CODE, publish_manual_close_signal
from service_ports import SCRIPT_DEFAULT_PORT
from system_logging import (
    capture_windowed_streams,
    configure_system_logging,
    is_debug_enabled,
    install_exception_hooks,
    log_debug,
    log_directory,
    log_error,
    set_debug_enabled,
)

DEFAULT_PORT = SCRIPT_DEFAULT_PORT


@dataclass
class ScriptConfig:
    topmost: bool = True
    bg_color: str = "#111111"
    opacity: float = 0.90
    original_color: str = "#f4f4f5"
    original_size: int = 20
    original_font: str = "Yu Gothic UI"
    translated_color: str = "#7dd3fc"
    translated_size: int = 20
    translated_font: str = "Malgun Gothic"
    debug_log: bool = False

    def update(self, data: dict[str, Any]) -> None:
        apply_display_config(self, data)
        set_debug_enabled(self.debug_log)


@dataclass
class _TranscriptHistoryEntry:
    """One entry with a stable document block for its translation."""

    entry_id: str
    translation: QTextBlock
    pending: bool
    text_length: int


class ScriptWindow(QWidget, TimerHost):
    # Bounds keep the transcript responsive even when producers run ahead of the UI.
    MAX_PENDING_EVENTS = 128
    MAX_EVENTS_PER_DRAIN = 24
    MAX_TRANSCRIPT_CHARS = 32 * 1024
    MAX_ENTRY_ID_CHARS = 512
    MAX_CONFIG_VALUE_CHARS = 4 * 1024
    # MekiScript is a live companion that can stay open for hours. Keep a
    # useful scrollback without retaining every historical line and ID for an
    # entire play session. The character limit also bounds rare long outputs.
    MAX_HISTORY_ENTRIES = 2048
    MAX_HISTORY_CHARS = 512 * 1024
    _PENDING_TRANSLATION_TEXT = "번역 대기 중…"

    def __init__(
        self,
        config: ScriptConfig,
        *,
        on_confirmed_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._wakeup = UiWakeup(self, self.drain)
        self.config = config
        self._on_confirmed_close = on_confirmed_close
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue(
            maxsize=self.MAX_PENDING_EVENTS
        )
        self._event_count_lock = threading.Lock()
        self._dropped_event_count = 0
        self._trimmed_payload_count = 0
        self.entry_ids: set[str] = set()
        self._history: deque[_TranscriptHistoryEntry] = deque()
        self._history_by_id: dict[str, _TranscriptHistoryEntry] = {}
        self._history_chars = 0
        self.last_original = ""
        self.last_translation = ""
        self.translation_count = 0
        self._applied_appearance: tuple[Any, ...] | None = None
        self.setWindowTitle("MekiScript")
        self.setMinimumSize(280, 160)
        self.resize(720, 480)
        self.move(120, 120)
        apply_qt_icon(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setUndoRedoEnabled(False)
        self.text.setFrameShape(QTextEdit.Shape.NoFrame)
        self.text.document().setDocumentMargin(7)
        layout.addWidget(self.text)
        self.apply_config()

    def apply_config(self) -> None:
        cfg = self.config
        changes_topmost = bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) != cfg.topmost
        if changes_topmost:
            visible = self.isVisible()
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, cfg.topmost)
            if visible:
                self.show()
        self.setWindowOpacity(cfg.opacity)
        appearance = (
            cfg.bg_color, cfg.original_color, cfg.original_size, cfg.original_font,
            cfg.translated_color, cfg.translated_size, cfg.translated_font,
        )
        if appearance == self._applied_appearance:
            return
        self._applied_appearance = appearance
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(cfg.bg_color))
        palette.setColor(QPalette.ColorRole.Base, QColor(cfg.bg_color))
        palette.setColor(QPalette.ColorRole.Text, QColor(cfg.original_color))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self.text.setStyleSheet(
            f"QTextEdit {{ background: {QColor(cfg.bg_color).name()}; border: 0; border-radius: 14px; }}"
        )
        formats = {kind: self._line_format(kind) for kind in ("original", "pending", "translated")}
        cursor = QTextCursor(self.text.document())
        cursor.beginEditBlock()
        for entry in self._history:
            self._format_line(entry.translation.previous(), formats["original"])
            self._format_line(entry.translation, formats["pending" if entry.pending else "translated"])
        cursor.endEditBlock()

    def _line_format(self, kind: str) -> QTextCharFormat:
        cfg = self.config
        original = kind == "original"
        pending = kind == "pending"
        font = QFont(
            normalize_font_name(cfg.original_font if original else cfg.translated_font),
            cfg.original_size if original else max(8, cfg.translated_size - (2 if pending else 0)),
        )
        font.setBold(original)
        font.setItalic(pending)
        char_format = QTextCharFormat()
        char_format.setFont(font)
        char_format.setForeground(QColor(cfg.original_color if original else cfg.translated_color))
        return char_format

    def _format_line(self, block: QTextBlock, char_format: QTextCharFormat) -> None:
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
        cursor.setCharFormat(char_format)

    @staticmethod
    def _should_report_count(count: int) -> bool:
        """Log the first overload and then only exponentially spaced repeats."""
        return count == 1 or (count > 0 and count & (count - 1) == 0)

    def _record_payload_trim(self, kind: str, field: str, original_length: int) -> None:
        with self._event_count_lock:
            self._trimmed_payload_count += 1
            count = self._trimmed_payload_count
        if self._should_report_count(count):
            log_error(
                "event_payload_trimmed",
                (
                    f"Trimmed oversized {kind}.{field} payload (chars={original_length}); "
                    f"trimmed_total={count}."
                ),
            )

    def _bounded_text(self, kind: str, field: str, value: Any, limit: int) -> str:
        text = str(value)
        if len(text) <= limit:
            return text
        self._record_payload_trim(kind, field, len(text))
        return text[:limit]

    def _compact_event_payload(self, kind: str, payload: Any) -> Any:
        """Keep queued payloads small and discard fields the UI never consumes."""
        if kind in {"append", "translation"}:
            source = payload if isinstance(payload, dict) else {}
            return {
                "id": self._bounded_text(
                    kind,
                    "id",
                    source.get("id", ""),
                    self.MAX_ENTRY_ID_CHARS,
                ),
                "text": self._bounded_text(
                    kind,
                    "text",
                    source.get("text", ""),
                    self.MAX_TRANSCRIPT_CHARS,
                ),
            }
        if kind == "config":
            source = payload if isinstance(payload, dict) else {}
            compact: dict[str, Any] = {}
            for key in ScriptConfig.__dataclass_fields__:
                if key not in source:
                    continue
                value = source[key]
                if isinstance(value, str):
                    value = self._bounded_text(
                        kind,
                        key,
                        value,
                        self.MAX_CONFIG_VALUE_CHARS,
                    )
                compact[key] = value
            return compact
        # ``clear`` has no payload.  Dropping arbitrary data here also avoids
        # retaining a large request object for a no-op UI action.
        return None

    def _record_queue_overflow(self, kind: str) -> None:
        with self._event_count_lock:
            self._dropped_event_count += 1
            count = self._dropped_event_count
        if self._should_report_count(count):
            log_error(
                "event_queue_overflow",
                (
                    f"Dropped {kind!r} UI event because the {self.MAX_PENDING_EVENTS}-item "
                    f"MekiScript queue is full; dropped_total={count}."
                ),
            )

    def enqueue(self, kind: str, payload: Any) -> bool:
        if kind not in {"append", "translation", "config", "clear"}:
            log_error("event_queue_rejected", f"Ignoring unknown MekiScript event: {kind!r}")
            return False
        if is_debug_enabled():
            payload_detail = (
                sorted(payload) if isinstance(payload, dict) else type(payload).__name__
            )
            log_debug("enqueue", f"kind: {kind}\npayload_keys: {payload_detail}")
        event = (kind, self._compact_event_payload(kind, payload))
        try:
            self.events.put_nowait(event)
            self._wakeup.schedule()
            return True
        except queue.Full:
            self._record_queue_overflow(kind)
            return False

    def _at_bottom(self) -> bool:
        scrollbar = self.text.verticalScrollBar()
        return scrollbar.value() >= scrollbar.maximum() - 2

    def _follow_bottom(self, was_at_bottom: bool) -> None:
        if was_at_bottom:
            scrollbar = self.text.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    @staticmethod
    def _inline_lines(text: str) -> str:
        # Separate paragraphs identify entries; line separators keep multiline
        # source text within the same paragraph.
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\u2029", "\n").replace("\n", "\u2028")

    def _append(self, payload: dict[str, Any]) -> None:
        entry_id = str(payload.get("id", "")).strip()
        original = str(payload.get("text", "")).strip()
        if not entry_id or not original or entry_id in self.entry_ids:
            return
        self.entry_ids.add(entry_id)
        self.last_original = original
        at_bottom = self._at_bottom()
        cursor = QTextCursor(self.text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        original_block = QTextBlockFormat()
        if self._history:
            original_block.setTopMargin(10)
            cursor.insertBlock(original_block)
        else:
            cursor.setBlockFormat(original_block)
        cursor.insertText(self._inline_lines(original), self._line_format("original"))
        translation_block = QTextBlockFormat()
        translation_block.setTopMargin(4)
        cursor.insertBlock(translation_block)
        cursor.insertText(self._PENDING_TRANSLATION_TEXT, self._line_format("pending"))
        entry = _TranscriptHistoryEntry(
            entry_id, cursor.block(), True, len(original) + len(self._PENDING_TRANSLATION_TEXT) + 2
        )
        self._history.append(entry)
        self._history_by_id[entry_id] = entry
        self._history_chars += entry.text_length
        self._trim_history()
        cursor.endEditBlock()
        self._follow_bottom(at_bottom)

    def _translation(self, payload: dict[str, Any]) -> None:
        entry_id = str(payload.get("id", "")).strip()
        entry = self._history_by_id.get(entry_id)
        if entry is None or not entry.pending:
            return
        translated = str(payload.get("text", "")).strip() or "(번역 결과 없음)"
        self.last_translation = translated
        self.translation_count += 1
        at_bottom = self._at_bottom()
        block = entry.translation
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
        cursor.beginEditBlock()
        cursor.insertText(self._inline_lines(translated), self._line_format("translated"))
        entry.pending = False
        change = len(translated) - len(self._PENDING_TRANSLATION_TEXT)
        entry.text_length += change
        self._history_chars += change
        self._trim_history()
        cursor.endEditBlock()
        self._follow_bottom(at_bottom)

    def _trim_history(self) -> None:
        removed = False
        while self._history and (
            len(self._history) > self.MAX_HISTORY_ENTRIES
            or self._history_chars > self.MAX_HISTORY_CHARS
        ):
            entry = self._history.popleft()
            self._history_by_id.pop(entry.entry_id)
            self.entry_ids.discard(entry.entry_id)
            self._history_chars -= entry.text_length
            removed = True
        if not removed:
            return
        cursor = QTextCursor(self.text.document())
        if self._history:
            first_original = self._history[0].translation.previous()
            cursor.setPosition(first_original.position(), QTextCursor.MoveMode.KeepAnchor)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()

    def _clear_transcript(self) -> None:
        self.text.clear()
        self.entry_ids.clear()
        self._history.clear()
        self._history_by_id.clear()
        self._history_chars = 0

    def drain(self) -> None:
        for _ in range(self.MAX_EVENTS_PER_DRAIN):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "append":
                    self._append(payload)
                elif kind == "translation":
                    self._translation(payload)
                elif kind == "config":
                    self.config.update(payload)
                    self.apply_config()
                elif kind == "clear":
                    self._clear_transcript()
            except Exception as exc:
                log_error("ui_event", exc)
        if not self.events.empty():
            self._wakeup.schedule()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not messagebox.askyesno(
            "MekiScript 종료", "MekiScript 창을 닫을까요?", parent=self
        ):
            event.ignore()
            return
        if self._on_confirmed_close is not None:
            try:
                self._on_confirmed_close()
            except Exception as exc:
                log_error("manual_close_signal", exc)
        event.accept()



def make_handler(
    window: ScriptWindow,
    ui_heartbeat: UiHeartbeat | None = None,
):
    def health_payload() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "app": "MekiScript",
            "entries": len(window.entry_ids),
            "translationCount": window.translation_count,
            "lastOriginal": window.last_original,
            "lastTranslation": window.last_translation,
        }
        if ui_heartbeat is not None:
            payload.update(ui_heartbeat.health_payload())
        return payload

    class Handler(BaseHTTPRequestHandler):
        # Persistent responses avoid a Windows teardown race with local clients;
        # an idle socket still releases its handler thread after 30 seconds.
        protocol_version = "HTTP/1.1"
        timeout = 30.0

        def handle(self) -> None:
            try:
                super().handle()
            except (ConnectionError, TimeoutError):
                self.close_connection = True

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] == "/health":
                _write_json(self, 200, health_payload())
            else:
                _write_json(self, 404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            try:
                payload = _read_json(self)
                routes = {"/append": "append", "/translation": "translation", "/config": "config", "/clear": "clear"}
                kind = routes.get(self.path)
                if not kind:
                    _write_json(self, 404, {"ok": False, "error": "not found"})
                    return
                if not window.enqueue(kind, payload):
                    # Producers need to know when the UI queue rejects a line.
                    _write_json(
                        self,
                        503,
                        {
                            "ok": False,
                            "error": "MekiScript UI queue is busy; retry shortly",
                        },
                    )
                    return
                _write_json(self, 200, {"ok": True})
            except (ConnectionError, TimeoutError):
                self.close_connection = True
            except (ValueError, TypeError) as exc:
                _write_json(self, 400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                log_error("http_request", exc)
                _write_json(self, 500, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MekiScript")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--topmost", type=int, default=1)
    parser.add_argument("--bg-color", default="#111111")
    parser.add_argument("--opacity", type=float, default=0.90)
    parser.add_argument("--original-color", default="#f4f4f5")
    parser.add_argument("--original-size", type=int, default=20)
    parser.add_argument("--original-font", default="Yu Gothic UI")
    parser.add_argument("--translated-color", default="#7dd3fc")
    parser.add_argument("--translated-size", type=int, default=20)
    parser.add_argument("--translated-font", default="Malgun Gothic")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--watchdog-manual-close-file", help=argparse.SUPPRESS)
    parser.add_argument("--watchdog-manual-close-token", help=argparse.SUPPRESS)
    parser.add_argument("--debug-log", action="store_true")
    return parser.parse_args()


def run_transcript_self_test() -> None:
    application = ensure_application()
    window = ScriptWindow(ScriptConfig(topmost=False))
    try:
        originals = [f"원문 {index}" for index in range(1, 9)]
        translations = [f"번역 {index}" for index in range(1, 9)]
        for index, original in enumerate(originals, 1):
            window._append({"id": f"chunk-{index}", "text": original})
        for index in range(8, 0, -1):
            window._translation({"id": f"chunk-{index}", "text": translations[index - 1]})

        transcript = window.text.toPlainText()
        for original, translated in zip(originals, translations):
            if transcript.count(original) != 1 or transcript.count(translated) != 1:
                raise RuntimeError("MekiScript 누적 대본 자체 검증에 실패했습니다.")
            if transcript.index(original) > transcript.index(translated):
                raise RuntimeError("원문보다 번역문이 먼저 표시되었습니다.")
        if window.translation_count != len(translations):
            raise RuntimeError("번역 완료 개수가 누적되지 않았습니다.")
        if window.last_translation != translations[0]:
            raise RuntimeError("마지막 번역 상태가 올바르지 않습니다.")

        # Verify that a long-running live session retains only bounded
        # scrollback and that removing an entry also removes its late-result
        # references to the corresponding document blocks.
        window._clear_transcript()
        total_history_entries = window.MAX_HISTORY_ENTRIES + 8
        for index in range(total_history_entries):
            entry_id = f"history-{index}"
            window._append({"id": entry_id, "text": f"원문 history {index}"})
            window._translation({"id": entry_id, "text": f"번역 history {index}"})
        if len(window.entry_ids) != window.MAX_HISTORY_ENTRIES:
            raise RuntimeError("MekiScript 누적 대본 메모리 제한 검증에 실패했습니다.")
        transcript = window.text.toPlainText()
        if "원문 history 0" in transcript or f"원문 history {total_history_entries - 1}" not in transcript:
            raise RuntimeError("MekiScript 누적 대본 스크롤백 정리에 실패했습니다.")
    finally:
        window.deleteLater()
        application.processEvents()


def main() -> int:
    args = parse_args()
    configure_system_logging("MekiScript", args.debug_log)
    install_exception_hooks()
    capture_windowed_streams()
    if args.self_test:
        ScriptConfig(opacity=args.opacity)
        run_transcript_self_test()
        return 0
    config = ScriptConfig(
        topmost=bool(args.topmost), bg_color=args.bg_color, opacity=args.opacity,
        original_color=args.original_color, original_size=args.original_size, original_font=args.original_font,
        translated_color=args.translated_color, translated_size=args.translated_size, translated_font=args.translated_font,
        debug_log=args.debug_log,
    )
    set_windows_app_id("MekiScript")
    application = ensure_application()
    manual_close_confirmed = threading.Event()

    def on_confirmed_close() -> None:
        manual_close_confirmed.set()
        publish_manual_close_signal(
            args.watchdog_manual_close_file,
            args.watchdog_manual_close_token,
            app_name="MekiScript",
        )

    window = ScriptWindow(config, on_confirmed_close=on_confirmed_close)
    ui_heartbeat = UiHeartbeat()
    ui_heartbeat.schedule(window)
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        make_handler(window, ui_heartbeat),
    )
    server.daemon_threads = True
    server.block_on_close = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    window.show()
    application.exec()
    server.shutdown()
    server.server_close()
    return MANUAL_CLOSE_EXIT_CODE if manual_close_confirmed.is_set() else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log_error("main", exc)
        if "--self-test" not in sys.argv[1:]:
            try:
                messagebox.showerror(
                    "MekiScript",
                    f"예기치 않은 오류로 프로그램을 종료합니다.\n\n{exc}\n\n로그: {log_directory('error_log', 'MekiScript')}",
                )
            except Exception:
                pass
        raise SystemExit(1) from exc
