"""Shared adaptive VAD, copied from NyanKat-Tab's speech segmenter.

Source: NyanKat-Tab/speech-core.mjs at commit
5af4ce39f8ef8d0500329b6eec912ea4ffe2ec28.
Global presets supply the settings for video and live capture.
Only probabilities are needed from the existing shared Silero ONNX model;
the NyanKat boundary rules below own padding, weak speech and forced cuts.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from audio_capture_core import SpeechSegment


SAMPLE_RATE = 16_000
FRAME_SIZE = 512


@dataclass(frozen=True)
class SubtitleVadSettings:
    """Snapshot of NyanKat-Tab/speech-core.mjs's fourteen VAD constants."""

    speech_threshold: float = 0.04
    release_threshold: float = 0.045
    weak_speech_threshold: float = 0.08
    clear_speech_threshold: float = 0.15
    min_speech_samples: int = 512
    end_silence_samples: int = 3584
    weak_end_silence_samples: int = 29696
    pre_padding_samples: int = 3584
    weak_pre_padding_samples: int = 19456
    post_padding_samples: int = 3584
    max_chunk_samples: int = 96000
    cut_lookback_samples: int = 18432
    cut_pause_samples: int = 1536
    overlap_samples: int = 8000


SUBTITLE_VAD_SETTINGS = SubtitleVadSettings()


def settings_for_preset(preset_name: str = "FAST") -> SubtitleVadSettings:
    """Read one immutable settings snapshot from a global VAD preset."""
    from audio_capture_core import VAD_PRESETS, normalize_preset

    preset = VAD_PRESETS[normalize_preset(preset_name)]

    def samples(key: str) -> int:
        return int(round(preset[key] * SAMPLE_RATE))

    return SubtitleVadSettings(
        speech_threshold=preset["threshold"],
        release_threshold=preset["release_threshold"],
        weak_speech_threshold=preset["weak_speech_threshold"],
        clear_speech_threshold=preset["clear_speech_threshold"],
        min_speech_samples=samples("min_speech_duration"),
        end_silence_samples=samples("min_silence_duration"),
        weak_end_silence_samples=samples("weak_min_silence_duration"),
        pre_padding_samples=samples("pre_padding"),
        weak_pre_padding_samples=samples("weak_pre_padding"),
        post_padding_samples=samples("post_padding"),
        max_chunk_samples=samples("max_segment_duration"),
        cut_lookback_samples=samples("cut_lookback"),
        cut_pause_samples=samples("cut_pause"),
        overlap_samples=samples("forced_cut_overlap"),
    )


def settings_for_fast() -> SubtitleVadSettings:
    """Compatibility alias for the NyanKat-equivalent FAST settings."""
    return settings_for_preset("FAST")


@dataclass(frozen=True)
class VadBoundary:
    """One finalized PCM range in absolute 16 kHz sample coordinates."""

    start: int
    end: int
    forced: bool
    previous_overlap: int


class VadBoundaryTracker:
    """Track NyanKat-style boundaries with bounded history and no PCM.

    Each push consumes one 512-sample probability and returns newly finalized
    boundaries only. Callers retain/copy the corresponding PCM themselves.
    A final padded model frame may return an end beyond real audio; clamp it
    when materializing PCM, then call ``finish(real_source_end)``.
    """

    def __init__(self, settings: SubtitleVadSettings | None = None) -> None:
        self.settings = settings if settings is not None else settings_for_fast()
        self.frames: deque[tuple[int, bool]] = deque()
        self.position = 0
        self.start: int | None = None
        self.last_speech_end = 0
        self.speech_samples = 0
        self.previous_cut = 0
        self.has_clear_speech = False
        self.last_release_end = 0
        self.last_output_end = 0

    def _output(self, start: int, end: int, forced: bool) -> list[VadBoundary]:
        # Do not emit a tail containing only the previous cut's overlap.
        if end <= start or end <= self.previous_cut:
            return []
        overlap = min(self.settings.overlap_samples, self.previous_cut - start)
        self.last_output_end = end
        return [VadBoundary(start, end, forced, max(0, overlap))]

    def _cut_near_pause(self, limit: int) -> int:
        cut = limit
        pause_start: int | None = None
        for frame_start, weak in self.frames:
            if frame_start < limit - self.settings.cut_lookback_samples:
                continue
            if frame_start > limit:
                break
            if weak:
                if pause_start is None:
                    pause_start = frame_start
            elif pause_start is not None:
                if frame_start - pause_start >= self.settings.cut_pause_samples:
                    cut = (pause_start + frame_start) // 2
                pause_start = None
        return cut

    def push(self, probability: float) -> list[VadBoundary]:
        settings = self.settings
        output: list[VadBoundary] = []
        frame_start = self.position
        self.position += FRAME_SIZE
        weak = probability < settings.weak_speech_threshold
        self.frames.append((frame_start, weak))
        if probability >= settings.speech_threshold:
            if self.start is None:
                isolated = frame_start - self.last_output_end >= settings.weak_pre_padding_samples
                pre_padding = (
                    settings.weak_pre_padding_samples
                    if weak and isolated
                    else settings.pre_padding_samples
                )
                self.start = max(0, frame_start - pre_padding)
            self.has_clear_speech |= probability >= settings.clear_speech_threshold
            self.last_speech_end = self.position
            if probability >= settings.release_threshold:
                self.last_release_end = self.position
            self.speech_samples += FRAME_SIZE

        if self.start is not None:
            end_silence = (
                settings.end_silence_samples
                if self.has_clear_speech
                else settings.weak_end_silence_samples
            )
            last_active_end = self.last_release_end if self.has_clear_speech else self.last_speech_end
            if self.position - last_active_end >= end_silence:
                if self.speech_samples >= settings.min_speech_samples:
                    output = self._output(
                        self.start,
                        min(self.position, self.last_speech_end + settings.post_padding_samples),
                        False,
                    )
                # BALANCED/LONG require more than one voiced frame. Reject
                # an expired short candidate instead of keeping its history
                # alive through arbitrary stretches of silence.
                self.start = None
                self.speech_samples = 0
                self.previous_cut = 0
                self.has_clear_speech = False
            elif (
                self.speech_samples >= settings.min_speech_samples
                and self.position - self.start >= settings.max_chunk_samples
            ):
                cut = self._cut_near_pause(self.start + settings.max_chunk_samples)
                output = self._output(self.start, cut, True)
                self.start = cut - settings.overlap_samples
                self.previous_cut = cut

        before = (
            self.position - settings.weak_pre_padding_samples
            if self.start is None
            else self.start
        )
        while self.frames and self.frames[0][0] + FRAME_SIZE <= before:
            self.frames.popleft()
        return output

    def finish(self, source_end: int) -> list[VadBoundary]:
        # NyanKat receives a live stream. A video also needs its last unfinished
        # utterance, without inventing silence or extending beyond the source.
        output: list[VadBoundary] = []
        if self.start is not None and self.speech_samples >= self.settings.min_speech_samples:
            output = self._output(
                self.start,
                min(source_end, self.position, self.last_speech_end + self.settings.post_padding_samples),
                False,
            )
        self.start = None
        self.speech_samples = 0
        self.previous_cut = 0
        self.has_clear_speech = False
        self.frames.clear()
        return output


class SileroProbabilityModel:
    """CPU inference for the existing shared x/h/c Silero model."""

    def __init__(self, vad_model: Path, num_threads: int = 4) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, int(num_threads))
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(vad_model), sess_options=options, providers=["CPUExecutionProvider"]
        )
        if {item.name for item in self.session.get_inputs()} != {"x", "h", "c"}:
            raise ValueError("공용 Silero VAD 모델의 입력 형식이 올바르지 않습니다.")
        self.hidden = np.zeros((2, 1, 64), dtype=np.float32)
        self.cell = np.zeros((2, 1, 64), dtype=np.float32)

    def probability(self, frame: np.ndarray) -> float:
        frame = np.asarray(frame, dtype=np.float32)
        if frame.size != FRAME_SIZE:
            raise ValueError("Silero VAD 프레임에는 512개 샘플이 필요합니다.")
        feed = {
            "x": np.ascontiguousarray(frame.reshape(1, FRAME_SIZE)),
            "h": self.hidden,
            "c": self.cell,
        }
        probability, self.hidden, self.cell = self.session.run(None, feed)
        return float(np.asarray(probability).reshape(-1)[0])


def _mono_audio(audio: np.ndarray) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 1:
        raise ValueError("자막 VAD에는 16 kHz 모노 오디오가 필요합니다.")
    return audio


def segments_from_probabilities(
    audio: np.ndarray,
    probabilities: Iterable[float],
    *,
    check_cancel: Callable[[], None] | None = None,
    settings: SubtitleVadSettings | None = None,
    preset_name: str = "FAST",
) -> list[SpeechSegment]:
    """Apply shared preset boundaries to one probability per 512 samples.

    Segment audio remains a view of the source, including a source memmap.
    The final probability describes a zero-padded frame when necessary.
    """
    audio = _mono_audio(audio)
    tracker = VadBoundaryTracker(settings if settings is not None else settings_for_preset(preset_name))
    segments: list[SpeechSegment] = []
    def materialize(boundaries: Iterable[VadBoundary]) -> None:
        for boundary in boundaries:
            start = max(0, boundary.start)
            end = min(len(audio), boundary.end)
            if end <= start or end - start <= boundary.previous_overlap:
                continue
            duration = (end - start) / SAMPLE_RATE
            segments.append(
                SpeechSegment(
                    id=len(segments) + 1,
                    start_time=start / SAMPLE_RATE,
                    end_time=end / SAMPLE_RATE,
                    duration=duration,
                    audio=audio[start:end],
                    is_forced_cut=boundary.forced or boundary.previous_overlap > 0,
                    previous_overlap=boundary.previous_overlap / SAMPLE_RATE,
                )
            )

    expected = (len(audio) + FRAME_SIZE - 1) // FRAME_SIZE
    count = 0
    for probability in probabilities:
        if check_cancel is not None:
            check_cancel()
        if count >= expected:
            raise ValueError("VAD 확률 개수가 오디오 프레임 수와 일치하지 않습니다.")
        materialize(tracker.push(float(probability)))
        count += 1
    if count != expected:
        raise ValueError("VAD 확률 개수가 오디오 프레임 수와 일치하지 않습니다.")
    materialize(tracker.finish(len(audio)))
    return segments


def collect_subtitle_segments(
    audio: np.ndarray,
    vad_model: Path,
    num_threads: int = 4,
    *,
    check_cancel: Callable[[], None] | None = None,
    progress: Callable[[float], None] | None = None,
    preset_name: str = "FAST",
) -> tuple[int, list[SpeechSegment]]:
    """Run the shared CPU Silero model with the selected global preset.

    ``progress`` receives a ratio in [0, 1]. The count describes the emitted
    VAD intervals, including forced cuts; no second merge/padding pass runs.
    """
    audio = _mono_audio(audio)
    if check_cancel is not None:
        check_cancel()
    if not len(audio):
        if progress is not None:
            progress(1.0)
        return 0, []

    model = SileroProbabilityModel(vad_model, num_threads=num_threads)
    settings = settings_for_preset(preset_name)

    def probabilities() -> Iterable[float]:
        frame_count = (len(audio) + FRAME_SIZE - 1) // FRAME_SIZE
        for index, start in enumerate(range(0, len(audio), FRAME_SIZE)):
            if check_cancel is not None:
                check_cancel()
            frame = audio[start : start + FRAME_SIZE]
            if len(frame) < FRAME_SIZE:
                frame = np.pad(frame, (0, FRAME_SIZE - len(frame)))
            probability = model.probability(frame)
            if progress is not None and (index % 128 == 0 or index + 1 == frame_count):
                progress((index + 1) / frame_count)
            yield probability

    segments = segments_from_probabilities(
        audio, probabilities(), check_cancel=check_cancel, settings=settings, preset_name=preset_name,
    )
    return len(segments), segments
