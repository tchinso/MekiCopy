from __future__ import annotations

import asyncio
from contextlib import suppress
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from fastapi import HTTPException, Response, WebSocketDisconnect

import hytrans_main
from hytrans import app as server
from hytrans import config
from hytrans.api_settings import ApiSettings, fingerprint, normalize_backend
from hytrans.browser import BrowserManager


class TranslatorApiConfigurationTests(unittest.TestCase):
    def test_browser_backend_has_no_api_credential_or_model_download_configuration(self) -> None:
        original_options = dict(vars(config.options))
        try:
            config.configure_server(backend="translator_api")
            public = config.runtime_config()
            self.assertEqual(normalize_backend("translator_api"), "translator_api")
            self.assertEqual(public.backend, "translator_api")
            self.assertEqual(public.modelId, "Browser Translator API")
            self.assertEqual(public.dtype, "browser")
            self.assertEqual(public.modelMode, "on-device")
            self.assertEqual(public.modelFiles, {})
            self.assertIsNone(config.selected_api_profile())
            self.assertIsNone(config.options.api_settings)
            self.assertEqual(fingerprint(ApiSettings(), "translator_api"), "translator_api")
        finally:
            for key, value in original_options.items():
                setattr(config.options, key, value)

    def test_cli_accepts_browser_backend_without_api_config(self) -> None:
        with mock.patch.object(sys, "argv", ["HYTrans.exe", "--backend", "translator_api"]):
            args = hytrans_main.parse_args()
        self.assertEqual(args.backend, "translator_api")
        self.assertIsNone(args.api_config)


class _FakeBrowserProcess:
    pid = 12345

    def __init__(self) -> None:
        self.exited = False

    def poll(self) -> int | None:
        return 0 if self.exited else None

    def wait(self, *, timeout: float | None = None) -> int:
        del timeout
        self.exited = True
        return 0

    def terminate(self) -> None:
        self.exited = True


class TranslatorApiBrowserProfileTests(unittest.TestCase):
    def test_restart_reuses_private_profile_and_keeps_browser_model_cache(self) -> None:
        url = "http://127.0.0.1:6996/translator_api_worker.html"
        browser = BrowserManager()
        commands: list[list[str]] = []

        def launch(command: list[str], **_kwargs: object) -> _FakeBrowserProcess:
            commands.append(command)
            return _FakeBrowserProcess()

        with tempfile.TemporaryDirectory(prefix="hytrans-translator-profile-") as temporary:
            profile_root = Path(temporary)
            with (
                mock.patch("hytrans.browser.chrome_profile_dir", return_value=profile_root),
                mock.patch.object(BrowserManager, "find_chrome", return_value="msedge.exe"),
                mock.patch("hytrans.browser.subprocess.Popen", side_effect=launch),
                mock.patch("hytrans.browser.subprocess.run", return_value=mock.Mock(returncode=0)),
                mock.patch.object(
                    BrowserManager,
                    "_activate_translator_api",
                    side_effect=[RuntimeError("initial click missed"), None, None],
                ) as activate,
            ):
                browser.start(url, translator_api=True)
                browser.activate_translator_api(url)
                profile = browser._profile_dir
                self.assertIsNotNone(profile)
                assert profile is not None
                self.assertEqual(profile.parent, profile_root)
                self.assertIn("translator-api-msedge-6996", profile.name)
                marker = profile / "browser-model-cache-marker"
                marker.write_text("retained", encoding="utf-8")
                self.assertTrue(browser.stop())
                self.assertEqual(marker.read_text(encoding="utf-8"), "retained")

                browser.start(url, translator_api=True)
                self.assertEqual(browser._profile_dir, profile)
                self.assertEqual(marker.read_text(encoding="utf-8"), "retained")
                self.assertTrue(browser.stop())
                self.assertTrue(marker.exists())

                self.assertEqual(len(commands), 2)
                for command in commands:
                    self.assertIn("--headless=new", command)
                    self.assertIn("--remote-debugging-port=0", command)
                    self.assertIn(f"--user-data-dir={profile}", command)
                    self.assertNotIn("--disable-background-networking", command)
                    self.assertEqual(command[-1], url)
                self.assertEqual(activate.call_count, 3)

    def test_interactive_fallback_opens_visible_app_and_skips_cdp(self) -> None:
        url = "http://127.0.0.1:6996/translator_api_worker.html"
        browser = BrowserManager()
        commands: list[list[str]] = []
        prompt_done = threading.Event()

        def launch(command: list[str], **_kwargs: object) -> _FakeBrowserProcess:
            commands.append(command)
            return _FakeBrowserProcess()

        with tempfile.TemporaryDirectory(prefix="hytrans-translator-profile-") as temporary:
            with (
                mock.patch("hytrans.browser.chrome_profile_dir", return_value=Path(temporary)),
                mock.patch.object(BrowserManager, "find_chrome", return_value="msedge.exe"),
                mock.patch("hytrans.browser.subprocess.Popen", side_effect=launch),
                mock.patch("hytrans.browser.subprocess.run", return_value=mock.Mock(returncode=0)),
                mock.patch.object(BrowserManager, "_activate_translator_api") as activate,
                mock.patch("hytrans.browser._show_interactive_prompt", side_effect=prompt_done.set) as prompt,
            ):
                browser.start(url, translator_api=True)
                original_profile = browser._profile_dir
                self.assertEqual(activate.call_count, 1)

                browser.start(url, translator_api=True, interactive=True)
                self.assertTrue(prompt_done.wait(timeout=1))
                self.assertEqual(browser._profile_dir, original_profile)
                browser.activate_translator_api(url)
                self.assertEqual(activate.call_count, 1)
                interactive_command = commands[-1]
                self.assertNotIn("--headless=new", interactive_command)
                self.assertNotIn("--remote-debugging-port=0", interactive_command)
                self.assertEqual(interactive_command[-1], f"--app={url}?manual=1")
                self.assertIn(f"--user-data-dir={original_profile}", interactive_command)

                self.assertTrue(browser.stop())
                browser.start(url, translator_api=True, interactive=True)
                self.assertTrue(browser.stop())
                self.assertEqual(prompt.call_count, 1)


class _WorkerSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.accepted = asyncio.Event()
        self.close_code: int | None = None
        self.headers: dict[str, str] = {}

    async def accept(self) -> None:
        self.accepted.set()

    async def receive_text(self) -> str:
        raw = await self.incoming.get()
        if raw is None:
            raise WebSocketDisconnect()
        return raw

    async def send_text(self, raw: str) -> None:
        await self.outgoing.put(json.loads(raw))

    async def close(self, code: int = 1000) -> None:
        self.close_code = code

    async def send(self, payload: dict[str, object]) -> None:
        await self.incoming.put(json.dumps(payload, ensure_ascii=False))


class TranslatorApiWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.original_options = dict(vars(config.options))
        self.original_state = dict(vars(server.state))
        self.original_queue = server.translation_queue
        self.original_queue_task = server.queue_task
        self.original_api_queue = server.api_translation_queue
        config.configure_server(backend="translator_api")
        await server.on_startup()

    async def asyncTearDown(self) -> None:
        await server.on_shutdown()
        server.translation_queue = self.original_queue
        server.queue_task = self.original_queue_task
        server.api_translation_queue = self.original_api_queue
        for key, value in self.original_options.items():
            setattr(config.options, key, value)
        for key, value in self.original_state.items():
            setattr(server.state, key, value)

    async def _wait_ready(self) -> None:
        async def wait() -> None:
            while not server.state.worker_ready:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait(), timeout=2)

    async def test_starts_browser_worker_queue_unready_without_paid_api_queue(self) -> None:
        self.assertIsNotNone(server.queue_task)
        self.assertFalse(server.queue_task.done())
        self.assertIsNone(server.api_translation_queue)
        self.assertFalse(server.state.worker_ready)
        self.assertFalse(server.state.worker_connected)
        ready = await server.ready()
        self.assertFalse(ready["ready"])
        health = await server.health(Response())
        self.assertEqual(health["backend"], "translator_api")
        self.assertFalse(health["ready"])
        with self.assertRaises(HTTPException) as raised:
            await server._translate_text("こんにちは")
        self.assertEqual(raised.exception.status_code, 503)

    async def test_matching_worker_ready_and_translation_forward_language_codes(self) -> None:
        worker = _WorkerSocket()
        worker_task = asyncio.create_task(server.worker_ws(worker))
        try:
            await asyncio.wait_for(worker.accepted.wait(), timeout=2)
            self.assertFalse(server.state.worker_ready)
            await worker.send({
                "type": "ready",
                "model": "Browser Translator API",
                "dtype": "browser",
                "modelMode": "on-device",
                "device": "browser",
            })
            await self._wait_ready()
            self.assertTrue(server.state.worker_connected)
            self.assertEqual((await server.ready())["backend"], "translator_api")

            translation = asyncio.create_task(server._translate_text("こんにちは"))
            payload = await asyncio.wait_for(worker.outgoing.get(), timeout=2)
            self.assertEqual(payload["type"], "translate")
            self.assertEqual(payload["text"], "こんにちは")
            self.assertEqual(payload["sourceLanguage"], "ja")
            self.assertEqual(payload["targetLanguage"], "ko")
            await worker.send({"type": "result", "id": payload["id"], "text": "안녕하세요"})
            self.assertEqual(await asyncio.wait_for(translation, timeout=2), "안녕하세요")
        finally:
            await worker.incoming.put(None)
            try:
                await asyncio.wait_for(worker_task, timeout=2)
            except asyncio.TimeoutError:
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task

    async def test_activation_failure_is_reported_to_waiting_worker(self) -> None:
        worker = _WorkerSocket()
        with mock.patch.object(
            server, "worker_activator", side_effect=RuntimeError("CDP click failed")
        ):
            worker_task = asyncio.create_task(server.worker_ws(worker))
            try:
                await asyncio.wait_for(worker.accepted.wait(), timeout=2)
                await worker.send({
                    "type": "activation_required",
                    "sourceLanguage": "ja",
                    "targetLanguage": "ko",
                })
                reply = await asyncio.wait_for(worker.outgoing.get(), timeout=2)
                self.assertEqual(reply, {
                    "type": "activation_error",
                    "message": "CDP click failed",
                    "sourceLanguage": "ja",
                    "targetLanguage": "ko",
                })
            finally:
                await worker.incoming.put(None)
                await asyncio.wait_for(worker_task, timeout=2)

    async def test_failed_automatic_click_switches_to_interactive_worker(self) -> None:
        worker = _WorkerSocket()
        interactive_opener = mock.Mock()
        with (
            mock.patch.object(server, "worker_activator", side_effect=RuntimeError("CDP click failed")),
            mock.patch.object(server, "worker_interactive_opener", interactive_opener),
        ):
            worker_task = asyncio.create_task(server.worker_ws(worker))
            await asyncio.wait_for(worker.accepted.wait(), timeout=2)
            await worker.send({"type": "activation_required"})
            await asyncio.wait_for(worker_task, timeout=2)
        interactive_opener.assert_called_once_with()
        self.assertFalse(server.state.worker_connected)
        self.assertFalse(server.state.worker_ready)

        replacement = _WorkerSocket()
        replacement_task = asyncio.create_task(server.worker_ws(replacement))
        try:
            await asyncio.wait_for(replacement.accepted.wait(), timeout=2)
            await replacement.send({
                "type": "ready", "model": "Browser Translator API",
                "dtype": "browser", "device": "browser",
            })
            await self._wait_ready()
            self.assertEqual(server.state.state, "READY")
        finally:
            await replacement.incoming.put(None)
            await asyncio.wait_for(replacement_task, timeout=2)

    async def test_browser_can_request_interactive_worker_after_click_rejection(self) -> None:
        worker = _WorkerSocket()
        interactive_opener = mock.Mock()
        with mock.patch.object(server, "worker_interactive_opener", interactive_opener):
            worker_task = asyncio.create_task(server.worker_ws(worker))
            await asyncio.wait_for(worker.accepted.wait(), timeout=2)
            await worker.send({
                "type": "manual_activation_required",
                "message": "user activation required",
            })
            await asyncio.wait_for(worker_task, timeout=2)
        interactive_opener.assert_called_once_with()
        self.assertFalse(server.state.worker_connected)
        self.assertEqual(server.state.error, "user activation required")

    async def test_loading_worker_expires_even_while_socket_stays_open(self) -> None:
        worker = _WorkerSocket()
        with mock.patch.object(server, "TRANSLATOR_API_STARTUP_TIMEOUT_SECONDS", 0.02):
            await asyncio.wait_for(server.worker_ws(worker), timeout=2)
        self.assertEqual(worker.close_code, 1011)
        self.assertFalse(server.state.worker_connected)
        self.assertFalse(server.state.worker_ready)
        self.assertEqual(server.state.state, "ERROR")
        self.assertIn("did not become ready", server.state.error or "")


if __name__ == "__main__":
    unittest.main()
