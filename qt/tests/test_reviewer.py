# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

import aqt.reviewer as reviewer_module
import aqt.rwkv_scheduler
from anki import cards_pb2
from anki.collection import OpChanges
from aqt.reviewer import RefreshNeeded, Reviewer, SchedulingStates


def scheduling_states_with_review_current() -> SchedulingStates:
    states = SchedulingStates()
    states.current.normal.review.scheduled_days = 1
    states.good.normal.review.scheduled_days = 1
    return states


def test_timebox_elapsed_secs_uses_collection_start_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.mw = SimpleNamespace(
        col=SimpleNamespace(conf={"timeLim": 120}, _startTime=100)
    )

    monkeypatch.setattr(reviewer_module.time, "time", lambda: 125.8)

    assert reviewer._timebox_elapsed_secs() == 25


def test_timebox_elapsed_secs_is_zero_when_disabled() -> None:
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.mw = SimpleNamespace(
        col=SimpleNamespace(conf={"timeLim": 0}, _startTime=100)
    )

    assert reviewer._timebox_elapsed_secs() == 0


def test_timebox_reps_uses_collection_start_reps() -> None:
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.mw = SimpleNamespace(
        col=SimpleNamespace(
            conf={"timeLim": 120}, _startReps=20, sched=SimpleNamespace(reps=27)
        )
    )

    assert reviewer._timebox_reps() == 7


def stub_bottom_html_translations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reviewer_module.tr, "studying_edit", lambda: "Edit")
    monkeypatch.setattr(reviewer_module.tr, "studying_more", lambda: "More")
    monkeypatch.setattr(
        reviewer_module.tr, "actions_shortcut_key", lambda val: str(val)
    )


def test_bottom_html_includes_timebox_progress_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_bottom_html_translations(monkeypatch)
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(time_taken=lambda: 1000)
    reviewer.mw = SimpleNamespace(col=SimpleNamespace(conf={"timeLim": 300}))

    html = reviewer._bottomHTML()

    assert "id=timebox-summary" in html
    assert "id=timebox-progress><div></div></div>" in html
    assert "timeboxLimit = 300;" in html


def test_bottom_html_hides_timebox_progress_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_bottom_html_translations(monkeypatch)
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(time_taken=lambda: 1000)
    reviewer.mw = SimpleNamespace(col=SimpleNamespace(conf={"timeLim": 0}))

    html = reviewer._bottomHTML()

    assert "id=timebox-summary" in html
    assert "id=timebox-progress hidden><div></div></div>" in html
    assert "timeboxLimit = 0;" in html


def test_typed_answer_callback_ignored_after_scheduler_state_cleared() -> None:
    class Card:
        def answer(self) -> str:
            raise AssertionError("stale callback should not render the answer")

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.mw = SimpleNamespace(state="review")
    reviewer.card = SimpleNamespace(id=123, answer=Card().answer)
    reviewer.state = "question"
    reviewer._v3 = None
    reviewer._question_update_id = 1
    reviewer._question_rendered = True

    reviewer._onTypedAnswer("typed", 123, 1)

    assert reviewer.typedAnswer == "typed"


def test_stale_typed_answer_callback_ignored_after_card_changes() -> None:
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.mw = SimpleNamespace(state="review")
    reviewer.card = SimpleNamespace(id=456)
    reviewer.state = "question"
    reviewer.typedAnswer = None
    reviewer._showAnswer = lambda: (_ for _ in ()).throw(
        AssertionError("stale callback should not show the answer")
    )

    reviewer._onTypedAnswer("typed", 123)

    assert reviewer.typedAnswer is None
    assert reviewer.state == "question"


def test_answer_card_ignored_until_answer_side_rendered() -> None:
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.mw = SimpleNamespace(state="review")
    reviewer.state = "answer"
    reviewer._answer_rendered = False

    reviewer._answerCard(3)

    assert reviewer.state == "answer"


def test_answer_rendered_updates_web_and_enables_answering() -> None:
    class Web:
        def __init__(self) -> None:
            self.update_count = 0

        def update(self) -> None:
            self.update_count += 1

    class MainWeb:
        def __init__(self) -> None:
            self.focused = False

        def setFocus(self) -> None:
            self.focused = True

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.web = Web()
    main_web = MainWeb()
    reviewer.mw = SimpleNamespace(web=main_web)
    reviewer.state = "answer"
    reviewer.card = object()
    reviewer._answer_update_id = 12
    reviewer._answer_rendered = False
    calls: list[str] = []
    reviewer._showEaseButtons = lambda: calls.append("buttons")
    reviewer._auto_advance_to_question_if_enabled = lambda: calls.append("auto")

    reviewer._linkHandler("qaUpdated:answer:11")

    assert reviewer.web.update_count == 0
    assert reviewer._answer_rendered is False
    assert calls == []

    reviewer._linkHandler("qaUpdated:answer:12")

    assert reviewer.web.update_count == 1
    assert reviewer._answer_rendered is True
    assert main_web.focused is True
    assert calls == ["buttons", "auto"]


def test_question_rendered_updates_only_current_question() -> None:
    class Web:
        def __init__(self) -> None:
            self.update_count = 0

        def update(self) -> None:
            self.update_count += 1

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.web = Web()
    reviewer.state = "question"
    reviewer.card = SimpleNamespace(id=123)
    reviewer._question_update_id = 12
    reviewer._question_rendered = False

    reviewer._linkHandler("qaUpdated:question:11")

    assert reviewer.web.update_count == 0
    assert reviewer._question_rendered is False

    reviewer._linkHandler("qaUpdated:question:12")

    assert reviewer.web.update_count == 1
    assert reviewer._question_rendered is True


def test_show_answer_ignored_until_current_question_rendered(monkeypatch) -> None:
    calls: list[str] = []

    class Card:
        id = 123

        def answer(self) -> str:
            return "back"

        def autoplay(self) -> bool:
            return False

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.mw = SimpleNamespace(state="review")
    reviewer.web = SimpleNamespace(eval=lambda script: calls.append(script))
    reviewer.card = Card()
    reviewer.state = "question"
    reviewer._v3 = object()
    reviewer._qa_update_id = 1
    reviewer._question_update_id = 1
    reviewer._question_rendered = False
    reviewer._mungeQA = lambda text: text

    monkeypatch.setattr(reviewer_module.av_player, "play_tags", lambda sounds: None)

    reviewer._showAnswer()

    assert calls == []
    assert reviewer.state == "question"

    reviewer._question_rendered = True
    reviewer._showAnswer()

    assert reviewer.state == "answer"
    assert len(calls) == 1


def test_typed_answer_waits_for_current_question_rendered() -> None:
    calls: list[str] = []

    class Web:
        def evalWithCallback(
            self, script: str, callback: Callable[[str], None]
        ) -> None:
            calls.append(script)
            callback("typed")

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.web = Web()
    reviewer.state = "question"
    reviewer.card = SimpleNamespace(id=123)
    reviewer.typedAnswer = None
    reviewer._question_update_id = 1
    reviewer._question_rendered = False
    reviewer._showAnswer = lambda: calls.append("show")

    reviewer._getTypedAnswer()

    assert calls == []
    assert reviewer.typedAnswer is None

    reviewer._question_rendered = True
    reviewer._getTypedAnswer()

    assert calls == ["getTypedAnswer();", "show"]
    assert reviewer.typedAnswer == "typed"


def test_stale_typed_answer_callback_ignored_after_question_update_changes() -> None:
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.state = "question"
    reviewer.card = SimpleNamespace(id=123)
    reviewer.typedAnswer = None
    reviewer._question_update_id = 2
    reviewer._question_rendered = True
    reviewer._showAnswer = lambda: (_ for _ in ()).throw(
        AssertionError("stale callback should not show the answer")
    )

    reviewer._onTypedAnswer("typed", 123, 1)

    assert reviewer.typedAnswer is None


def test_blocked_review_actions_ignore_enter_and_answer_shortcuts() -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("review action should be blocked")

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.mw = SimpleNamespace(state="review")
    reviewer.web = SimpleNamespace(evalWithCallback=fail)
    reviewer.bottom = SimpleNamespace(web=SimpleNamespace(evalWithCallback=fail))
    reviewer.card = SimpleNamespace(id=123, answer=fail)
    reviewer._v3 = object()
    reviewer._answer_rendered = True
    reviewer.set_review_actions_blocked(True)

    reviewer.state = "question"
    reviewer.onEnterKey()
    reviewer._showAnswer()

    reviewer.state = "answer"
    reviewer.onEnterKey()
    reviewer._answerCard(3)

    assert reviewer.state == "answer"


def test_answer_only_block_allows_show_answer_but_not_rating() -> None:
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.mw = SimpleNamespace(state="review")
    reviewer.state = "question"
    reviewer._review_actions_blocked = False
    reviewer._review_actions_block_id = 0
    reviewer._review_answer_actions_blocked = False
    reviewer._review_answer_actions_block_id = 0
    reviewer._v3 = object()
    calls: list[str] = []
    reviewer._getTypedAnswer = lambda: calls.append("show")

    reviewer._set_review_answer_actions_blocked(True)
    reviewer.onEnterKey()

    assert calls == ["show"]

    reviewer.state = "answer"
    reviewer._answer_rendered = True
    reviewer._answerCard(3)

    assert calls == ["show"]


def test_show_question_preserves_existing_review_action_block(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Card:
        id = 123
        ord = 0

        def question(self) -> str:
            return "front"

        def answer(self) -> str:
            return "back"

        def autoplay(self) -> bool:
            return False

    class Web:
        def setPlaybackRequiresGesture(self, value: bool) -> None:
            calls.append(f"gesture:{value}")

        def eval(self, script: str) -> None:
            calls.append("eval")

        def evalWithCallback(
            self, script: str, callback: Callable[[str], None]
        ) -> None:
            callback("")

        def update(self) -> None:
            calls.append("update")

    monkeypatch.setattr(
        reviewer_module.theme_manager,
        "body_classes_for_card_ord",
        lambda _card_ord: "",
    )
    monkeypatch.setattr(
        reviewer_module.av_player,
        "play_tags",
        lambda sounds: calls.append("audio"),
    )

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.web = Web()
    reviewer.mw = SimpleNamespace(
        col=SimpleNamespace(
            media=SimpleNamespace(escape_media_filenames=lambda text: text)
        ),
        web=SimpleNamespace(setFocus=lambda: calls.append("focus")),
        state="review",
    )
    reviewer.card = Card()
    reviewer._reps = 0
    reviewer._qa_update_id = 0
    reviewer._v3 = object()
    reviewer.auto_advance_enabled = False
    reviewer._mungeQA = lambda text: text
    reviewer._run_state_mutation_hook = lambda: calls.append("mutation")
    reviewer._update_flag_icon = lambda: calls.append("flag")
    reviewer._update_mark_icon = lambda: calls.append("mark")
    reviewer._showAnswerButton = lambda: calls.append("button")
    reviewer._auto_advance_to_answer_if_enabled = lambda: calls.append("auto")
    reviewer._run_after_question_shown_callbacks = lambda: calls.append("after")
    reviewer.set_review_actions_blocked(True)

    reviewer._showQuestion()

    assert reviewer._review_actions_are_blocked() is True

    reviewer._showAnswer = lambda: calls.append("answer")
    reviewer._linkHandler("ans")

    assert "answer" not in calls

    reviewer.set_review_actions_blocked(False)
    reviewer._linkHandler("qaUpdated:question:1")
    reviewer._linkHandler("ans")

    assert calls[-1] == "answer"


def test_answer_buttons_wait_for_pending_scheduling_states() -> None:
    class Progress:
        def __init__(self) -> None:
            self.single_shots = 0

        def single_shot(self, delay: int, callback: Callable[[], None]) -> None:
            self.single_shots += 1

    reviewer = Reviewer.__new__(Reviewer)
    progress = Progress()
    reviewer.mw = SimpleNamespace(progress=progress)
    reviewer._states_mutated = True
    reviewer._scheduling_states_pending = True
    reviewer._v3 = SimpleNamespace(states=SchedulingStates())
    reviewer._answerButtons = lambda: (_ for _ in ()).throw(
        AssertionError("answer buttons should wait for scheduling states")
    )

    reviewer._showEaseButtons()

    assert progress.single_shots == 1


def test_answer_card_populates_empty_scheduling_states_before_answering(
    monkeypatch,
) -> None:
    populated_states = scheduling_states_with_review_current()
    built_with: list[SchedulingStates] = []

    class Scheduler:
        def get_scheduling_states(
            self, card_id: int, desired_retention_override: float | None = None
        ) -> SchedulingStates:
            assert card_id == 123
            assert desired_retention_override == 0.8
            return populated_states

        def build_answer(
            self,
            *,
            card: object,
            states: SchedulingStates,
            rating: int,
            desired_retention_override: float | None = None,
        ) -> object:
            built_with.append(states)
            return SimpleNamespace(new_state=states.good)

    class Operation:
        def success(self, callback: Callable[[object], None]) -> object:
            return self

        def run_in_background(self, *, initiator: object) -> None:
            pass

    captured_answers: list[object] = []

    def fake_answer_card(*, parent: object, answer: object) -> Operation:
        captured_answers.append(answer)
        return Operation()

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123, custom_data='{"v":"reschedule"}')
    reviewer.mw = SimpleNamespace(
        state="review", col=SimpleNamespace(sched=Scheduler())
    )
    reviewer.state = "answer"
    reviewer._answer_rendered = True
    reviewer._desired_retention_override = 0.8
    reviewer._scheduling_states_pending = False
    reviewer._v3 = SimpleNamespace(
        states=SchedulingStates(), rating_from_ease=lambda ease: ease
    )

    monkeypatch.setattr("aqt.reviewer.answer_card", fake_answer_card)

    reviewer._answerCard(3)

    assert built_with == [populated_states]
    assert captured_answers
    assert reviewer._v3.states.current.custom_data == '{"v":"reschedule"}'


def test_answer_card_updates_undo_actions_before_after_answering(monkeypatch) -> None:
    states = scheduling_states_with_review_current()
    calls: list[str] = []
    captured_answers: list[object] = []

    class Scheduler:
        def build_answer(
            self,
            *,
            card: object,
            states: SchedulingStates,
            rating: int,
            desired_retention_override: float | None = None,
        ) -> object:
            return SimpleNamespace(new_state=states.good)

        def state_is_leech(self, new_state: object) -> bool:
            return False

    class Operation:
        def __init__(self) -> None:
            self._callback: Callable[[object], None] | None = None

        def success(self, callback: Callable[[object], None]) -> object:
            self._callback = callback
            return self

        def run_in_background(self, *, initiator: object) -> None:
            assert self._callback is not None
            self._callback(SimpleNamespace())

    def fake_answer_card(*, parent: object, answer: object) -> Operation:
        captured_answers.append(answer)
        return Operation()

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123, custom_data="", queue=0)
    reviewer.mw = SimpleNamespace(
        state="review",
        col=SimpleNamespace(sched=Scheduler()),
        update_undo_actions=lambda: calls.append("undo"),
    )
    reviewer.state = "answer"
    reviewer._answer_rendered = True
    reviewer._desired_retention_override = None
    reviewer._scheduling_states_pending = False
    reviewer._v3 = SimpleNamespace(
        states=states,
        rating_from_ease=lambda ease: ease,
    )
    reviewer._rwkv_review_prediction = aqt.rwkv_scheduler.RwkvReviewerPrediction(
        card_id=123,
        retrievability=0.62,
        review_enabled=True,
        interval_override_used=True,
        s90_overrides=aqt.rwkv_scheduler.RwkvIntervalOverride(good=10),
    )
    reviewer._after_answering = lambda ease: calls.append("after")

    monkeypatch.setattr("aqt.reviewer.answer_card", fake_answer_card)

    reviewer._answerCard(3)

    assert calls == ["undo", "after"]
    assert not hasattr(captured_answers[0], "rwkv_s90")
    assert captured_answers[0].rwkv_retrievability == 0.62


def test_after_answering_updates_rwkv_review_state() -> None:
    class RwkvBackend:
        def __init__(self) -> None:
            self.reviewed: list[tuple[int, int]] = []

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> None:
            return None

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            self.reviewed.append((getattr(card, "id"), ease))

    backend = RwkvBackend()
    previous_backend = aqt.rwkv_scheduler.set_reviewer_backend(backend)
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer._answeredIds = []
    reviewer.check_timebox = lambda: True

    try:
        reviewer._after_answering(3)
    finally:
        aqt.rwkv_scheduler.set_reviewer_backend(previous_backend)

    assert backend.reviewed == [(123, 3)]
    assert reviewer._answeredIds == [123]


def test_after_answering_refreshes_rwkv_queue_order_after_next_card(
    monkeypatch,
) -> None:
    calls: list[str] = []
    work = object()
    result = object()

    def record_reviewer_answer(reviewer: object, card: object, ease: int) -> None:
        calls.append("record")

    def prepare_reviewer_queue_order_async_work(reviewer: object) -> object:
        calls.append("build")
        return work

    def score_reviewer_queue_order_async_work(arg: object) -> object:
        assert arg is work
        calls.append("score")
        return result

    def install_reviewer_queue_order_async_result(
        reviewer: object, arg: object
    ) -> bool:
        assert arg is result
        calls.append("install")
        return True

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], None],
            on_done: Callable[[Future[None]], None],
            uses_collection: bool = True,
        ) -> None:
            calls.append("collection" if uses_collection else "free")
            value = task()
            future: Future[object] = Future()
            future.set_result(value)
            on_done(future)

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer._answeredIds = []
    reviewer.state = "transition"
    reviewer.mw = SimpleNamespace(
        taskman=Taskman(),
        update_undo_actions=lambda: calls.append("undo"),
    )
    reviewer.check_timebox = lambda: False

    def next_card() -> None:
        calls.append("next")
        reviewer.card = SimpleNamespace(id=456)
        reviewer.state = "question"

    reviewer.nextCard = next_card

    monkeypatch.setattr(
        aqt.rwkv_scheduler, "record_reviewer_answer", record_reviewer_answer
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_reviewer_queue_order_async_work",
        prepare_reviewer_queue_order_async_work,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "score_reviewer_queue_order_async_work",
        score_reviewer_queue_order_async_work,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "install_reviewer_queue_order_async_result",
        install_reviewer_queue_order_async_result,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_refresh_due",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_needs_intervening_review_refresh",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "update_reviewer_queue_intervening_reviews",
        lambda reviewer, card: calls.append(f"intervening:{card.id}"),
    )

    reviewer._after_answering(3)

    assert calls == ["record", "intervening:123", "next"]
    assert reviewer._answeredIds == [123]

    reviewer._run_after_question_shown_callbacks()

    assert calls == [
        "record",
        "intervening:123",
        "next",
        "collection",
        "build",
        "free",
        "score",
        "collection",
        "install",
        "undo",
    ]
    assert reviewer._answeredIds == [123]


def test_after_answering_deferred_refresh_skips_queue_rewrite_without_intervening_guard(
    monkeypatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "record_reviewer_answer",
        lambda reviewer, card, ease: calls.append("record"),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_refresh_due",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_refresh_before_next_card",
        lambda reviewer: False,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_needs_intervening_review_refresh",
        lambda reviewer: False,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "update_reviewer_queue_intervening_reviews",
        lambda reviewer, card: calls.append("intervening"),
    )

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer._answeredIds = []
    reviewer.state = "transition"
    reviewer.check_timebox = lambda: False
    reviewer.nextCard = lambda: calls.append("next")

    reviewer._after_answering(3)

    assert calls == ["record", "next"]
    assert reviewer._answeredIds == [123]


def test_after_answering_refreshes_rwkv_queue_before_closing_last_card(
    monkeypatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "record_reviewer_answer",
        lambda reviewer, card, ease: calls.append("record"),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_refresh_due",
        lambda reviewer: False,
    )

    def prepare_then_next(
        queued_at: float | None = None,
        *,
        answered_card_id: int | None = None,
        fade_after: bool = False,
        show_next_card: bool = False,
    ) -> None:
        assert queued_at is not None
        assert fade_after is False
        calls.append(f"prepare:{answered_card_id}:{show_next_card}")

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer._answeredIds = []
    reviewer._v3 = SimpleNamespace(
        queued_cards=SimpleNamespace(
            new_count=0,
            learning_count=0,
            review_count=1,
        )
    )
    reviewer.check_timebox = lambda: False
    reviewer.nextCard = lambda: calls.append("next")
    reviewer._prepare_rwkv_queue_order_then_next_card = prepare_then_next

    reviewer._after_answering(3)

    assert calls == ["record", "prepare:123:True"]
    assert reviewer._answeredIds == [123]


def test_after_answering_interval_refresh_prefetches_during_next_question(
    monkeypatch,
) -> None:
    calls: list[str] = []
    work = object()
    result = object()

    def record_reviewer_answer(reviewer: object, card: object, ease: int) -> None:
        calls.append(f"record:{card.id}:{ease}")

    def prepare_reviewer_queue_order_async_work(reviewer: object) -> object:
        assert reviewer.card.id == 333
        assert reviewer.state == "question"
        calls.append("build")
        return work

    def score_reviewer_queue_order_async_work(arg: object) -> object:
        assert arg is work
        calls.append("score")
        return result

    def install_reviewer_queue_order_async_result(
        reviewer: object, arg: object
    ) -> bool:
        assert arg is result
        calls.append("install")
        return True

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], None],
            on_done: Callable[[Future[None]], None],
            uses_collection: bool = True,
        ) -> None:
            calls.append("collection" if uses_collection else "free")
            value = task()
            future: Future[object] = Future()
            future.set_result(value)
            on_done(future)

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=222)
    reviewer._answeredIds = [111]
    reviewer.state = "transition"
    reviewer.mw = SimpleNamespace(
        taskman=Taskman(),
        update_undo_actions=lambda: calls.append("undo"),
    )
    reviewer.check_timebox = lambda: False

    def next_card() -> None:
        calls.append("next:333")
        reviewer.card = SimpleNamespace(id=333)
        reviewer.state = "question"

    reviewer.nextCard = next_card

    monkeypatch.setattr(
        aqt.rwkv_scheduler, "record_reviewer_answer", record_reviewer_answer
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_reviewer_queue_order_async_work",
        prepare_reviewer_queue_order_async_work,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "score_reviewer_queue_order_async_work",
        score_reviewer_queue_order_async_work,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "install_reviewer_queue_order_async_result",
        install_reviewer_queue_order_async_result,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_refresh_due",
        lambda reviewer: len(reviewer._answeredIds) % 2 == 0,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_needs_intervening_review_refresh",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "update_reviewer_queue_intervening_reviews",
        lambda reviewer, card: calls.append(f"intervening:{card.id}"),
    )

    reviewer._after_answering(3)

    assert calls == ["record:222:3", "intervening:222", "next:333"]
    assert reviewer._answeredIds == [111, 222]

    reviewer._run_after_question_shown_callbacks()

    assert calls == [
        "record:222:3",
        "intervening:222",
        "next:333",
        "collection",
        "build",
        "free",
        "score",
        "collection",
        "install",
        "undo",
    ]
    assert reviewer._answeredIds == [111, 222]


def test_after_answering_rwkv_new_gather_refreshes_before_next_card(
    monkeypatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "record_reviewer_answer",
        lambda reviewer, card, ease: calls.append("record"),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_refresh_due",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_refresh_before_next_card",
        lambda reviewer: True,
    )

    def prepare_then_next(
        queued_at: float | None = None,
        *,
        answered_card_id: int | None = None,
        fade_after: bool = False,
        show_next_card: bool = False,
    ) -> None:
        assert queued_at is not None
        assert fade_after is False
        calls.append(f"prepare:{answered_card_id}:{show_next_card}")

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=222)
    reviewer._answeredIds = [111]
    reviewer._v3 = SimpleNamespace(
        queued_cards=SimpleNamespace(
            new_count=2,
            learning_count=0,
            review_count=0,
        )
    )
    reviewer.check_timebox = lambda: False
    reviewer.nextCard = lambda: calls.append("next")
    reviewer._prepare_rwkv_queue_order_then_next_card = prepare_then_next

    reviewer._after_answering(3)

    assert calls == ["record", "prepare:222:True"]
    assert reviewer._answeredIds == [111, 222]


def test_after_answering_skips_rwkv_queue_order_until_refresh_due(
    monkeypatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "record_reviewer_answer",
        lambda reviewer, card, ease: calls.append("record"),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_reviewer_queue_order",
        lambda reviewer: calls.append("prepare"),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_refresh_due",
        lambda reviewer: False,
    )

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer._answeredIds = []
    reviewer.check_timebox = lambda: False
    reviewer.nextCard = lambda: calls.append("next")

    reviewer._after_answering(3)

    assert calls == ["record", "next"]
    assert reviewer._answeredIds == [123]


def test_after_answering_without_rwkv_queue_order_fetches_next_immediately(
    monkeypatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "record_reviewer_answer",
        lambda reviewer, card, ease: calls.append("record"),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_reviewer_queue_order",
        lambda reviewer: calls.append("prepare"),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: False,
    )

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer._answeredIds = []
    reviewer.check_timebox = lambda: False
    reviewer.nextCard = lambda: calls.append("next")

    reviewer._after_answering(3)

    assert calls == ["record", "prepare", "next"]


def test_cleanup_triggers_rwkv_queue_order_exit_refresh(monkeypatch) -> None:
    calls: list[str] = []
    work = object()
    result = object()

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], object],
            on_done: Callable[[Future[object]], None],
            uses_collection: bool = True,
        ) -> None:
            calls.append("collection" if uses_collection else "free")
            value = task()
            future: Future[object] = Future()
            future.set_result(value)
            on_done(future)

    def prepare_reviewer_queue_order_async_work(
        reviewer: object,
        *,
        reason: str = "review queue",
    ) -> object:
        calls.append(f"build:{reason}")
        return work

    def score_reviewer_queue_order_async_work(arg: object) -> object:
        assert arg is work
        calls.append("score")
        return result

    def install_reviewer_queue_order_async_result(
        reviewer: object,
        arg: object,
    ) -> bool:
        assert arg is result
        calls.append("install")
        return True

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_exit_refresh_needed",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_reviewer_queue_order_async_work",
        prepare_reviewer_queue_order_async_work,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "score_reviewer_queue_order_async_work",
        score_reviewer_queue_order_async_work,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "install_reviewer_queue_order_async_result",
        install_reviewer_queue_order_async_result,
    )
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer._answeredIds = [123]
    reviewer.auto_advance_enabled = True
    reviewer.mw = SimpleNamespace(
        taskman=Taskman(),
        update_undo_actions=lambda: calls.append("undo"),
    )

    reviewer.cleanup()

    assert calls == [
        "collection",
        "build:review queue exit refresh",
        "free",
        "score",
        "collection",
        "install",
        "undo",
    ]
    assert reviewer.card is None
    assert reviewer.auto_advance_enabled is False


def test_cleanup_skips_rwkv_queue_order_exit_refresh_without_answers(
    monkeypatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_exit_refresh_needed",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_reviewer_queue_order",
        lambda reviewer: calls.append("prepare"),
    )

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer._answeredIds = []
    reviewer.auto_advance_enabled = True

    reviewer.cleanup()

    assert calls == []
    assert reviewer.card is None
    assert reviewer.auto_advance_enabled is False


def test_refresh_queues_with_rwkv_queue_order_prepares_before_first_card(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], None],
            on_done: Callable[[Future[None]], None],
            uses_collection: bool = True,
        ) -> None:
            assert uses_collection is True
            calls.append("background")
            task()
            future: Future[None] = Future()
            future.set_result(None)
            on_done(future)

    def next_card() -> None:
        calls.append("next")
        reviewer.card = SimpleNamespace(id=123)
        reviewer.state = "question"

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_reviewer_queue_order",
        lambda reviewer: calls.append("prepare"),
    )

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = None
    reviewer.state = "overview"
    reviewer._refresh_needed = RefreshNeeded.QUEUES
    reviewer.mw = SimpleNamespace(
        taskman=Taskman(),
        fade_in_webview=lambda: calls.append("fade"),
    )
    reviewer.nextCard = next_card

    reviewer.refresh_if_needed()

    assert calls == ["background", "prepare", "next", "fade"]
    assert reviewer._refresh_needed is None


def test_study_queue_refresh_with_rwkv_queue_order_prepares_before_replacing_current_card(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], None],
            on_done: Callable[[Future[None]], None],
            uses_collection: bool = True,
        ) -> None:
            assert uses_collection is True
            calls.append("background")
            task()
            future: Future[None] = Future()
            future.set_result(None)
            on_done(future)

    def next_card() -> None:
        calls.append("next")
        reviewer.card = SimpleNamespace(id=456)
        reviewer.state = "question"

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_reviewer_queue_order",
        lambda reviewer: calls.append("prepare"),
    )

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer.state = "question"
    reviewer._refresh_needed = None
    reviewer.mw = SimpleNamespace(
        taskman=Taskman(),
        fade_in_webview=lambda: calls.append("fade"),
    )
    reviewer.nextCard = next_card

    changes = OpChanges()
    changes.study_queues = True
    dirty = reviewer.op_executed(changes, handler=None, focused=True)

    assert calls == ["background", "prepare", "next", "fade"]
    assert reviewer.card.id == 456
    assert reviewer._refresh_needed is None
    assert dirty is False


def test_study_queue_refresh_with_rwkv_undo_card_skips_queue_order_prepare(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def next_card() -> None:
        calls.append("next")
        assert aqt.rwkv_scheduler.pop_reviewer_undo_card_id(reviewer) == 456
        reviewer.card = SimpleNamespace(id=456)
        reviewer.state = "question"

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_reviewer_queue_order",
        lambda reviewer: calls.append("prepare"),
    )

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer.state = "question"
    reviewer._refresh_needed = None
    reviewer.mw = SimpleNamespace(fade_in_webview=lambda: calls.append("fade"))
    reviewer.nextCard = next_card
    aqt.rwkv_scheduler.queue_reviewer_undo_card_ids(reviewer, [456])

    changes = OpChanges()
    changes.study_queues = True
    dirty = reviewer.op_executed(changes, handler=None, focused=True)

    assert calls == ["next", "fade"]
    assert reviewer.card.id == 456
    assert reviewer._refresh_needed is None
    assert dirty is False


def test_study_queue_refresh_with_rwkv_undo_card_runs_when_unfocused(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def next_card() -> None:
        calls.append("next")
        assert aqt.rwkv_scheduler.pop_reviewer_undo_card_id(reviewer) == 456
        reviewer.card = SimpleNamespace(id=456)
        reviewer.state = "question"

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_reviewer_queue_order",
        lambda reviewer: calls.append("prepare"),
    )

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer.state = "question"
    reviewer._refresh_needed = None
    reviewer.mw = SimpleNamespace(fade_in_webview=lambda: calls.append("fade"))
    reviewer.nextCard = next_card
    reviewer._prepare_rwkv_queue_order_then_next_card = lambda *args, **kwargs: (
        (_ for _ in ()).throw(
            AssertionError("undo-restored card should not wait for ascending order")
        )
    )
    aqt.rwkv_scheduler.queue_reviewer_undo_card_ids(reviewer, [456])

    changes = OpChanges()
    changes.study_queues = True
    dirty = reviewer.op_executed(changes, handler=None, focused=False)

    assert calls == ["next", "fade"]
    assert reviewer.card.id == 456
    assert reviewer._refresh_needed is None
    assert dirty is False


@pytest.mark.parametrize("focused", [True, False])
def test_study_queue_refresh_while_rwkv_undo_restored_card_is_active_is_ignored(
    monkeypatch,
    focused: bool,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "undo-restored card should not be replaced by queue refresh"
        )

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        fail,
    )

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=456)
    reviewer.state = "question"
    reviewer._refresh_needed = None
    reviewer._rwkv_undo_restored_card_requires_queue_invalidation = True
    reviewer.nextCard = fail
    reviewer._prepare_rwkv_queue_order_then_next_card = fail
    reviewer.mw = SimpleNamespace(fade_in_webview=fail)

    changes = OpChanges()
    changes.study_queues = True
    dirty = reviewer.op_executed(changes, handler=None, focused=focused)

    assert reviewer.card.id == 456
    assert reviewer._refresh_needed is None
    assert dirty is False


def test_enter_on_rwkv_undo_restored_card_with_pending_refresh_shows_answer() -> None:
    calls: list[str] = []

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "pending queue refresh should not replace undo-restored card on Enter"
        )

    class Web:
        def evalWithCallback(
            self, script: str, callback: Callable[[str | None], None]
        ) -> None:
            calls.append(script)
            callback("")

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.web = Web()
    reviewer.card = SimpleNamespace(id=456)
    reviewer.state = "question"
    reviewer._refresh_needed = RefreshNeeded.QUEUES
    reviewer._question_update_id = 7
    reviewer._question_rendered = True
    reviewer._rwkv_undo_restored_card_requires_queue_invalidation = True
    reviewer._showAnswer = lambda: calls.append(f"answer:{reviewer.card.id}")
    reviewer.nextCard = fail
    reviewer._prepare_rwkv_queue_order_then_next_card = fail

    reviewer.onEnterKey()

    assert calls == ["getTypedAnswer();", "answer:456"]
    assert reviewer.card.id == 456
    assert reviewer._refresh_needed is RefreshNeeded.QUEUES


def test_rwkv_undo_stale_previous_front_cannot_trigger_show_answer() -> None:
    calls: list[str] = []

    class Web:
        def update(self) -> None:
            calls.append("update")

        def evalWithCallback(
            self, script: str, callback: Callable[[str | None], None]
        ) -> None:
            calls.append(script)
            callback("")

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.web = Web()
    reviewer.card = SimpleNamespace(id=123)
    reviewer.state = "question"
    reviewer._v3 = object()
    reviewer._question_update_id = 2
    reviewer._question_rendered = False
    reviewer._showAnswer = lambda: calls.append(f"answer:{reviewer.card.id}")

    reviewer._linkHandler("qaUpdated:question:1")
    reviewer._linkHandler("ans")

    assert calls == []

    reviewer._linkHandler("qaUpdated:question:2")
    reviewer._linkHandler("ans")

    assert calls == ["update", "getTypedAnswer();", "answer:123"]


def test_next_card_restores_rwkv_undone_card_before_normal_queue() -> None:
    calls: list[str] = []
    states = scheduling_states_with_review_current()

    class RestoredCard:
        id = 123
        nid = 456
        did = 1
        custom_data = "restored"
        started = False

        def current_deck_id(self) -> int:
            return self.did

        def _to_backend_card(self) -> cards_pb2.Card:
            return cards_pb2.Card(
                id=self.id,
                note_id=self.nid,
                deck_id=self.did,
                ctype=2,
                queue=2,
                custom_data=self.custom_data,
            )

        def start_timer(self) -> None:
            self.started = True
            calls.append("start")

    restored_card = RestoredCard()

    class Scheduler:
        def get_scheduling_states(
            self,
            card_id: int,
            *,
            desired_retention_override: float | None = None,
        ) -> SchedulingStates:
            assert card_id == restored_card.id
            assert desired_retention_override is None
            calls.append("states")
            return states

        def get_queued_cards(self) -> object:
            raise AssertionError("normal queue should not be used")

    class Decks:
        def name(self, deck_id: int, default: bool = False) -> str:
            assert deck_id == restored_card.did
            assert default is True
            return "Default"

    class Progress:
        def __init__(self) -> None:
            self.delay: int | None = None
            self.callback: Callable[[], None] | None = None

        def single_shot(self, delay: int, callback: Callable[[], None]) -> None:
            self.delay = delay
            self.callback = callback

    progress = Progress()
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=999)
    reviewer._v3 = None
    reviewer._scheduling_states_pending = False
    reviewer._desired_retention_override = None
    reviewer._reps = 1
    reviewer._previous_card_info = SimpleNamespace(
        set_card=lambda card: calls.append(f"previous:{card.id}")
    )
    reviewer._card_info = SimpleNamespace(
        set_card=lambda card: calls.append(f"current:{card.id}")
    )
    reviewer.mw = SimpleNamespace(
        col=SimpleNamespace(
            get_card=lambda card_id: restored_card,
            sched=Scheduler(),
            decks=Decks(),
        ),
        progress=progress,
        moveToState=lambda state: calls.append(f"state:{state}"),
    )

    def show_question() -> None:
        calls.append("question")
        reviewer.state = "question"
        reviewer._question_update_id = 1
        reviewer._question_rendered = True

    reviewer._showQuestion = show_question
    aqt.rwkv_scheduler.queue_reviewer_undo_card_ids(reviewer, [restored_card.id])
    reviewer.set_review_actions_blocked(True)

    reviewer.nextCard()

    assert reviewer.card is restored_card
    assert reviewer._rwkv_undo_restored_card_requires_queue_invalidation is True
    assert reviewer._v3.states is states
    assert reviewer._v3.context.deck_name == "Default"
    assert restored_card.started is True
    assert reviewer._review_actions_are_blocked() is False
    assert reviewer._answer_actions_are_blocked() is True
    assert progress.delay == reviewer_module.UNDO_RESTORED_CARD_ANSWER_UNBLOCK_DELAY_MS
    assert progress.callback is not None
    assert calls == ["states", "start", "previous:999", "current:123", "question"]

    reviewer.web = SimpleNamespace(
        evalWithCallback=lambda script, callback: callback("")
    )
    reviewer._showAnswer = lambda: calls.append("answer")
    reviewer._linkHandler("ans")

    assert calls[-1] == "answer"

    progress.callback()
    assert reviewer._answer_actions_are_blocked() is False

    reviewer._linkHandler("ans")

    assert calls[-1] == "answer"


def test_answer_rwkv_undo_restored_card_invalidates_queue_before_answer(
    monkeypatch,
) -> None:
    states = scheduling_states_with_review_current()
    calls: list[str] = []

    class Scheduler:
        def build_answer(
            self,
            *,
            card: object,
            states: SchedulingStates,
            rating: int,
            desired_retention_override: float | None = None,
        ) -> object:
            calls.append("build")
            return SimpleNamespace(new_state=states.good)

        def state_is_leech(self, new_state: object) -> bool:
            return False

    class Operation:
        def success(self, callback: Callable[[object], None]) -> object:
            return self

        def run_in_background(self, *, initiator: object) -> None:
            calls.append("run")

    def fake_answer_card(*, parent: object, answer: object) -> Operation:
        calls.append("answer")
        return Operation()

    def fake_invalidate(reviewer: object, card: object) -> None:
        calls.append(f"invalidate:{card.id}")

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123, custom_data="")
    reviewer.mw = SimpleNamespace(
        state="review", col=SimpleNamespace(sched=Scheduler())
    )
    reviewer.state = "answer"
    reviewer._answer_rendered = True
    reviewer._desired_retention_override = None
    reviewer._scheduling_states_pending = False
    reviewer._rwkv_undo_restored_card_requires_queue_invalidation = True
    reviewer._v3 = SimpleNamespace(
        states=states,
        rating_from_ease=lambda ease: ease,
    )

    monkeypatch.setattr("aqt.reviewer.answer_card", fake_answer_card)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "invalidate_reviewer_queue_for_card_answer",
        fake_invalidate,
    )

    reviewer._answerCard(3)

    assert calls == ["build", "invalidate:123", "answer", "run"]
    assert reviewer._rwkv_undo_restored_card_requires_queue_invalidation is False


def test_rwkv_queue_refresh_does_not_replace_card_after_generation_changes() -> None:
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = SimpleNamespace(id=123)
    reviewer.state = "question"
    reviewer._review_card_generation = 1

    assert reviewer._rwkv_queue_refresh_target_is_current(123, "question", 1)

    reviewer._review_card_generation = 2

    assert not reviewer._rwkv_queue_refresh_target_is_current(123, "question", 1)


def test_refresh_queues_without_rwkv_queue_order_prepares_before_next_card(
    monkeypatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "reviewer_queue_order_enabled",
        lambda reviewer: False,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_reviewer_queue_order",
        lambda reviewer: calls.append("prepare"),
    )

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = None
    reviewer._refresh_needed = RefreshNeeded.QUEUES
    reviewer.mw = SimpleNamespace(fade_in_webview=lambda: calls.append("fade"))
    reviewer.nextCard = lambda: calls.append("next")

    reviewer.refresh_if_needed()

    assert calls == ["prepare", "next", "fade"]
    assert reviewer._refresh_needed is None


def test_answer_card_updates_rwkv_state_used_by_other_card(
    monkeypatch,
) -> None:
    class Scheduler:
        def build_answer(
            self,
            *,
            card: object,
            states: SchedulingStates,
            rating: int,
            desired_retention_override: float | None = None,
        ) -> object:
            return SimpleNamespace(new_state=states.good)

        def state_is_leech(self, new_state: object) -> bool:
            return False

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return {"id": deck_id * 10, "rwkvReviewEnabled": True}

    class Operation:
        def __init__(self) -> None:
            self._callback: Callable[[object], None] | None = None

        def success(self, callback: Callable[[object], None]) -> object:
            self._callback = callback
            return self

        def run_in_background(self, *, initiator: object) -> None:
            assert self._callback is not None
            self._callback(SimpleNamespace())

    class RwkvRuntime:
        def review(
            self,
            *,
            review_input: aqt.rwkv_scheduler.RwkvReviewInput,
            card_state: object | None,
            note_state: object | None,
            deck_state: object | None,
            preset_state: object | None,
            global_state: object | None,
        ) -> aqt.rwkv_scheduler.RwkvReviewTransition:
            review_count = global_state if isinstance(global_state, int) else 0
            if review_input.is_query:
                return aqt.rwkv_scheduler.RwkvReviewTransition(
                    prediction=aqt.rwkv_scheduler.RwkvReviewPrediction(
                        retrievability=0.40 + 0.20 * review_count,
                        interval_overrides=aqt.rwkv_scheduler.RwkvIntervalOverride(
                            again=2 + review_count,
                            hard=3 + review_count,
                            good=5 + review_count,
                            easy=8 + review_count,
                        ),
                    )
                )

            return aqt.rwkv_scheduler.RwkvReviewTransition(
                card_state=("card", review_input.identity.card_id),
                note_state=("note", review_input.identity.note_id),
                deck_state=("deck", review_input.identity.deck_id),
                preset_state=("preset", review_input.identity.preset_id),
                global_state=review_count + 1,
            )

    def fake_answer_card(*, parent: object, answer: object) -> Operation:
        return Operation()

    previous_backend = aqt.rwkv_scheduler.set_reviewer_backend(
        aqt.rwkv_scheduler.RwkvStatefulReviewerBackend(RwkvRuntime())
    )
    states = SchedulingStates()
    states.current.normal.review.elapsed_days = 7
    states.good.normal.review.scheduled_days = 3

    card_a = SimpleNamespace(
        id=1,
        nid=10,
        did=100,
        queue=2,
        custom_data="",
        time_taken=lambda capped=True: 1234,
    )
    card_b = SimpleNamespace(id=2, nid=20, did=100)
    reviewer = Reviewer.__new__(Reviewer)
    reviewer.card = card_a
    reviewer.mw = SimpleNamespace(
        state="review",
        col=SimpleNamespace(sched=Scheduler(), decks=Decks()),
    )
    reviewer.state = "answer"
    reviewer._answer_rendered = True
    reviewer._desired_retention_override = None
    reviewer._scheduling_states_pending = False
    reviewer._answeredIds = []
    reviewer.check_timebox = lambda: True
    reviewer._v3 = SimpleNamespace(
        states=states,
        rating_from_ease=lambda ease: ease,
    )

    monkeypatch.setattr("aqt.reviewer.answer_card", fake_answer_card)

    try:
        before = aqt.rwkv_scheduler.update_reviewer_scheduling_states(
            states,
            reviewer,
            card_b,
        )
        reviewer._answerCard(3)
        after = aqt.rwkv_scheduler.update_reviewer_scheduling_states(
            states,
            reviewer,
            card_b,
        )
    finally:
        aqt.rwkv_scheduler.set_reviewer_backend(previous_backend)

    assert before is states
    assert after is states
    assert before.good.normal.review.scheduled_days == 3
    assert after.good.normal.review.scheduled_days == 3
    assert aqt.rwkv_scheduler.current_reviewer_retrievability(
        reviewer, card_b
    ) == pytest.approx(0.60)
    assert reviewer._answeredIds == [1]
