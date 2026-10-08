from __future__ import annotations

import asyncio
from email.message import Message
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error

from fastapi.testclient import TestClient

import hytrans_main
from hytrans import app as server
from hytrans import config
from hytrans.api_client import (
    ApiTranslationError, ApiTranslationQueue, ApiTranslator,
    build_api_request, parse_api_response,
)
from hytrans.api_settings import (
    ApiSettings, PROVIDERS, ProviderSettings, api_settings_path, fingerprint,
    load_api_settings, render_prompt, save_api_settings, validate_provider_settings,
)
from hytrans.queue import TranslationQueueOverloadedError


def configured_settings() -> ApiSettings:
    settings = ApiSettings()
    for profile in settings.profiles.values():
        profile.api_key = "synthetic-test-key"
        profile.account_id = "a" * 32
    return settings


class ApiSettingsTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_keys_round_trip_encrypted_and_custom_models_prompt_remain_editable(self) -> None:
        settings = configured_settings()
        profile = settings.profiles["cloudflare"]
        profile.models = ["future/model", "custom/model"]
        profile.model = "future/model"
        profile.prompt = '{source} -> {target}: {text} {"literal": "100%"}'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "translation_api.json"
            self.assertTrue(save_api_settings(settings, path))
            saved_text = path.read_text(encoding="utf-8")
            self.assertNotIn("synthetic-test-key", saved_text)
            saved = json.loads(saved_text)
            self.assertEqual(saved["profiles"]["cloudflare"]["api_key"]["scheme"], "windows-dpapi")
            restored = load_api_settings(path)
            self.assertEqual(restored.profiles["cloudflare"], profile)

    def test_failed_key_protection_does_not_replace_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "translation_api.json"
            path.write_text("previous settings", encoding="utf-8")
            with mock.patch("hytrans.api_settings._dpapi", side_effect=OSError("failed")):
                self.assertFalse(save_api_settings(configured_settings(), path))
            self.assertEqual(path.read_text(encoding="utf-8"), "previous settings")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_bad_encrypted_key_reports_reentry_without_exposing_ciphertext(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "translation_api.json"
            path.write_text(json.dumps({"profiles": {"groq": {"api_key": {"scheme": "windows-dpapi", "value": "bad!"}}}}), encoding="utf-8")
            profile = load_api_settings(path).profiles["groq"]
            self.assertEqual(profile.api_key, "")
            self.assertIsNotNone(validate_provider_settings(profile, "groq"))
            self.assertNotIn("bad!", profile.credential_error)

    def test_default_path_honors_actual_settings_fallback(self) -> None:
        module = mock.Mock(SETTINGS_FILE="C:/fallback/settings.cfg")
        with mock.patch.dict(sys.modules, {"mekicopy_settings": module}):
            self.assertEqual(api_settings_path(), Path("C:/fallback/translation_api.json"))

    def test_prompt_substitution_preserves_literal_braces_and_input_placeholders(self) -> None:
        self.assertEqual(
            render_prompt('{source}>{target} {text} {"100%":true}', source="Japanese", target="Korean", text="入力 {source}"),
            'Japanese>Korean 入力 {source} {"100%":true}',
        )

    def test_validate_model_is_open_but_token_account_and_input_placeholder_are_required(self) -> None:
        profile = configured_settings().profiles["cloudflare"]
        profile.model = "@future/custom/model"
        self.assertIsNone(validate_provider_settings(profile, "cloudflare"))
        profile.account_id = "invalid/account"
        self.assertIsNotNone(validate_provider_settings(profile, "cloudflare"))
        profile.account_id = "a" * 32
        profile.api_key = "key\nInjected:header"
        self.assertIsNotNone(validate_provider_settings(profile, "cloudflare"))
        profile.api_key = "valid"
        profile.prompt = "translate without input"
        self.assertIsNotNone(validate_provider_settings(profile, "cloudflare"))

    def test_fingerprint_changes_for_selected_models_keys_and_prompts(self) -> None:
        settings = configured_settings()
        baseline = fingerprint(settings, "groq")
        settings.profiles["cloudflare"].api_key = "unrelated"
        self.assertEqual(fingerprint(settings, "groq"), baseline)
        for attribute, value in (("model", "future"), ("api_key", "new-key"), ("prompt", "{text} changed"), ("models", ["future"])):
            settings = configured_settings()
            setattr(settings.profiles["groq"], attribute, value)
            self.assertNotEqual(fingerprint(settings, "groq"), baseline)


class ApiTransportTests(unittest.TestCase):
    def test_three_official_urls_bearer_headers_and_provider_token_parameters(self) -> None:
        settings = configured_settings()
        urls = {
            "cloudflare": f"https://api.cloudflare.com/client/v4/accounts/{'a' * 32}/ai/v1/chat/completions",
            "deepinfra": "https://api.deepinfra.com/v1/openai/chat/completions",
            "groq": "https://api.groq.com/openai/v1/chat/completions",
        }
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                profile = settings.profiles[provider]
                profile.prompt = "{source}/{target}/{text}"
                request = build_api_request(provider, profile, "入力", source="Japanese", target="Korean", max_new_tokens=2048)
                self.assertEqual(request.full_url, urls[provider])
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-test-key")
                body = json.loads(request.data)
                self.assertEqual(body["messages"], [{"role": "user", "content": "Japanese/Korean/入力"}])
                self.assertIs(body["stream"], False)
                token_parameter = "max_completion_tokens" if provider == "groq" else "max_tokens"
                self.assertEqual(body[token_parameter], 2048)
                if provider == "groq":
                    self.assertIs(body["include_reasoning"], False)
                    self.assertNotIn("reasoning_format", body)
                if provider == "deepinfra":
                    self.assertEqual(body["reasoning_effort"], "none")

    def test_unknown_future_models_do_not_receive_groq_model_specific_options(self) -> None:
        profile = configured_settings().profiles["groq"]
        profile.model = "qwen/future-model"
        body = json.loads(build_api_request("groq", profile, "text").data)
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("include_reasoning", body)
        profile = configured_settings().profiles["deepinfra"]
        profile.model = "future/custom-model"
        body = json.loads(build_api_request("deepinfra", profile, "text").data)
        self.assertNotIn("reasoning_effort", body)

    def test_content_parsing_rejects_partial_empty_and_reasoning_only_outputs(self) -> None:
        self.assertEqual(parse_api_response({"choices": [{"message": {"content": "<think>reasoning</think>\n번역"}, "finish_reason": "stop"}]}), "번역")
        for choice in (
            {"message": {"content": "partial"}, "finish_reason": "length"},
            {"message": {"content": " "}, "finish_reason": "stop"},
            {"message": {"content": "<think>unfinished"}},
            {"message": {"content": None, "reasoning": "analysis"}},
            {"message": {"content": "unsafe"}, "finish_reason": "content_filter"},
        ):
            with self.subTest(choice=choice), self.assertRaises(ApiTranslationError):
                parse_api_response({"choices": [choice]})

    def test_mocked_http_response_and_timeout(self) -> None:
        translator = ApiTranslator("groq", configured_settings().profiles["groq"])
        translator._opener = mock.Mock()
        translator._opener.open.return_value = io.BytesIO(json.dumps({"choices": [{"message": {"content": "번역"}, "finish_reason": "stop"}]}).encode("utf-8"))
        self.assertEqual(translator.translate("入力"), "번역")
        translator._opener.open.side_effect = TimeoutError("synthetic-test-key")
        with self.assertRaises(ApiTranslationError) as raised:
            translator.translate("入力")
        self.assertEqual(raised.exception.status_code, 504)
        self.assertNotIn("synthetic-test-key", str(raised.exception))

    def test_provider_errors_never_echo_body_and_rate_limit_keeps_retry_after(self) -> None:
        translator = ApiTranslator("cloudflare", configured_settings().profiles["cloudflare"])
        translator._opener = mock.Mock()
        for status, expected in ((401, 502), (403, 502), (404, 502), (429, 429), (503, 503)):
            headers = Message()
            headers["Retry-After"] = "12"
            translator._opener.open.side_effect = urllib.error.HTTPError("https://provider", status, "synthetic-test-key", headers, io.BytesIO(b"synthetic-test-key"))
            with self.subTest(status=status), self.assertRaises(ApiTranslationError) as raised:
                translator.translate("入力")
            self.assertEqual(raised.exception.status_code, expected)
            self.assertNotIn("synthetic-test-key", str(raised.exception))
            if status == 429:
                self.assertEqual(raised.exception.retry_after, "12")


class ApiQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_admission_and_cancelled_pending_job_never_bills(self) -> None:
        started = threading.Event()
        release = threading.Event()
        translator = mock.Mock()
        def translate(text, **kwargs):
            started.set()
            self.assertTrue(release.wait(3))
            return "번역"
        translator.translate.side_effect = translate
        queue = ApiTranslationQueue(translator, concurrency=1, max_queued_jobs=1)
        queue.start()
        first = asyncio.create_task(queue.submit("first"))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        pending = asyncio.create_task(queue.submit("abandoned"))
        await asyncio.sleep(0)
        with self.assertRaises(TranslationQueueOverloadedError):
            await queue.submit("overflow")
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        release.set()
        self.assertEqual(await first, "번역")
        await queue.queue.join()
        self.assertEqual(translator.translate.call_count, 1)
        await queue.stop()


class ApiRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_options = dict(vars(config.options))
        self.original_state = dict(vars(server.state))
        config.options.backend = "groq"
        config.options.api_settings = configured_settings()
        self.mock_translate = mock.patch.object(ApiTranslator, "translate", return_value="번역").start()

    def tearDown(self) -> None:
        mock.patch.stopall()
        for key, value in self.original_options.items():
            setattr(config.options, key, value)
        for key, value in self.original_state.items():
            setattr(server.state, key, value)

    def test_all_translation_routes_ready_health_config_and_no_model_download(self) -> None:
        with TestClient(server.app, client=("127.0.0.1", 10001)) as client:
            health = client.get("/health").json()
            self.assertEqual(health["backend"], "groq")
            self.assertTrue(health["ready"])
            self.assertFalse(health["workerConnected"])
            self.assertTrue(client.get("/ready").json()["ready"])
            public = client.get("/config").json()
            self.assertEqual(public["modelFiles"], {})
            self.assertEqual(public["promptTemplate"], "")
            self.assertNotIn("synthetic-test-key", json.dumps(public))
            self.assertEqual(client.get("/translate", params={"text": "入力", "format": "json", "source": "French", "target": "Korean"}).json()["text"], "번역")
            self.assertEqual(client.post("/translate", json={"text": "入力", "realtime": True}).text, "번역")
            with mock.patch.object(server, "_send_to_overlay", new=mock.AsyncMock()) as send:
                response = client.post("/translate-and-show", json={"text": "入力", "overlayUrl": "http://127.0.0.1:8888/show"})
                self.assertEqual(response.json()["text"], "번역")
                send.assert_awaited_once_with("번역", "http://127.0.0.1:8888/show")
            with mock.patch.object(server.model_download_manager, "start", side_effect=AssertionError("API downloaded a model")):
                self.assertEqual(client.post("/model/prepare").json()["state"], "API")
            self.assertEqual(self.mock_translate.call_args_list[0].kwargs["source"], "French")
            self.assertEqual(self.mock_translate.call_args_list[1].kwargs["max_new_tokens"], config.MAX_NEW_TOKENS)

    def test_paid_routes_reject_cross_site_origin_fetch_and_nonloopback_clients(self) -> None:
        with TestClient(server.app, client=("127.0.0.1", 10001)) as client:
            for headers in ({"Origin": "https://attacker.example"}, {"Sec-Fetch-Site": "cross-site"}):
                self.assertEqual(client.get("/translate", params={"text": "入力"}, headers=headers).status_code, 403)
                self.assertEqual(client.post("/translate", json={"text": "入力"}, headers=headers).status_code, 403)
                self.assertEqual(client.post("/translate-and-show", json={"text": "入力"}, headers=headers).status_code, 403)
            self.assertEqual(client.post("/translate", json={"text": "入力"}, headers={"Origin": f"http://127.0.0.1:{config.options.port}"}).status_code, 200)
        with TestClient(server.app, client=("192.0.2.1", 10001)) as client:
            self.assertEqual(client.get("/translate", params={"text": "入力"}).status_code, 403)
        self.assertEqual(self.mock_translate.call_count, 1)

    def test_missing_key_is_unready_without_network_or_model_worker(self) -> None:
        config.options.api_settings.profiles["groq"].api_key = ""
        with TestClient(server.app, client=("127.0.0.1", 10001)) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertFalse(client.get("/ready").json()["ready"])
            self.assertEqual(client.post("/translate", json={"text": "入力"}).status_code, 503)
            self.assertIsNone(server.queue_task)
            self.mock_translate.assert_not_called()

    def test_rate_limit_response_reaches_callers_but_service_remains_ready(self) -> None:
        self.mock_translate.side_effect = ApiTranslationError("rate limit", status_code=429, retry_after="3")
        with TestClient(server.app, client=("127.0.0.1", 10001)) as client:
            result = client.post("/translate", json={"text": "入力"})
            self.assertEqual(result.status_code, 429)
            self.assertEqual(result.headers["retry-after"], "3")
            self.assertTrue(client.get("/ready").json()["ready"])

    def test_cli_defaults_local_and_accepts_encrypted_api_path(self) -> None:
        with mock.patch.object(sys, "argv", ["HYTrans.exe"]):
            self.assertEqual(hytrans_main.parse_args().backend, "local")
        with mock.patch.object(sys, "argv", ["HYTrans.exe", "--backend", "cloudflare", "--api-config", "C:/settings/translation_api.json"]):
            args = hytrans_main.parse_args()
        self.assertEqual(args.backend, "cloudflare")
        self.assertEqual(args.api_config, "C:/settings/translation_api.json")


if __name__ == "__main__":
    unittest.main()
