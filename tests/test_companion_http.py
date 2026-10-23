from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from companion_http import urlopen


@contextmanager
def local_server(request_seen=None):
    requests = []
    requests_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                # Timeout and bounded-cleanup tests deliberately disconnect
                # while the server is sleeping or waiting for another request.
                pass

        def do_GET(self):
            self.do_POST()

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            with requests_lock:
                requests.append((self.command, self.path, body, dict(self.headers)))
            if request_seen is not None:
                request_seen.set()
            if self.path == "/disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            if self.path == "/timeout":
                time.sleep(0.2)
            status = 409 if self.path == "/error" else 307 if self.path == "/redirect" else 200
            payload = json.dumps({"text": "응답", "body": body.decode("utf-8")}, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Companion", "test")
            if status == 307:
                self.send_header("Location", "/ok")
            self.end_headers()
            try:
                self.wfile.write(payload)
                if self.path == "/slow-close":
                    # The request deadline is intentionally much longer than
                    # this server's failure to promptly close after a FIN.
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class CompanionHttpTests(unittest.TestCase):
    def test_repeated_real_responses_preserve_body_headers_and_do_not_request_server_close(self):
        with local_server() as (base_url, requests):
            for _ in range(40):
                request = urllib.request.Request(
                    base_url + "/ok?value=1", data="본문".encode("utf-8"),
                    headers={"Content-Type": "text/plain; charset=utf-8", "Connection": "close"},
                )
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getcode(), 200)
                    self.assertEqual(response.geturl(), request.full_url)
                    self.assertEqual(response.getheader("X-Companion"), "test")
                    self.assertEqual(response.info()["X-Companion"], "test")
                    self.assertEqual(json.loads(response.read()), {"text": "응답", "body": "본문"})
                self.assertTrue(response.closed)
            self.assertEqual(len(requests), 40)
            self.assertTrue(all(item[0] == "POST" for item in requests))
            self.assertTrue(all("Connection" not in item[3] for item in requests))

    def test_simultaneous_commands_have_independent_connections_and_bodies(self):
        with local_server() as (base_url, requests):
            def send(index):
                with urlopen(base_url + "/ok", data=str(index).encode(), timeout=2) as response:
                    return json.loads(response.read())["body"]

            with ThreadPoolExecutor(max_workers=8) as executor:
                replies = list(executor.map(send, range(24)))
            self.assertEqual(replies, [str(index) for index in range(24)])
            self.assertEqual(len(requests), 24)

    def test_http_error_has_status_headers_and_readable_original_body(self):
        with local_server() as (base_url, requests):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urlopen(base_url + "/error", data=b"command", timeout=2)
            with raised.exception as error:
                self.assertEqual(error.code, 409)
                self.assertEqual(error.headers["X-Companion"], "test")
                self.assertEqual(json.loads(error.read())["body"], "command")
            self.assertEqual(len(requests), 1)

    def test_lost_post_response_is_not_retried(self):
        with local_server() as (base_url, requests):
            with self.assertRaises((OSError, urllib.error.URLError)):
                urlopen(base_url + "/disconnect", data=b"execute-once", timeout=2)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][2], b"execute-once")

    def test_post_redirect_does_not_replay_a_command(self):
        with local_server() as (base_url, requests):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urlopen(base_url + "/redirect", data=b"execute-once", timeout=2)
            raised.exception.close()
            self.assertEqual(raised.exception.code, 307)
            self.assertEqual(len(requests), 1)

    def test_response_timeout_does_not_retry_a_post(self):
        request_seen = threading.Event()
        with local_server(request_seen) as (base_url, requests):
            with self.assertRaises(TimeoutError):
                urlopen(base_url + "/timeout", data=b"execute-once", timeout=0.03)
            self.assertTrue(request_seen.wait(2), "server never received the request")
            self.assertEqual(len(requests), 1)

    def test_cleanup_wait_is_bounded_separately_from_a_long_request_deadline(self):
        with local_server() as (base_url, _requests):
            started = time.monotonic()
            with urlopen(base_url + "/slow-close", timeout=650) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["text"], "응답")
            self.assertLess(time.monotonic() - started, 0.4)

    def test_external_http_and_all_https_keep_standard_urllib_behavior(self):
        for target in ("http://example.com/path", "https://127.0.0.1/path"):
            with self.subTest(target=target), mock.patch("companion_http.urllib.request.urlopen") as standard:
                request = urllib.request.Request(target)
                result = urlopen(request, data=b"body", timeout=12)
                self.assertIs(result, standard.return_value)
                standard.assert_called_once_with(request, data=b"body", timeout=12)


if __name__ == "__main__":
    unittest.main()
