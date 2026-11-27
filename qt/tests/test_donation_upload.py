# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import asyncio
import socket
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
