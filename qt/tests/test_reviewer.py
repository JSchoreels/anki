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
