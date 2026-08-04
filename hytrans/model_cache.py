from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import unquote, urlparse

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from .logging_setup import debug, error
from .model_files import (
    ModelProfile,
    active_model_profile,
    is_verified_model_file,
    record_verified_model_file,
)
from .paths import models_dir

MODEL_REVISION_SEGMENTS = {"resolve", "raw"}


def _write_and_hash_chunk(handle: BinaryIO, digest: Any, chunk: bytes) -> None:
    """Perform potentially large disk and hashing work outside the event loop."""

    handle.write(chunk)
    digest.update(chunk)


def model_dir(profile: ModelProfile | None = None) -> Path:
    selected = profile or active_model_profile()
    return models_dir().joinpath(*selected.model_id.split("/"))


def _is_safe_relative_path(relative_path: str) -> bool:
    if not relative_path or "\\" in relative_path or "\x00" in relative_path:
        return False
    path = PurePosixPath(relative_path)
    return (
        not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _relative_path_from_parts(
    parts: list[str],
    profile: ModelProfile,
) -> str | None:
    model_parts = profile.model_id.split("/")
    for index in range(0, len(parts) - len(model_parts) + 1):
        if parts[index : index + len(model_parts)] != model_parts:
            continue

        remainder = parts[index + len(model_parts) :]
        if not remainder:
            return None

        if remainder[0] in MODEL_REVISION_SEGMENTS:
            if len(remainder) < 3 or remainder[1] != profile.revision:
                return None
            relative_parts = remainder[2:]
        else:
            relative_parts = remainder

        relative_path = "/".join(relative_parts)
        if not _is_safe_relative_path(relative_path):
            return None
        # Never accept arbitrary files merely because a URL contains the model
        # repository name. Only pinned, checksummed profile files are cacheable.
        return relative_path if relative_path in profile.files else None

    return None


def model_relative_path_from_url(
    raw_url: str,
    profile: ModelProfile | None = None,
) -> str:
    selected = profile or active_model_profile()
    parsed = urlparse(raw_url)
    path = unquote(parsed.path if parsed.scheme else raw_url)
    path = path.replace("\\", "/").strip("/")
    parts = [part for part in path.split("/") if part]
    relative_path = _relative_path_from_parts(parts, selected)
    if not relative_path:
        raise HTTPException(status_code=400, detail="url is not a pinned HYTrans model file")
    return relative_path


def cached_model_file(
    raw_url: str,
    profile: ModelProfile | None = None,
) -> Path:
    selected = profile or active_model_profile()
    relative_path = model_relative_path_from_url(raw_url, selected)
    return model_dir(selected).joinpath(*relative_path.split("/"))


def model_cache_status(raw_url: str) -> dict[str, object]:
    profile = active_model_profile()
    relative_path = model_relative_path_from_url(raw_url, profile)
    root = model_dir(profile)
    target = root.joinpath(*relative_path.split("/"))
    spec = profile.files[relative_path]
    if not is_verified_model_file(root, relative_path, profile):
        payload: dict[str, object] = {
            "ok": True,
            "exists": False,
            "expectedSize": spec.size,
            "modelKey": profile.key,
            "modelId": profile.model_id,
        }
        try:
            if target.is_file():
                payload["invalid"] = True
                payload["size"] = target.stat().st_size
        except OSError:
            pass
        return payload
    return {
        "ok": True,
        "exists": True,
        "size": target.stat().st_size,
        "modelKey": profile.key,
        "modelId": profile.model_id,
    }


def model_cache_file_response(raw_url: str) -> FileResponse:
    profile = active_model_profile()
    relative_path = model_relative_path_from_url(raw_url, profile)
    root = model_dir(profile)
    target = root.joinpath(*relative_path.split("/"))
    if not is_verified_model_file(root, relative_path, profile):
        raise HTTPException(status_code=404, detail="model file is not cached")
    response = FileResponse(target)
    # Avoid tens of thousands of tiny ASGI sends for the 1.3+ GiB ONNX file.
    response.chunk_size = 8 * 1024 * 1024
    return response


async def save_model_cache_file(raw_url: str, request: Request) -> dict[str, object]:
    profile = active_model_profile()
    relative_path = model_relative_path_from_url(raw_url, profile)
    root = model_dir(profile)
    target = root.joinpath(*relative_path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    spec = profile.files[relative_path]

    request_size = int(request.headers.get("content-length", "0") or 0)
    if request_size and request_size != spec.size:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unexpected model file size: expected {spec.size}, "
                f"received Content-Length {request_size}"
            ),
        )
    # A missing manifest requires hashing the complete ONNX file. Keep that
    # multi-gigabyte read off the FastAPI event loop so health requests and
    # other uploads remain responsive.
    if await asyncio.to_thread(
        is_verified_model_file,
        root,
        relative_path,
        profile,
    ):
        current_size = target.stat().st_size
        return {
            "ok": True,
            "path": relative_path,
            "bytes": current_size,
            "reused": True,
            "modelKey": profile.key,
            "modelId": profile.model_id,
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
                if total > spec.size:
                    raise ValueError(
                        f"model file exceeds expected size of {spec.size} bytes"
                    )
                # ASGI may provide very large chunks for the 1.3+ GiB model.
                # Serialize writes through one worker thread so health and
                # shutdown requests remain responsive during local caching.
                await asyncio.to_thread(_write_and_hash_chunk, handle, digest, chunk)
        if total != spec.size:
            raise ValueError(
                f"incomplete model file: expected {spec.size} bytes, received {total}"
            )
        actual_digest = digest.hexdigest()
        if actual_digest != spec.sha256:
            raise ValueError("model file checksum mismatch")
        await asyncio.to_thread(os.replace, temp_target, target)
        await asyncio.to_thread(
            record_verified_model_file,
            root,
            relative_path,
            digest=actual_digest,
            profile=profile,
        )
        debug("model_cache_save", f"{relative_path}\nbytes: {total}")
        return {
            "ok": True,
            "path": relative_path,
            "bytes": total,
            "modelKey": profile.key,
            "modelId": profile.model_id,
        }
    except HTTPException:
        raise
    except Exception as exc:
        error("model_cache_save", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        try:
            temp_target.unlink(missing_ok=True)
        except OSError:
            pass
