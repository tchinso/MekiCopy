from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import urllib.request

import uvicorn

from hytrans.app import app, state
from hytrans.browser import BrowserManager
from hytrans.config import DEFAULT_OVERLAY_URL, DEFAULT_PORT, HOST, configure_server
from hytrans.logging_setup import configure_logging, debug, error

_window_stream = None


def prepare_windowed_streams() -> None:
    global _window_stream
    if sys.stderr is None:
        _window_stream = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = _window_stream
    if sys.stdout is None:
        sys.stdout = sys.stderr


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HYTrans Japanese to Korean server")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--overlay-url", default=DEFAULT_OVERLAY_URL)
    parser.add_argument("--debug-log", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def ensure_port_available(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise RuntimeError(
                f"HYTrans could not start because port {port} is already in use."
            ) from exc


def wait_server_ready(host: str, port: int, timeout: float = 15.0) -> bool:
    url = f"http://{host}:{port}/health"
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def main() -> int:
    prepare_windowed_streams()
    args = parse_args()
    configure_server(
        host=args.host,
        port=args.port,
        overlay_url=args.overlay_url,
        debug_log=args.debug_log,
    )
    configure_logging(args.debug_log)

    browser = BrowserManager()
    server: uvicorn.Server | None = None
    try:
        ensure_port_available(args.host, args.port)
        config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        if not wait_server_ready(args.host, args.port):
            raise RuntimeError("HYTrans server failed to start")

        if not args.no_browser:
            state.state = "BROWSER_OPENING"
            browser.start(f"http://{args.host}:{args.port}/worker.html")

        debug("main", f"server running on {args.host}:{args.port}")
        while server_thread.is_alive():
            time.sleep(0.5)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        error("main", exc)
        return 1
    finally:
        browser.stop()
        if server:
            server.should_exit = True


if __name__ == "__main__":
    raise SystemExit(main())
