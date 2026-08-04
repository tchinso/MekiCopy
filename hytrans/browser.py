from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from .logging_setup import debug
from .paths import chrome_profile_dir


class BrowserManager:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
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

    def start(self, url: str) -> None:
        with self._lock:
            # Reopening after a websocket/model failure must release the old
            # ONNX/WebGPU process tree first.  Merely replacing the Popen handle
            # leaves the previous worker window and its multi-gigabyte model
            # allocation alive.
            if not self._stop_locked():
                raise RuntimeError("the previous HYTrans worker browser did not stop")

            chrome = self.find_chrome()
            if not chrome:
                raise RuntimeError("Chrome or Edge was not found")

            profile = chrome_profile_dir()
            profile.mkdir(parents=True, exist_ok=True)
            args = [
                chrome,
                f"--app={url}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-mode",
            ]
            debug("browser_start", "\n".join(args))
            self.process = subprocess.Popen(args)

    def stop(self) -> bool:
        with self._lock:
            return self._stop_locked()

    def _stop_locked(self) -> bool:
        process = self.process
        self.process = None
        if process is None:
            return True

        try:
            if process.poll() is not None:
                try:
                    process.wait(timeout=0)
                except OSError:
                    pass
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
            stopped = process.poll() is not None
            if not stopped:
                debug("browser_stop", f"process still alive after kill: pid={process.pid}")
            return stopped
        finally:
            # Clear a reaped handle, but retain a process that resisted every
            # termination attempt so a later stop can retry it and start()
            # cannot orphan it by overwriting the only handle.
            self.process = process if process.poll() is None else None
