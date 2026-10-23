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

from runtime_paths import prepare_tk_environment
from overlay_receipts import DisplayReceipts

TK_RUNTIME_DIRNAME = "MekiCopyRuntime"
_DLL_DIR_HANDLES = []

if getattr(sys, "frozen", False):
    _frozen_resource_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    if os.name == "nt":
        path_items = os.environ.get("PATH", "").split(os.pathsep)
        if _frozen_resource_dir not in path_items:
            os.environ["PATH"] = os.pathsep.join([_frozen_resource_dir, *path_items])
        if hasattr(os, "add_dll_directory"):
            try:
                _DLL_DIR_HANDLES.append(os.add_dll_directory(_frozen_resource_dir))
            except OSError:
                pass
prepare_tk_environment(TK_RUNTIME_DIRNAME)
import tkinter as tk
from tkinter import messagebox

from app_identity import apply_tk_icon, set_windows_app_id
from companion_json import MAX_REQUEST_BYTES, read_json as _read_request_json, write_json as _write_json
from display_config import apply_display_config, normalize_font_name, validate_display_colors
from companion_liveness import UiHeartbeat
from companion_manual_close import MANUAL_CLOSE_EXIT_CODE, publish_manual_close_signal
from service_ports import OVERLAYER_DEFAULT_PORT
from system_logging import (
    capture_windowed_streams,
    configure_system_logging,
    install_exception_hooks,
    install_tk_exception_hook,
    log_directory,
    log_debug as system_debug,
    log_error as system_error,
    set_debug_enabled,
)
from win32_overlay import TopmostWindowController, get_top_level_hwnd

DEFAULT_PORT = OVERLAYER_DEFAULT_PORT
DEFAULT_GEOMETRY = "780x180+120+120"
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


class OverlayerApp:
    # Keep translation display responsive during a burst without burning a
    # 20 Hz Tk timer while the overlay is unchanged.
    ACTIVE_EVENT_POLL_MS = 50
    IDLE_EVENT_POLL_MS = 200
    # Text updates are coalesced into one latest-value slot.  Configuration
    # changes retain their ordering, but their queue is bounded so a stalled
    # Tk window cannot consume unbounded memory.
    MAX_PENDING_CONFIG_EVENTS = 32
    MAX_CONFIG_EVENTS_PER_DRAIN = 8
    MAX_TEXT_CHARS = 32 * 1024
    MAX_CONFIG_VALUE_CHARS = 4 * 1024

    def __init__(
        self,
        root: tk.Tk,
        config: OverlayConfig,
        *,
        on_confirmed_close: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
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
        self._drag_start: tuple[int, int] | None = None
        self._window_start: tuple[int, int] | None = None
        self._topmost_controller: TopmostWindowController | None = None
        self._applied_config: dict[str, Any] = {}

        self.root.title("MekiOverlayer")
        self.root.geometry(DEFAULT_GEOMETRY)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")

        self.label = tk.Label(
            self.root,
            text="",
            justify="center",
            anchor="center",
            padx=18,
            pady=14,
        )
        self.label.pack(fill=tk.BOTH, expand=True)
        self.root.bind("<Configure>", self._on_configure)
        self.root.bind("<ButtonPress-1>", self._on_drag_start)
        self.root.bind("<B1-Motion>", self._on_drag_move)
        self.label.bind("<ButtonPress-1>", self._on_drag_start)
        self.label.bind("<B1-Motion>", self._on_drag_move)
        self.apply_config()
        # Preserve the overlay's established drag interaction.  Its native
        # topmost updates still use SWP_NOACTIVATE; WS_EX_NOACTIVATE is reserved
        # for the detached OCR button, where keeping the game foreground is
        # more important than keyboard interaction with the overlay itself.
        self._topmost_controller = TopmostWindowController(
            self.root,
            enabled=self.config.topmost,
            no_activate=False,
            debug_log=self.config.debug_log,
        )
        self._topmost_controller.start()
        self.root.after(self.IDLE_EVENT_POLL_MS, self._drain_events)

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
        # frames.  One locked slot bounds memory and always lets Tk consume the
        # newest available text on its next tick.
        value = self._bounded_text("text", text, self.MAX_TEXT_CHARS)
        with self._text_lock:
            self.display_receipts.complete(
                self._pending_request_id, error="superseded by another display request"
            )
            self.last_text = value
            self._pending_text = value
            self._pending_request_id = request_id

    def cancel_display(self, request_id: str) -> None:
        with self._text_lock:
            self.display_receipts.cancel(request_id)
            if self._pending_request_id == request_id:
                self._pending_text = None
                self._pending_request_id = None

    def enqueue_config(self, data: dict[str, Any]) -> bool:
        try:
            self.events.put_nowait(("config", self._compact_config(data)))
            return True
        except queue.Full:
            self._record_config_overflow()
            return False

    def _take_pending_text(self) -> str | None:
        with self._text_lock:
            text = self._pending_text
            self._pending_text = None
            return text

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

        # Every translation may include the same config.  Remapping the native
        # window for those requests disturbs dragging and generates unnecessary
        # WinEvent callbacks; only a titlebar change requires withdrawing Tk.
        frame_changed = "hide_titlebar" in changed
        geometry = self.root.geometry() if previous and frame_changed else None
        if frame_changed:
            self.root.withdraw()
            self.root.overrideredirect(cfg.hide_titlebar)
            if geometry is not None:
                self.root.geometry(geometry)
        if "bg_color" in changed:
            self.root.configure(bg=cfg.bg_color)
        if "opacity" in changed:
            self.root.attributes("-alpha", cfg.opacity)
        if "topmost" in changed:
            self.root.attributes("-topmost", cfg.topmost)
        if "fixed_size" in changed:
            self.root.resizable(not cfg.fixed_size, not cfg.fixed_size)
            if cfg.fixed_size:
                self.root.update_idletasks()
                width = max(240, self.root.winfo_width())
                height = max(80, self.root.winfo_height())
                self.root.minsize(width, height)
                self.root.maxsize(width, height)
            else:
                self.root.minsize(240, 80)
                self.root.maxsize(10000, 10000)
        if changed & {"bg_color", "text_color", "text_font", "text_size"}:
            self.label.configure(
                bg=cfg.bg_color,
                fg=cfg.text_color,
                font=(normalize_font_name(cfg.text_font), cfg.text_size, "bold"),
            )
            self._update_wraplength()
        if frame_changed:
            self.root.deiconify()
        self.root.update_idletasks()
        if frame_changed or "exclude_from_capture" in changed:
            self._apply_capture_exclusion()
        if self._topmost_controller is not None:
            if "debug_log" in changed:
                self._topmost_controller.set_debug_log(cfg.debug_log)
            if "topmost" in changed:
                self._topmost_controller.set_enabled(cfg.topmost)
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
            top_level_hwnd = get_top_level_hwnd(self.root)
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

    def _on_configure(self, event: tk.Event) -> None:
        if event.widget == self.root:
            self._update_wraplength()

    def _on_root_destroy(self, event: tk.Event) -> None:
        if event.widget == self.root and self._topmost_controller is not None:
            self._topmost_controller.close()

    def _update_wraplength(self) -> None:
        width = max(80, self.root.winfo_width() - 36)
        self.label.configure(wraplength=width)

    def _on_drag_start(self, event: tk.Event) -> None:
        if not self.config.hide_titlebar:
            return
        self._drag_start = (event.x_root, event.y_root)
        self._window_start = (self.root.winfo_x(), self.root.winfo_y())

    def _on_drag_move(self, event: tk.Event) -> None:
        if not self._drag_start or not self._window_start:
            return
        dx = event.x_root - self._drag_start[0]
        dy = event.y_root - self._drag_start[1]
        self.root.geometry(f"+{self._window_start[0] + dx}+{self._window_start[1] + dy}")

    def close(self) -> None:
        if not messagebox.askyesno(
            "MekiOverlayer 종료",
            "MekiOverlayer 창을 닫을까요?",
            parent=self.root,
        ):
            return
        if self._on_confirmed_close is not None:
            try:
                self._on_confirmed_close()
            except Exception as exc:
                log_error("manual_close_signal", exc)
        if self._topmost_controller is not None:
            self._topmost_controller.close()
        self.root.destroy()

    def _drain_events(self) -> None:
        handled_event = False
        for _ in range(self.MAX_CONFIG_EVENTS_PER_DRAIN):
            try:
                event_type, payload = self.events.get_nowait()
            except queue.Empty:
                break
            handled_event = True
            try:
                if event_type == "config":
                    validate_display_colors(self.root, payload)
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
            handled_event = True
            try:
                # Keep cancellation and UI application in one critical section.
                # A receipt is acknowledged only after Tk has flushed redraws.
                with self._text_lock:
                    if not self.display_receipts.cancelled(request_id):
                        self.label.configure(text=text)
                        self.root.deiconify()
                        self.root.lift()
                        self.root.update_idletasks()
                        self.display_receipts.complete(request_id)
            except Exception as exc:
                self.display_receipts.complete(request_id, error=str(exc))
                log_error("display_text", exc)
        self.root.after(
            (
                self.ACTIVE_EVENT_POLL_MS
                if handled_event or not self.events.empty() or self._has_pending_text()
                else self.IDLE_EVENT_POLL_MS
            ),
            self._drain_events,
        )


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
                        # Settings must not look persisted when Tk has fallen
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
        root = tk.Tk()
        install_tk_exception_hook(root)
        apply_tk_icon(root)
        manual_close_confirmed = threading.Event()

        def on_confirmed_close() -> None:
            manual_close_confirmed.set()
            publish_manual_close_signal(
                args.watchdog_manual_close_file,
                args.watchdog_manual_close_token,
                app_name="MekiOverlayer",
            )

        app_ref = OverlayerApp(root, config, on_confirmed_close=on_confirmed_close)
        ui_heartbeat = UiHeartbeat()
        ui_heartbeat.schedule(root)
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
        root.mainloop()
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
