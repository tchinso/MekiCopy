"""Shared localhost service port defaults and validation."""

from __future__ import annotations


HYTRANS_DEFAULT_PORT = 6996
OVERLAYER_DEFAULT_PORT = 6997
AUDIO_CAPTURE_DEFAULT_PORT = 6998
SCRIPT_DEFAULT_PORT = 6999

SERVICE_DEFAULT_PORTS = {
    "HYTrans": HYTRANS_DEFAULT_PORT,
    "MekiOverlayer": OVERLAYER_DEFAULT_PORT,
    "MekiAudioCapture": AUDIO_CAPTURE_DEFAULT_PORT,
    "MekiScript": SCRIPT_DEFAULT_PORT,
}


def normalize_port(value: object, fallback: int | None = None) -> int:
    """Return a valid TCP port, or the validated fallback when supplied."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        if fallback is None:
            raise ValueError("port must be a number from 1 to 65535") from None
        port = int(fallback)
    if not 1 <= port <= 65535:
        if fallback is None:
            raise ValueError("port must be a number from 1 to 65535")
        port = int(fallback)
    if not 1 <= port <= 65535:
        raise ValueError("fallback port must be a number from 1 to 65535")
    return port


def validate_unique_ports(ports: dict[str, int]) -> None:
    """Raise a user-readable error when two local services share a port."""
    owners: dict[int, str] = {}
    for name, raw_port in ports.items():
        port = normalize_port(raw_port)
        previous = owners.get(port)
        if previous is not None:
            raise ValueError(f"{previous}와 {name}의 포트 번호({port})는 같을 수 없습니다.")
        owners[port] = name
