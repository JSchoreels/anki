# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

import aqt.errors
import aqt.main
from aqt.main import AnkiQt


class CloseEvent:
    def __init__(self) -> None:
        self.ignored = False
        self.accepted = False

    def ignore(self) -> None:
        self.ignored = True

    def accept(self) -> None:
        self.accepted = True


class Progress:
    def __init__(self) -> None:
        self.scheduled: list[Callable[[], None]] = []

    def single_shot(
        self,
        ms: int,
        func: Callable[[], None],
        requires_collection: bool = True,
    ) -> None:
        self.scheduled.append(func)


def setup_mw() -> tuple[AnkiQt, list[str], Progress]:
    mw = AnkiQt.__new__(AnkiQt)
    progress = Progress()
    calls: list[str] = []

    mw.state = "deckBrowser"
    mw.progress = progress
    mw._background_op_count = 0
    mw._unload_profile_and_exit_pending = False
    mw.unloadProfileAndExit = lambda: calls.append("unload")  # type: ignore[method-assign]

    return mw, calls, progress


def test_close_event_unloads_profile_when_no_background_op() -> None:
    mw, calls, progress = setup_mw()
    event = CloseEvent()

    mw.closeEvent(event)  # type: ignore[arg-type]

    assert event.ignored
    assert calls == ["unload"]
    assert not progress.scheduled


def test_close_event_waits_for_background_op_before_unloading_profile() -> None:
    mw, calls, progress = setup_mw()
    mw._background_op_count = 1
    event = CloseEvent()

    mw.closeEvent(event)  # type: ignore[arg-type]
    mw.closeEvent(event)  # type: ignore[arg-type]

    assert event.ignored
    assert calls == []
    assert len(progress.scheduled) == 1

    mw._background_op_count = 0
    progress.scheduled.pop()()

    assert calls == ["unload"]


def test_cleanup_and_exit_closes_profile_manager(monkeypatch) -> None:
    mw = AnkiQt.__new__(AnkiQt)
    progress = Progress()
    calls: list[str] = []

    mw.errorHandler = type(
        "ErrorHandler", (), {"unload": lambda self: calls.append("unload_errors")}
    )()
    mw.mediaServer = type(
        "MediaServer", (), {"shutdown": lambda self: calls.append("shutdown_media")}
    )()
    mw.backend = type(
        "Backend",
        (),
        {
            "await_backup_completion": lambda self: calls.append(
                "await_backup_completion"
            )
        },
    )()
    mw.pm = type(
        "ProfileManager", (), {"close": lambda self: calls.append("close_pm")}
    )()
    mw.app = type(
        "App",
        (),
        {
            "_unset_windows_shutdown_block_reason": lambda self: calls.append(
                "unset_shutdown_block"
            ),
            "exit": lambda self, code: calls.append(f"exit:{code}"),
        },
    )()
    mw.progress = progress
    mw.deleteLater = lambda: calls.append("delete_later")  # type: ignore[method-assign]
    monkeypatch.setattr(aqt.main.gc, "collect", lambda: calls.append("gc"))

    mw.cleanupAndExit()

    assert calls == [
        "unload_errors",
        "shutdown_media",
        "await_backup_completion",
        "close_pm",
        "delete_later",
        "unset_shutdown_block",
    ]
    assert len(progress.scheduled) == 1

    progress.scheduled.pop()()

    assert calls[-2:] == ["gc", "exit:0"]


def test_error_handler_unload_keeps_excepthook_and_detaches_logging_stream(
    monkeypatch,
) -> None:
    old_stderr = sys.stderr
    previous_excepthook = sys.excepthook
    excepthook = lambda etype, value, tb: None
    logger = logging.getLogger("test_error_handler_unload")
    logger.handlers.clear()

    handler = type("ErrorHandler", (), {})()
    handler._oldstderr = old_stderr
    stream_handler = logging.StreamHandler(stream=handler)
    logger.addHandler(stream_handler)

    monkeypatch.setattr(sys, "stderr", handler)
    monkeypatch.setattr(sys, "excepthook", excepthook)

    try:
        aqt.errors.ErrorHandler.unload(handler)

        assert sys.stderr is old_stderr
        assert sys.excepthook is excepthook
        assert stream_handler.stream is old_stderr
    finally:
        logger.handlers.clear()
        sys.excepthook = previous_excepthook
