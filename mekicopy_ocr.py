from __future__ import annotations

import configparser
import datetime as _dt
import os
import tempfile
import traceback
import tkinter as tk
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
from mekicopy_settings import SETTINGS_FILE

_OCR_ENGINE = None
_ORT_PRELOAD_READY = False

def postprocess_text(text: str) -> str:
    return " ".join(text.split())


def _log_timestamp() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_file_path(kind: str, filename: str) -> str:
    directory = os.path.join(_get_app_dir(), kind)
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, filename)


def _is_debug_logging_enabled() -> bool:
    parser = configparser.ConfigParser()
    try:
        parser.read(SETTINGS_FILE, encoding="utf-8")
        return parser.getboolean("settings", "debug_logging", fallback=False)
    except configparser.Error:
        return False


def _log_runtime_error(stage: str, exc: Exception) -> None:
    log_path = _log_file_path("error_log", "mekicopy_error.log")
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write("\n=== ERROR ===\n")
            handle.write(f"time: {_log_timestamp()}\n")
            handle.write(f"stage: {stage}\n")
            handle.write(f"type: {type(exc).__name__}\n")
            handle.write(f"message: {exc}\n")
            handle.write(traceback.format_exc())
    except OSError:
        pass


def _log_runtime_message(stage: str, message: str) -> None:
    if not _is_debug_logging_enabled():
        return
    log_path = _log_file_path("debug_log", "mekicopy_debug.log")
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write("\n=== DEBUG ===\n")
            handle.write(f"time: {_log_timestamp()}\n")
            handle.write(f"stage: {stage}\n")
            handle.write(message.rstrip() + "\n")
    except OSError:
        pass


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


def _preload_onnxruntime_gpu_dlls() -> None:
    global _ORT_PRELOAD_READY
    if _ORT_PRELOAD_READY:
        return

    _ORT_PRELOAD_READY = True
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


def _find_bundled_model(filename: str) -> str | None:
    candidate_dirs = [
        os.path.join(_get_resource_dir(), "runtime_models"),
        os.path.join(_get_resource_dir(), "runtime_models", "meikiocr"),
        os.path.join(_get_app_dir(), "runtime_models"),
        os.path.join(_get_app_dir(), "runtime_models", "meikiocr"),
    ]
    for directory in candidate_dirs:
        candidate = os.path.join(directory, filename)
        if os.path.exists(candidate):
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

    if "CUDAExecutionProvider" in available_providers:
        try:
            return _create_meikiocr_engine(meikiocr_ocr, "CUDAExecutionProvider")
        except Exception as exc:
            _log_runtime_error("create_cuda_meikiocr_engine", exc)

    return _create_meikiocr_engine(meikiocr_ocr, "CPUExecutionProvider")


def _get_ocr_engine():
    global _OCR_ENGINE
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


def run_meikiocr(image_path: str) -> str:
    try:
        _prepare_native_runtime_paths()
        import cv2
        import numpy as np

        ocr = _get_ocr_engine()
        image = None
        try:
            image_data = np.fromfile(image_path, dtype=np.uint8)
            if image_data.size:
                image = cv2.imdecode(image_data, cv2.IMREAD_COLOR)
        except Exception as exc:
            _log_runtime_error("read_image_unicode_path", exc)
        if image is None:
            image = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if image is None:
            return ""
        results = ocr.run_ocr(image)
    except Exception as exc:
        _log_runtime_error("run_meikiocr", exc)
        return postprocess_text(str(exc))

    output_lines = []
    for line in results:
        text = line.get("text", "")
        if text:
            output_lines.append(text)
    return postprocess_text("\n".join(output_lines))


def capture_region(left: int, top: int, width: int, height: int) -> Image.Image:
    result = capture_region_result(left, top, width, height)
    if result.image is None:
        raise RuntimeError(_capture_problem_message(result))
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
    image = result.image
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        temp_path = temp_file.name
        image.save(temp_path)
    try:
        text = run_meikiocr(temp_path)
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    return text


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
