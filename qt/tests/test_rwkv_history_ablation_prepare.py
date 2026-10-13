# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from tools.rwkv_history_ablation_prepare import reset_rows

from aqt.rwkv_scheduler import _benchmark_retained_historical_review_rows


def test_reset_comparison_retains_same_post_reset_answers_and_other_cards() -> None:
    # Two resets on card 1: the latest boundary removes both older sequences.
    # Card 2 has no reset; card 3 has no answer after its reset yet.
    rows = [
        (review_id, card_id, 1, 1, 3, 1000, kind, 1, 2500)
        for review_id, card_id, kind in [
            (10, 1, 0),
            (20, 2, 0),
            (30, 1, 1),
            (40, 3, 1),
            (50, 1, 0),
            (60, 2, 1),
            (70, 1, 0),
        ]
    ]
    resets = {1: 45, 3: 80}

    complete, complete_cutoffs = reset_rows(rows, resets, keep_previous=True)
    fresh, fresh_cutoffs = reset_rows(rows, resets, keep_previous=False)

    # The production feature adapter must keep all selected rows instead of
    # applying its usual latest-Learning-sequence filter a second time.
    complete_inputs = _benchmark_retained_historical_review_rows(
        complete, preserved_learning_start_cutoffs=complete_cutoffs
    )
    fresh_inputs = _benchmark_retained_historical_review_rows(
        fresh, preserved_learning_start_cutoffs=fresh_cutoffs
    )
    assert [(row[0], state) for row, state in complete_inputs] == [
        (10, 0),
        (20, 0),
        (30, 2),
        (40, 2),
        (50, 1),
        (60, 2),
        (70, 1),
    ]
    assert [(row[0], state) for row, state in fresh_inputs] == [
        (20, 0),
        (50, 0),
        (60, 2),
        (70, 1),
    ]
