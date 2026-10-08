from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid

from runtime_paths import state_data_dir


BACKEND_LABELS = {
    "local": "HY-MT1.5 (로컬)",
    "cloudflare": "Cloudflare Workers AI",
    "deepinfra": "DeepInfra",
    "groq": "Groq",
}
PROVIDERS = tuple(key for key in BACKEND_LABELS if key != "local")
DEFAULT_PROMPT = (
    "Translate the following {source} text into {target}. Preserve its meaning, "
    "tone, names, and line breaks. Return only the translated text, without "
    "explanation, notes, reasoning, or quotation marks.\n\n{text}"
)
# Editable starting suggestions, checked against each provider's official
# catalog on 2026-10-08. They are deliberately not a whitelist.
DEFAULT_MODELS = {
    "cloudflare": (
        "@cf/qwen/qwen3-30b-a3b-fp8",
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    ),
    "deepinfra": ("deepseek-ai/DeepSeek-V4-Flash-0731", "Qwen/Qwen3-32B"),
    "groq": ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"),
}
_PLACEHOLDER_RE = re.compile(r"\{(source|target|text)\}")
_WRITE_LOCK = threading.RLock()


def normalize_backend(value: str | None) -> str:
    key = str(value or "").strip().lower()
    return key if key in BACKEND_LABELS else "local"


@dataclass
class ProviderSettings:
    api_key: str = field(default="", repr=False)
    account_id: str = ""
    model: str = ""
    models: list[str] = field(default_factory=list)
    prompt: str = DEFAULT_PROMPT
    credential_error: str | None = field(default=None, repr=False)


def _default_profiles() -> dict[str, ProviderSettings]:
    return {
        provider: ProviderSettings(
            model=models[0], models=list(models),
            prompt=DEFAULT_PROMPT + ("\n/no_think" if provider == "cloudflare" else ""),
        )
        for provider, models in DEFAULT_MODELS.items()
    }


@dataclass
class ApiSettings:
    profiles: dict[str, ProviderSettings] = field(default_factory=_default_profiles)


def api_settings_path() -> Path:
    # Settings can move to a writable fallback after a concrete file save
    # failure. Honor that actual location without importing the Tk application
    # into an otherwise independent HYTrans API process.
    module = sys.modules.get("mekicopy_settings")
    settings_file = getattr(module, "SETTINGS_FILE", None)
    if settings_file:
        return Path(settings_file).parent / "translation_api.json"
    return state_data_dir("MekiCopy") / "translation_api.json"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise OSError("Windows DPAPI is required to save API credentials")
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _DataBlob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN prevents a credential operation from opening
    # a desktop prompt. Encryption is bound to the current Windows user.
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        success = function(ctypes.byref(source), None, None, None, None, 1,
                           ctypes.byref(output))
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        success = function(ctypes.byref(source), "HYTrans API credential", None,
                           None, None, 1, ctypes.byref(output))
    if not success:
        raise OSError("Windows could not protect or unlock the API credential")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def protect_api_key(api_key: str) -> dict[str, str] | None:
    if not api_key:
        return None
    protected = _dpapi(api_key.encode("utf-8"), decrypt=False)
    return {"scheme": "windows-dpapi", "value": base64.b64encode(protected).decode("ascii")}


def _unprotect_api_key(value: object) -> tuple[str, str | None]:
    if value is None or value == "":
        return "", None
    if not isinstance(value, dict) or value.get("scheme") != "windows-dpapi":
        return "", "저장된 API 키 형식이 올바르지 않습니다. 키를 다시 입력하세요."
    try:
        data = base64.b64decode(value.get("value", ""), validate=True)
        return _dpapi(data, decrypt=True).decode("utf-8"), None
    except (OSError, ValueError, TypeError, UnicodeError):
        return "", "이 Windows 사용자로 저장된 API 키를 열 수 없습니다. 키를 다시 입력하세요."


def _string(value: object, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def load_api_settings(path: str | Path | None = None) -> ApiSettings:
    settings = ApiSettings()
    try:
        payload = json.loads(Path(path or api_settings_path()).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return settings
    except (OSError, ValueError, UnicodeError):
        for profile in settings.profiles.values():
            profile.credential_error = "API 설정 파일을 읽을 수 없습니다. 설정을 다시 저장하세요."
        return settings
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        for profile in settings.profiles.values():
            profile.credential_error = "API 설정 파일 형식이 올바르지 않습니다."
        return settings
    for provider, profile in settings.profiles.items():
        saved = payload["profiles"].get(provider)
        if not isinstance(saved, dict):
            continue
        profile.api_key, profile.credential_error = _unprotect_api_key(saved.get("api_key"))
        profile.account_id = _string(saved.get("account_id"))
        profile.model = _string(saved.get("model"), profile.model)
        profile.prompt = _string(saved.get("prompt"), profile.prompt)
        if isinstance(saved.get("models"), list):
            profile.models = list(dict.fromkeys(
                model.strip() for model in saved["models"]
                if isinstance(model, str) and model.strip()
            ))
    return settings


def save_api_settings(settings: ApiSettings, path: str | Path | None = None) -> bool:
    destination = Path(path or api_settings_path())
    temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        profiles = {}
        for provider in PROVIDERS:
            profile = settings.profiles[provider]
            profiles[provider] = {
                "api_key": protect_api_key(profile.api_key.strip()),
                "account_id": profile.account_id.strip(),
                "model": profile.model.strip(),
                "models": list(dict.fromkeys(model.strip() for model in profile.models if model.strip())),
                "prompt": profile.prompt,
            }
        text = json.dumps({"version": 1, "profiles": profiles}, ensure_ascii=False, indent=2) + "\n"
        with _WRITE_LOCK:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        return True
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def validate_provider_settings(profile: ProviderSettings, provider: str) -> str | None:
    if provider not in PROVIDERS:
        return "지원하지 않는 API 서비스입니다."
    if profile.credential_error and not profile.api_key.strip():
        return profile.credential_error
    if not profile.api_key.strip():
        return "API 키를 입력하세요."
    if any(ord(char) < 33 or ord(char) > 126 for char in profile.api_key.strip()):
        return "API 키에 공백 또는 올바르지 않은 문자가 있습니다."
    if provider == "cloudflare" and not re.fullmatch(r"[a-fA-F0-9]{32}", profile.account_id.strip()):
        return "Cloudflare Account ID (32자리 영숫자)를 입력하세요."
    if not profile.model.strip():
        return "모델 ID를 입력하세요."
    if len(profile.model) > 512 or any(ord(char) < 32 for char in profile.model):
        return "모델 ID 형식이 올바르지 않습니다."
    if not profile.prompt.strip():
        return "번역 프롬프트를 입력하세요."
    if "{text}" not in profile.prompt:
        return "번역 프롬프트에 {text}를 포함하세요."
    if len(profile.prompt) > 32_000:
        return "번역 프롬프트가 너무 깁니다."
    return None


def render_prompt(template: str, *, source: str, target: str, text: str) -> str:
    replacements = {"source": source, "target": target, "text": text}
    return _PLACEHOLDER_RE.sub(lambda match: replacements[match.group(1)], template)


def fingerprint(settings: ApiSettings, backend: str) -> str:
    backend = normalize_backend(backend)
    if backend == "local":
        return "local"
    profile = settings.profiles[backend]
    # Kept only in the controller's memory for restart decisions; this digest
    # is never published through HTTP, logs, or the command line.
    values = [backend, profile.api_key.strip(), profile.account_id.strip(),
              profile.model.strip(), list(profile.models), profile.prompt]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()
