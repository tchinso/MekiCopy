"""Executable smoke checks for the desktop UI and process lifecycle."""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from typing import Any, Callable

from PySide6.QtWidgets import QWidget

import mekicopy_settings
from mekicopy_capture import Region
from mekicopy_companions import (
    DETACHED_WINDOW_TITLE,
    _close_detached_window,
    _find_detached_window,
    _mekicopy_process_command,
)
from mekicopy_ocr import _get_ocr_engine, _log_runtime_error, _log_runtime_message
from mekicopy_runtime import _get_app_dir, _get_icon_path
from mekicopy_theme import ensure_application
from mekicopy_settings import (
    Bookmark,
    _font_has_character,
    load_detached_region,
    save_settings,
)


def run_detached_button_self_test(create_app: Callable[[], Any]) -> None:
    _log_runtime_message("self_test_detached_button", "starting")
    app = None
    try:
        app = create_app()
        ensure_application().processEvents()
        if not app.root.isVisible() or not app.button.isVisible():
            raise RuntimeError("detached button UI was not created")
        if app.root.windowTitle() != DETACHED_WINDOW_TITLE:
            raise RuntimeError("detached button window title is not stable")
        if app.button.text() != app.ocr_action_label():
            raise RuntimeError("detached button label does not match settings")
        command = _mekicopy_process_command("--detached-button")
        if "--detached-button" not in command:
            raise RuntimeError("detached process command is invalid")
        _log_runtime_message(
            "self_test_detached_button",
            f"command: {command}\nwindow: {app.capture_geometry(persist=False)}",
        )
    except Exception as exc:
        _log_runtime_error("self_test_detached_button", exc)
        raise SystemExit(1)
    finally:
        if app is not None:
            app.close()


def run_detached_survival_self_test() -> None:
    if os.name != "nt":
        return
    _log_runtime_message("self_test_detached_survival", "starting")
    if _find_detached_window():
        _close_detached_window()
        deadline = time.monotonic() + 5.0
        while _find_detached_window() and time.monotonic() < deadline:
            time.sleep(0.05)
        if _find_detached_window():
            raise RuntimeError("an existing detached button window could not be closed")

    launcher = subprocess.Popen(
        _mekicopy_process_command("--self-test-detached-launcher"),
        cwd=_get_app_dir(),
        close_fds=True,
    )
    try:
        exit_code = launcher.wait(timeout=30)
    except subprocess.TimeoutExpired:
        launcher.kill()
        _close_detached_window()
        raise RuntimeError("detached survival launcher did not exit")
    if exit_code != 0:
        _close_detached_window()
        raise RuntimeError(f"detached survival launcher exited with {exit_code}")

    hwnd = None
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        hwnd = _find_detached_window()
        if hwnd:
            break
        time.sleep(0.05)
    if not hwnd:
        _close_detached_window()
        raise RuntimeError("detached button did not survive its launcher process")

    process_id = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value or process_id.value == launcher.pid:
        _close_detached_window()
        raise RuntimeError("detached button is not running in an independent process")
    _log_runtime_message(
        "self_test_detached_survival",
        f"launcher_pid: {launcher.pid}\ndetached_pid: {process_id.value}",
    )
    # The native window may exist before Qt starts dispatching WM_CLOSE.
    time.sleep(1.0)
    _close_detached_window()
    deadline = time.monotonic() + 30.0
    while _find_detached_window() and time.monotonic() < deadline:
        time.sleep(0.05)
    if _find_detached_window():
        raise RuntimeError("detached button did not close after survival test")


def run_tray_stress_self_test(create_window: Callable[[], QWidget], cycles: int = 100) -> None:
    _log_runtime_message("self_test_tray_stress", f"starting cycles={cycles}")
    app = None
    try:
        app = create_window()
        app.settings.minimize_to_tray = True
        app.show()
        ensure_application().processEvents()
        if os.name != "nt":
            return

        user32 = ctypes.windll.user32
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        tray_hwnd = None
        for cycle in range(1, cycles + 1):
            app.showMinimized()
            minimize_deadline = time.monotonic() + 3.0
            while time.monotonic() < minimize_deadline:
                ensure_application().processEvents()
                if app.tray_icon._active and not app.isVisible():
                    break
                time.sleep(0.005)
            if not app.tray_icon._active or app.isVisible():
                raise RuntimeError(
                    f"cycle {cycle}: minimize-to-tray failed (state={app.windowState()})"
                )

            current_hwnd = app.tray_icon._hwnd
            if not current_hwnd:
                raise RuntimeError(f"cycle {cycle}: tray callback window is missing")
            if tray_hwnd is None:
                tray_hwnd = current_hwnd
            elif current_hwnd != tray_hwnd:
                raise RuntimeError(f"cycle {cycle}: tray callback HWND changed")

            # Explorer emits the double-click notification asynchronously. Two
            # queued notifications also verify duplicate clicks are coalesced.
            for _ in range(2):
                if not user32.PostMessageW(
                    current_hwnd,
                    app.tray_icon.WM_TRAY_CALLBACK,
                    1,
                    app.tray_icon.WM_LBUTTONDBLCLK,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())

            restore_deadline = time.monotonic() + 3.0
            while time.monotonic() < restore_deadline:
                ensure_application().processEvents()
                if (
                    app.isVisible() and not app.isMinimized()
                    and not app.tray_icon._active
                    and not app._restoring_from_tray
                ):
                    break
                time.sleep(0.005)
            if (not app.isVisible() or app.isMinimized()) or app.tray_icon._active:
                raise RuntimeError(
                    f"cycle {cycle}: tray restore failed "
                    f"(state={app.windowState()}, active={app.tray_icon._active})"
                )

        _log_runtime_message(
            "self_test_tray_stress",
            f"completed cycles={cycles}\ntray_hwnd={tray_hwnd}",
        )
    except Exception as exc:
        _log_runtime_error("self_test_tray_stress", exc)
        raise SystemExit(1)
    finally:
        if app is not None:
            app.close()


def _snapshot_runtime_state(paths: tuple[str, ...]) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for path in paths:
        try:
            with open(path, "rb") as handle:
                snapshot[path] = handle.read()
        except FileNotFoundError:
            snapshot[path] = None
        except OSError as exc:
            _log_runtime_error("snapshot_runtime_state", exc)
            snapshot[path] = None
    return snapshot


def _restore_runtime_state(snapshot: dict[str, bytes | None]) -> None:
    for path, payload in snapshot.items():
        try:
            if payload is None:
                if os.path.exists(path):
                    os.remove(path)
                continue
            with open(path, "wb") as handle:
                handle.write(payload)
        except OSError as exc:
            _log_runtime_error("restore_runtime_state", exc)


def run_ui_self_test(
    create_window: Callable[[], QWidget],
    *,
    window_size: tuple[int, int],
    expected_ocr_button_height: int,
) -> None:
    window_width, window_height = window_size
    _log_runtime_message("self_test_ui", "starting")
    app = None
    runtime_snapshot = _snapshot_runtime_state(
        (
            mekicopy_settings.SETTINGS_FILE,
            mekicopy_settings.DETACHED_REGION_FILE,
            mekicopy_settings.DETACHED_GEOMETRY_FILE,
        )
    )
    try:
        app = create_window()
        app.show()
        ensure_application().processEvents()
        if app.height() != window_height:
            raise RuntimeError(
                f"main window height is {app.height()}, expected {window_height}"
            )
        expected_tabs = {"영역 설정", "캡쳐 설정", "도구", "행동", "소리 번역", "영상 자막 생성", "설정"}
        if set(app.tab_frames) != expected_tabs:
            raise RuntimeError(f"unexpected main tabs: {set(app.tab_frames)}")
        if app._workflow_mode is not None:
            raise RuntimeError("main window did not open at the translation workflow picker")
        if any(frame.isVisible() for frame in app.tab_frames.values()):
            raise RuntimeError("workflow content is visible before a workflow is selected")

        app._select_workflow("screen")
        ensure_application().processEvents()
        visible_tabs = {name for name, button in app.tab_buttons.items() if button.isVisible()}
        if visible_tabs != {"영역 설정", "캡쳐 설정", "도구", "행동", "설정"}:
            raise RuntimeError(f"unexpected screen translation tabs: {visible_tabs}")
        app._select_tab("행동")
        ensure_application().processEvents()
        ocr_button_height = app.ocr_button.height()
        if ocr_button_height != expected_ocr_button_height:
            raise RuntimeError(
                f"main OCR button height is {ocr_button_height}, expected {expected_ocr_button_height}"
            )
        if app.realtime_button is not app.ocr_button:
            raise RuntimeError("manual and realtime translation do not share the action button")
        initial_realtime_enabled = app.settings.realtime_ocr_enabled
        for realtime_enabled, expected_label in ((False, "번역 후 표시"), (True, "실시간 OCR 시작")):
            app.settings.realtime_ocr_enabled = realtime_enabled
            app._sync_realtime_ocr()
            ensure_application().processEvents()
            if app.ocr_button.text() != expected_label:
                raise RuntimeError(f"unexpected translation action: {app.ocr_button.text()}")
            if app.detach_button.isEnabled() == realtime_enabled:
                raise RuntimeError("detached button availability does not match translation mode")
        app.settings.realtime_ocr_enabled = initial_realtime_enabled
        app._sync_realtime_ocr()
        app._select_tab("도구")
        ensure_application().processEvents()
        if not app.hytrans_button.isEnabled():
            raise RuntimeError("translation engine launch button is disabled")
        if not app.overlayer_button.isEnabled():
            raise RuntimeError("translation display launch button is disabled")
        for workflow, frame_name in (("audio", "소리 번역"), ("subtitle", "영상 자막 생성")):
            app._select_workflow(workflow)
            ensure_application().processEvents()
            if app._workflow_mode != workflow or not app.tab_frames[frame_name].isVisible():
                raise RuntimeError(f"{workflow} workflow did not show its content")
            if app.tab_frames["행동"].isVisible():
                raise RuntimeError(f"screen translation actions are visible in {workflow} workflow")
            app._show_workflow_picker()
            ensure_application().processEvents()
            if app._workflow_mode is not None:
                raise RuntimeError(f"{workflow} workflow could not return to the picker")
        app._select_workflow("screen")
        app._select_tab("영역 설정")
        ensure_application().processEvents()

        bookmark = Bookmark(name="self-test", left=11, top=22, width=333, height=44)
        expected_region = Region(left=11, top=22, width=333, height=44)
        app._load_bookmark_region(bookmark)
        if app.active_region != expected_region:
            raise RuntimeError(f"bookmark did not populate active region: {app.active_region}")
        if "확정 영역 · 설정되지 않음" in app.status_label.text():
            raise RuntimeError("bookmark status still reports no active region")
        app._select_tab("행동")
        app.resize(app.minimumSize())
        ensure_application().processEvents()
        action_viewport = app._tab_scroll_areas["행동"].viewport()
        action_bottom = app.ocr_button.mapTo(action_viewport, app.ocr_button.rect().bottomRight()).y()
        if action_bottom >= action_viewport.height():
            raise RuntimeError("translation action is clipped at the minimum window size")
        app.resize(window_width, window_height)

        tray_roundtrip = False
        tray_message_window_reused = False
        if os.name == "nt":
            tray_hwnd = None
            for _ in range(3):
                if not app.tray_icon.show():
                    break
                tray_roundtrip = True
                current_hwnd = app.tray_icon._hwnd
                if tray_hwnd is None:
                    tray_hwnd = current_hwnd
                elif current_hwnd != tray_hwnd:
                    raise RuntimeError("tray message window was recreated during restore")
                app.hide()
                ensure_application().processEvents()
                ctypes.windll.user32.SendMessageW(
                    current_hwnd,
                    app.tray_icon.WM_TRAY_CALLBACK,
                    1,
                    app.tray_icon.WM_LBUTTONDBLCLK,
                )
                ctypes.windll.user32.SendMessageW(
                    current_hwnd,
                    app.tray_icon.WM_TRAY_CALLBACK,
                    1,
                    app.tray_icon.WM_LBUTTONDBLCLK,
                )
                deadline = time.monotonic() + 2.0
                while (not app.isVisible() or app.isMinimized()) and time.monotonic() < deadline:
                    ensure_application().processEvents()
                    time.sleep(0.01)
                if (not app.isVisible() or app.isMinimized()):
                    raise RuntimeError(f"tray restore left app in state: {app.windowState()}")
            tray_message_window_reused = bool(
                tray_roundtrip
                and app.tray_icon._window_class_atom
                and app.tray_icon._hwnd == tray_hwnd
            )
            if tray_roundtrip and not tray_message_window_reused:
                raise RuntimeError("tray message window did not survive restore")

        if load_detached_region() != expected_region:
            raise RuntimeError("active region was not published for detached process")
        if "--detached-button" not in _mekicopy_process_command("--detached-button"):
            raise RuntimeError("detached OCR button process command was not created")

        app._on_open_settings()
        ensure_application().processEvents()
        if not app.settings_window or not app.settings_window.isVisible():
            raise RuntimeError("settings window was not created")
        font_picker = app.settings_window.overlayer_text_font
        korean_font_count = sum(
            _font_has_character(font_picker.itemText(index)) for index in range(font_picker.count())
        )
        if not korean_font_count:
            raise RuntimeError("font menu contains no Korean-capable fonts")

        app.settings_window.close()
        save_settings(app.settings)
        _log_runtime_message(
            "self_test_ui",
            (
                f"main_ocr_button_height: {ocr_button_height}\n"
                f"main_window_height: {app.height()}\n"
                f"tray_roundtrip: {tray_roundtrip}\n"
                f"tray_message_window_reused: {tray_message_window_reused}\n"
                f"korean_font_count: {korean_font_count}\n"
                f"settings_file: {mekicopy_settings.SETTINGS_FILE}\n"
                f"icon_path: {_get_icon_path()}"
            ),
        )
    except Exception as exc:
        _log_runtime_error("self_test_ui", exc)
        raise SystemExit(1)
    finally:
        if app is not None:
            app.close()
        _restore_runtime_state(runtime_snapshot)

def run_runtime_self_test() -> None:
    from importlib.metadata import version

    _log_runtime_message("self_test_runtime", "starting")
    meikiocr_version = version("meikiocr")
    if meikiocr_version != "0.3.5":
        raise RuntimeError(
            f"unexpected meikiocr version: {meikiocr_version} (expected 0.3.5)"
        )
    engine = _get_ocr_engine()
    _log_runtime_message(
        "self_test_runtime",
        f"meikiocr_version: {meikiocr_version}\n"
        f"active_provider: {getattr(engine, 'active_provider', 'unknown')}",
    )
