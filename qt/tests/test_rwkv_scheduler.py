# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anki.scheduler.v3 import SchedulingState, SchedulingStates
from aqt import rwkv_scheduler
from aqt.rwkv_scheduler import (
    RwkvIntervalOverride,
    RwkvRecallPoint,
    RwkvReviewCandidate,
    RwkvReviewIdentity,
    RwkvReviewInput,
    RwkvReviewPrediction,
    RwkvReviewPredictionRequest,
    RwkvReviewTransition,
    RwkvStatefulReviewerBackend,
    RwkvWarmUpProgress,
    apply_review_interval_overrides,
    configure_reviewer_backend_from_environment,
    current_reviewer_diagnostics,
    current_reviewer_retrievability,
    interval_from_recall_curve,
    prepare_reviewer_queue_order,
    prepare_stats_retrievability_scores,
    record_collection_redo,
    record_collection_undo,
    record_reviewer_answer,
    rwkv_card_info_rows,
    rwkv_review_enabled,
    rwkv_review_identity,
    rwkv_review_input,
    set_reviewer_backend,
    update_reviewer_scheduling_states,
)


@pytest.fixture(autouse=True)
def reset_rwkv_reviewer_backend() -> Iterator[None]:
    previous = set_reviewer_backend(None)
    try:
        yield
    finally:
        set_reviewer_backend(previous)


def test_rwkv_queue_refresh_due_uses_nested_refresh_interval() -> None:
    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "other": {
                    "jschoreels.rwkv": {
                        "rwkv_review_enabled": True,
                        "rwkv_review_refresh_interval": 3,
                    }
                }
            }

    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())),
        card=SimpleNamespace(id=1, did=100),
        _answeredIds=[1, 2],
    )

    assert not rwkv_scheduler.reviewer_queue_order_refresh_due(reviewer)

    reviewer._answeredIds.append(3)

    assert rwkv_scheduler.reviewer_queue_order_refresh_due(reviewer)


def test_rwkv_queue_refresh_on_exit_uses_nested_config() -> None:
    class Decks:
        def get_current_id(self) -> int:
            return 100

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "reviewOrder": 7,
                "other": {
                    "jschoreels.rwkv": {
                        "rwkv_review_enabled": True,
                        "rwkv_review_refresh_on_exit": True,
                    }
                },
            }

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())))

    assert rwkv_scheduler.reviewer_queue_order_refresh_on_exit_enabled(reviewer)


def test_rwkv_queue_refresh_due_uses_direct_refresh_interval() -> None:
    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "rwkvReviewEnabled": True,
                "rwkvReviewRefreshInterval": 2,
            }

    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())),
        card=SimpleNamespace(id=1, did=100),
        _answeredIds=[1],
    )

    assert not rwkv_scheduler.reviewer_queue_order_refresh_due(reviewer)

    reviewer._answeredIds.append(2)

    assert rwkv_scheduler.reviewer_queue_order_refresh_due(reviewer)


def test_rwkv_queue_refresh_on_exit_uses_direct_config() -> None:
    class Decks:
        def get_current_id(self) -> int:
            return 100

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "reviewOrder": 7,
                "rwkvReviewEnabled": True,
                "rwkvReviewRefreshOnExit": True,
            }

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())))

    assert rwkv_scheduler.reviewer_queue_order_refresh_on_exit_enabled(reviewer)


def test_interval_from_recall_curve_interpolates_target() -> None:
    interval = interval_from_recall_curve(
        [
            RwkvRecallPoint(elapsed_days=6, retrievability=0.80),
            RwkvRecallPoint(elapsed_days=0, retrievability=0.95),
            RwkvRecallPoint(elapsed_days=2, retrievability=0.90),
        ],
        target_retention=0.86,
        max_interval_days=36500,
    )

    assert interval == 4


def test_interval_from_recall_curve_returns_none_when_target_not_reached() -> None:
    interval = interval_from_recall_curve(
        [
            RwkvRecallPoint(elapsed_days=0, retrievability=0.98),
            RwkvRecallPoint(elapsed_days=7, retrievability=0.93),
        ],
        target_retention=0.90,
        max_interval_days=36500,
    )

    assert interval is None


def test_interval_from_recall_curve_clamps_to_review_day_bounds() -> None:
    immediate_interval = interval_from_recall_curve(
        [RwkvRecallPoint(elapsed_days=0, retrievability=0.80)],
        target_retention=0.90,
        max_interval_days=36500,
    )
    max_interval = interval_from_recall_curve(
        [
            RwkvRecallPoint(elapsed_days=1, retrievability=0.95),
            RwkvRecallPoint(elapsed_days=100, retrievability=0.50),
        ],
        target_retention=0.90,
        max_interval_days=5,
    )

    assert immediate_interval == 1
    assert max_interval == 5


def test_interval_from_recall_curve_returns_none_for_nonmonotonic_curve() -> None:
    interval = interval_from_recall_curve(
        [
            RwkvRecallPoint(elapsed_days=0, retrievability=0.95),
            RwkvRecallPoint(elapsed_days=2, retrievability=0.90),
            RwkvRecallPoint(elapsed_days=5, retrievability=0.92),
        ],
        target_retention=0.91,
        max_interval_days=36500,
    )

    assert interval is None


@pytest.mark.parametrize(
    "points",
    [
        [RwkvRecallPoint(elapsed_days=math.inf, retrievability=0.90)],
        [RwkvRecallPoint(elapsed_days=-1, retrievability=0.90)],
        [RwkvRecallPoint(elapsed_days=1, retrievability=1.01)],
        [
            RwkvRecallPoint(elapsed_days=1, retrievability=0.90),
            RwkvRecallPoint(elapsed_days=1, retrievability=0.80),
        ],
    ],
)
def test_interval_from_recall_curve_rejects_invalid_points(
    points: list[RwkvRecallPoint],
) -> None:
    with pytest.raises(ValueError):
        interval_from_recall_curve(
            points,
            target_retention=0.90,
            max_interval_days=36500,
        )


@pytest.mark.parametrize("target_retention", [math.nan, -0.01, 1.01])
def test_interval_from_recall_curve_rejects_invalid_target(
    target_retention: float,
) -> None:
    with pytest.raises(ValueError):
        interval_from_recall_curve(
            [RwkvRecallPoint(elapsed_days=1, retrievability=0.90)],
            target_retention=target_retention,
            max_interval_days=36500,
        )


def test_apply_review_interval_overrides_changes_only_answer_review_states() -> None:
    states = SchedulingStates()
    states.current.CopyFrom(_normal_review_state(interval=9, fuzz_delta=7))
    states.again.CopyFrom(_normal_review_state(interval=1, fuzz_delta=1))
    states.hard.CopyFrom(_normal_review_state(interval=2, fuzz_delta=2))
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))
    states.easy.CopyFrom(_normal_review_state(interval=4, fuzz_delta=4))

    updated = apply_review_interval_overrides(
        states,
        RwkvIntervalOverride(again=10, hard=20, good=30, easy=40),
    )

    assert updated.current.normal.review.scheduled_days == 9
    assert updated.current.normal.review.fuzz_delta_days == 7
    assert updated.again.normal.review.scheduled_days == 10
    assert updated.hard.normal.review.scheduled_days == 20
    assert updated.good.normal.review.scheduled_days == 30
    assert updated.easy.normal.review.scheduled_days == 40
    assert updated.again.normal.review.fuzz_delta_days == 0
    assert updated.hard.normal.review.fuzz_delta_days == 0
    assert updated.good.normal.review.fuzz_delta_days == 0
    assert updated.easy.normal.review.fuzz_delta_days == 0

    assert states.again.normal.review.scheduled_days == 1
    assert states.hard.normal.review.scheduled_days == 2
    assert states.good.normal.review.scheduled_days == 3
    assert states.easy.normal.review.scheduled_days == 4


def test_apply_review_interval_overrides_skips_non_review_states() -> None:
    states = SchedulingStates()
    states.again.CopyFrom(_learning_state())
    states.hard.CopyFrom(_relearning_state())
    states.good.CopyFrom(_filtered_preview_state())
    states.easy.CopyFrom(_normal_review_state(interval=4, fuzz_delta=4))

    updated = apply_review_interval_overrides(
        states,
        RwkvIntervalOverride(again=10, hard=20, good=30, easy=40),
    )

    assert updated.again.normal.learning.scheduled_secs == 60
    assert updated.hard.normal.relearning.review.scheduled_days == 2
    assert updated.hard.normal.relearning.learning.scheduled_secs == 120
    assert updated.good.filtered.preview.scheduled_secs == 180
    assert updated.easy.normal.review.scheduled_days == 40
    assert updated.easy.normal.review.fuzz_delta_days == 0


def test_apply_review_interval_overrides_rejects_invalid_interval() -> None:
    with pytest.raises(ValueError):
        apply_review_interval_overrides(
            SchedulingStates(),
            RwkvIntervalOverride(good=0),
        )


def test_reviewer_rwkv_prediction_uses_reviews_of_other_cards() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    before = update_reviewer_scheduling_states(states, reviewer, card_b)
    record_reviewer_answer(reviewer, card_a, ease=3)
    after = update_reviewer_scheduling_states(states, reviewer, card_b)

    assert before.good.normal.review.scheduled_days == 5
    assert after.good.normal.review.scheduled_days == 6
    assert current_reviewer_retrievability(reviewer, card_b) == pytest.approx(0.55)
    diagnostics = current_reviewer_diagnostics(
        reviewer,
        card_b,
        fallback_source="FSRS",
    )
    assert diagnostics is not None
    assert diagnostics.retrievability == pytest.approx(0.55)
    assert diagnostics.retrievability_source == "RWKV"
    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=card_b,
        fallback_source="FSRS",
    ) == [
        ("RWKV computed R", "55%"),
        ("Retrievability source", "RWKV"),
    ]
    assert runtime.reviewed == [(1, 3)]
    assert runtime.queries == [(2, None, None), (2, 1, ("deck", 100, 1))]
    assert runtime.query_inputs[0].is_query is True
    assert runtime.query_inputs[0].ease is None
    assert runtime.query_inputs[0].duration_millis is None
    assert runtime.query_inputs[0].identity.preset_id == 1000
    assert runtime.query_inputs[0].day_offset == 42
    assert runtime.query_inputs[0].current_normal_state_kind == "review"
    assert runtime.query_inputs[0].current_elapsed_days == 7
    assert runtime.answered_inputs[0].is_query is False
    assert runtime.answered_inputs[0].ease == 3
    assert runtime.answered_inputs[0].duration_millis == 1234
    assert runtime.answered_inputs[0].reps == 5
    assert runtime.answered_inputs[0].lapses == 1
    assert states.good.normal.review.scheduled_days == 3


def test_stateful_reviewer_backend_batches_runtime_predictions() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.batch_requests: list[RwkvReviewPredictionRequest] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            self.batch_requests.extend(requests)
            return [
                RwkvReviewPrediction(
                    retrievability=0.10 * request.review_input.identity.card_id,
                    interval_overrides=RwkvIntervalOverride(
                        good=10 + request.review_input.identity.card_id
                    ),
                )
                for request in requests
            ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = _rwkv_reviewer()
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)
    card_c = _rwkv_card(card_id=3, note_id=30, duration_millis=6789)
    backend.review_answered(reviewer=reviewer, card=card_a, ease=3)

    predictions = backend.predict_reviews(
        [
            RwkvReviewCandidate(reviewer=reviewer, card=card_b),
            RwkvReviewCandidate(reviewer=reviewer, card=card_c),
        ]
    )

    assert [prediction.retrievability for prediction in predictions if prediction] == [
        pytest.approx(0.20),
        pytest.approx(0.30),
    ]
    assert [
        prediction.interval_overrides.good for prediction in predictions if prediction
    ] == [
        12,
        13,
    ]
    assert runtime.queries == []
    assert [
        (
            request.review_input.identity.card_id,
            request.review_input.is_query,
            request.global_state,
            request.deck_state,
        )
        for request in runtime.batch_requests
    ] == [
        (2, True, 1, ("deck", 100, 1)),
        (3, True, 1, ("deck", 100, 1)),
    ]


def test_stateful_reviewer_backend_caches_batch_query_predictions() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.batch_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            self.batch_card_ids.append(
                [request.review_input.identity.card_id for request in requests]
            )
            return [
                RwkvReviewPrediction(
                    retrievability=0.10 * request.review_input.identity.card_id,
                    interval_overrides=RwkvIntervalOverride(
                        good=10 + request.review_input.identity.card_id
                    ),
                )
                for request in requests
            ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = _rwkv_reviewer()
    candidates = [
        RwkvReviewCandidate(
            reviewer=reviewer,
            card=_rwkv_card(card_id=2, note_id=20, duration_millis=5678),
        ),
        RwkvReviewCandidate(
            reviewer=reviewer,
            card=_rwkv_card(card_id=3, note_id=30, duration_millis=6789),
        ),
    ]

    first = backend.predict_reviews(candidates)
    second = backend.predict_reviews(candidates)

    assert [prediction.retrievability for prediction in first if prediction] == [
        pytest.approx(0.20),
        pytest.approx(0.30),
    ]
    assert [prediction.retrievability for prediction in second if prediction] == [
        pytest.approx(0.20),
        pytest.approx(0.30),
    ]
    assert runtime.batch_card_ids == [[2, 3]]


def test_rwkv_review_scores_batches_only_prediction_cache_misses() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.batch_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            self.batch_card_ids.append(
                [request.review_input.identity.card_id for request in requests]
            )
            return [
                RwkvReviewPrediction(
                    retrievability=0.10 * request.review_input.identity.card_id,
                )
                for request in requests
            ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    card_a = _rwkv_card(card_id=2, note_id=20, duration_millis=2345)
    card_b = _rwkv_card(card_id=3, note_id=30, duration_millis=3456)
    card_c = _rwkv_card(card_id=4, note_id=40, duration_millis=4567)

    backend.predict_reviews(
        [
            RwkvReviewCandidate(reviewer=reviewer, card=card_a),
            RwkvReviewCandidate(reviewer=reviewer, card=card_c),
        ]
    )
    runtime.batch_card_ids.clear()

    scores = rwkv_scheduler._rwkv_review_scores_for_candidates(
        [
            RwkvReviewCandidate(reviewer=reviewer, card=card_a),
            RwkvReviewCandidate(reviewer=reviewer, card=card_b),
            RwkvReviewCandidate(reviewer=reviewer, card=card_c),
        ],
        batch_size=1,
    )

    assert scores == [
        (2, pytest.approx(0.20)),
        (3, pytest.approx(0.30)),
        (4, pytest.approx(0.40)),
    ]
    assert runtime.batch_card_ids == [[3]]


def test_stateful_reviewer_backend_clears_prediction_cache_after_answer() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = _rwkv_reviewer()
    reviewed_card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    queried_card = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)

    before = backend.predict_review(reviewer=reviewer, card=queried_card)
    cached_before = backend.predict_review(reviewer=reviewer, card=queried_card)
    backend.review_answered(reviewer=reviewer, card=reviewed_card, ease=3)
    after = backend.predict_review(reviewer=reviewer, card=queried_card)

    assert before is not None
    assert cached_before is not None
    assert after is not None
    assert before.retrievability == pytest.approx(0.45)
    assert cached_before.retrievability == pytest.approx(0.45)
    assert after.retrievability == pytest.approx(0.55)
    assert runtime.queries == [
        (2, None, None),
        (2, 1, ("deck", 100, 1)),
    ]


def test_reviewer_rwkv_warmup_replays_historical_reviews_once_before_prediction() -> (
    None
):
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(
        historical_review_rows=[
            (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
            (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
        ],
    )
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_b)
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_b)

    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert runtime.queries == [
        (1, None, None),
        (1, 1, ("deck", 100, 1)),
        (2, 2, ("deck", 100, 2)),
    ]
    assert current_reviewer_retrievability(reviewer, card_b) == pytest.approx(0.65)
    assert runtime.answered_inputs[0].day_offset == 40
    assert runtime.answered_inputs[0].current_elapsed_seconds == -1
    assert runtime.answered_inputs[1].current_elapsed_seconds == 90_000
    assert runtime.answered_inputs[1].current_elapsed_days == 1
    assert runtime.answered_inputs[1].card_type == 3
    assert runtime.answered_inputs[1].duration_millis == 2345


def test_reviewer_rwkv_warmup_uses_historical_interval_split_rules() -> None:
    first_review = (39 * 86_400 + 100) * 1000
    second_review = (40 * 86_400 + 100) * 1000
    third_review = (41 * 86_400 + 100) * 1000
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(
        resolved_preset_id="addon:test:current",
        historical_review_rows=[
            (first_review, 1, 10, 100, 2, 1234, 1, 20, 2500),
            (second_review, 1, 10, 100, 3, 2345, 1, 30, 2400),
            (third_review, 1, 10, 100, 4, 3456, 1, 40, 2300),
        ],
    )
    reviewer.mw.col.get_config = lambda key: {
        "simulator_rules": [
            {
                "preset_id": "addon:test:young",
                "max_interval_days": 20.0,
            },
            {
                "preset_id": "addon:test:mature",
                "min_interval_days": 21.0,
            },
        ],
    }

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    assert [item.identity.preset_id for item in runtime.answered_inputs] == [
        _expected_preset_hash("addon:test:young"),
        _expected_preset_hash("addon:test:young"),
        _expected_preset_hash("addon:test:mature"),
    ]


def test_reviewer_rwkv_prediction_skips_until_background_warmup_finishes() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(historical_review_rows=[])
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert updated.good.normal.review.scheduled_days == 3
    assert runtime.reviewed == []
    assert runtime.queries == []
    assert current_reviewer_retrievability(reviewer, card) is None


def test_reviewer_rwkv_answer_update_skips_until_background_warmup_finishes() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(historical_review_rows=[], rpc=rpc)
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    record_reviewer_answer(reviewer, card, ease=3)

    assert runtime.reviewed == []
    assert rpc.card_info_calls == []

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    record_reviewer_answer(reviewer, card, ease=3)

    assert runtime.reviewed == [(1, 3)]
    assert rpc.card_info_calls == [{"card_id": 1, "retrievability": None}]


def test_reviewer_rwkv_warmup_reports_review_progress() -> None:
    backend = RwkvStatefulReviewerBackend(_CacheRuntime())
    progress: list[RwkvWarmUpProgress] = []

    backend.warm_up(
        [
            _rwkv_review_input(card_id=1, note_id=10),
            _rwkv_review_input(card_id=2, note_id=20),
        ],
        progress=progress.append,
    )

    assert progress == [
        RwkvWarmUpProgress(processed_reviews=0, total_reviews=2),
        RwkvWarmUpProgress(processed_reviews=1, total_reviews=2),
        RwkvWarmUpProgress(processed_reviews=2, total_reviews=2),
    ]


def test_reviewer_rwkv_warmup_progress_label_includes_elapsed_and_remaining() -> None:
    label = rwkv_scheduler._rwkv_replay_progress_label(
        "Building RWKV state cache",
        RwkvWarmUpProgress(processed_reviews=2, total_reviews=4),
        elapsed_seconds=6,
    )

    assert (
        label == "Building RWKV state cache: 2/4 reviews | elapsed: 6s | remaining: 6s"
    )


def test_reviewer_rwkv_warmup_progress_label_formats_long_times() -> None:
    label = rwkv_scheduler._rwkv_replay_progress_label(
        "Building RWKV state cache",
        RwkvWarmUpProgress(processed_reviews=1, total_reviews=2),
        elapsed_seconds=3661,
    )

    assert (
        label == "Building RWKV state cache: 1/2 reviews | "
        "elapsed: 1h 01m 01s | remaining: 1h 01m 01s"
    )


def test_reviewer_rwkv_warmup_saves_and_reuses_local_state_cache(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert rwkv_scheduler.rwkv_state_cache_usable(reviewer.mw) is True
    assert (tmp_path / "rwkv-state-cache" / "snapshot-v1.bin").exists()
    assert (tmp_path / "rwkv-state-cache" / "deltas-v1.log").exists()

    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    assert restored_runtime.reviewed == []
    assert restored_runtime.restored_cache_states == [b"runtime-cache"]
    snapshot = rwkv_scheduler._reviewer_backend.cache_snapshot()
    assert snapshot.card_states[1] == b"card-1-3"
    assert snapshot.global_state == b"global-2"


def test_reviewer_rwkv_warmup_cache_replays_only_new_revlogs(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    snapshot_path = tmp_path / "rwkv-state-cache" / "snapshot-v1.bin"
    delta_path = tmp_path / "rwkv-state-cache" / "deltas-v1.log"
    snapshot_size = snapshot_path.stat().st_size
    delta_size = delta_path.stat().st_size

    rows.append((second_review, 1, 10, 100, 3, 2345, 2, 5, 2400))
    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    assert restored_runtime.reviewed == [(1, 3)]
    assert restored_runtime.answered_inputs[0].current_elapsed_seconds == 90_000
    assert snapshot_path.stat().st_size == snapshot_size
    assert delta_path.stat().st_size > delta_size

    delta_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(delta_runtime))

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    assert delta_runtime.reviewed == [(1, 3)]
    assert delta_runtime.answered_inputs[0].current_elapsed_seconds == 90_000


def test_rwkv_state_cache_build_uses_modal_progress(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )
    monkeypatch.setattr("aqt.utils.tooltip", lambda *args, **kwargs: None)

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    taskman, progress_updates = _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.build_rwkv_state_cache_with_progress(reviewer.mw)

    assert taskman.with_progress_kwargs is not None
    assert taskman.with_progress_kwargs["immediate"] is True
    assert taskman.with_progress_kwargs["uses_collection"] is True
    assert taskman.with_progress_kwargs["title"] == "RWKV State Cache"
    assert rwkv_scheduler.rwkv_state_cache_usable(reviewer.mw) is True
    assert any(
        update["value"] == 2
        and update["max"] == 2
        and str(update["label"]).startswith(
            "Building RWKV state cache: 2/2 reviews | elapsed: "
        )
        and str(update["label"]).endswith(" | remaining: 0s")
        for update in progress_updates
    )


def test_rwkv_state_cache_force_rebuild_replays_full_history(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    assert rwkv_scheduler.warm_up_rwkv_state(reviewer.mw) is True
    assert runtime.reviewed == [(1, 2), (1, 3)]

    runtime.reviewed.clear()
    assert rwkv_scheduler.warm_up_rwkv_state(reviewer.mw) is True
    assert runtime.reviewed == []

    assert (
        rwkv_scheduler.warm_up_rwkv_state(
            reviewer.mw,
            force_rebuild=True,
        )
        is True
    )

    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert runtime.restored_cache_states[-1] == b"runtime-cache"


def test_startup_loads_usable_rwkv_state_cache_with_progress(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))
    taskman, progress_updates = _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.prepare_rwkv_state_cache_on_startup(reviewer.mw)

    assert taskman.with_progress_kwargs is not None
    assert taskman.with_progress_kwargs["label"] == "Loading RWKV state cache..."
    assert taskman.with_progress_kwargs["immediate"] is True
    assert taskman.with_progress_kwargs["uses_collection"] is True
    assert taskman.with_progress_kwargs["title"] == "RWKV State Cache"
    assert restored_runtime.restored_cache_states == [b"runtime-cache"]
    assert restored_runtime.reviewed == []
    assert any(
        update["label"] == "Loading new RWKV reviews..." for update in progress_updates
    )


def test_reviewer_rwkv_undo_restores_previous_review_state() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    counter = _UndoCounter(reviewer)
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)
    card_c = _rwkv_card(card_id=3, note_id=30, duration_millis=6789)

    counter.set(1)
    record_reviewer_answer(reviewer, card_a, ease=3)
    counter.set(2)
    record_reviewer_answer(reviewer, card_b, ease=4)
    assert update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_c)
    assert current_reviewer_retrievability(reviewer, card_c) == pytest.approx(0.65)
    assert runtime.runtime_review_count == 2

    record_collection_undo(_undo_result(counter=2, next_counter=3))
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_c)
    assert current_reviewer_retrievability(reviewer, card_c) == pytest.approx(0.55)
    assert runtime.runtime_review_count == 1

    record_collection_undo(_undo_result(counter=1, next_counter=4))
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_c)
    assert current_reviewer_retrievability(reviewer, card_c) == pytest.approx(0.45)
    assert runtime.runtime_review_count == 0


def test_reviewer_rwkv_undo_does_not_clear_review_queue_scores() -> None:
    class Backend:
        def __init__(self) -> None:
            self.undone: list[tuple[int, int | None]] = []

        def answer_undone(self, counter: int, next_counter: int | None) -> bool:
            self.undone.append((counter, next_counter))
            return True

    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer()
    reviewer.mw.col._backend = rpc
    previous_backend = set_reviewer_backend(Backend())
    try:
        record_collection_undo(_undo_result(counter=2, next_counter=3))
    finally:
        backend = set_reviewer_backend(previous_backend)

    assert isinstance(backend, Backend)
    assert backend.undone == [(2, 3)]
    assert rpc.calls == []


def test_reviewer_rwkv_redo_reapplies_review_state_with_new_counter() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    counter = _UndoCounter(reviewer)
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)

    counter.set(1)
    record_reviewer_answer(reviewer, card_a, ease=3)
    record_collection_undo(_undo_result(counter=1, next_counter=2))
    record_collection_redo(_undo_result(counter=2, next_counter=3))

    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_b)
    assert current_reviewer_retrievability(reviewer, card_b) == pytest.approx(0.55)
    assert runtime.runtime_review_count == 1

    record_collection_undo(_undo_result(counter=3, next_counter=4))
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_b)
    assert current_reviewer_retrievability(reviewer, card_b) == pytest.approx(0.45)
    assert runtime.runtime_review_count == 0


def test_reviewer_rwkv_disabled_keeps_intervals_but_reports_diagnostics() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(rwkv_review_enabled=False)
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert rwkv_review_enabled(reviewer, card) is False
    assert updated.good.normal.review.scheduled_days == 3
    assert updated.good.normal.review.fuzz_delta_days == 3
    assert current_reviewer_retrievability(reviewer, card) == pytest.approx(0.45)
    diagnostics = current_reviewer_diagnostics(
        reviewer,
        card,
        fallback_source="FSRS",
    )
    assert diagnostics is not None
    assert diagnostics.retrievability_source == "FSRS (RWKV disabled)"
    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    ) == [
        ("RWKV computed R", "45%"),
        ("Retrievability source", "FSRS (RWKV disabled)"),
    ]


def test_rwkv_review_enabled_reads_legacy_fsrs_other_key() -> None:
    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1,
                "other": {
                    "jschoreels.fsrs": {
                        "rwkv_review_enabled": True,
                    },
                },
            }

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    assert rwkv_review_enabled(reviewer, card) is True


def test_rwkv_review_enabled_reads_top_level_rwkv_key() -> None:
    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1,
                "other": {},
                "jschoreels.rwkv": {
                    "rwkv_review_enabled": True,
                },
            }

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    assert rwkv_review_enabled(reviewer, card) is True


def test_reviewer_rwkv_enabled_without_interval_keeps_scheduler_interval() -> None:
    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(retrievability=0.62)

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            pass

    set_reviewer_backend(Backend())
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert rwkv_review_enabled(reviewer, card) is True
    assert updated.good.normal.review.scheduled_days == 3
    diagnostics = current_reviewer_diagnostics(
        reviewer,
        card,
        fallback_source="FSRS",
    )
    assert diagnostics is not None
    assert diagnostics.retrievability_source == "FSRS (RWKV interval unavailable)"


def test_prepare_reviewer_queue_order_scores_selected_deck_tree_reviews() -> None:
    class Backend:
        def __init__(self) -> None:
            self.predicted_card_ids: list[int] = []

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.predicted_card_ids.append(card.id)
            return RwkvReviewPrediction(retrievability={1: 0.80, 2: 0.20}[card.id])

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.predicted_card_ids == [1, 2]
    assert rpc.preset_id_calls == [[1, 2]]
    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    scores = rpc.calls[0]["scores"]
    assert isinstance(scores, list)
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [(1, pytest.approx(0.80)), (2, pytest.approx(0.20))]


def test_prepare_reviewer_queue_order_reuses_resolved_preset_ids() -> None:
    class Backend:
        def __init__(self) -> None:
            self.predicted_card_ids: list[int] = []

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.predicted_card_ids.append(card.id)
            return RwkvReviewPrediction(retrievability=0.80)

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.predicted_card_ids == [1, 2, 1, 2]
    assert rpc.preset_id_calls == [[1, 2]]


def test_rwkv_resolved_preset_cache_invalidates_selected_cards() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)

    assert rwkv_scheduler._resolved_fsrs_preset_ids(reviewer, [1, 2]) == {
        1: "1000",
        2: "1000",
    }
    assert rwkv_scheduler._resolved_fsrs_preset_ids(reviewer, [1, 2]) == {
        1: "1000",
        2: "1000",
    }

    rwkv_scheduler._invalidate_resolved_preset_id_cache(reviewer, card_ids=[1])

    assert rwkv_scheduler._resolved_fsrs_preset_ids(reviewer, [1, 2]) == {
        1: "1000",
        2: "1000",
    }
    assert rpc.preset_id_calls == [[1, 2], [1]]


def test_prepare_stats_retrievability_scores_ignores_review_order() -> None:
    class Backend:
        def __init__(self) -> None:
            self.predicted_card_ids: list[int] = []

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.predicted_card_ids.append(card.id)
            return RwkvReviewPrediction(
                retrievability={1: 0.75, 3: 0.25, 4: 0.55, 5: 0.71}[card.id]
            )

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            if deck_id == 100:
                return {"id": 1000, "rwkvReviewEnabled": True}
            if deck_id == 200:
                return {"id": 2000, "rwkvReviewEnabled": False}
            if deck_id == 300:
                return {
                    "id": 3000,
                    "other": {"jschoreels.fsrs": {"rwkv_review_enabled": True}},
                }
            if deck_id == 400:
                return {
                    "id": 4000,
                    "other": {"jschoreels.rwkv": {"rwkv_review_enabled": True}},
                }
            raise AssertionError(f"unexpected deck {deck_id}")

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

        def get_scheduling_states(self, card_id: int) -> SchedulingStates:
            raise AssertionError("stats graph scores should bulk-load card rows")

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            assert "from cards" in sql
            assert "id in (1,2,3,4,5)" in sql
            return [
                (1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, ""),
                (2, 20, 200, 0, 2, 2, 50, 0, 4, 2500, 5, 1, ""),
                (3, 30, 300, 0, 2, 2, 50, 0, 4, 2500, 5, 1, ""),
                (4, 40, 400, 0, 2, 2, 50, 0, 4, 2500, 5, 1, ""),
                (5, 50, 100, 0, 2, -1, 50, 0, 4, 2500, 5, 1, ""),
            ]

    class Collection:
        def __init__(self, rpc: _RwkvQueueScoreRpc) -> None:
            self._backend = rpc
            self.db = DB()
            self.decks = Decks()
            self.sched = Scheduler()

        def find_cards(self, search: str, order: bool = False) -> list[int]:
            assert search == "rated:7"
            assert order is False
            return [1, 2, 3, 4, 5]

        def get_card(self, card_id: int) -> SimpleNamespace:
            raise AssertionError("stats graph scores should bulk-load card rows")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.predicted_card_ids == [1, 3, 4, 5]
    assert rpc.preset_id_calls == [[1, 2, 3, 4, 5]]
    assert rpc.calls == []
    assert len(rpc.stats_calls) == 1
    assert rpc.stats_calls[0]["search"] == "rated:7"
    scores = rpc.stats_calls[0]["scores"]
    assert isinstance(scores, list)
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.75)),
        (3, pytest.approx(0.25)),
        (4, pytest.approx(0.55)),
        (5, pytest.approx(0.71)),
    ]


def test_prepare_stats_retrievability_scores_waits_for_pending_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Backend:
        def cache_snapshot(self) -> object:
            return object()

        def restore_cache_snapshot(self, snapshot: object) -> None:
            pass

        def predict_reviews(
            self,
            candidates: list[RwkvReviewCandidate],
        ) -> list[RwkvReviewPrediction]:
            return [RwkvReviewPrediction(retrievability=0.64) for _ in candidates]

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("stats graph scores should use batch prediction")

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {"id": 1000, "rwkvReviewEnabled": True}

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            assert "from cards" in sql
            assert "id in (1)" in sql
            return [(1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, "")]

    class Collection:
        def __init__(self, rpc: _RwkvQueueScoreRpc) -> None:
            self._backend = rpc
            self.db = DB()
            self.decks = Decks()
            self.sched = Scheduler()

        def find_cards(self, search: str, order: bool = False) -> list[int]:
            assert search == "rated:7"
            assert order is False
            return [1]

    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    previous_backend = set_reviewer_backend(Backend())
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    rwkv_scheduler._reviewer_backend_warmup_pending_keys.add(key)
    monkeypatch.setattr(rwkv_scheduler, "_RWKV_STATS_WARMUP_WAIT_TIMEOUT_SECS", 1.0)
    monkeypatch.setattr(rwkv_scheduler, "_RWKV_STATS_WARMUP_WAIT_INTERVAL_SECS", 0.001)

    def finish_warmup() -> None:
        time.sleep(0.01)
        rwkv_scheduler._reviewer_backend_warmup_pending_keys.discard(key)
        rwkv_scheduler._reviewer_backend_warmup_keys.add(key)

    thread = threading.Thread(target=finish_warmup)
    thread.start()
    try:
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        thread.join()
        set_reviewer_backend(previous_backend)

    assert len(rpc.stats_calls) == 1
    scores = rpc.stats_calls[0]["scores"]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.64)),
    ]


def test_prepare_reviewer_queue_order_batches_with_deck_option() -> None:
    class Backend:
        def __init__(self) -> None:
            self.batch_card_ids: list[list[int]] = []

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("single-card prediction should not be used")

        def predict_reviews(self, candidates) -> list[RwkvReviewPrediction]:
            self.batch_card_ids.append(
                [getattr(getattr(candidate, "card"), "id") for candidate in candidates]
            )
            return [
                RwkvReviewPrediction(retrievability=0.50) for candidate in candidates
            ]

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        batch_size=64,
        card_count=65,
        rwkv_config_in_other=True,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.batch_card_ids == [list(range(1, 65)), [65]]
    assert rpc.preset_id_calls == [list(range(1, 66))]
    scores = rpc.calls[0]["scores"]
    assert isinstance(scores, list)
    assert len(scores) == 65


def test_prepare_reviewer_queue_order_uses_per_card_scheduling_states() -> None:
    class Backend:
        def __init__(self) -> None:
            self.elapsed_by_card_id: dict[int, int | None] = {}

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            identity = rwkv_review_identity(reviewer, card)
            assert identity is not None
            review_input = rwkv_review_input(
                reviewer=reviewer,
                card=card,
                identity=identity,
                ease=None,
            )
            self.elapsed_by_card_id[card.id] = review_input.current_elapsed_days
            return RwkvReviewPrediction(retrievability=0.50)

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.elapsed_by_card_id == {1: 4, 2: 1}


def test_prepare_reviewer_queue_order_clears_scores_when_disabled() -> None:
    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("unexpected RWKV prediction")

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=0)
    previous_backend = set_reviewer_backend(Backend())
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    assert rpc.calls[0]["scores"] == []


def test_reviewer_rwkv_uses_resolved_fsrs_preset_for_card() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(resolved_preset_id="addon:test:medical")
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card)

    assert runtime.query_inputs[0].identity.preset_id == _expected_preset_hash(
        "addon:test:medical"
    )


def test_card_info_queries_rwkv_without_cached_reviewer_prediction() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(rpc=rpc)
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    ) == [
        ("RWKV computed R", "45%"),
        ("Retrievability source", "RWKV"),
    ]
    assert runtime.query_inputs[0].current_normal_state_kind == "review"
    assert runtime.query_inputs[0].current_elapsed_days == 7
    assert rpc.card_info_calls == [
        {"card_id": 1, "retrievability": pytest.approx(0.45)}
    ]


def test_card_info_restores_local_state_cache_before_query(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (second_review, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_model_cache_key",
        lambda: {"model": "test"},
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))

    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=_rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        fallback_source="FSRS",
    ) == [
        ("RWKV computed R", "45%"),
        ("Retrievability source", "FSRS (RWKV interval unavailable)"),
    ]
    assert restored_runtime.restored_cache_states == [b"runtime-cache"]
    assert restored_runtime.reviewed == []


def test_card_info_skips_rwkv_query_until_background_warmup_finishes() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(historical_review_rows=[])
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    ) == [
        ("RWKV computed R", "Unavailable"),
        ("Retrievability source", "FSRS (RWKV unavailable)"),
    ]
    assert runtime.queries == []


def test_card_info_configures_embedded_backend_for_rwkv_enabled_card(
    monkeypatch, tmp_path
) -> None:
    created: list[dict[str, object]] = []
    model_path = tmp_path / "rwkv.pth"
    model_path.write_bytes(b"model")

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=0.66,
                interval_overrides=RwkvIntervalOverride(good=4),
            )

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            pass

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: model_path,
    )
    monkeypatch.setattr(
        "aqt.rwkv_srs_benchmark.EmbeddedRwkvReviewerBackend",
        Backend,
    )

    assert rwkv_card_info_rows(
        reviewer=_rwkv_reviewer(),
        card=_rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        fallback_source="FSRS",
    ) == [
        ("RWKV computed R", "66%"),
        ("Retrievability source", "RWKV"),
    ]
    assert created == [
        {
            "model_path": model_path,
            "device": "cpu",
            "dtype": "float",
        }
    ]


def test_reviewer_rwkv_prediction_is_a_query_until_review_recorded() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = SimpleNamespace()
    card = SimpleNamespace(id=1)

    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card)
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card)

    assert current_reviewer_retrievability(reviewer, card) == pytest.approx(0.45)
    assert runtime.reviewed == []


def test_srs_benchmark_backend_builds_query_and_answer_rows() -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def item(self) -> float:
            return 0.72

    class Process:
        def __init__(self) -> None:
            self.query_rows: list[dict[str, object]] = []
            self.answer_rows: list[dict[str, object]] = []

        def imm_predict(self, row: dict[str, object]) -> Probability:
            self.query_rows.append(row)
            return Probability()

        def process_row(self, row: dict[str, object]) -> None:
            self.answer_rows.append(row)

    process = Process()
    backend = SrsBenchmarkRwkvReviewerBackend(process=process)
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    prediction = backend.predict_review(reviewer=reviewer, card=card)
    backend.review_answered(reviewer=reviewer, card=card, ease=3)

    assert prediction is not None
    assert prediction.retrievability == pytest.approx(0.72)
    assert process.query_rows == [
        {
            "card_id": 1,
            "note_id": 10,
            "deck_id": 100,
            "preset_id": 1000,
            "elapsed_days": 7,
            "elapsed_seconds": 604800,
            "day_offset": 42,
            "duration": 0.0,
            "state": 2,
            "rating": 1,
        }
    ]
    assert process.answer_rows[0]["duration"] == pytest.approx(1.234)
    assert process.answer_rows[0]["rating"] == 3


def test_srs_benchmark_backend_warmup_processes_historical_rows() -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def item(self) -> float:
            return 0.72

    class Process:
        def __init__(self) -> None:
            self.answer_rows: list[dict[str, object]] = []

        def imm_predict(self, row: dict[str, object]) -> Probability:
            return Probability()

        def process_row(self, row: dict[str, object]) -> object:
            self.answer_rows.append(row)
            return object()

    process = Process()
    backend = SrsBenchmarkRwkvReviewerBackend(process=process)
    backend.warm_up(
        [
            RwkvReviewInput(
                identity=RwkvReviewIdentity(
                    card_id=1,
                    note_id=10,
                    deck_id=100,
                    preset_id=1000,
                ),
                is_query=False,
                ease=3,
                duration_millis=1234,
                card_type=2,
                card_queue=2,
                card_due=None,
                interval_days=4,
                ease_factor=2500,
                reps=None,
                lapses=None,
                day_offset=42,
                current_state_kind="normal",
                current_normal_state_kind="review",
                current_elapsed_days=7,
                current_elapsed_seconds=604800,
            )
        ]
    )

    assert len(process.answer_rows) == 1
    assert process.answer_rows[0]["card_id"] == 1
    assert process.answer_rows[0]["rating"] == 3
    assert process.answer_rows[0]["duration"] == pytest.approx(1.234)


def test_srs_benchmark_backend_updates_other_card_retrievability() -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def __init__(self, value: float) -> None:
            self.value = value

        def item(self) -> float:
            return self.value

    class Process:
        def __init__(self) -> None:
            self.review_count = 0

        def imm_predict(self, row: dict[str, object]) -> Probability:
            return Probability(0.40 + 0.20 * self.review_count)

        def process_row(self, row: dict[str, object]) -> None:
            self.review_count += 1

    backend = SrsBenchmarkRwkvReviewerBackend(process=Process())
    reviewer = _rwkv_reviewer()
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=5678)

    before = backend.predict_review(reviewer=reviewer, card=card_b)
    backend.review_answered(reviewer=reviewer, card=card_a, ease=3)
    after = backend.predict_review(reviewer=reviewer, card=card_b)

    assert before is not None
    assert after is not None
    assert before.retrievability == pytest.approx(0.40)
    assert after.retrievability == pytest.approx(0.60)


def test_srs_benchmark_backend_uses_ahead_curve_for_good_interval() -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def __init__(self, value: float) -> None:
            self.value = value

        def item(self) -> float:
            return self.value

    class Process:
        def imm_predict(self, row: dict[str, object]) -> Probability:
            return Probability(0.80)

        def process_row(self, row: dict[str, object]) -> object:
            return object()

        def predict_func(self, curve: object, elapsed_seconds: int) -> Probability:
            elapsed_days = elapsed_seconds // 86_400
            return Probability(1.0 - elapsed_days * 0.025)

    backend = SrsBenchmarkRwkvReviewerBackend(
        process=Process(),
        target_retention=0.90,
        max_interval_days=30,
    )
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    before = backend.predict_review(reviewer=reviewer, card=card)
    backend.review_answered(reviewer=reviewer, card=card, ease=3)
    after = backend.predict_review(reviewer=reviewer, card=card)

    assert before is not None
    assert before.interval_overrides.good is None
    assert after is not None
    assert after.retrievability == pytest.approx(0.80)
    assert after.interval_overrides.good == 4


def test_srs_benchmark_backend_batches_predictions() -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def __init__(self, value: float) -> None:
            self.value = value

        def item(self) -> float:
            return self.value

    class Process:
        def __init__(self) -> None:
            self.rows: list[list[dict[str, object]]] = []

        def imm_predict(self, row: dict[str, object]) -> Probability:
            raise AssertionError("single-card prediction should not be used")

        def imm_predict_many(self, rows: list[dict[str, object]]) -> list[Probability]:
            self.rows.append(rows)
            return [Probability(0.10 * int(row["card_id"])) for row in rows]

        def process_row(self, row: dict[str, object]) -> object:
            return object()

        def predict_func(self, curve: object, elapsed_seconds: int) -> Probability:
            return Probability(0.80)

    process = Process()
    backend = SrsBenchmarkRwkvReviewerBackend(process=process)
    reviewer = _rwkv_reviewer()
    card_a = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card_b = _rwkv_card(card_id=2, note_id=20, duration_millis=2345)

    predictions = backend.predict_reviews(
        [
            SimpleNamespace(reviewer=reviewer, card=card_a),
            SimpleNamespace(reviewer=reviewer, card=card_b),
        ]
    )

    assert [prediction.retrievability for prediction in predictions if prediction] == [
        pytest.approx(0.10),
        pytest.approx(0.20),
    ]
    assert [[row["card_id"] for row in rows] for rows in process.rows] == [[1, 2]]


def test_embedded_rust_runtime_batches_bridge_predictions() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        def __init__(self) -> None:
            self.requests: list[list[tuple[object, ...]]] = []

        def predict_many(
            self,
            requests: list[tuple[object, ...]],
        ) -> list[tuple[float, int | None]]:
            self.requests.append(requests)
            return [(0.25, 7), (0.75, None)]

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process
    requests = [
        RwkvReviewPredictionRequest(
            review_input=_rwkv_review_input(card_id=1, note_id=10),
            card_state=b"card-1",
            note_state=b"note-10",
            deck_state=b"deck-100",
            preset_state=b"preset-1000",
            global_state=b"global",
        ),
        RwkvReviewPredictionRequest(
            review_input=_rwkv_review_input(card_id=2, note_id=20),
            card_state=b"card-2",
            note_state=b"note-20",
            deck_state=b"deck-100",
            preset_state=b"preset-1000",
            global_state=b"global",
        ),
    ]

    predictions = runtime.predict_many(requests)

    assert [prediction.retrievability for prediction in predictions if prediction] == [
        pytest.approx(0.25),
        pytest.approx(0.75),
    ]
    assert [
        prediction.interval_overrides.good for prediction in predictions if prediction
    ] == [
        7,
        None,
    ]
    assert process.requests == [
        [
            (
                1,
                10,
                100,
                1000,
                True,
                None,
                None,
                2,
                42,
                7,
                604800,
                b"card-1",
                b"note-10",
                b"deck-100",
                b"preset-1000",
                b"global",
            ),
            (
                2,
                20,
                100,
                1000,
                True,
                None,
                None,
                2,
                42,
                7,
                604800,
                b"card-2",
                b"note-20",
                b"deck-100",
                b"preset-1000",
                b"global",
            ),
        ]
    ]


def test_configure_reviewer_backend_uses_srs_benchmark_override(monkeypatch) -> None:
    created: list[dict[str, object]] = []

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(retrievability=0.61)

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            pass

    monkeypatch.setenv("ANKI_RWKV_BENCHMARK_PATH", "/tmp/srs-benchmark")
    monkeypatch.setenv("ANKI_RWKV_MODEL_PATH", "/tmp/rwkv.pth")
    monkeypatch.setenv("ANKI_RWKV_DEVICE", "cpu")
    monkeypatch.setenv("ANKI_RWKV_DTYPE", "float")
    monkeypatch.setattr(
        "aqt.rwkv_srs_benchmark.SrsBenchmarkRwkvReviewerBackend",
        Backend,
    )

    assert configure_reviewer_backend_from_environment() is True
    assert created == [
        {
            "benchmark_path": "/tmp/srs-benchmark",
            "model_path": "/tmp/rwkv.pth",
            "device": "cpu",
            "dtype": "float",
        }
    ]
    reviewer = SimpleNamespace()
    card = SimpleNamespace(id=1)
    update_reviewer_scheduling_states(
        SchedulingStates(),
        reviewer,
        card,
    )
    assert current_reviewer_retrievability(reviewer, card) == pytest.approx(0.61)


def test_configure_reviewer_backend_uses_embedded_default(
    monkeypatch, tmp_path
) -> None:
    created: list[dict[str, object]] = []
    model_path = tmp_path / "rwkv.pth"
    model_path.write_bytes(b"model")

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(retrievability=0.73)

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            pass

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setenv("ANKI_RWKV_DEVICE", "cpu")
    monkeypatch.setenv("ANKI_RWKV_DTYPE", "float")
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: model_path,
    )
    monkeypatch.setattr(
        "aqt.rwkv_srs_benchmark.EmbeddedRwkvReviewerBackend",
        Backend,
    )

    assert configure_reviewer_backend_from_environment() is True
    assert created == [
        {
            "model_path": model_path,
            "device": "cpu",
            "dtype": "float",
        }
    ]


def test_configure_reviewer_backend_treats_missing_torch_as_unavailable(
    monkeypatch, tmp_path, caplog
) -> None:
    model_path = tmp_path / "rwkv.pth"
    model_path.write_bytes(b"model")

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            raise ModuleNotFoundError("No module named 'torch'", name="torch")

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: model_path,
    )
    monkeypatch.setattr(
        "aqt.rwkv_srs_benchmark.EmbeddedRwkvReviewerBackend",
        Backend,
    )

    with caplog.at_level("ERROR", logger="aqt.rwkv_scheduler"):
        assert configure_reviewer_backend_from_environment() is False

    assert "failed to configure RWKV scheduler backend" not in caplog.text


def test_configure_reviewer_backend_uses_model_env_with_embedded_runner(
    monkeypatch,
) -> None:
    created: list[dict[str, object]] = []

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(retrievability=0.73)

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            pass

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.setenv("ANKI_RWKV_MODEL_PATH", "/tmp/custom-rwkv.pth")
    monkeypatch.setenv("ANKI_RWKV_DEVICE", "mps")
    monkeypatch.setenv("ANKI_RWKV_DTYPE", "bfloat16")
    monkeypatch.setattr(
        "aqt.rwkv_srs_benchmark.EmbeddedRwkvReviewerBackend",
        Backend,
    )

    assert configure_reviewer_backend_from_environment() is True
    assert created == [
        {
            "model_path": Path("/tmp/custom-rwkv.pth"),
            "device": "mps",
            "dtype": "bfloat16",
        }
    ]


class _SharedReviewRuntime:
    def __init__(self) -> None:
        self.reviewed: list[tuple[int, int]] = []
        self.queries: list[tuple[int, object | None, object | None]] = []
        self.query_inputs: list[RwkvReviewInput] = []
        self.answered_inputs: list[RwkvReviewInput] = []
        self.runtime_review_count = 0

    def review(
        self,
        *,
        review_input: RwkvReviewInput,
        card_state: object | None,
        note_state: object | None,
        deck_state: object | None,
        preset_state: object | None,
        global_state: object | None,
    ) -> RwkvReviewTransition:
        identity = review_input.identity
        review_count = global_state if isinstance(global_state, int) else 0
        ease = review_input.ease
        if ease is None:
            self.query_inputs.append(review_input)
            self.queries.append((identity.card_id, global_state, deck_state))
            return RwkvReviewTransition(
                prediction=RwkvReviewPrediction(
                    retrievability=0.45 + review_count * 0.10,
                    interval_overrides=RwkvIntervalOverride(good=5 + review_count),
                ),
            )

        self.answered_inputs.append(review_input)
        self.reviewed.append((identity.card_id, ease))
        self.runtime_review_count += 1
        return RwkvReviewTransition(
            card_state=("card", identity.card_id, ease),
            note_state=("note", identity.note_id, ease),
            deck_state=("deck", identity.deck_id, review_count + 1),
            preset_state=("preset", identity.preset_id, review_count + 1),
            global_state=review_count + 1,
        )

    def snapshot(self, review_input: RwkvReviewInput) -> object:
        return self.runtime_review_count

    def restore(self, state: object | None) -> None:
        self.runtime_review_count = state if isinstance(state, int) else 0


class _CacheRuntime:
    def __init__(self) -> None:
        self.reviewed: list[tuple[int, int]] = []
        self.answered_inputs: list[RwkvReviewInput] = []
        self.restored_cache_states: list[bytes] = []

    def review(
        self,
        *,
        review_input: RwkvReviewInput,
        card_state: object | None,
        note_state: object | None,
        deck_state: object | None,
        preset_state: object | None,
        global_state: object | None,
    ) -> RwkvReviewTransition:
        del card_state, note_state, deck_state, preset_state, global_state
        identity = review_input.identity
        ease = review_input.ease
        if ease is None:
            return RwkvReviewTransition(
                prediction=RwkvReviewPrediction(retrievability=0.45)
            )

        self.reviewed.append((identity.card_id, ease))
        self.answered_inputs.append(review_input)
        return RwkvReviewTransition(
            card_state=f"card-{identity.card_id}-{ease}".encode(),
            note_state=f"note-{identity.note_id}-{ease}".encode(),
            deck_state=f"deck-{identity.deck_id}-{ease}".encode(),
            preset_state=f"preset-{identity.preset_id}-{ease}".encode(),
            global_state=f"global-{len(self.reviewed)}".encode(),
        )

    def cache_state(self) -> bytes:
        return b"runtime-cache"

    def restore_cache_state(self, state: bytes) -> None:
        self.restored_cache_states.append(state)


class _UndoCounter:
    def __init__(self, reviewer: SimpleNamespace) -> None:
        self.value = 0
        reviewer.mw.col.undo_status = self.undo_status

    def set(self, value: int) -> None:
        self.value = value

    def undo_status(self) -> SimpleNamespace:
        return SimpleNamespace(last_step=self.value)


def _undo_result(*, counter: int, next_counter: int) -> SimpleNamespace:
    return SimpleNamespace(
        counter=counter,
        new_status=SimpleNamespace(last_step=next_counter),
    )


class _RwkvQueueScoreRpc:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.stats_calls: list[dict[str, object]] = []
        self.card_info_calls: list[dict[str, object]] = []
        self.preset_id_calls: list[list[int]] = []

    def set_rwkv_review_queue_scores(
        self,
        *,
        deck_id: int,
        scores: list[object],
    ) -> None:
        self.calls.append({"deck_id": deck_id, "scores": scores})

    def set_rwkv_stats_graph_scores(
        self,
        *,
        search: str,
        scores: list[object],
    ) -> None:
        self.stats_calls.append({"search": search, "scores": scores})

    def set_rwkv_card_info_score(self, message: Any) -> None:
        self.card_info_calls.append(
            {
                "card_id": getattr(message, "card_id"),
                "retrievability": (
                    getattr(message, "retrievability")
                    if message.HasField("retrievability")
                    else None
                ),
            }
        )

    def get_fsrs_preset_ids_for_cards(self, cids: list[int]) -> SimpleNamespace:
        self.preset_id_calls.append(list(cids))
        return SimpleNamespace(
            items=[
                SimpleNamespace(card_id=card_id, preset_id="1000") for card_id in cids
            ]
        )


def _rwkv_queue_reviewer(
    *,
    rpc: _RwkvQueueScoreRpc,
    review_order: int,
    batch_size: int | None = None,
    card_count: int = 2,
    rwkv_config_in_other: bool = False,
) -> SimpleNamespace:
    cards = {
        card_id: _rwkv_card(
            card_id=card_id,
            note_id=card_id * 10,
            duration_millis=1234,
        )
        for card_id in range(1, card_count + 1)
    }
    if 1 in cards:
        cards[1].due = 42
    if 2 in cards:
        cards[2].due = 45

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            assert "did in (100,101)" in sql
            assert "queue = ?" in sql
            assert args == (2,)
            return list(cards)

        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            assert "from cards" in sql
            assert f"id in ({','.join(str(card_id) for card_id in cards)})" in sql
            return [
                (
                    card.id,
                    card.nid,
                    card.did,
                    0,
                    card.type,
                    card.queue,
                    card.due,
                    0,
                    card.ivl,
                    card.factor,
                    card.reps,
                    card.lapses,
                    "",
                )
                for card in cards.values()
            ]

    class Decks:
        def get_current_id(self) -> int:
            return 100

        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            assert deck_id == 100
            return [100, 101]

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            config: dict[str, object] = {
                "id": deck_id * 10,
                "reviewOrder": review_order,
            }
            if rwkv_config_in_other:
                nested: dict[str, object] = {"rwkv_review_enabled": True}
                if batch_size is not None:
                    nested["rwkv_review_batch_size"] = batch_size
                config["other"] = {"jschoreels.rwkv": nested}
            else:
                config["rwkvReviewEnabled"] = True
                if batch_size is not None:
                    config["rwkvReviewBatchSize"] = batch_size
            return config

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

        def get_scheduling_states(self, card_id: int) -> SchedulingStates:
            raise AssertionError("queue scores should bulk-load card rows")

    col = SimpleNamespace(
        _backend=rpc,
        db=DB(),
        decks=Decks(),
        sched=Scheduler(),
        get_card=lambda card_id: (_ for _ in ()).throw(
            AssertionError("queue scores should bulk-load card rows")
        ),
    )
    return SimpleNamespace(mw=SimpleNamespace(col=col))


def _rwkv_reviewer(
    *,
    rwkv_review_enabled: bool = True,
    resolved_preset_id: str | None = "1000",
    rpc: _RwkvQueueScoreRpc | None = None,
    historical_review_rows: (
        list[tuple[int, int, int, int, int, int, int, int, int]] | None
    ) = None,
) -> SimpleNamespace:
    states = SchedulingStates()
    states.current.normal.review.elapsed_days = 7

    class Scheduler:
        def __init__(self) -> None:
            self.states = states

        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

        def get_scheduling_states(self, card_id: int) -> SchedulingStates:
            return self.states

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return {
                "id": deck_id * 10,
                "rwkvReviewEnabled": rwkv_review_enabled,
            }

    col = SimpleNamespace(
        sched=Scheduler(),
        decks=Decks(),
    )
    if rpc is not None:
        col._backend = rpc
    if historical_review_rows is not None:

        class DB:
            def all(self, sql: str, *args: object) -> list[tuple[int, ...]]:
                assert "from revlog r" in sql
                assert "join cards c" in sql
                assert args == ()
                return historical_review_rows

            def execute(self, sql: str) -> None:
                assert "CREATE TABLE IF NOT EXISTS" in sql

            def executemany(
                self,
                sql: str,
                rows: list[tuple[int, float, str, int]],
            ) -> None:
                assert "INSERT OR REPLACE" in sql
                assert len(rows) == len(historical_review_rows)

        col.db = DB()
    if resolved_preset_id is not None:
        col.fsrs_preset_for_card = lambda card_id: SimpleNamespace(
            id=resolved_preset_id
        )

    return SimpleNamespace(
        _v3=SimpleNamespace(states=states),
        mw=SimpleNamespace(col=col),
    )


def _rwkv_cache_reviewer(
    *,
    profile_folder: Path,
    rows: list[tuple[int, int, int, int, int, int, int, int, int]],
) -> SimpleNamespace:
    states = SchedulingStates()

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[int, ...]]:
            assert "from revlog r" in sql
            assert "join cards c" in sql
            if args:
                assert len(args) == 1
                after_review_id = args[0]
                assert isinstance(after_review_id, int)
                return [row for row in rows if row[0] > after_review_id]
            return list(rows)

        def scalar(self, sql: str, *args: object) -> int:
            if "select crt from col" in sql:
                assert args == ()
                return 12345
            assert "from revlog" in sql
            assert len(args) == 1
            last_review_id = args[0]
            assert isinstance(last_review_id, int)
            return sum(1 for row in rows if row[0] <= last_review_id)

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

        def get_scheduling_states(self, card_id: int) -> SchedulingStates:
            return states

    class Decks:
        def all_config(self) -> list[dict[str, object]]:
            return [{"rwkvReviewEnabled": True}]

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return {"id": deck_id * 10, "rwkvReviewEnabled": True}

    col = SimpleNamespace(
        db=DB(),
        sched=Scheduler(),
        decks=Decks(),
        path=str(profile_folder / "collection.anki2"),
        fsrs_preset_for_card=lambda card_id: SimpleNamespace(id="1000"),
    )
    mw = SimpleNamespace(
        col=col,
        pm=SimpleNamespace(profileFolder=lambda: str(profile_folder)),
    )
    return SimpleNamespace(_v3=SimpleNamespace(states=states), mw=mw)


def _attach_progress_taskman(
    mw: SimpleNamespace,
) -> tuple[Any, list[dict[str, object]]]:
    progress_updates: list[dict[str, object]] = []

    class Progress:
        def update(self, **kwargs: object) -> None:
            progress_updates.append(kwargs)

    class Taskman:
        def __init__(self) -> None:
            self.with_progress_kwargs: dict[str, object] | None = None

        def run_on_main(self, callback: object) -> None:
            assert callable(callback)
            callback()

        def with_progress(
            self,
            task: object,
            on_done: object,
            **kwargs: object,
        ) -> None:
            assert callable(task)
            assert callable(on_done)
            self.with_progress_kwargs = kwargs
            future: Future[bool] = Future()
            try:
                future.set_result(task())
            except Exception as exc:
                future.set_exception(exc)
            on_done(future)

    taskman = Taskman()
    mw.taskman = taskman
    mw.progress = Progress()
    mw.inMainThread = lambda: True
    return taskman, progress_updates


def _expected_preset_hash(preset_id: str) -> int:
    digest = hashlib.blake2b(preset_id.encode("utf8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def _rwkv_card(
    *,
    card_id: int,
    note_id: int,
    duration_millis: int,
    deck_id: int = 100,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=card_id,
        nid=note_id,
        did=deck_id,
        type=2,
        queue=2,
        due=50,
        ivl=4,
        factor=2500,
        reps=5,
        lapses=1,
        time_taken=lambda capped=True: duration_millis,
    )


def _rwkv_review_input(*, card_id: int, note_id: int) -> RwkvReviewInput:
    return RwkvReviewInput(
        identity=RwkvReviewIdentity(
            card_id=card_id,
            note_id=note_id,
            deck_id=100,
            preset_id=1000,
        ),
        is_query=True,
        ease=None,
        duration_millis=None,
        card_type=2,
        card_queue=2,
        card_due=50,
        interval_days=4,
        ease_factor=2500,
        reps=5,
        lapses=1,
        day_offset=42,
        current_state_kind="normal",
        current_normal_state_kind="review",
        current_elapsed_days=7,
        current_elapsed_seconds=604800,
    )


def _normal_review_state(interval: int, fuzz_delta: int) -> SchedulingState:
    state = SchedulingState()
    state.normal.review.scheduled_days = interval
    state.normal.review.fuzz_delta_days = fuzz_delta
    return state


def _learning_state() -> SchedulingState:
    state = SchedulingState()
    state.normal.learning.scheduled_secs = 60
    return state


def _relearning_state() -> SchedulingState:
    state = SchedulingState()
    state.normal.relearning.review.scheduled_days = 2
    state.normal.relearning.learning.scheduled_secs = 120
    return state


def _filtered_preview_state() -> SchedulingState:
    state = SchedulingState()
    state.filtered.preview.scheduled_secs = 180
    return state
