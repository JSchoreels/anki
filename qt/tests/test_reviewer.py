# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

import aqt.rwkv_scheduler
from aqt.reviewer import Reviewer, SchedulingStates


def scheduling_states_with_review_current() -> SchedulingStates:
    states = SchedulingStates()
    states.current.normal.review.scheduled_days = 1
    states.good.normal.review.scheduled_days = 1
    return states


def test_typed_answer_callback_ignored_after_scheduler_state_cleared() -> None:
    class Card:
        def answer(self) -> str:
            raise AssertionError("stale callback should not render the answer")

    reviewer = Reviewer.__new__(Reviewer)
    reviewer.mw = SimpleNamespace(state="review")
    reviewer.card = Card()
    reviewer._v3 = None

    reviewer._onTypedAnswer("typed")

    assert reviewer.typedAnswer == "typed"


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
                            good=5 + review_count
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

    assert before.good.normal.review.scheduled_days == 5
    assert after.good.normal.review.scheduled_days == 6
    assert aqt.rwkv_scheduler.current_reviewer_retrievability(
        reviewer, card_b
    ) == pytest.approx(0.60)
    assert reviewer._answeredIds == [1]
