from __future__ import annotations

import argparse
import ctypes
import os
import queue
import socket
import sys
import threading
import traceback
from dataclasses import asdict, dataclass
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QCloseEvent, QFont, QKeySequence, QMouseEvent, QPalette, QShortcut
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from app_identity import apply_qt_icon, set_windows_app_id
from mekicopy_theme import TimerHost, ensure_application, messagebox
from overlay_receipts import DisplayReceipts
from qt_companion import UiWakeup
from companion_json import MAX_REQUEST_BYTES, read_json as _read_request_json, write_json as _write_json
from display_config import apply_display_config, normalize_font_name
from companion_liveness import UiHeartbeat
from companion_manual_close import MANUAL_CLOSE_EXIT_CODE, publish_manual_close_signal
from service_ports import OVERLAYER_DEFAULT_PORT
from system_logging import (
    capture_windowed_streams,
    configure_system_logging,
    install_exception_hooks,
    log_directory,
    log_debug as system_debug,
    log_error as system_error,
    set_debug_enabled,
)
from win32_overlay import TopmostWindowController, get_top_level_hwnd

DEFAULT_PORT = OVERLAYER_DEFAULT_PORT
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011




def log_error(stage: str, exc: BaseException | str) -> None:
    system_error(stage, exc, component="MekiOverlayer")


def log_debug(enabled: bool, stage: str, message: str) -> None:
    system_debug(stage, message, component="MekiOverlayer", enabled=enabled)


@dataclass
class OverlayConfig:
    topmost: bool = True
    hide_titlebar: bool = False
    fixed_size: bool = False
    exclude_from_capture: bool = False
    bg_color: str = "#111111"
    opacity: float = 0.78
    text_color: str = "#ffffff"
    text_size: int = 28
    text_font: str = "Malgun Gothic"
    debug_log: bool = False

    def __post_init__(self) -> None:
        self.text_font = normalize_font_name(self.text_font)

    def update_from_dict(self, data: dict[str, Any]) -> None:
        apply_display_config(self, data)
        set_debug_enabled(self.debug_log)


class OverlayerApp(QWidget, TimerHost):
    # Text updates are coalesced into one latest-value slot.  Configuration
    # changes retain their ordering, but their queue is bounded so a stalled
    # window cannot consume unbounded memory.
    MAX_PENDING_CONFIG_EVENTS = 32
    MAX_CONFIG_EVENTS_PER_DRAIN = 8
    MAX_TEXT_CHARS = 32 * 1024
    MAX_CONFIG_VALUE_CHARS = 4 * 1024

    def __init__(
        self,
        config: OverlayConfig,
        *,
        on_confirmed_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._wakeup = UiWakeup(self, self._drain_events)
        self.config = config
        self._on_confirmed_close = on_confirmed_close
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue(
            maxsize=self.MAX_PENDING_CONFIG_EVENTS
        )
        self._text_lock = threading.Lock()
        self._pending_text: str | None = None
        self._pending_request_id: str | None = None
        self.display_receipts = DisplayReceipts()
        self._event_count_lock = threading.Lock()
        self._dropped_config_count = 0
        self._trimmed_payload_count = 0
        self.last_text = ""
        self._drag_start: QPoint | None = None
        self._window_start: QPoint | None = None
        self._topmost_controller: TopmostWindowController | None = None
        self._applied_config: dict[str, Any] = {}

        self.setWindowTitle("MekiOverlayer")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.resize(720, 152)
        self.move(120, 120)
        apply_qt_icon(self)
        self.label = QLabel()
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.addWidget(self.label)
        shortcut = QShortcut(QKeySequence("Escape"), self)
        shortcut.activated.connect(self.close)
        self.apply_config()
        self._topmost_controller = TopmostWindowController(
            self,
            enabled=self.config.topmost,
            no_activate=False,
            debug_log=self.config.debug_log,
        )
        self._topmost_controller.start()

    @staticmethod
    def _should_report_count(count: int) -> bool:
        """Log the first overload and then only exponentially spaced repeats."""
        return count == 1 or (count > 0 and count & (count - 1) == 0)

    def _record_payload_trim(self, field: str, original_length: int) -> None:
        with self._event_count_lock:
            self._trimmed_payload_count += 1
            count = self._trimmed_payload_count
        if self._should_report_count(count):
            log_error(
                "event_payload_trimmed",
                (
                    f"Trimmed oversized MekiOverlayer {field} payload (chars={original_length}); "
                    f"trimmed_total={count}."
                ),
            )

    def _bounded_text(self, field: str, value: Any, limit: int) -> str:
        text = str(value)
        if len(text) <= limit:
            return text
        self._record_payload_trim(field, len(text))
        return text[:limit]

    def _compact_config(self, data: dict[str, Any]) -> dict[str, Any]:
        """Retain only configuration fields that ``OverlayConfig`` consumes."""
        source = data if isinstance(data, dict) else {}
        compact: dict[str, Any] = {}
        for key in OverlayConfig.__dataclass_fields__:
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, str):
                value = self._bounded_text(
                    key,
                    value,
                    self.MAX_CONFIG_VALUE_CHARS,
                )
            compact[key] = value
        return compact

    def _record_config_overflow(self) -> None:
        with self._event_count_lock:
            self._dropped_config_count += 1
            count = self._dropped_config_count
        if self._should_report_count(count):
            log_error(
                "event_queue_overflow",
                (
                    "Dropped a MekiOverlayer config event because the "
                    f"{self.MAX_PENDING_CONFIG_EVENTS}-item queue is full; "
                    f"dropped_total={count}."
                ),
            )

    def enqueue_text(self, text: str, request_id: str | None = None) -> None:
        # Showing an outdated translation is worse than skipping intermediate
        # frames. One locked slot lets the GUI consume the newest text.
        value = self._bounded_text("text", text, self.MAX_TEXT_CHARS)
        with self._text_lock:
            self.display_receipts.complete(
                self._pending_request_id, error="superseded by another display request"
            )
            self.last_text = value
            self._pending_text = value
            self._pending_request_id = request_id
        self._wakeup.schedule()

    def cancel_display(self, request_id: str) -> None:
        with self._text_lock:
            self.display_receipts.cancel(request_id)
            if self._pending_request_id == request_id:
                self._pending_text = None
                self._pending_request_id = None

    def enqueue_config(self, data: dict[str, Any]) -> bool:
        try:
            self.events.put_nowait(("config", self._compact_config(data)))
            self._wakeup.schedule()
            return True
        except queue.Full:
            self._record_config_overflow()
            return False

    def _has_pending_text(self) -> bool:
        with self._text_lock:
            return self._pending_text is not None

    def apply_config(self) -> None:
        cfg = self.config
        current = asdict(cfg)
        previous = self._applied_config
        changed = {key for key, value in current.items() if previous.get(key) != value}
        if not changed:
            return

        frame_changed = bool(changed & {"hide_titlebar", "topmost"})
        if frame_changed:
            geometry = self.geometry()
            visible = self.isVisible()
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, cfg.hide_titlebar)
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, cfg.topmost)
            self.setGeometry(geometry)
            if visible:
                self.show()
        if "opacity" in changed:
            self.setWindowOpacity(cfg.opacity)
        if "fixed_size" in changed:
            if cfg.fixed_size:
                self.setFixedSize(self.size().expandedTo(self.minimumSizeHint()))
            else:
                self.setMinimumSize(220, 68)
                self.setMaximumSize(16777215, 16777215)
        if changed & {"bg_color", "text_color"}:
            palette = self.palette()
            palette.setColor(QPalette.ColorRole.Window, QColor(cfg.bg_color))
            self.setPalette(palette)
            self.setAutoFillBackground(True)
            self.label.setStyleSheet(f"color: {QColor(cfg.text_color).name()};")
        if changed & {"text_font", "text_size"}:
            self.label.setFont(QFont(normalize_font_name(cfg.text_font), cfg.text_size, QFont.Weight.Bold))
        if frame_changed or "exclude_from_capture" in changed:
            self._apply_capture_exclusion()
        if self._topmost_controller is not None:
            if "debug_log" in changed:
                self._topmost_controller.set_debug_log(cfg.debug_log)
            if "topmost" in changed:
                self._topmost_controller.set_enabled(cfg.topmost)
            elif frame_changed:
                self._topmost_controller.refresh()
        self._applied_config = current

    def _apply_capture_exclusion(self) -> bool:
        if os.name != "nt":
            return False
        affinity = WDA_EXCLUDEFROMCAPTURE if self.config.exclude_from_capture else WDA_NONE
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            set_window_display_affinity = user32.SetWindowDisplayAffinity
            set_window_display_affinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            set_window_display_affinity.restype = wintypes.BOOL
            top_level_hwnd = get_top_level_hwnd(self)
            if not top_level_hwnd:
                return False
            if set_window_display_affinity(top_level_hwnd, affinity):
                log_debug(
                    self.config.debug_log,
                    "capture_exclusion",
                    f"SetWindowDisplayAffinity=0x{affinity:08X}",
                )
                return True
            error_code = ctypes.get_last_error()
            raise ctypes.WinError(error_code)
        except Exception as exc:
            log_error("set_window_display_affinity", exc)
            return False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.config.hide_titlebar and event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.globalPosition().toPoint()
            self._window_start = self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(self._window_start + event.globalPosition().toPoint() - self._drag_start)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_start = None
        self._window_start = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not messagebox.askyesno(
            "MekiOverlayer 종료", "MekiOverlayer 창을 닫을까요?", parent=self
        ):
            event.ignore()
            return
        if self._on_confirmed_close is not None:
            try:
                self._on_confirmed_close()
            except Exception as exc:
                log_error("manual_close_signal", exc)
        if self._topmost_controller is not None:
            self._topmost_controller.close()
        event.accept()

    def _drain_events(self) -> None:
        for _ in range(self.MAX_CONFIG_EVENTS_PER_DRAIN):
            try:
                event_type, payload = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                if event_type == "config":
                    self.config.update_from_dict(payload)
                    self.apply_config()
            except Exception as exc:
                log_error("ui_event", exc)
        with self._text_lock:
            text = self._pending_text
            request_id = self._pending_request_id
            self._pending_text = None
            self._pending_request_id = None
        if self.display_receipts.cancelled(request_id):
            text = None
        if text is not None:
            try:
                # Keep cancellation and UI application in one critical section.
                # A receipt is acknowledged only after the widget has repainted.
                with self._text_lock:
                    if not self.display_receipts.cancelled(request_id):
                        self.label.setText(text)
                        if self.isMinimized():
                            self.showNormal()
                        elif not self.isVisible():
                            self.show()
                        self.raise_()
                        self.label.repaint()
                        self.display_receipts.complete(request_id)
            except Exception as exc:
                self.display_receipts.complete(request_id, error=str(exc))
                log_error("display_text", exc)
        if not self.events.empty() or self._has_pending_text():
            self._wakeup.schedule()



def make_handler(
    app_ref: OverlayerApp,
    ui_heartbeat: UiHeartbeat | None = None,
):
    def health_payload() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "app": "MekiOverlayer",
            "text": getattr(app_ref, "last_text", ""),
        }
        if ui_heartbeat is not None:
            payload.update(ui_heartbeat.health_payload())
        return payload

    class OverlayerHandler(BaseHTTPRequestHandler):
        # Keep responses open for the shared companion transport. Closing each
        # response can race Windows socket teardown and reset a valid reply.
        protocol_version = "HTTP/1.1"
        timeout = 30.0

        def handle(self) -> None:
            try:
                super().handle()
            except (ConnectionError, TimeoutError):
                # An idle pooled connection or a client closing its window is
                # normal; it must not become a server-thread traceback.
                self.close_connection = True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                _write_json(self, 200, health_payload())
                return
            if parsed.path == "/show":
                query = parse_qs(parsed.query)
                text = query.get("text", [""])[0]
                app_ref.enqueue_text(text)
                _write_json(self, 200, {"ok": True})
                return
            _write_json(self, 404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                payload = _read_request_json(self)
                if parsed.path == "/cancel-display":
                    app_ref.cancel_display(str(payload.get("requestId", "")))
                    _write_json(self, 200, {"ok": True})
                    return
                if parsed.path == "/show":
                    wait_for_display = payload.get("waitForDisplay") is True
                    if wait_for_display:
                        request_id = str(payload.get("requestId", ""))
                        receipt = app_ref.display_receipts.create(request_id)
                        app_ref.enqueue_text(str(payload.get("text", "")), request_id)
                        if not receipt.event.wait(timeout=5.0):
                            app_ref.cancel_display(request_id)
                            _write_json(self, 504, {"ok": False, "displayed": False, "error": "overlay UI display timed out"})
                            return
                        _write_json(self, 200 if receipt.displayed else 409, {
                            "ok": receipt.displayed, "displayed": receipt.displayed,
                            "requestId": request_id, "error": receipt.error,
                            "displayedMonotonic": receipt.completed_at,
                        })
                        return
                    app_ref.enqueue_text(str(payload.get("text", "")))
                    log_debug(app_ref.config.debug_log, "show", f"chars: {len(str(payload.get('text', '')))}")
                    _write_json(self, 200, {"ok": True})
                    return
                if parsed.path == "/config":
                    if not app_ref.enqueue_config(payload):
                        # Settings must not look persisted when the GUI has fallen
                        # behind its bounded queue. Native callers can retry
                        # this transient condition just like text delivery.
                        _write_json(
                            self,
                            503,
                            {
                                "ok": False,
                                "error": "MekiOverlayer UI queue is busy; retry shortly",
                            },
                        )
                        return
                    _write_json(self, 200, {"ok": True})
                    return
                _write_json(self, 404, {"ok": False, "error": "not found"})
            except (ConnectionError, TimeoutError):
                self.close_connection = True
            except (ValueError, TypeError) as exc:
                _write_json(self, 400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                log_error("http_post", exc)
                _write_json(self, 500, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            log_debug(app_ref.config.debug_log, "http", format % args)

    return OverlayerHandler


def run_server(server: ThreadingHTTPServer, app_ref: OverlayerApp, port: int) -> None:
    log_debug(app_ref.config.debug_log, "server", f"listening on 127.0.0.1:{port}")
    server.serve_forever()


def ensure_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"MekiOverlayer를 시작할 수 없습니다. 127.0.0.1:{port} 포트가 이미 사용 중입니다."
            ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MekiOverlayer")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--topmost", type=int, default=1)
    parser.add_argument("--hide-titlebar", type=int, default=0)
    parser.add_argument("--fixed-size", type=int, default=0)
    parser.add_argument("--exclude-from-capture", type=int, default=0)
    parser.add_argument("--bg-color", default="#111111")
    parser.add_argument("--opacity", type=float, default=0.78)
    parser.add_argument("--text-color", default="#ffffff")
    parser.add_argument("--text-size", type=int, default=28)
    parser.add_argument("--text-font", default="Malgun Gothic")
    parser.add_argument("--watchdog-manual-close-file", help=argparse.SUPPRESS)
    parser.add_argument("--watchdog-manual-close-token", help=argparse.SUPPRESS)
    parser.add_argument("--debug-log", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_system_logging("MekiOverlayer", args.debug_log)
    set_debug_enabled(args.debug_log)
    install_exception_hooks()
    capture_windowed_streams()
    set_windows_app_id("MekiOverlayer")
    config = OverlayConfig(
        topmost=bool(args.topmost),
        hide_titlebar=bool(args.hide_titlebar),
        fixed_size=bool(args.fixed_size),
        exclude_from_capture=bool(args.exclude_from_capture),
        bg_color=args.bg_color,
        opacity=args.opacity,
        text_color=args.text_color,
        text_size=args.text_size,
        text_font=args.text_font,
        debug_log=args.debug_log,
    )
    server: ThreadingHTTPServer | None = None
    server_started = False
    try:
        application = ensure_application()
        manual_close_confirmed = threading.Event()

        def on_confirmed_close() -> None:
            manual_close_confirmed.set()
            publish_manual_close_signal(
                args.watchdog_manual_close_file,
                args.watchdog_manual_close_token,
                app_name="MekiOverlayer",
            )

        app_ref = OverlayerApp(config, on_confirmed_close=on_confirmed_close)
        ui_heartbeat = UiHeartbeat()
        ui_heartbeat.schedule(app_ref)
        try:
            server = ThreadingHTTPServer(
                ("127.0.0.1", args.port),
                make_handler(app_ref, ui_heartbeat),
            )
        except OSError as exc:
            raise RuntimeError(
                f"MekiOverlayer를 시작할 수 없습니다. 127.0.0.1:{args.port} 포트가 이미 사용 중입니다."
            ) from exc
        server.daemon_threads = True
        server.block_on_close = False
        thread = threading.Thread(
            target=run_server,
            args=(server, app_ref, args.port),
            daemon=True,
        )
        thread.start()
        server_started = True
        app_ref.show()
        application.exec()
        return MANUAL_CLOSE_EXIT_CODE if manual_close_confirmed.is_set() else 0
    except Exception as exc:
        log_error("main", exc)
        if "--self-test" not in sys.argv[1:]:
            try:
                messagebox.showerror(
                    "MekiOverlayer",
                    f"예기치 않은 오류로 프로그램을 종료합니다.\n\n{exc}\n\n로그: {log_directory('error_log', 'MekiOverlayer')}",
                )
            except Exception:
                pass
        return 1
    finally:
        if server is not None:
            if server_started:
                server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
