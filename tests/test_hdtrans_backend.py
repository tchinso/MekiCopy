from __future__ import annotations

import asyncio
import ctypes
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from hytrans import app as server, config
from hytrans.api_settings import ApiSettings, PROVIDERS, fingerprint
from hytrans.hdtrans_backend import HdTransBackend, HdTransError, NativeLibrary, validate_dll_architecture
from hytrans.hdtrans_download import VERSION
from hytrans.hdtrans_settings import HdTransSettings, load_hdtrans_settings, save_hdtrans_settings, hdtrans_settings_path
from hytrans.queue import TranslationQueueOverloadedError
from meki_subtitle_pipeline import _translation_callable


def write_pe(path: Path, machine: int = 0x8664) -> None:
    data = bytearray(256)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, machine)
    path.write_bytes(data)


class FakeLibrary:
    def __init__(self, path: Path) -> None:
        self.calls = []
        self.threads = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.hold = False
        self.error = ""

    def record(self, name, *args):
        self.calls.append((name, *args))
        self.threads.append(threading.get_ident())

    def initialize(self, dictionary):
        self.record("initialize", dictionary)

    def version(self):
        self.record("version")
        return VERSION

    def diagnostics(self):
        self.record("diagnostics")
        return self.error

    def reset(self):
        self.record("reset")

    def reload(self):
        self.record("reload")

    def translate(self, text):
        self.record("translate", text)
        self.started.set()
        if self.hold:
            if not self.release.wait(5):
                raise RuntimeError("test release timeout")
        return "번역: " + text

    def close(self):
        self.record("terminate")


class NativeCtypesTests(unittest.TestCase):
    def test_truncated_pe_headers_report_engine_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hdTransEngine.dll"
            path.write_bytes(b"MZ")
            with self.assertRaisesRegex(HdTransError, "truncated DOS"):
                validate_dll_architecture(path)
            write_pe(path)
            path.write_bytes(path.read_bytes()[:0x84])
            with self.assertRaisesRegex(HdTransError, "truncated PE"):
                validate_dll_architecture(path)

    def test_declares_size_t_utf8_byte_length_original_pointer_and_finally_free(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hdTransEngine.dll"
            write_pe(path)
            dll = mock.Mock()
            buffer = ctypes.create_string_buffer("번역".encode())
            pointer = ctypes.addressof(buffer)
            def translate(text, length, out, outn):
                self.assertEqual(length, len("日本語".encode()))
                self.assertEqual(text, "日本語".encode())
                ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = pointer
                ctypes.cast(outn, ctypes.POINTER(ctypes.c_size_t))[0] = len("번역".encode())
                return 0
            dll.hdTrans_Translate.side_effect = translate
            native = NativeLibrary(path, loader=lambda _: dll)
            self.assertIs(dll.hdTrans_Translate.argtypes[1], ctypes.c_size_t)
            self.assertIs(dll.hdTrans_Translate.argtypes[2]._type_, ctypes.c_void_p)
            self.assertEqual(native.translate("日本語"), "번역")
            self.assertEqual(dll.hdTrans_FreeMem.call_args.args[0].value, pointer)
            buffer[0] = b"\xff"
            with self.assertRaises(UnicodeDecodeError):
                native.translate("日本語")
            self.assertEqual(dll.hdTrans_FreeMem.call_count, 2)
            with self.assertRaisesRegex(HdTransError, "Only one"):
                NativeLibrary(path, loader=lambda _: dll)
            native.close()

    def test_architecture_mismatch_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong.dll"
            write_pe(path, 0x14C if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x8664)
            with self.assertRaisesRegex(HdTransError, "architecture mismatch"):
                validate_dll_architecture(path)

    def test_noncloud_classification_and_settings_roundtrip(self):
        self.assertEqual(PROVIDERS, ("cloudflare", "deepinfra", "groq"))
        self.assertEqual(fingerprint(ApiSettings(), "hdtrans"), "hdtrans")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            settings = HdTransSettings(False, [str(Path(directory) / "user.tsv")])
            self.assertTrue(save_hdtrans_settings(settings, path))
            self.assertEqual(load_hdtrans_settings(path), settings)

    def test_server_uses_controller_config_directory_in_frozen_sibling_layout(self):
        from hytrans.hdtrans_settings import configure_hdtrans_settings_path
        original = dict(vars(config.options))
        try:
            config.configure_server(backend="hdtrans", api_config="C:/suite/MekiCopy/translation_api.json")
            self.assertEqual(hdtrans_settings_path(), Path("C:/suite/MekiCopy/hdtrans_settings.json"))
        finally:
            configure_hdtrans_settings_path(None)
            for key, value in original.items():
                setattr(config.options, key, value)


class NativeQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.native = FakeLibrary(root)
        self.backend = HdTransBackend(max_queued_jobs=1, settings=HdTransSettings(),
            installer=lambda **kwargs: root, library_factory=lambda _: self.native)
        await self.backend.submit("initialize")

    async def asyncTearDown(self):
        self.native.release.set()
        await self.backend.close()
        self.temporary.cleanup()

    async def test_context_same_owner_preserved_owner_boundary_and_manual_reset(self):
        await self.backend.submit("translate", "first", "ocr:1", False)
        await self.backend.submit("translate", "second", "ocr:1", False)
        self.assertEqual([c[0] for c in self.native.calls].count("reset"), 1)
        await self.backend.submit("translate", "audio", "audio", False)
        await self.backend.submit("reset")
        self.assertEqual([c[0] for c in self.native.calls].count("reset"), 3)
        self.assertEqual(len(set(self.native.threads)), 1)

    async def test_option_transition_reload_and_translate_are_serialized(self):
        with mock.patch("hytrans.hdtrans_backend.save_hdtrans_settings", return_value=True):
            await self.backend.submit("settings", HdTransSettings(False))
        await self.backend.submit("translate", "one", "ocr", False)
        await self.backend.submit("translate", "two", "ocr", False)
        self.assertEqual([c[0] for c in self.native.calls].count("reset"), 3)
        await self.backend.submit("reload")
        self.assertIn(("reload",), self.native.calls)

    async def test_bounded_queue_pending_cancel_and_event_loop_remains_responsive(self):
        self.native.hold = True
        first = asyncio.create_task(self.backend.submit("translate", "first", "ocr", False))
        self.assertTrue(await asyncio.to_thread(self.native.started.wait, 2))
        pending = asyncio.create_task(self.backend.submit("translate", "abandoned", "ocr", False))
        await asyncio.sleep(0)
        with self.assertRaises(TranslationQueueOverloadedError):
            await self.backend.submit("reset")
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        # If the native call blocked the event loop this coroutine cannot run.
        self.assertEqual(await asyncio.wait_for(asyncio.sleep(0, result="responsive"), 0.5), "responsive")
        self.native.release.set()
        self.assertEqual(await first, "번역: first")
        await asyncio.to_thread(self.backend.queue.join)
        self.assertNotIn(("translate", "abandoned"), self.native.calls)

    async def test_timeout_does_not_terminate_running_call_and_resets_before_next(self):
        self.native.hold = True
        with self.assertRaises(asyncio.TimeoutError):
            await self.backend.submit("translate", "late", "ocr", False, timeout=0.02)
        self.assertNotIn(("terminate",), self.native.calls)
        self.assertEqual(self.backend.status()["state"], "NATIVE_TIMEOUT")
        self.assertFalse(self.backend.status()["ready"])
        self.native.release.set()
        await self.backend.submit("translate", "next", "ocr", False)
        self.assertTrue(self.backend.status()["ready"])
        names = [call[0] for call in self.native.calls]
        first, second = [i for i, name in enumerate(names) if name == "translate"]
        self.assertIn("reset", names[first + 1:second])

    async def test_shutdown_waits_for_active_call_before_terminate(self):
        self.native.hold = True
        translation = asyncio.create_task(self.backend.submit("translate", "active", "ocr", False))
        self.assertTrue(await asyncio.to_thread(self.native.started.wait, 2))
        closing = asyncio.create_task(self.backend.close())
        await asyncio.sleep(0.02)
        self.assertFalse(closing.done())
        self.assertNotIn(("terminate",), self.native.calls)
        self.native.release.set()
        await translation
        await closing
        self.assertEqual(self.native.calls[-1], ("terminate",))


class NativeApiTests(unittest.TestCase):
    def test_initialization_is_nonblocking_cancelled_install_can_retry(self):
        original_options, original_state = dict(vars(config.options)), dict(vars(server.state))
        started, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            native = FakeLibrary(Path(directory))
            def installer(*, cancel_event, progress):
                started.set()
                if not release.wait(3):
                    raise RuntimeError("test initialization release timeout")
                if cancel_event.is_set():
                    raise HdTransError("install cancelled")
                return Path(directory)
            def factory():
                return HdTransBackend(settings=HdTransSettings(), installer=installer, library_factory=lambda _: native)
            try:
                config.options.backend = "hdtrans"
                with mock.patch.object(server, "HdTransBackend", side_effect=factory):
                    with TestClient(server.app, client=("127.0.0.1", 10001)) as client:
                        try:
                            self.assertTrue(started.wait(2))
                            self.assertEqual(client.get("/health").status_code, 200)
                            self.assertFalse(client.get("/ready").json()["ready"])
                            self.assertEqual(client.post("/hdtrans/download/cancel").status_code, 200)
                        finally:
                            release.set()
                        for _ in range(100):
                            snapshot = client.get("/hdtrans/status").json()
                            if snapshot["state"] == "CANCELLED":
                                break
                        self.assertEqual(snapshot["state"], "CANCELLED")
                        self.assertNotIn("initialize", [call[0] for call in native.calls])
                        client.post("/model/prepare")
                        for _ in range(100):
                            if client.get("/ready").json()["ready"]:
                                break
                        self.assertTrue(client.get("/ready").json()["ready"])
            finally:
                release.set()
                for key, value in original_options.items():
                    setattr(config.options, key, value)
                for key, value in original_state.items():
                    setattr(server.state, key, value)

    def test_native_ready_without_browser_preserves_routes_and_work_boundary(self):
        original_options, original_state = dict(vars(config.options)), dict(vars(server.state))
        with tempfile.TemporaryDirectory() as directory:
            native = FakeLibrary(Path(directory))
            def factory():
                return HdTransBackend(settings=HdTransSettings(), installer=lambda **kwargs: Path(directory),
                                      library_factory=lambda _: native)
            try:
                config.options.backend = "hdtrans"
                with mock.patch.object(server, "HdTransBackend", side_effect=factory):
                    with TestClient(server.app, client=("127.0.0.1", 10001)) as client:
                        for _ in range(100):
                            if client.get("/ready").json()["ready"]:
                                break
                        ready = client.get("/ready").json()
                        self.assertTrue(ready["ready"])
                        self.assertFalse(ready["workerConnected"])
                        self.assertEqual(client.get("/health").json()["dtype"], "native")
                        self.assertEqual(client.get("/config").json()["modelFiles"], {})
                        self.assertTrue(client.get("/model/status").json()["complete"])
                        self.assertEqual(client.post("/translate?format=json", json={"text": "日本語", "contextOwner": "subtitle:file"}).json()["text"], "번역: 日本語")
                        self.assertEqual(client.get("/hdtrans/status").json()["contextOwner"], "subtitle:file")
                        self.assertEqual(client.post("/hdtrans/context/reset").status_code, 200)
                        self.assertEqual(client.post("/translate", json={"text": "hello", "source": "English"}).status_code, 400)
                        with mock.patch.object(server, "_send_to_overlay", new=mock.AsyncMock()):
                            self.assertEqual(client.post("/translate-and-show", json={"text": "入力"}).json()["backend"], "hdtrans")
                        self.assertEqual(client.put("/hdtrans/settings", json={"preserveContext": False}, headers={"Origin": "https://example.com"}).status_code, 403)
            finally:
                for key, value in original_options.items():
                    setattr(config.options, key, value)
                for key, value in original_state.items():
                    setattr(server.state, key, value)

    def test_each_subtitle_file_uses_fresh_boundary_shared_by_its_cues(self):
        with mock.patch("meki_subtitle_pipeline.translate_via_hytrans", return_value="번역") as translate:
            first = _translation_callable(translate=None, translation_url="http://127.0.0.1:6996")
            first("one")
            first("two")
            second = _translation_callable(translate=None, translation_url="http://127.0.0.1:6996")
            second("three")
        owners = [call.kwargs["context_owner"] for call in translate.call_args_list]
        self.assertEqual(owners[0], owners[1])
        self.assertNotEqual(owners[1], owners[2])


if __name__ == "__main__":
    unittest.main()
