from __future__ import annotations

import os
import hashlib
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from .model_files import (
    MODEL_FILE_SPECS,
    MODEL_ID,
    is_verified_model_file,
    record_verified_model_file,
)
from .logging_setup import debug, error
from .paths import models_dir

MODEL_REVISION_SEGMENTS = {"resolve", "raw"}


def model_dir() -> Path:
    return models_dir().joinpath(*MODEL_ID.split("/"))


def _is_safe_relative_path(relative_path: str) -> bool:
    if not relative_path:
        return False
    path = Path(relative_path)
    return not path.is_absolute() and ".." not in path.parts


def _relative_path_from_parts(parts: list[str]) -> str | None:
    model_parts = MODEL_ID.split("/")
    for index in range(0, len(parts) - len(model_parts) + 1):
        if parts[index : index + len(model_parts)] != model_parts:
            continue

        remainder = parts[index + len(model_parts) :]
        if not remainder:
            return None

        if remainder[0] in MODEL_REVISION_SEGMENTS and len(remainder) >= 3:
            relative_parts = remainder[2:]
        else:
            relative_parts = remainder

        relative_path = "/".join(relative_parts)
        return relative_path if _is_safe_relative_path(relative_path) else None

    return None


def model_relative_path_from_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    path = unquote(parsed.path if parsed.scheme else raw_url)
    path = path.replace("\\", "/").strip("/")
    parts = [part for part in path.split("/") if part]
    relative_path = _relative_path_from_parts(parts)
    if not relative_path:
        raise HTTPException(status_code=400, detail="url is not a HYTrans model file")
    return relative_path


def cached_model_file(raw_url: str) -> Path:
    relative_path = model_relative_path_from_url(raw_url)
    return model_dir().joinpath(*relative_path.split("/"))


def model_cache_status(raw_url: str) -> dict[str, object]:
    relative_path = model_relative_path_from_url(raw_url)
    target = cached_model_file(raw_url)
    if not is_verified_model_file(model_dir(), relative_path):
        payload: dict[str, object] = {"ok": True, "exists": False}
        if target.is_file():
            payload["invalid"] = True
            payload["size"] = target.stat().st_size
        spec = MODEL_FILE_SPECS.get(relative_path)
        if spec:
            payload["expectedSize"] = spec.size
        return payload
    return {
        "ok": True,
        "exists": True,
        "size": target.stat().st_size,
    }


def model_cache_file_response(raw_url: str) -> FileResponse:
    relative_path = model_relative_path_from_url(raw_url)
    target = cached_model_file(raw_url)
    if not is_verified_model_file(model_dir(), relative_path):
        raise HTTPException(status_code=404, detail="model file is not cached")
    return FileResponse(target)


async def save_model_cache_file(raw_url: str, request: Request) -> dict[str, object]:
    relative_path = model_relative_path_from_url(raw_url)
    target = cached_model_file(raw_url)
    target.parent.mkdir(parents=True, exist_ok=True)
    spec = MODEL_FILE_SPECS.get(relative_path)
    request_size = int(request.headers.get("content-length", "0") or 0)
    expected_size = spec.size if spec else request_size
    if is_verified_model_file(model_dir(), relative_path):
        current_size = target.stat().st_size
        return {
            "ok": True,
            "path": relative_path,
            "bytes": current_size,
            "reused": True,
        }

    temp_target = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")

    total = 0
    digest = hashlib.sha256()
    try:
        with temp_target.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                digest.update(chunk)
                handle.write(chunk)
        if total <= 0:
            raise ValueError("refusing to cache an empty model file")
        if expected_size > 0 and total != expected_size:
            raise ValueError(
                f"incomplete model file: expected {expected_size} bytes, received {total}"
            )
        actual_digest = digest.hexdigest()
        if spec and actual_digest != spec.sha256:
            raise ValueError("model file checksum mismatch")
        os.replace(temp_target, target)
        if spec:
            record_verified_model_file(
                model_dir(),
                relative_path,
                digest=actual_digest,
            )
        debug("model_cache_save", f"{relative_path}\nbytes: {total}")
        return {"ok": True, "path": relative_path, "bytes": total}
    except Exception as exc:
        try:
            temp_target.unlink(missing_ok=True)
        except OSError:
            pass
        error("model_cache_save", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
