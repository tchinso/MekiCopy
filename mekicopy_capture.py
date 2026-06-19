from __future__ import annotations

import ctypes
import datetime as _dt
import json
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable

import mss
from PIL import Image, ImageGrab, ImageStat

MIN_SIZE_PX = 10

_DPI_AWARENESS_READY = False
_CAPTURE_MANAGER = None

_AppDirProvider = Callable[[], str]
_ErrorLogger = Callable[[str, Exception], None]
_MessageLogger = Callable[[str, str], None]


def _default_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _default_error_logger(_stage: str, _exc: Exception) -> None:
    return None


def _default_message_logger(_stage: str, _message: str) -> None:
    return None


_app_dir_provider: _AppDirProvider = _default_app_dir
_error_logger: _ErrorLogger = _default_error_logger
_message_logger: _MessageLogger = _default_message_logger


def configure_capture_runtime(
    app_dir_provider: _AppDirProvider | None = None,
    error_logger: _ErrorLogger | None = None,
    message_logger: _MessageLogger | None = None,
) -> None:
    global _app_dir_provider, _error_logger, _message_logger
    if app_dir_provider is not None:
        _app_dir_provider = app_dir_provider
    if error_logger is not None:
        _error_logger = error_logger
    if message_logger is not None:
        _message_logger = message_logger


def _get_app_dir() -> str:
    return _app_dir_provider()


def _log_runtime_error(stage: str, exc: Exception) -> None:
    _error_logger(stage, exc)


def _log_runtime_message(stage: str, message: str) -> None:
    _message_logger(stage, message)


def enable_dpi_awareness() -> None:
    global _DPI_AWARENESS_READY
    if _DPI_AWARENESS_READY or os.name != "nt":
        return
    _DPI_AWARENESS_READY = True
    try:
        # Per-monitor v2 keeps Tk coordinates aligned with physical pixels where
        # Windows allows it. Older systems fall back below.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


@dataclass
class Region:
    left: int
    top: int
    width: int
    height: int


class CaptureStatus:
    OK = "ok"
    DISPLAY_CHANGED = "display_changed"
    REGION_CLAMPED = "region_clamped"
    REGION_OUT_OF_BOUNDS = "region_out_of_bounds"
    BLACK_FRAME_SUSPECTED = "black_frame_suspected"
    CAPTURE_EXCEPTION = "capture_exception"
    SIZE_MISMATCH = "size_mismatch"


@dataclass(frozen=True)
class MonitorInfo:
    index: int
    left: int
    top: int
    width: int
    height: int
    is_virtual: bool = False

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center(self) -> tuple[float, float]:
        return (self.left + self.width / 2, self.top + self.height / 2)

    @property
    def signature_part(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "is_virtual": self.is_virtual,
        }


@dataclass(frozen=True)
class RegionRatio:
    x_ratio: float
    y_ratio: float
    w_ratio: float
    h_ratio: float

    def to_dict(self) -> dict:
        return {
            "x_ratio": self.x_ratio,
            "y_ratio": self.y_ratio,
            "w_ratio": self.w_ratio,
            "h_ratio": self.h_ratio,
        }


@dataclass
class CaptureFrameStats:
    width: int
    height: int
    mean: float
    stddev: float
    blank_suspected: bool

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "mean": round(self.mean, 3),
            "stddev": round(self.stddev, 3),
            "blank_suspected": self.blank_suspected,
        }


@dataclass
class CaptureResult:
    image: Image.Image | None
    status: str
    strategy: str
    region: Region
    monitors: list[MonitorInfo] = field(default_factory=list)
    stats: CaptureFrameStats | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def _monitor_from_mss(index: int, monitor: dict, is_virtual: bool) -> MonitorInfo:
    return MonitorInfo(
        index=index,
        left=int(monitor.get("left", 0)),
        top=int(monitor.get("top", 0)),
        width=int(monitor.get("width", 0)),
        height=int(monitor.get("height", 0)),
        is_virtual=is_virtual,
    )


def monitor_signature(monitors: list[MonitorInfo]) -> tuple[tuple[int, int, int, int], ...]:
    return tuple(monitor.signature_part for monitor in monitors)


def _region_right(region: Region) -> int:
    return region.left + region.width


def _region_bottom(region: Region) -> int:
    return region.top + region.height


def _region_center(region: Region) -> tuple[float, float]:
    return (region.left + region.width / 2, region.top + region.height / 2)


def _point_in_monitor(point: tuple[float, float], monitor: MonitorInfo) -> bool:
    x, y = point
    return monitor.left <= x < monitor.right and monitor.top <= y < monitor.bottom


def region_intersection_area(region: Region, monitor: MonitorInfo) -> int:
    left = max(region.left, monitor.left)
    top = max(region.top, monitor.top)
    right = min(_region_right(region), monitor.right)
    bottom = min(_region_bottom(region), monitor.bottom)
    return max(0, right - left) * max(0, bottom - top)


def find_monitor_for_region(
    region: Region,
    monitors: list[MonitorInfo],
) -> MonitorInfo | None:
    if not monitors:
        return None
    center = _region_center(region)
    for monitor in monitors:
        if _point_in_monitor(center, monitor):
            return monitor
    intersecting = [
        (monitor, region_intersection_area(region, monitor))
        for monitor in monitors
    ]
    intersecting.sort(key=lambda item: item[1], reverse=True)
    if intersecting and intersecting[0][1] > 0:
        return intersecting[0][0]
    cx, cy = center
    return min(
        monitors,
        key=lambda monitor: (monitor.center[0] - cx) ** 2 + (monitor.center[1] - cy) ** 2,
    )


def match_monitor_by_previous_rect(
    previous: MonitorInfo | None,
    monitors: list[MonitorInfo],
) -> MonitorInfo | None:
    if not monitors:
        return None
    if previous is None:
        return monitors[0]
    for monitor in monitors:
        if monitor.signature_part == previous.signature_part:
            return monitor
    px, py = previous.center
    return min(
        monitors,
        key=lambda monitor: (monitor.center[0] - px) ** 2 + (monitor.center[1] - py) ** 2,
    )


def ratio_for_region(region: Region, monitor: MonitorInfo) -> RegionRatio:
    width = max(1, monitor.width)
    height = max(1, monitor.height)
    return RegionRatio(
        x_ratio=(region.left - monitor.left) / width,
        y_ratio=(region.top - monitor.top) / height,
        w_ratio=region.width / width,
        h_ratio=region.height / height,
    )


def region_from_ratio(monitor: MonitorInfo, ratio: RegionRatio) -> Region:
    return Region(
        left=monitor.left + int(round(monitor.width * ratio.x_ratio)),
        top=monitor.top + int(round(monitor.height * ratio.y_ratio)),
        width=max(MIN_SIZE_PX, int(round(monitor.width * ratio.w_ratio))),
        height=max(MIN_SIZE_PX, int(round(monitor.height * ratio.h_ratio))),
    )


def clamp_region_to_monitor(region: Region, monitor: MonitorInfo) -> Region:
    left = max(region.left, monitor.left)
    top = max(region.top, monitor.top)
    right = min(_region_right(region), monitor.right)
    bottom = min(_region_bottom(region), monitor.bottom)
    return Region(
        left=left,
        top=top,
        width=max(0, right - left),
        height=max(0, bottom - top),
    )


def regions_equal(first: Region, second: Region) -> bool:
    return (
        first.left == second.left
        and first.top == second.top
        and first.width == second.width
        and first.height == second.height
    )


def _capture_stats(image: Image.Image) -> CaptureFrameStats:
    sample = image.convert("L")
    if sample.width > 240 or sample.height > 240:
        sample.thumbnail((240, 240))
    stat = ImageStat.Stat(sample)
    mean = float(stat.mean[0]) if stat.mean else 0.0
    stddev = float(stat.stddev[0]) if stat.stddev else 0.0
    blank_suspected = mean <= 3.0 and stddev <= 2.0
    return CaptureFrameStats(
        width=image.width,
        height=image.height,
        mean=mean,
        stddev=stddev,
        blank_suspected=blank_suspected,
    )


class CaptureManager:
    def __init__(self) -> None:
        self.sct = None
        self.monitor_signature: tuple[tuple[int, int, int, int], ...] = ()
        self.last_result: CaptureResult | None = None
        self.blank_frame_count = 0
        self.failure_count = 0
        self.reinitialize("startup")

    def close(self) -> None:
        if self.sct is None:
            return
        try:
            self.sct.close()
        except Exception:
            pass
        self.sct = None

    def reinitialize(self, reason: str) -> None:
        self.close()
        self.sct = mss.mss()
        self.monitor_signature = monitor_signature(self.get_monitors(refresh=False))
        _log_runtime_message(
            "capture_reinitialize",
            f"reason: {reason}\nmonitor_signature: {self.monitor_signature}",
        )

    def get_monitors(self, refresh: bool = True) -> list[MonitorInfo]:
        if self.sct is None:
            self.reinitialize("missing_mss")
        assert self.sct is not None
        raw_monitors = list(self.sct.monitors)
        monitors: list[MonitorInfo] = []
        for index, monitor in enumerate(raw_monitors):
            monitors.append(_monitor_from_mss(index, monitor, is_virtual=(index == 0)))
        return monitors

    def get_real_monitors(self) -> list[MonitorInfo]:
        monitors = [monitor for monitor in self.get_monitors() if not monitor.is_virtual]
        if monitors:
            return monitors
        return self.get_monitors()[:1]

    def refresh_if_display_changed(self) -> bool:
        try:
            with mss.mss() as fresh:
                fresh_monitors = [
                    _monitor_from_mss(index, monitor, is_virtual=(index == 0))
                    for index, monitor in enumerate(list(fresh.monitors))
                ]
            fresh_signature = monitor_signature(fresh_monitors)
        except Exception as exc:
            _log_runtime_error("capture_check_display_changed", exc)
            self.reinitialize("display_check_failed")
            return True
        if fresh_signature == self.monitor_signature:
            return False
        previous = self.monitor_signature
        self.reinitialize("display_changed")
        _log_runtime_message(
            "capture_display_changed",
            f"before: {previous}\nafter: {fresh_signature}",
        )
        return True

    def resolve_region(
        self,
        region: Region,
        previous_monitor: MonitorInfo | None = None,
        ratio: RegionRatio | None = None,
    ) -> tuple[Region, MonitorInfo | None, RegionRatio | None, list[str]]:
        display_changed = self.refresh_if_display_changed()
        monitors = self.get_real_monitors()
        messages: list[str] = []
        target_monitor = find_monitor_for_region(region, monitors)
        adjusted = region

        if display_changed and ratio is not None:
            matched = match_monitor_by_previous_rect(previous_monitor, monitors)
            if matched is not None:
                adjusted = region_from_ratio(matched, ratio)
                target_monitor = matched
                messages.append("디스플레이 변경 감지: 비율 좌표로 OCR 영역을 복구했습니다.")
        elif ratio is not None and target_monitor is not None:
            if region_intersection_area(region, target_monitor) <= 0:
                adjusted = region_from_ratio(target_monitor, ratio)
                messages.append("OCR 영역이 모니터 밖에 있어 비율 좌표로 복구했습니다.")

        if target_monitor is None:
            target_monitor = find_monitor_for_region(adjusted, monitors)
        if target_monitor is None:
            return adjusted, None, ratio, messages

        clamped = clamp_region_to_monitor(adjusted, target_monitor)
        if not regions_equal(clamped, adjusted):
            adjusted = clamped
            messages.append("OCR 영역을 현재 모니터 범위 안으로 보정했습니다.")

        if adjusted.width < MIN_SIZE_PX or adjusted.height < MIN_SIZE_PX:
            messages.append("OCR 영역이 너무 작습니다. 영역을 다시 지정해주세요.")
            return adjusted, target_monitor, ratio, messages

        return adjusted, target_monitor, ratio_for_region(adjusted, target_monitor), messages

    def capture_region(self, requested_region: Region) -> CaptureResult:
        enable_dpi_awareness()
        display_changed = self.refresh_if_display_changed()
        monitors = self.get_monitors()
        virtual_monitor = monitors[0] if monitors else None
        warnings: list[str] = []
        region = requested_region
        status = CaptureStatus.DISPLAY_CHANGED if display_changed else CaptureStatus.OK

        if requested_region.width < MIN_SIZE_PX or requested_region.height < MIN_SIZE_PX:
            result = CaptureResult(
                image=None,
                status=CaptureStatus.REGION_OUT_OF_BOUNDS,
                strategy="none",
                region=requested_region,
                monitors=monitors,
                error="capture region is too small",
            )
            self.last_result = result
            return result

        if virtual_monitor is not None:
            clamped = clamp_region_to_monitor(requested_region, virtual_monitor)
            if clamped.width < MIN_SIZE_PX or clamped.height < MIN_SIZE_PX:
                result = CaptureResult(
                    image=None,
                    status=CaptureStatus.REGION_OUT_OF_BOUNDS,
                    strategy="none",
                    region=clamped,
                    monitors=monitors,
                    error="capture region is outside the virtual desktop",
                )
                self.last_result = result
                return result
            if not regions_equal(clamped, requested_region):
                region = clamped
                status = CaptureStatus.REGION_CLAMPED
                warnings.append("capture region was clamped to the virtual desktop")

        attempts = [
            ("mss", False),
            ("mss_reinitialized", True),
            ("pillow_imagegrab", False),
            ("gdi_bitblt", False),
        ]
        retry_mss = False
        first_blank: tuple[str, Image.Image, CaptureFrameStats] | None = None
        errors: list[str] = []

        for strategy, force_reinit in attempts:
            if strategy == "mss_reinitialized" and not retry_mss:
                continue
            try:
                if force_reinit:
                    self.reinitialize("retry_after_failed_or_blank_frame")
                    monitors = self.get_monitors()
                image = self._capture_with_strategy(strategy, region)
                if image.size != (region.width, region.height):
                    stats = _capture_stats(image)
                    errors.append(
                        f"{strategy}: size mismatch {image.size} != {(region.width, region.height)}"
                    )
                    if first_blank is None:
                        first_blank = (strategy, image, stats)
                    retry_mss = retry_mss or strategy == "mss"
                    continue
                stats = _capture_stats(image)
                if stats.blank_suspected:
                    if first_blank is None:
                        first_blank = (strategy, image, stats)
                    retry_mss = retry_mss or strategy == "mss"
                    continue
                self.blank_frame_count = 0
                self.failure_count = 0
                result = CaptureResult(
                    image=image,
                    status=status,
                    strategy=strategy,
                    region=region,
                    monitors=monitors,
                    stats=stats,
                    warnings=warnings,
                )
                self.last_result = result
                return result
            except Exception as exc:
                errors.append(f"{strategy}: {type(exc).__name__}: {exc}")
                if strategy == "mss":
                    retry_mss = True
                    self.reinitialize("mss_exception")

        if first_blank is not None:
            strategy, image, stats = first_blank
            self.blank_frame_count += 1
            self.failure_count += 1
            result = CaptureResult(
                image=image,
                status=CaptureStatus.BLACK_FRAME_SUSPECTED,
                strategy=strategy,
                region=region,
                monitors=monitors,
                stats=stats,
                error="\n".join(errors) if errors else None,
                warnings=warnings,
            )
            self.last_result = result
            _log_runtime_message(
                "capture_blank_frame",
                (
                    f"count: {self.blank_frame_count}\n"
                    f"strategy: {strategy}\n"
                    f"region: {region}\n"
                    f"stats: {stats.to_dict()}\n"
                    f"errors: {errors}"
                ),
            )
            return result

        self.failure_count += 1
        if self.failure_count >= 3:
            self.reinitialize("repeated_capture_failures")
        result = CaptureResult(
            image=None,
            status=CaptureStatus.CAPTURE_EXCEPTION,
            strategy="none",
            region=region,
            monitors=monitors,
            error="\n".join(errors) if errors else "capture failed",
            warnings=warnings,
        )
        self.last_result = result
        _log_runtime_message("capture_failed", result.error or "")
        return result

    def _capture_with_strategy(self, strategy: str, region: Region) -> Image.Image:
        if strategy in ("mss", "mss_reinitialized"):
            return self._capture_with_mss(region)
        if strategy == "pillow_imagegrab":
            return self._capture_with_pillow(region)
        if strategy == "gdi_bitblt":
            return self._capture_with_gdi(region)
        raise ValueError(f"unknown capture strategy: {strategy}")

    def _capture_with_mss(self, region: Region) -> Image.Image:
        if self.sct is None:
            self.reinitialize("missing_mss_before_capture")
        assert self.sct is not None
        mss_region = {
            "left": region.left,
            "top": region.top,
            "width": region.width,
            "height": region.height,
        }
        sct_image = self.sct.grab(mss_region)
        return Image.frombytes("RGB", sct_image.size, sct_image.rgb)

    def _capture_with_pillow(self, region: Region) -> Image.Image:
        bbox = (
            region.left,
            region.top,
            _region_right(region),
            _region_bottom(region),
        )
        try:
            image = ImageGrab.grab(bbox=bbox, all_screens=True)
        except TypeError:
            image = ImageGrab.grab(bbox=bbox)
        return image.convert("RGB")

    def _capture_with_gdi(self, region: Region) -> Image.Image:
        if os.name != "nt":
            raise RuntimeError("GDI capture is only available on Windows")

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        SRCCOPY = 0x00CC0020
        CAPTUREBLT = 0x40000000
        DIB_RGB_COLORS = 0

        user32.GetDC.restype = wintypes.HDC
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.BitBlt.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC,
            wintypes.HBITMAP,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            raise RuntimeError("GetDC failed")
        hdc_mem = None
        hbitmap = None
        old_bitmap = None
        try:
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            if not hdc_mem:
                raise RuntimeError("CreateCompatibleDC failed")
            hbitmap = gdi32.CreateCompatibleBitmap(hdc_screen, region.width, region.height)
            if not hbitmap:
                raise RuntimeError("CreateCompatibleBitmap failed")
            old_bitmap = gdi32.SelectObject(hdc_mem, hbitmap)
            if not gdi32.BitBlt(
                hdc_mem,
                0,
                0,
                region.width,
                region.height,
                hdc_screen,
                region.left,
                region.top,
                SRCCOPY | CAPTUREBLT,
            ):
                raise RuntimeError("BitBlt failed")

            bitmap_info = BITMAPINFO()
            bitmap_info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bitmap_info.bmiHeader.biWidth = region.width
            bitmap_info.bmiHeader.biHeight = -region.height
            bitmap_info.bmiHeader.biPlanes = 1
            bitmap_info.bmiHeader.biBitCount = 32
            bitmap_info.bmiHeader.biCompression = 0
            buffer_size = region.width * region.height * 4
            pixels = ctypes.create_string_buffer(buffer_size)
            lines = gdi32.GetDIBits(
                hdc_mem,
                hbitmap,
                0,
                region.height,
                pixels,
                ctypes.byref(bitmap_info),
                DIB_RGB_COLORS,
            )
            if lines != region.height:
                raise RuntimeError("GetDIBits failed")
            return Image.frombuffer(
                "RGB",
                (region.width, region.height),
                pixels,
                "raw",
                "BGRX",
                0,
                1,
            ).copy()
        finally:
            if old_bitmap and hdc_mem:
                gdi32.SelectObject(hdc_mem, old_bitmap)
            if hbitmap:
                gdi32.DeleteObject(hbitmap)
            if hdc_mem:
                gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)


def get_capture_manager() -> CaptureManager:
    global _CAPTURE_MANAGER
    if _CAPTURE_MANAGER is None:
        _CAPTURE_MANAGER = CaptureManager()
    return _CAPTURE_MANAGER


def capture_region_result(left: int, top: int, width: int, height: int) -> CaptureResult:
    region = Region(left=left, top=top, width=width, height=height)
    return get_capture_manager().capture_region(region)


def capture_problem_message(result: CaptureResult) -> str:
    if result.status == CaptureStatus.BLACK_FRAME_SUSPECTED:
        return (
            "캡처 결과가 검은 화면으로 의심됩니다.\n\n"
            "가능한 원인:\n"
            "- 게임이 독점 전체화면 모드로 실행 중일 수 있습니다.\n"
            "- 구형 DirectDraw/DirectX 렌더링이 mss/GDI 캡처를 막고 있을 수 있습니다.\n"
            "- 브라우저, Electron, WebView 기반 게임이면 하드웨어 가속 영향일 수 있습니다.\n\n"
            "권장 해결:\n"
            "1. 게임을 창모드 또는 무테창 모드로 바꿔보세요.\n"
            "2. 구형 게임이면 DxWnd 또는 dgVoodoo2 같은 창모드 래퍼를 검토해보세요.\n"
            "3. 브라우저/Electron 기반이면 하드웨어 가속을 꺼보세요.\n"
            "4. 해상도가 바뀐 뒤에는 OCR 영역을 다시 지정해보세요."
        )
    if result.status == CaptureStatus.REGION_OUT_OF_BOUNDS:
        return "OCR 영역이 현재 화면 범위를 벗어났습니다. 영역을 다시 지정해주세요."
    if result.status == CaptureStatus.CAPTURE_EXCEPTION:
        return f"화면 캡처에 실패했습니다.\n\n{result.error or '원인을 확인할 수 없습니다.'}"
    if result.status == CaptureStatus.SIZE_MISMATCH:
        return "캡처 이미지 크기가 예상과 다릅니다. 모니터를 다시 검색하거나 OCR 영역을 다시 지정해주세요."
    return result.error or "캡처 상태를 확인해주세요."


def run_capture_diagnostics(region: Region | None = None) -> str:
    manager = get_capture_manager()
    manager.reinitialize("diagnostics")
    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    directory = os.path.join(_get_app_dir(), "diagnostics", timestamp)
    os.makedirs(directory, exist_ok=True)

    monitors = manager.get_monitors()
    report: dict = {
        "timestamp": _dt.datetime.now().isoformat(),
        "monitor_signature": monitor_signature(monitors),
        "monitors": [monitor.to_dict() for monitor in monitors],
        "captures": [],
        "last_result": None,
    }

    for monitor in monitors:
        if monitor.width < MIN_SIZE_PX or monitor.height < MIN_SIZE_PX:
            continue
        capture_region_for_monitor = Region(
            left=monitor.left,
            top=monitor.top,
            width=monitor.width,
            height=monitor.height,
        )
        result = manager.capture_region(capture_region_for_monitor)
        filename = f"monitor_{monitor.index}{'_virtual' if monitor.is_virtual else ''}.png"
        entry = {
            "monitor": monitor.to_dict(),
            "status": result.status,
            "strategy": result.strategy,
            "file": filename if result.image else None,
            "stats": result.stats.to_dict() if result.stats else None,
            "error": result.error,
        }
        if result.image:
            result.image.save(os.path.join(directory, filename))
        report["captures"].append(entry)

    if region is not None:
        result = manager.capture_region(region)
        filename = "current_ocr_region.png"
        if result.image:
            result.image.save(os.path.join(directory, filename))
        report["current_ocr_region"] = {
            "region": {
                "left": region.left,
                "top": region.top,
                "width": region.width,
                "height": region.height,
            },
            "status": result.status,
            "strategy": result.strategy,
            "file": filename if result.image else None,
            "stats": result.stats.to_dict() if result.stats else None,
            "error": result.error,
            "warnings": result.warnings,
        }

    if manager.last_result is not None:
        report["last_result"] = {
            "status": manager.last_result.status,
            "strategy": manager.last_result.strategy,
            "region": {
                "left": manager.last_result.region.left,
                "top": manager.last_result.region.top,
                "width": manager.last_result.region.width,
                "height": manager.last_result.region.height,
            },
            "stats": manager.last_result.stats.to_dict()
            if manager.last_result.stats
            else None,
            "error": manager.last_result.error,
        }

    with open(os.path.join(directory, "diagnostics.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return directory

