# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin

import aiohttp
from aiohttp.abc import AbstractStreamWriter
from aiohttp.payload import Payload

from aqt.qt import QDialog, QThread, pyqtSignal

DROPBOX_UPLOAD_PREFIX = "https://content.dropboxapi.com/apitul/1/"
TIMEOUT_SECONDS = 60
# Pauses before resending a chunk after a transient failure.
RETRY_DELAYS: tuple[float, ...] = (2, 10, 30)

logger = logging.getLogger(__name__)


class CampaignStatusError(ValueError):
    def __init__(self, status: int) -> None:
        super().__init__(f"Campaign request failed ({status})")
        self.status = status


def _transient(exc: Exception) -> bool:
    # Other 4xx statuses mean an expired or invalid cursor or link.
    if isinstance(exc, CampaignStatusError):
        return exc.status == 429 or exc.status >= 500
    return isinstance(exc, (aiohttp.ClientError, TimeoutError))


async def _retry(
    attempt: Callable[[], Awaitable[dict]], delays: Sequence[float]
) -> dict:
    for delay in delays:
        try:
            return await attempt()
        except Exception as exc:
            if not _transient(exc):
                raise
            # Network exceptions can include the private upload URL.
            logger.info(
                "Campaign request failed (%s), retrying in %ss",
                type(exc).__name__,
                delay,
            )
        await asyncio.sleep(delay)
    return await attempt()


async def _post(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    body: Payload | None = None,
) -> dict:
    async with session.post(
        url, headers=headers, data=body, allow_redirects=False
    ) as response:
        if response.status != 200:
            raise CampaignStatusError(response.status)
        data = await response.json(content_type=None)
        if not isinstance(data, dict):
            raise ValueError("Invalid campaign response")
        return data


class _ArchiveBody(Payload):
    """Stream one byte range and report progress across the whole archive."""

    def __init__(
        self, file: BinaryIO, length: int, size: int, progress: Callable[[int], None]
    ) -> None:
        super().__init__(file, content_type="application/octet-stream")
        self._size = length
        self.file = file
        self.remaining = length
        self.archive_size = size
        self.progress = progress

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        raise TypeError("Binary upload")

    async def write(self, writer: AbstractStreamWriter) -> None:
        while self.remaining:
            start = self.file.tell()
            block = self.file.read(min(self.remaining, 64 * 1024))
            if not block:
                raise OSError("Incomplete campaign archive")
            await asyncio.wait_for(writer.write(block), TIMEOUT_SECONDS)
            self.remaining -= len(block)
            percent = 100 * self.file.tell() // self.archive_size
            if percent != 100 * start // self.archive_size:
                self.progress(percent)


async def _send_archive(
    path: Path,
    broker: str,
    broker_headers: dict[str, str],
    direct_limit: int,
    progress: Callable[[int], None],
) -> bool:
    size = path.stat().st_size
    timeout = aiohttp.ClientTimeout(
        total=None, connect=TIMEOUT_SECONDS, sock_read=TIMEOUT_SECONDS
    )
    # A stalled address must not delay other addresses for the same hostname.
    connector = aiohttp.TCPConnector(happy_eyeballs_delay=0.25)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=True,
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        with path.open("rb") as file:

            async def send(
                url: str, start: int, length: int, headers: dict[str, str]
            ) -> dict:
                # A retry resends the whole range.
                file.seek(start)
                body = _ArchiveBody(file, length, size, progress)
                reply = await _post(session, url, headers, body)
                if body.remaining:
                    raise ValueError("Incomplete campaign upload")
                return reply

            if size <= direct_limit:

                async def direct() -> dict:
                    # A failed link may be spent, so each attempt requests a new one.
                    data = await _post(session, broker, broker_headers)
                    link = data.get("link")
                    if not isinstance(link, str) or not link.startswith(
                        DROPBOX_UPLOAD_PREFIX
                    ):
                        raise ValueError("Invalid campaign upload link")
                    return await send(link, 0, size, {})

                data = await _retry(direct, RETRY_DELAYS[:1])
            else:
                headers = {**broker_headers, "X-Upload-Size": str(size)}
                data = await _post(session, urljoin(broker, "upload-session"), headers)
                while file.tell() < size:
                    token = data.get("session")
                    length = data.get("chunk_size")
                    if (
                        not isinstance(token, str)
                        or not token
                        or not token.isascii()
                        or type(length) is not int
                        or not 0 < length <= 8 * 1024 * 1024
                        or data.get("offset") != file.tell()
                    ):
                        return False
                    # The broker accepts a resent chunk with the same cursor.
                    start = file.tell()
                    data = await _retry(
                        partial(
                            send,
                            urljoin(broker, "upload-chunk"),
                            start,
                            min(length, size - start),
                            {"Authorization": "Bearer " + token},
                        ),
                        RETRY_DELAYS,
                    )
        receipt = data.get("content-hash")
        return isinstance(receipt, str) and bool(re.fullmatch("[0-9a-f]+", receipt))


class CampaignUpload(QThread):
    progress = pyqtSignal(int)
    done = pyqtSignal(int)

    def __init__(
        self, path: Path, broker: str, broker_headers: dict[str, str], direct_limit: int
    ) -> None:
        super().__init__()
        self.failed = False
        self._loop = asyncio.new_event_loop()
        self._task = self._loop.create_task(
            _send_archive(
                path, broker, broker_headers, direct_limit, self.progress.emit
            )
        )

    def run(self) -> None:
        try:
            success = self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # Network exceptions can include the private upload URL.
            logger.warning("Campaign upload failed (%s)", type(exc).__name__)
            success = False
        self.failed = not success
        self.done.emit(
            QDialog.DialogCode.Accepted if success else QDialog.DialogCode.Rejected
        )

    def stop(self) -> None:
        # Release the archive before the dialog's caller removes its temporary directory.
        self._loop.call_soon_threadsafe(self._task.cancel)
        self.wait()
        self._loop.close()
