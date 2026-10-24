# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import anki.collection  # Initialize collection before the cards/decks import cycle.
import aqt
from anki.collection import OpChangesWithCount, OpChangesWithId
from anki.decks import DeckId, FilteredDeckConfig
from anki.scheduler import FilteredDeckForUpdate
from aqt import rwkv_filtered_deck, rwkv_scheduler
from aqt.filtered_deck import FilteredDeckConfigDialog
from aqt.operations import scheduling
from aqt.operations.scheduling import FilteredDeckRwkvPolicy, RwkvFilteredDeckStateCold
from aqt.qt import QApplication, QEventLoop, QMessageBox, QTimer, QWidget
from aqt.rwkv_scheduler import FilteredDeckRwkvNeed, RwkvStatsPreparationStatus

Order = FilteredDeckConfig.SearchTerm.Order


@pytest.fixture(autouse=True)
def isolate_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests patch backend boundaries, so no resident state is published.
    # Monkeypatch restores the backend and leaves the suite's runtime maps intact.
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)


def config_for(
    search: str = "deck:Default", order: int = Order.RETRIEVABILITY_ASCENDING
) -> FilteredDeckConfig:
    return FilteredDeckConfig(
        search_terms=[
            FilteredDeckConfig.SearchTerm(search=search, order=order, limit=100)
        ]
    )


@pytest.mark.parametrize(
    "search,order,expected",
    [
        ("prop:r<0.9", Order.RANDOM, FilteredDeckRwkvNeed.NONE),
        ("prop:r<0.9", Order.DUE, FilteredDeckRwkvNeed.NONE),
        ("deck:Default", Order.RETRIEVABILITY_ASCENDING, FilteredDeckRwkvNeed.ORDER),
        ("deck:Default", Order.RETRIEVABILITY_DESCENDING, FilteredDeckRwkvNeed.ORDER),
        ("prop:rwkv:r<0.9", Order.RANDOM, FilteredDeckRwkvNeed.SEARCH),
        ("is:rwkv:due", Order.RANDOM, FilteredDeckRwkvNeed.SEARCH),
        ("-is:rwkv-curve:due", Order.RANDOM, FilteredDeckRwkvNeed.SEARCH),
        ("prop:rwkv-curve:r<0.9", Order.RANDOM, FilteredDeckRwkvNeed.SEARCH),
        (
            "prop:rwkv:r<0.9",
            Order.RETRIEVABILITY_ASCENDING,
            FilteredDeckRwkvNeed.SEARCH,
        ),
    ],
)
def test_rwkv_need_classifies_active_search_and_order(
    search: str, order: int, expected: FilteredDeckRwkvNeed
) -> None:
    assert rwkv_scheduler.filtered_deck_rwkv_need(config_for(search, order)) is expected


def test_rwkv_need_ignores_inactive_and_third_filters() -> None:
    config = config_for("prop:rwkv:r<0.9")
    config.search_terms[0].limit = 0
    config.search_terms.add(search="prop:r<0.9", limit=10, order=Order.RANDOM)
    config.search_terms.add(search="is:rwkv:due", limit=10, order=Order.RANDOM)
    assert rwkv_scheduler.filtered_deck_rwkv_need(config) is FilteredDeckRwkvNeed.NONE

    config.search_terms[1].order = Order.RETRIEVABILITY_ASCENDING
    assert rwkv_scheduler.filtered_deck_rwkv_need(config) is FilteredDeckRwkvNeed.ORDER
    config.search_terms[0].limit = 10
    assert rwkv_scheduler.filtered_deck_rwkv_need(config) is FilteredDeckRwkvNeed.SEARCH


@pytest.mark.parametrize(
    "backend,resident,loading,metadata,compatible,expected",
    [
        (False, False, False, None, False, False),
        (True, True, False, None, False, False),
        (True, False, True, None, False, False),
        (True, False, False, {"version": 1}, True, False),
        (True, False, False, None, False, True),
        (True, False, False, {"version": 1}, False, True),
    ],
)
def test_state_cold_uses_only_cheap_readiness_checks(
    monkeypatch: pytest.MonkeyPatch,
    backend: bool,
    resident: bool,
    loading: bool,
    metadata: dict[str, object] | None,
    compatible: bool,
    expected: bool,
) -> None:
    mw = SimpleNamespace()
    monkeypatch.setattr(
        rwkv_scheduler, "configure_reviewer_backend_from_environment", lambda: backend
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_resident_state_ready", lambda mw: resident
    )
    monkeypatch.setattr(rwkv_scheduler, "rwkv_state_cache_loading", lambda mw: loading)
    monkeypatch.setattr(
        rwkv_scheduler, "_read_rwkv_state_cache_metadata", lambda reviewer: metadata
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_state_cache_metadata_compatible",
        lambda reviewer, metadata: compatible,
    )
    for name in (
        "rwkv_state_cache_usable",
        "_warm_up_reviewer_backend",
        "_rwkv_collection_config_state",
    ):
        monkeypatch.setattr(
            rwkv_scheduler,
            name,
            lambda *args, **kwargs: pytest.fail("expensive or review-enabled check"),
        )

    assert rwkv_scheduler.filtered_deck_rwkv_state_cold(mw) is expected


def test_filtered_preparation_refuses_full_replay_but_default_still_warms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                build_search_string=lambda *terms, joiner="AND": f" {joiner} ".join(
                    terms
                )
            )
        )
    )
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", object())
    monkeypatch.setattr(
        rwkv_scheduler, "_reviewer_backend_warmed_up", lambda reviewer: False
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_reviewer_backend_warmup_pending", lambda reviewer: False
    )
    restore = MagicMock(return_value=False)
    clear_scores = MagicMock()
    monkeypatch.setattr(rwkv_scheduler, "_prepare_reviewer_backend_from_cache", restore)
    monkeypatch.setattr(rwkv_scheduler, "_set_rwkv_stats_graph_scores", clear_scores)
    warm_up = MagicMock(side_effect=AssertionError("full replay must not run"))
    monkeypatch.setattr(rwkv_scheduler, "_warm_up_reviewer_backend", warm_up)

    assert (
        rwkv_scheduler.prepare_filtered_deck_retrievability_scores(
            reviewer, config_for(), allow_full_replay=False
        )
        is RwkvStatsPreparationStatus.COLD
    )
    restore.assert_called_once_with(reviewer)
    warm_up.assert_not_called()
    clear_scores.assert_called_once_with(reviewer, "deck:Default AND -is:suspended", [])

    warm_up.side_effect = None
    warm_up.return_value = False
    assert (
        rwkv_scheduler.prepare_filtered_deck_retrievability_scores(
            reviewer, config_for()
        )
        is RwkvStatsPreparationStatus.FAILED
    )
    warm_up.assert_called_once_with(reviewer)


def test_no_replay_preparation_waits_for_existing_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = SimpleNamespace()
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", object())
    ready = False

    def wait(*args: Any, **kwargs: Any) -> bool:
        nonlocal ready
        ready = True
        return True

    monkeypatch.setattr(
        rwkv_scheduler, "_reviewer_backend_warmed_up", lambda reviewer: ready
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_reviewer_backend_warmup_pending", lambda reviewer: not ready
    )
    monkeypatch.setattr(rwkv_scheduler, "_wait_for_reviewer_backend_warmup", wait)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_warm_up_reviewer_backend",
        lambda *args: pytest.fail("full replay"),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_capture_reviewer_backend_prediction_state_token",
        lambda reviewer: None,
    )

    # The next scoring phase is intentionally unavailable, but warm-up completed.
    assert (
        rwkv_scheduler.prepare_stats_retrievability_scores(
            reviewer, "deck:Default", warm_up_if_needed=True, allow_full_replay=False
        )
        is RwkvStatsPreparationStatus.FAILED
    )
    assert ready


@pytest.fixture
def operation_context(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    deck = FilteredDeckForUpdate(id=200, name="Anatomie 🩺", config=config_for())
    sched = SimpleNamespace(
        get_or_create_filtered_deck=lambda **kwargs: deck,
        rebuild_filtered_deck=MagicMock(return_value=OpChangesWithCount(count=1)),
        add_or_update_filtered_deck=MagicMock(return_value=OpChangesWithId(id=200)),
    )
    col = SimpleNamespace(sched=sched)
    monkeypatch.setattr(aqt, "mw", None)
    monkeypatch.setattr(
        scheduling,
        "_run_preserving_rwkv_state",
        lambda col, mutation, **kwargs: mutation(),
    )
    return SimpleNamespace(col=col, deck=deck)


def run_operation(context: SimpleNamespace, operation: str, **kwargs: Any) -> Any:
    if operation == "rebuild":
        return scheduling.rebuild_filtered_deck(
            parent=None, deck_id=DeckId(200), **kwargs
        )._op(context.col)
    return scheduling.add_or_update_filtered_deck(
        parent=None, deck=context.deck, **kwargs
    )._op(context.col)


@pytest.mark.parametrize("operation", ["rebuild", "add_or_update"])
def test_require_state_cold_aborts_before_mutation(
    operation_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    prepare = MagicMock(return_value=RwkvStatsPreparationStatus.COLD)
    monkeypatch.setattr(
        rwkv_scheduler, "prepare_filtered_deck_retrievability_scores", prepare
    )
    with caplog.at_level("INFO", logger="aqt.rwkv_scheduler"):
        with pytest.raises(RwkvFilteredDeckStateCold, match="build it before"):
            run_operation(
                operation_context,
                operation,
                rwkv_policy=FilteredDeckRwkvPolicy.REQUIRE_STATE,
            )
    assert prepare.call_args.kwargs == {"allow_full_replay": False}
    operation_context.col.sched.rebuild_filtered_deck.assert_not_called()
    operation_context.col.sched.add_or_update_filtered_deck.assert_not_called()
    assert "status=cold" in caplog.text


@pytest.mark.parametrize("operation", ["rebuild", "add_or_update"])
def test_skip_mutates_without_preparing_and_logs_user_choice(
    operation_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    prepare = MagicMock(side_effect=AssertionError("must skip preparation"))
    monkeypatch.setattr(
        scheduling, "_prepare_filtered_deck_retrievability_scores", prepare
    )
    with caplog.at_level("INFO", logger="aqt.rwkv_scheduler"):
        run_operation(
            operation_context, operation, rwkv_policy=FilteredDeckRwkvPolicy.SKIP
        )
    prepare.assert_not_called()
    getattr(
        operation_context.col.sched, f"{operation}_filtered_deck"
    ).assert_called_once()
    assert len(caplog.records) == 1
    message = caplog.records[0].message
    assert "skipped by user choice; FSRS retrievability order is used" in message
    assert f"operation={operation} deck_id=200 deck_name='Anatomie 🩺'" in message
    assert "'search': 'deck:Default'" in message
    assert "'order': 'RETRIEVABILITY_ASCENDING'" in message


@pytest.mark.parametrize("operation", ["rebuild", "add_or_update"])
def test_operation_defaults_to_full_warmup(
    operation_context: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    prepare = MagicMock(return_value=RwkvStatsPreparationStatus.READY)
    monkeypatch.setattr(
        rwkv_scheduler, "prepare_filtered_deck_retrievability_scores", prepare
    )
    run_operation(operation_context, operation)
    assert prepare.call_args.kwargs == {"allow_full_replay": True}
    getattr(
        operation_context.col.sched, f"{operation}_filtered_deck"
    ).assert_called_once()


class FakeOp:
    def __init__(self, policy: FilteredDeckRwkvPolicy) -> None:
        self.policy = policy
        self.ran = False
        self.on_failure: Callable[[Exception], None] | None = None

    def failure(self, callback: Callable[[Exception], None]) -> FakeOp:
        self.on_failure = callback
        return self

    def run_in_background(self) -> None:
        self.ran = True


@pytest.fixture
def gui_flow(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    flow = SimpleNamespace(
        mw=SimpleNamespace(), parent=object(), ops=[], prompts=[], builds=[]
    )
    monkeypatch.setattr(
        rwkv_scheduler, "filtered_deck_rwkv_state_cold", lambda mw: True
    )
    monkeypatch.setattr(
        rwkv_filtered_deck,
        "ask_user_dialog",
        lambda text, **kwargs: flow.prompts.append(dict(text=text, **kwargs)),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "build_rwkv_state_cache_with_progress",
        lambda mw, **kwargs: flow.builds.append((mw, kwargs)),
    )

    def make_op(policy: FilteredDeckRwkvPolicy) -> FakeOp:
        op = FakeOp(policy)
        flow.ops.append(op)
        return op

    flow.run = lambda config: rwkv_filtered_deck.run_filtered_deck_op_with_rwkv_check(
        mw=flow.mw, parent=flow.parent, config=config, make_op=make_op
    )
    return flow


@pytest.mark.parametrize("no_need", [False, True])
def test_gui_starts_require_state_without_popup(
    gui_flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, no_need: bool
) -> None:
    def cold(mw: object) -> bool:
        assert not no_need, "NONE should not even inspect the state"
        return False

    monkeypatch.setattr(rwkv_scheduler, "filtered_deck_rwkv_state_cold", cold)
    gui_flow.run(config_for(order=Order.RANDOM) if no_need else config_for())
    assert gui_flow.prompts == []
    assert [(op.policy, op.ran) for op in gui_flow.ops] == [
        (FilteredDeckRwkvPolicy.REQUIRE_STATE, True)
    ]
    assert gui_flow.ops[0].on_failure is not None


@pytest.mark.parametrize("search", [False, True])
def test_gui_popup_offers_fsrs_only_for_ordering(
    gui_flow: SimpleNamespace, search: bool
) -> None:
    gui_flow.run(config_for("prop:rwkv:r<0.9") if search else config_for())
    prompt = gui_flow.prompts[0]
    assert prompt["buttons"] == (
        ["Build RWKV State", QMessageBox.StandardButton.Cancel]
        if search
        else [
            "Build RWKV State",
            "Build Deck Now in FSRS Order",
            QMessageBox.StandardButton.Cancel,
        ]
    )
    assert (
        "search uses RWKV values" if search else "sorted by retrievability"
    ) in prompt["text"]
    assert prompt["title"] == "RWKV State"
    assert prompt["parent"] is gui_flow.parent
    assert prompt["default_button"] == 0
    assert gui_flow.ops == []


@pytest.mark.parametrize("ready", [False, True])
def test_gui_build_continues_only_on_success(
    gui_flow: SimpleNamespace, ready: bool
) -> None:
    gui_flow.run(config_for())
    gui_flow.prompts[0]["callback"](0)
    assert len(gui_flow.builds) == 1
    mw, build = gui_flow.builds[0]
    assert mw is gui_flow.mw
    assert build["record_retrievability_cache"] is False
    assert gui_flow.ops == []
    build["on_done"](ready)
    assert [(op.policy, op.ran) for op in gui_flow.ops] == (
        [(FilteredDeckRwkvPolicy.REQUIRE_STATE, True)] if ready else []
    )


def test_gui_fsrs_choice_skips_preparation(gui_flow: SimpleNamespace) -> None:
    gui_flow.run(config_for())
    gui_flow.prompts[0]["callback"](1)
    assert [(op.policy, op.ran, op.on_failure) for op in gui_flow.ops] == [
        (FilteredDeckRwkvPolicy.SKIP, True, None)
    ]
    assert gui_flow.builds == []


@pytest.mark.parametrize("choice", [1, 2])
def test_gui_records_declined_for_fsrs_order_only(
    gui_flow: SimpleNamespace, choice: int
) -> None:
    gui_flow.run(config_for())

    gui_flow.prompts[0]["callback"](choice)

    assert rwkv_scheduler.rwkv_state_declined(gui_flow.mw) is (choice == 1)
    assert [(op.policy, op.ran) for op in gui_flow.ops] == (
        [(FilteredDeckRwkvPolicy.SKIP, True)] if choice == 1 else []
    )
    assert gui_flow.builds == []
    gui_flow.run(config_for())
    assert len(gui_flow.prompts) == 2


@pytest.mark.parametrize(
    "search,choice", [(False, 2), (False, -1), (True, 1), (True, -1)]
)
def test_gui_cancel_or_close_does_nothing(
    gui_flow: SimpleNamespace, search: bool, choice: int
) -> None:
    gui_flow.run(config_for("is:rwkv:due") if search else config_for())
    gui_flow.prompts[0]["callback"](choice)
    assert gui_flow.ops == []
    assert gui_flow.builds == []


@pytest.mark.parametrize("cold_error", [False, True])
def test_gui_failure_prompts_for_cold_state_or_shows_exception(
    gui_flow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, cold_error: bool
) -> None:
    monkeypatch.setattr(
        rwkv_scheduler, "filtered_deck_rwkv_state_cold", lambda mw: False
    )
    show_exception = MagicMock()
    monkeypatch.setattr(rwkv_filtered_deck.errors, "show_exception", show_exception)
    gui_flow.run(config_for())
    exc = RwkvFilteredDeckStateCold() if cold_error else ValueError("failed")
    gui_flow.ops[0].on_failure(exc)
    if cold_error:
        assert len(gui_flow.prompts) == 1
        show_exception.assert_not_called()
    else:
        assert gui_flow.prompts == []
        show_exception.assert_called_once_with(parent=gui_flow.parent, exception=exc)


@pytest.mark.parametrize("succeed", [False, True])
def test_dialog_stays_open_until_filtered_op_succeeds(succeed: bool) -> None:
    dialog = MagicMock()
    dialog.deck = FilteredDeckForUpdate(config=config_for())
    op = MagicMock()
    op.success.return_value = op

    def run(**kwargs: Any) -> None:
        assert kwargs["mw"] is dialog.mw
        assert kwargs["parent"] is dialog
        assert kwargs["config"] is dialog.deck.config
        if succeed:
            kwargs["make_op"](FilteredDeckRwkvPolicy.REQUIRE_STATE)
            op.success.call_args.args[0](OpChangesWithId(id=200))

    with (
        patch.object(
            rwkv_filtered_deck, "run_filtered_deck_op_with_rwkv_check", side_effect=run
        ),
        patch(
            "aqt.filtered_deck.add_or_update_filtered_deck", return_value=op
        ) as make_op,
        patch("aqt.filtered_deck.QDialog.accept") as accept,
        patch("aqt.filtered_deck.aqt.dialogs.markClosed") as closed,
        patch("aqt.filtered_deck.saveGeom"),
        patch(
            "aqt.filtered_deck.gui_hooks.filtered_deck_dialog_will_add_or_update_deck"
        ) as will_add,
        patch(
            "aqt.filtered_deck.gui_hooks.filtered_deck_dialog_did_add_or_update_deck"
        ) as did_add,
    ):
        FilteredDeckConfigDialog.accept(dialog)
        will_add.assert_called_once_with(dialog, dialog.deck)
        if succeed:
            make_op.assert_called_once_with(
                parent=dialog,
                deck=dialog.deck,
                rwkv_policy=FilteredDeckRwkvPolicy.REQUIRE_STATE,
            )
            did_add.assert_called_once_with(dialog, dialog.deck, 200)
            accept.assert_called_once_with(dialog)
            closed.assert_called_once_with(dialog.DIALOG_KEY)
        else:
            make_op.assert_not_called()
            accept.assert_not_called()
            closed.assert_not_called()


@pytest.fixture(scope="module")
def app() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("choice", ["fsrs", "cancel", "build"])
def test_real_qt_prompt_and_background_filtered_deck_flow(
    app: Any,
    operation_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
) -> None:
    from aqt import operations
    from aqt.taskman import TaskManager

    mw = operation_context.col.mw = SimpleNamespace(col=operation_context.col)
    mw.weakref = lambda: mw
    mw.state = "test"
    progress_labels: list[str | None] = []
    mw.progress = SimpleNamespace(
        start=lambda **kwargs: progress_labels.append(kwargs.get("label")),
        finish=lambda: None,
    )
    mw._increase_background_ops = lambda: None
    mw._decrease_background_ops = lambda: None
    taskman = TaskManager(mw)
    mw.taskman = taskman
    monkeypatch.setattr(aqt, "mw", mw)
    monkeypatch.setattr(operations, "on_op_finished", lambda *args: None)
    ready = False
    monkeypatch.setattr(
        rwkv_scheduler, "configure_reviewer_backend_from_environment", lambda: True
    )
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_resident_state_ready", lambda mw: ready)
    monkeypatch.setattr(rwkv_scheduler, "rwkv_state_cache_loading", lambda mw: False)
    monkeypatch.setattr(
        rwkv_scheduler, "_read_rwkv_state_cache_metadata", lambda reviewer: None
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_refresh_active_rwkv_count_view", lambda mw: False
    )
    monkeypatch.setattr("aqt.utils.tooltip", lambda *args, **kwargs: None)
    builds: list[bool] = []

    def warm_up(mw: object, **kwargs: Any) -> bool:
        nonlocal ready
        builds.append(threading.current_thread() is threading.main_thread())
        ready = True
        return True

    monkeypatch.setattr(rwkv_scheduler, "warm_up_rwkv_state", warm_up)
    prepare = MagicMock(return_value=RwkvStatsPreparationStatus.READY)
    monkeypatch.setattr(
        rwkv_scheduler, "prepare_filtered_deck_retrievability_scores", prepare
    )
    worker_calls: list[tuple[int, bool]] = []

    def rebuild(deck_id: int) -> OpChangesWithCount:
        worker_calls.append(
            (deck_id, threading.current_thread() is threading.main_thread())
        )
        return OpChangesWithCount(count=1)

    operation_context.col.sched.rebuild_filtered_deck = rebuild
    loop = QEventLoop()
    successes: list[tuple[int, bool]] = []
    errors: list[Exception] = []

    def success(out: OpChangesWithCount) -> None:
        successes.append(
            (out.count, threading.current_thread() is threading.main_thread())
        )
        loop.quit()

    monkeypatch.setattr(
        rwkv_filtered_deck.errors,
        "show_exception",
        lambda **kwargs: (errors.append(kwargs["exception"]), loop.quit()),
    )
    parent = QWidget()
    parent.show()
    click_timer = QTimer()
    clicked: list[str] = []

    def click_choice() -> None:
        for widget in QApplication.topLevelWidgets():
            if (
                not isinstance(widget, QMessageBox)
                or not widget.isVisible()
                or widget.parent() is not parent
            ):
                continue
            button = (
                widget.button(QMessageBox.StandardButton.Cancel)
                if choice == "cancel"
                else next(
                    button
                    for button in widget.buttons()
                    if button.text()
                    == (
                        "Build RWKV State"
                        if choice == "build"
                        else "Build Deck Now in FSRS Order"
                    )
                )
            )
            clicked.append(choice)
            click_timer.stop()
            button.click()
            if choice == "cancel":
                QTimer.singleShot(50, loop.quit)
            return

    click_timer.timeout.connect(click_choice)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    try:
        rwkv_filtered_deck.run_filtered_deck_op_with_rwkv_check(
            mw=mw,
            parent=parent,
            config=operation_context.deck.config,
            make_op=lambda policy: scheduling.rebuild_filtered_deck(
                parent=parent, deck_id=DeckId(200), rwkv_policy=policy
            ).success(success),
        )
        click_timer.start(10)
        timeout.start(5_000)
        loop.exec()
        assert clicked == [choice], [
            (type(widget).__name__, widget.isVisible(), widget.windowTitle())
            for widget in QApplication.topLevelWidgets()
        ]
        assert errors == []
        assert worker_calls == ([] if choice == "cancel" else [(200, False)])
        assert successes == ([] if choice == "cancel" else [(1, True)])
        if choice == "build":
            assert builds == [False]
            assert progress_labels == ["Building RWKV state cache...", None]
            assert prepare.call_args.kwargs == {"allow_full_replay": False}
        else:
            assert builds == []
            prepare.assert_not_called()
    finally:
        click_timer.stop()
        timeout.stop()
        taskman._collection_executor.shutdown()
        taskman._no_collection_executor.shutdown()
        for message in parent.findChildren(QMessageBox):
            message.close()
        parent.close()
        app.processEvents()
