"""Offline executable check: no provider request and no user state changes."""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import tempfile

from .api_client import ApiTranslationError, ApiTranslationQueue, ApiTranslator, parse_api_response
from .api_settings import ApiSettings, PROVIDERS, load_api_settings, save_api_settings


class _OfflineResponse(io.BytesIO):
    def __init__(self) -> None:
        super().__init__(json.dumps({"choices": [{"message": {"content": "번역 확인"}, "finish_reason": "stop"}]}).encode("utf-8"))


class _OfflineOpener:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.calls = 0

    def open(self, request, *, timeout):
        expected = {
            "cloudflare": "https://api.cloudflare.com/client/v4/accounts/00000000000000000000000000000000/ai/v1/chat/completions",
            "deepinfra": "https://api.deepinfra.com/v1/openai/chat/completions",
            "groq": "https://api.groq.com/openai/v1/chat/completions",
        }
        assert request.full_url == expected[self.provider]
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer hytrans-offline-test-token"
        body = json.loads(request.data.decode("utf-8"))
        assert body["stream"] is False
        assert body["messages"][0]["role"] == "user"
        assert "日本語" in body["messages"][0]["content"]
        assert body["max_completion_tokens" if self.provider == "groq" else "max_tokens"] == 2048
        if self.provider == "groq":
            assert body["include_reasoning"] is False
            assert "reasoning_format" not in body
        assert timeout > 0
        self.calls += 1
        return _OfflineResponse()


async def _check_queues(settings: ApiSettings) -> None:
    for provider in PROVIDERS:
        translator = ApiTranslator(provider, settings.profiles[provider])
        offline = _OfflineOpener(provider)
        translator._opener = offline
        queue = ApiTranslationQueue(translator)
        queue.start()
        try:
            assert await queue.submit("日本語") == "번역 확인"
            assert offline.calls == 1
        finally:
            await queue.stop()


def run_api_self_test() -> dict[str, object]:
    settings = ApiSettings()
    for profile in settings.profiles.values():
        profile.api_key = "hytrans-offline-test-token"
        profile.account_id = "0" * 32
    asyncio.run(_check_queues(settings))
    for payload in ({"choices": []}, {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}):
        try:
            parse_api_response(payload)
        except ApiTranslationError:
            pass
        else:
            raise RuntimeError("API response validation failed")
    protected = False
    if os.name == "nt":
        with tempfile.TemporaryDirectory(prefix="hytrans-api-selftest-") as directory:
            path = Path(directory) / "translation_api.json"
            assert save_api_settings(settings, path)
            assert "hytrans-offline-test-token" not in path.read_text(encoding="utf-8")
            restored = load_api_settings(path)
            assert all(restored.profiles[key].api_key == settings.profiles[key].api_key for key in PROVIDERS)
            protected = True
    return {"ok": True, "providers": list(PROVIDERS), "offline": True, "dpapi": protected}
