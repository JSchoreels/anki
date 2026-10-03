# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Measure browser cache requests and simulate Windows mpv pipe replies."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch


def distribution(values: list[float]) -> dict[str, Any]:
    quartiles = statistics.quantiles(values, n=4, method="inclusive")
    return {
        "samples_ms": values,
        "median_ms": statistics.median(values),
        "q1_ms": quartiles[0],
        "q3_ms": quartiles[2],
    }


def audio_benchmark(samples: int) -> dict[str, Any]:
    from aqt import mpv

    class PipeError(Exception):
        pass

    class FakePlayer(mpv.MPVBase):
        def __init__(self):
            self.debug = False
            self._sock = cast(Any, object())
            self._prepare_thread()

        def __del__(self):
            pass

    player = FakePlayer()
    lock = threading.Lock()
    idle = threading.Event()
    errors: list[str] = []
    readiness = 0.0
    reply: bytes | None = None
    writes = 0
    reads = 0
    no_data = 0

    def write_file(sock, data):
        nonlocal readiness, reply, writes
        request = json.loads(data)
        with lock:
            writes += 1
            readiness = time.perf_counter() + 0.01
            reply = (
                json.dumps({"error": "success", "data": request["command"][1]}).encode()
                + b"\n"
            )
        return 0, len(data)

    def read_file(sock, size):
        nonlocal reply, reads, no_data
        with lock:
            reads += 1
            if reply is not None and time.perf_counter() >= readiness:
                data, reply = reply, None
                return 0, data
            no_data += 1
            if player._request_queue.empty():
                idle.set()
        raise PipeError(232)

    def reader():
        try:
            player._reader()
        except Exception as exc:
            errors.append(repr(exc))

    latency = []
    offsets = []
    with (
        patch.object(mpv, "is_win", True),
        patch.multiple(
            mpv,
            create=True,
            pywintypes=SimpleNamespace(error=PipeError),
            winerror=SimpleNamespace(ERROR_NO_DATA=232, ERROR_BROKEN_PIPE=109),
            win32file=SimpleNamespace(ReadFile=read_file, WriteFile=write_file),
        ),
    ):
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for sample in range(samples):
                idle.clear()
                if not idle.wait(2):
                    raise RuntimeError(f"Simulated pipe did not become idle: {errors}")
                # Spread requests across the original 100ms idle poll window.
                offset = 0.1 * (sample + 0.5) / samples
                threading.Event().wait(offset)
                offsets.append(offset * 1000)
                started = time.perf_counter()
                player._send_message({"command": ["benchmark", sample]}, timeout=2)
                response = player._get_response(timeout=2)
                latency.append((time.perf_counter() - started) * 1000)
                if response != sample:
                    raise RuntimeError("mpv response was routed to the wrong request")
        finally:
            player._stop_event.set()
            if hasattr(player, "_request_sent"):
                player._request_sent.set()
            thread.join(2)
        if thread.is_alive() or errors:
            raise RuntimeError(f"Simulated pipe reader failed to stop: {errors}")
    return {
        "kind": "Windows pipe polling simulated on the current host; no native Windows/mpv timing",
        "reply_ready_after_ms": 10,
        "idle_request_offsets_ms": offsets,
        "command_latency": distribution(latency),
        "commands": writes,
        "pipe_reads": reads,
        "empty_pipe_reads": no_data,
        "response_routing": "all command replies matched their requesting caller",
    }


ASSETS = [
    "js/vendor/jquery.min.js",
    "js/vendor/jquery-ui.min.js",
    "js/vendor/plot.js",
    "css/deckbrowser.css",
    "css/webview.css",
    "imgs/anki-logo-thin.png",
]


def asset_headers() -> dict[str, Any]:
    from aqt import mediasrv

    headers: dict[str, Any] = {}
    with patch.object(mediasrv, "dev_mode", False):
        for path in ASSETS:
            with mediasrv.app.test_request_context():
                response = mediasrv._handle_builtin_file_request(
                    mediasrv.BundledFileRequest(path)
                )
                if response.status_code != 200:
                    raise RuntimeError(f"Missing built asset: {path}")
                headers[path] = {
                    "cache_control": response.headers.get("Cache-Control"),
                    "bytes": len(response.get_data()),
                }
    return headers


def asset_benchmark(samples: int) -> dict[str, Any]:
    from flask import Flask, Response
    from werkzeug.serving import make_server

    from aqt import mediasrv
    from aqt.qt import (
        QApplication,
        QEventLoop,
        QTimer,
        QUrl,
        QWebEnginePage,
        QWebEngineProfile,
    )

    assets = ASSETS
    headers = asset_headers()
    with patch.object(mediasrv, "dev_mode", False):
        result: dict[str, Any] = {"verified_response_headers": headers}
        try:
            app = QApplication.instance() or QApplication(["clanki-asset-benchmark"])
            profile = QWebEngineProfile()
            if not profile.isOffTheRecord():
                raise RuntimeError("Benchmark requires an off-the-record profile")
            page = QWebEnginePage(profile)
            server_app = Flask("clanki-asset-benchmark")
            requests = dict.fromkeys(assets, 0)
            transferred = 0
            request_lock = threading.Lock()
            markup = (
                "<!doctype html><html><head>"
                + "".join(
                    f'<script src="/assets/{path}"></script>'
                    for path in assets
                    if path.startswith("js/")
                )
                + "".join(
                    f'<link rel="stylesheet" href="/assets/{path}">'
                    for path in assets
                    if path.startswith("css/")
                )
                + '</head><body><img src="/assets/imgs/anki-logo-thin.png"></body></html>'
            )

            @server_app.route("/")
            def index():
                return Response(
                    markup, mimetype="text/html", headers={"Cache-Control": "no-store"}
                )

            @server_app.route("/assets/<path:path>")
            def asset(path):
                nonlocal transferred
                response = mediasrv._handle_builtin_file_request(
                    mediasrv.BundledFileRequest(path)
                )
                with request_lock:
                    if path in requests:
                        requests[path] += 1
                        transferred += len(response.get_data())
                return response

            server = make_server("127.0.0.1", 0, server_app, threaded=True)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            durations = []
            request_counts = []
            byte_counts = []
            cold_load_ms = None
            try:
                # One cold page load primes the same session profile used below.
                for sample in range(samples + 1):
                    loop = QEventLoop()
                    timer = QTimer()
                    timer.setSingleShot(True)
                    loaded: list[bool] = []

                    def finished(ok):
                        loaded.append(ok)
                        loop.quit()

                    page.loadFinished.connect(finished)
                    timer.timeout.connect(loop.quit)
                    with request_lock:
                        before_requests = requests.copy()
                        before_bytes = transferred
                    started = time.perf_counter()
                    timer.start(15_000)
                    page.load(
                        QUrl(f"http://127.0.0.1:{server.server_port}/?sample={sample}")
                    )
                    loop.exec()
                    elapsed = (time.perf_counter() - started) * 1000
                    timer.stop()
                    page.loadFinished.disconnect(finished)
                    if loaded != [True]:
                        raise RuntimeError("Offscreen asset page timed out or failed")
                    if not sample:
                        cold_load_ms = elapsed
                        if any(count != 1 for count in requests.values()):
                            raise RuntimeError(
                                "Cold page did not fetch each asset once"
                            )
                    else:
                        durations.append(elapsed)
                        with request_lock:
                            request_counts.append(
                                {
                                    path: requests[path] - before_requests[path]
                                    for path in assets
                                }
                            )
                            byte_counts.append(transferred - before_bytes)
                result.update(
                    {
                        "kind": "actual offscreen QWebEngine page loads with a shared session profile",
                        "profile_off_the_record": True,
                        "cold_load_ms": cold_load_ms,
                        "warm_page_load": distribution(durations),
                        "warm_server_requests_per_sample": request_counts,
                        "warm_transferred_bytes_per_sample": byte_counts,
                        "total_server_requests": requests,
                    }
                )
            finally:
                server.shutdown()
                server_thread.join(2)
                server.server_close()
                page.deleteLater()
                app.processEvents()
                profile.deleteLater()
                app.processEvents()
        except Exception as exc:
            result.update(
                {
                    "kind": "response header verification only; browser benchmark unavailable",
                    "browser_error": repr(exc),
                }
            )
        return result


def isolated_asset_benchmark(root: Path, samples: int) -> dict[str, Any]:
    # Native Chromium startup can abort instead of raising a Python exception.
    # Keep that failure outside the process recording the other benchmark.
    with tempfile.TemporaryDirectory(
        prefix="clanki-browser-", dir="/private/tmp"
    ) as temporary:
        output = Path(temporary) / "browser.json"
        try:
            process = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--source-root",
                    str(root),
                    "--output",
                    str(output),
                    "--samples",
                    str(samples),
                    "--asset-worker",
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if process.returncode == 0 and output.exists():
                return json.loads(output.read_text())
            error = f"Browser subprocess exit {process.returncode}: {process.stderr[-2000:]}"
        except subprocess.TimeoutExpired:
            error = "Browser subprocess exceeded its 90-second safety timeout"
    return {
        "kind": "response header verification only; browser benchmark unavailable",
        "verified_response_headers": asset_headers(),
        "browser_error": error,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--asset-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must be at least 2")
    root = args.source_root.resolve()
    output = args.output.resolve()
    if output.suffix != ".json" or "Anki2" in output.parts:
        parser.error("output must be a JSON file outside any Anki profile")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
    sys.path[:0] = [
        str(root / "pylib"),
        str(root / "out" / "pylib"),
        str(root / "qt"),
        str(root / "out" / "qt"),
    ]
    import aqt
    from anki import _rsbridge

    if not Path(aqt.__file__).resolve().is_relative_to(root):
        raise RuntimeError("aqt was not imported from the requested source root")
    if not Path(_rsbridge.__file__).resolve().is_relative_to(root / "out" / "rust"):
        raise RuntimeError(
            "Native backend was not loaded from the requested source build"
        )
    if args.asset_worker:
        output.write_text(json.dumps(asset_benchmark(args.samples), indent=2) + "\n")
        return
    report = {
        "source_root": str(root),
        "samples": args.samples,
        "windows_mpv_simulation": audio_benchmark(args.samples),
        "builtin_web_assets": isolated_asset_benchmark(root, args.samples),
        "caveats": [
            "Windows pipe and 10ms reply readiness are simulated; native Windows validation remains necessary.",
            "Browser timings measure an isolated representative asset page, not a complete Anki screen.",
            "No cache speed claim is supported if only response header verification succeeds.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
