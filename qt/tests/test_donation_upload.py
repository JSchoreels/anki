# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import asyncio
import json
import socket
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Thread

import pytest

from aqt import donation_upload


@pytest.mark.parametrize("chunked", [False, True])
def test_stalled_first_address_uses_second_without_repeating_request(
    tmp_path, monkeypatch, chunked
):
    archive = tmp_path / "test.colpkg"
    archive.write_bytes(b"collection")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (self.path, self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(200)
            self.end_headers()
            if self.path == "/link":
                self.wfile.write(b'{"link":"' + link.encode() + b'"}')
            elif self.path == "/upload-session":
                self.wfile.write(b'{"session":"token","offset":0,"chunk_size":32}')
            else:
                self.wfile.write(b'{"content-hash":"ab"}')

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        origin = f"http://127.0.0.1:{server.server_port}"
        link = origin + "/archive"
        monkeypatch.setattr(donation_upload, "DROPBOX_UPLOAD_PREFIX", link)
        monkeypatch.setenv("no_proxy", "*")
        Thread(target=server.serve_forever, daemon=True).start()

        async def upload():
            loop = asyncio.get_running_loop()
            getaddrinfo = loop.getaddrinfo
            connect = loop.sock_connect
            stalled = asyncio.Event()

            async def resolve(host, port, **kwargs):
                if host == "campaign.test":
                    return [
                        (
                            socket.AF_INET,
                            socket.SOCK_STREAM,
                            socket.IPPROTO_TCP,
                            "",
                            (ip, port),
                        )
                        for ip in ("127.0.0.2", "127.0.0.1")
                    ]
                return await getaddrinfo(host, port, **kwargs)

            async def connect_to(sock, address):
                if address[0] == "127.0.0.2":
                    stalled.set()
                    await asyncio.Future()
                return await connect(sock, address)

            monkeypatch.setattr(loop, "getaddrinfo", resolve)
            monkeypatch.setattr(loop, "sock_connect", connect_to)
            broker = f"http://campaign.test:{server.server_port}/link"
            assert await asyncio.wait_for(
                donation_upload._send_archive(
                    archive, broker, {}, 4 if chunked else 100, lambda _: None
                ),
                5,
            )
            assert stalled.is_set()

        try:
            asyncio.run(upload())
        finally:
            server.shutdown()
    assert requests == [
        ("/upload-session" if chunked else "/link", b""),
        ("/upload-chunk" if chunked else "/archive", b"collection"),
    ]


@pytest.mark.parametrize("waiting_for", ["link", "direct upload", "chunk"])
def test_cancel_closes_pending_connection(tmp_path, monkeypatch, waiting_for):
    archive = tmp_path / "test.colpkg"
    archive.write_bytes(b"collection")
    waiting = Event()
    disconnected = Event()
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/upload-session" or (
                self.path == "/link" and waiting_for == "direct upload"
            ):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    b'{"session":"token","offset":0,"chunk_size":4}'
                    if self.path == "/upload-session"
                    else b'{"link":"' + link.encode() + b'"}'
                )
                return
            self.connection.settimeout(5)
            waiting.set()
            if self.rfile.read(1) == b"":
                disconnected.set()

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        origin = f"http://127.0.0.1:{server.server_port}"
        link = origin + "/archive"
        monkeypatch.setattr(donation_upload, "DROPBOX_UPLOAD_PREFIX", link)
        Thread(target=server.serve_forever, daemon=True).start()
        worker = donation_upload.CampaignUpload(
            archive, origin + "/link", {}, 4 if waiting_for == "chunk" else 100
        )
        worker.start()
        try:
            assert waiting.wait(5)
        finally:
            worker.stop()
            server.shutdown()
        assert disconnected.is_set()
        assert not worker.failed
        assert (
            paths
            == {
                "link": ["/link"],
                "direct upload": ["/link", "/archive"],
                "chunk": ["/upload-session", "/upload-chunk"],
            }[waiting_for]
        )


def serve(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.mark.parametrize("failure", ["502", "drop"])
@pytest.mark.parametrize("chunked", [True, False])
def test_transient_failure_is_retried(tmp_path, monkeypatch, chunked, failure):
    monkeypatch.setattr(donation_upload, "RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setenv("no_proxy", "*")
    archive = tmp_path / "test.colpkg"
    archive.write_bytes(b"collection")
    requests = []
    received = bytearray()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, self.headers.get("Authorization"), body))
            if self.path == "/link":
                reply = {"link": origin + "/archive"}
            elif self.path == "/upload-session":
                reply = {"session": "cursor-0", "offset": 0, "chunk_size": 4}
            elif len(requests) == 2:
                # The first transfer of archive data fails.
                if failure == "502":
                    self.send_response(502)
                    self.end_headers()
                # Otherwise the connection closes without a response.
                return
            else:
                received.extend(body)
                if chunked and len(received) < len(b"collection"):
                    reply = {
                        "session": f"cursor-{len(received)}",
                        "offset": len(received),
                        "chunk_size": 4,
                    }
                else:
                    reply = {"content-hash": "ab"}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

    server, origin = serve(Handler)
    monkeypatch.setattr(donation_upload, "DROPBOX_UPLOAD_PREFIX", origin + "/archive")
    try:
        assert asyncio.run(
            donation_upload._send_archive(
                archive, origin + "/link", {}, 4 if chunked else 100, lambda _: None
            )
        )
    finally:
        server.shutdown()
        server.server_close()
    assert bytes(received) == b"collection"
    if chunked:
        # The failed chunk is resent with the same cursor.
        assert requests == [
            ("/upload-session", None, b""),
            ("/upload-chunk", "Bearer cursor-0", b"coll"),
            ("/upload-chunk", "Bearer cursor-0", b"coll"),
            ("/upload-chunk", "Bearer cursor-4", b"ecti"),
            ("/upload-chunk", "Bearer cursor-8", b"on"),
        ]
    else:
        # The direct upload retries once with a fresh link.
        assert requests == [
            ("/link", None, b""),
            ("/archive", None, b"collection"),
            ("/link", None, b""),
            ("/archive", None, b"collection"),
        ]


def test_rejected_cursor_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(donation_upload, "RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setenv("no_proxy", "*")
    archive = tmp_path / "test.colpkg"
    archive.write_bytes(b"collection")
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            paths.append(self.path)
            if self.path == "/upload-session":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"session":"token","offset":0,"chunk_size":4}')
            else:
                self.send_response(403)
                self.end_headers()

    server, origin = serve(Handler)
    try:
        with pytest.raises(donation_upload.CampaignStatusError) as error:
            asyncio.run(
                donation_upload._send_archive(
                    archive, origin + "/link", {}, 4, lambda _: None
                )
            )
    finally:
        server.shutdown()
        server.server_close()
    assert error.value.status == 403
    assert paths == ["/upload-session", "/upload-chunk"]


def test_cancel_during_retry_backoff_returns_promptly(tmp_path, monkeypatch):
    monkeypatch.setattr(donation_upload, "RETRY_DELAYS", (30, 30, 30))
    archive = tmp_path / "test.colpkg"
    archive.write_bytes(b"collection")
    answered = Event()
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            paths.append(self.path)
            if self.path == "/upload-session":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"session":"token","offset":0,"chunk_size":4}')
                return
            self.send_response(502)
            self.end_headers()
            answered.set()

    server, origin = serve(Handler)
    worker = donation_upload.CampaignUpload(archive, origin + "/link", {}, 4)
    worker.start()
    try:
        assert answered.wait(5)
        time.sleep(0.2)
        started = time.monotonic()
        worker.stop()
        assert time.monotonic() - started < 2
    finally:
        server.shutdown()
        server.server_close()
    assert not worker.failed
    assert paths == ["/upload-session", "/upload-chunk"]
