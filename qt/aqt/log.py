# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import errno
import logging
import os
import sys
from collections.abc import Iterable
from io import StringIO, TextIOWrapper
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from threading import RLock, get_ident
from typing import Any, Optional, TextIO, cast

from aqt.log_redaction import redact_log_text

# All loggers with the following prefix will be treated as add-on loggers
#
# To instatiate a logger with this prefix, use aqt.AddonManager.get_logger()
#
# NOTE: Add-ons might also directly instantiate a logger with this prefix, e.g. in
#       order to avoid depending on the Anki codebase, so this prefix should not
#       be changed.
ADDON_LOGGER_PREFIX = "addon."

# Formatter used for all loggers
_LOG_FORMAT = "%(asctime)s:%(levelname)s:%(name)s: %(message)s"
FORMATTER = logging.Formatter(_LOG_FORMAT)


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


class ExecutionLog(RotatingFileHandler):
    """Retain the desktop application's Python console output across restarts."""

    lock: RLock
    terminator = ""

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
            delay=True,
            encoding="utf-8",
            errors="backslashreplace",
        )
        self.setFormatter(_RedactingFormatter())
        self.write_error: str | None = None
        self._partial_lines: dict[tuple[str, int], StringIO] = {}

    def write_console(self, stream: str, text: str) -> None:
        # print() may write a label, a secret and the newline separately.
        key = (stream, get_ident())
        with self.lock:
            complete, newline, tail = text.rpartition("\n")
            if newline:
                partial = self._partial_lines.pop(key, None)
                if partial:
                    complete = partial.getvalue() + complete
                self._write_record(complete + newline)
            if tail:
                if key not in self._partial_lines:
                    self._partial_lines[key] = StringIO()
                self._partial_lines[key].write(tail)

    def _write_record(self, text: str) -> None:
        self.handle(logging.LogRecord("console", logging.INFO, "", 0, text, (), None))

    def close(self) -> None:
        with self.lock:
            for partial in self._partial_lines.values():
                self._write_record(partial.getvalue() + "\n")
            self._partial_lines.clear()
            super().close()

    def _open(self) -> TextIOWrapper:
        return open(
            self.baseFilename,
            "a",
            encoding=self.encoding,
            errors=self.errors,
            opener=lambda path, flags: os.open(path, flags, 0o600),
        )

    def handleError(self, record: logging.LogRecord) -> None:
        # The default handler prints to stderr, which also feeds this log.
        self.write_error = str(sys.exc_info()[1])

    def paths(self) -> list[Path]:
        return [
            Path(f"{self.baseFilename}.{index}")
            for index in range(self.backupCount, 0, -1)
        ] + [Path(self.baseFilename)]

    def snapshot(self) -> bytes:
        with self.lock:
            self.flush()
            chunks = []
            for path in self.paths():
                if path.exists():
                    chunks.append(path.read_bytes())
            for partial in self._partial_lines.values():
                chunks.append(
                    (partial.getvalue() + "\n").encode(errors="backslashreplace")
                )
            if self.write_error:
                chunks.append(
                    f"\nExecution log write failed: {self.write_error}\n".encode()
                )
        # Older files may predate redaction. Join rotations first because older
        # versions could rotate between individual writes from the same print().
        text = b"".join(chunks).decode("utf-8", errors="surrogateescape")
        return redact_log_text(text).encode("utf-8", errors="surrogateescape")


class _LoggedStream:
    def __init__(self, stream: TextIO, log: ExecutionLog, name: str) -> None:
        self.stream = stream
        self.log = log
        self.name = name

    def write(self, text: str) -> int:
        if text:
            self.log.write_console(self.name, text)
        return self.stream.write(text)

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class ConsoleHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, BrokenPipeError) or (
            sys.platform == "win32"
            and isinstance(error, OSError)
            and error.errno == errno.EINVAL
        ):
            # The launcher can exit before Anki finishes handling Ctrl+C.
            # Windows reports a closed output pipe as EINVAL.
            # Redirect the descriptor so later prints and the final flush also work.
            with open(os.devnull, "w") as devnull:
                os.dup2(devnull.fileno(), self.stream.fileno())
        else:
            super().handleError(record)


class AnkiLoggerManager(logging.Manager):
    # inspired by: https://github.com/abdnh/ankiutils/blob/master/src/ankiutils/log.py

    def __init__(
        self,
        logs_path: Path | str,
        existing_loggers: dict[str, logging.Logger | logging.PlaceHolder],
        rootnode: logging.RootLogger,
    ):
        super().__init__(rootnode)
        self.loggerDict = existing_loggers
        self.logs_path = Path(logs_path)

    def getLogger(self, name: str) -> logging.Logger:
        if not name.startswith(ADDON_LOGGER_PREFIX) or name in self.loggerDict:
            return super().getLogger(name)

        module = name.removeprefix(ADDON_LOGGER_PREFIX).split(".", maxsplit=1)[0]
        addon_logger_name = f"{ADDON_LOGGER_PREFIX}{module}"

        if addon_logger_name not in self.loggerDict:
            # Create a new add-on logger
            logger = super().getLogger(addon_logger_name)

            path = (
                get_addon_logs_folder(self.logs_path, module=module) / f"{module}.log"
            )
            path.parent.mkdir(parents=True, exist_ok=True)

            # Keep the last 10 days of logs
            handler = TimedRotatingFileHandler(
                filename=path, when="D", interval=1, backupCount=10, encoding="utf-8"
            )
            handler.setFormatter(_RedactingFormatter(_LOG_FORMAT))

            logger.addHandler(handler)

        return super().getLogger(name)


def get_addon_logs_folder(logs_path: Path | str, module: str) -> Path:
    return Path(logs_path) / "addons" / module


def find_addon_logger(module: str) -> logging.Logger | None:
    return cast(
        Optional[logging.Logger],
        logging.Logger.manager.loggerDict.get(f"{ADDON_LOGGER_PREFIX}{module}"),
    )


def setup_logging(path: Path | str, **kwargs) -> ExecutionLog:
    """
    Set up logging for the application.

    Mirror Python console output to a rotating execution log under path. Add-on
    loggers also keep their separate files. If the execution log cannot be
    opened, continue with console and add-on logging.

    Args:
        path (Path): The path where the log files should be stored.
        **kwargs: Arbitrary keyword arguments for logging.basicConfig
    """

    # Patch root logger manager to handle add-on loggers
    logger_manager = AnkiLoggerManager(
        path, existing_loggers=logging.Logger.manager.loggerDict, rootnode=logging.root
    )
    logging.Logger.manager = logger_manager

    # Capture the same formatted records as the console, plus print() and
    # tracebacks. ErrorHandler forwards stderr to stdout after the UI starts.
    if isinstance(sys.stdout, _LoggedStream):
        sys.stdout.log.close()
        sys.stdout = sys.stdout.stream
    if isinstance(sys.stderr, _LoggedStream):
        sys.stderr = sys.stderr.stream
    execution_log = ExecutionLog(Path(path) / "execution.log")
    sys.stdout = cast(TextIO, _LoggedStream(sys.stdout, execution_log, "stdout"))
    sys.stderr = cast(TextIO, _LoggedStream(sys.stderr, execution_log, "stderr"))
    stdout_handler = ConsoleHandler(stream=sys.stdout)
    stdout_handler.setFormatter(FORMATTER)
    logging.basicConfig(handlers=[stdout_handler], force=True, **kwargs)
    logging.captureWarnings(True)

    # Silence some loggers of external libraries:
    silenced_loggers = [
        "waitress.queue",
    ]
    for logger in silenced_loggers:
        logging.getLogger(logger).setLevel(logging.CRITICAL)
        logging.getLogger(logger).propagate = False

    return execution_log
