from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .paths import assets_dir, models_dir

MODEL_ID = "onnx-community/HY-MT1.5-1.8B-ONNX"
DTYPE = "q4"
SOURCE_LANG = "Japanese"
TARGET_LANG = "Korean"
MAX_NEW_TOKENS = 2048
HOST = "127.0.0.1"
DEFAULT_PORT = 6550
DEFAULT_OVERLAY_URL = "http://127.0.0.1:6551/show"
TRANSLATE_TIMEOUT_SECONDS = 120
MAX_INPUT_CHARS = 8000


class RuntimeConfig(BaseModel):
    modelId: str = MODEL_ID
    dtype: str = DTYPE
    modelMode: str
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
    onnx_dir = model_path / "onnx"
    required = [
        model_path / "config.json",
        model_path / "tokenizer.json",
    ]
    has_onnx_file = onnx_dir.exists() and any(
        path.suffix == ".onnx" for path in onnx_dir.rglob("*")
    )
    if all(path.exists() for path in required) and has_onnx_file:
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
        hasLocalWasm=has_local_wasm_files(),
    )
