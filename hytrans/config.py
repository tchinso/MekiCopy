from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path

from pydantic import BaseModel

from service_ports import HYTRANS_DEFAULT_PORT, OVERLAYER_DEFAULT_PORT

from .model_files import (
    DEFAULT_MODEL_ID,
    MT2_PROFILE,
    ModelProfile,
    active_model_profile,
    configure_model,
    is_complete_model,
    required_model_file_sizes,
)
from .paths import assets_dir, models_dir

DTYPE = MT2_PROFILE.dtype
REQUIRED_MODEL_FILES = tuple(MT2_PROFILE.files)
# Compatibility for callers that used the old constant. It describes only the
# default model; runtime code below always reads the active profile dynamically.
REQUIRED_Q4_MODEL_FILES = REQUIRED_MODEL_FILES
SOURCE_LANG = "Japanese"
TARGET_LANG = "Korean"
MAX_NEW_TOKENS = 2048
HOST = "127.0.0.1"
DEFAULT_PORT = HYTRANS_DEFAULT_PORT
DEFAULT_OVERLAY_URL = f"http://127.0.0.1:{OVERLAYER_DEFAULT_PORT}/show"
# Kept for compatibility with external imports. Runtime requests use
# translation_timeout_seconds(), which gives the larger MT2 model more time and
# scales conservatively for long input instead of repeatedly killing a healthy,
# merely slow browser worker.
TRANSLATE_TIMEOUT_SECONDS = 120
MT2_TRANSLATE_TIMEOUT_SECONDS = 240
MAX_TRANSLATE_TIMEOUT_SECONDS = 600
MAX_INPUT_CHARS = 8000


def _configured_translate_timeout() -> int | None:
    raw = os.environ.get("HYTRANS_TRANSLATE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return None
    try:
        return min(MAX_TRANSLATE_TIMEOUT_SECONDS, max(30, int(raw)))
    except ValueError:
        return None


def translation_timeout_seconds(input_chars: int) -> int:
    """Return a bounded timeout suited to the active model and request size."""

    configured = _configured_translate_timeout()
    base = configured or (
        MT2_TRANSLATE_TIMEOUT_SECONDS
        if selected_model_profile().key == "mt2"
        else TRANSLATE_TIMEOUT_SECONDS
    )
    # Tokenization and generation cost grows with input size. Add at most three
    # minutes so malformed or stuck workers are still eventually recycled.
    input_allowance = min(180, math.ceil(max(0, input_chars) / 50))
    return min(MAX_TRANSLATE_TIMEOUT_SECONDS, base + input_allowance)


class RuntimeConfig(BaseModel):
    modelKey: str
    modelId: str
    revision: str
    dtype: str
    promptTemplate: str
    modelMode: str
    modelFiles: dict[str, int]
    source: str = SOURCE_LANG
    target: str = TARGET_LANG
    maxNewTokens: int = MAX_NEW_TOKENS
    hasLocalWasm: bool = False
    debugLog: bool = False


@dataclass
class ServerOptions:
    host: str = HOST
    port: int = DEFAULT_PORT
    overlay_url: str = DEFAULT_OVERLAY_URL
    debug_log: bool = False
    model_id: str = DEFAULT_MODEL_ID


options = ServerOptions()


def configure_server(
    *,
    host: str = HOST,
    port: int = DEFAULT_PORT,
    overlay_url: str = DEFAULT_OVERLAY_URL,
    debug_log: bool = False,
    model_id: str = DEFAULT_MODEL_ID,
) -> None:
    profile = configure_model(model_id)
    options.host = host
    options.port = port
    options.overlay_url = overlay_url
    options.debug_log = debug_log
    options.model_id = profile.key


def selected_model_profile() -> ModelProfile:
    return active_model_profile()


def _model_path(profile: ModelProfile) -> Path:
    return models_dir().joinpath(*profile.model_id.split("/"))


def detect_model_mode() -> str:
    profile = selected_model_profile()
    if is_complete_model(_model_path(profile), profile):
        return "local"
    return "remote"


def has_local_wasm_files() -> bool:
    wasm_dir = assets_dir() / "wasm"
    if not wasm_dir.exists():
        return False
    return any(path.suffix == ".wasm" for path in wasm_dir.iterdir())


def runtime_config() -> RuntimeConfig:
    profile = selected_model_profile()
    return RuntimeConfig(
        modelKey=profile.key,
        modelId=profile.model_id,
        revision=profile.revision,
        dtype=profile.dtype,
        promptTemplate=profile.prompt,
        modelMode=(
            "local"
            if is_complete_model(_model_path(profile), profile)
            else "remote"
        ),
        modelFiles=required_model_file_sizes(profile),
        hasLocalWasm=has_local_wasm_files(),
        debugLog=options.debug_log,
    )
