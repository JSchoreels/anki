# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

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
