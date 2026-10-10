from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .logging_setup import debug
from .paths import chrome_profile_dir


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


class BrowserManager:
    """Own the private headless Chromium runtime used by browser backends."""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self._profile_dir: Path | None = None
        self._persistent_profile = False
        self._lock = threading.RLock()

    def find_chrome(self, *, prefer_edge: bool = False) -> str | None:
        chrome_candidates = [
            shutil.which("chrome"),
            shutil.which("chrome.exe"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        edge_candidates = [
            shutil.which("msedge"),
            shutil.which("msedge.exe"),
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        candidates = (edge_candidates + chrome_candidates if prefer_edge
                      else chrome_candidates + edge_candidates)
        for item in candidates:
            if item and Path(item).exists():
                return str(item)
        return None

    @staticmethod
    def _worker_command(chrome: str, url: str, profile: Path,
                        *, translator_api: bool = False) -> list[str]:
        command = [
            chrome,
            "--headless=new",
            # Prevent Edge's compatibility relaunch from escaping the private
            # profile/Popen handle that HYTrans owns and shuts down.
            "--edge-skip-compat-layer-relaunch",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-mode",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--window-size=800,600",
        ]
        if translator_api:
            # The browser owns and downloads its language-pair models. CDP is
            # used only to deliver the real click required for a first install.
            command.append("--remote-debugging-port=0")
        else:
            # Keep MT1.5 WebGPU-first, with WASM/CPU fallback. Do not report
            # software rasterization as GPU translation.
            command.extend((
                "--disable-background-networking",
                "--enable-unsafe-webgpu",
                "--enable-features=Vulkan",
                "--disable-gpu-sandbox",
                "--disable-software-rasterizer",
            ))
        command.append(url)
        return command

    def start(self, url: str, *, translator_api: bool = False) -> None:
        with self._lock:
            # Reopening after a websocket/model failure must release the old
            # ONNX/WebGPU process tree first.  Merely replacing the Popen handle
            # leaves the previous private worker and its multi-gigabyte model
            # allocation alive.
            if not self._stop_locked():
                raise RuntimeError("the previous HYTrans worker did not stop")

            chrome = self.find_chrome(prefer_edge=translator_api)
            if not chrome:
                raise RuntimeError("Chrome or Edge was not found")

            profile_root = chrome_profile_dir()
            profile_root.mkdir(parents=True, exist_ok=True)
            if translator_api:
                # Browser-managed translation models live in the profile. Use
                # a stable, private profile per executable and HYTrans port so
                # restarting the server does not force another model download.
                port = urlsplit(url).port or 0
                profile = profile_root / f"translator-api-{Path(chrome).stem.lower()}-{port}"
                profile.mkdir(parents=False, exist_ok=True)
            else:
                # MT1.5 does not need browser-owned state; keep its isolated,
                # disposable worker profile.
                profile = profile_root / f"worker-{os.getpid()}-{uuid.uuid4().hex}"
                profile.mkdir(parents=False, exist_ok=False)
            self._persistent_profile = translator_api
            args = self._worker_command(chrome, url, profile, translator_api=translator_api)
            debug("private_worker_start", "\n".join(args))
            self._profile_dir = profile
            try:
                if translator_api:
                    # A crashed predecessor can leave its CDP port file or
                    # process behind. Only this profile is owned by HYTrans.
                    self._stop_profile_processes_locked()
                    (profile / "DevToolsActivePort").unlink(missing_ok=True)
                self.process = subprocess.Popen(args, creationflags=_creation_flags())
                if translator_api:
                    try:
                        self._activate_translator_api(profile, url)
                    except Exception as exc:
                        # A warm model can still initialize without a click.
                        # The page reports an actionable error if it needs one.
                        debug("translator_api_activation", str(exc))
            except Exception:
                self._stop_locked()
                self._remove_stopped_profile_locked()
                raise

    def _activate_translator_api(self, profile: Path, url: str) -> None:
        """Deliver a trusted Chromium click to start an initial model download."""
        from websockets.sync.client import connect

        deadline = time.monotonic() + 15
        port_file = profile / "DevToolsActivePort"
        port: int | None = None
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError("browser exited before Translator API activation")
            try:
                port = int(port_file.read_text(encoding="ascii").splitlines()[0])
                break
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
        if port is None:
            raise RuntimeError("browser debugging endpoint did not open")

        target_url = f"http://127.0.0.1:{port}/json/list"
        websocket_url: str | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(target_url, timeout=2) as response:
                    targets = json.load(response)
                websocket_url = next(
                    (item["webSocketDebuggerUrl"] for item in targets
                     if item.get("type") == "page" and item.get("url") == url),
                    None,
                )
                if websocket_url:
                    break
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.1)
        if websocket_url is None:
            raise RuntimeError("Translator API browser page did not open")

        with connect(websocket_url, open_timeout=3, close_timeout=1) as socket:
            request_id = 0

            def call(method: str, params: dict[str, object]) -> dict[str, object]:
                nonlocal request_id
                request_id += 1
                socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
                while True:
                    response = json.loads(socket.recv(timeout=3))
                    if response.get("id") == request_id:
                        if "error" in response:
                            raise RuntimeError(str(response["error"]))
                        return response.get("result", {})

            expression = (
                "(() => { const b = document.getElementById('translator-start-button'); "
                "if (!b || b.dataset.armed !== 'true') return null; "
                "const r = b.getBoundingClientRect(); "
                "return {x: r.left + r.width / 2, y: r.top + r.height / 2, "
                "disabled: b.disabled}; })()"
            )
            while time.monotonic() < deadline:
                result = call("Runtime.evaluate", {
                    "expression": expression, "returnByValue": True,
                })
                location = result.get("result", {}).get("value")
                if isinstance(location, dict):
                    if location.get("disabled"):
                        return
                    x, y = location["x"], location["y"]
                    for event_type in ("mousePressed", "mouseReleased"):
                        call("Input.dispatchMouseEvent", {
                            "type": event_type, "x": x, "y": y,
                            "button": "left", "clickCount": 1,
                        })
                    debug("translator_api_activation", "trusted browser click delivered")
                    return
                time.sleep(0.1)
        raise RuntimeError("Translator API start button was not ready")

    def activate_translator_api(self, url: str) -> None:
        with self._lock:
            if self.process is None or self._profile_dir is None or not self._persistent_profile:
                raise RuntimeError("Translator API browser is not running")
            self._activate_translator_api(self._profile_dir, url)

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
        persistent = self._persistent_profile
        self._persistent_profile = False
        if persistent:
            return
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
