# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from aqt import rwkv_scheduler
from aqt.reviewer import Reviewer


@pytest.fixture
def cold_reviewer(monkeypatch: pytest.MonkeyPatch) -> tuple[Reviewer, SimpleNamespace]:
    state = SimpleNamespace(ready=False, queued=False, builds=[], shown=[], exits=[])

    class Taskman:
        def with_progress(self, task, on_done, **kwargs) -> None:
            state.builds.append((task, on_done, kwargs))

        def run_in_background(self, task, on_done, **kwargs) -> None:
            future = Future()
            future.set_result(task())
            on_done(future)

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer.state = "transition"
    reviewer._reps = 1
    reviewer._previous_card_info = SimpleNamespace(set_card=lambda _card: None)
    reviewer._card_info = SimpleNamespace(set_card=lambda _card: None)
    reviewer.mw = SimpleNamespace(state="review", col=object(), taskman=Taskman())

    def move_to_state(target: str) -> None:
        reviewer.mw.state = target
        state.exits.append(target)

    def fetch() -> None:
        if state.queued:
            reviewer.card = SimpleNamespace(id=456)

    def show_question() -> None:
        reviewer.state = "question"
        state.shown.append(reviewer.card.id)

    def install(*args) -> bool:
        state.queued = True
        return True

    reviewer.mw.moveToState = move_to_state
    reviewer._get_rwkv_undo_restored_card = lambda: False
    reviewer._get_next_v3_card = fetch
    reviewer._showQuestion = show_question
    reviewer._cancel_qa_transition = lambda: None
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", object())
    monkeypatch.setattr(rwkv_scheduler, "reviewer_queue_order_enabled", lambda _: True)
    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_resident_state_ready", lambda _: state.ready
    )
    monkeypatch.setattr(
        rwkv_scheduler, "warm_up_rwkv_state", lambda *args, **kwargs: state.ready
    )
    monkeypatch.setattr(
        rwkv_scheduler, "prepare_reviewer_queue_order_async_work", lambda _: object()
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "score_reviewer_queue_order_async_work",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        rwkv_scheduler, "install_reviewer_queue_order_async_result", install
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "prewarm_reviewer_queue_score_cache",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("aqt.utils.tooltip", lambda *args, **kwargs: None)
    return reviewer, state


def test_next_card_recovers_cold_rwkv_state_before_ending(cold_reviewer) -> None:
    reviewer, state = cold_reviewer

    reviewer.nextCard()

    assert state.exits == []
    assert state.shown == []
    assert reviewer._review_actions_are_blocked()
    task, on_done, options = state.builds.pop()
    assert options["label"] == "Recovering RWKV state..."
    state.ready = True
    future = Future()
    future.set_result(task())
    on_done(future)

    assert state.shown == [456]
    assert state.exits == []
    assert not reviewer._review_actions_are_blocked()


def test_next_card_waits_for_existing_rwkv_recovery(cold_reviewer) -> None:
    reviewer, state = cold_reviewer
    completed = []
    rwkv_scheduler.build_rwkv_state_cache_with_progress(
        reviewer.mw, on_done=completed.append
    )

    reviewer.nextCard()
    reviewer.nextCard()

    assert state.exits == []
    assert len(state.builds) == 1
    task, on_done, _options = state.builds.pop()
    state.ready = True
    future = Future()
    future.set_result(task())
    on_done(future)

    assert state.shown == [456]
    assert completed == [True]
    assert state.exits == []


def test_next_card_is_released_when_existing_rwkv_cache_load_fails(
    cold_reviewer, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewer, state = cold_reviewer
    monkeypatch.setattr(
        rwkv_scheduler, "load_rwkv_state_cache", lambda *args, **kwargs: False
    )
    prompts = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "_show_rwkv_state_cache_prompt",
        lambda _mw: prompts.append(True),
    )
    rwkv_scheduler.load_rwkv_state_cache_with_progress(
        reviewer.mw, prompt_if_unavailable=True
    )
    reviewer.nextCard()
    assert state.exits == []
    assert len(state.builds) == 1
    task, on_done, _options = state.builds.pop()
    future = Future()
    future.set_result(task())
    on_done(future)

    assert state.exits == ["overview"]
    assert not reviewer._review_actions_are_blocked()
    assert prompts == [True]


@pytest.mark.parametrize("failure", [False, RuntimeError("recovery failed")])
def test_next_card_stops_retrying_when_rwkv_recovery_fails(
    cold_reviewer, failure
) -> None:
    reviewer, state = cold_reviewer
    reviewer.nextCard()
    task, on_done, _options = state.builds.pop()
    future = Future()
    if isinstance(failure, Exception):
        future.set_exception(failure)
    else:
        future.set_result(task())
    on_done(future)

    assert state.exits == ["overview"]
    assert state.shown == []
    assert state.builds == []
    assert not reviewer._review_actions_are_blocked()


@pytest.mark.parametrize("changed_target", ["overview", "collection", "generation"])
def test_next_card_ignores_stale_rwkv_recovery(cold_reviewer, changed_target) -> None:
    reviewer, state = cold_reviewer
    reviewer.nextCard()
    task, on_done, _options = state.builds.pop()
    if changed_target == "overview":
        reviewer.mw.state = "overview"
    elif changed_target == "collection":
        reviewer.mw.col = object()
    else:
        reviewer._review_card_generation += 1
    state.ready = True
    future = Future()
    future.set_result(task())
    on_done(future)

    assert state.shown == []
    assert state.exits == []
    assert not state.queued


def test_next_card_ends_normally_when_rwkv_state_is_ready(cold_reviewer) -> None:
    reviewer, state = cold_reviewer
    state.ready = True

    reviewer.nextCard()

    assert state.exits == ["overview"]
    assert state.builds == []


def test_next_card_ends_when_recovered_rwkv_queue_is_empty(
    cold_reviewer, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewer, state = cold_reviewer
    monkeypatch.setattr(
        rwkv_scheduler, "install_reviewer_queue_order_async_result", lambda *args: True
    )
    reviewer.nextCard()
    task, on_done, _options = state.builds.pop()
    state.ready = True
    future = Future()
    future.set_result(task())
    on_done(future)

    assert state.exits == ["overview"]
    assert state.builds == []


def test_next_card_recovers_through_qt_background_tasks(
    cold_reviewer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event, current_thread, main_thread

    from aqt.qt import QCoreApplication, QEventLoop, QTimer
    from aqt.taskman import TaskManager

    app = QCoreApplication.instance() or QCoreApplication([])
    reviewer, state = cold_reviewer
    reviewer.mw.weakref = lambda: reviewer.mw
    taskman = TaskManager(reviewer.mw)
    reviewer.mw.taskman = taskman
    progress_events = []
    reviewer.mw.progress = SimpleNamespace(
        start=lambda **kwargs: progress_events.append(kwargs["label"]),
        finish=lambda: progress_events.append("finished"),
    )
    loop = QEventLoop()
    release = Event()
    waiting_states = []
    worker_threads = []

    def observe_waiting() -> None:
        waiting_states.append(
            (reviewer.mw.state, reviewer.card, reviewer._review_actions_are_blocked())
        )
        release.set()

    def recover(*args, **kwargs) -> bool:
        worker_threads.append(current_thread() is not main_thread())
        taskman.run_on_main(observe_waiting)
        if not release.wait(10):
            raise TimeoutError("Qt did not deliver the recovery progress callback")
        state.ready = True
        return True

    monkeypatch.setattr(rwkv_scheduler, "warm_up_rwkv_state", recover)
    show_question = reviewer._showQuestion

    def show_and_finish() -> None:
        show_question()
        loop.quit()

    reviewer._showQuestion = show_and_finish
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    timeout.start(10_000)
    QTimer.singleShot(0, reviewer.nextCard)
    try:
        loop.exec()
        assert waiting_states == [("review", None, True)]
        assert worker_threads == [True]
        assert progress_events == ["Recovering RWKV state...", "finished"]
        assert state.shown == [456]
        assert state.exits == []
        assert not reviewer._review_actions_are_blocked()
    finally:
        timeout.stop()
        release.set()
        taskman._collection_executor.shutdown()
        taskman._no_collection_executor.shutdown()
        app.processEvents()
