from __future__ import annotations

import argparse
import secrets
import socket
import sys
import threading
import time
import urllib.request

import uvicorn

from app_identity import set_windows_app_id
from hytrans.app import (
    app,
    configure_shutdown_handler,
    configure_worker_opener,
    state,
)
from hytrans.browser import BrowserManager
from hytrans.config import DEFAULT_OVERLAY_URL, DEFAULT_PORT, HOST, configure_server
from hytrans.logging_setup import configure_logging, debug, error
from hytrans.model_files import DEFAULT_MODEL_ID, SUPPORTED_MODEL_IDS
from system_logging import capture_windowed_streams, install_exception_hooks

def prepare_windowed_streams() -> None:
    capture_windowed_streams()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HYTrans Japanese to Korean server")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--overlay-url", default=DEFAULT_OVERLAY_URL)
    parser.add_argument(
        "--model",
        "--model-id",
        dest="model_id",
        choices=SUPPORTED_MODEL_IDS,
        default=DEFAULT_MODEL_ID,
    )
    parser.add_argument("--debug-log", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def ensure_port_available(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
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
    set_windows_app_id("HYTrans")
    args = parse_args()
    configure_server(
        host=args.host,
        port=args.port,
        overlay_url=args.overlay_url,
        debug_log=args.debug_log,
        model_id=args.model_id,
    )
    configure_logging(args.debug_log)
    install_exception_hooks()
    prepare_windowed_streams()

    browser = BrowserManager()
    server: uvicorn.Server | None = None
    server_thread: threading.Thread | None = None
    shutdown_requested = threading.Event()
    worker_url = f"http://{args.host}:{args.port}/worker.html"
    configure_worker_opener(lambda: browser.start(worker_url))
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
        configure_shutdown_handler(
            shutdown_requested.set,
            secrets.token_urlsafe(32),
        )
        server_thread = threading.Thread(
            target=server.run,
            name="HYTransServer",
            daemon=True,
        )
        server_thread.start()

        if not wait_server_ready(args.host, args.port):
            raise RuntimeError("HYTrans server failed to start")

        if not args.no_browser:
            state.state = "BROWSER_OPENING"
            browser.start(worker_url)

        debug("main", f"server running on {args.host}:{args.port}")
        while server_thread.is_alive():
            if shutdown_requested.is_set():
                # Release the Chrome profile and ONNX resources before the
                # listening port disappears. MekiCopy waits for that port to
                # close before launching the replacement HYTrans instance.
                if browser.stop():
                    server.should_exit = True
                else:
                    debug(
                        "shutdown",
                        "browser process is still alive; keeping the server "
                        "open so the restart controller can force-stop it",
                    )
            time.sleep(0.5)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        error("main", exc)
        return 1
    finally:
        if server:
            server.should_exit = True
        if server_thread and server_thread.is_alive():
            server_thread.join(timeout=10)
        browser.stop()
        configure_shutdown_handler(None)
        configure_worker_opener(None)


if __name__ == "__main__":
    raise SystemExit(main())
