"""HTTP requests to local companions without racing server-side socket teardown.

urllib forces ``Connection: close`` even for HTTP/1.1 requests. On Windows the
server can consequently close before the client has read its response, losing
successful POST responses to WinError 10054. Keep the connection open until its
length-delimited response has been consumed, then close it from the client side.
External URLs retain urllib's normal proxy, TLS and redirect behavior.
"""

from __future__ import annotations

import copy
import http.client
import io
import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from urllib.response import addinfourl


_CLOSE_TIMEOUT_SECONDS = 0.1


def _is_loopback_http(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "http" or parsed.username is not None or parsed.password is not None:
        return False
    host = parsed.hostname or ""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _close_connection(connection: http.client.HTTPConnection, *, complete: bool) -> None:
    try:
        if complete and connection.sock is not None:
            # A half-close lets persistent servers finish receiving before their
            # socket is destroyed. Drain their FIN briefly to avoid the Windows
            # proactor's shutdown/close race. Cleanup must not mask a response or
            # hold up a caller whose request had a much longer timeout.
            connection.sock.settimeout(_CLOSE_TIMEOUT_SECONDS)
            connection.sock.shutdown(socket.SHUT_WR)
            connection.sock.recv(1)
    except OSError:
        pass
    finally:
        connection.close()


def urlopen(
    url,
    data=None,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    *,
    context=None,
):
    """Open a URL, using an independent HTTP/1.1 connection for loopback HTTP.

    Local responses are buffered before returning a urllib-style response. No
    request is retried or redirected: a POST executes at most once, including
    when its response is lost. HTTP errors retain their readable response body.
    """
    target = url.full_url if isinstance(url, urllib.request.Request) else url
    if not _is_loopback_http(target):
        kwargs = {} if context is None else {"context": context}
        return urllib.request.urlopen(url, data=data, timeout=timeout, **kwargs)

    if isinstance(url, urllib.request.Request):
        request = copy.copy(url)
        if data is not None:
            request.data = data
    else:
        request = urllib.request.Request(url, data=data)
    parsed = urlsplit(request.full_url)
    headers = {
        key: value
        for key, value in request.header_items()
        if key.lower() not in {"connection", "proxy-connection"}
    }
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    complete = False
    try:
        try:
            connection.request(
                request.get_method(), request.selector, body=request.data, headers=headers,
            )
        except OSError as exc:
            raise urllib.error.URLError(exc) from exc
        response = connection.getresponse()
        try:
            body = response.read()
            status, reason, response_headers = response.status, response.reason, response.headers
            complete = True
        finally:
            response.close()
    finally:
        _close_connection(connection, complete=complete)

    if status >= 300:
        # Do not replay state-changing commands through a redirect. The caller
        # receives the original status and body, just as for other HTTP errors.
        raise urllib.error.HTTPError(request.full_url, status, reason, response_headers, io.BytesIO(body))
    result = addinfourl(io.BytesIO(body), response_headers, request.full_url, status)
    result.reason = result.msg = reason
    result.getheader = response_headers.get
    result.getheaders = lambda: list(response_headers.items())
    return result
