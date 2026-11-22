# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anki.scheduler.v3 import QueuedCards


@pytest.fixture(autouse=True)
def stub_translations(monkeypatch: pytest.MonkeyPatch) -> None:
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        tr,
        "decks_rwkv_review_potential_tooltip",
        lambda *, count, total: f'{count} due now; "up to" {total}.',
    )
    monkeypatch.setattr(
        tr,
        "decks_rwkv_review_waiting_tooltip",
        lambda *, count: f"{count} waiting.",
    )


def overview_table(
    queued: QueuedCards, *, counts_pending: bool = False, due_counts: bool = True
) -> str:
    from aqt.overview import Overview

    overview = Overview.__new__(Overview)
    overview._rwkv_counts_pending = counts_pending
    overview.mw = SimpleNamespace(
        button=lambda *args, **kwargs: "",
        col=SimpleNamespace(
            conf={"dueCounts": due_counts},
            v3_scheduler=lambda: True,
            decks=SimpleNamespace(get_current_id=lambda: 1),
            sched=SimpleNamespace(
                get_queued_cards_without_states=lambda *, fetch_limit: queued,
                deck_due_tree=lambda _deck_id: SimpleNamespace(
                    new_count=queued.new_count,
                    learn_count=queued.learning_count,
                    review_count=queued.review_count,
                ),
            ),
        ),
    )
    return overview._table()


def test_overview_shows_rwkv_review_potential_with_tooltip() -> None:
    table = overview_table(
        QueuedCards(
            review_count=50, rwkv_review_headroom=30, rwkv_waiting_review_count=2
        )
    )

    assert (
        "<span class=review-count>50</span><span class=review-limit "
        'title="50 due now; &quot;up to&quot; 80. 2 waiting."> (≤80)</span>'
    ) in table


@pytest.mark.parametrize(
    ("headroom", "counts_pending", "due_counts"),
    [(None, False, True), (0, False, True), (30, True, True), (30, False, False)],
)
def test_overview_hides_rwkv_review_potential(
    headroom: int | None, counts_pending: bool, due_counts: bool
) -> None:
    table = overview_table(
        QueuedCards(review_count=50, rwkv_review_headroom=headroom),
        counts_pending=counts_pending,
        due_counts=due_counts,
    )

    assert "review-limit" not in table
    assert "(/" not in table
