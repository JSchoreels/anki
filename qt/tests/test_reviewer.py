# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from types import SimpleNamespace

from aqt.reviewer import Reviewer


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
