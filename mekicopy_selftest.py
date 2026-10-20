"""Executable smoke checks for the desktop UI and process lifecycle."""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
import tkinter as tk
from ctypes import wintypes
from typing import Any, Callable

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
        app.root.update_idletasks()
        if not app.root.winfo_exists() or not app.button.winfo_exists():
            raise RuntimeError("detached button UI was not created")
        if app.root.title() != DETACHED_WINDOW_TITLE:
            raise RuntimeError("detached button window title is not stable")
        if app.button.cget("text") != app.ocr_action_label():
            raise RuntimeError("detached button label does not match settings")
        command = _mekicopy_process_command("--detached-button")
        if "--detached-button" not in command:
            raise RuntimeError("detached process command is invalid")
        _log_runtime_message(
            "self_test_detached_button",
            f"command: {command}\nwindow: {app.root.geometry()}",
        )
    except Exception as exc:
        _log_runtime_error("self_test_detached_button", exc)
        raise SystemExit(1)
    finally:
        if app and app.root.winfo_exists():
            app._closing = True
            app.root.destroy()


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
    # FindWindow can succeed while Tk is still finishing constructor work,
    # before mainloop has started dispatching WM_CLOSE.
    time.sleep(1.0)
    _close_detached_window()
    deadline = time.monotonic() + 30.0
    while _find_detached_window() and time.monotonic() < deadline:
        time.sleep(0.05)
    if _find_detached_window():
        raise RuntimeError("detached button did not close after survival test")


def run_tray_stress_self_test(create_window: Callable[[], tk.Tk], cycles: int = 100) -> None:
    _log_runtime_message("self_test_tray_stress", f"starting cycles={cycles}")
    app = None
    try:
        app = create_window()
        app.settings.minimize_to_tray = True
        app.update_idletasks()
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
            app.iconify()
            minimize_deadline = time.monotonic() + 3.0
            while time.monotonic() < minimize_deadline:
                app.update()
                if app.tray_icon._active and app.state() == "withdrawn":
                    break
                time.sleep(0.005)
            if not app.tray_icon._active or app.state() != "withdrawn":
                raise RuntimeError(
                    f"cycle {cycle}: minimize-to-tray failed (state={app.state()})"
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
                app.update()
                if (
                    app.state() == "normal"
                    and not app.tray_icon._active
                    and not app._restoring_from_tray
                ):
                    break
                time.sleep(0.005)
            if app.state() != "normal" or app.tray_icon._active:
                raise RuntimeError(
                    f"cycle {cycle}: tray restore failed "
                    f"(state={app.state()}, active={app.tray_icon._active})"
                )

        _log_runtime_message(
            "self_test_tray_stress",
            f"completed cycles={cycles}\ntray_hwnd={tray_hwnd}",
        )
    except Exception as exc:
        _log_runtime_error("self_test_tray_stress", exc)
        raise SystemExit(1)
    finally:
        if app:
            try:
                app.tray_icon.close()
                app.destroy()
            except tk.TclError:
                pass


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
    create_window: Callable[[], tk.Tk],
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
        app.update_idletasks()
        if app.winfo_height() != window_height:
            raise RuntimeError(
                f"main window height is {app.winfo_height()}, expected {window_height}"
            )
        expected_tabs = {
            "영역",
            "캡쳐",
            "음성인식",
            "자막 생성",
            "도구/설정",
            "행동",
        }
        if set(app.tab_frames) != expected_tabs:
            raise RuntimeError(f"unexpected main tabs: {set(app.tab_frames)}")
        app._select_tab("행동")
        app.update_idletasks()
        ocr_button_height = app.ocr_button.master.winfo_height()
        if ocr_button_height != expected_ocr_button_height:
            raise RuntimeError(
                f"main OCR button height is {ocr_button_height}, expected {expected_ocr_button_height}"
            )
        app._select_tab("영역")
        app.update_idletasks()
        region_frame = app.tab_frames["영역"]
        if (not app.realtime_button.winfo_ismapped()
                or app.realtime_button.master is not region_frame
                or app.realtime_button.winfo_y() + app.realtime_button.winfo_height() > region_frame.winfo_height()):
            raise RuntimeError("realtime OCR control is clipped outside the region tab")
        app._select_tab("도구/설정")
        app.update_idletasks()
        expected_overlay_state = (
            tk.NORMAL if app.settings.overlay_translation_mode else tk.DISABLED
        )
        if app.hytrans_button.cget("state") != expected_overlay_state:
            raise RuntimeError("HYTrans button state does not match overlay mode")
        if app.overlayer_button.cget("state") != expected_overlay_state:
            raise RuntimeError("MekiOverlayer button state does not match overlay mode")
        app._select_tab("자막 생성")
        app.update_idletasks()
        if "MekiSubtitle" not in app.tab_frames["자막 생성"].winfo_children()[0].cget("text"):
            raise RuntimeError("MekiSubtitle tab title was not created")
        app._select_tab("영역")
        app.update_idletasks()

        bookmark = Bookmark(name="self-test", left=11, top=22, width=333, height=44)
        expected_region = Region(left=11, top=22, width=333, height=44)
        app._load_bookmark_region(bookmark)
        if app.active_region != expected_region:
            raise RuntimeError(f"bookmark did not populate active region: {app.active_region}")
        if "확정 영역 :\n설정되지 않음" in app.status_label.cget("text"):
            raise RuntimeError("bookmark status still reports no active region")
        app._select_tab("영역")
        app.geometry("500x420")
        app.update_idletasks()
        if app.realtime_button.winfo_y() + app.realtime_button.winfo_height() > region_frame.winfo_height():
            raise RuntimeError("realtime OCR control is clipped at the minimum window size")
        app.geometry(f"{window_width}x{window_height}")

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
                app.withdraw()
                app.update_idletasks()
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
                while app.state() != "normal" and time.monotonic() < deadline:
                    app.update()
                    time.sleep(0.01)
                if app.state() != "normal":
                    raise RuntimeError(f"tray restore left app in state: {app.state()}")
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
        app.update_idletasks()
        if not app.settings_window or not app.settings_window.winfo_exists():
            raise RuntimeError("settings window was not created")
        if not app.settings_window.korean_font_names:
            raise RuntimeError("font menu contains no Korean-capable fonts")
        unsupported_fonts = [
            name
            for name in app.settings_window.korean_font_names
            if not _font_has_character(name)
        ]
        if unsupported_fonts:
            raise RuntimeError(f"font menu contains unsupported fonts: {unsupported_fonts}")
        korean_font_count = len(app.settings_window.korean_font_names)

        app.settings_window._on_close()
        save_settings(app.settings)
        _log_runtime_message(
            "self_test_ui",
            (
                f"main_ocr_button_height: {ocr_button_height}\n"
                f"main_window_height: {app.winfo_height()}\n"
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
        if app and app.winfo_exists():
            app.tray_icon.close()
            app.destroy()
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
