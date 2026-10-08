from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
import socket
import urllib.error
import urllib.request

from .api_settings import BACKEND_LABELS, ProviderSettings, render_prompt, validate_provider_settings
from .queue import TranslationQueueOverloadedError


API_TIMEOUT_SECONDS = 90
MAX_RESPONSE_BYTES = 1024 * 1024
_THINK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


class ApiTranslationError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Provider endpoints are fixed HTTPS URLs. A redirect must never carry
        # the user's bearer token to another origin.
        return None


def api_endpoint(provider: str, profile: ProviderSettings) -> str:
    if provider == "cloudflare":
        return f"https://api.cloudflare.com/client/v4/accounts/{profile.account_id.strip()}/ai/v1/chat/completions"
    if provider == "deepinfra":
        return "https://api.deepinfra.com/v1/openai/chat/completions"
    if provider == "groq":
        return "https://api.groq.com/openai/v1/chat/completions"
    raise ApiTranslationError("Unsupported translation API provider", status_code=503)


def build_api_request(provider: str, profile: ProviderSettings, text: str,
                      *, source: str = "Japanese", target: str = "Korean",
                      max_new_tokens: int = 2048) -> urllib.request.Request:
    issue = validate_provider_settings(profile, provider)
    if issue:
        raise ApiTranslationError(issue, status_code=503)
    model = profile.model.strip()
    prompt = render_prompt(profile.prompt, source=source, target=target, text=text)
    body: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    body["max_completion_tokens" if provider == "groq" else "max_tokens"] = max_new_tokens
    # These model-specific options are documented by their providers. Hide
    # reasoning when the model can return it separately and leave arbitrary
    # future custom model IDs free of unsupported provider parameters.
    if provider == "groq" and model.startswith("openai/gpt-oss-"):
        body["reasoning_effort"] = "low"
        body["include_reasoning"] = False
    if provider == "groq" and model in {"qwen/qwen3.6-27b", "qwen/qwen3.8-27b", "qwen/qwen3-32b"}:
        body["reasoning_effort"] = "none"
    if provider == "deepinfra" and model in {
        "deepseek-ai/DeepSeek-V4-Flash-0731", "deepseek-ai/DeepSeek-V4-Pro-0813",
        "zai-org/GLM-5.2", "moonshotai/Kimi-K3", "inclusionAI/Ling-3.0-flash",
    }:
        body["reasoning_effort"] = "none"
    return urllib.request.Request(
        api_endpoint(provider, profile),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {profile.api_key.strip()}",
                 "Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "HYTrans/1.0"},
        method="POST",
    )


def parse_api_response(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ApiTranslationError("Translation API returned an invalid response")
    if payload.get("error") or payload.get("success") is False:
        raise ApiTranslationError("Translation API rejected the request")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ApiTranslationError("Translation API returned no translation")
    choice = choices[0]
    if choice.get("finish_reason") in {"length", "max_tokens"}:
        raise ApiTranslationError("Translation API output was truncated; try a shorter input or another model")
    if choice.get("finish_reason") in {"content_filter", "tool_calls", "function_call"}:
        raise ApiTranslationError("Translation API did not return a complete text translation")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    if not isinstance(content, str):
        raise ApiTranslationError("Translation API returned no text translation")
    text = _THINK_RE.sub("", content).strip()
    if not text or text.lower().startswith("<think>"):
        raise ApiTranslationError("Translation API returned an empty translation or unfinished reasoning")
    return text


def _http_error(exc: urllib.error.HTTPError, provider: str) -> ApiTranslationError:
    label = BACKEND_LABELS[provider]
    if exc.code in {401, 403}:
        detail = f"{label}: API authentication failed; check the API key and permissions"
        if provider == "cloudflare":
            detail += " and Account ID"
        return ApiTranslationError(detail, status_code=502)
    if exc.code == 429:
        value = exc.headers.get("Retry-After", "") if exc.headers else ""
        retry_after = value if value.isdigit() and len(value) <= 6 else None
        return ApiTranslationError(f"{label}: API rate limit or capacity exceeded; retry later", status_code=429, retry_after=retry_after)
    if exc.code in {400, 404, 422}:
        return ApiTranslationError(f"{label}: API request rejected; check the model ID and settings")
    if exc.code >= 500:
        return ApiTranslationError(f"{label}: API service is temporarily unavailable", status_code=503)
    return ApiTranslationError(f"{label}: API returned HTTP {exc.code}")


class ApiTranslator:
    def __init__(self, provider: str, profile: ProviderSettings) -> None:
        self.provider = provider
        self.profile = profile
        self._opener = urllib.request.build_opener(_NoRedirect())

    def translate(self, text: str, *, source: str = "Japanese", target: str = "Korean",
                  max_new_tokens: int = 2048) -> str:
        request = build_api_request(self.provider, self.profile, text, source=source,
                                    target=target, max_new_tokens=max_new_tokens)
        try:
            with self._opener.open(request, timeout=API_TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ApiTranslationError("Translation API response is too large")
                payload = json.loads(raw.decode("utf-8"))
            return parse_api_response(payload)
        except urllib.error.HTTPError as exc:
            # Never expose provider response bodies: services can echo request
            # parameters, prompts, or credentials in error messages.
            failure = _http_error(exc, self.provider)
            exc.close()
            raise failure from None
        except (TimeoutError, socket.timeout):
            raise ApiTranslationError("Translation API request timed out", status_code=504) from None
        except urllib.error.URLError as exc:
            code = 504 if isinstance(exc.reason, (TimeoutError, socket.timeout)) else 502
            raise ApiTranslationError("Translation API connection failed; check your network", status_code=code) from None
        except (ValueError, UnicodeError, TypeError):
            raise ApiTranslationError("Translation API returned an invalid JSON response") from None


@dataclass
class _ApiJob:
    text: str
    source: str
    target: str
    max_new_tokens: int
    future: asyncio.Future[str]


class ApiTranslationQueue:
    """Bound both paid API requests in flight and retained waiting requests."""

    def __init__(self, translator: ApiTranslator, *, concurrency: int = 2, max_queued_jobs: int = 16) -> None:
        self.translator = translator
        self.queue: asyncio.Queue[_ApiJob] = asyncio.Queue(maxsize=max_queued_jobs)
        self.tasks: list[asyncio.Task] = []
        self.concurrency = concurrency
        self.stopped = False

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self._run()) for _ in range(self.concurrency)]

    async def submit(self, text: str, *, source: str = "Japanese", target: str = "Korean",
                     max_new_tokens: int = 2048) -> str:
        if self.stopped:
            raise ApiTranslationError("Translation API server is stopping", status_code=503)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        job = _ApiJob(text, source, target, max_new_tokens, future)
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            raise TranslationQueueOverloadedError("translation queue is busy; retry shortly") from None
        try:
            return await asyncio.wait_for(future, timeout=API_TIMEOUT_SECONDS + 30)
        except asyncio.TimeoutError:
            raise ApiTranslationError("Translation API queue or request timed out", status_code=504) from None

    async def _run(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job.future.done():
                    continue
                # An HTTP client disconnect cancels its result future, while
                # this worker continues owning the socket until its bounded
                # timeout ends. It cannot admit unlimited abandoned requests.
                result = await asyncio.to_thread(
                    self.translator.translate, job.text, source=job.source,
                    target=job.target, max_new_tokens=job.max_new_tokens,
                )
                if not job.future.done():
                    job.future.set_result(result)
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.cancel()
                raise
            except ApiTranslationError as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
            except Exception:
                if not job.future.done():
                    job.future.set_exception(ApiTranslationError("Translation API request failed"))
            finally:
                self.queue.task_done()

    async def stop(self) -> None:
        self.stopped = True
        while not self.queue.empty():
            job = self.queue.get_nowait()
            if not job.future.done():
                job.future.set_exception(ApiTranslationError("Translation API server is stopping", status_code=503))
            self.queue.task_done()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
