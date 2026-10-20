from __future__ import annotations

import asyncio
from contextlib import suppress
import ipaddress
import json
from pathlib import Path
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import (
    MAX_INPUT_CHARS,
    MAX_NEW_TOKENS,
    options,
    realtime_max_new_tokens,
    runtime_config,
    selected_api_profile,
    translation_timeout_seconds,
)
from .api_client import ApiTranslationError, ApiTranslationQueue, ApiTranslator
from .api_settings import BROWSER_BACKENDS, PROVIDERS, validate_provider_settings
from .hdtrans_backend import HdTransBackend, HdTransError
from .hdtrans_settings import HdTransSettings, load_hdtrans_settings
from .logging_setup import configure_logging, debug, error
from .model_cache import (
    model_cache_file_response,
    model_cache_status,
    save_model_cache_file,
)
from .model_download import model_download_manager
from .model_files import active_model_profile
from .paths import assets_dir, models_dir
from .queue import TranslationQueue, TranslationQueueOverloadedError
from .state import AppState
from system_logging import log_debug as system_debug
from system_logging import log_error as system_error

app = FastAPI(title="HYTrans")
state = AppState()
translation_queue = TranslationQueue()
queue_task: asyncio.Task | None = None
api_translation_queue: ApiTranslationQueue | None = None
api_active_requests = 0
hdtrans_backend: HdTransBackend | None = None
hdtrans_prepare_task: asyncio.Task | None = None
worker_opener: Callable[[], None] | None = None
worker_activator: Callable[[], None] | None = None
worker_interactive_opener: Callable[[], None] | None = None
worker_open_task: asyncio.Task[None] | None = None
shutdown_handler: Callable[[], None] | None = None
shutdown_token: str | None = None
TRANSLATOR_API_STARTUP_TIMEOUT_SECONDS = 10 * 60


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
    realtime: bool = False
    source: str | None = None
    target: str | None = None
    contextOwner: str | None = None
    resetContext: bool = False


class HdTransSettingsBody(BaseModel):
    preserveContext: bool = True
    userDictionaries: list[str] = []


class HdTransInstallBody(BaseModel):
    force: bool = False


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
        "backend": options.backend,
    }


def configure_worker_opener(opener: Callable[[], None] | None) -> None:
    global worker_opener
    worker_opener = opener


def configure_worker_activator(activator: Callable[[], None] | None) -> None:
    global worker_activator
    worker_activator = activator


def configure_worker_interactive_opener(opener: Callable[[], None] | None) -> None:
    global worker_interactive_opener
    worker_interactive_opener = opener


async def _open_interactive_worker(websocket: WebSocket, reason: str) -> bool:
    if worker_interactive_opener is None:
        return False
    debug("translator_api_manual_activation", reason)
    _clear_current_worker(websocket, reason)
    try:
        await asyncio.to_thread(worker_interactive_opener)
    except Exception as exc:
        state.error = str(exc) or type(exc).__name__
        error("translator_api_manual_activation", exc)
        return False
    return True


async def _open_worker_singleflight() -> None:
    """Coalesce concurrent timeout recovery and explicit reopen requests."""
    global worker_open_task
    if state.worker_connected:
        return
    if worker_opener is None:
        raise RuntimeError("worker opener is unavailable")
    if worker_open_task is None or worker_open_task.done():
        worker_open_task = asyncio.create_task(asyncio.to_thread(worker_opener))
    task = worker_open_task
    try:
        await asyncio.shield(task)
    finally:
        if worker_open_task is task and task.done():
            worker_open_task = None


def configure_shutdown_handler(
    handler: Callable[[], None] | None,
    token: str | None = None,
) -> None:
    global shutdown_handler, shutdown_token
    shutdown_handler = handler
    shutdown_token = token


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.casefold() == "localhost"


def _is_trusted_shutdown_origin(origin: str | None) -> bool:
    # Native callers such as MekiCopy do not send Origin. Browser POSTs do, so
    # only the HYTrans loopback origin may reach the token comparison.
    if origin is None:
        return True
    try:
        parsed = urlparse(origin)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and _is_loopback_host(parsed.hostname)
            and (parsed.port or (443 if parsed.scheme == "https" else 80))
            == options.port
        )
    except ValueError:
        return False


def _validate_api_caller(request: Request) -> None:
    if options.backend not in PROVIDERS:
        return
    client_host = request.client.host if request.client else ""
    if not _is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="translation API is limited to loopback clients")
    if not _is_trusted_shutdown_origin(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail="untrusted translation API origin")
    if request.headers.get("sec-fetch-site", "").casefold() == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site translation API requests are blocked")


def _clear_current_worker(websocket: WebSocket, reason: str) -> bool:
    """Move state to an error only when this websocket still owns the worker."""
    if not translation_queue.clear_worker(websocket, reason=reason):
        return False
    state.worker_connected = False
    state.worker_ready = False
    state.state = "ERROR"
    state.device = None
    state.device_detail = None
    state.warning = None
    state.error = reason
    return True


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


_LANGUAGE_NAMES = {
    "japanese": "ja",
    "korean": "ko",
    "english": "en",
    "chinese": "zh",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "russian": "ru",
}


def _translator_language_tag(value: str | None, *, default: str) -> str:
    raw = (default if value is None else value).strip()
    language = _LANGUAGE_NAMES.get(raw.casefold(), raw)
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language):
        raise HTTPException(status_code=400, detail="Translator API languages must use BCP 47 tags")
    return language


async def _translate_text(text: str, *, realtime: bool = False,
                          source: str | None = None, target: str | None = None,
                          context_owner: str = "shared", reset_context: bool = False) -> str:
    if options.backend == "hdtrans":
        return await _translate_native_text(text, source=source, target=target,
                                            context_owner=context_owner, reset_context=reset_context)
    if options.backend in PROVIDERS:
        return await _translate_api_text(text, source=source, target=target)
    source_language = target_language = None
    if options.backend == "translator_api":
        source_language = _translator_language_tag(source, default="ja")
        target_language = _translator_language_tag(target, default="ko")
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
        max_new_tokens = (
            realtime_max_new_tokens(len(text)) if realtime else MAX_NEW_TOKENS
        )
        result = await translation_queue.submit(
            text=text,
            timeout=translation_timeout_seconds(len(text)),
            max_new_tokens=max_new_tokens,
            source_language=source_language,
            target_language=target_language,
        )
        result = result.strip()
        if not result:
            raise RuntimeError("translation worker returned an empty result")
        state.state = "READY"
        state.error = None
        debug(
            "translation_success",
            (
                f"input_chars: {len(text)}\noutput_chars: {len(result)}\n"
                f"max_new_tokens: {max_new_tokens}\nrealtime: {realtime}"
            ),
        )
        return result
    except asyncio.TimeoutError:
        state.error = "translation timeout"
        if translation_queue.worker_ws is None:
            state.worker_connected = False
            state.worker_ready = False
            state.state = "WORKER_TIMEOUT"
            if worker_opener is not None:
                try:
                    await _open_worker_singleflight()
                    if not state.worker_connected:
                        state.state = "WORKER_STARTING"
                except Exception as exc:
                    state.state = "ERROR"
                    state.error = str(exc)
                    error("worker_timeout_reopen", exc)
        else:
            state.state = "READY"
        raise HTTPException(status_code=504, detail="translation timeout")
    except TranslationQueueOverloadedError as exc:
        # A bounded queue protects the game and companion processes from a
        # burst of retained request bodies.  The connected worker remains
        # healthy, so expose this as a retryable overload rather than a 500.
        state.state = "READY" if state.worker_ready else "ERROR"
        state.error = None
        debug("translation_overloaded", str(exc))
        raise HTTPException(status_code=429, detail="translation queue is busy; retry shortly") from exc
    except HTTPException:
        raise
    except Exception as exc:
        # A request-specific generation error does not make a connected model
        # permanently unhealthy. Fatal/disconnect paths clear worker_ready.
        state.state = "READY" if state.worker_ready else "ERROR"
        state.error = str(exc)
        error("translate", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _translate_api_text(text: str, *, source: str | None = None,
                              target: str | None = None) -> str:
    global api_active_requests
    if not state.worker_ready or api_translation_queue is None:
        raise HTTPException(status_code=503, detail=state.error or "translation API is not configured")
    for language in (source, target):
        if language is not None and (not language.strip() or len(language) > 100):
            raise HTTPException(status_code=400, detail="invalid source or target language")
    api_active_requests += 1
    state.state = "BUSY"
    try:
        # API reasoning models use output tokens for both reasoning and the
        # translation. Preserve the bulk budget even for short realtime input.
        result = await api_translation_queue.submit(
            text, source=(source or "Japanese").strip(),
            target=(target or "Korean").strip(), max_new_tokens=MAX_NEW_TOKENS,
        )
        state.error = None
        debug("api_translation_success", f"backend: {options.backend}\ninput_chars: {len(text)}\noutput_chars: {len(result)}")
        return result
    except TranslationQueueOverloadedError:
        raise HTTPException(status_code=429, detail="translation queue is busy; retry shortly") from None
    except ApiTranslationError as exc:
        state.error = str(exc)
        error("api_translate", str(exc))
        headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
        raise HTTPException(status_code=exc.status_code, detail=str(exc), headers=headers) from None
    except Exception:
        # Transport internals and credentials never reach HTTP errors or logs.
        state.error = "Translation API request failed"
        error("api_translate", state.error)
        raise HTTPException(status_code=502, detail=state.error) from None
    finally:
        api_active_requests -= 1
        state.state = "BUSY" if api_active_requests else "READY"


def _sync_hdtrans_state() -> dict[str, object]:
    snapshot = hdtrans_backend.status() if hdtrans_backend else {"ready": False, "state": "STARTING"}
    state.worker_ready = bool(snapshot.get("ready"))
    state.worker_connected = False
    state.state = str(snapshot.get("state", "STARTING")).upper()
    state.backend = "hdtrans"
    state.model = "hdTrans"
    state.dtype = "native"
    state.model_mode = "native"
    state.device = "cpu"
    state.device_detail = "hdTrans C API"
    state.error = snapshot.get("error")
    state.warning = snapshot.get("userDictionaryError") or None
    return snapshot


async def _initialize_hdtrans() -> None:
    assert hdtrans_backend is not None
    try:
        await hdtrans_backend.submit("initialize", timeout=None)
    except Exception as exc:
        error("hdtrans_initialize", exc)
    finally:
        _sync_hdtrans_state()


def _prepare_hdtrans() -> dict[str, object]:
    global hdtrans_prepare_task
    if hdtrans_backend is None:
        raise HTTPException(status_code=503, detail="hdTrans backend is unavailable")
    snapshot = _sync_hdtrans_state()
    if not snapshot.get("ready") and (hdtrans_prepare_task is None or hdtrans_prepare_task.done()):
        hdtrans_backend.cancel_install.clear()
        hdtrans_prepare_task = asyncio.create_task(_initialize_hdtrans())
    return {"ok": True, **snapshot}


async def _native_command(operation: str, *arguments: object, timeout: float | None = 120) -> object:
    if hdtrans_backend is None or options.backend != "hdtrans":
        raise HTTPException(status_code=409, detail="hdTrans is not the selected engine")
    try:
        return await hdtrans_backend.submit(operation, *arguments, timeout=timeout)
    except TranslationQueueOverloadedError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="hdTrans timeout; the native call is still completing safely") from exc
    except (HdTransError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503 if operation == "translate" else 400, detail=str(exc)) from exc
    finally:
        _sync_hdtrans_state()


async def _translate_native_text(text: str, *, source: str | None, target: str | None,
                                context_owner: str, reset_context: bool) -> str:
    if not _sync_hdtrans_state().get("ready"):
        raise HTTPException(status_code=503, detail=state.error or "hdTrans is not ready")
    for language, allowed in ((source, {"ja", "japanese"}), (target, {"ko", "korean"})):
        if language is not None and language.casefold() not in allowed:
            raise HTTPException(status_code=400, detail="hdTrans supports Japanese to Korean only")
    if not context_owner or len(context_owner) > 200 or any(ord(char) < 32 for char in context_owner):
        raise HTTPException(status_code=400, detail="invalid contextOwner")
    return await _native_command("translate", text, context_owner, reset_context,
                                 timeout=translation_timeout_seconds(len(text)))


def _validate_native_control(request: Request) -> None:
    host = request.client.host if request.client else ""
    if not _is_loopback_host(host) or not _is_trusted_shutdown_origin(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail="hdTrans control is limited to trusted loopback clients")
    if request.headers.get("sec-fetch-site", "").casefold() == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site hdTrans control requests are blocked")


@app.on_event("startup")
async def on_startup() -> None:
    global queue_task, translation_queue, worker_open_task, api_translation_queue, api_active_requests
    global hdtrans_backend, hdtrans_prepare_task
    # A FastAPI app object may be stopped and started again in the same
    # interpreter (embedded hosting and lifespan tests do this). ``stop``
    # permanently wakes the old queue, so each server lifetime needs a fresh
    # admission queue rather than reusing an object marked as stopped.
    translation_queue = TranslationQueue()
    worker_open_task = None
    state.state = "STARTING"
    state.backend = options.backend
    state.worker_ready = False
    state.worker_connected = False
    state.device = None
    state.device_detail = None
    state.model = None
    state.dtype = None
    state.model_mode = None
    state.warning = None
    state.error = None
    state.started_at = time.time()
    api_active_requests = 0
    api_translation_queue = None
    hdtrans_backend = None
    hdtrans_prepare_task = None
    if options.backend == "hdtrans":
        hdtrans_backend = HdTransBackend()
        queue_task = None
        _prepare_hdtrans()
        return
    if options.backend in PROVIDERS:
        profile = selected_api_profile()
        issue = validate_provider_settings(profile, options.backend) if profile else "API settings are unavailable"
        state.device = "api"
        state.device_detail = options.backend
        state.model = profile.model.strip() if profile else ""
        state.dtype = "api"
        state.model_mode = "api"
        state.error = issue
        state.worker_ready = issue is None
        state.state = "ERROR" if issue else "READY"
        queue_task = None
        if profile and issue is None:
            api_translation_queue = ApiTranslationQueue(ApiTranslator(options.backend, profile))
            api_translation_queue.start()
        debug("startup", f"backend: {options.backend}\nport: {options.port}")
        return
    queue_task = asyncio.create_task(
        translation_queue.run(default_max_new_tokens=MAX_NEW_TOKENS)
    )
    if options.backend == "translator_api":
        state.model = "Browser Translator API"
        state.dtype = "browser"
        state.model_mode = "on-device"
        debug("startup", f"backend: translator_api\nport: {options.port}")
        return
    profile = active_model_profile()
    debug("startup", f"model: {profile.model_id}\nport: {options.port}")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global queue_task, api_translation_queue
    global hdtrans_backend, hdtrans_prepare_task
    state.state = "STOPPING"
    if hdtrans_prepare_task is not None and not hdtrans_prepare_task.done():
        hdtrans_prepare_task.cancel()
        with suppress(asyncio.CancelledError):
            await hdtrans_prepare_task
    hdtrans_prepare_task = None
    if hdtrans_backend is not None:
        await hdtrans_backend.close()
        hdtrans_backend = None
    translation_queue.stop()
    if api_translation_queue is not None:
        await api_translation_queue.stop()
        api_translation_queue = None
    if queue_task:
        queue_task.cancel()
        with suppress(asyncio.CancelledError):
            await queue_task
        queue_task = None


@app.get("/health")
async def health(response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    profile = active_model_profile()
    if options.backend == "hdtrans":
        _sync_hdtrans_state()
    return {
        "ok": True,
        "app": "HYTrans",
        "server": "running",
        "state": state.state,
        "backend": options.backend,
        "workerConnected": state.worker_connected,
        "ready": state.worker_ready,
        "model": state.model if options.backend != "local" else profile.model_id,
        "dtype": ("api" if options.backend in PROVIDERS else
                  "native" if options.backend == "hdtrans" else
                  "browser" if options.backend == "translator_api" else profile.dtype),
        # The per-process capability is readable by native loopback clients.
        # Same-origin policy prevents an unrelated website from reading it,
        # while /shutdown additionally checks Origin and the custom header.
        "controlToken": shutdown_token,
    }


@app.post("/shutdown")
async def shutdown(request: Request) -> dict[str, object]:
    client_host = request.client.host if request.client else ""
    if not _is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="shutdown is limited to loopback clients")
    if not _is_trusted_shutdown_origin(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail="untrusted shutdown origin")
    if shutdown_handler is None or shutdown_token is None:
        raise HTTPException(status_code=503, detail="shutdown handler is unavailable")
    supplied_token = request.headers.get("x-hytrans-shutdown-token", "")
    if not supplied_token or not secrets.compare_digest(supplied_token, shutdown_token):
        raise HTTPException(status_code=403, detail="invalid shutdown token")
    asyncio.get_running_loop().call_later(0.1, shutdown_handler)
    return {"ok": True, "state": "STOPPING"}


@app.get("/ready")
async def ready() -> dict[str, object]:
    if options.backend == "hdtrans":
        _sync_hdtrans_state()
    return state.as_ready_payload()


@app.post("/worker/reopen")
async def reopen_worker() -> dict[str, object]:
    if options.backend not in BROWSER_BACKENDS:
        return {"ok": state.worker_ready, "workerConnected": False,
                "backend": options.backend, "state": state.state}
    if state.worker_connected:
        return {"ok": True, "workerConnected": True, "state": state.state}
    if worker_opener is None:
        raise HTTPException(status_code=503, detail="worker opener is unavailable")
    try:
        state.state = "WORKER_STARTING"
        state.error = None
        await _open_worker_singleflight()
        return {
            "ok": True,
            "workerConnected": state.worker_connected,
            "state": state.state,
        }
    except Exception as exc:
        state.state = "ERROR"
        state.error = str(exc)
        error("worker_reopen", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/config")
async def get_config() -> dict[str, object]:
    config = await asyncio.to_thread(runtime_config)
    return config.model_dump()


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
    return await asyncio.to_thread(model_cache_status, url)


@app.get("/model-cache")
async def get_model_cache_file(url: str) -> FileResponse:
    return await asyncio.to_thread(model_cache_file_response, url)


@app.post("/model-cache")
async def post_model_cache_file(url: str, request: Request) -> dict[str, object]:
    return await save_model_cache_file(url, request)


@app.post("/model/prepare")
async def prepare_model() -> dict[str, object]:
    if options.backend == "hdtrans":
        return _prepare_hdtrans()
    if options.backend == "translator_api":
        return {"ok": state.worker_ready, "state": state.state,
                "backend": options.backend, "model": state.model}
    if options.backend in PROVIDERS:
        return {"ok": True, "state": "API", "backend": options.backend, "model": state.model}
    return await asyncio.to_thread(model_download_manager.start)


@app.get("/model/status")
async def model_status() -> dict[str, object]:
    if options.backend == "hdtrans":
        return _sync_hdtrans_state()
    if options.backend == "translator_api":
        return {"state": state.state, "complete": state.worker_ready,
                "backend": options.backend, "model": state.model}
    if options.backend in PROVIDERS:
        return {"state": "API", "complete": True, "backend": options.backend, "model": state.model}
    return await asyncio.to_thread(model_download_manager.status)


@app.get("/worker.html")
async def worker_html() -> FileResponse:
    return FileResponse(assets_dir() / "worker.html")


@app.get("/hdtrans/status")
async def hdtrans_status() -> dict[str, object]:
    if options.backend == "hdtrans":
        return _sync_hdtrans_state()
    return {"backend": "hdtrans", "ready": False, "state": "INACTIVE",
            "settings": load_hdtrans_settings().as_payload(), "singleSession": True,
            "reloadResetsContext": True}


@app.put("/hdtrans/settings")
async def hdtrans_settings(body: HdTransSettingsBody, request: Request) -> object:
    _validate_native_control(request)
    return await _native_command("settings", HdTransSettings(body.preserveContext, body.userDictionaries))


@app.post("/hdtrans/context/reset")
async def hdtrans_reset(request: Request) -> object:
    _validate_native_control(request)
    return await _native_command("reset")


@app.post("/hdtrans/reload")
async def hdtrans_reload(request: Request) -> object:
    _validate_native_control(request)
    return await _native_command("reload")


@app.post("/hdtrans/install")
async def hdtrans_install(body: HdTransInstallBody, request: Request) -> dict[str, object]:
    _validate_native_control(request)
    if options.backend != "hdtrans":
        raise HTTPException(status_code=409, detail="hdTrans is not the selected engine")
    if body.force and hdtrans_backend is not None and hdtrans_backend.status().get("ready"):
        raise HTTPException(status_code=409, detail="Restart HYTrans before replacing a loaded hdTrans DLL")
    return _prepare_hdtrans()


@app.post("/hdtrans/download/cancel")
async def hdtrans_cancel(request: Request) -> dict[str, object]:
    _validate_native_control(request)
    if hdtrans_backend is not None:
        hdtrans_backend.cancel_install.set()
    return {"ok": True, "phase": "취소 요청됨; 진행 중인 네이티브 호출은 완료 후 정리됩니다."}


@app.get("/hdtrans/updates")
async def hdtrans_updates() -> dict[str, object]:
    from .hdtrans_download import check_for_updates
    try:
        return await asyncio.to_thread(check_for_updates)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"hdTrans 업데이트 확인 실패: {exc}") from exc


@app.get("/translator_api_worker.html")
async def translator_api_worker_html() -> FileResponse:
    return FileResponse(assets_dir() / "translator_api_worker.html")


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
    request: Request,
    text: str = "",
    format: str = "text",
    source: str | None = None,
    target: str | None = None,
    contextOwner: str | None = None,
    resetContext: bool = False,
) -> PlainTextResponse | dict[str, object]:
    _validate_api_caller(request)
    clean_text = _validate_text(text)
    result = await _translate_text(clean_text, source=source, target=target,
                                   context_owner=contextOwner or "shared", reset_context=resetContext)
    if format == "json":
        return _json_response(True, result)
    return PlainTextResponse(result, media_type="text/plain; charset=utf-8")


@app.post("/translate", response_model=None)
async def translate_post(
    body: TranslateBody,
    request: Request,
    format: str = "text",
) -> PlainTextResponse | dict[str, object]:
    _validate_api_caller(request)
    clean_text = _validate_text(body.text)
    result = await _translate_text(clean_text, realtime=body.realtime, source=body.source, target=body.target,
                                   context_owner=body.contextOwner or "shared", reset_context=body.resetContext)
    if format == "json":
        return _json_response(True, result)
    return PlainTextResponse(result, media_type="text/plain; charset=utf-8")


@app.post("/translate-and-show")
async def translate_and_show(body: TranslateBody, request: Request) -> dict[str, object]:
    _validate_api_caller(request)
    clean_text = _validate_text(body.text)
    result = await _translate_text(clean_text, realtime=body.realtime, source=body.source, target=body.target,
                                   context_owner=body.contextOwner or "overlay", reset_context=body.resetContext)
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
    if options.backend not in BROWSER_BACKENDS:
        await websocket.close(code=1008)
        return
    if not _is_trusted_shutdown_origin(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    translation_queue.set_worker(websocket)
    state.worker_connected = True
    state.worker_ready = False
    state.state = "WORKER_CONNECTED"
    state.device = None
    state.device_detail = None
    state.warning = None
    state.error = None
    debug("worker_connected", "websocket connected")
    startup_deadline = (
        time.monotonic() + TRANSLATOR_API_STARTUP_TIMEOUT_SECONDS
        if options.backend == "translator_api" else None
    )

    try:
        while True:
            if startup_deadline is not None and not state.worker_ready:
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=max(0, startup_deadline - time.monotonic()),
                    )
                except asyncio.TimeoutError:
                    detail = "Translator API worker did not become ready within 10 minutes"
                    if _clear_current_worker(websocket, detail):
                        error("translator_api_startup_timeout", detail)
                    await websocket.close(code=1011)
                    return
            else:
                raw = await websocket.receive_text()
            if not translation_queue.is_current_worker(websocket):
                debug("worker_stale", "ignoring message from replaced websocket")
                return
            message = json.loads(raw)
            msg_type = message.get("type")

            if msg_type == "loading":
                state.state = "WORKER_LOADING"
                debug("worker_loading", str(message.get("message", "")))

            elif msg_type == "activation_required" and options.backend == "translator_api":
                activation_error = None
                if worker_activator is None:
                    activation_error = "browser activator is unavailable"
                else:
                    try:
                        await asyncio.to_thread(worker_activator)
                    except Exception as exc:
                        activation_error = str(exc) or type(exc).__name__
                if activation_error:
                    error("translator_api_activation", activation_error)
                    if await _open_interactive_worker(websocket, activation_error):
                        return
                    if translation_queue.is_current_worker(websocket):
                        await websocket.send_text(json.dumps({
                            "type": "activation_error",
                            "message": activation_error,
                            "sourceLanguage": message.get("sourceLanguage"),
                            "targetLanguage": message.get("targetLanguage"),
                        }))

            elif msg_type == "manual_activation_required" and options.backend == "translator_api":
                detail = str(message.get("message") or "a browser click is required")
                if await _open_interactive_worker(websocket, detail):
                    return
                if translation_queue.is_current_worker(websocket):
                    await websocket.send_text(json.dumps({
                        "type": "activation_error",
                        "message": detail,
                        "sourceLanguage": message.get("sourceLanguage"),
                        "targetLanguage": message.get("targetLanguage"),
                    }))

            elif msg_type == "ready":
                reported_model = str(message.get("model") or "")
                reported_dtype = str(message.get("dtype") or "")
                if options.backend == "translator_api":
                    expected_model, expected_dtype = "Browser Translator API", "browser"
                else:
                    profile = active_model_profile()
                    expected_model, expected_dtype = profile.model_id, profile.dtype
                if (
                    reported_model != expected_model
                    or reported_dtype != expected_dtype
                ):
                    detail = (
                        "worker profile mismatch: "
                        f"expected {expected_model}/{expected_dtype}, "
                        f"received {reported_model}/{reported_dtype}"
                    )
                    _clear_current_worker(websocket, detail)
                    error("worker_profile_mismatch", detail)
                    await websocket.close(code=1008)
                    return
                state.worker_ready = True
                state.state = "READY"
                state.device = message.get("device")
                state.device_detail = message.get("deviceDetail")
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
                detail = str(message.get("message", "worker fatal error"))
                if _clear_current_worker(websocket, detail):
                    error("worker_fatal", detail)
                else:
                    debug("worker_stale_fatal", detail)
                try:
                    await websocket.close(code=1011)
                except RuntimeError:
                    pass
                return

    except WebSocketDisconnect:
        if _clear_current_worker(websocket, "worker disconnected"):
            debug("worker_disconnected", "websocket disconnected")
        else:
            debug("worker_stale_disconnected", "replaced websocket disconnected")
    except Exception as exc:
        if _clear_current_worker(websocket, str(exc)):
            error("worker_websocket", exc)
            raise
        debug("worker_stale_error", str(exc))


if assets_dir().exists():
    app.mount("/assets", StaticFiles(directory=assets_dir()), name="assets")
