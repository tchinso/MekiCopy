from __future__ import annotations

import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from .logging_setup import debug
from .paths import chrome_profile_dir


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


class BrowserManager:
    """Own the private, headless Chromium runtime used by Transformers.js.

    HYTrans still uses the verified bundled JavaScript/ONNX runtime, but it no
    longer opens a user-facing app window.  That makes the worker lifecycle
    belong to HYTrans itself, so a user cannot accidentally close the model
    runtime while translation is in progress.
    """

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self._profile_dir: Path | None = None
        self._lock = threading.RLock()

    def find_chrome(self) -> str | None:
        candidates = [
            shutil.which("chrome"),
            shutil.which("chrome.exe"),
            shutil.which("msedge"),
            shutil.which("msedge.exe"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        for item in candidates:
            if item and Path(item).exists():
                return str(item)
        return None

    @staticmethod
    def _worker_command(chrome: str, url: str, profile: Path) -> list[str]:
        return [
            chrome,
            "--headless=new",
            # Prevent Edge's compatibility relaunch from escaping the private
            # profile/Popen handle that HYTrans owns and shuts down.
            "--edge-skip-compat-layer-relaunch",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-background-mode",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            # Keep the previous WebGPU-first behavior while allowing the
            # worker to fall back to WASM/CPU when no usable adapter exists.
            "--enable-unsafe-webgpu",
            "--enable-features=Vulkan",
            "--disable-gpu-sandbox",
            # The worker never renders user-facing content. Refuse Chromium's
            # software rasterizer so an emulated SwiftShader path cannot be
            # reported as GPU translation while consuming the game's CPU.
            "--disable-software-rasterizer",
            "--window-size=800,600",
            url,
        ]

    def start(self, url: str) -> None:
        with self._lock:
            # Reopening after a websocket/model failure must release the old
            # ONNX/WebGPU process tree first.  Merely replacing the Popen handle
            # leaves the previous private worker and its multi-gigabyte model
            # allocation alive.
            if not self._stop_locked():
                raise RuntimeError("the previous HYTrans worker did not stop")

            chrome = self.find_chrome()
            if not chrome:
                raise RuntimeError("Chrome or Edge was not found")

            profile_root = chrome_profile_dir()
            profile_root.mkdir(parents=True, exist_ok=True)
            # A per-process profile prevents a stopped/relaunched HYTrans
            # worker from sharing Chromium state with another companion that
            # is translating at the same time.
            profile = profile_root / f"worker-{os.getpid()}-{uuid.uuid4().hex}"
            profile.mkdir(parents=False, exist_ok=False)
            args = self._worker_command(chrome, url, profile)
            debug("private_worker_start", "\n".join(args))
            self._profile_dir = profile
            try:
                self.process = subprocess.Popen(args, creationflags=_creation_flags())
            except Exception:
                self._remove_stopped_profile_locked()
                raise

    def stop(self) -> bool:
        with self._lock:
            return self._stop_locked()

    def _stop_profile_processes_locked(self) -> None:
        """Stop an Edge compatibility relaunch outside the Popen process tree."""

        profile = self._profile_dir
        if os.name != "nt" or profile is None:
            return
        environment = os.environ.copy()
        environment["HYTRANS_CHROME_PROFILE"] = str(profile)
        script = (
            "$needle=$env:HYTRANS_CHROME_PROFILE; "
            "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" "
            "| Where-Object { $_.CommandLine -and $_.CommandLine.Contains($needle) } "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
                creationflags=_creation_flags(),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _remove_stopped_profile_locked(self) -> None:
        profile = self._profile_dir
        if profile is None:
            return
        self._profile_dir = None
        try:
            root = chrome_profile_dir().resolve()
            resolved = profile.resolve()
            if resolved.is_relative_to(root) and resolved != root:
                shutil.rmtree(resolved, ignore_errors=True)
        except OSError:
            pass

    def _stop_locked(self) -> bool:
        process = self.process
        self.process = None
        if process is None:
            self._stop_profile_processes_locked()
            self._remove_stopped_profile_locked()
            return True

        try:
            if process.poll() is not None:
                try:
                    process.wait(timeout=0)
                except OSError:
                    pass
                self._stop_profile_processes_locked()
                self._remove_stopped_profile_locked()
                return True

            if os.name == "nt":
                completed: subprocess.CompletedProcess | None = None
                try:
                    completed = subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=10,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except (OSError, subprocess.TimeoutExpired):
                    completed = None

                if completed is not None and completed.returncode == 0:
                    try:
                        process.wait(timeout=5)
                        self._stop_profile_processes_locked()
                        self._remove_stopped_profile_locked()
                        return True
                    except (OSError, subprocess.TimeoutExpired):
                        pass

            # taskkill can fail when Chrome is already exiting or when a
            # non-Windows browser is used.  Fall back to the normal terminate /
            # kill sequence and always reap the child process handle.
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
                self._stop_profile_processes_locked()
                self._remove_stopped_profile_locked()
                return True
            except (OSError, subprocess.TimeoutExpired):
                pass

            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self._stop_profile_processes_locked()
            stopped = process.poll() is not None
            if stopped:
                self._remove_stopped_profile_locked()
            if not stopped:
                debug("browser_stop", f"process still alive after kill: pid={process.pid}")
            return stopped
        finally:
            # Clear a reaped handle, but retain a process that resisted every
            # termination attempt so a later stop can retry it and start()
            # cannot orphan it by overwriting the only handle.
            self.process = process if process.poll() is None else None
