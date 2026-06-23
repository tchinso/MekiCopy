from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from service_ports import HYTRANS_DEFAULT_PORT, OVERLAYER_DEFAULT_PORT

from .model_files import (
    MODEL_FILE_SPECS,
    MODEL_ID,
    MODEL_REVISION,
    is_complete_model,
    required_model_file_sizes,
)
from .paths import assets_dir, models_dir

DTYPE = "q4"
REQUIRED_Q4_MODEL_FILES = tuple(MODEL_FILE_SPECS)
SOURCE_LANG = "Japanese"
TARGET_LANG = "Korean"
MAX_NEW_TOKENS = 2048
HOST = "127.0.0.1"
DEFAULT_PORT = HYTRANS_DEFAULT_PORT
DEFAULT_OVERLAY_URL = f"http://127.0.0.1:{OVERLAYER_DEFAULT_PORT}/show"
TRANSLATE_TIMEOUT_SECONDS = 120
MAX_INPUT_CHARS = 8000


class RuntimeConfig(BaseModel):
    modelId: str = MODEL_ID
    revision: str = MODEL_REVISION
    dtype: str = DTYPE
    modelMode: str
    modelFiles: dict[str, int]
    source: str = SOURCE_LANG
    target: str = TARGET_LANG
    maxNewTokens: int = MAX_NEW_TOKENS
    hasLocalWasm: bool = False


@dataclass
class ServerOptions:
    host: str = HOST
    port: int = DEFAULT_PORT
    overlay_url: str = DEFAULT_OVERLAY_URL
    debug_log: bool = False


options = ServerOptions()


def configure_server(
    *,
    host: str = HOST,
    port: int = DEFAULT_PORT,
    overlay_url: str = DEFAULT_OVERLAY_URL,
    debug_log: bool = False,
) -> None:
    options.host = host
    options.port = port
    options.overlay_url = overlay_url
    options.debug_log = debug_log


def detect_model_mode() -> str:
    model_path = models_dir().joinpath(*MODEL_ID.split("/"))
    if is_complete_model(model_path):
        return "local"
    return "remote"


def has_local_wasm_files() -> bool:
    wasm_dir = assets_dir() / "wasm"
    if not wasm_dir.exists():
        return False
    return any(path.suffix == ".wasm" for path in wasm_dir.iterdir())


def runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        modelMode=detect_model_mode(),
        modelFiles=required_model_file_sizes(),
        hasLocalWasm=has_local_wasm_files(),
    )
