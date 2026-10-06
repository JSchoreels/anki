# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from types import SimpleNamespace

import pytest

import aqt.errors
import aqt.main
import aqt.rwkv_scheduler
import aqt.sync
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
        lambda _owner, _initiator, op_changes: calls.append(
            "rwkv" if op_changes is changes else "wrong changes"
        ),
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
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "request_rwkv_state_cache_recovery",
        lambda _owner, **_kwargs: calls.append("rwkv recovery"),
    )
    changes = OpChanges()
    changes.deck_config = True

    mw.on_operation_did_execute(changes, handler=object())

    assert calls == ["rwkv preset", "rwkv recovery", "screen"]


def test_note_content_mutation_preserves_unchanged_rwkv_state_before_refresh(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.state = "deckBrowser"
    mw.deckBrowser = SimpleNamespace(
        op_executed=lambda _changes, _handler, _focused: calls.append("screen") or False
    )
    initiator = object()
    monkeypatch.setattr(aqt.main, "current_window", lambda: mw)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "collection_content_did_change",
        lambda _owner, handler: calls.append(
            "rwkv content" if handler is initiator else "wrong initiator"
        ),
    )
    changes = OpChanges()
    changes.note = True
    changes.note_text = True

    mw.on_operation_did_execute(changes, handler=initiator)

    assert calls == ["rwkv content", "screen"]


@pytest.mark.parametrize("changed", ["deck", "deck_config"])
def test_deck_or_preset_change_requests_rwkv_recovery_after_invalidation(
    monkeypatch, changed: str
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.state = "review"
    mw.reviewer = SimpleNamespace(
        op_executed=lambda *_args: calls.append("screen") or False
    )
    monkeypatch.setattr(aqt.main, "current_window", lambda: mw)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "study_queues_did_change",
        lambda *_args: calls.append("invalidate"),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "request_rwkv_state_cache_recovery",
        lambda _mw, *, reason, allow_during_review: calls.append(
            "recover during review" if allow_during_review else "recover"
        ),
    )
    changes = OpChanges(study_queues=True)
    setattr(changes, changed, True)

    mw.on_operation_did_execute(changes, handler=object())

    assert calls == ["invalidate", "recover during review", "screen"]


def test_legacy_reset_invalidates_rwkv_without_requesting_recovery(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.state = "deckBrowser"
    mw.deckBrowser = SimpleNamespace(
        op_executed=lambda *_args: calls.append("screen") or False
    )
    mw.toolbar = SimpleNamespace(update_sync_status=lambda: None)
    mw.col = SimpleNamespace(models=SimpleNamespace(_clear_cache=lambda: None))
    monkeypatch.setattr(aqt.main, "current_window", lambda: mw)
    monkeypatch.setattr(
        aqt.main.gui_hooks, "operation_did_execute", mw.on_operation_did_execute
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "study_queues_did_change",
        lambda *_args: calls.append("invalidate"),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "request_rwkv_state_cache_recovery",
        lambda *_args, **_kwargs: calls.append("recover"),
    )

    def revalidate(window: AnkiQt) -> None:
        assert window is mw
        assert not window._legacy_reset_in_progress
        calls.append("revalidate")

    monkeypatch.setattr(
        aqt.rwkv_scheduler, "revalidate_rwkv_state_after_legacy_reset", revalidate
    )

    mw._synthesize_op_did_execute_from_reset()

    assert calls == ["invalidate", "screen", "revalidate"]
    assert not mw._legacy_reset_in_progress


def test_startup_sync_can_defer_rwkv_refresh(
    monkeypatch,
) -> None:
    calls: list[object] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.can_auto_sync = lambda: True  # type: ignore[method-assign]

    def sync(
        after_sync: Callable[[], None],
        *,
        refresh_rwkv_state: bool = True,
    ) -> None:
        calls.append(("sync", refresh_rwkv_state))
        after_sync()

    mw._sync_collection_and_media = sync  # type: ignore[method-assign]

    mw.maybe_auto_sync_on_open_close(
        lambda synced: calls.append(("done", synced)),
        refresh_rwkv_state=False,
    )

    assert calls == [
        ("sync", False),
        ("done", True),
    ]


def test_sync_can_finish_without_separate_rwkv_refresh(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.col = SimpleNamespace(
        models=SimpleNamespace(_clear_cache=lambda: calls.append("clear models"))
    )
    mw.reset = lambda: calls.append("reset")  # type: ignore[method-assign]

    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_will_start",
        lambda: calls.append("sync start"),
    )
    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_did_finish",
        lambda: calls.append("sync finish"),
    )

    def sync_collection(
        _mw: object,
        on_done: Callable[[], None],
        *,
        on_remote_collection_changes: Callable[
            [aqt.sync.RemoteCollectionChanges], None
        ],
    ) -> None:
        on_remote_collection_changes(
            aqt.sync.RemoteCollectionChanges(collection_changed=True)
        )
        on_done()

    monkeypatch.setattr(aqt.main, "sync_collection", sync_collection)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "refresh_rwkv_state_after_sync",
        lambda _mw, _on_done: calls.append("rwkv refresh"),
    )

    mw._sync_collection_and_media(
        lambda: calls.append("done"),
        refresh_rwkv_state=False,
    )

    assert calls == [
        "sync start",
        "clear models",
        "sync finish",
        "reset",
        "done",
    ]


def test_sync_skips_rwkv_refresh_without_remote_collection_changes(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.col = SimpleNamespace(
        models=SimpleNamespace(_clear_cache=lambda: calls.append("clear models"))
    )
    mw.reset = lambda: calls.append("reset")  # type: ignore[method-assign]

    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_will_start",
        lambda: calls.append("sync start"),
    )
    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_did_finish",
        lambda: calls.append("sync finish"),
    )

    def sync_collection(
        _mw: object,
        on_done: Callable[[], None],
        *,
        on_remote_collection_changes: Callable[
            [aqt.sync.RemoteCollectionChanges], None
        ],
    ) -> None:
        on_remote_collection_changes(aqt.sync.RemoteCollectionChanges())
        on_done()

    monkeypatch.setattr(aqt.main, "sync_collection", sync_collection)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "refresh_rwkv_state_after_sync",
        lambda _mw, _on_done: calls.append("rwkv refresh"),
    )

    mw._sync_collection_and_media(lambda: calls.append("done"))

    assert calls == [
        "sync start",
        "clear models",
        "sync finish",
        "reset",
        "done",
    ]


def test_sync_resets_ui_before_refreshing_rwkv_for_remote_collection_changes(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.col = SimpleNamespace(
        models=SimpleNamespace(_clear_cache=lambda: calls.append("clear models"))
    )
    mw.reset = lambda: calls.append("reset")  # type: ignore[method-assign]

    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_will_start",
        lambda: calls.append("sync start"),
    )
    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "sync_did_finish",
        lambda: calls.append("sync finish"),
    )

    def sync_collection(
        _mw: object,
        on_done: Callable[[], None],
        *,
        on_remote_collection_changes: Callable[
            [aqt.sync.RemoteCollectionChanges], None
        ],
    ) -> None:
        on_remote_collection_changes(
            aqt.sync.RemoteCollectionChanges(
                collection_changed=True,
                review_ids=(123,),
            )
        )
        on_done()

    monkeypatch.setattr(aqt.main, "sync_collection", sync_collection)

    def refresh(
        _mw: object,
        on_done: Callable[[], None],
        *,
        remote_review_ids: tuple[int, ...],
    ) -> None:
        calls.append(f"rwkv refresh {remote_review_ids}")
        on_done()

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "refresh_rwkv_state_after_sync",
        refresh,
    )

    mw._sync_collection_and_media(lambda: calls.append("done"))

    assert calls == [
        "sync start",
        "clear models",
        "sync finish",
        "reset",
        "rwkv refresh (123,)",
        "done",
    ]


def test_profile_load_marks_rwkv_startup_before_loading_collection(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.loadCollection = lambda: calls.append("load collection") or False  # type: ignore[method-assign]
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "begin_rwkv_state_cache_startup",
        lambda _mw: calls.append("begin rwkv"),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "finish_rwkv_state_cache_startup",
        lambda _mw: calls.append("finish rwkv"),
    )

    mw.loadProfile()

    assert calls == [
        "begin rwkv",
        "load collection",
        "finish rwkv",
    ]


def test_profile_unload_cancels_rwkv_counts_before_closing_collection(
    monkeypatch,
) -> None:
    calls: list[str] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.deckBrowser = SimpleNamespace(
        cancel_rwkv_count_refresh=lambda: calls.append("cancel rwkv counts")
    )
    mw.unloadCollection = lambda _callback: calls.append(  # type: ignore[method-assign]
        "unload collection"
    )
    monkeypatch.setattr(
        aqt.main.gui_hooks,
        "profile_will_close",
        lambda: calls.append("profile will close"),
    )

    mw.unloadProfile(lambda: calls.append("done"))

    assert calls == [
        "cancel rwkv counts",
        "profile will close",
        "unload collection",
    ]


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
    mw.toolbarWeb = type(
        "ToolbarWeb", (), {"cleanup": lambda self: calls.append("cleanup_toolbar")}
    )()
    mw.web = type("Web", (), {"cleanup": lambda self: calls.append("cleanup_web")})()
    mw.bottomWeb = type(
        "BottomWeb", (), {"cleanup": lambda self: calls.append("cleanup_bottom")}
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
        "cleanup_toolbar",
        "cleanup_web",
        "cleanup_bottom",
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


def _rollover_mw(
    monkeypatch, state: str, cutoff: int
) -> tuple[AnkiQt, list[str], list[int]]:
    """A main window at the day-rollover check: `calls` records the
    reviewer refreshes and the day_did_change calls, `timers` the delay of
    each next check in ms."""
    calls: list[str] = []
    timers: list[int] = []
    mw = AnkiQt.__new__(AnkiQt)
    mw.state = state
    mw.col = SimpleNamespace(sched=SimpleNamespace(day_cutoff=cutoff))  # type: ignore[assignment]

    def refresh_if_needed() -> None:
        calls.append(f"reviewer refresh {mw.reviewer._refresh_needed.name}")

    mw.reviewer = SimpleNamespace(  # type: ignore[assignment]
        _refresh_needed=None, refresh_if_needed=refresh_if_needed
    )
    mw.progress = SimpleNamespace(  # type: ignore[assignment]
        timer=lambda ms, func, repeat, parent: timers.append(ms)
    )
    monkeypatch.setattr(aqt.main, "int_time", lambda: 1_000)
    monkeypatch.setattr(
        aqt.main.gui_hooks, "day_did_change", lambda: calls.append("day_did_change")
    )
    mw._last_day_cutoff = cutoff
    return mw, calls, timers


def test_the_day_rollover_fires_day_did_change_in_the_reviewer(monkeypatch) -> None:
    """The check updated the remembered cutoff while reviewing, and then
    compared the updated value to decide on the hook, so the hook never
    fired while the reviewer was open."""

    mw, calls, timers = _rollover_mw(monkeypatch, "review", cutoff=900)
    mw.col.sched.day_cutoff = 900 + 86_400

    mw._check_day_rollover()

    assert calls == ["reviewer refresh QUEUES", "day_did_change"]
    # and the next check waits for the next cutoff
    assert timers == [(900 + 86_400 - 1_000) * 1000]


def test_the_day_rollover_fires_day_did_change_once_outside_the_reviewer(
    monkeypatch,
) -> None:
    mw, calls, timers = _rollover_mw(monkeypatch, "deckBrowser", cutoff=900)
    mw.col.sched.day_cutoff = 900 + 86_400

    mw._check_day_rollover()
    # a check that comes again on the same day (a timer that fires early)
    # is no second rollover
    mw._check_day_rollover()

    assert calls == ["day_did_change"]
    assert len(timers) == 2


def test_no_rollover_changes_nothing(monkeypatch) -> None:
    mw, calls, timers = _rollover_mw(monkeypatch, "review", cutoff=5_000)

    mw._check_day_rollover()

    assert calls == []
    assert timers == [4_000_000]
