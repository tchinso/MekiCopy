"""Persistent hdTrans options, independent from installed engine versions.

Only the option and ordered official dictionary paths are persisted. The
native engine's learned names/context always stay in its in-memory session.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
import threading
import uuid

from runtime_paths import state_data_dir

_WRITE_LOCK = threading.RLock()
_PATH_OVERRIDE: Path | None = None


def configure_hdtrans_settings_path(path: str | Path | None) -> None:
    global _PATH_OVERRIDE
    _PATH_OVERRIDE = Path(path).expanduser().resolve() if path else None


@dataclass
class HdTransSettings:
    preserve_context: bool = True
    user_dictionaries: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, object]:
        return {"preserveContext": self.preserve_context,
                "userDictionaries": list(self.user_dictionaries)}


def hdtrans_settings_path() -> Path:
    if _PATH_OVERRIDE is not None:
        return _PATH_OVERRIDE
    module = sys.modules.get("mekicopy_settings")
    settings_file = getattr(module, "SETTINGS_FILE", None)
    root = Path(settings_file).parent if settings_file else state_data_dir("MekiCopy")
    return root / "hdtrans_settings.json"


def normalize_settings(settings: HdTransSettings) -> HdTransSettings:
    paths: list[str] = []
    for value in settings.user_dictionaries:
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError("사용자 사전 경로가 올바르지 않습니다.")
        path = str(Path(value).expanduser().resolve())
        if path not in paths:
            paths.append(path)
    if len(paths) > 100:
        raise ValueError("사용자 사전은 최대 100개까지 등록할 수 있습니다.")
    return HdTransSettings(bool(settings.preserve_context), paths)


def load_hdtrans_settings(path: str | Path | None = None) -> HdTransSettings:
    try:
        payload = json.loads(Path(path or hdtrans_settings_path()).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return HdTransSettings()
        paths = payload.get("userDictionaries", [])
        return normalize_settings(HdTransSettings(
            preserve_context=payload.get("preserveContext", True) is not False,
            user_dictionaries=paths if isinstance(paths, list) else [],
        ))
    except (OSError, ValueError, TypeError, UnicodeError):
        return HdTransSettings()


def save_hdtrans_settings(settings: HdTransSettings, path: str | Path | None = None) -> bool:
    destination = Path(path or hdtrans_settings_path())
    temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        normalized = normalize_settings(settings)
        payload = {"version": 1, **normalized.as_payload()}
        with _WRITE_LOCK:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        return True
    except (OSError, ValueError, TypeError, UnicodeError):
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
