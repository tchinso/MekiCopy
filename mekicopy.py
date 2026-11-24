import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import time

from dataclasses import replace
from typing import Callable

from PySide6.QtCore import QEvent, QPoint, QRect, QTimer, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from PIL import Image
from mekicopy_capture import (
    MIN_SIZE_PX,
    MonitorInfo,
    Region,
    RegionRatio,
    clamp_region_to_monitor as _clamp_region_to_monitor,
    enable_dpi_awareness as _enable_dpi_awareness,
    find_monitor_for_region as _find_monitor_for_region,
    get_capture_manager,
    match_monitor_by_previous_rect as _match_monitor_by_previous_rect,
    monitor_signature as _monitor_signature,
    ratio_for_region as _ratio_for_region,
    region_from_ratio as _region_from_ratio,
    regions_equal as _regions_equal,
    run_capture_diagnostics,
)
from mekicopy_companions import (
    DETACHED_WINDOW_TITLE,
    overlay_config_payload,
    _WindowsNamedMutex,
    _activate_detached_window,
    _close_detached_window,
    _describe_translation_status,
    _find_companion_executable,
    _find_magpie_executable,
    _install_latest_magpie,
    _is_loopback_port_open,
    _is_process_alive,
    _json_request,
    _launch_detached_button_process,
    _probe_service,
    _startupinfo_for_background,
    _terminate_process_tree,
    _validate_service_health,
    request_translation_and_show,
)
from companion_watchdog import (
    CompanionWatchdog,
    RecoveryRequest,
    default_health_evaluator,
)
from companion_manual_close import (
    MANUAL_CLOSE_EXIT_CODE,
    ManualCloseSignal,
    consume_manual_close_signal,
    create_manual_close_signal,
    discard_manual_close_signal,
    manual_close_signal_arguments,
)
from mekicopy_ocr import (
    OcrError,
    _log_runtime_error,
    _log_runtime_message,
    capture_ocr_image,
    ocr_and_translate,
    recognize_image,
    take_ocr_progress,
)
from mekicopy_hotkey import GlobalHotkeyManager
from mekicopy_region_ui import (
    pick_bookmark,
    run_picker_and_capture,
    run_region_view,
    run_selection,
)
from mekicopy_runtime import (
    _get_app_dir,
    _set_app_user_model_id,
    _set_window_icon,
)
from mekicopy_settings import (
    DETACHED_DEFAULT_GEOMETRY,
    MAX_DETACHED_DIMENSION,
    AppSettings,
    Bookmark,
    load_bookmarks,
    load_detached_geometry,
    load_detached_region,
    load_settings,
    parse_detached_geometry,
    save_bookmarks,
    save_detached_geometry,
    save_detached_region,
    save_settings,
)
from mekicopy_settings_window import SettingsWindow
from meki_subtitle_window import MekiSubtitleWindow
from hytrans.model_files import PLAMO3_PROFILE, get_model_profile
from hytrans.api_settings import (
    ApiSettings,
    BACKEND_LABELS,
    PROVIDERS,
    api_settings_path,
    fingerprint,
    load_api_settings,
    normalize_backend,
    save_api_settings,
    validate_provider_settings,
)
from mekicopy_theme import (
    RoundedButton,
    TimerHost,
    configure_window_theme,
    ensure_application,
    make_font,
    messagebox,
    simpledialog,
)
from mekicopy_qt_tasks import QtTaskRunner
from mekicopy_realtime import (
    REALTIME_LOCK_LABEL,
    REALTIME_STATE_FILENAME,
    RealtimeOcrController,
    RealtimeUnavailableError,
    detached_translation_locked,
    perform_cycle,
)
from mekicopy_tray import WindowsTrayIcon
from system_logging import (
    configure_system_logging,
    install_exception_hooks,
    log_directory,
    set_debug_enabled,
)
from win32_overlay import TopmostWindowController
import mekicopy_settings

MAIN_WINDOW_WIDTH = 534
MAIN_WINDOW_HEIGHT = 338
MAIN_WINDOW_MIN_WIDTH = 456
MAIN_WINDOW_MIN_HEIGHT = 330
MAIN_TAB_BAR_WIDTH = 116
OCR_BUTTON_HEIGHT_PX = 148
GLOBAL_HOTKEY_POLL_MS = 50
TRANSLATION_STATUS_POLL_MS = 1_500
DETACHED_MUTEX_NAME = "Local\\MekiCopy.DetachedOcrButton"
MAGPIE_LAUNCH_NOTICE = (
    "데스크톱 캡쳐 모드는 Graphics Capture(1순위 권장) 또는 "
    "GDI(2순위 권장)을 사용해주세요"
)
_COMPANION_PROCESS_ATTRIBUTES = {
    "MekiAudioCapture": "audio_capture_process",
    "MekiScript": "script_process",
    "MekiOverlayer": "overlayer_process",
    "HYTrans": "hytrans_process",
}
_MANUALLY_CLOSABLE_COMPANIONS = frozenset(
    _COMPANION_PROCESS_ATTRIBUTES
) - {"HYTrans"}


class DetachedWindow(TimerHost, QWidget):
    def __init__(self, owner: "DetachedOcrButtonApp") -> None:
        super().__init__()
        self.owner = owner

    def closeEvent(self, event) -> None:
        self.owner._shutdown()
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.owner._schedule_geometry_save()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self.owner._schedule_geometry_save()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.owner.button and self.owner.settings.detached_hide_titlebar:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self.owner._on_mouse_down(event)
            elif event.type() == QEvent.Type.MouseMove and event.buttons() & Qt.MouseButton.LeftButton:
                self.owner._on_mouse_drag(event)
            elif event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self.owner._on_mouse_release(event)
                if self.owner._suppress_next_click:
                    self.owner._suppress_next_click = False
                    watched.setDown(False)
                    return True
        return super().eventFilter(watched, event)


def _read_window_geometry(value: str, fallback: str) -> QRect:
    width, height, x, y = (
        parse_detached_geometry(value) or parse_detached_geometry(fallback) or (260, 160, 120, 120)
    )
    return QRect(x, y, width, height)


class DetachedOcrButtonApp:
    MIN_WIDTH = 120
    MIN_HEIGHT = 80
    RESIZE_MARGIN = 10
    # Read settings only when the file metadata changes.
    SETTINGS_POLL_MS = 2_000

    def __init__(self) -> None:
        _enable_dpi_awareness()
        _set_app_user_model_id()
        ensure_application()
        self.root = DetachedWindow(self)
        configure_window_theme(self.root)
        self._task_runner = QtTaskRunner(self.root)
        self.settings = load_settings()
        self.root.setWindowTitle(DETACHED_WINDOW_TITLE)
        _set_window_icon(self.root)
        self._drag_action: str | None = None
        self._drag_start: tuple[int, int] | None = None
        self._drag_geometry: tuple[int, int, int, int] | None = None
        self._suppress_next_click = False
        self._settings_signature = self._settings_key(self.settings)
        self._settings_file_state = self._current_settings_file_state()
        self._settings_after_id: QTimer | None = None
        self._geometry_after_id: QTimer | None = None
        self._realtime_locked = False
        self._closing = False
        self._topmost_controller = TopmostWindowController(
            self.root,
            enabled=self.settings.detached_always_on_top,
            no_activate=True,
            debug_log=self.settings.debug_logging,
        )

        self.button = RoundedButton(
            self.root,
            text=self.ocr_action_label(),
            command=self._on_button_command,
            font=("Malgun Gothic", 13, "bold"),
            variant="primary",
            radius=16,
            focus_on_click=False,
        )
        layout = QVBoxLayout(self.root)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.addWidget(self.button)
        self.button.installEventFilter(self.root)

        geometry = _read_window_geometry(
            load_detached_geometry(self.settings.detached_geometry or DETACHED_DEFAULT_GEOMETRY),
            DETACHED_DEFAULT_GEOMETRY,
        )
        screens = ensure_application().screens()
        if screens and not any(screen.availableGeometry().intersects(geometry) for screen in screens):
            geometry.moveCenter(screens[0].availableGeometry().center())
        self.root.setGeometry(geometry)
        self.apply_settings()
        self._topmost_controller.start()
        self._schedule_settings_poll()

    def run(self) -> None:
        ensure_application().exec()

    def ocr_action_label(self) -> str:
        if self.settings.realtime_ocr_enabled:
            return "실시간 번역 모드 사용 중"
        if getattr(self, "_realtime_locked", False):
            return REALTIME_LOCK_LABEL
        return "번역 후 표시"

    def _refresh_realtime_lock(self) -> None:
        self._realtime_locked = detached_translation_locked(
            mekicopy_settings.STATE_DIR / REALTIME_STATE_FILENAME
        )
        runner = getattr(self, "_task_runner", None)
        busy = runner is not None and runner.is_running("detached_ocr")
        progress = getattr(self, "_ocr_progress_text", "") if busy else ""
        if self.button.property("variant") != "success":
            self.button.setText(progress or self.ocr_action_label())
        self.button.setEnabled(not (busy or self._realtime_locked or self.settings.realtime_ocr_enabled))

    def current_size(self) -> tuple[int, int]:
        return max(self.MIN_WIDTH, self.root.width()), max(self.MIN_HEIGHT, self.root.height())

    def capture_geometry(self, persist: bool = True) -> str:
        rect = self.root.geometry()
        geometry = f"{rect.width()}x{rect.height()}{rect.x():+}{rect.y():+}"
        self.settings.detached_geometry = geometry
        if self.settings.detached_fixed_size:
            self.settings.detached_fixed_width, self.settings.detached_fixed_height = self.current_size()
        if persist:
            save_detached_geometry(geometry)
        return geometry

    def apply_settings(self) -> None:
        settings = self.settings
        self._refresh_realtime_lock()
        previous_geometry = self.root.geometry()
        flags = Qt.WindowType.Window | Qt.WindowType.WindowCloseButtonHint
        if settings.detached_hide_titlebar:
            flags |= Qt.WindowType.FramelessWindowHint
        elif not settings.detached_fixed_size:
            flags |= Qt.WindowType.WindowMinMaxButtonsHint
        if settings.detached_always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        if self.root.windowFlags() != flags:
            self.root.setWindowFlags(flags)
            self.root.setGeometry(previous_geometry)
        if settings.detached_fixed_size:
            self.root.setFixedSize(
                max(self.MIN_WIDTH, min(MAX_DETACHED_DIMENSION, settings.detached_fixed_width)),
                max(self.MIN_HEIGHT, min(MAX_DETACHED_DIMENSION, settings.detached_fixed_height)),
            )
        else:
            self.root.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
            self.root.setMaximumSize(16777215, 16777215)
        self.root.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.root.show()
        self._topmost_controller.set_debug_log(settings.debug_logging)
        self._topmost_controller.set_enabled(settings.detached_always_on_top)

    def close(self) -> None:
        if not self._closing:
            self.root.close()

    def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._task_runner.close()
        self._topmost_controller.close()
        for timer in (self._settings_after_id, self._geometry_after_id):
            if timer is not None:
                self.root.after_cancel(timer)
        try:
            self.capture_geometry(persist=True)
        except Exception as exc:
            _log_runtime_error("save_detached_geometry_on_close", exc)

    def _on_button_command(self) -> None:
        if self._suppress_next_click:
            self._suppress_next_click = False
            return
        self.settings = load_settings()
        self._refresh_realtime_lock()
        if self._realtime_locked or self.settings.realtime_ocr_enabled:
            return
        region = load_detached_region()
        if not region:
            messagebox.showerror(
                "MekiCopy",
                "설정된 영역이 없습니다. MekiCopy에서 영역을 확정해주세요.",
                parent=self.root,
            )
            return
        try:
            region, _, _, _ = get_capture_manager().resolve_region(region)
            save_detached_region(region)
        except Exception as exc:
            _log_runtime_error("detached_prepare_region", exc)
            messagebox.showerror(
                "MekiCopy",
                f"캡처 영역 확인 실패:\n{exc}",
                parent=self.root,
            )
            return

        try:
            image = capture_ocr_image(
                region.left,
                region.top,
                region.width,
                region.height,
            )
        except OcrError as exc:
            messagebox.showwarning("MekiCopy", str(exc), parent=self.root)
            return

        self._start_ocr_task(image)

    def _start_ocr_task(self, image: Image.Image) -> None:
        self._ocr_progress_text = ""
        settings = self.settings
        simple_feedback = settings.simple_copy_complete
        hytrans_url = f"http://127.0.0.1:{settings.hytrans_port}"
        overlayer_url = f"http://127.0.0.1:{settings.overlayer_port}"
        overlayer_config = overlay_config_payload(settings, include_debug_log=False)

        def operation() -> str:
            text = recognize_image(image)
            if text.strip():
                # A realtime session may have started while this manual OCR ran.
                if detached_translation_locked(mekicopy_settings.STATE_DIR / REALTIME_STATE_FILENAME):
                    return ""
                request_translation_and_show(
                    text,
                    hytrans_url=hytrans_url,
                    overlayer_url=overlayer_url,
                    overlayer_config=overlayer_config,
                )
            return text

        def on_success(text: str) -> None:
            self._ocr_progress_text = ""
            self._refresh_realtime_lock()
            if not text.strip():
                messagebox.showwarning("MekiCopy", "인식된 텍스트가 없습니다.", parent=self.root)
                return
            if simple_feedback:
                self._show_feedback()
            else:
                messagebox.showinfo(
                    "MekiCopy", "번역 결과를 MekiOverlayer에 표시했습니다.", parent=self.root,
                )

        def on_error(exc: Exception) -> None:
            self._ocr_progress_text = ""
            self._refresh_realtime_lock()
            _log_runtime_error("detached_translate", exc)
            detail = str(exc)
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    detail = f"HTTP {exc.code}\n{exc.read().decode('utf-8', errors='replace')}"
                except Exception:
                    detail = f"HTTP {exc.code}"
            messagebox.showerror("MekiCopy", f"번역 및 표시 실패:\n{detail}", parent=self.root)

        self._task_runner.submit(
            "detached_ocr",
            operation,
            on_success=on_success,
            on_error=on_error,
        )

    def _send_overlayer_config(self) -> None:
        settings = self.settings
        _json_request(
            f"http://127.0.0.1:{settings.overlayer_port}/config",
            overlay_config_payload(settings, include_debug_log=False),
            timeout=3,
            method="POST",
        )

    def _show_feedback(self) -> None:
        button = self.button
        original_font = button.font()
        button.set_variant("success")
        button.setFont(make_font(("Malgun Gothic", 30, "bold")))
        button.setText("완료")

        def restore() -> None:
            if not self._closing:
                button.setFont(original_font)
                button.set_variant("primary")
                self._refresh_realtime_lock()

        self.root.after(1000, restore)

    def _settings_key(self, settings: AppSettings) -> tuple:
        return (
            settings.detached_always_on_top,
            settings.detached_hide_titlebar,
            settings.detached_fixed_size,
            settings.detached_fixed_width,
            settings.detached_fixed_height,
            settings.realtime_ocr_enabled,
        )

    @staticmethod
    def _current_settings_file_state() -> tuple[str, int, int]:
        """Return a cheap change marker without parsing the settings file."""

        path = mekicopy_settings.SETTINGS_FILE
        try:
            stat = os.stat(path)
            return path, int(stat.st_mtime_ns), int(stat.st_size)
        except OSError:
            # Missing files are normal before the first explicit save.
            return path, -1, -1

    def _schedule_settings_poll(self) -> None:
        if not self._closing:
            self._settings_after_id = self.root.after(
                self.SETTINGS_POLL_MS,
                self._poll_settings,
            )

    def _poll_settings(self) -> None:
        self._settings_after_id = None
        if self._closing:
            return
        progress = take_ocr_progress()
        if progress is not None:
            self._ocr_progress_text = progress
        self._refresh_realtime_lock()
        file_state = self._current_settings_file_state()
        if file_state == self._settings_file_state:
            self._schedule_settings_poll()
            return
        latest = load_settings()
        self._settings_file_state = file_state
        signature = self._settings_key(latest)
        if signature != self._settings_signature:
            self.capture_geometry(persist=True)
            self.settings = latest
            self.apply_settings()
            self._settings_signature = signature
        else:
            self.settings = latest
            self._topmost_controller.set_debug_log(latest.debug_logging)
        self._schedule_settings_poll()

    def _schedule_geometry_save(self) -> None:
        if self._closing:
            return
        if self._geometry_after_id is not None:
            self.root.after_cancel(self._geometry_after_id)
        self._geometry_after_id = self.root.after(250, self._persist_geometry)

    def _persist_geometry(self) -> None:
        self._geometry_after_id = None
        if not self._closing:
            self.capture_geometry(persist=True)

    def _on_mouse_down(self, event) -> None:
        position = event.globalPosition().toPoint()
        self._drag_action = self._hit_test(position.x(), position.y())
        self._drag_start = (position.x(), position.y())
        rect = self.root.geometry()
        self._drag_geometry = rect.getRect()
        self._suppress_next_click = False

    def _on_mouse_drag(self, event) -> None:
        if not self._drag_action or not self._drag_start or not self._drag_geometry:
            return
        start_x, start_y = self._drag_start
        origin_x, origin_y, origin_width, origin_height = self._drag_geometry
        position = event.globalPosition().toPoint()
        dx = position.x() - start_x
        dy = position.y() - start_y
        if not self._suppress_next_click and abs(dx) + abs(dy) < ensure_application().startDragDistance():
            return
        self._suppress_next_click = True

        if self.settings.detached_fixed_size or self._drag_action == "move":
            self.root.move(origin_x + dx, origin_y + dy)
            return

        x = origin_x
        y = origin_y
        width = origin_width
        height = origin_height
        if "e" in self._drag_action:
            width = max(self.MIN_WIDTH, origin_width + dx)
        if "s" in self._drag_action:
            height = max(self.MIN_HEIGHT, origin_height + dy)
        if "w" in self._drag_action:
            width = max(self.MIN_WIDTH, origin_width - dx)
            x = origin_x + origin_width - width
        if "n" in self._drag_action:
            height = max(self.MIN_HEIGHT, origin_height - dy)
            y = origin_y + origin_height - height
        self.root.setGeometry(x, y, width, height)

    def _on_mouse_release(self, _event) -> None:
        self._drag_action = None
        self._drag_start = None
        self._drag_geometry = None

    def _hit_test(self, x_root: int, y_root: int) -> str:
        if self.settings.detached_fixed_size:
            return "move"
        position = self.root.mapFromGlobal(QPoint(x_root, y_root))
        x, y = position.x(), position.y()
        width, height = self.root.width(), self.root.height()
        left = x <= self.RESIZE_MARGIN
        right = x >= width - self.RESIZE_MARGIN
        top = y <= self.RESIZE_MARGIN
        bottom = y >= height - self.RESIZE_MARGIN
        if top and left:
            return "nw"
        if top and right:
            return "ne"
        if bottom and left:
            return "sw"
        if bottom and right:
            return "se"
        if left:
            return "w"
        if right:
            return "e"
        if top:
            return "n"
        if bottom:
            return "s"
        return "move"


class MainWindow(TimerHost, QWidget):
    def __init__(self) -> None:
        _enable_dpi_awareness()
        _set_app_user_model_id()
        ensure_application()
        super().__init__()
        configure_window_theme(self)
        self._task_runner = QtTaskRunner(self)
        self._global_hotkey = GlobalHotkeyManager()
        self._global_hotkey_after_id: QTimer | None = None
        self._global_hotkey_settings: tuple[bool, str] | None = None
        self._global_hotkey_paused = False
        self._global_hotkey_action_active = False
        self.settings = load_settings()
        self._hytrans_config_fingerprint = fingerprint(
            load_api_settings(), self.settings.hytrans_backend
        )
        self._translation_status_api_model: str | None = None
        self._translation_status_after_id: QTimer | None = None
        self._translation_status_last_target = None
        self._translation_status_labels: list[QLabel] = []
        self.detached_process: subprocess.Popen | None = None
        self.settings_window: SettingsWindow | None = None
        self.hytrans_process: subprocess.Popen | None = None
        self._hytrans_restart_after_id: QTimer | None = None
        self._hytrans_restart_process: subprocess.Popen | None = None
        self._hytrans_restart_old_port: int | None = None
        self._hytrans_restart_attempts = 0
        self._hytrans_restart_notify = True
        self.overlayer_process: subprocess.Popen | None = None
        self.audio_capture_process: subprocess.Popen | None = None
        self.script_process: subprocess.Popen | None = None
        # Per-launch notices distinguish a user-confirmed close from a crash.
        self._companion_manual_close_signals: dict[str, ManualCloseSignal] = {}
        self.magpie_process: subprocess.Popen | None = None
        self.subtitle_window: MekiSubtitleWindow | None = None
        self.hdtrans_settings_window = None
        self.tray_icon = WindowsTrayIcon(self, "MekiCopy", self._restore_from_tray)
        self.setWindowTitle("MekiCopy")
        _set_window_icon(self)
        self.resize(MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT)
        self.setMinimumSize(MAIN_WINDOW_MIN_WIDTH, MAIN_WINDOW_MIN_HEIGHT)
        self.draft_region: Region | None = None
        self.active_region: Region | None = None
        self.active_region_ratio: RegionRatio | None = None
        self.active_monitor: MonitorInfo | None = None
        self.capture_status_text = "캡처 준비"
        self._closing = False
        self._restoring_from_tray = False
        # Track only our children; never terminate another instance sharing a port.
        self._companion_watchdog = CompanionWatchdog(
            interval_seconds=3.0,
            health_timeout_seconds=0.75,
            failures_before_recovery=3,
            cooldown_seconds=20.0,
            startup_grace_seconds=15.0,
        )
        self._companion_health_evaluators = {
            "MekiAudioCapture": default_health_evaluator(
                "MekiAudioCapture",
                require_ui_responsive=True,
                max_ui_heartbeat_age_seconds=7.0,
            ),
            "MekiScript": default_health_evaluator(
                "MekiScript",
                require_ui_responsive=True,
                max_ui_heartbeat_age_seconds=7.0,
            ),
            "MekiOverlayer": default_health_evaluator(
                "MekiOverlayer",
                require_ui_responsive=True,
                max_ui_heartbeat_age_seconds=7.0,
            ),
            # HYTrans manages its browser worker; this watchdog monitors the server.
            "HYTrans": default_health_evaluator("HYTrans"),
        }
        self._companion_watchdog_after_id: QTimer | None = None
        self._realtime_state_after_id: QTimer | None = None
        self._realtime_requested = False
        self._realtime_service_status: dict[str, bool] = {}
        self._realtime_service_ports: tuple[int, int] | None = None
        self._realtime_service_processes = None
        self._realtime_service_checked_at = 0.0
        self._realtime_service_probe_at = 0.0
        self._build_ui()
        self._realtime_ocr = RealtimeOcrController(
            self, self._task_runner,
            prepare_operation=self._prepare_realtime_operation,
            on_result=self._on_realtime_result,
            on_error=self._on_realtime_error,
            on_state=self._on_realtime_state_changed,
        )
        self.apply_settings(self.settings, persist=False)
        self._companion_watchdog.start()
        self._schedule_companion_watchdog_poll()
        self._schedule_global_hotkey_poll()
        self._schedule_translation_status_poll()

    def _build_ui(self) -> None:
        self._workflow_mode: str | None = None
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 8, 12, 12)
        root_layout.setSpacing(7)
        header = QHBoxLayout()
        mark = QLabel("✿", self)
        mark.setProperty("role", "accent")
        mark.setFont(make_font(("Segoe UI Symbol", 17)))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(27, 30)
        header.addWidget(mark)
        self.workflow_title = QLabel("MekiCopy", self)
        self.workflow_title.setFont(make_font(("Malgun Gothic", 15, "bold")))
        header.addWidget(self.workflow_title)
        header.addStretch()
        self.workflow_back_button = RoundedButton(
            self, text="‹  작업 선택", command=self._show_workflow_picker,
            height=31, radius=11, variant="subtle",
        )
        self.workflow_back_button.setFixedWidth(105)
        header.addWidget(self.workflow_back_button)
        root_layout.addLayout(header)

        self.workflow_pages = QStackedWidget(self)
        root_layout.addWidget(self.workflow_pages, stretch=1)

        self.workflow_picker = QFrame(self.workflow_pages)
        self.workflow_picker.setProperty("role", "card")
        picker = QVBoxLayout(self.workflow_picker)
        picker.setContentsMargins(15, 12, 15, 12)
        picker.setSpacing(7)
        title = QLabel("무엇을 번역하시겠어요?", self.workflow_picker)
        title.setFont(make_font(("Malgun Gothic", 17, "bold")))
        picker.addWidget(title)
        self.workflow_buttons: dict[str, RoundedButton] = {}
        for mode, label in (
            ("screen", "화면에 나오는 일본어 번역"),
            ("audio", "스피커에서 나오는 일본어 음성 번역"),
            ("subtitle", "영상 파일의 한국어 자막 생성"),
        ):
            button = RoundedButton(
                self.workflow_picker, text=label,
                height=45, font=("Malgun Gothic", 11, "bold"),
                variant="primary" if mode == "screen" else "normal",
                radius=14, anchor="w",
            )
            button.clicked.connect(lambda checked=False, selected=mode: self._select_workflow(selected))
            picker.addWidget(button)
            self.workflow_buttons[mode] = button
        settings = RoundedButton(
            self.workflow_picker, "설정 열기", self._on_open_settings,
            height=31, radius=11, variant="subtle",
        )
        settings.setFixedHeight(31)
        picker.addWidget(settings, alignment=Qt.AlignmentFlag.AlignRight)
        self.workflow_pages.addWidget(self.workflow_picker)

        self.workflow_body = QFrame(self.workflow_pages)
        body = QHBoxLayout(self.workflow_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        self.workflow_pages.addWidget(self.workflow_body)

        self.tab_buttons: dict[str, RoundedButton] = {}
        self.tab_frames: dict[str, QFrame] = {}
        self.active_tab_name = "영역 설정"
        self.tab_bar = QWidget(self.workflow_body)
        self.tab_bar.setFixedWidth(MAIN_TAB_BAR_WIDTH)
        tabs = QVBoxLayout(self.tab_bar)
        tabs.setContentsMargins(0, 0, 0, 0)
        tabs.setSpacing(5)
        body.addWidget(self.tab_bar)

        self.workflow_content = QFrame(self.workflow_body)
        self.workflow_content.setProperty("role", "card")
        content_layout = QVBoxLayout(self.workflow_content)
        content_layout.setContentsMargins(7, 7, 7, 7)
        self.tab_pages = QStackedWidget(self.workflow_content)
        content_layout.addWidget(self.tab_pages)
        body.addWidget(self.workflow_content, stretch=1)
        self._tab_scroll_areas: dict[str, QScrollArea] = {}

        for name in (
            "영역 설정", "캡쳐 설정", "도구", "행동", "소리 번역", "영상 자막 생성", "설정",
        ):
            button = RoundedButton(self.tab_bar, text=name, anchor="w", height=34, radius=12)
            if name == "설정":
                button.clicked.connect(self._on_open_settings)
            else:
                button.clicked.connect(lambda checked=False, tab_name=name: self._select_tab(tab_name))
            tabs.addWidget(button)
            self.tab_buttons[name] = button

            scroll = QScrollArea(self.tab_pages)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setStyleSheet("QWidget#MekiTab, QScrollArea > QWidget > QWidget { background: white; }")
            frame = QFrame()
            frame.setObjectName("MekiTab")
            tab_layout = QVBoxLayout(frame)
            tab_layout.setContentsMargins(4, 3, 4, 3)
            tab_layout.setSpacing(5)
            tab_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            scroll.setWidget(frame)
            self.tab_frames[name] = frame
            self._tab_scroll_areas[name] = scroll
            self.tab_pages.addWidget(scroll)
        tabs.addStretch()

        self._build_region_tab(self.tab_frames["영역 설정"])
        self._build_capture_tab(self.tab_frames["캡쳐 설정"])
        self._build_audio_tab(self.tab_frames["소리 번역"])
        self._build_subtitle_tab(self.tab_frames["영상 자막 생성"])
        self._build_tools_tab(self.tab_frames["도구"])
        self._build_action_tab(self.tab_frames["행동"])
        self._add_tab_button(self.tab_frames["설정"], "설정 열기", self._on_open_settings)

        self._show_workflow_picker()
        self._update_status()

    def _show_workflow_picker(self) -> None:
        self._workflow_mode = None
        self._realtime_requested = False
        self._sync_realtime_ocr()
        self._configure_global_hotkey(self.settings)
        self.workflow_back_button.hide()
        self.workflow_title.setText("MekiCopy")
        self.workflow_pages.setCurrentWidget(self.workflow_picker)

    def _select_workflow(self, mode: str) -> None:
        choices = {
            "screen": ("화면 번역", ("영역 설정", "캡쳐 설정", "도구", "행동", "설정")),
            "audio": ("소리 번역", ("소리 번역",)),
            "subtitle": ("영상 자막 생성", ("영상 자막 생성",)),
        }
        title, tabs = choices[mode]
        if self._workflow_mode != mode:
            self._realtime_requested = False
        self._workflow_mode = mode
        self._sync_realtime_ocr()
        self._configure_global_hotkey(self.settings)
        self.workflow_title.setText(title)
        self.workflow_back_button.show()
        for name, button in self.tab_buttons.items():
            button.setVisible(name in tabs)
        self.tab_bar.setVisible(mode == "screen")
        self.workflow_pages.setCurrentWidget(self.workflow_body)
        self._select_tab(tabs[0])

    @staticmethod
    def _add_tab_label(frame: QFrame, text: str = "", *, role: str = "") -> QLabel:
        label = QLabel(text, frame)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        if role:
            label.setProperty("role", role)
        frame.layout().addWidget(label)
        return label

    def _build_region_tab(self, frame: QFrame) -> None:
        self.status_label = self._add_tab_label(frame, role="muted")
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_label.setContentsMargins(4, 2, 4, 4)
        self._add_tab_button(frame, "임시 영역 선택", self._on_select_region, variant="primary")
        for text, command in (
            ("현재 임시/확정 영역 보기", self._on_view_regions),
            ("임시 영역을 확정", self._on_set_region),
            ("확정 영역을 북마크에 추가", self._on_save_active_bookmark),
            ("북마크 영역 불러오기", self._on_load_bookmark),
        ):
            self._add_tab_button(frame, text, command)

    def _build_capture_tab(self, frame: QFrame) -> None:
        self.capture_status_label = self._add_tab_label(frame, self.capture_status_text, role="accent")
        self.capture_status_label.setContentsMargins(4, 2, 4, 4)

        for text, command in (
            ("캡쳐 진단 저장", self._on_capture_diagnostics),
            ("모니터 다시 검색", self._on_refresh_capture_monitors),
            ("비율 좌표로 영역 복구", self._on_restore_region_from_ratio),
        ):
            self._add_tab_button(frame, text, command)
        self.magpie_button = self._add_tab_button(frame, "MagPie 실행", self._on_start_magpie)

    def _build_tools_tab(self, frame: QFrame) -> None:
        self._build_translation_status_label(frame)
        self.hytrans_button = self._add_tab_button(
            frame, "번역 엔진 실행", self._on_start_hytrans, variant="primary",
        )
        self.overlayer_button = self._add_tab_button(
            frame, "번역 결과 표시기 실행", self._on_start_overlayer,
        )
        self._add_tab_button(frame, "모든 도구 연결 상태 확인", self._on_test_overlay_connection)

    def _build_audio_tab(self, frame: QFrame) -> None:
        self._build_translation_status_label(frame)
        self._add_tab_button(frame, "녹음 엔진 실행", self._on_start_audio_capture, variant="primary")
        self._add_tab_button(frame, "번역 엔진 실행", self._on_start_hytrans)
        self._add_tab_button(frame, "번역 결과 표시기 실행", self._on_start_script)
        self._add_tab_button(frame, "연결 상태 확인", self._on_test_audio_connection)
        self._add_tab_button(frame, "설정", self._on_open_settings)

    def _build_translation_status_label(self, frame: QFrame) -> None:
        label = self._add_tab_label(frame, role="accent")
        label.setContentsMargins(4, 2, 4, 4)
        label.hide()
        self._translation_status_labels.append(label)

    def _set_translation_status(self, text: str) -> None:
        for label in self._translation_status_labels:
            label.setText(text)
            label.setVisible(bool(text))

    def _schedule_translation_status_poll(self, *, immediately: bool = False) -> None:
        if self._translation_status_after_id is not None:
            self.after_cancel(self._translation_status_after_id)
        self._translation_status_after_id = self.after(
            0 if immediately else TRANSLATION_STATUS_POLL_MS, self._poll_translation_status,
        )

    def _translation_status_target(self) -> tuple[str, str, str, str, subprocess.Popen | None]:
        return (
            self._hytrans_base_url(), normalize_backend(self.settings.hytrans_backend),
            self.settings.hytrans_model_id, self._hytrans_config_fingerprint, self.hytrans_process,
        )

    def _poll_translation_status(self) -> None:
        self._translation_status_after_id = None
        if self._closing:
            return
        self._schedule_translation_status_poll()
        if (self._workflow_mode not in {"screen", "audio"}
                or self.active_tab_name not in {"도구", "소리 번역"}
                or not self.isVisible() or self.isMinimized()):
            return
        target = self._translation_status_target()
        url, backend, model_id, _, process = target
        if backend == "hdtrans":
            self._translation_status_last_target = None
            self._set_translation_status("")
            return
        model = self._translation_status_api_model
        name = BACKEND_LABELS[backend]
        if backend == "local":
            profile = get_model_profile(model_id)
            model = profile.model_id
            if profile is PLAMO3_PROFILE:
                name = "pLaMo-3"
        title = f"번역 엔진 상태 · {name}"
        if self._hytrans_restart_after_id is not None:
            self._translation_status_last_target = None
            self._set_translation_status(f"{title}\n엔진 재시작 중")
            return
        if self._translation_status_last_target != target:
            self._translation_status_last_target = target
            self._set_translation_status(f"{title}\n상태 확인 중")

        def show(status: str) -> None:
            if (not self._closing and self._hytrans_restart_after_id is None
                    and self._translation_status_target() == target):
                self._set_translation_status(f"{title}\n{status}")

        def unavailable(_error: Exception) -> None:
            if _is_process_alive(process):
                status = "번역 서버 응답 대기 중"
            elif process is not None:
                status = "번역 서버가 종료됨"
            else:
                status = "실행 대기"
            show(status)

        self._task_runner.submit(
            "translation_status", lambda: _json_request(f"{url}/translation/status", timeout=0.75),
            on_success=lambda payload: show(_describe_translation_status(payload, backend, model)),
            on_error=unavailable,
        )

    def _build_subtitle_tab(self, frame: QFrame) -> None:
        title = self._add_tab_label(frame, "영상 자막 생성", role="accent")
        title.setFont(make_font(("Malgun Gothic", 14, "bold")))
        self._add_tab_label(
            frame, "영상 파일의 일본어 음성을 인식하고 번역해 한국어 자막 파일을 만듭니다.",
            role="muted",
        )
        self._add_tab_button(
            frame, "자막 제작 도구 열기", self._on_open_meki_subtitle, variant="primary",
        )
        self._add_tab_button(frame, "설정", self._on_open_settings)

    @staticmethod
    def _bring_forward(window: QWidget) -> None:
        window.showNormal()
        window.raise_()
        window.activateWindow()

    def _on_open_meki_subtitle(self) -> None:
        if self.subtitle_window is not None:
            self._bring_forward(self.subtitle_window)
            return

        def forget_window() -> None:
            self.subtitle_window = None

        window = MekiSubtitleWindow(
            self,
            settings=self.settings,
            hytrans_url=self._hytrans_base_url,
            start_hytrans=self._on_start_hytrans,
            on_destroy=forget_window,
        )
        self.subtitle_window = window
        _set_window_icon(window)
        window.show()

    def _build_action_tab(self, frame: QFrame) -> None:
        self.detach_button = self._add_tab_button(frame, "", self._on_detach_ocr_button)
        self.ocr_button = RoundedButton(
            frame,
            text="번역 후 표시",
            command=self._on_translation_action,
            font=("Malgun Gothic", 16, "bold"),
            variant="primary",
            radius=16,
        )
        self.ocr_button.setFixedHeight(OCR_BUTTON_HEIGHT_PX)
        frame.layout().addWidget(self.ocr_button)
        self.realtime_button = self.ocr_button
        self.action_status_label = self._add_tab_label(frame, role="muted")

    @staticmethod
    def _add_tab_button(
        frame: QFrame,
        text: str,
        command: Callable[[], None],
        *,
        variant: str = "normal",
    ) -> RoundedButton:
        button = RoundedButton(frame, text=text, command=command, height=35, radius=12, variant=variant)
        frame.layout().addWidget(button)
        return button

    def _select_tab(self, name: str) -> None:
        self.active_tab_name = name
        self.tab_pages.setCurrentWidget(self._tab_scroll_areas[name])
        for tab_name, button in self.tab_buttons.items():
            button.set_variant("tab-active" if tab_name == name else "normal")
        if name in {"도구", "소리 번역"}:
            self._schedule_translation_status_poll(immediately=True)

    def ocr_action_label(self) -> str:
        realtime = getattr(self, "_realtime_ocr", None)
        if self.settings.realtime_ocr_enabled:
            return "실시간 OCR 중지" if realtime is not None and realtime.active else "실시간 OCR 시작"
        return "번역 후 표시"

    def _on_translation_action(self) -> None:
        if self.settings.realtime_ocr_enabled:
            self._on_toggle_realtime_ocr()
        else:
            self._on_ocr_copy(source_button=self.ocr_button)

    def _apply_overlay_mode_ui(self) -> None:
        realtime = getattr(self, "_realtime_ocr", None)
        active = realtime is not None and realtime.active
        realtime_mode = self.settings.realtime_ocr_enabled
        reason = self._realtime_block_reason() if realtime_mode and realtime is not None else ""
        if self.ocr_button.property("variant") != "success" or realtime_mode:
            self.ocr_button.setText(self.ocr_action_label())
        self.ocr_button.setEnabled(not (realtime_mode and reason and not active))
        self.detach_button.setText("'번역 후 표시' 버튼 분리하기")
        self.detach_button.setEnabled(not realtime_mode)
        status_label = getattr(self, "action_status_label", None)
        if status_label is not None:
            status_label.setText(self.capture_status_text if active else reason)

    @staticmethod
    def _global_hotkey_configuration(settings: AppSettings) -> tuple[bool, str]:
        return settings.global_hotkey_enabled and not settings.realtime_ocr_enabled, settings.global_hotkey

    def _configure_global_hotkey(
        self,
        settings: AppSettings,
        *,
        force: bool = False,
    ) -> tuple[bool, str]:
        configuration = self._global_hotkey_configuration(settings)
        if getattr(self, "_workflow_mode", "screen") != "screen":
            configuration = (False, configuration[1])
        if self._global_hotkey_paused:
            self._global_hotkey_settings = configuration
            return True, ""
        if not force and configuration == self._global_hotkey_settings:
            return True, ""

        result = self._global_hotkey.configure(
            enabled=configuration[0],
            hotkey=configuration[1],
        )
        if result.ok:
            self._global_hotkey_settings = configuration
            return True, ""
        detail = result.error or "알 수 없는 오류"
        _log_runtime_error("global_hotkey", detail)
        return False, detail

    def pause_global_hotkey(self) -> None:
        """Temporarily release the key while the settings capture field has focus."""

        if self._closing or self._global_hotkey_paused:
            return
        result = self._global_hotkey.configure(
            enabled=False,
            hotkey=self.settings.global_hotkey,
        )
        if result.ok:
            self._global_hotkey_paused = True
        else:
            _log_runtime_error("pause_global_hotkey", result.error or "unknown error")

    def resume_global_hotkey(self) -> None:
        if self._closing or not self._global_hotkey_paused:
            return
        self._global_hotkey_paused = False
        configured, detail = self._configure_global_hotkey(self.settings, force=True)
        if not configured:
            # Keep it released if re-registration failed, rather than leaving
            # the settings field unable to accept the selected ordinary key.
            self._global_hotkey_paused = True
            _log_runtime_error("resume_global_hotkey", detail)

    def _schedule_global_hotkey_poll(self) -> None:
        if not self._closing:
            self._global_hotkey_after_id = self.after(
                GLOBAL_HOTKEY_POLL_MS,
                self._poll_global_hotkey,
            )

    def _poll_global_hotkey(self) -> None:
        self._global_hotkey_after_id = None
        if self._closing:
            return
        progress = take_ocr_progress()
        if progress is not None:
            self._set_capture_status(progress)
        activations = self._global_hotkey.drain_activations()
        # Collapse key repeats that arrive in the same event-loop turn.
        # The keyed task runner also serializes manual OCR requests.
        if (
            activations
            and self.settings.global_hotkey_enabled
            and not self.settings.realtime_ocr_enabled
            and getattr(self, "_workflow_mode", "screen") == "screen"
            and not self._global_hotkey_paused
            and not self._global_hotkey_action_active
            and not (getattr(self, "_realtime_ocr", None)
                     and self._realtime_ocr.translation_locked)
            and not self._task_runner.is_running("main_ocr")
        ):
            self._global_hotkey_action_active = True
            started = self._on_ocr_copy(
                source_button=self.ocr_button,
                on_finished=self._on_global_hotkey_action_finished,
            )
            if not started:
                self._global_hotkey_action_active = False
        self._schedule_global_hotkey_poll()

    def _on_global_hotkey_action_finished(self) -> None:
        self._global_hotkey_action_active = False

    def _format_region(self, region: Region | None) -> str:
        if not region:
            return "설정되지 않음"
        return f"({region.left}, {region.top}) · {region.width} × {region.height} px"

    def _update_status(self) -> None:
        draft_text = self._format_region(self.draft_region)
        active_text = self._format_region(self.active_region)
        self.status_label.setText(f"임시 영역 · {draft_text}\n확정 영역 · {active_text}")

    def _set_capture_status(self, text: str) -> None:
        self.capture_status_text = text
        if hasattr(self, "capture_status_label"):
            self.capture_status_label.setText(text)
        label = getattr(self, "action_status_label", None)
        if label is not None:
            label.setText(text)

    def _set_capture_status_from_last_result(self) -> None:
        result = get_capture_manager().last_result
        if result is None:
            return
        stats = ""
        if result.stats is not None:
            stats = f" / mean={result.stats.mean:.1f}, std={result.stats.stddev:.1f}"
        self._set_capture_status(
            f"캡처 상태: {result.status} / 방식: {result.strategy}{stats}"
        )

    def _remember_active_region_profile(self) -> None:
        self._sync_realtime_ocr()
        if not self.active_region:
            self.active_region_ratio = None
            self.active_monitor = None
            return
        try:
            save_detached_region(self.active_region)
        except OSError as exc:
            _log_runtime_error("save_detached_region", exc)
        try:
            manager = get_capture_manager()
            manager.refresh_if_display_changed()
            monitors = manager.get_real_monitors()
            monitor = _find_monitor_for_region(self.active_region, monitors)
            if monitor is None:
                self.active_region_ratio = None
                self.active_monitor = None
                self._set_capture_status("캡처 상태: OCR 영역과 일치하는 모니터를 찾지 못했습니다.")
                return
            self.active_monitor = monitor
            self.active_region_ratio = _ratio_for_region(self.active_region, monitor)
            self._set_capture_status(
                (
                    "캡처 상태: 영역 프로필 저장됨 "
                    f"(monitor={monitor.index}, ratio={self.active_region_ratio.to_dict()})"
                )
            )
        except Exception as exc:
            _log_runtime_error("remember_active_region_profile", exc)
            self._set_capture_status(f"캡처 상태: 영역 프로필 저장 실패 - {exc}")

    def _prepare_active_region_for_capture(self) -> bool:
        if not self.active_region:
            messagebox.showerror("MekiCopy", "설정된 영역이 없습니다.", parent=self)
            return False
        try:
            region, monitor, ratio, messages = get_capture_manager().resolve_region(
                self.active_region,
                previous_monitor=self.active_monitor,
                ratio=self.active_region_ratio,
            )
        except Exception as exc:
            _log_runtime_error("prepare_active_region_for_capture", exc)
            messagebox.showerror("MekiCopy", f"캡처 영역 확인 실패:\n{exc}", parent=self)
            return False

        if region.width < MIN_SIZE_PX or region.height < MIN_SIZE_PX:
            messagebox.showerror(
                "MekiCopy",
                "OCR 영역이 현재 화면에서 너무 작습니다. 영역을 다시 지정해주세요.",
                parent=self,
            )
            return False

        changed = not _regions_equal(region, self.active_region)
        self.active_region = region
        try:
            save_detached_region(region)
        except OSError as exc:
            _log_runtime_error("save_detached_region", exc)
        self.active_monitor = monitor
        self.active_region_ratio = ratio
        if changed:
            self._sync_realtime_ocr()
            self.draft_region = Region(
                left=region.left,
                top=region.top,
                width=region.width,
                height=region.height,
            )
            self._update_status()
        if messages:
            self._set_capture_status(" / ".join(messages))
        return True

    def _on_refresh_capture_monitors(self) -> None:
        try:
            get_capture_manager().reinitialize("manual_refresh")
            self._prepare_active_region_for_capture()
            monitors = get_capture_manager().get_monitors()
            self._set_capture_status(
                f"캡처 상태: 모니터 {len(monitors)}개 재검색 완료 / {_monitor_signature(monitors)}"
            )
        except Exception as exc:
            _log_runtime_error("refresh_capture_monitors", exc)
            messagebox.showerror("MekiCopy", f"모니터 재검색 실패:\n{exc}", parent=self)

    def _on_restore_region_from_ratio(self) -> None:
        if not self.active_region_ratio:
            messagebox.showerror("MekiCopy", "복구할 비율 좌표가 없습니다.", parent=self)
            return
        monitors = get_capture_manager().get_real_monitors()
        monitor = _match_monitor_by_previous_rect(self.active_monitor, monitors)
        if monitor is None:
            messagebox.showerror("MekiCopy", "대상 모니터를 찾지 못했습니다.", parent=self)
            return
        region = _region_from_ratio(monitor, self.active_region_ratio)
        region = _clamp_region_to_monitor(region, monitor)
        self.active_region = region
        self.draft_region = Region(
            left=region.left,
            top=region.top,
            width=region.width,
            height=region.height,
        )
        self.active_monitor = monitor
        self.active_region_ratio = _ratio_for_region(region, monitor)
        self._sync_realtime_ocr()
        try:
            save_detached_region(region)
        except OSError as exc:
            _log_runtime_error("save_detached_region", exc)
        self._update_status()
        self._set_capture_status("캡처 상태: 비율 좌표로 OCR 영역을 복구했습니다.")

    def _on_capture_diagnostics(self) -> None:
        region = self.active_region

        def succeeded(directory) -> None:
            self._set_capture_status(f"캡처 진단 저장 완료: {directory}")
            messagebox.showinfo(
                "MekiCopy", f"캡처 진단 파일을 저장했습니다:\n{directory}", parent=self,
            )

        def failed(exc: Exception) -> None:
            _log_runtime_error("capture_diagnostics", exc)
            messagebox.showerror("MekiCopy", f"캡처 진단 실패:\n{exc}", parent=self)

        if self._task_runner.submit(
            "capture_diagnostics", lambda: run_capture_diagnostics(region),
            on_success=succeeded, on_error=failed,
        ):
            self._set_capture_status("캡처 진단을 진행 중입니다.")

    def _launch_magpie(self, executable: str) -> None:
        if _is_process_alive(self.magpie_process):
            messagebox.showinfo("MekiCopy", "MagPie가 이미 실행 중입니다.", parent=self)
            return
        if not self.settings.suppress_magpie_launch_notice:
            messagebox.showinfo("MagPie 실행 안내", MAGPIE_LAUNCH_NOTICE, parent=self)
        try:
            self.magpie_process = subprocess.Popen(
                [executable],
                cwd=os.path.dirname(executable),
            )
            self._set_capture_status(f"MagPie 실행됨: {executable}")
            _log_runtime_message("start_magpie", executable)
        except Exception as exc:
            _log_runtime_error("start_magpie", exc)
            messagebox.showerror("MekiCopy", f"MagPie 실행 실패:\n{exc}", parent=self)

    def _on_start_magpie(self) -> None:
        executable = _find_magpie_executable()
        if executable:
            self._launch_magpie(executable)
            return
        def reset_button() -> None:
            self.magpie_button.setEnabled(True)
            self.magpie_button.setText("MagPie 실행")

        def installed(executable: str) -> None:
            reset_button()
            self._set_capture_status(f"MagPie 설치 완료: {executable}")
            self._launch_magpie(executable)

        def failed(exc: Exception) -> None:
            reset_button()
            _log_runtime_error("install_magpie", exc)
            self._set_capture_status("MagPie 다운로드 또는 설치 실패")
            messagebox.showerror("MekiCopy", f"MagPie 다운로드 또는 설치 실패:\n{exc}", parent=self)

        if self._task_runner.submit(
            "magpie_install", _install_latest_magpie, on_success=installed, on_error=failed,
        ):
            self.magpie_button.setEnabled(False)
            self.magpie_button.setText("MagPie 다운로드 중…")
            self._set_capture_status("MagPie 최신 버전을 다운로드하고 압축을 푸는 중입니다…")

    def _on_select_region(self) -> None:
        initial = self.draft_region or self.active_region
        selection = run_selection(
            initial_region=initial,
            capture_on_enter=False,
            parent=self,
        )
        if selection:
            self.draft_region = selection
            self._update_status()

    def _on_view_regions(self) -> None:
        if not self.draft_region and not self.active_region:
            messagebox.showerror("MekiCopy", "표시할 영역이 없습니다.")
            return
        run_region_view(self.draft_region, self.active_region, parent=self)

    def _on_save_active_bookmark(self) -> None:
        if not self.active_region:
            messagebox.showerror("MekiCopy", "확정 영역이 없습니다.")
            return
        name = simpledialog.askstring("MekiCopy", "북마크 이름을 입력하세요", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if any(separator in name for separator in ("\t", "\n", "\r")):
            messagebox.showerror("MekiCopy", "북마크 이름에 탭 또는 줄바꿈을 사용할 수 없습니다.", parent=self)
            return
        bookmarks = load_bookmarks()
        if name in bookmarks and not messagebox.askyesno(
            "MekiCopy",
            "같은 이름의 북마크가 있습니다. 덮어쓸까요?",
            parent=self,
        ):
            return
        bookmarks[name] = Bookmark(
            name=name,
            left=self.active_region.left,
            top=self.active_region.top,
            width=self.active_region.width,
            height=self.active_region.height,
        )
        if not save_bookmarks(bookmarks):
            messagebox.showerror("MekiCopy", "북마크를 저장하지 못했습니다.", parent=self)
            return
        messagebox.showinfo("MekiCopy", "확정 영역이 북마크에 추가되었습니다.", parent=self)

    def _load_bookmark_region(self, bookmark: Bookmark) -> None:
        region = Region(
            left=bookmark.left,
            top=bookmark.top,
            width=bookmark.width,
            height=bookmark.height,
        )
        self.draft_region = region
        self.active_region = Region(
            left=region.left,
            top=region.top,
            width=region.width,
            height=region.height,
        )
        self._remember_active_region_profile()
        self._update_status()

    def _on_load_bookmark(self) -> None:
        bookmark = pick_bookmark(parent=self)
        if not bookmark:
            return
        self._load_bookmark_region(bookmark)

    def _on_set_region(self) -> None:
        if not self.draft_region:
            messagebox.showerror("MekiCopy", "먼저 영역을 지정하거나 북마크를 불러오세요.")
            return
        self.active_region = Region(
            left=self.draft_region.left,
            top=self.draft_region.top,
            width=self.draft_region.width,
            height=self.draft_region.height,
        )
        self._remember_active_region_profile()
        self._update_status()
        messagebox.showinfo("MekiCopy", "인식 영역이 설정되었습니다!")

    def _on_detach_ocr_button(self) -> None:
        if self.settings.realtime_ocr_enabled:
            return
        if self.active_region:
            try:
                save_detached_region(self.active_region)
            except OSError as exc:
                _log_runtime_error("save_detached_region", exc)
        try:
            self.detached_process = _launch_detached_button_process()
        except Exception as exc:
            _log_runtime_error("start_detached_button", exc)
            messagebox.showerror(
                "MekiCopy",
                f"분리 버튼 실행 실패:\n{exc}",
                parent=self,
            )

    def _on_open_settings(self) -> None:
        if self.settings_window is not None:
            self._bring_forward(self.settings_window)
            return
        self.settings_window = SettingsWindow(self)
        self.settings_window.show()

    def _hytrans_base_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.hytrans_port}"

    def _overlayer_base_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.overlayer_port}"

    def _overlayer_show_url(self) -> str:
        return f"{self._overlayer_base_url()}/show"

    def _script_base_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.script_port}"

    def _audio_capture_base_url(self) -> str:
        return f"http://127.0.0.1:{self.settings.audio_capture_port}"

    def _warn_if_port_busy(self, app_name: str, port: int) -> bool:
        if not _is_loopback_port_open(port):
            return False
        messagebox.showerror(
            "MekiCopy",
            (
                f"{app_name}를 시작할 수 없습니다.\n\n"
                f"127.0.0.1:{port} 포트가 이미 열려 있습니다. "
                "이전 실행이 시작 중이면 잠시 뒤 연결 상태 확인을 눌러보고, "
                "다른 프로그램이 사용 중이면 설정에서 포트를 바꿔주세요."
            ),
            parent=self,
        )
        return True

    def _tracked_process_is_starting(
        self,
        app_name: str,
        process: subprocess.Popen | None,
    ) -> bool:
        if not _is_process_alive(process):
            return False
        messagebox.showinfo(
            "MekiCopy",
            f"{app_name}가 이미 실행 중이거나 시작을 마무리하고 있습니다.",
            parent=self,
        )
        return True

    def _watch_started_process(
        self,
        app_name: str,
        process: subprocess.Popen | None,
        base_url: str | None = None,
        *,
        notify: bool = True,
    ) -> None:
        if process is None:
            return

        def check() -> None:
            if self._closing:
                return
            if not _is_process_alive(process):
                exit_code = process.poll()
                _log_runtime_message(
                    "companion_exited_after_start",
                    f"{app_name} exit_code={exit_code}",
                )
                if exit_code == MANUAL_CLOSE_EXIT_CODE:
                    # A confirmed close during startup is not a launch failure.
                    return
                if notify:
                    messagebox.showerror(
                        "MekiCopy",
                        (
                            f"{app_name}가 시작 직후 종료되었습니다.\n\n"
                            f"종료 코드: {exit_code}\n"
                            "포트 충돌, 누락된 런타임, 손상된 설정이 원인일 수 있습니다. "
                            "디버그 로그를 켜고 다시 실행하면 error_log/debug_log에 더 자세히 남습니다."
                        ),
                        parent=self,
                    )
                return
            if not base_url:
                return
            try:
                health = _json_request(f"{base_url.rstrip('/')}/health", timeout=0.75)
                _log_runtime_message(
                    "companion_health_after_start",
                    f"{app_name}: {health}",
                )
            except Exception as exc:
                _log_runtime_message(
                    "companion_health_pending",
                    f"{app_name}: {exc}",
                )

        self.after(1600, check)

    def _track_owned_companion(
        self,
        app_name: str,
        process: subprocess.Popen,
        base_url: str,
        *,
        manual_close_signal: ManualCloseSignal | None = None,
    ) -> None:
        """Register only a child this MekiCopy instance just spawned."""

        evaluator = self._companion_health_evaluators.get(app_name)
        if evaluator is None:
            raise ValueError(f"지원하지 않는 감시 대상입니다: {app_name}")
        signals = getattr(self, "_companion_manual_close_signals", None)
        if signals is None:
            signals = {}
            self._companion_manual_close_signals = signals
        previous_signal = signals.get(app_name)
        if manual_close_signal is None:
            signals.pop(app_name, None)
        else:
            signals[app_name] = manual_close_signal
        if previous_signal is not manual_close_signal:
            discard_manual_close_signal(previous_signal)
        self._companion_watchdog.track(
            app_name,
            expected_app=app_name,
            base_url=base_url,
            process=process,
            evaluator=evaluator,
        )

    def _create_manual_close_signal(self, app_name: str) -> ManualCloseSignal | None:
        """Prepare an optional close notice without making launch depend on disk."""

        try:
            return create_manual_close_signal(app_name)
        except OSError as exc:
            # The exit code preserves a confirmed close if the notice file fails.
            _log_runtime_error("create_companion_manual_close_signal", exc)
            return None

    def _consume_manual_close_signal(
        self,
        app_name: str,
        process: subprocess.Popen | None,
    ) -> bool:
        """Stop supervising a child which the user explicitly chose to close."""

        signals = getattr(self, "_companion_manual_close_signals", None)
        if not signals:
            return False
        signal = signals.get(app_name)
        if signal is None:
            return False
        process_id = getattr(process, "pid", None)
        if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
            return False
        if not consume_manual_close_signal(
            signal,
            app_name=app_name,
            process_id=process_id,
        ):
            return False
        signals.pop(app_name, None)
        self._companion_watchdog.untrack(app_name)
        _log_runtime_message(
            "companion_manual_close",
            f"{app_name} was closed after user confirmation; watchdog recovery disabled",
        )
        return True

    def _consume_pending_manual_close_signals(self) -> None:
        """Process close notices before draining queued recovery requests."""

        for app_name in _MANUALLY_CLOSABLE_COMPANIONS:
            process_attribute = _COMPANION_PROCESS_ATTRIBUTES[app_name]
            process = getattr(self, process_attribute, None)
            try:
                has_exited = process is not None and process.poll() is not None
            except Exception:
                has_exited = False
            if not has_exited:
                # Poll idle notices less often; recovery rechecks them before acting.
                continue
            MainWindow._consume_manual_close_signal(
                self,
                app_name,
                process,
            )

    def _suppress_confirmed_manual_close_recovery(
        self,
        request: RecoveryRequest,
        process: subprocess.Popen | None,
    ) -> bool:
        """Reject a recovery request when its child confirmed a manual close."""

        if request.name not in _MANUALLY_CLOSABLE_COMPANIONS:
            return False
        if MainWindow._consume_manual_close_signal(self, request.name, process):
            return True
        if request.exit_code != MANUAL_CLOSE_EXIT_CODE:
            return False
        if not self._companion_watchdog.matches_process(request, process):
            return False

        # Fall back to exit status when a confirmed close could not write its notice.
        signals = getattr(self, "_companion_manual_close_signals", None)
        if signals:
            discard_manual_close_signal(signals.pop(request.name, None))
        self._companion_watchdog.untrack(request.name)
        _log_runtime_message(
            "companion_manual_close",
            f"{request.name} exited after user confirmation; watchdog recovery disabled",
        )
        return True

    def _schedule_companion_watchdog_poll(self) -> None:
        if self._closing:
            return
        try:
            self._companion_watchdog_after_id = self.after(
                # Health probes run off-thread; this timer handles queued recovery.
                500,
                self._poll_companion_watchdog,
            )
        except RuntimeError:
            self._companion_watchdog_after_id = None

    def _poll_companion_watchdog(self) -> None:
        self._companion_watchdog_after_id = None
        try:
            if self._closing:
                return
            self._refresh_realtime_services()
            self._consume_pending_manual_close_signals()
            for request in self._companion_watchdog.drain_recovery_requests():
                process_attribute = _COMPANION_PROCESS_ATTRIBUTES.get(request.name)
                process = (
                    getattr(self, process_attribute, None)
                    if process_attribute is not None
                    else None
                )
                if MainWindow._suppress_confirmed_manual_close_recovery(
                    self,
                    request,
                    process,
                ):
                    continue
                # Claiming also drops a queued failure when the companion has
                # become healthy again before this UI callback runs.
                if self._companion_watchdog.claim_recovery(request):
                    self._recover_owned_companion(request)
        finally:
            self._schedule_companion_watchdog_poll()

    def _recover_owned_companion(self, request: RecoveryRequest) -> None:
        """Restart one unresponsive child without blocking the probe thread."""

        process_attribute = _COMPANION_PROCESS_ATTRIBUTES.get(request.name)
        if process_attribute is None:
            self._companion_watchdog.acknowledge_recovery(request, succeeded=False)
            return

        process = getattr(self, process_attribute, None)
        if MainWindow._suppress_confirmed_manual_close_recovery(self, request, process):
            return
        if not self._companion_watchdog.matches_process(request, process):
            # A stale request must never terminate a replacement process.
            _log_runtime_message(
                "companion_watchdog_recovery",
                f"ignored stale ownership for {request.name}",
            )
            self._companion_watchdog.acknowledge_recovery(request, succeeded=False)
            return
        _log_runtime_message(
            "companion_watchdog_recovery",
            (
                f"app={request.name}\nreason={request.reason}\n"
                f"detail={request.detail}\nfailures={request.consecutive_failures}"
            ),
        )
        self._set_capture_status(
            f"{request.name} 응답 문제를 감지해 해당 프로그램만 자동 복구하고 있습니다…"
        )

        if request.name == "HYTrans":
            # Terminate our Popen tree; its former port may belong to another app.
            if _is_process_alive(process):
                try:
                    _terminate_process_tree(process)
                except Exception as exc:
                    _log_runtime_error("companion_watchdog_terminate", exc)
            if _is_process_alive(process):
                self._companion_watchdog.acknowledge_recovery(request, succeeded=False)
                return
            if getattr(self, process_attribute, None) is process:
                setattr(self, process_attribute, None)
            if self._launch_hytrans(restarted=True, notify=False):
                # The new Popen registration makes this request stale.
                return
            self._companion_watchdog.acknowledge_recovery(request, succeeded=False)
            return

        if _is_process_alive(process):
            try:
                _terminate_process_tree(process)
            except Exception as exc:
                _log_runtime_error("companion_watchdog_terminate", exc)

        # Do not overwrite a handle which taskkill could not actually stop.
        if _is_process_alive(process):
            self._companion_watchdog.acknowledge_recovery(request, succeeded=False)
            return
        if getattr(self, process_attribute, None) is process:
            setattr(self, process_attribute, None)

        restart = {
            "MekiAudioCapture": self._launch_audio_capture,
            "MekiScript": self._launch_script,
            "MekiOverlayer": self._launch_overlayer,
        }[request.name]
        if restart(notify=False):
            # The new Popen registration makes this request stale.
            return
        self._companion_watchdog.acknowledge_recovery(request, succeeded=False)

    def _script_config_payload(self) -> dict:
        return {
            "topmost": self.settings.script_always_on_top,
            "bg_color": self.settings.script_bg_color,
            "opacity": self.settings.script_bg_opacity,
            "original_color": self.settings.script_original_text_color,
            "original_size": self.settings.script_original_text_size,
            "original_font": self.settings.script_original_text_font,
            "translated_color": self.settings.script_translated_text_color,
            "translated_size": self.settings.script_translated_text_size,
            "translated_font": self.settings.script_translated_text_font,
            "debug_log": self.settings.debug_logging,
        }

    def _audio_capture_config_payload(self) -> dict:
        return {
            "sttModel": self.settings.audio_stt_model,
            "precision": self.settings.audio_stt_precision,
            "preset": self.settings.audio_chunk_preset,
            "scriptUrl": self._script_base_url(),
            "hytransUrl": self._hytrans_base_url(),
            "debugLog": self.settings.debug_logging,
        }

    def _send_hytrans_logging_config(self, log_errors: bool = True) -> bool:
        try:
            _json_request(
                f"{self._hytrans_base_url()}/logging/config",
                {"debugLog": self.settings.debug_logging},
                timeout=2 if log_errors else 0.25,
                method="POST",
            )
            return True
        except Exception as exc:
            if log_errors:
                _log_runtime_error("send_hytrans_logging_config", exc)
            return False

    def _send_script_config(self, log_errors: bool = True) -> bool:
        try:
            _json_request(
                f"{self._script_base_url()}/config",
                self._script_config_payload(),
                timeout=2 if log_errors else 0.25,
                method="POST",
            )
            return True
        except Exception as exc:
            if log_errors:
                _log_runtime_error("send_script_config", exc)
            return False

    def _send_audio_capture_config(self, log_errors: bool = True) -> bool:
        try:
            _json_request(
                f"{self._audio_capture_base_url()}/config",
                self._audio_capture_config_payload(),
                timeout=2 if log_errors else 0.25,
                method="POST",
            )
            return True
        except Exception as exc:
            if log_errors:
                _log_runtime_error("send_audio_capture_config", exc)
            return False

    def _on_start_script(self) -> None:
        try:
            _probe_service("MekiScript", self._script_base_url(), timeout=1)
            self._send_script_config()
            messagebox.showinfo(
                "MekiCopy",
                "MekiScript가 이미 실행 중입니다. 현재 설정을 적용했습니다.",
                parent=self,
            )
            return
        except Exception:
            if self._warn_if_port_busy("MekiScript", self.settings.script_port):
                return
        if self._tracked_process_is_starting("MekiScript", self.script_process):
            return

        self._launch_script()

    def _launch_script(self, *, notify: bool = True) -> bool:
        command = _find_companion_executable("MekiScript", "meki_script.py")
        if not command:
            detail = "MekiScript 실행 파일을 찾을 수 없습니다."
            _log_runtime_message("start_script", detail)
            if notify:
                messagebox.showerror("MekiCopy", detail, parent=self)
            return False
        manual_close_signal = self._create_manual_close_signal("MekiScript")
        cfg = self._script_config_payload()
        command += [
            "--port", str(self.settings.script_port),
            "--topmost", "1" if cfg["topmost"] else "0",
            "--bg-color", str(cfg["bg_color"]),
            "--opacity", str(cfg["opacity"]),
            "--original-color", str(cfg["original_color"]),
            "--original-size", str(cfg["original_size"]),
            "--original-font", str(cfg["original_font"]),
            "--translated-color", str(cfg["translated_color"]),
            "--translated-size", str(cfg["translated_size"]),
            "--translated-font", str(cfg["translated_font"]),
        ]
        if manual_close_signal is not None:
            command += manual_close_signal_arguments(manual_close_signal)
        if self.settings.debug_logging:
            command.append("--debug-log")
        try:
            self.script_process = subprocess.Popen(command, cwd=_get_app_dir())
            _log_runtime_message("start_script", " ".join(command))
            self._watch_started_process(
                "MekiScript",
                self.script_process,
                self._script_base_url(),
                notify=notify,
            )
            self._track_owned_companion(
                "MekiScript",
                self.script_process,
                self._script_base_url(),
                manual_close_signal=manual_close_signal,
            )
            if notify:
                messagebox.showinfo("MekiCopy", "MekiScript를 실행했습니다.", parent=self)
            return True
        except Exception as exc:
            discard_manual_close_signal(manual_close_signal)
            _log_runtime_error("start_script", exc)
            if notify:
                messagebox.showerror("MekiCopy", f"MekiScript 실행 실패:\n{exc}", parent=self)
            return False

    def _on_start_audio_capture(self) -> None:
        try:
            _probe_service("MekiAudioCapture", self._audio_capture_base_url(), timeout=1)
            self._send_audio_capture_config()
            messagebox.showinfo(
                "MekiCopy",
                "MekiAudioCapture가 이미 실행 중입니다. 현재 설정을 적용했습니다.",
                parent=self,
            )
            return
        except Exception:
            if self._warn_if_port_busy(
                "MekiAudioCapture",
                self.settings.audio_capture_port,
            ):
                return
        if self._tracked_process_is_starting(
            "MekiAudioCapture",
            self.audio_capture_process,
        ):
            return

        self._launch_audio_capture()

    def _launch_audio_capture(self, *, notify: bool = True) -> bool:
        command = _find_companion_executable("MekiAudioCapture", "meki_audio_capture.py")
        if not command:
            detail = "MekiAudioCapture 실행 파일을 찾을 수 없습니다."
            _log_runtime_message("start_audio_capture", detail)
            if notify:
                messagebox.showerror("MekiCopy", detail, parent=self)
            return False
        manual_close_signal = self._create_manual_close_signal("MekiAudioCapture")
        cfg = self._audio_capture_config_payload()
        command += [
            "--port", str(self.settings.audio_capture_port),
            "--stt-model", str(cfg["sttModel"]),
            "--precision", str(cfg["precision"]),
            "--preset", str(cfg["preset"]),
            "--script-url", str(cfg["scriptUrl"]),
            "--hytrans-url", str(cfg["hytransUrl"]),
        ]
        if manual_close_signal is not None:
            command += manual_close_signal_arguments(manual_close_signal)
        if self.settings.debug_logging:
            command.append("--debug-log")
        try:
            self.audio_capture_process = subprocess.Popen(command, cwd=_get_app_dir())
            _log_runtime_message("start_audio_capture", " ".join(command))
            self._watch_started_process(
                "MekiAudioCapture",
                self.audio_capture_process,
                self._audio_capture_base_url(),
                notify=notify,
            )
            self._track_owned_companion(
                "MekiAudioCapture",
                self.audio_capture_process,
                self._audio_capture_base_url(),
                manual_close_signal=manual_close_signal,
            )
            if notify:
                messagebox.showinfo("MekiCopy", "MekiAudioCapture를 실행했습니다.", parent=self)
            return True
        except Exception as exc:
            discard_manual_close_signal(manual_close_signal)
            _log_runtime_error("start_audio_capture", exc)
            if notify:
                messagebox.showerror("MekiCopy", f"MekiAudioCapture 실행 실패:\n{exc}", parent=self)
            return False

    def _on_test_audio_connection(self) -> None:
        services = (
            ("MekiAudioCapture", self._audio_capture_base_url()),
            ("HYTrans", self._hytrans_base_url()),
            ("MekiScript", self._script_base_url()),
        )
        successes: list[str] = []
        failures: list[str] = []
        for name, base_url in services:
            try:
                state = _probe_service(name, base_url)
                successes.append(f"{name}: {state}")
            except Exception as exc:
                failures.append(f"{name}: 연결 실패 ({exc})")
        if failures:
            messagebox.showerror(
                "MekiCopy",
                "\n".join([*successes, *failures]),
                parent=self,
            )
        else:
            self._send_script_config(log_errors=False)
            self._send_audio_capture_config(log_errors=False)
            messagebox.showinfo(
                "MekiCopy",
                "세 도구의 연결 상태가 정상입니다.\n" + "\n".join(successes),
                parent=self,
            )

    def _overlayer_config_payload(self) -> dict:
        return overlay_config_payload(self.settings)

    def _send_overlayer_config(self, log_errors: bool = True) -> bool:
        try:
            _json_request(
                f"{self._overlayer_base_url()}/config",
                self._overlayer_config_payload(),
                timeout=2 if log_errors else 0.25,
                method="POST",
            )
            return True
        except Exception as exc:
            if log_errors:
                _log_runtime_error("send_overlayer_config", exc)
            return False

    def _on_hdtrans_settings(self) -> None:
        from hdtrans_settings_window import HdTransSettingsWindow
        if self.hdtrans_settings_window is not None:
            self._bring_forward(self.hdtrans_settings_window)
            return
        self.hdtrans_settings_window = HdTransSettingsWindow(self, main_window=self)
        self.hdtrans_settings_window.show()

    def _on_start_hytrans(self) -> bool:
        if self._hytrans_restart_after_id is not None:
            messagebox.showinfo(
                "MekiCopy",
                "변경된 번역 설정으로 HYTrans를 재시작하고 있습니다.",
                parent=self,
            )
            return True
        try:
            health = _json_request(f"{self._hytrans_base_url()}/health", timeout=1)
            # An ERROR worker can recover via /worker/reopen; validate server identity.
            if health.get("ok") is not True or health.get("app") != "HYTrans":
                _validate_service_health("HYTrans", health)
                raise RuntimeError("HYTrans service identity could not be verified")
            expected_profile = get_model_profile(self.settings.hytrans_model_id)
            backend = normalize_backend(self.settings.hytrans_backend)
            expected_model = (
                expected_profile.model_id if backend == "local" else
                "Browser Translator API" if backend == "translator_api" else
                "hdTrans" if backend == "hdtrans" else
                load_api_settings().profiles[backend].model
            )
            if (
                str(health.get("backend") or "local") != backend
                or health.get("model") != expected_model
                or (backend == "local" and health.get("dtype") != expected_profile.dtype)
            ):
                return self._begin_hytrans_restart(
                    self.settings.hytrans_port,
                    "running-profile-mismatch",
                )
            self._send_hytrans_logging_config(log_errors=False)
            try:
                ready = _json_request(f"{self._hytrans_base_url()}/ready", timeout=1)
            except Exception as exc:
                _log_runtime_error("check_hytrans_ready", exc)
                messagebox.showerror(
                    "MekiCopy",
                    f"HYTrans 준비 상태를 확인하지 못했습니다:\n{exc}",
                    parent=self,
                )
                return False
            if backend == "hdtrans" and not ready.get("ready"):
                self._on_hdtrans_settings()
                return True
            if backend in PROVIDERS and not ready.get("ready"):
                messagebox.showerror(
                    "MekiCopy",
                    f"HYTrans API 설정을 확인해 주세요:\n{ready.get('error') or ready.get('state') or '준비되지 않음'}",
                    parent=self,
                )
                return False
            if backend in {"local", "translator_api"} and not (ready.get("workerConnected") or ready.get("ready")):
                try:
                    _json_request(
                        f"{self._hytrans_base_url()}/worker/reopen",
                        {},
                        timeout=3,
                        method="POST",
                    )
                except Exception as exc:
                    _log_runtime_error("restart_hytrans_worker", exc)
                    messagebox.showerror(
                        "MekiCopy",
                        f"HYTrans 번역 worker를 다시 시작하지 못했습니다:\n{exc}",
                        parent=self,
                    )
                    return False
                messagebox.showinfo(
                    "MekiCopy",
                    "HYTrans 비공개 번역 worker를 다시 시작했습니다.",
                    parent=self,
                )
                return True
            messagebox.showinfo("MekiCopy", "HYTrans 서버가 이미 실행 중입니다.", parent=self)
            return True
        except Exception:
            if self._tracked_process_is_starting("HYTrans", self.hytrans_process):
                return True
            if self._warn_if_port_busy("HYTrans", self.settings.hytrans_port):
                return False

        return self._launch_hytrans()

    def _launch_hytrans(self, *, restarted: bool = False, notify: bool = True) -> bool:
        already_starting = (
            self._tracked_process_is_starting("HYTrans", self.hytrans_process)
            if notify
            else _is_process_alive(self.hytrans_process)
        )
        if already_starting:
            # A launched process may still be loading; callers can wait for /ready.
            return True
        command = _find_companion_executable("HYTrans", "hytrans_main.py")
        if not command:
            detail = "HYTrans 실행 파일을 찾을 수 없습니다."
            _log_runtime_message("start_hytrans", detail)
            if notify:
                messagebox.showerror("MekiCopy", detail, parent=self)
            return False

        backend = normalize_backend(self.settings.hytrans_backend)
        profile = get_model_profile(self.settings.hytrans_model_id)
        command += [
            "--port",
            str(self.settings.hytrans_port),
            "--overlay-url",
            self._overlayer_show_url(),
            "--model",
            profile.key,
            "--backend",
            backend,
            "--api-config",
            str(api_settings_path()),
        ]
        if self.settings.debug_logging:
            command.append("--debug-log")
        try:
            self.hytrans_process = subprocess.Popen(
                command,
                cwd=_get_app_dir(),
                startupinfo=_startupinfo_for_background(),
            )
            _log_runtime_message("start_hytrans", " ".join(command))
            self._watch_started_process(
                "HYTrans",
                self.hytrans_process,
                self._hytrans_base_url(),
                notify=notify,
            )
            self._track_owned_companion(
                "HYTrans",
                self.hytrans_process,
                self._hytrans_base_url(),
            )
            action = "재시작했습니다" if restarted else "실행했습니다"
            if notify:
                engine = profile.display_label if backend == "local" else BACKEND_LABELS[backend]
                messagebox.showinfo(
                    "MekiCopy",
                    f"HYTrans 서버를 {action}.\n번역 방식: {engine}",
                    parent=self,
                )
            return True
        except Exception as exc:
            _log_runtime_error("start_hytrans", exc)
            if notify:
                messagebox.showerror("MekiCopy", f"HYTrans 실행 실패:\n{exc}", parent=self)
            return False

    def _begin_hytrans_restart(
        self,
        old_port: int,
        reason: str,
        *,
        notify: bool = True,
    ) -> bool:
        if self._hytrans_restart_after_id is not None:
            return True

        old_base_url = f"http://127.0.0.1:{old_port}"
        tracked_process = (
            self.hytrans_process if _is_process_alive(self.hytrans_process) else None
        )
        service_running = False
        shutdown_token = ""
        try:
            health = _json_request(f"{old_base_url}/health", timeout=0.75)
            # An ERROR worker still supports shutdown; validate server identity.
            service_running = (
                health.get("ok") is True
                and health.get("app") == "HYTrans"
            )
            shutdown_token = str(health.get("controlToken") or "")
        except Exception:
            service_running = False

        if not service_running and tracked_process is None:
            return False

        # Suspend recovery while settings intentionally replace this child.
        watchdog = getattr(self, "_companion_watchdog", None)
        if watchdog is not None:
            watchdog.untrack("HYTrans")

        if service_running:
            try:
                _json_request(
                    f"{old_base_url}/shutdown",
                    {},
                    timeout=2,
                    method="POST",
                    headers=(
                        {"X-HYTrans-Shutdown-Token": shutdown_token}
                        if shutdown_token
                        else None
                    ),
                )
            except Exception as exc:
                _log_runtime_error("restart_hytrans_shutdown", exc)
                if tracked_process is None:
                    if notify:
                        messagebox.showerror(
                            "MekiCopy",
                            (
                                "실행 중인 HYTrans에 종료를 요청하지 못해 자동 재시작할 수 "
                                "없습니다. HYTrans를 닫은 뒤 다시 실행해 주세요."
                            ),
                            parent=self,
                        )
                    return False
                _terminate_process_tree(tracked_process)
        elif tracked_process is not None:
            _terminate_process_tree(tracked_process)

        _log_runtime_message(
            "restart_hytrans",
            f"reason={reason}, old_port={old_port}, model={self.settings.hytrans_model_id}",
        )
        self._hytrans_restart_process = tracked_process
        self._hytrans_restart_old_port = old_port
        self._hytrans_restart_attempts = 0
        self._hytrans_restart_notify = notify
        self._hytrans_restart_after_id = self.after(100, self._poll_hytrans_restart)
        return True

    def _poll_hytrans_restart(self) -> None:
        self._hytrans_restart_after_id = None
        if self._closing:
            return

        self._hytrans_restart_attempts += 1
        process = self._hytrans_restart_process
        process_alive = _is_process_alive(process)
        old_port = self._hytrans_restart_old_port
        port_open = bool(old_port and _is_loopback_port_open(old_port))

        if not process_alive and not port_open:
            if process is self.hytrans_process:
                self.hytrans_process = None
            self._hytrans_restart_process = None
            self._hytrans_restart_old_port = None
            self._hytrans_restart_attempts = 0
            self._launch_hytrans(
                restarted=True,
                notify=getattr(self, "_hytrans_restart_notify", True),
            )
            return

        # Give graceful shutdown time to close the private worker and Uvicorn. If a
        # process owned by this MekiCopy instance hangs, fall back to its tree.
        if self._hytrans_restart_attempts == 12 and process_alive:
            try:
                _terminate_process_tree(process)
            except Exception as exc:
                _log_runtime_error("restart_hytrans_force_stop", exc)

        if self._hytrans_restart_attempts >= 40:
            self._hytrans_restart_process = None
            self._hytrans_restart_old_port = None
            self._hytrans_restart_attempts = 0
            if getattr(self, "_hytrans_restart_notify", True):
                messagebox.showerror(
                    "MekiCopy",
                    "기존 HYTrans가 종료되지 않아 재시작하지 못했습니다.",
                    parent=self,
                )
            else:
                _log_runtime_message(
                    "restart_hytrans",
                    "watchdog recovery could not stop the previous HYTrans process",
                )
            return

        self._hytrans_restart_after_id = self.after(250, self._poll_hytrans_restart)

    def _on_start_overlayer(self) -> None:
        try:
            _probe_service("MekiOverlayer", self._overlayer_base_url(), timeout=1)
            if not self._send_overlayer_config():
                messagebox.showerror(
                    "MekiCopy",
                    "MekiOverlayer에 현재 설정을 적용하지 못했습니다. 연결 상태를 확인해 주세요.",
                    parent=self,
                )
                return
            messagebox.showinfo(
                "MekiCopy",
                "MekiOverlayer가 이미 실행 중입니다. 현재 설정을 적용했습니다.",
                parent=self,
            )
            return
        except Exception:
            if self._warn_if_port_busy(
                "MekiOverlayer",
                self.settings.overlayer_port,
            ):
                return

        if self._tracked_process_is_starting("MekiOverlayer", self.overlayer_process):
            return

        self._launch_overlayer()

    def _launch_overlayer(self, *, notify: bool = True) -> bool:
        command = _find_companion_executable("MekiOverlayer", "meki_overlayer.py")
        if not command:
            detail = "MekiOverlayer 실행 파일을 찾을 수 없습니다."
            _log_runtime_message("start_overlayer", detail)
            if notify:
                messagebox.showerror("MekiCopy", detail, parent=self)
            return False

        manual_close_signal = self._create_manual_close_signal("MekiOverlayer")
        cfg = self._overlayer_config_payload()
        command += [
            "--port",
            str(self.settings.overlayer_port),
            "--topmost",
            "1" if cfg["topmost"] else "0",
            "--hide-titlebar",
            "1" if cfg["hide_titlebar"] else "0",
            "--fixed-size",
            "1" if cfg["fixed_size"] else "0",
            "--exclude-from-capture",
            "1" if cfg["exclude_from_capture"] else "0",
            "--bg-color",
            str(cfg["bg_color"]),
            "--opacity",
            str(cfg["opacity"]),
            "--text-color",
            str(cfg["text_color"]),
            "--text-size",
            str(cfg["text_size"]),
            "--text-font",
            str(cfg["text_font"]),
        ]
        if manual_close_signal is not None:
            command += manual_close_signal_arguments(manual_close_signal)
        if self.settings.debug_logging:
            command.append("--debug-log")
        try:
            self.overlayer_process = subprocess.Popen(
                command,
                cwd=_get_app_dir(),
                startupinfo=None,
            )
            _log_runtime_message("start_overlayer", " ".join(command))
            self._watch_started_process(
                "MekiOverlayer",
                self.overlayer_process,
                self._overlayer_base_url(),
                notify=notify,
            )
            self._track_owned_companion(
                "MekiOverlayer",
                self.overlayer_process,
                self._overlayer_base_url(),
                manual_close_signal=manual_close_signal,
            )
            if notify:
                messagebox.showinfo("MekiCopy", "MekiOverlayer를 실행했습니다.", parent=self)
            return True
        except Exception as exc:
            discard_manual_close_signal(manual_close_signal)
            _log_runtime_error("start_overlayer", exc)
            if notify:
                messagebox.showerror("MekiCopy", f"MekiOverlayer 실행 실패:\n{exc}", parent=self)
            return False

    def _on_test_overlay_connection(self, parent: QWidget | None = None) -> None:
        owner = parent or self
        failures: list[str] = []
        try:
            hytrans_state = _probe_service("HYTrans", self._hytrans_base_url())
        except Exception as exc:
            failures.append(f"HYTrans 연결 실패: {exc}")
        try:
            _probe_service("MekiOverlayer", self._overlayer_base_url())
        except Exception as exc:
            failures.append(f"MekiOverlayer 연결 실패: {exc}")

        if failures:
            messagebox.showerror("MekiCopy", "\n".join(failures), parent=owner)
            return

        if not self._send_overlayer_config():
            messagebox.showerror(
                "MekiCopy",
                "MekiOverlayer 설정을 적용하지 못해 연결 테스트를 중단했습니다.",
                parent=owner,
            )
            return
        try:
            _json_request(
                f"{self._hytrans_base_url()}/overlay-test",
                {"text": "MekiCopy 연결 테스트", "overlayUrl": self._overlayer_show_url()},
                timeout=5,
                method="POST",
            )
            messagebox.showinfo(
                "MekiCopy",
                f"HYTrans -> MekiOverlayer 표시 흐름이 정상입니다.\n번역 모델: {hytrans_state}",
                parent=owner,
            )
        except Exception as exc:
            _log_runtime_error("test_overlay_connection", exc)
            messagebox.showerror("MekiCopy", f"연결 테스트 실패:\n{exc}", parent=owner)

    def _on_toggle_realtime_ocr(self) -> None:
        if self._realtime_ocr.active:
            self._realtime_requested = False
            self._sync_realtime_ocr()
            self._set_capture_status("실시간 OCR을 중지했습니다.")
            return
        reason = self._realtime_block_reason()
        if reason:
            self._set_capture_status(f"실시간 OCR 시작 불가: {reason}")
            return
        self._realtime_requested = True
        self._sync_realtime_ocr()

    def _realtime_block_reason(self, *, include_services: bool = True) -> str:
        if self._closing:
            return "프로그램 종료 중입니다."
        if getattr(self, "_workflow_mode", "screen") != "screen":
            return "화면 번역을 선택하세요."
        if self.active_region is None:
            return "먼저 OCR 영역을 확정하세요."
        if not self.settings.realtime_ocr_enabled:
            return "설정의 실시간/수동 번역에서 실시간 번역 모드 사용을 켜세요."
        if getattr(self, "_hytrans_restart_after_id", None) is not None:
            return "HYTrans 재시작이 끝날 때까지 기다리세요."
        if include_services:
            ports = (self.settings.hytrans_port, self.settings.overlayer_port)
            if (self._realtime_service_ports != ports
                    or self._realtime_service_processes != self._realtime_process_snapshot()
                    or time.monotonic() - self._realtime_service_checked_at > 5.0):
                return "HYTrans와 MekiOverlayer 연결을 확인 중입니다."
            missing = [name for name in ("HYTrans", "MekiOverlayer")
                       if not self._realtime_service_status.get(name)]
            if missing:
                return f"{' / '.join(missing)}를 실행하고 번역 준비를 마치세요."
        return ""

    def _realtime_process_snapshot(self):
        return tuple((process, _is_process_alive(process)) for process in (
            getattr(self, "hytrans_process", None), getattr(self, "overlayer_process", None),
        ))

    def _refresh_realtime_services(self) -> None:
        """Probe both owned and external services without waiting on the GUI thread."""
        self._sync_realtime_ocr()
        if self._realtime_block_reason(include_services=False):
            return
        now = time.monotonic()
        if now - self._realtime_service_probe_at < 1.0:
            return
        ports = (self.settings.hytrans_port, self.settings.overlayer_port)
        config_fingerprint = (self.settings.hytrans_backend, self.settings.hytrans_model_id,
                              self._hytrans_config_fingerprint)
        processes = self._realtime_process_snapshot()

        def probe():
            status = {}
            for name, port in zip(("HYTrans", "MekiOverlayer"), ports):
                try:
                    _probe_service(name, f"http://127.0.0.1:{port}", timeout=0.5)
                    status[name] = True
                except Exception:
                    status[name] = False
            return status

        def complete(status):
            if (self._closing or ports != (self.settings.hytrans_port, self.settings.overlayer_port)
                    or processes != self._realtime_process_snapshot()
                    or config_fingerprint != (self.settings.hytrans_backend, self.settings.hytrans_model_id,
                                               self._hytrans_config_fingerprint)):
                return
            self._realtime_service_ports = ports
            self._realtime_service_processes = processes
            self._realtime_service_status = status
            self._realtime_service_checked_at = time.monotonic()
            self._sync_realtime_ocr()

        if self._task_runner.submit(
            "realtime_services", probe, on_success=complete,
            on_error=lambda error: complete({}),
        ):
            self._realtime_service_probe_at = now

    def _sync_realtime_ocr(self) -> None:
        realtime = getattr(self, "_realtime_ocr", None)
        if realtime is None:
            return
        region = self.active_region
        target = None if region is None else (region.left, region.top, region.width, region.height)
        reason = self._realtime_block_reason()
        was_active = realtime.active
        if reason:
            self._realtime_requested = False
        realtime.configure(
            enabled=self._realtime_requested and not reason,
            interval=self.settings.realtime_ocr_interval,
            translate=True,
            fingerprint=(target, repr(self.settings), getattr(self, "_hytrans_config_fingerprint", None)),
        )
        self._apply_overlay_mode_ui()
        if was_active and reason:
            self._set_capture_status(f"실시간 OCR 중지: {reason}")

    def _on_realtime_state_changed(self) -> None:
        if not self._closing:
            self._apply_overlay_mode_ui()
        self._publish_realtime_state()

    def _publish_realtime_state(self) -> None:
        if self._realtime_state_after_id is not None:
            try:
                self.after_cancel(self._realtime_state_after_id)
            except RuntimeError:
                pass
            self._realtime_state_after_id = None
        realtime = self._realtime_ocr
        active = realtime.active and not self._closing
        mekicopy_settings._write_state_text(REALTIME_STATE_FILENAME, json.dumps({
            "version": 1, "pid": os.getpid(), "active": active,
            "translation": realtime.translate, "updatedAt": time.time(),
        }))
        if active:
            self._realtime_state_after_id = self.after(1000, self._publish_realtime_state)

    def _prepare_realtime_operation(self, session, previous):
        # Snapshot UI-owned state before handing any work to the worker.
        region, settings = self.active_region, self.settings
        monitor, ratio = self.active_monitor, self.active_region_ratio
        reason = self._realtime_block_reason()
        if reason:
            self._realtime_requested = False
            self._sync_realtime_ocr()
            raise RealtimeUnavailableError(reason)
        overlayer_config = overlay_config_payload(settings)

        def translate(text: str) -> dict:
            return request_translation_and_show(
                text,
                hytrans_url=f"http://127.0.0.1:{settings.hytrans_port}",
                overlayer_url=f"http://127.0.0.1:{settings.overlayer_port}",
                overlayer_config=overlayer_config,
                request_id=f"{session.owner}:{time.monotonic_ns()}",
                is_cancelled=session.cancelled.is_set,
                wait_for_display=True,
                context_owner=session.owner,
            )

        def operation():
            # Check again immediately before capture, including externally
            # launched services and exits since the last background probe.
            try:
                _probe_service("HYTrans", f"http://127.0.0.1:{settings.hytrans_port}", timeout=0.5)
                _probe_service("MekiOverlayer", f"http://127.0.0.1:{settings.overlayer_port}", timeout=0.5)
            except Exception as exc:
                raise RealtimeUnavailableError(str(exc)) from exc
            if session.cancelled.is_set():
                return perform_cycle(lambda: "", session=session, previous=previous)
            target, resolved_monitor, resolved_ratio, _ = get_capture_manager().resolve_region(
                region, previous_monitor=monitor, ratio=ratio,
            )

            def recognize() -> str:
                image = capture_ocr_image(target.left, target.top, target.width, target.height)
                return recognize_image(image, is_cancelled=session.cancelled.is_set)

            result = perform_cycle(
                recognize, session=session,
                previous=previous if _regions_equal(target, region) else None,
                translate=translate,
            )
            return replace(result, capture_target=(target, resolved_monitor, resolved_ratio))

        return operation

    def _on_realtime_result(self, result) -> bool:
        if self._closing:
            return False
        if result.capture_target is not None:
            region, monitor, ratio = result.capture_target
            if not _regions_equal(region, self.active_region):
                self.active_region = region
                self.active_monitor, self.active_region_ratio = monitor, ratio
                self.draft_region = replace(region)
                save_detached_region(region)
                self._update_status()
        if result.displayed_at is None:
            self._set_capture_status("실시간 OCR: 번역 표시를 확인하지 못했습니다.")
            return False
        self._set_capture_status("실시간 OCR: 새 번역 표시 완료. 다음 인식을 기다립니다.")
        return True

    def _on_realtime_error(self, error: Exception) -> None:
        _log_runtime_error("realtime_ocr", error)
        if isinstance(error, RealtimeUnavailableError):
            self._realtime_requested = False
            self._realtime_service_status = {}
            self._sync_realtime_ocr()
            self._set_capture_status(f"실시간 OCR 중지: {error}")
            return
        self._set_capture_status(f"실시간 OCR 실패 (다음 주기에 재시도): {error}")

    def _on_ocr_copy(
        self,
        source_button: RoundedButton | None = None,
        *,
        on_finished: Callable[[], None] | None = None,
    ) -> bool:
        realtime = getattr(self, "_realtime_ocr", None)
        if (self.settings.realtime_ocr_enabled
                or getattr(self, "_workflow_mode", "screen") != "screen"
                or (realtime is not None and realtime.translation_locked)
                or self._task_runner.is_running("main_ocr")):
            return False
        if not self.active_region:
            messagebox.showerror("MekiCopy", "설정된 영역이 없습니다.", parent=self)
            return False
        if not self._prepare_active_region_for_capture():
            return False
        try:
            image = capture_ocr_image(
                self.active_region.left,
                self.active_region.top,
                self.active_region.width,
                self.active_region.height,
            )
        except OcrError as exc:
            self._set_capture_status_from_last_result()
            messagebox.showwarning("MekiCopy", str(exc), parent=self)
            return False
        self._set_capture_status_from_last_result()
        return self._start_ocr_task(
            image,
            source_button=source_button or self.ocr_button,
            simple_feedback=self.settings.simple_copy_complete,
            on_finished=on_finished,
        )

    def _start_ocr_task(
        self,
        image: Image.Image,
        *,
        source_button: RoundedButton,
        simple_feedback: bool,
        on_finished: Callable[[], None] | None = None,
    ) -> bool:
        realtime = getattr(self, "_realtime_ocr", None)
        if realtime is not None and realtime.translation_locked:
            return False
        settings = self.settings
        hytrans_url = f"http://127.0.0.1:{settings.hytrans_port}"
        overlayer_url = f"http://127.0.0.1:{settings.overlayer_port}"
        overlayer_config = overlay_config_payload(settings)

        def operation() -> str:
            text = recognize_image(image)
            if text.strip():
                request_translation_and_show(
                    text,
                    hytrans_url=hytrans_url,
                    overlayer_url=overlayer_url,
                    overlayer_config=overlayer_config,
                )
            return text

        def on_success(text: str) -> None:
            try:
                if not text.strip():
                    messagebox.showwarning("MekiCopy", "인식된 텍스트가 없습니다.", parent=self)
                    return
                if simple_feedback:
                    self._show_copy_feedback(source_button)
                else:
                    messagebox.showinfo(
                        "MekiCopy", "번역 결과를 MekiOverlayer에 표시했습니다.", parent=self,
                    )
            finally:
                if on_finished is not None:
                    on_finished()

        def on_error(exc: Exception) -> None:
            try:
                _log_runtime_error("ocr_translate_and_show", exc)
                detail = str(exc)
                if isinstance(exc, urllib.error.HTTPError):
                    try:
                        detail = f"HTTP {exc.code}\n{exc.read().decode('utf-8', errors='replace')}"
                    except Exception:
                        detail = f"HTTP {exc.code}"
                messagebox.showerror("MekiCopy", f"번역 및 표시 실패:\n{detail}", parent=self)
            finally:
                if on_finished is not None:
                    on_finished()

        return self._task_runner.submit(
            "main_ocr",
            operation,
            on_success=on_success,
            on_error=on_error,
        )

    def _show_copy_feedback(self, button: RoundedButton) -> None:
        previous_timer = getattr(button, "_feedback_timer", None)
        if previous_timer is not None:
            self.after_cancel(previous_timer)
        else:
            button._feedback_font = button.font()
        button.set_variant("success")
        button.setText("✓")
        button.setFont(make_font(("Segoe UI Symbol", 58, "bold")))

        def restore() -> None:
            button._feedback_timer = None
            if not self._closing:
                button.setFont(button._feedback_font)
                button.set_variant("primary")
                button.setText(self.ocr_action_label())

        button._feedback_timer = self.after(1000, restore)

    def apply_settings(
        self, settings: AppSettings, persist: bool, *, api_settings: ApiSettings | None = None
    ) -> bool | None:
        previous = self.settings
        backend = normalize_backend(settings.hytrans_backend)
        previous_api_settings = load_api_settings()
        selected_api_settings = api_settings if api_settings is not None else previous_api_settings
        if persist and backend in PROVIDERS:
            validation_error = validate_provider_settings(selected_api_settings.profiles[backend], backend)
            if validation_error:
                messagebox.showerror("MekiCopy", validation_error, parent=self.settings_window or self)
                return None
        current_fingerprint = fingerprint(selected_api_settings, backend)
        restart_reason_parts: list[str] = []
        if normalize_backend(previous.hytrans_backend) != backend:
            restart_reason_parts.append("backend")
        if getattr(self, "_hytrans_config_fingerprint", current_fingerprint) != current_fingerprint:
            restart_reason_parts.append("api-config")
        if previous.hytrans_model_id != settings.hytrans_model_id:
            restart_reason_parts.append("model")
        if previous.hytrans_port != settings.hytrans_port:
            restart_reason_parts.append("port")

        if (
            persist
            and previous.hytrans_port != settings.hytrans_port
            and _is_loopback_port_open(settings.hytrans_port)
        ):
            messagebox.showerror(
                "MekiCopy",
                (
                    f"새 HYTrans 포트가 이미 사용 중입니다: {settings.hytrans_port}\n"
                    "실행 중인 프로그램을 종료하거나 다른 포트를 선택하세요."
                ),
                parent=self.settings_window or self,
            )
            return None

        hotkey_changed = self._global_hotkey_configuration(
            previous
        ) != self._global_hotkey_configuration(settings)
        was_hotkey_paused = self._global_hotkey_paused
        if persist and was_hotkey_paused:
            # Validate registration of the new key before saving it.
            self._global_hotkey_paused = False
        hotkey_configured, hotkey_error = self._configure_global_hotkey(settings)
        if not hotkey_configured:
            if persist:
                if was_hotkey_paused:
                    self.pause_global_hotkey()
                messagebox.showerror(
                    "MekiCopy",
                    (
                        f"전역 단축키 '{settings.global_hotkey}'를 등록하지 못했습니다.\n"
                        f"{hotkey_error}"
                    ),
                    parent=self.settings_window or self,
                )
                return None
            _log_runtime_error("global_hotkey_startup", hotkey_error)

        if persist and not save_settings(settings):
            if hotkey_changed:
                restored, restore_error = self._configure_global_hotkey(
                    previous,
                    force=True,
                )
                if not restored:
                    _log_runtime_error("restore_global_hotkey", restore_error)
            if was_hotkey_paused:
                self.pause_global_hotkey()
            _log_runtime_error("save_settings", "settings file could not be written")
            messagebox.showerror(
                "MekiCopy",
                "설정 파일을 저장하지 못했습니다. 프로그램 폴더의 쓰기 권한을 확인해 주세요.",
                parent=self.settings_window or self,
            )
            return None

        if persist and api_settings is not None and not save_api_settings(api_settings):
            rolled_back = save_settings(previous)
            if hotkey_changed:
                restored, restore_error = self._configure_global_hotkey(previous, force=True)
                if not restored:
                    _log_runtime_error("restore_global_hotkey", restore_error)
            if was_hotkey_paused:
                self.pause_global_hotkey()
            detail = "API 번역 설정을 저장하지 못했습니다. 쓰기 권한을 확인해 주세요."
            if not rolled_back:
                detail += "\n기존 일반 설정 복원에도 실패했습니다. 저장 경로를 확인해 주세요."
            messagebox.showerror("MekiCopy", detail, parent=self.settings_window or self)
            return None

        self.settings = settings
        if restart_reason_parts or previous.overlayer_port != settings.overlayer_port:
            self._realtime_service_ports = None
        hdtrans_button = getattr(self, "hdtrans_settings_button", None)
        if hdtrans_button is not None:
            hdtrans_button.setEnabled(backend == "hdtrans")
        self._hytrans_config_fingerprint = current_fingerprint
        profile = selected_api_settings.profiles.get(backend)
        self._translation_status_api_model = profile.model.strip() if profile else None
        set_debug_enabled(self.settings.debug_logging)
        self._apply_main_topmost()
        if self.subtitle_window is not None:
            self.subtitle_window.refresh_settings(self.settings)
        self._apply_overlay_mode_ui()
        self._sync_realtime_ocr()
        self._send_overlayer_config(log_errors=False)
        self._send_script_config(log_errors=False)
        self._send_audio_capture_config(log_errors=False)
        self._send_hytrans_logging_config(log_errors=False)
        if persist and restart_reason_parts:
            restarting = self._begin_hytrans_restart(
                previous.hytrans_port,
                ",".join(restart_reason_parts),
            )
            if backend == "hdtrans":
                if not restarting and not _is_loopback_port_open(settings.hytrans_port):
                    self._launch_hytrans(notify=False)
                self._on_hdtrans_settings()
            return restarting
        return False

    def _apply_main_topmost(self) -> None:
        flag = Qt.WindowType.WindowStaysOnTopHint
        desired = self.settings.main_always_on_top
        if bool(self.windowFlags() & flag) != desired:
            was_visible = self.isVisible()
            self.setWindowFlag(flag, desired)
            if was_visible:
                self.show()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if (event.type() == QEvent.Type.WindowStateChange and self.isMinimized()
                and self.settings.minimize_to_tray and not self._closing
                and not self._restoring_from_tray):
            self.after(0, self._minimize_to_tray)

    def _minimize_to_tray(self) -> None:
        if self.isMinimized() and self.tray_icon.show():
            self.hide()

    def _restore_from_tray(self) -> None:
        self._restoring_from_tray = True
        try:
            self.tray_icon.hide()
            self._apply_main_topmost()
            self._bring_forward(self)
        finally:
            self.after(100, lambda: setattr(self, "_restoring_from_tray", False))

    def closeEvent(self, event) -> None:
        if self._closing or self._on_close():
            super().closeEvent(event)
        else:
            event.ignore()

    def _on_close(self) -> bool:
        subtitle_window = self.subtitle_window
        if (
            subtitle_window is not None
            and subtitle_window.is_running
            and not messagebox.askyesno(
                "MekiCopy 종료",
                "MekiSubtitle 자막 생성이 진행 중입니다. 지금 종료하면 작업이 중단됩니다.\n\n그래도 종료할까요?",
                parent=self,
            )
        ):
            return False
        if _is_process_alive(self.audio_capture_process):
            try:
                audio_health = _json_request(
                    f"{self._audio_capture_base_url()}/health",
                    timeout=1.0,
                )
                audio_state = str(audio_health.get("state") or "").upper()
            except Exception:
                # A tracked process with an unreachable health endpoint may be
                # using the port captured at launch or may be hung mid-write.
                # Treat unknown as active so closing never silently force-kills it.
                audio_state = "UNKNOWN"
            if audio_state in {
                "STARTING",
                "RECORDING",
                "STOPPING",
                "PROCESSING",
                "TRANSLATING",
                "DOWNLOADING",
                "UNKNOWN",
            } and not messagebox.askyesno(
                "MekiCopy 종료",
                "음성 녹음 또는 처리가 진행 중입니다. 지금 종료하면 작업이 중단됩니다.\n\n그래도 종료할까요?",
                parent=self,
            ):
                return False
        self._closing = True
        if self._translation_status_after_id is not None:
            self.after_cancel(self._translation_status_after_id)
            self._translation_status_after_id = None
        self._realtime_ocr.close()
        if self._global_hotkey_after_id is not None:
            try:
                self.after_cancel(self._global_hotkey_after_id)
            except RuntimeError:
                pass
            self._global_hotkey_after_id = None
        self._global_hotkey.close()
        if self._companion_watchdog_after_id is not None:
            try:
                self.after_cancel(self._companion_watchdog_after_id)
            except RuntimeError:
                pass
            self._companion_watchdog_after_id = None
        self._companion_watchdog.stop()
        for signal in self._companion_manual_close_signals.values():
            discard_manual_close_signal(signal)
        self._companion_manual_close_signals.clear()
        if subtitle_window is not None:
            subtitle_window.cancel_for_parent_shutdown()
        if self._hytrans_restart_after_id is not None:
            try:
                self.after_cancel(self._hytrans_restart_after_id)
            except RuntimeError:
                pass
            self._hytrans_restart_after_id = None
        self._task_runner.close()
        self.tray_icon.close()
        _close_detached_window()
        if not save_settings(self.settings):
            _log_runtime_error(
                "save_settings_on_close",
                "settings file could not be written",
            )
        for process in (
            self.hytrans_process,
            self.overlayer_process,
            self.audio_capture_process,
            self.script_process,
        ):
            if _is_process_alive(process):
                try:
                    _terminate_process_tree(process)
                except Exception:
                    pass
        return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MekiCopy 영역 OCR 도구")
    parser.add_argument("--bookmark", help="저장된 북마크 이름으로 캡처")
    parser.add_argument("--pick-bookmark", action="store_true", help="북마크 목록에서 선택")
    parser.add_argument("--adjust-bookmark", help="북마크 영역을 불러와 미세조정 후 저장")
    parser.add_argument("--self-test-runtime", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test-ui", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--self-test-tray-stress",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--detached-button", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--self-test-detached-button",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--self-test-detached-survival",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--self-test-detached-launcher",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def run_detached_button() -> None:
    mutex = _WindowsNamedMutex(DETACHED_MUTEX_NAME)
    if mutex.already_exists:
        _activate_detached_window()
        mutex.close()
        return
    app: DetachedOcrButtonApp | None = None
    try:
        app = DetachedOcrButtonApp()
        app.run()
    except Exception as exc:
        _log_runtime_error("detached_button", exc)
        raise
    finally:
        try:
            if app is not None:
                app.close()
        finally:
            mutex.close()


def main() -> None:
    _enable_dpi_awareness()
    _set_app_user_model_id()
    args = parse_args()
    startup_settings = load_settings()
    configure_system_logging("MekiCopy", startup_settings.debug_logging)
    install_exception_hooks()
    if args.self_test_detached_launcher:
        _launch_detached_button_process()
        os._exit(0)
    if args.self_test_runtime:
        from mekicopy_selftest import run_runtime_self_test

        run_runtime_self_test()
        return
    if args.detached_button:
        run_detached_button()
        return
    if args.self_test_detached_button:
        from mekicopy_selftest import run_detached_button_self_test

        run_detached_button_self_test(DetachedOcrButtonApp)
        return
    if args.self_test_detached_survival:
        from mekicopy_selftest import run_detached_survival_self_test

        run_detached_survival_self_test()
        return
    if args.self_test_tray_stress:
        from mekicopy_selftest import run_tray_stress_self_test

        run_tray_stress_self_test(MainWindow)
        return
    if args.self_test_ui:
        from mekicopy_selftest import run_ui_self_test

        run_ui_self_test(
            MainWindow, window_size=(MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT),
            expected_ocr_button_height=OCR_BUTTON_HEIGHT_PX,
        )
        return
    if args.bookmark:
        bookmarks = load_bookmarks()
        bookmark = bookmarks.get(args.bookmark)
        if not bookmark:
            messagebox.showerror("MekiCopy", "북마크를 찾을 수 없습니다.")
            return
        ocr_and_translate(bookmark.left, bookmark.top, bookmark.width, bookmark.height)
        return
    if args.pick_bookmark:
        run_picker_and_capture()
        return
    if args.adjust_bookmark:
        bookmarks = load_bookmarks()
        bookmark = bookmarks.get(args.adjust_bookmark)
        if not bookmark:
            messagebox.showerror("MekiCopy", "북마크를 찾을 수 없습니다.")
            return
        region = run_selection(initial_region=bookmark, capture_on_enter=False)
        if region is not None:
            bookmarks[bookmark.name] = Bookmark(
                name=bookmark.name, left=region.left, top=region.top,
                width=region.width, height=region.height,
            )
            if not save_bookmarks(bookmarks):
                messagebox.showerror("MekiCopy", "바뀐 북마크 영역을 저장하지 못했습니다.")
        return
    window = MainWindow()
    window.show()
    ensure_application().exec()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _log_runtime_error("main", exc)
        if not any(arg.startswith("--self-test") for arg in sys.argv[1:]):
            try:
                messagebox.showerror(
                    "MekiCopy",
                    f"예기치 않은 오류로 프로그램을 종료합니다.\n\n{exc}\n\n로그: {log_directory('error_log', 'MekiCopy')}",
                )
            except Exception:
                pass
        raise SystemExit(1) from exc
