# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from types import SimpleNamespace

import aqt.errors
import aqt.main
import aqt.rwkv_scheduler
from anki.collection import OpChanges
from aqt.main import (
    OUTDATED_FSRS7_PREVIEW_WARNING_MAX_PRESETS,
    AnkiQt,
    _clear_outdated_fsrs7_preview_params,
    _outdated_fsrs7_preview_preset_names,
    _outdated_fsrs7_preview_warning_text,
)


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


def test_study_queue_mutation_invalidates_rwkv_before_screen_refresh(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.state = "deckBrowser"
    mw.reviewer = object()
    mw.deckBrowser = SimpleNamespace(
        op_executed=lambda _changes, _handler, _focused: calls.append("screen") or False
    )
    monkeypatch.setattr(aqt.main, "current_window", lambda: mw)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "study_queues_did_change",
        lambda _owner, _initiator: calls.append("rwkv"),
    )
    changes = OpChanges()
    changes.study_queues = True

    mw.on_operation_did_execute(changes, handler=object())

    assert calls == ["rwkv", "screen"]


def test_non_queue_preset_mutation_invalidates_rwkv_before_screen_refresh(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.state = "deckBrowser"
    mw.deckBrowser = SimpleNamespace(
        op_executed=lambda _changes, _handler, _focused: calls.append("screen") or False
    )
    monkeypatch.setattr(aqt.main, "current_window", lambda: mw)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "fsrs_preset_resolution_did_change",
        lambda _owner: calls.append("rwkv preset"),
    )
    changes = OpChanges()
    changes.tag = True

    mw.on_operation_did_execute(changes, handler=object())

    assert calls == ["rwkv preset", "screen"]


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

    def excepthook(etype: object, value: object, tb: object) -> None:
        pass

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


def test_outdated_fsrs7_preview_preset_names_detects_35_value_params() -> None:
    configs = [
        {"name": "Default", "fsrsParams7": [1.0] * 34},
        {"name": "Preview", "fsrsParams7": [1.0] * 35},
        {
            "name": "Fork fields",
            "other": {"jschoreels.fsrs": {"fsrs_params_7": [1.0] * 35}},
        },
        {
            "name": "Flattened fork fields",
            "jschoreels.fsrs": {"fsrs_params_7": [1.0] * 35},
        },
        {"id": 3, "name": "", "fsrsParams7": [1.0] * 35},
        {"name": "Invalid other count", "fsrsParams7": [1.0] * 36},
        {"name": "Missing params"},
    ]

    assert _outdated_fsrs7_preview_preset_names(configs) == [
        "Preview",
        "Fork fields",
        "Flattened fork fields",
        "Preset 3",
    ]


def test_clear_outdated_fsrs7_preview_params_only_removes_35_value_params() -> None:
    config = {
        "fsrsParams7": [1.0] * 35,
        "fsrs_params_7": [2.0] * 34,
        "jschoreels.fsrs": {
            "fsrs_params_7": [3.0] * 35,
            "fsrs_minimum_interval_secs": 2,
        },
        "other": {
            "jschoreels.fsrs": {
                "fsrs_params_7": [4.0] * 35,
                "fsrs_dynamic_desired_retention_enabled": True,
            },
        },
    }

    assert _clear_outdated_fsrs7_preview_params(config)

    assert config["fsrsParams7"] == []
    assert config["fsrs_params_7"] == [2.0] * 34
    assert config["jschoreels.fsrs"] == {"fsrs_minimum_interval_secs": 2}
    assert config["other"]["jschoreels.fsrs"] == {
        "fsrs_dynamic_desired_retention_enabled": True
    }


def test_clear_outdated_fsrs7_preview_params_ignores_valid_params() -> None:
    config = {"fsrsParams7": [1.0] * 34}

    assert not _clear_outdated_fsrs7_preview_params(config)
    assert config == {"fsrsParams7": [1.0] * 34}


def test_outdated_fsrs7_preview_warning_text_limits_preset_list() -> None:
    names = [
        f"Preset {idx}" for idx in range(OUTDATED_FSRS7_PREVIEW_WARNING_MAX_PRESETS + 2)
    ]

    text = _outdated_fsrs7_preview_warning_text(names)

    assert "35 values" in text
    assert "34 values" in text
    assert f"- Preset {OUTDATED_FSRS7_PREVIEW_WARNING_MAX_PRESETS - 1}" in text
    assert f"- Preset {OUTDATED_FSRS7_PREVIEW_WARNING_MAX_PRESETS}" not in text
    assert "...and 2 more" in text
