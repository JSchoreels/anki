# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from collections import deque
from unittest.mock import Mock

from tools.rwkv_candidate_refresh_backtest import (
    Accuracy,
    PartialQueue,
    Review,
    ReviewPool,
    measure_day,
)


def test_top_k_uses_all_decisions_including_ineligible_picks() -> None:
    accuracy = Accuracy()
    scores = {10: 0.1, 20: 0.2, 30: 0.3, 40: 0.4, 50: 0.5, 60: 0.6}
    ranks = {card: rank for rank, card in enumerate(scores, start=1)}
    for pick in (10, 20, 30, 40, 50, 60, None, 70):
        accuracy.observe(pick, ranks, scores)

    summary = accuracy.summary()
    assert summary["decisions"] == 8
    assert summary["no_eligible_pick"] == 2
    assert summary["top_k"] == {
        1: {"matches": 1, "percent": 12.5},
        2: {"matches": 2, "percent": 25.0},
        3: {"matches": 3, "percent": 37.5},
        4: {"matches": 4, "percent": 50.0},
        5: {"matches": 5, "percent": 62.5},
    }


def test_precomputed_reference_uses_the_last_completed_full_update() -> None:
    inference = Mock()
    inference.predict_retrievability_many_from_warm_up.side_effect = [
        [0.1, 0.2, 0.3],  # Before the first answer, card 1 is lowest.
        [0.3, 0.2, 0.1],  # After the first answer, card 3 is lowest.
        [0.2, 0.1, 0.3],  # After the second answer, card 2 is lowest.
    ]
    pool = ReviewPool(inference, 7, {1, 2, 3}, 0.85, True, 0, deque(maxlen=0))
    pool.last = {card: Review(card_id=card, interval_days=1) for card in (1, 2, 3)}
    rows = [
        Review(card_id=card, deck_id=7, review_id=card * 1000, interval_days=1, ease=3)
        for card in (1, 2, 3)
    ]
    queue = PartialQueue(window=3, baseline=False)

    measure_day(pool, rows, [queue], ascending=False, sizes=[])

    assert queue.before_refresh_vs_full_precomputed.ranks == [1, 1]
    assert queue.before_refresh.ranks == [3, 3]
    assert queue.after_refresh.ranks == [1, 1]
