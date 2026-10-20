"""Sequential, cancellation-safe realtime OCR scheduling, independent of Tk widgets."""

from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


REALTIME_INTERVALS = (1.0, 1.5, 2.0, 2.5, 3.0)
REALTIME_LOCK_LABEL = "실시간 인식 중입니다"
REALTIME_STATE_FILENAME = "realtime_ocr_state.json"
_JAPANESE = re.compile(
    "[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff"
    "\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\uff66-\uff9f\U00020000-\U0002fa1f々〆〇]"
)


def normalize_interval(value: object) -> float:
    try:
        interval = float(value)
    except (ValueError, TypeError):
        return 2.0
    return interval if interval in REALTIME_INTERVALS else 2.0


def comparison_text(text: str) -> str:
    """Normalize whitespace only; original OCR output is never rewritten."""
    return " ".join(text.split())


def japanese_character_count(text: str) -> int:
    return len(_JAPANESE.findall(text))


def levenshtein_distance(first: str, second: str) -> int:
    if len(first) < len(second):
        first, second = second, first
    previous = list(range(len(second) + 1))
    for row, a in enumerate(first, 1):
        current = [row]
        for column, b in enumerate(second, 1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (a != b)))
        previous = current
    return previous[-1]


def is_duplicate(previous: str | None, current: str) -> bool:
    if previous is None:
        return False
    before, after = comparison_text(previous), comparison_text(current)
    if before == after:
        return True
    if japanese_character_count(previous) < 15 or japanese_character_count(current) < 15:
        return False
    # Integer arithmetic includes the exact 92% boundary without float error.
    distance = levenshtein_distance(before, after)
    return distance * 100 <= max(len(before), len(after)) * 8


def next_start(started_at: float, interval: float, displayed_at: float | None) -> float:
    deadline = started_at + interval
    return max(deadline, displayed_at + 1.0) if displayed_at is not None else deadline


@dataclass
class RealtimeSession:
    generation: int
    owner: str = field(default_factory=lambda: "ocr:" + uuid.uuid4().hex)
    cancelled: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class RealtimeResult:
    text: str
    skipped: bool = False
    displayed_at: float | None = None
    capture_target: object = None


def perform_cycle(
    recognize: Callable[[], str],
    *,
    session: RealtimeSession,
    previous: str | None,
    translate: Callable[[str], dict] | None = None,
) -> RealtimeResult:
    """Keep deduplication before translation, including native context mutation."""
    if session.cancelled.is_set():
        return RealtimeResult("", skipped=True)
    original = recognize()
    if (session.cancelled.is_set() or not original.strip()
            or is_duplicate(previous, original)):
        return RealtimeResult(original, skipped=True)
    if translate is None:
        return RealtimeResult(original)
    receipt = translate(original)
    if session.cancelled.is_set():
        return RealtimeResult(original, skipped=True)
    displayed_at = receipt.get("displayedMonotonic") if isinstance(receipt, dict) else None
    if not isinstance(displayed_at, (int, float)) or not math.isfinite(displayed_at):
        raise RuntimeError("오버레이의 실제 표시 완료를 확인하지 못했습니다.")
    return RealtimeResult(original, displayed_at=float(displayed_at))


class RealtimeOcrController:
    """Own one OCR → translation → display operation and its next Tk timer.

    Configuration and result callbacks run on Tk's thread. Workers never access
    widgets. An obsolete worker must finish before a new session may submit work.
    """

    def __init__(self, root, runner, *, prepare_operation, on_result, on_error,
                 on_state=lambda: None, clock=time.monotonic) -> None:
        self.root, self.runner = root, runner
        self.prepare_operation, self.on_result = prepare_operation, on_result
        self.on_error, self.on_state, self.clock = on_error, on_state, clock
        self.active = False
        self.interval = 2.0
        self.translate = False
        self.previous: str | None = None
        self.session: RealtimeSession | None = None
        self._signature = None
        self._generation = 0
        self._after_id = None
        self._in_flight = False
        self._started_at: float | None = None
        self._closed = False

    @property
    def translation_locked(self) -> bool:
        return self.active and self.translate

    def configure(self, *, enabled: bool, interval: float, translate: bool,
                  fingerprint=None) -> None:
        interval = normalize_interval(interval)
        signature = (bool(enabled), interval, bool(translate), fingerprint)
        if self._closed or signature == self._signature:
            return
        self._signature = signature
        if self.session:
            self.session.cancelled.set()
        self._cancel_timer()
        self._generation += 1
        self.active, self.interval, self.translate = bool(enabled), interval, bool(translate)
        self.previous = None
        self.session = RealtimeSession(self._generation) if self.active else None
        self.on_state()
        if self.active and not self._in_flight:
            earliest = self.clock() if self._started_at is None else self._started_at + interval
            self._schedule(earliest)

    def close(self) -> None:
        self._closed = True
        self.active = False
        if self.session:
            self.session.cancelled.set()
        self._cancel_timer()
        self.on_state()

    def _cancel_timer(self) -> None:
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _schedule(self, deadline: float) -> None:
        if self._closed or not self.active:
            return
        self._cancel_timer()
        self._after_id = self.root.after(max(0, math.ceil((deadline - self.clock()) * 1000)),
                                         self._begin)

    def _begin(self) -> None:
        self._after_id = None
        if not self.active or self._closed or self._in_flight:
            return
        if self.runner.is_running("main_ocr"):
            # A manual operation started before activation; never overlap it.
            self._schedule(self.clock() + self.interval)
            return
        session = self.session
        self._started_at = self.clock()
        self._in_flight = True
        try:
            operation = self.prepare_operation(session, self.previous)
            submitted = self.runner.submit(
                "main_ocr", operation,
                on_success=lambda result: self._finish(session, result=result),
                on_error=lambda error: self._finish(session, error=error),
            )
            if not submitted:
                self._finish(session)
        except Exception as exc:
            self._finish(session, error=exc)

    def _finish(self, session, *, result=None, error=None) -> None:
        self._in_flight = False
        if self._closed or not self.active:
            return
        if session is not self.session or session.cancelled.is_set():
            self._schedule((self._started_at or 0.0) + self.interval)
            return
        displayed_at = None
        try:
            if error is not None:
                self.on_error(error)
            elif result is not None and not result.skipped:
                if (self.on_result(result) and self.active
                        and session is self.session and not session.cancelled.is_set()):
                    self.previous = result.text
                    displayed_at = result.displayed_at
        except Exception as exc:
            self.on_error(exc)
        finally:
            if session is self.session and not session.cancelled.is_set():
                self._schedule(next_start(self._started_at, self.interval, displayed_at))


def detached_translation_locked(path: Path, *, now=None) -> bool:
    """Read the main process's short lease; settings alone never imply running.

    A main-process crash expires the lease instead of leaving the detached
    button permanently disabled. The companion never starts an OCR scheduler.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = (time.time() if now is None else now) - float(payload["updatedAt"])
        return (payload.get("active") is True and payload.get("translation") is True
                and 0 <= age <= 4.0)
    except (OSError, ValueError, TypeError, KeyError):
        return False
