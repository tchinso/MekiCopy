"""Bounded JSON object transport for local companion HTTP servers."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any

MAX_REQUEST_BYTES = 1024 * 1024


def read_json(handler: BaseHTTPRequestHandler, *, max_bytes: int = MAX_REQUEST_BYTES) -> dict[str, Any]:
    try:
        if handler.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is not supported")
        length = int(handler.headers.get("Content-Length", "0") or "0")
        if not 0 <= length <= max_bytes:
            raise ValueError("request body is too large")
        raw = handler.rfile.read(length) if length else b"{}"
        if len(raw) != length and length:
            raise ValueError("request body is incomplete")
    except ValueError:
        handler.close_connection = True
        raise
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
