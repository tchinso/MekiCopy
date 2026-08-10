from __future__ import annotations

import configparser
import hashlib
import os
import sys
import threading
import tkinter as tk
from functools import lru_cache
from typing import Callable
from tkinter import messagebox

import mss
from PIL import Image
from mekicopy_capture import (
    MIN_SIZE_PX,
    CaptureStatus,
    Region,
    capture_problem_message as _capture_problem_message,
    capture_region_result,
    configure_capture_runtime,
)
from mekicopy_runtime import (
    _get_app_dir,
    _get_resource_dir,
    _prepare_native_runtime_paths,
    _prepare_tk_library_paths,
    _prepare_windowed_streams,
)
import mekicopy_settings
from system_logging import log_debug as _system_debug
from system_logging import log_error as _system_error

_OCR_ENGINE = None
_ORT_PRELOAD_READY = False
_OCR_ENGINE_LOCK = threading.Lock()
_OCR_RUN_LOCK = threading.Lock()
CUDA_PROVIDER_REQUIRED_DLLS = (
    "cublasLt64_13.dll",
    "cublas64_13.dll",
    "cudnn64_9.dll",
)
OCR_MODEL_MANIFEST = {
    "meiki.text.detect.v0.1.960x544.onnx": (
        14_503_825,
        "40b6a016667745cae7d3055929ae3b8b1e7716aac795f5904cd3c2c7c3b8404b",
    ),
    "meiki.text.rec.v0.960x32.onnx": (
        18_593_254,
        "3e96bc772fbee9717e536a6353032bb944c3382dd2f6960ef4890decda43b000",
    ),
    "meiki.text.rec.v0.vertical.32x480.onnx": (
        12_872_961,
        "2c2a83a23bc3b7e6c63962175f507ecc6c5e85cc174f17bdec37d9bbd0bf895a",
    ),
}

def postprocess_text(text: str) -> str:
    return " ".join(text.split())


def _is_debug_logging_enabled() -> bool:
    parser = configparser.ConfigParser()
    try:
        parser.read(mekicopy_settings.SETTINGS_FILE, encoding="utf-8")
        return parser.getboolean("settings", "debug_logging", fallback=False)
    except (configparser.Error, TypeError, ValueError, OSError):
        return False


def _log_runtime_error(stage: str, exc: Exception) -> None:
    _system_error(stage, exc, component="MekiCopy")


def _log_runtime_message(stage: str, message: str) -> None:
    _system_debug(
        stage,
        message,
        component="MekiCopy",
        enabled=_is_debug_logging_enabled(),
    )


configure_capture_runtime(
    app_dir_provider=_get_app_dir,
    error_logger=_log_runtime_error,
    message_logger=_log_runtime_message,
)


def _patch_onnxruntime_compat() -> None:
    try:
        import onnxruntime as ort
    except Exception:
        return

    if not hasattr(ort, "set_default_logger_severity"):

        def _noop_set_default_logger_severity(_level: int) -> None:
            return None

        ort.set_default_logger_severity = _noop_set_default_logger_severity


def _runtime_dll_search_dirs() -> list[str]:
    directories = [
        os.path.dirname(sys.executable),
        _get_app_dir(),
        _get_resource_dir(),
    ]
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            directories.append(meipass)
    directories.extend(os.environ.get("PATH", "").split(os.pathsep))
    seen: set[str] = set()
    result: list[str] = []
    for directory in directories:
        if not directory:
            continue
        normalized = os.path.normcase(os.path.abspath(directory))
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(directory)
    return result


def _has_runtime_dll(filename: str) -> bool:
    return any(
        os.path.exists(os.path.join(directory, filename))
        for directory in _runtime_dll_search_dirs()
    )


def _cuda_provider_looks_usable() -> bool:
    if os.name != "nt":
        return True
    missing = [
        filename
        for filename in CUDA_PROVIDER_REQUIRED_DLLS
        if not _has_runtime_dll(filename)
    ]
    if missing:
        _log_runtime_message(
            "cuda_provider_skipped",
            "missing DLLs: " + ", ".join(missing),
        )
        return False
    return True


def _preload_onnxruntime_gpu_dlls() -> None:
    global _ORT_PRELOAD_READY
    if _ORT_PRELOAD_READY:
        return

    _ORT_PRELOAD_READY = True
    if not _cuda_provider_looks_usable():
        return
    try:
        import onnxruntime as ort
    except Exception as exc:
        _log_runtime_error("import_onnxruntime", exc)
        return

    preload_dlls = getattr(ort, "preload_dlls", None)
    if not callable(preload_dlls):
        return

    try:
        preload_dlls(cuda=True, cudnn=True, msvc=True)
    except Exception as exc:
        _log_runtime_error("preload_onnxruntime_gpu_dlls", exc)


@lru_cache(maxsize=16)
def _valid_bundled_model(path: str, filename: str) -> bool:
    expected = OCR_MODEL_MANIFEST.get(filename)
    if expected is None:
        return False
    try:
        if os.path.getsize(path) != expected[0]:
            return False
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected[1]
    except OSError:
        return False


def _find_bundled_model(filename: str) -> str | None:
    candidate_dirs = [
        os.path.join(_get_app_dir(), "runtime_models"),
        os.path.join(_get_app_dir(), "runtime_models", "meikiocr"),
        os.path.join(_get_resource_dir(), "runtime_models"),
        os.path.join(_get_resource_dir(), "runtime_models", "meikiocr"),
    ]
    for directory in candidate_dirs:
        candidate = os.path.join(directory, filename)
        if _valid_bundled_model(candidate, filename):
            return candidate
    return None


def _patch_meikiocr_model_loader(meikiocr_ocr) -> None:
    if getattr(meikiocr_ocr, "_mekicopy_patched", False):
        return

    original_get_model_path = meikiocr_ocr._get_model_path

    def _local_first_model_path(repo_id: str, filename: str) -> str:
        local_path = _find_bundled_model(filename)
        if local_path:
            return local_path
        return original_get_model_path(repo_id, filename)

    meikiocr_ocr._get_model_path = _local_first_model_path
    meikiocr_ocr._mekicopy_patched = True


def _get_available_ort_providers() -> list[str]:
    try:
        import onnxruntime as ort
    except Exception as exc:
        _log_runtime_error("get_available_ort_providers", exc)
        return []
    try:
        return list(ort.get_available_providers())
    except Exception as exc:
        _log_runtime_error("get_available_ort_providers", exc)
        return []


def _create_meikiocr_engine(meikiocr_ocr, provider: str):
    engine = meikiocr_ocr.MeikiOCR(provider=provider)
    active_provider = getattr(engine, "active_provider", provider)
    _log_runtime_message(
        "create_meikiocr_engine",
        f"requested_provider: {provider}\nactive_provider: {active_provider}",
    )
    return engine


def _create_best_meikiocr_engine(meikiocr_ocr):
    available_providers = _get_available_ort_providers()
    _log_runtime_message(
        "onnxruntime_providers",
        "available_providers: " + ", ".join(available_providers),
    )

    if "CUDAExecutionProvider" in available_providers and _cuda_provider_looks_usable():
        try:
            return _create_meikiocr_engine(meikiocr_ocr, "CUDAExecutionProvider")
        except Exception as exc:
            _log_runtime_error("create_cuda_meikiocr_engine", exc)

    return _create_meikiocr_engine(meikiocr_ocr, "CPUExecutionProvider")


def _get_ocr_engine():
    global _OCR_ENGINE
    with _OCR_ENGINE_LOCK:
        if _OCR_ENGINE is not None:
            return _OCR_ENGINE

        _prepare_windowed_streams()
        _prepare_native_runtime_paths()
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TQDM_DISABLE", "1")
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        _patch_onnxruntime_compat()
        _preload_onnxruntime_gpu_dlls()
        import meikiocr.ocr as meikiocr_ocr

        _patch_meikiocr_model_loader(meikiocr_ocr)
        _OCR_ENGINE = _create_best_meikiocr_engine(meikiocr_ocr)
        return _OCR_ENGINE


class OcrError(RuntimeError):
    """A recoverable OCR engine or input failure."""


def recognize_image(image: Image.Image) -> str:
    """Run OCR on a captured image without touching Tk or temporary files."""
    try:
        _prepare_native_runtime_paths()
        import numpy as np

        rgb_image = image.convert("RGB")
        # OpenCV/MeikiOCR receive BGR data when they load an image file. Keep
        # that channel order while avoiding fragile temporary Unicode paths.
        image_data = np.asarray(rgb_image)[:, :, ::-1].copy()
        if image_data.size == 0:
            raise OcrError("captured image is empty")
        with _OCR_RUN_LOCK:
            results = _get_ocr_engine().run_ocr(image_data)
    except OcrError:
        raise
    except Exception as exc:
        _log_runtime_error("recognize_image", exc)
        raise OcrError(str(exc) or type(exc).__name__) from exc

    output_lines = []
    for line in results:
        text = line.get("text", "")
        if text:
            output_lines.append(text)
    return postprocess_text("\n".join(output_lines))


def run_meikiocr(image_path: str) -> str:
    try:
        with Image.open(image_path) as source:
            return recognize_image(source)
    except OcrError:
        raise
    except Exception as exc:
        _log_runtime_error("run_meikiocr", exc)
        raise OcrError(str(exc) or type(exc).__name__) from exc


def capture_region(left: int, top: int, width: int, height: int) -> Image.Image:
    result = capture_region_result(left, top, width, height)
    if result.image is None:
        raise RuntimeError(_capture_problem_message(result))
    return result.image


def capture_ocr_image(left: int, top: int, width: int, height: int) -> Image.Image:
    """Capture a valid OCR frame without presenting a Tk dialog."""
    if width < MIN_SIZE_PX or height < MIN_SIZE_PX:
        raise OcrError("OCR capture region is too small")
    result = capture_region_result(left, top, width, height)
    if result.status in (
        CaptureStatus.REGION_OUT_OF_BOUNDS,
        CaptureStatus.BLACK_FRAME_SUSPECTED,
        CaptureStatus.CAPTURE_EXCEPTION,
        CaptureStatus.SIZE_MISMATCH,
    ) or result.image is None:
        raise OcrError(_capture_problem_message(result))
    return result.image


def copy_text_to_clipboard(
    text: str,
    notify: bool = True,
    parent: tk.Misc | None = None,
) -> None:
    _prepare_tk_library_paths()
    clip_owner = parent
    created_clip_owner = False
    if clip_owner is None:
        clip_owner = tk.Tk()
        clip_owner.withdraw()
        created_clip_owner = True

    clip_owner.clipboard_clear()
    clip_owner.clipboard_append(text)
    clip_owner.update()
    if created_clip_owner:
        clip_owner.destroy()
    if notify:
        messagebox.showinfo("MekiCopy", "복사되었습니다!", parent=parent)


def ocr_region(
    left: int,
    top: int,
    width: int,
    height: int,
    parent: tk.Misc | None = None,
) -> str | None:
    if width < MIN_SIZE_PX or height < MIN_SIZE_PX:
        messagebox.showerror("MekiCopy", "캡처 영역이 너무 작습니다.", parent=parent)
        return None
    result = capture_region_result(left, top, width, height)
    if result.status in (
        CaptureStatus.REGION_OUT_OF_BOUNDS,
        CaptureStatus.BLACK_FRAME_SUSPECTED,
        CaptureStatus.CAPTURE_EXCEPTION,
        CaptureStatus.SIZE_MISMATCH,
    ):
        messagebox.showwarning("MekiCopy", _capture_problem_message(result), parent=parent)
        return None
    if result.image is None:
        messagebox.showwarning("MekiCopy", _capture_problem_message(result), parent=parent)
        return None
    try:
        return recognize_image(result.image)
    except OcrError as exc:
        messagebox.showerror("MekiCopy", f"OCR ?ㅽ뻾 ?ㅽ뙣:\n{exc}", parent=parent)
        return None


def ocr_and_copy(
    left: int,
    top: int,
    width: int,
    height: int,
    notify: bool = True,
    parent: tk.Misc | None = None,
    on_copy_complete: Callable[[], None] | None = None,
) -> None:
    text = ocr_region(left, top, width, height, parent=parent)
    if text is None:
        return
    copy_text_to_clipboard(text, notify=notify, parent=parent)
    if on_copy_complete:
        on_copy_complete()
