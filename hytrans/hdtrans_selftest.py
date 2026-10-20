"""Offline frozen-runtime smoke check using the actual native C API."""
from __future__ import annotations

import asyncio

from .hdtrans_backend import HdTransBackend
from .hdtrans_settings import HdTransSettings


async def _run() -> dict:
    backend = HdTransBackend(settings=HdTransSettings())
    try:
        status = await backend.submit("initialize", timeout=120)
        if not status.get("ready"):
            raise RuntimeError("hdTrans did not become ready")
        first = await backend.submit("translate", "今日は良い天気です。", "self-test", False)
        await backend.submit("reset")
        second = await backend.submit("translate", "今日は良い天気です。", "self-test", False)
        if not first or first != second:
            raise RuntimeError("hdTrans reset/translation smoke failed")
        return {"ok": True, "backend": "hdtrans", "version": status.get("version"),
                "workerConnected": False, "text": first}
    finally:
        await backend.close()


def run_hdtrans_self_test() -> dict:
    return asyncio.run(_run())
