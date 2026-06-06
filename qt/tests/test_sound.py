# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import subprocess
from collections.abc import Callable

import aqt.sound
from aqt.sound import SimpleProcessPlayer


class _ProcessPlayer(SimpleProcessPlayer):
    def rank_for_tag(self, tag) -> int:
        return 0


class FakeTaskman:
    def run_on_main(self, fn: Callable[[], None]) -> None:
        fn()


class FakeStdin:
    closed = False

    def close(self) -> None:
        self.closed = True


class SlowToStopProcess:
    args = ["player"]
    returncode = 0

    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.terminated = False
        self.killed = False
        self._first_wait = True

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if timeout == 1 and self._first_wait:
            self._first_wait = False
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode


def test_stopping_slow_player_kills_process(monkeypatch) -> None:
    monkeypatch.setattr(
        aqt.sound.gui_hooks, "av_player_did_begin_playing", lambda *_args: None
    )
    player = _ProcessPlayer(FakeTaskman())
    process = SlowToStopProcess()
    player._process = process
    player._terminate_flag = True

    player._wait_for_termination(object())

    assert process.terminated
    assert process.killed
    assert process.stdin.closed
    assert player._process is None
