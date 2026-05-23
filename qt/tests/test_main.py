# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Callable

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
