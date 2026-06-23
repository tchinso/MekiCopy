from __future__ import annotations

import asyncio
import json
from pathlib import Path
import urllib.error
import urllib.request
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import (
    MAX_INPUT_CHARS,
    TRANSLATE_TIMEOUT_SECONDS,
    options,
    runtime_config,
)
from .logging_setup import configure_logging, debug, error
from .model_cache import (
    model_cache_file_response,
    model_cache_status,
    save_model_cache_file,
)
from .model_download import model_download_manager
from .paths import assets_dir, models_dir
from .queue import TranslationQueue
from .state import AppState
from system_logging import log_debug as system_debug
from system_logging import log_error as system_error

app = FastAPI(title="HYTrans")
state = AppState()
translation_queue = TranslationQueue()
queue_task: asyncio.Task | None = None
worker_opener: Callable[[], None] | None = None


class ModelFileResponse(FileResponse):
    """Stream multi-gigabyte ONNX data in browser-friendly chunks.

    Starlette's 64 KiB default creates more than twenty thousand JavaScript
    progress chunks for this model's 1.3 GiB external-data file.  Transformers
    must combine those chunks before constructing the ONNX session, which can
    degrade into extreme garbage-collection stalls.  Eight MiB keeps progress
    responsive while avoiding that pathological allocation pattern.  Range
    requests remain supported by FileResponse.
    """

    chunk_size = 8 * 1024 * 1024


class TranslateBody(BaseModel):
    text: str
    overlayUrl: str | None = None


class ClientLogBody(BaseModel):
    level: str = "debug"
    stage: str = "worker"
    message: str = ""


class LoggingConfigBody(BaseModel):
    debugLog: bool


def _validate_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")
    if len(text) > MAX_INPUT_CHARS:
        raise HTTPException(status_code=413, detail="text is too long")
    return text


def _json_response(ok: bool, text: str = "") -> dict[str, object]:
    return {
        "ok": ok,
        "text": text,
        "device": state.device,
        "model": state.model,
        "dtype": state.dtype,
    }


def configure_worker_opener(opener: Callable[[], None] | None) -> None:
    global worker_opener
    worker_opener = opener


def _post_overlay_text(url: str, text: str) -> None:
    data = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status >= 400:
            raise RuntimeError(f"overlay returned HTTP {response.status}")


async def _send_to_overlay(text: str, overlay_url: str | None = None) -> None:
    target = overlay_url or options.overlay_url
    debug("overlay_send", f"url: {target}\nchars: {len(text)}")
    await asyncio.to_thread(_post_overlay_text, target, text)


async def _translate_text(text: str) -> str:
    if not state.worker_ready:
        error(
            "translate_not_ready",
            (
                f"model is not ready; state={state.state}; "
                f"workerConnected={state.worker_connected}; error={state.error or ''}"
            ),
        )
        raise HTTPException(status_code=503, detail="model is not ready")
    try:
        state.state = "BUSY"
        result = await translation_queue.submit(
            text=text,
            timeout=TRANSLATE_TIMEOUT_SECONDS,
        )
        result = result.strip()
        if not result:
            raise RuntimeError("translation worker returned an empty result")
        state.state = "READY"
        debug("translation_success", f"input_chars: {len(text)}\noutput_chars: {len(result)}")
        return result
    except asyncio.TimeoutError:
        state.state = "READY" if state.worker_ready else "ERROR"
        raise HTTPException(status_code=504, detail="translation timeout")
    except HTTPException:
        raise
    except Exception as exc:
        state.state = "ERROR"
        state.error = str(exc)
        error("translate", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.on_event("startup")
async def on_startup() -> None:
    global queue_task
    state.state = "STARTING"
    config = runtime_config()
    queue_task = asyncio.create_task(
        translation_queue.run(max_new_tokens=config.maxNewTokens)
    )
    debug("startup", f"model_mode: {config.modelMode}\nport: {options.port}")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    state.state = "STOPPING"
    translation_queue.stop()
    if queue_task:
        queue_task.cancel()


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "app": "HYTrans",
        "server": "running",
        "state": state.state,
        "workerConnected": state.worker_connected,
        "ready": state.worker_ready,
    }


@app.get("/ready")
async def ready() -> dict[str, object]:
    return state.as_ready_payload()


@app.post("/worker/reopen")
async def reopen_worker() -> dict[str, object]:
    if state.worker_connected:
        return {"ok": True, "workerConnected": True, "state": state.state}
    if worker_opener is None:
        raise HTTPException(status_code=503, detail="worker opener is unavailable")
    try:
        state.state = "BROWSER_OPENING"
        state.error = None
        await asyncio.to_thread(worker_opener)
        return {"ok": True, "workerConnected": False, "state": state.state}
    except Exception as exc:
        state.state = "ERROR"
        state.error = str(exc)
        error("worker_reopen", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/config")
async def get_config() -> dict[str, object]:
    return runtime_config().model_dump()


@app.post("/logging/config")
async def configure_runtime_logging(body: LoggingConfigBody) -> dict[str, object]:
    options.debug_log = bool(body.debugLog)
    configure_logging(options.debug_log)
    return {"ok": True, "debugLog": options.debug_log}


@app.post("/client-log")
async def client_log(body: ClientLogBody) -> dict[str, object]:
    message = body.message.strip()
    if not message:
        return {"ok": True}
    stage = f"HyTransWorker.{body.stage.strip() or 'worker'}"
    if body.level.lower() in {"error", "fatal"}:
        system_error(stage, message, component="HyTransWorker")
    else:
        system_debug(
            stage,
            message,
            component="HyTransWorker",
            enabled=options.debug_log,
        )
    return {"ok": True}


@app.get("/model-cache-status")
async def get_model_cache_status(url: str) -> dict[str, object]:
    return model_cache_status(url)


@app.get("/model-cache")
async def get_model_cache_file(url: str) -> FileResponse:
    return model_cache_file_response(url)


@app.post("/model-cache")
async def post_model_cache_file(url: str, request: Request) -> dict[str, object]:
    return await save_model_cache_file(url, request)


@app.post("/model/prepare")
async def prepare_model() -> dict[str, object]:
    return model_download_manager.start()


@app.get("/model/status")
async def model_status() -> dict[str, object]:
    return model_download_manager.status()


@app.get("/worker.html")
async def worker_html() -> FileResponse:
    return FileResponse(assets_dir() / "worker.html")


@app.api_route("/models/{relative_path:path}", methods=["GET", "HEAD"])
async def local_model_file(relative_path: str) -> FileResponse:
    root = models_dir().resolve()
    target = (root / Path(relative_path)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="model file not found") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="model file not found")
    return ModelFileResponse(target)


@app.get("/translate", response_model=None)
async def translate_get(
    text: str = "",
    format: str = "text",
    source: str | None = None,
    target: str | None = None,
) -> PlainTextResponse | dict[str, object]:
    del source, target
    clean_text = _validate_text(text)
    result = await _translate_text(clean_text)
    if format == "json":
        return _json_response(True, result)
    return PlainTextResponse(result, media_type="text/plain; charset=utf-8")


@app.post("/translate", response_model=None)
async def translate_post(
    body: TranslateBody,
    format: str = "text",
) -> PlainTextResponse | dict[str, object]:
    clean_text = _validate_text(body.text)
    result = await _translate_text(clean_text)
    if format == "json":
        return _json_response(True, result)
    return PlainTextResponse(result, media_type="text/plain; charset=utf-8")


@app.post("/translate-and-show")
async def translate_and_show(body: TranslateBody) -> dict[str, object]:
    clean_text = _validate_text(body.text)
    result = await _translate_text(clean_text)
    try:
        await _send_to_overlay(result, body.overlayUrl)
    except Exception as exc:
        error("overlay_send", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _json_response(True, result)


@app.post("/overlay-test")
async def overlay_test(body: TranslateBody) -> dict[str, object]:
    clean_text = _validate_text(body.text)
    try:
        await _send_to_overlay(clean_text, body.overlayUrl)
    except Exception as exc:
        error("overlay_test", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "text": clean_text}


@app.websocket("/ws/worker")
async def worker_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    state.worker_connected = True
    state.worker_ready = False
    state.state = "WORKER_CONNECTED"
    translation_queue.set_worker(websocket)
    debug("worker_connected", "websocket connected")

    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            msg_type = message.get("type")

            if msg_type == "loading":
                state.state = "WORKER_LOADING"
                debug("worker_loading", str(message.get("message", "")))

            elif msg_type == "ready":
                state.worker_ready = True
                state.state = "READY"
                state.device = message.get("device")
                state.model = message.get("model")
                state.dtype = message.get("dtype")
                state.model_mode = message.get("modelMode")
                state.warning = message.get("warning")
                state.error = None
                debug("worker_ready", json.dumps(message, ensure_ascii=False))

            elif msg_type == "result":
                translation_queue.resolve(
                    request_id=message["id"],
                    text=message.get("text", ""),
                )

            elif msg_type == "error":
                translation_queue.reject(
                    request_id=message["id"],
                    message=message.get("message", "worker error"),
                )

            elif msg_type == "fatal":
                state.worker_ready = False
                state.state = "ERROR"
                state.error = message.get("message", "worker fatal error")
                error("worker_fatal", state.error)

    except WebSocketDisconnect:
        state.worker_connected = False
        state.worker_ready = False
        state.state = "ERROR"
        state.error = "worker disconnected"
        translation_queue.clear_worker()
        debug("worker_disconnected", "websocket disconnected")
    except Exception as exc:
        state.worker_connected = False
        state.worker_ready = False
        state.state = "ERROR"
        state.error = str(exc)
        translation_queue.clear_worker()
        error("worker_websocket", exc)
        raise


if assets_dir().exists():
    app.mount("/assets", StaticFiles(directory=assets_dir()), name="assets")
