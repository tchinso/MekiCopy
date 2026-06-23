from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .logging_setup import debug
from .paths import chrome_profile_dir


class BrowserManager:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None

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

    def stop(self) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            if os.name == "nt":
                completed = subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode == 0:
                    return
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
