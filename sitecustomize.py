from __future__ import annotations

try:
    import onnxruntime as ort
except Exception:
    ort = None


if ort is not None and not hasattr(ort, "set_default_logger_severity"):

    def _noop_set_default_logger_severity(_level: int) -> None:
        return None

    ort.set_default_logger_severity = _noop_set_default_logger_severity
