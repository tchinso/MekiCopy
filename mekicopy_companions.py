from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from ctypes import wintypes

from mekicopy_runtime import _get_app_dir

DETACHED_WINDOW_TITLE = "MekiCopy - 분리 버튼"
MAGPIE_RELEASE_API_URL = "https://api.github.com/repos/Blinue/Magpie/releases/latest"

def _json_request(
    url: str,
    payload: dict | None = None,
    timeout: float = 5.0,
    method: str | None = None,
) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    if not raw:
        return {}
    return json.loads(raw)


def _validate_service_health(expected_app: str, payload: dict) -> str:
    if payload.get("ok") is not True:
        raise RuntimeError(f"{expected_app}가 정상 상태를 반환하지 않았습니다.")
    actual_app = payload.get("app")
    if actual_app != expected_app:
        raise RuntimeError(
            f"예상한 {expected_app} 대신 {actual_app or '알 수 없는 서비스'}가 응답했습니다."
        )
    state = str(payload.get("state") or payload.get("server") or "연결됨")
    if state.upper() == "ERROR":
        detail = payload.get("status") or payload.get("error") or "오류 상태"
        raise RuntimeError(f"{expected_app} 오류: {detail}")
    return state


def _probe_service(expected_app: str, base_url: str, timeout: float = 2.0) -> str:
    health = _json_request(f"{base_url.rstrip('/')}/health", timeout=timeout)
    state = _validate_service_health(expected_app, health)
    if expected_app != "HYTrans":
        return state

    ready = _json_request(f"{base_url.rstrip('/')}/ready", timeout=timeout)
    if ready.get("workerConnected") is not True:
        raise RuntimeError("HYTransWorker가 연결되어 있지 않습니다.")
    if ready.get("ready") is not True:
        worker_state = ready.get("state") or "모델 준비 중"
        detail = ready.get("error")
        suffix = f" ({detail})" if detail else ""
        raise RuntimeError(f"HYTrans 번역 모델이 준비되지 않았습니다: {worker_state}{suffix}")
    return str(ready.get("state") or "READY")


def _validated_translation_text(payload: dict) -> str:
    if payload.get("ok") is not True:
        raise RuntimeError("HYTrans가 번역 실패 상태를 반환했습니다.")
    text = str(payload.get("text") or "").strip()
    if not text:
        raise RuntimeError("HYTrans가 빈 번역 결과를 반환했습니다.")
    return text


def _is_process_alive(process: subprocess.Popen | None) -> bool:
    return bool(process and process.poll() is None)


def _mekicopy_process_command(*arguments: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]
    entrypoint = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mekicopy.py")
    return [sys.executable, entrypoint, *arguments]


def _detached_process_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )


def _launch_detached_button_process() -> subprocess.Popen:
    return subprocess.Popen(
        _mekicopy_process_command("--detached-button"),
        cwd=_get_app_dir(),
        close_fds=True,
        creationflags=_detached_process_creation_flags(),
    )


class _WindowsNamedMutex:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str) -> None:
        self.handle: int | None = None
        self.already_exists = False
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.SetLastError(0)
        self.handle = kernel32.CreateMutexW(None, False, name)
        if not self.handle:
            raise ctypes.WinError(kernel32.GetLastError())
        self.already_exists = kernel32.GetLastError() == self.ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle and os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(self.handle)
            self.handle = None


def _find_detached_window() -> int | None:
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    hwnd = user32.FindWindowW(None, DETACHED_WINDOW_TITLE)
    return int(hwnd) if hwnd else None


def _activate_detached_window() -> bool:
    hwnd = _find_detached_window()
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def _close_detached_window() -> bool:
    hwnd = _find_detached_window()
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    return bool(user32.PostMessageW(hwnd, 0x0010, 0, 0))  # WM_CLOSE


def _find_companion_executable(app_name: str, script_name: str) -> list[str] | None:
    exe_name = f"{app_name}.exe"
    app_dir = _get_app_dir()
    candidates = [
        os.path.join(app_dir, exe_name),
        os.path.join(os.path.dirname(app_dir), "MekiDisplay", exe_name),
        os.path.join(os.path.dirname(app_dir), app_name, exe_name),
        os.path.join(os.path.dirname(os.path.dirname(app_dir)), app_name, exe_name),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return [candidate]

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_name)
    if os.path.exists(script_path):
        return [sys.executable, script_path]
    return None


def _startupinfo_for_background() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return startupinfo


def _magpie_install_dir() -> str:
    return os.path.join(os.path.dirname(_get_app_dir()), "MagPie")


def _find_magpie_executable(directory: str | None = None) -> str | None:
    install_dir = directory or _magpie_install_dir()
    direct_path = os.path.join(install_dir, "MagPie.exe")
    if os.path.isfile(direct_path):
        return direct_path
    if not os.path.isdir(install_dir):
        return None
    for root, _dirs, files in os.walk(install_dir):
        for filename in files:
            if filename.casefold() == "magpie.exe":
                return os.path.join(root, filename)
    return None


def _select_magpie_release_asset(release: dict) -> dict:
    assets = [
        asset
        for asset in release.get("assets", [])
        if str(asset.get("name", "")).lower().endswith(".zip")
        and asset.get("browser_download_url")
    ]
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    for asset in assets:
        if architecture in str(asset.get("name", "")).lower():
            return asset
    raise RuntimeError(f"{architecture}용 MagPie 최신 릴리스 ZIP 파일이 없습니다.")


def _safe_extract_zip(archive_path: str, destination: str) -> None:
    destination_real = os.path.realpath(destination)
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            member_path = os.path.realpath(os.path.join(destination, info.filename))
            if os.path.commonpath([destination_real, member_path]) != destination_real:
                raise RuntimeError("MagPie 압축 파일에 안전하지 않은 경로가 포함되어 있습니다.")
        archive.extractall(destination)


def _install_latest_magpie() -> str:
    request = urllib.request.Request(
        MAGPIE_RELEASE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "MekiCopy-MagPie-Installer",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))
    asset = _select_magpie_release_asset(release)
    install_dir = _magpie_install_dir()
    suite_root = os.path.dirname(install_dir)
    os.makedirs(suite_root, exist_ok=True)
    staging_dir = tempfile.mkdtemp(prefix=".magpie-install-", dir=suite_root)
    try:
        archive_path = os.path.join(staging_dir, "MagPie.zip")
        download_request = urllib.request.Request(
            str(asset["browser_download_url"]),
            headers={"User-Agent": "MekiCopy-MagPie-Installer"},
        )
        with urllib.request.urlopen(download_request, timeout=120) as response:
            with open(archive_path, "wb") as archive_file:
                shutil.copyfileobj(response, archive_file)

        extracted_dir = os.path.join(staging_dir, "extracted")
        os.makedirs(extracted_dir, exist_ok=True)
        _safe_extract_zip(archive_path, extracted_dir)
        staged_executable = _find_magpie_executable(extracted_dir)
        if not staged_executable:
            raise RuntimeError("다운로드한 MagPie 압축 파일에서 MagPie.exe를 찾지 못했습니다.")

        source_root = os.path.dirname(staged_executable)
        os.makedirs(install_dir, exist_ok=True)
        for entry in os.scandir(source_root):
            destination = os.path.join(install_dir, entry.name)
            if entry.is_dir():
                shutil.copytree(entry.path, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(entry.path, destination)

        installed_executable = _find_magpie_executable(install_dir)
        if not installed_executable:
            raise RuntimeError("MagPie 설치 후 실행 파일을 찾지 못했습니다.")
        return installed_executable
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
