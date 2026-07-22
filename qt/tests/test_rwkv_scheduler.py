# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from anki import cards_pb2, scheduler_pb2
from anki.scheduler.v3 import SchedulingState, SchedulingStates
from aqt import rwkv_scheduler
from aqt.rwkv_scheduler import (
    RwkvBackendCacheSnapshot,
    RwkvIntervalOverride,
    RwkvRecallPoint,
    RwkvReviewCandidate,
    RwkvReviewerPrediction,
    RwkvReviewerStateSnapshot,
    RwkvReviewIdentity,
    RwkvReviewInput,
    RwkvReviewPrediction,
    RwkvReviewPredictionRequest,
    RwkvReviewState,
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
    prewarm_reviewer_queue_score_cache,
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
from aqt.rwkv_srs_benchmark import (
    _rust_warmup_chunk_size,
    _workload_snapshot_for_review_inputs,
)

RWKV_AFTER_REVIEW_UNAVAILABLE_ROW = (
    "RWKV : R After Review",
    "Again:Unavailable Hard:Unavailable Good:Unavailable Easy:Unavailable",
)
RWKV_AFTER_TEN_MINUTES_UNAVAILABLE_ROW = (
    "RWKV : R After 10min",
    "Again:Unavailable Hard:Unavailable Good:Unavailable Easy:Unavailable",
)
RWKV_AFTER_REVIEW_UNAVAILABLE_ROWS = [
    RWKV_AFTER_REVIEW_UNAVAILABLE_ROW,
    RWKV_AFTER_TEN_MINUTES_UNAVAILABLE_ROW,
]
NEXT_S90_UNAVAILABLE_ROWS = [
    (
        "RWKV Curve Next S90",
        "Again:Unavailable Hard:Unavailable Good:Unavailable Easy:Unavailable",
    ),
    (
        "FSRS Next S90",
        "Again:Unavailable Hard:Unavailable Good:Unavailable Easy:Unavailable",
    ),
]
RWKV_BUTTON_PROBABILITY_ROW = (
    "RWKV : Answer Button Probability",
    "Again:55% Hard:10% Good:20% Easy:15%",
)


@pytest.fixture(autouse=True)
def reset_rwkv_reviewer_backend() -> Iterator[None]:
    previous = set_reviewer_backend(None)
    previous_warmup_keys = set(rwkv_scheduler._reviewer_backend_warmup_keys)
    previous_pending_keys = set(rwkv_scheduler._reviewer_backend_warmup_pending_keys)
    previous_preset_cache = dict(rwkv_scheduler._resolved_preset_id_cache)
    previous_queue_score_maps = dict(rwkv_scheduler._rwkv_review_queue_score_maps)
    previous_queue_target_maps = dict(rwkv_scheduler._rwkv_review_queue_target_maps)
    previous_queue_score_generations = dict(
        rwkv_scheduler._rwkv_review_queue_score_generations
    )
    previous_queue_score_config_keys = dict(
        rwkv_scheduler._rwkv_review_queue_score_config_keys
    )
    previous_queue_collection_key = rwkv_scheduler._rwkv_review_queue_collection_key
    previous_dynamic_dr_generation = (
        rwkv_scheduler._dynamic_desired_retention_generation
    )
    previous_study_queue_generation = rwkv_scheduler._rwkv_study_queue_generation
    previous_input_batch_cache = (
        rwkv_scheduler._rwkv_review_input_batch_module_cache.copy()
    )
    previous_stats_prepare = dict(rwkv_scheduler._rwkv_stats_prepare_in_flight)
    previous_score_prewarm = set(rwkv_scheduler._rwkv_score_prewarm_in_flight)
    previous_workload_job = rwkv_scheduler._rwkv_workload_job
    previous_memorised_job = rwkv_scheduler._rwkv_memorised_history_job
    previous_startup_prompt_shown = rwkv_scheduler._rwkv_startup_prompt_shown
    rwkv_scheduler._reviewer_backend_warmup_keys.clear()
    rwkv_scheduler._reviewer_backend_warmup_pending_keys.clear()
    rwkv_scheduler._resolved_preset_id_cache.clear()
    rwkv_scheduler._rwkv_review_queue_score_maps.clear()
    rwkv_scheduler._rwkv_review_queue_target_maps.clear()
    rwkv_scheduler._rwkv_review_queue_score_generations.clear()
    rwkv_scheduler._rwkv_review_queue_score_config_keys.clear()
    rwkv_scheduler._rwkv_review_queue_collection_key = None
    rwkv_scheduler._dynamic_desired_retention_generation = 0
    rwkv_scheduler._rwkv_study_queue_generation = 0
    rwkv_scheduler._rwkv_review_input_batch_module_cache.clear()
    rwkv_scheduler._rwkv_stats_prepare_in_flight.clear()
    rwkv_scheduler._rwkv_score_prewarm_in_flight.clear()
    rwkv_scheduler._rwkv_startup_prompt_shown = False
    rwkv_scheduler.cancel_rwkv_workload()
    rwkv_scheduler._rwkv_workload_job = None
    rwkv_scheduler._rwkv_memorised_history_job = None
    try:
        yield
    finally:
        rwkv_scheduler.cancel_rwkv_workload()
        rwkv_scheduler._rwkv_workload_job = previous_workload_job
        rwkv_scheduler._rwkv_memorised_history_job = previous_memorised_job
        set_reviewer_backend(previous)
        rwkv_scheduler._reviewer_backend_warmup_keys.clear()
        rwkv_scheduler._reviewer_backend_warmup_keys.update(previous_warmup_keys)
        rwkv_scheduler._reviewer_backend_warmup_pending_keys.clear()
        rwkv_scheduler._reviewer_backend_warmup_pending_keys.update(
            previous_pending_keys
        )
        rwkv_scheduler._resolved_preset_id_cache.clear()
        rwkv_scheduler._resolved_preset_id_cache.update(previous_preset_cache)
        rwkv_scheduler._rwkv_review_queue_score_maps.clear()
        rwkv_scheduler._rwkv_review_queue_score_maps.update(previous_queue_score_maps)
        rwkv_scheduler._rwkv_review_queue_target_maps.clear()
        rwkv_scheduler._rwkv_review_queue_target_maps.update(previous_queue_target_maps)
        rwkv_scheduler._rwkv_review_queue_score_generations.clear()
        rwkv_scheduler._rwkv_review_queue_score_generations.update(
            previous_queue_score_generations
        )
        rwkv_scheduler._rwkv_review_queue_score_config_keys.clear()
        rwkv_scheduler._rwkv_review_queue_score_config_keys.update(
            previous_queue_score_config_keys
        )
        rwkv_scheduler._rwkv_review_queue_collection_key = previous_queue_collection_key
        rwkv_scheduler._dynamic_desired_retention_generation = (
            previous_dynamic_dr_generation
        )
        rwkv_scheduler._rwkv_study_queue_generation = previous_study_queue_generation
        rwkv_scheduler._rwkv_review_input_batch_module_cache.clear()
        rwkv_scheduler._rwkv_review_input_batch_module_cache.update(
            previous_input_batch_cache
        )
        rwkv_scheduler._rwkv_stats_prepare_in_flight.clear()
        rwkv_scheduler._rwkv_stats_prepare_in_flight.update(previous_stats_prepare)
        rwkv_scheduler._rwkv_score_prewarm_in_flight.clear()
        rwkv_scheduler._rwkv_score_prewarm_in_flight.update(previous_score_prewarm)
        rwkv_scheduler._rwkv_startup_prompt_shown = previous_startup_prompt_shown


def test_rwkv_queue_refresh_due_uses_nested_refresh_interval() -> None:
    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "reviewOrder": 7,
                "other": {
                    "jschoreels.rwkv": {
                        "rwkv_review_enabled": True,
                        "rwkv_review_instant_order_enabled": True,
                        "rwkv_review_refresh_interval": 3,
                    }
                },
            }

    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())),
        card=SimpleNamespace(id=1, did=100),
        _answeredIds=[1, 2],
    )

    assert not rwkv_scheduler.reviewer_queue_order_refresh_due(reviewer)

    reviewer._answeredIds.append(3)

    assert rwkv_scheduler.reviewer_queue_order_refresh_due(reviewer)


def test_rwkv_first_review_elapsed_source_reads_direct_and_nested_config() -> None:
    assert rwkv_scheduler._rwkv_review_first_review_elapsed_from_card_creation(
        {"rwkvReviewFirstReviewElapsedFromCardCreation": True}
    )
    assert rwkv_scheduler._rwkv_review_first_review_elapsed_from_card_creation(
        {
            "other": {
                "jschoreels.rwkv": {
                    "rwkv_review_first_review_elapsed_from_card_creation": True,
                }
            }
        }
    )
    assert rwkv_scheduler._rwkv_review_first_review_elapsed_from_card_creation({})
    assert not rwkv_scheduler._rwkv_review_first_review_elapsed_from_card_creation(
        {"rwkvReviewFirstReviewElapsedFromCardCreation": False}
    )


def test_rwkv_min_intervening_reviews_defaults_to_five_and_allows_zero() -> None:
    assert rwkv_scheduler._rwkv_review_min_intervening_reviews({}) == 5
    assert (
        rwkv_scheduler._rwkv_review_min_intervening_reviews(
            {"rwkvReviewMinInterveningReviews": 0}
        )
        == 0
    )


def test_rwkv_review_batch_size_accepts_8192_and_rejects_larger_values() -> None:
    assert rwkv_scheduler._rwkv_review_batch_size({"rwkvReviewBatchSize": 8192}) == 8192
    assert rwkv_scheduler._rwkv_review_batch_size({"rwkvReviewBatchSize": 8193}) == 512


def test_rwkv_review_input_batch_cache_key_includes_first_review_elapsed_mode() -> None:
    missing_elapsed = _rwkv_reviewer(
        rwkv_review_first_review_elapsed_from_card_creation=False
    )
    card_creation_elapsed = _rwkv_reviewer(
        rwkv_review_first_review_elapsed_from_card_creation=True
    )

    missing_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=missing_elapsed,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=True,
    )
    card_creation_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=card_creation_elapsed,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=True,
    )

    assert missing_key is not None
    assert card_creation_key is not None
    assert missing_key != card_creation_key


def test_rwkv_review_queue_score_config_key_includes_first_review_elapsed_mode() -> (
    None
):
    missing_elapsed = _rwkv_reviewer(
        rwkv_review_first_review_elapsed_from_card_creation=False
    )
    card_creation_elapsed = _rwkv_reviewer(
        rwkv_review_first_review_elapsed_from_card_creation=True
    )

    assert rwkv_scheduler._rwkv_review_queue_score_config_key(
        missing_elapsed,
        100,
    ) != rwkv_scheduler._rwkv_review_queue_score_config_key(
        card_creation_elapsed,
        100,
    )


def test_rwkv_queue_caches_are_scoped_to_collection() -> None:
    first = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    second = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())

    first_input_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=first,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    second_input_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=second,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )

    assert first_input_key is not None
    assert second_input_key is not None
    assert first_input_key != second_input_key

    rwkv_scheduler._set_rwkv_review_queue_scores(first, 100, [(1, 0.25)])
    assert rwkv_scheduler._rwkv_review_queue_score_map_for_deck(first, 100) == {
        1: pytest.approx(0.25)
    }
    assert rwkv_scheduler._rwkv_review_queue_score_map_for_deck(second, 100) is None


def test_dynamic_desired_retention_change_invalidates_rwkv_and_resets_ui() -> None:
    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_count_clears = 0

        def clear_rwkv_deck_count_scores(self) -> None:
            self.deck_count_clears += 1

    rpc = Rpc()
    reviewer = _rwkv_reviewer(
        rpc=rpc,
        rwkv_review_instant_order_enabled=True,
    )
    reviewer.mw.col.decks.get_current_id = lambda: 100
    reviewer.mw.col.decks.deck_and_child_ids = lambda deck_id: [deck_id]
    resets: list[bool] = []
    reviewer.mw.reset = lambda: resets.append(True)

    context_before = rwkv_scheduler._rwkv_review_queue_context(reviewer, 100)
    cache_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=reviewer,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    assert context_before is not None
    assert cache_key is not None

    rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.75}
    rwkv_scheduler._rwkv_review_queue_target_maps[100] = {1: 0.90}
    rwkv_scheduler._rwkv_review_input_batch_module_cache[cache_key] = (
        rwkv_scheduler.RwkvReviewInputBatchBuild(
            inputs_by_batch_size={},
            loaded_rows=0,
            parsed_cards=0,
            cards_with_state=0,
            disabled_config_cards=0,
            eligible_cards=0,
            deck_configs=0,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        )
    )
    rwkv_scheduler._rwkv_score_prewarm_in_flight.add((1, 2, 3, 4, (100,)))

    rwkv_scheduler.dynamic_desired_retention_did_change(reviewer.mw)

    assert rwkv_scheduler._rwkv_review_queue_score_maps == {}
    assert rwkv_scheduler._rwkv_review_queue_target_maps == {}
    assert rwkv_scheduler._rwkv_review_input_batch_module_cache == {}
    assert rwkv_scheduler._rwkv_score_prewarm_in_flight == set()
    assert rpc.calls[-1] == {"deck_id": 100, "scores": []}
    assert rpc.deck_count_clears == 1
    assert resets == [True]
    assert rwkv_scheduler._rwkv_review_queue_context(reviewer, 100) != context_before


def test_study_queue_change_invalidates_cached_and_async_rwkv_work() -> None:
    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_count_clears = 0

        def clear_rwkv_deck_count_scores(self) -> None:
            self.deck_count_clears += 1

    rpc = Rpc()
    reviewer = _rwkv_reviewer(
        rpc=rpc,
        rwkv_review_instant_order_enabled=True,
    )
    reviewer.mw.reviewer = SimpleNamespace()
    reviewer.mw.col.decks.get_current_id = lambda: 100
    reviewer.mw.col.decks.deck_and_child_ids = lambda deck_id: [deck_id]
    context = rwkv_scheduler._rwkv_review_queue_context(reviewer, 100)
    cache_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=reviewer,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    assert context is not None
    assert cache_key is not None

    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={},
        loaded_rows=0,
        parsed_cards=0,
        cards_with_state=0,
        disabled_config_cards=0,
        eligible_cards=0,
        deck_configs=0,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    result = rwkv_scheduler.RwkvReviewQueueOrderAsyncResult(
        context=context,
        deck_id=100,
        reason="review queue",
        state_generation=0,
        scores=((1, 0.25),),
        input_build=input_build,
        cache_hits=0,
        runtime_requests=1,
        warmup_elapsed_ms=0.0,
        build_elapsed_ms=0.0,
        score_elapsed_ms=0.0,
    )
    rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.25}
    rwkv_scheduler._rwkv_review_queue_target_maps[100] = {1: 0.90}
    rwkv_scheduler._rwkv_review_input_batch_module_cache[cache_key] = input_build
    rwkv_scheduler._rwkv_score_prewarm_in_flight.add((1, 2, 3, 4, (100,)))

    rwkv_scheduler.study_queues_did_change(reviewer.mw, initiator=object())

    assert rwkv_scheduler._rwkv_study_queue_generation == 1
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {}
    assert rwkv_scheduler._rwkv_review_queue_target_maps == {}
    assert rwkv_scheduler._rwkv_review_input_batch_module_cache == {}
    assert rwkv_scheduler._rwkv_score_prewarm_in_flight == set()
    assert rpc.calls[-1] == {"deck_id": 100, "scores": []}
    assert rpc.deck_count_clears == 1
    assert not rwkv_scheduler.install_reviewer_queue_order_async_result(
        reviewer,
        result,
    )


def test_study_queue_change_invalidates_resolved_preset_ids() -> None:
    class Rpc(_RwkvQueueScoreRpc):
        preset_id = "1000"

        def get_fsrs_preset_ids_for_cards(self, cids: list[int]) -> SimpleNamespace:
            self.preset_id_calls.append(list(cids))
            return SimpleNamespace(
                items=[
                    SimpleNamespace(card_id=card_id, preset_id=self.preset_id)
                    for card_id in cids
                ]
            )

    rpc = Rpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    reviewer.mw.reviewer = SimpleNamespace()

    assert rwkv_scheduler._resolved_fsrs_preset_ids(reviewer, [1]) == {1: "1000"}
    rpc.preset_id = "2000"
    rwkv_scheduler.study_queues_did_change(reviewer.mw, initiator=object())

    assert rwkv_scheduler._resolved_fsrs_preset_ids(reviewer, [1]) == {1: "2000"}
    assert rpc.preset_id_calls == [[1], [1]]


def test_reviewer_answer_does_not_invalidate_rwkv_queue_caches() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(rpc=rpc)
    reviewer.mw.reviewer = reviewer
    rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.25}

    rwkv_scheduler.study_queues_did_change(reviewer.mw, initiator=reviewer)

    assert rwkv_scheduler._rwkv_study_queue_generation == 0
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {100: {1: 0.25}}
    assert rpc.calls == []


def test_async_reviewer_queue_result_rejects_changed_queue_context() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_reviewer(
        rpc=rpc,
        rwkv_review_instant_order_enabled=True,
    )
    reviewer.mw.col.decks.get_current_id = lambda: 100
    reviewer.mw.col.decks.deck_and_child_ids = lambda deck_id: [deck_id]
    context = rwkv_scheduler._rwkv_review_queue_context(reviewer, 100)
    assert context is not None
    result = rwkv_scheduler.RwkvReviewQueueOrderAsyncResult(
        context=context,
        deck_id=100,
        reason="review queue",
        state_generation=0,
        scores=((1, 0.25),),
        input_build=rwkv_scheduler.RwkvReviewInputBatchBuild(
            inputs_by_batch_size={},
            loaded_rows=0,
            parsed_cards=0,
            cards_with_state=0,
            disabled_config_cards=0,
            eligible_cards=0,
            deck_configs=0,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        ),
        cache_hits=0,
        runtime_requests=1,
        warmup_elapsed_ms=0.0,
        build_elapsed_ms=0.0,
        score_elapsed_ms=0.0,
    )

    reviewer.mw.col.sched._timing_today = lambda: SimpleNamespace(
        now=43 * 86_400 + 100,
        days_elapsed=43,
        next_day_at=44 * 86_400,
    )

    assert not rwkv_scheduler.install_reviewer_queue_order_async_result(
        reviewer,
        result,
    )
    assert rpc.calls == []


def test_rwkv_workload_fallback_grade_probabilities_shift_with_retrievability() -> None:
    low = rwkv_scheduler._fallback_rwkv_grade_probabilities(0.55)
    high = rwkv_scheduler._fallback_rwkv_grade_probabilities(0.95)

    assert math.isclose(sum(low), 1.0)
    assert math.isclose(sum(high), 1.0)
    assert low[0] > high[0]
    assert high[3] > low[3]


def test_rwkv_workload_review_model_populates_time_matrix_from_cache() -> None:
    rows = [
        (retrievability, ease, int((base_seconds + ease) * 1000))
        for retrievability, base_seconds in [(0.95, 10), (0.85, 12), (0.75, 14)]
        for ease in (1, 2, 3, 4)
    ]

    class Db:
        def all(self, _query: str) -> list[tuple[float, int, int]]:
            return rows

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(db=Db())))

    model = rwkv_scheduler._rwkv_simulator_review_model(reviewer)
    response = scheduler_pb2.SimulateFsrsWorkloadResponse()
    rwkv_scheduler._apply_rwkv_review_time_model(response, model)
    high_r_bucket = rwkv_scheduler._rwkv_simulator_ui_bucket(0.95)

    assert response.review_time_r_bucket_count == 20
    assert response.review_time_s_bucket_count == 1
    assert len(response.review_time_again_seconds) == 20
    assert len(response.review_time_sample_counts) == 20
    assert sum(response.review_time_sample_counts) == len(rows)
    assert response.review_time_sample_counts[high_r_bucket] == 4
    assert list(response.review_time_again_coeffs) == pytest.approx(
        [10.0, 20.0, 0.0, 0.0, 0.0]
    )
    assert list(response.review_time_easy_coeffs) == pytest.approx(
        [13.0, 20.0, 0.0, 0.0, 0.0]
    )
    assert list(response.review_time_grade_weights) == pytest.approx(
        [0.25, 0.25, 0.25, 0.25]
    )
    assert len(response.review_time_success_grade_probs) == 60
    assert len(response.review_time_transition_probs) == 16


def test_rwkv_workload_simulation_inputs_include_new_and_eligible_review_cards() -> (
    None
):
    review = _rwkv_review_input(card_id=1, note_id=10)
    new = replace(
        _rwkv_review_input(card_id=4, note_id=40),
        card_type=0,
        current_elapsed_days=None,
    )
    learning = replace(
        _rwkv_review_input(card_id=2, note_id=20),
        card_type=1,
    )
    missing_elapsed = replace(
        _rwkv_review_input(card_id=3, note_id=30),
        current_elapsed_days=None,
    )
    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={
            64: [(1, review), (2, learning), (4, new)],
            512: [(3, missing_elapsed)],
        },
        loaded_rows=4,
        parsed_cards=4,
        cards_with_state=4,
        disabled_config_cards=0,
        eligible_cards=4,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )

    assert rwkv_scheduler._rwkv_simulation_inputs(input_build) == [
        (1, review, 64),
        (4, new, 64),
    ]


def test_rwkv_workload_review_counts_are_monotonic_by_dr() -> None:
    response = scheduler_pb2.SimulateFsrsWorkloadResponse()
    response.review_count[30] = 10
    response.review_count[31] = 8
    response.review_count[33] = 12
    response.review_count[34] = 11
    preset = response.preset_workload.add()
    preset.name = "Preset"
    preset.review_count[30] = 5
    preset.review_count[31] = 3
    preset.review_count[33] = 7

    rwkv_scheduler._enforce_monotonic_rwkv_workload_review_counts(response)

    assert dict(response.review_count) == {30: 10, 31: 10, 33: 12, 34: 12}
    assert dict(preset.review_count) == {30: 5, 31: 5, 33: 7}


def test_rwkv_workload_review_order_uses_shared_request_order() -> None:
    short = rwkv_scheduler._rwkv_simulation_card(
        replace(_rwkv_review_input(card_id=1, note_id=10), interval_days=3),
        RwkvReviewPrediction(retrievability=0.8),
        target_retention=0.9,
    )
    long = rwkv_scheduler._rwkv_simulation_card(
        replace(_rwkv_review_input(card_id=2, note_id=20), interval_days=30),
        RwkvReviewPrediction(retrievability=0.7),
        target_retention=0.9,
    )

    short_prediction = RwkvReviewPrediction(retrievability=0.8)
    long_prediction = RwkvReviewPrediction(retrievability=0.7)
    assert rwkv_scheduler._rwkv_simulation_review_sort_key(
        short, short_prediction, 3, 0.9
    ) < rwkv_scheduler._rwkv_simulation_review_sort_key(long, long_prediction, 3, 0.9)
    assert rwkv_scheduler._rwkv_simulation_review_sort_key(
        long, long_prediction, 7, 0.9
    ) < rwkv_scheduler._rwkv_simulation_review_sort_key(short, short_prediction, 7, 0.9)


def test_rwkv_workload_simulation_uses_embedded_runtime_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = _rwkv_review_input(card_id=1, note_id=10)
    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={64: [(1, review)]},
        loaded_rows=1,
        parsed_cards=1,
        cards_with_state=1,
        disabled_config_cards=0,
        eligible_cards=1,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    snapshot = RwkvBackendCacheSnapshot(
        card_states={},
        note_states={},
        deck_states={},
        preset_states={},
        global_state=None,
        runtime_state=b"runtime",
    )
    progress_updates: list[tuple[int, int]] = []
    real_set_progress = rwkv_scheduler._set_rwkv_workload_progress

    def record_progress(current: int, total: int) -> None:
        progress_updates.append((current, total))
        real_set_progress(current, total)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_set_rwkv_workload_progress",
        record_progress,
    )

    class Backend:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.restored: list[RwkvBackendCacheSnapshot] = []

        def cached_review_input_predictions(self, inputs: object) -> object:
            raise AssertionError("fast path should not use Python prediction batches")

        def cache_snapshot(self) -> RwkvBackendCacheSnapshot:
            return snapshot

        def restore_cache_snapshot(self, restored: RwkvBackendCacheSnapshot) -> None:
            self.restored.append(restored)

        def simulate_workload(self, **kwargs: object) -> object:
            progress = cast(Callable[[int, int], None], kwargs.pop("progress"))
            progress(5, 71)
            self.calls.append(kwargs)
            return (
                1.25,
                2.5,
                [
                    (30, 3.0, 4.0, 5.0, 6),
                    (31, 7.0, 8.0, 9.0, 4),
                    (32, 11.0, 12.0, 13.0, 10),
                ],
            )

    backend = Backend()
    review_model = rwkv_scheduler._RwkvSimulatorReviewModel(
        grade_seconds=(1.0, 2.0, 3.0, 4.0),
        bucket_probabilities={},
        review_time_r_bucket_count=2,
        review_time_s_bucket_count=1,
        review_time_again_seconds=(11.0, 12.0),
        review_time_hard_seconds=(21.0, 22.0),
        review_time_good_seconds=(31.0, 32.0),
        review_time_easy_seconds=(41.0, 42.0),
        review_time_sample_counts=(3, 4),
        review_time_again_coeffs=(10.0, 1.0, 0.0, 0.0, 0.0),
        review_time_hard_coeffs=(20.0, 2.0, 0.0, 0.0, 0.0),
        review_time_good_coeffs=(30.0, 3.0, 0.0, 0.0, 0.0),
        review_time_easy_coeffs=(40.0, 4.0, 0.0, 0.0, 0.0),
        review_time_grade_weights=(0.1, 0.2, 0.3, 0.4),
        review_time_transition_probs=(0.25,) * 16,
        review_time_transition_counts=(0,) * 16,
        review_time_success_grade_probs=(1 / 3,) * 6,
        review_time_success_grade_counts=(3, 4),
    )
    set_reviewer_backend(backend)
    monkeypatch.setattr(
        rwkv_scheduler,
        "configure_reviewer_backend_from_environment",
        lambda: True,
    )
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend_warmed_up", lambda _: True)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_input_batches_for_search",
        lambda **_: input_build,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_simulator_review_model",
        lambda _: review_model,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_simulation_memorized",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("fast path should not use Python simulation")
        ),
    )

    request = scheduler_pb2.SimulateFsrsReviewRequest(
        search="deck:current",
        days_to_simulate=12,
        review_limit=34,
        new_limit=7,
        new_cards_ignore_review_limit=True,
        max_interval=456,
        review_order=3,
        suspend_after_lapse_count=9,
    )
    response = rwkv_scheduler.simulate_rwkv_workload(request, mw=SimpleNamespace())

    assert response.reviewless_end_memorized == pytest.approx(1.25)
    assert response.reviewless_end_weighted_memorized == pytest.approx(2.5)
    assert dict(response.memorized) == {
        30: pytest.approx(3.0),
        31: pytest.approx(7.0),
        32: pytest.approx(11.0),
    }
    assert dict(response.weighted_memorized) == {
        30: pytest.approx(4.0),
        31: pytest.approx(8.0),
        32: pytest.approx(12.0),
    }
    assert dict(response.cost) == {
        30: pytest.approx(5.0),
        31: pytest.approx(9.0),
        32: pytest.approx(13.0),
    }
    assert dict(response.review_count) == {30: 6, 31: 6, 32: 10}
    assert response.review_time_r_bucket_count == 2
    assert response.review_time_s_bucket_count == 1
    assert list(response.review_time_again_seconds) == pytest.approx([11.0, 12.0])
    assert list(response.review_time_hard_seconds) == pytest.approx([21.0, 22.0])
    assert list(response.review_time_good_seconds) == pytest.approx([31.0, 32.0])
    assert list(response.review_time_easy_seconds) == pytest.approx([41.0, 42.0])
    assert list(response.review_time_sample_counts) == [3, 4]
    assert list(response.review_time_again_coeffs) == pytest.approx(
        [10.0, 1.0, 0.0, 0.0, 0.0]
    )
    assert list(response.review_time_good_coeffs) == pytest.approx(
        [30.0, 3.0, 0.0, 0.0, 0.0]
    )
    assert list(response.review_time_grade_weights) == pytest.approx(
        [0.1, 0.2, 0.3, 0.4]
    )
    assert len(response.review_time_transition_probs) == 16
    assert len(response.review_time_success_grade_probs) == 6
    assert backend.restored == []
    assert backend.calls == [
        {
            "inputs": [(1, review, 64)],
            "snapshot": snapshot,
            "min_dr": 30,
            "max_dr": 99,
            "target_dr_step": 1,
            "days_to_simulate": 12,
            "scheduling": rwkv_scheduler._RwkvWorkloadScheduling(
                review_limit=34,
                new_limit=7,
                new_cards_ignore_review_limit=True,
                max_interval=456,
                review_order=3,
                suspend_after_lapses=9,
            ),
            "state_update_interval": 1,
            "review_model": review_model,
        }
    ]
    assert progress_updates == [(0, 0), (0, 71), (5, 71), (71, 71)]


def test_rwkv_workload_background_job_can_be_polled_and_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    captured_cancel_event: list[threading.Event | None] = []

    def simulate(data: bytes, *, cancel_event: threading.Event | None = None) -> bytes:
        assert data == b"request"
        captured_cancel_event.append(cancel_event)
        started.set()
        release.wait(timeout=5)
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("cancelled")
        response = scheduler_pb2.SimulateFsrsWorkloadResponse(
            reviewless_end_memorized=1.25,
        )
        return response.SerializeToString()

    monkeypatch.setattr(rwkv_scheduler, "simulate_rwkv_workload_bytes", simulate)

    rwkv_scheduler.start_rwkv_workload_bytes(b"request")
    assert started.wait(timeout=5)
    assert rwkv_scheduler.rwkv_workload_result_bytes() is None

    rwkv_scheduler.cancel_rwkv_workload()
    assert captured_cancel_event[0] is not None
    assert captured_cancel_event[0].is_set()
    release.set()
    for _ in range(100):
        try:
            rwkv_scheduler.rwkv_workload_result_bytes()
        except ValueError as exc:
            assert str(exc) == "cancelled"
            break
        time.sleep(0.01)
    else:
        raise AssertionError("RWKV workload job did not finish")


def test_rwkv_workload_background_job_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = scheduler_pb2.SimulateFsrsWorkloadResponse(
        reviewless_end_memorized=2.5,
    )

    def simulate_done(
        data: bytes,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bytes:
        assert data == b"request"
        assert cancel_event is not None
        return response.SerializeToString()

    monkeypatch.setattr(
        rwkv_scheduler,
        "simulate_rwkv_workload_bytes",
        simulate_done,
    )

    rwkv_scheduler.start_rwkv_workload_bytes(b"request")
    for _ in range(100):
        result = rwkv_scheduler.rwkv_workload_result_bytes()
        if result is not None:
            parsed = scheduler_pb2.SimulateFsrsWorkloadResponse()
            parsed.ParseFromString(result)
            assert parsed.reviewless_end_memorized == pytest.approx(2.5)
            break
        time.sleep(0.01)
    else:
        raise AssertionError("RWKV workload job did not finish")


def test_rwkv_workload_simulation_samples_evenly_by_card_id() -> None:
    inputs = [
        (card_id, _rwkv_review_input(card_id=card_id, note_id=card_id + 100), 64)
        for card_id in [50, 10, 40, 20, 30]
    ]

    sampled = rwkv_scheduler._sample_rwkv_simulation_inputs(inputs, 3)

    assert [card_id for card_id, _, _ in sampled] == [10, 30, 50]


def test_rwkv_workload_sampling_scales_daily_review_limit() -> None:
    assert rwkv_scheduler._rwkv_sampled_review_limit(200, 25.0) == 8
    assert rwkv_scheduler._rwkv_sampled_review_limit(10, 25.0) == 1
    assert rwkv_scheduler._rwkv_sampled_review_limit(0, 25.0) == 0
    assert rwkv_scheduler._rwkv_sampled_review_limit(200, 1.0) == 200


def test_rwkv_workload_scaling_caps_review_count_to_daily_limit() -> None:
    response = scheduler_pb2.SimulateFsrsWorkloadResponse(
        reviewless_end_memorized=1.0,
        reviewless_end_weighted_memorized=2.0,
    )
    response.memorized[30] = 3.0
    response.weighted_memorized[30] = 4.0
    response.cost[30] = 5.0
    response.review_count[30] = 12

    rwkv_scheduler._scale_rwkv_workload_response(
        response,
        10.0,
        review_count_cap=50,
    )

    assert response.reviewless_end_memorized == pytest.approx(10.0)
    assert response.reviewless_end_weighted_memorized == pytest.approx(20.0)
    assert dict(response.memorized) == {30: pytest.approx(30.0)}
    assert dict(response.weighted_memorized) == {30: pytest.approx(40.0)}
    assert dict(response.cost) == {30: pytest.approx(50.0)}
    assert dict(response.review_count) == {30: 50}


def test_rwkv_workload_target_drs_include_max_endpoint() -> None:
    assert rwkv_scheduler._rwkv_workload_target_drs(30, 95, 10) == [
        30,
        40,
        50,
        60,
        70,
        80,
        90,
        95,
    ]
    assert rwkv_scheduler._rwkv_workload_progress_total_for_step(30, 95, 10) == 9


def test_rwkv_memorised_history_builds_progressive_daily_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt import rwkv_srs_benchmark

    review_one = replace(
        _rwkv_review_input(card_id=1, note_id=101),
        is_query=False,
        ease=3,
        duration_millis=1000,
        day_offset=10,
        current_elapsed_days=-1,
        current_elapsed_seconds=-1,
    )
    review_two = replace(
        _rwkv_review_input(card_id=2, note_id=102),
        is_query=False,
        ease=3,
        duration_millis=1000,
        day_offset=11,
        current_elapsed_days=-1,
        current_elapsed_seconds=-1,
    )
    history = rwkv_scheduler.RwkvHistoricalReviewInputs(
        reviews=[review_one, review_two],
        review_ids=[1000, 2000],
        previous_review_id_by_card={1: 1000, 2: 2000},
        previous_interval_days_by_card={1: 4, 2: 4},
        review_count_by_card={1: 1, 2: 1},
        last_review_id=2000,
        review_count=2,
    )

    class Runtime:
        warmups: list[list[RwkvReviewInput]] = []

        def __init__(self, **_kwargs: object) -> None:
            pass

        def warm_up_reviews_in_place(self, reviews: Sequence[RwkvReviewInput]) -> None:
            self.warmups.append(list(reviews))

        def predict_retrievability_many_from_warm_up(
            self, reviews: Sequence[RwkvReviewInput]
        ) -> list[float]:
            return [
                1.0 - 0.1 * (review.current_elapsed_days or 0) for review in reviews
            ]

    monkeypatch.setattr(rwkv_srs_benchmark, "_RustRwkvRuntime", Runtime)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        lambda _reviewer: history,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_timing_today",
        lambda _reviewer: SimpleNamespace(days_elapsed=11, next_day_at=1_000_000),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_current_embedded_rwkv_model_path",
        lambda: Path("model.bin"),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_deck_config_for_deck_id",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_memorised_history_identity",
        lambda *_args, **_kwargs: "identity",
    )
    job = rwkv_scheduler.RwkvMemorisedHistoryJob(
        cancel_event=threading.Event(),
        display_card_ids=frozenset((1, 2)),
    )

    rwkv_scheduler._compute_rwkv_memorised_history(SimpleNamespace(), job)

    assert job.current == 3
    assert job.total == 3
    assert job.completed_through_day == 11
    assert job.retrievability_by_day == pytest.approx([1.0, 1.9])
    assert job.note_retrievability_by_day == pytest.approx([1.0, 1.9])
    assert job.card_count_by_day == [1, 2]
    assert job.result is not None
    assert job.result.identity == "identity"
    assert [(card.card_id, card.start_day) for card in job.result.cards] == [
        (1, 10),
        (2, 11),
    ]
    assert [
        int.from_bytes(job.result.cards[0].values[offset : offset + 2], "little")
        for offset in (0, 2)
    ] == [65_535, round(0.9 * 65_535)]


def test_rwkv_memorised_identity_counts_only_retained_learning_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        create table cards (id integer primary key);
        create table revlog (
            id integer primary key,
            cid integer not null,
            ease integer not null,
            type integer not null,
            factor integer not null
        );
        insert into cards values (1);
        insert into revlog values (1000, 1, 3, 0, 2500);
        insert into revlog values (2000, 1, 3, 1, 2500);
        insert into revlog values (3000, 1, 3, 0, 2500);
        insert into revlog values (4000, 1, 3, 0, 2500);
        insert into revlog values (5000, 1, 3, 1, 2500);
        """
    )

    class DB:
        def all(self, sql: str) -> list[tuple[int, int]]:
            return connection.execute(sql).fetchall()

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_memorised_history_identity",
        lambda _reviewer, *, last_review_id, review_count: json.dumps(
            {
                "lastReviewId": last_review_id,
                "reviewCount": review_count,
            }
        ),
    )
    identity = json.loads(
        rwkv_scheduler.rwkv_memorised_history_identity(
            SimpleNamespace(col=SimpleNamespace(db=DB()))
        )
    )

    assert identity == {"lastReviewId": 5000, "reviewCount": 3}


def test_rwkv_memorised_cancel_checkpoint_resumes_without_repredicting_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt import rwkv_srs_benchmark

    reviews = [
        replace(
            _rwkv_review_input(card_id=1, note_id=101),
            is_query=False,
            ease=3,
            duration_millis=1000,
            day_offset=10,
        ),
        replace(
            _rwkv_review_input(card_id=2, note_id=102),
            is_query=False,
            ease=3,
            duration_millis=1000,
            day_offset=11,
        ),
    ]
    history = rwkv_scheduler.RwkvHistoricalReviewInputs(
        reviews=reviews,
        review_ids=[1000, 2000],
        previous_review_id_by_card={1: 1000, 2: 2000},
        previous_interval_days_by_card={1: 4, 2: 4},
        review_count_by_card={1: 1, 2: 1},
        last_review_id=2000,
        review_count=2,
    )
    jobs: list[rwkv_scheduler.RwkvMemorisedHistoryJob] = []

    class Runtime:
        instances: list[Runtime] = []

        def __init__(self, **_kwargs: object) -> None:
            self.warmup_sizes: list[int] = []
            self.prediction_sizes: list[int] = []
            self.instances.append(self)

        def warm_up_reviews_in_place(self, inputs: Sequence[RwkvReviewInput]) -> None:
            self.warmup_sizes.append(len(inputs))

        def predict_retrievability_many_from_warm_up(
            self, inputs: Sequence[RwkvReviewInput]
        ) -> list[float]:
            self.prediction_sizes.append(len(inputs))
            if len(self.instances) == 1:
                jobs[0].cancel_event.set()
            return [0.8] * len(inputs)

    monkeypatch.setattr(rwkv_srs_benchmark, "_RustRwkvRuntime", Runtime)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        lambda _reviewer: history,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_timing_today",
        lambda _reviewer: SimpleNamespace(days_elapsed=11, next_day_at=1_000_000),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_current_embedded_rwkv_model_path",
        lambda: Path("model.bin"),
    )
    monkeypatch.setattr(rwkv_scheduler, "_deck_config_for_deck_id", lambda *_args: None)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_memorised_history_identity",
        lambda *_args, **_kwargs: "identity",
    )

    cancelled = rwkv_scheduler.RwkvMemorisedHistoryJob(
        cancel_event=threading.Event(),
        display_card_ids=frozenset((1, 2)),
    )
    jobs.append(cancelled)
    rwkv_scheduler._compute_rwkv_memorised_history(SimpleNamespace(), cancelled)

    checkpoint = cancelled.result
    assert checkpoint is not None
    assert not checkpoint.complete
    assert checkpoint.completed_through_day == 10
    assert cancelled.phase == "cancelled"

    resumed = rwkv_scheduler.RwkvMemorisedHistoryJob(
        cancel_event=threading.Event(),
        display_card_ids=frozenset((1, 2)),
        checkpoint=checkpoint,
    )
    jobs.append(resumed)
    rwkv_scheduler._compute_rwkv_memorised_history(SimpleNamespace(), resumed)

    assert resumed.result is not None
    assert resumed.result.complete
    assert resumed.result.completed_through_day == 11
    assert Runtime.instances[1].warmup_sizes == [1, 1]
    assert Runtime.instances[1].prediction_sizes == [2]


def test_rwkv_memorised_completed_cache_reuses_days_before_new_review() -> None:
    cached_identity = json.dumps(
        {
            "version": 1,
            "model": "same",
            "dayOffset": 11,
            "lastReviewId": 1000,
            "reviewCount": 1,
        },
        sort_keys=True,
    )
    current_identity = json.dumps(
        {
            "version": 1,
            "model": "same",
            "dayOffset": 11,
            "lastReviewId": 2000,
            "reviewCount": 2,
        },
        sort_keys=True,
    )
    completed = rwkv_scheduler.RwkvMemorisedHistoryResult(
        identity=cached_identity,
        first_day=10,
        last_day=11,
        cards=(
            rwkv_scheduler.RwkvMemorisedCardSeries(
                card_id=1,
                note_id=101,
                start_day=10,
                values=(50_000).to_bytes(2, "little") + (40_000).to_bytes(2, "little"),
            ),
        ),
        completed_through_day=11,
        total=2,
        complete=True,
    )
    reviews = [
        replace(_rwkv_review_input(card_id=1, note_id=101), day_offset=10),
        replace(_rwkv_review_input(card_id=2, note_id=102), day_offset=11),
    ]

    checkpoint = rwkv_scheduler._rwkv_memorised_completed_prefix_checkpoint(
        completed,
        identity=current_identity,
        first_day=10,
        last_day=11,
        total=3,
        reviews=reviews,
        review_ids=[1000, 2000],
    )

    assert checkpoint is not None
    assert checkpoint.identity == current_identity
    assert checkpoint.completed_through_day == 10
    assert checkpoint.last_day == 11
    assert checkpoint.total == 3
    assert not checkpoint.complete
    assert checkpoint.cards == (
        replace(completed.cards[0], values=(50_000).to_bytes(2, "little")),
    )


def test_rwkv_memorised_completed_cache_appends_new_scheduler_day() -> None:
    cached_identity = json.dumps(
        {
            "version": 1,
            "model": "same",
            "dayOffset": 11,
            "lastReviewId": 1000,
            "reviewCount": 1,
        },
        sort_keys=True,
    )
    current_identity = json.dumps(
        {
            "version": 1,
            "model": "same",
            "dayOffset": 12,
            "lastReviewId": 1000,
            "reviewCount": 1,
        },
        sort_keys=True,
    )
    completed = rwkv_scheduler.RwkvMemorisedHistoryResult(
        identity=cached_identity,
        first_day=10,
        last_day=11,
        cards=(
            rwkv_scheduler.RwkvMemorisedCardSeries(
                card_id=1,
                note_id=101,
                start_day=10,
                values=(50_000).to_bytes(2, "little") + (40_000).to_bytes(2, "little"),
            ),
        ),
        completed_through_day=11,
        total=2,
        complete=True,
    )
    reviews = [replace(_rwkv_review_input(card_id=1, note_id=101), day_offset=10)]

    checkpoint = rwkv_scheduler._rwkv_memorised_completed_prefix_checkpoint(
        completed,
        identity=current_identity,
        first_day=10,
        last_day=12,
        total=3,
        reviews=reviews,
        review_ids=[1000],
    )

    assert checkpoint is not None
    assert checkpoint.completed_through_day == 11
    assert checkpoint.last_day == 12
    assert checkpoint.cards == completed.cards


def test_rwkv_memorised_completed_cache_rejects_model_change() -> None:
    completed = rwkv_scheduler.RwkvMemorisedHistoryResult(
        identity=json.dumps(
            {
                "version": 1,
                "model": "old",
                "dayOffset": 11,
                "lastReviewId": 1000,
                "reviewCount": 1,
            },
            sort_keys=True,
        ),
        first_day=10,
        last_day=11,
        cards=(),
        completed_through_day=11,
        complete=True,
    )

    checkpoint = rwkv_scheduler._rwkv_memorised_completed_prefix_checkpoint(
        completed,
        identity=json.dumps(
            {
                "version": 1,
                "model": "new",
                "dayOffset": 12,
                "lastReviewId": 1000,
                "reviewCount": 1,
            },
            sort_keys=True,
        ),
        first_day=10,
        last_day=12,
        total=3,
        reviews=[replace(_rwkv_review_input(card_id=1, note_id=101), day_offset=10)],
        review_ids=[1000],
    )

    assert checkpoint is None


def test_rust_rwkv_warm_up_in_place_skips_snapshot_serialization() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        calls: list[tuple[list[tuple[object, ...]], bool]] = []

        def warm_up_reviews(
            self,
            rows: list[tuple[object, ...]],
            record_predictions: bool,
        ) -> list[object]:
            self.calls.append((rows, record_predictions))
            return []

    runtime = object.__new__(_RustRwkvRuntime)
    runtime._process = Process()
    runtime._process_lock = threading.RLock()

    runtime.warm_up_reviews_in_place([_rwkv_review_input(card_id=1, note_id=101)])

    assert len(runtime._process.calls) == 1
    assert runtime._process.calls[0][1] is False


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
                        "rwkv_review_instant_order_enabled": True,
                        "rwkv_review_refresh_on_exit": True,
                    }
                },
            }

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())))

    assert rwkv_scheduler.reviewer_queue_order_refresh_on_exit_enabled(reviewer)
    assert rwkv_scheduler.reviewer_queue_order_exit_refresh_needed(reviewer)


def test_rwkv_queue_exit_refresh_skips_current_queue_scores() -> None:
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
                        "rwkv_review_instant_order_enabled": True,
                        "rwkv_review_refresh_on_exit": True,
                    }
                },
            }

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(decks=Decks())))
    previous_backend = set_reviewer_backend(SimpleNamespace(state_generation=lambda: 7))
    try:
        rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.9}
        rwkv_scheduler._rwkv_review_queue_score_generations[100] = 7

        assert not rwkv_scheduler.reviewer_queue_order_exit_refresh_needed(reviewer)
    finally:
        set_reviewer_backend(previous_backend)


def test_rwkv_queue_refresh_due_uses_direct_refresh_interval() -> None:
    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "reviewOrder": 7,
                "rwkvReviewEnabled": True,
                "rwkvReviewInstantOrderEnabled": True,
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
                "rwkvReviewInstantOrderEnabled": True,
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


def test_interval_from_recall_curve_returns_max_when_target_not_reached() -> None:
    interval = interval_from_recall_curve(
        [
            RwkvRecallPoint(elapsed_days=0, retrievability=0.98),
            RwkvRecallPoint(elapsed_days=7, retrievability=0.93),
        ],
        target_retention=0.90,
        max_interval_days=365,
    )

    assert interval == 365


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
        RwkvIntervalOverride(again=11, hard=22, good=33, easy=44),
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
    assert updated.again.normal.review.memory_state.stability == pytest.approx(11)
    assert updated.hard.normal.review.memory_state.stability == pytest.approx(22)
    assert updated.good.normal.review.memory_state.stability == pytest.approx(33)
    assert updated.easy.normal.review.memory_state.stability == pytest.approx(44)
    assert not updated.good.normal.review.memory_state.HasField("stability_internal")
    assert updated.good.normal.review.memory_state.difficulty == pytest.approx(5.0)

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
        RwkvIntervalOverride(again=11, hard=22, good=33, easy=44),
    )

    assert updated.again.normal.learning.scheduled_secs == 60
    assert updated.hard.normal.relearning.review.scheduled_days == 20
    assert updated.hard.normal.relearning.review.fuzz_delta_days == 0
    assert (
        updated.hard.normal.relearning.review.memory_state.stability
        == pytest.approx(22)
    )
    assert updated.hard.normal.relearning.learning.scheduled_secs == 120
    assert updated.good.filtered.preview.scheduled_secs == 180
    assert updated.easy.normal.review.scheduled_days == 40
    assert updated.easy.normal.review.fuzz_delta_days == 0
    assert updated.easy.normal.review.memory_state.stability == pytest.approx(44)


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
        (
            "RWKV Curve Next S90",
            "Again:4d Hard:5d Good:7d Easy:10d",
        ),
        NEXT_S90_UNAVAILABLE_ROWS[1],
        *RWKV_AFTER_REVIEW_UNAVAILABLE_ROWS,
    ]
    assert runtime.reviewed == [(1, 3)]
    assert runtime.queries == [
        (2, None, None),
        (2, 1, ("deck", 100, 1)),
    ]
    assert runtime.query_inputs[0].is_query is True
    assert runtime.query_inputs[0].ease is None
    assert runtime.query_inputs[0].duration_millis is None
    assert runtime.query_inputs[0].identity.preset_id == 1000
    assert runtime.query_inputs[0].day_offset == 42
    assert runtime.query_inputs[0].current_normal_state_kind == "review"
    assert runtime.query_inputs[0].current_elapsed_days is None
    assert runtime.answered_inputs[0].is_query is False
    assert runtime.answered_inputs[0].ease == 3
    assert runtime.answered_inputs[0].duration_millis == 1234
    assert runtime.answered_inputs[0].reps == 5
    assert runtime.answered_inputs[0].lapses == 1
    assert states.good.normal.review.scheduled_days == 3


def test_reviewer_rwkv_prediction_overrides_all_grade_intervals_and_s90() -> None:
    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=0.62,
                interval_overrides=RwkvIntervalOverride(
                    again=1,
                    hard=4,
                    good=9,
                    easy=18,
                ),
                s90_overrides=RwkvIntervalOverride(
                    again=2,
                    hard=5,
                    good=10,
                    easy=20,
                ),
            )

    set_reviewer_backend(Backend())
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.again.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))
    states.hard.CopyFrom(_normal_review_state(interval=6, fuzz_delta=6))
    states.good.CopyFrom(_normal_review_state(interval=12, fuzz_delta=12))
    states.easy.CopyFrom(_normal_review_state(interval=24, fuzz_delta=24))
    states.again.normal.review.memory_state.stability = 30
    states.hard.normal.review.memory_state.stability = 60
    states.good.normal.review.memory_state.stability = 120
    states.easy.normal.review.memory_state.stability = 240

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert updated is not states
    assert updated.again.normal.review.scheduled_days == 1
    assert updated.hard.normal.review.scheduled_days == 4
    assert updated.good.normal.review.scheduled_days == 9
    assert updated.easy.normal.review.scheduled_days == 18
    assert updated.again.normal.review.memory_state.stability == pytest.approx(2)
    assert updated.hard.normal.review.memory_state.stability == pytest.approx(5)
    assert updated.good.normal.review.memory_state.stability == pytest.approx(10)
    assert updated.easy.normal.review.memory_state.stability == pytest.approx(20)
    assert states.again.normal.review.scheduled_days == 3
    assert states.hard.normal.review.scheduled_days == 6
    assert states.good.normal.review.scheduled_days == 12
    assert states.easy.normal.review.scheduled_days == 24
    diagnostics = current_reviewer_diagnostics(
        reviewer,
        card,
        fallback_source="FSRS",
    )
    assert diagnostics is not None
    assert diagnostics.retrievability == pytest.approx(0.62)
    assert diagnostics.retrievability_source == "RWKV"


@pytest.mark.parametrize(
    "enforce_grade_order",
    [True, False],
)
def test_reviewer_rwkv_grade_order_setting_is_passed_to_the_curve_computation(
    enforce_grade_order: bool,
) -> None:
    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=0.62,
                interval_overrides=RwkvIntervalOverride(
                    again=17,
                    hard=245,
                    good=2,
                    easy=1_035,
                ),
                s90_overrides=RwkvIntervalOverride(
                    again=14,
                    hard=60,
                    good=3,
                    easy=90,
                ),
            )

    set_reviewer_backend(Backend())
    reviewer = _rwkv_reviewer(
        rwkv_review_enforce_grade_order=enforce_grade_order,
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    identity = rwkv_review_identity(reviewer, card)
    assert identity is not None
    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=identity,
        ease=None,
    )
    states = SchedulingStates()
    for rating in ("again", "hard", "good", "easy"):
        getattr(states, rating).CopyFrom(_normal_review_state(interval=1, fuzz_delta=0))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert review_input.enforce_grade_order is enforce_grade_order
    assert tuple(
        getattr(updated, rating).normal.review.scheduled_days
        for rating in ("again", "hard", "good", "easy")
    ) == (17, 245, 2, 1_035)
    assert tuple(
        round(getattr(updated, rating).normal.review.memory_state.stability)
        for rating in ("again", "hard", "good", "easy")
    ) == (14, 60, 3, 90)


def test_rwkv_review_input_uses_preset_desired_retention_for_all_grade_targets() -> (
    None
):
    reviewer = _rwkv_reviewer(preset_desired_retention=0.86)
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=None,
    )

    assert review_input.target_retentions == pytest.approx((0.86, 0.86, 0.86, 0.86))


def test_rwkv_review_input_falls_back_to_deck_desired_retention() -> None:
    reviewer = _rwkv_reviewer(
        resolved_preset_id=None,
        deck_desired_retention=0.82,
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=None,
    )

    assert review_input.target_retentions == pytest.approx((0.82, 0.82, 0.82, 0.82))


def test_rwkv_review_input_uses_dynamic_desired_retention_per_grade() -> None:
    reviewer = _rwkv_reviewer(preset_desired_retention=0.86)
    reviewer._v3.states.dynamic_desired_retention_enabled = True
    reviewer._v3.states.dynamic_desired_retentions.extend([0.81, 0.82, 0.83, 0.84])
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=None,
    )

    assert review_input.target_retentions == pytest.approx((0.81, 0.82, 0.83, 0.84))


def test_rwkv_review_input_encodes_filtered_state_like_training_data() -> None:
    reviewer = _rwkv_reviewer()
    reviewer._v3.states.current.Clear()
    reviewer._v3.states.current.filtered.preview.scheduled_secs = 60
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=3,
    )

    assert review_input.current_state_kind == "filtered"
    assert review_input.card_type == int(rwkv_scheduler.RwkvReviewState.FILTERED)


@pytest.mark.parametrize(
    ("review_kind", "expected_state"),
    [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6)],
)
def test_historical_review_kind_maps_to_training_dataset_state(
    review_kind: int,
    expected_state: int,
) -> None:
    assert rwkv_scheduler._historical_review_state(review_kind) == expected_state


def test_historical_learning_start_maps_to_zero_and_resets_retained_sequence() -> None:
    rows = [
        (1_000, 1, 10, 100, 3, 100, 0, 1, 2500),
        (2_000, 1, 10, 100, 3, 100, 1, 2, 2500),
        (3_000, 1, 10, 100, 3, 100, 0, 1, 2500),
        (4_000, 1, 10, 100, 3, 100, 0, 1, 2500),
        (5_000, 1, 10, 100, 3, 100, 1, 3, 2500),
        (6_000, 2, 20, 100, 3, 100, 1, 3, 2500),
    ]

    retained = rwkv_scheduler._benchmark_retained_historical_review_rows(rows)

    assert [(row[0], state) for row, state in retained] == [
        (3_000, 0),
        (4_000, 1),
        (5_000, 2),
    ]


@pytest.mark.parametrize(
    ("previous_kind", "previous_ease", "expected_state"),
    [
        (1, 1, RwkvReviewState.RELEARNING),
        (1, 3, RwkvReviewState.FILTERED),
        (2, 1, RwkvReviewState.RELEARNING),
        (2, 2, RwkvReviewState.RELEARNING),
        (2, 3, RwkvReviewState.FILTERED),
        (2, 4, RwkvReviewState.FILTERED),
        (0, 3, RwkvReviewState.FILTERED),
        (3, 3, RwkvReviewState.REVIEW),
    ],
)
def test_live_same_day_review_uses_scheduler_valid_synthetic_state(
    previous_kind: int,
    previous_ease: int,
    expected_state: RwkvReviewState,
) -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (42 * 86_400 + 50) * 1000
    reviewer.mw.col.db = SimpleNamespace(
        first=lambda sql, card_id: (previous_id, previous_ease, previous_kind)
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_state = rwkv_scheduler._rwkv_review_state_for_live_context(
        reviewer,
        card,
        base_review_state=int(RwkvReviewState.REVIEW),
        answered_at_millis=(42 * 86_400 + 100) * 1000,
    )

    assert review_state == int(expected_state)


def test_live_interday_rwkv_answer_remains_review() -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (41 * 86_400 + 50) * 1000
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: (previous_id, 1, 1))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_state = rwkv_scheduler._rwkv_review_state_for_live_context(
        reviewer,
        card,
        base_review_state=int(RwkvReviewState.REVIEW),
        answered_at_millis=(42 * 86_400 + 100) * 1000,
    )

    assert review_state == int(RwkvReviewState.REVIEW)


def test_live_review_after_explicit_filtered_answer_respects_scheduler_state() -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (42 * 86_400 + 50) * 1000
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: (previous_id, 3, 3))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_state = rwkv_scheduler._rwkv_review_state_for_live_context(
        reviewer,
        card,
        base_review_state=int(RwkvReviewState.REVIEW),
        answered_at_millis=(42 * 86_400 + 100) * 1000,
    )

    assert review_state == int(RwkvReviewState.REVIEW)


def test_rwkv_input_batch_applies_dynamic_desired_retention_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DynamicRetentionInfo:
        def __init__(self, desired_retention: float | None) -> None:
            self.desired_retention = desired_retention

    cards = {1: SimpleNamespace(id=1), 2: SimpleNamespace(id=2)}

    class Collection:
        def get_card(self, card_id: int) -> object:
            return cards[card_id]

    col = Collection()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    first = replace(
        _rwkv_review_input(card_id=1, note_id=10),
        target_retentions=(0.90, 0.90, 0.90, 0.90),
    )
    second = replace(
        _rwkv_review_input(card_id=2, note_id=20),
        target_retentions=(0.81, 0.82, 0.83, 0.84),
    )
    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={512: [(1, first), (2, second)]},
        loaded_rows=2,
        parsed_cards=2,
        cards_with_state=2,
        disabled_config_cards=0,
        eligible_cards=2,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )

    def resolver(
        *,
        collection: object,
        cards: Sequence[object],
        current_desired_retentions: Mapping[int, float | None],
    ) -> dict[int, DynamicRetentionInfo]:
        assert collection is col
        assert [card.id for card in cards] == [1, 2]
        assert set(current_desired_retentions) == {1, 2}
        assert current_desired_retentions[1] == pytest.approx(0.90)
        assert current_desired_retentions[2] == pytest.approx(0.83)
        return {
            1: DynamicRetentionInfo(0.50),
            2: DynamicRetentionInfo(0.83),
        }

    monkeypatch.setattr(
        rwkv_scheduler,
        "_dynamic_desired_retention_info_for_cards_resolver",
        lambda: resolver,
    )

    resolved = rwkv_scheduler._resolve_dynamic_desired_retentions_for_input_build(
        reviewer,
        input_build,
    )

    resolved_inputs = dict(resolved.inputs_by_batch_size[512])
    assert resolved_inputs[1].target_retentions == pytest.approx(
        (0.50, 0.50, 0.50, 0.50)
    )
    assert resolved_inputs[2].target_retentions == pytest.approx(
        (0.81, 0.82, 0.83, 0.84)
    )
    assert resolved.dynamic_desired_retentions_resolved
    assert (
        rwkv_scheduler._resolve_dynamic_desired_retentions_for_input_build(
            reviewer,
            resolved,
        )
        is resolved
    )


def test_rwkv_review_input_uses_exact_elapsed_for_review_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 42 * 86_400 + 100
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: float(now))
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=1234,
        last_review_time=now - 30,
    )

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=None,
    )

    assert review_input.current_elapsed_days == 0
    assert review_input.current_elapsed_seconds == 30


def test_rwkv_review_input_uses_exact_elapsed_for_filtered_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 42 * 86_400 + 100
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: float(now))
    reviewer = _rwkv_reviewer()
    reviewer._v3.states.current.Clear()
    reviewer._v3.states.current.filtered.rescheduling.original_state.review.elapsed_days = 7
    card = _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=1234,
        last_review_time=now - 30,
    )

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(
            card_id=1,
            note_id=10,
            deck_id=100,
            preset_id=1000,
        ),
        ease=None,
    )

    assert review_input.card_type == 4
    assert review_input.current_state_kind == "filtered"
    assert review_input.current_normal_state_kind is None
    assert review_input.current_elapsed_days == 0
    assert review_input.current_elapsed_seconds == 30


def test_rwkv_review_input_falls_back_to_latest_eligible_review_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 42 * 86_400 + 100
    previous_review_time = 41 * 86_400 + 100
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: float(now))
    reviewer = _rwkv_reviewer()

    class DB:
        def first(self, sql: str, card_id: int) -> tuple[int, int, int]:
            assert "from revlog" in sql
            assert card_id == 1
            return previous_review_time * 1000, 3, 1

    reviewer.mw.col.db = DB()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(
            card_id=1,
            note_id=10,
            deck_id=100,
            preset_id=1000,
        ),
        ease=None,
    )

    assert review_input.card_type == 2
    assert review_input.current_elapsed_days == 1
    assert review_input.current_elapsed_seconds == 86_400


def test_rwkv_review_input_uses_card_creation_elapsed_for_new_cards() -> None:
    reviewer = _rwkv_reviewer(rwkv_review_first_review_elapsed_from_card_creation=True)
    reviewer._v3.states.current.normal.new.SetInParent()
    card = _rwkv_card(
        card_id=(42 * 86_400 + 100 - 90_000) * 1000,
        note_id=10,
        duration_millis=1234,
    )
    card.type = 0
    card.queue = 0

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(
            card_id=card.id,
            note_id=10,
            deck_id=100,
            preset_id=1000,
        ),
        ease=None,
    )

    assert review_input.current_normal_state_kind == "new"
    assert review_input.current_elapsed_days == 1
    assert review_input.current_elapsed_seconds == 90_000


def test_rwkv_review_input_leaves_new_card_elapsed_missing_by_default() -> None:
    reviewer = _rwkv_reviewer()
    reviewer._v3.states.current.normal.new.SetInParent()
    card = _rwkv_card(
        card_id=(42 * 86_400 + 100 - 90_000) * 1000,
        note_id=10,
        duration_millis=1234,
    )
    card.type = 0
    card.queue = 0

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(
            card_id=card.id,
            note_id=10,
            deck_id=100,
            preset_id=1000,
        ),
        ease=None,
    )

    assert review_input.current_normal_state_kind == "new"
    assert review_input.current_elapsed_days is None
    assert review_input.current_elapsed_seconds is None


def test_rwkv_stats_graph_review_input_uses_exact_elapsed_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: 10_000.0)
    card = rwkv_scheduler.RwkvStatsGraphCard(
        id=1,
        nid=10,
        did=100,
        odid=0,
        type=2,
        queue=2,
        due=50,
        odue=0,
        ivl=4,
        factor=2500,
        reps=5,
        lapses=1,
        last_review_time=9_970,
    )

    review_input = rwkv_scheduler._rwkv_review_input_for_stats_graph_card(
        card=card,
        deck_config={"id": 1000, "rwkvReviewEnabled": True},
        timing=SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400),
    )

    assert review_input is not None
    assert review_input.current_elapsed_seconds == 30


def test_rwkv_stats_graph_review_input_uses_card_creation_elapsed_by_default() -> None:
    now = 42 * 86_400 + 100
    card = rwkv_scheduler.RwkvStatsGraphCard(
        id=(now - 90_000) * 1000,
        nid=10,
        did=100,
        odid=0,
        type=0,
        queue=0,
        due=50,
        odue=0,
        ivl=0,
        factor=0,
        reps=0,
        lapses=0,
        last_review_time=None,
    )

    review_input = rwkv_scheduler._rwkv_review_input_for_stats_graph_card(
        card=card,
        deck_config={
            "id": 1000,
            "rwkvReviewEnabled": True,
        },
        timing=SimpleNamespace(
            now=now,
            days_elapsed=42,
            next_day_at=43 * 86_400,
        ),
    )

    assert review_input is not None
    assert review_input.current_state_kind == "normal"
    assert review_input.current_normal_state_kind == "new"
    assert review_input.current_elapsed_days == 1
    assert review_input.current_elapsed_seconds == 90_000


def test_rwkv_stats_graph_new_card_creation_elapsed_can_be_disabled() -> None:
    now = 42 * 86_400 + 100
    card = rwkv_scheduler.RwkvStatsGraphCard(
        id=(now - 90_000) * 1000,
        nid=10,
        did=100,
        odid=0,
        type=0,
        queue=0,
        due=50,
        odue=0,
        ivl=0,
        factor=0,
        reps=0,
        lapses=0,
        last_review_time=None,
    )

    review_input = rwkv_scheduler._rwkv_review_input_for_stats_graph_card(
        card=card,
        deck_config={
            "id": 1000,
            "rwkvReviewEnabled": True,
            "rwkvReviewFirstReviewElapsedFromCardCreation": False,
        },
        timing=SimpleNamespace(
            now=now,
            days_elapsed=42,
            next_day_at=43 * 86_400,
        ),
    )

    assert review_input is not None
    assert review_input.current_state_kind == "normal"
    assert review_input.current_normal_state_kind == "new"
    assert review_input.current_elapsed_days is None
    assert review_input.current_elapsed_seconds is None


def test_record_reviewer_answer_does_not_write_card_s90_separately() -> None:
    class Backend:
        def __init__(self) -> None:
            self.answers: list[tuple[int, int]] = []

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            self.answers.append((card.id, ease))

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    rwkv_scheduler._reviewer_backend_warmup_keys.add((id(backend), id(reviewer.mw.col)))
    reviewer.mw.col.update_card = lambda card, skip_undo_entry=False: pytest.fail(
        "unexpected card update"
    )
    reviewer.mw.col.db = SimpleNamespace(
        execute=lambda *args, **kwargs: pytest.fail("unexpected DB execute"),
        executemany=lambda *args, **kwargs: pytest.fail("unexpected DB executemany"),
        scalar=lambda *args, **kwargs: pytest.fail("unexpected DB scalar"),
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card.load = lambda: pytest.fail("unexpected card reload")
    reviewer._rwkv_review_prediction = RwkvReviewerPrediction(
        card_id=1,
        retrievability=0.62,
        review_enabled=True,
        interval_override_used=True,
        s90_overrides=RwkvIntervalOverride(
            again=2,
            hard=5,
            good=10,
            easy=20,
        ),
    )

    record_reviewer_answer(reviewer, card, ease=4)

    assert backend.answers == [(1, 4)]


def test_set_answer_rwkv_metadata_sets_retrievability_and_selected_s90() -> None:
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    answer = SimpleNamespace()
    reviewer._rwkv_review_prediction = RwkvReviewerPrediction(
        card_id=1,
        retrievability=0.62,
        review_enabled=True,
        interval_override_used=True,
        s90_overrides=RwkvIntervalOverride(
            again=2,
            hard=5,
            good=10,
            easy=20,
        ),
    )

    rwkv_scheduler.set_answer_rwkv_metadata(answer, reviewer, card, ease=4)

    assert answer.rwkv_s90 == pytest.approx(20)
    assert answer.rwkv_retrievability == pytest.approx(0.62)
    assert answer.rwkv_review_kind == 1


def test_set_answer_rwkv_metadata_persists_same_day_relearning_kind() -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (42 * 86_400 + 50) * 1000
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: (previous_id, 1, 1))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    answer = SimpleNamespace(answered_at_millis=(42 * 86_400 + 100) * 1000)

    rwkv_scheduler.set_answer_rwkv_metadata(answer, reviewer, card, ease=3)

    assert answer.rwkv_review_kind == 2
    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        ease=3,
    )
    assert review_input.card_type == int(RwkvReviewState.RELEARNING)
    assert review_input.card_queue == int(rwkv_scheduler.QUEUE_TYPE_DAY_LEARN_RELEARN)
    assert review_input.current_normal_state_kind == "relearning"


def test_failed_answer_retry_replaces_pending_rwkv_input() -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (40 * 86_400 + 100) * 1000
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: (previous_id, 3, 0))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1_234)

    rwkv_scheduler.set_answer_rwkv_metadata(
        SimpleNamespace(answered_at_millis=(42 * 86_400 + 50) * 1000),
        reviewer,
        card,
        ease=3,
    )

    # Simulate an answer operation failure followed by a retry after card changes.
    card.did = 200
    card.due = 75
    rwkv_scheduler.set_answer_rwkv_metadata(
        SimpleNamespace(answered_at_millis=(42 * 86_400 + 100) * 1000),
        reviewer,
        card,
        ease=3,
    )

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=200),
        ease=3,
    )
    assert review_input.identity.deck_id == 200
    assert review_input.card_due == 75


def test_ineligible_answer_retry_clears_pending_rwkv_input() -> None:
    reviewer = _rwkv_reviewer()
    previous_id = (40 * 86_400 + 100) * 1000
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: (previous_id, 3, 0))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1_234)

    rwkv_scheduler.set_answer_rwkv_metadata(
        SimpleNamespace(answered_at_millis=(42 * 86_400 + 50) * 1000),
        reviewer,
        card,
        ease=3,
    )

    card.did = 200
    card.due = 75
    reviewer.mw.col.decks.config_dict_for_deck_id = lambda deck_id: {
        "id": deck_id * 10,
        "rwkvReviewEnabled": deck_id == 100,
    }
    rwkv_scheduler.set_answer_rwkv_metadata(
        SimpleNamespace(answered_at_millis=(42 * 86_400 + 100) * 1000),
        reviewer,
        card,
        ease=3,
    )

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=200),
        ease=3,
    )
    assert review_input.identity.deck_id == 200
    assert review_input.card_due == 75


def test_live_answer_after_card_reload_matches_historical_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_review_id = (40 * 86_400 + 100) * 1000 + 123
    answered_at_millis = (42 * 86_400 + 100) * 1000 + 987
    raw_duration_millis = 9_000
    persisted_duration_millis = 5_000
    monkeypatch.setattr(
        rwkv_scheduler.time,
        "time",
        lambda: answered_at_millis / 1000,
    )
    rows: list[tuple[object, ...]] = [
        (
            previous_review_id,
            1,
            10,
            100,
            3,
            1_234,
            0,
            4,
            2_500,
        ),
    ]

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert "from revlog r" in sql
            assert args == ()
            return list(rows)

        def first(self, sql: str, card_id: int) -> tuple[int, int, int]:
            assert "from revlog" in sql
            assert card_id == 1
            row = rows[-1]
            return int(row[0]), int(row[4]), int(row[6])

    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    reviewer.mw.col.db = DB()
    rwkv_scheduler._reviewer_backend_warmup_keys.add((id(backend), id(reviewer.mw.col)))
    card = _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=raw_duration_millis,
        last_review_time=previous_review_id // 1000,
    )
    card.time_limit = lambda: persisted_duration_millis
    answer = SimpleNamespace(
        answered_at_millis=answered_at_millis,
        milliseconds_taken=raw_duration_millis,
    )

    rwkv_scheduler.set_answer_rwkv_metadata(answer, reviewer, card, ease=3)

    assert runtime.answered_inputs == []
    rows.append(
        (
            answered_at_millis,
            1,
            10,
            100,
            3,
            persisted_duration_millis,
            1,
            5,
            2_400,
        )
    )
    # Simulate Card.load() after the answer operation has persisted the new row.
    card.last_review_time = answered_at_millis // 1000
    card.time_taken = lambda capped=True: raw_duration_millis + 2_000
    card.ivl = 5
    card.factor = 2_400
    card.reps = 6

    record_reviewer_answer(reviewer, card, ease=3)

    live = runtime.answered_inputs[-1]
    rebuilt = rwkv_scheduler._historical_rwkv_review_inputs(reviewer).reviews[-1]
    assert (
        live.identity,
        live.ease,
        live.duration_millis,
        live.card_type,
        live.day_offset,
        live.current_elapsed_days,
        live.current_elapsed_seconds,
    ) == (
        rebuilt.identity,
        rebuilt.ease,
        rebuilt.duration_millis,
        rebuilt.card_type,
        rebuilt.day_offset,
        rebuilt.current_elapsed_days,
        rebuilt.current_elapsed_seconds,
    )
    assert live.duration_millis == persisted_duration_millis
    assert (live.current_elapsed_days, live.current_elapsed_seconds) == (2, 172_800)


def test_live_synthetic_filtered_state_chains_within_reviewer_session() -> None:
    reviewer = _rwkv_reviewer()
    first_id = (42 * 86_400 + 50) * 1000
    second_id = (42 * 86_400 + 100) * 1000
    latest = [(first_id, 3, 1)]
    reviewer.mw.col.db = SimpleNamespace(first=lambda sql, card_id: latest[0])
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    first_answer = SimpleNamespace(answered_at_millis=second_id)

    rwkv_scheduler.set_answer_rwkv_metadata(first_answer, reviewer, card, ease=3)
    record_reviewer_answer(reviewer, card, ease=3)

    assert first_answer.rwkv_review_kind == 3
    latest[0] = (second_id, 3, 3)
    next_answer = SimpleNamespace(answered_at_millis=second_id + 1_000)
    rwkv_scheduler.set_answer_rwkv_metadata(next_answer, reviewer, card, ease=3)
    assert next_answer.rwkv_review_kind == 3


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
                        again=7 + request.review_input.identity.card_id,
                        hard=8 + request.review_input.identity.card_id,
                        good=10 + request.review_input.identity.card_id,
                        easy=14 + request.review_input.identity.card_id,
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
    first, second = [prediction for prediction in predictions if prediction]
    assert first.interval_overrides == RwkvIntervalOverride(
        again=9,
        hard=10,
        good=12,
        easy=16,
    )
    assert second.interval_overrides == RwkvIntervalOverride(
        again=10,
        hard=11,
        good=13,
        easy=17,
    )
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


def test_rwkv_review_scores_use_retrievability_only_batch_path() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.retrievability_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            raise AssertionError("score-only batches should not use full predictions")

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.retrievability_card_ids.append(
                [request.review_input.identity.card_id for request in requests]
            )
            return [
                0.10 * request.review_input.identity.card_id for request in requests
            ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()

    scores = rwkv_scheduler._rwkv_review_scores_for_candidates(
        [
            RwkvReviewCandidate(
                reviewer=reviewer,
                card=_rwkv_card(card_id=2, note_id=20, duration_millis=2345),
            ),
            RwkvReviewCandidate(
                reviewer=reviewer,
                card=_rwkv_card(card_id=3, note_id=30, duration_millis=3456),
            ),
        ],
        batch_size=512,
    )

    assert scores == [
        (2, pytest.approx(0.20)),
        (3, pytest.approx(0.30)),
    ]
    assert runtime.retrievability_card_ids == [[2, 3]]


def test_rwkv_review_input_scores_use_resident_state_and_cache() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.resident_card_ids: list[list[int]] = []

        def predict_retrievability_many_from_warm_up(
            self,
            review_inputs: list[RwkvReviewInput],
        ) -> list[float]:
            card_ids = [review_input.identity.card_id for review_input in review_inputs]
            self.resident_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    inputs = [
        (2, _rwkv_review_input(card_id=2, note_id=20)),
        (3, _rwkv_review_input(card_id=3, note_id=30)),
    ]

    first = rwkv_scheduler._rwkv_review_scores_for_inputs(inputs, batch_size=1)
    second = rwkv_scheduler._rwkv_review_scores_for_inputs(inputs, batch_size=1)

    assert first == [(2, pytest.approx(0.20)), (3, pytest.approx(0.30))]
    assert second == first
    assert runtime.resident_card_ids == [[2, 3]]


def test_rwkv_current_retrievability_requests_use_resident_state() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.resident_card_ids: list[list[int]] = []
            self.serialized_card_ids: list[list[int]] = []

        def predict_retrievability_many_from_warm_up(
            self,
            review_inputs: list[RwkvReviewInput],
        ) -> list[float]:
            card_ids = [review_input.identity.card_id for review_input in review_inputs]
            self.resident_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            card_ids = [request.review_input.identity.card_id for request in requests]
            self.serialized_card_ids.append(card_ids)
            return [0.20 * card_id for card_id in card_ids]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    review_input = _rwkv_review_input(card_id=2, note_id=20)
    request = backend._prediction_request(review_input.identity, review_input)

    current = backend.predict_retrievability_requests([request])
    snapshot = backend.predict_retrievability_requests(
        [replace(request, card_state=b"older-state")]
    )

    assert [prediction.retrievability for prediction in current] == [
        pytest.approx(0.20)
    ]
    assert [prediction.retrievability for prediction in snapshot] == [
        pytest.approx(0.40)
    ]
    assert runtime.resident_card_ids == [[2]]
    assert runtime.serialized_card_ids == [[2]]


def test_rwkv_review_scores_upscale_default_retrievability_batch_size() -> None:
    assert rwkv_scheduler._rwkv_retrievability_batch_size(512) == 2048
    assert rwkv_scheduler._rwkv_retrievability_batch_size(8192) == 8192

    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.retrievability_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            raise AssertionError("score-only batches should not use full predictions")

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.retrievability_card_ids.append(
                [request.review_input.identity.card_id for request in requests]
            )
            return [0.50 for _ in requests]

    def candidates(count: int) -> list[RwkvReviewCandidate]:
        reviewer = _rwkv_reviewer()
        return [
            RwkvReviewCandidate(
                reviewer=reviewer,
                card=_rwkv_card(
                    card_id=card_id,
                    note_id=card_id * 10,
                    duration_millis=1000 + card_id,
                ),
            )
            for card_id in range(1, count + 1)
        ]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)

    scores = rwkv_scheduler._rwkv_review_scores_for_candidates(
        candidates(600),
        batch_size=512,
    )

    assert len(scores) == 600
    assert runtime.retrievability_card_ids == [list(range(1, 601))]

    runtime.retrievability_card_ids.clear()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    scores = rwkv_scheduler._rwkv_review_scores_for_candidates(
        candidates(130),
        batch_size=64,
    )

    assert len(scores) == 130
    assert runtime.retrievability_card_ids == [
        list(range(1, 65)),
        list(range(65, 129)),
        [129, 130],
    ]


def test_rwkv_retrievability_batches_do_not_populate_prediction_cache() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.retrievability_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            raise AssertionError("score-only batches should not use full predictions")

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            card_ids = [request.review_input.identity.card_id for request in requests]
            self.retrievability_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

    reviewer = _rwkv_reviewer()
    candidates = [
        RwkvReviewCandidate(
            reviewer=reviewer,
            card=_rwkv_card(card_id=2, note_id=20, duration_millis=2345),
        ),
        RwkvReviewCandidate(
            reviewer=reviewer,
            card=_rwkv_card(card_id=3, note_id=30, duration_millis=3456),
        ),
    ]
    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)

    first = rwkv_scheduler._rwkv_review_scores_for_candidates(
        candidates,
        batch_size=512,
    )
    second = rwkv_scheduler._rwkv_review_scores_for_candidates(
        candidates,
        batch_size=512,
    )

    assert first == [(2, pytest.approx(0.20)), (3, pytest.approx(0.30))]
    assert second == first
    assert runtime.retrievability_card_ids == [[2, 3], [2, 3]]


def test_rwkv_retrievability_batch_cache_does_not_hide_full_intervals() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.full_card_ids: list[list[int]] = []
            self.retrievability_card_ids: list[list[int]] = []

        def predict_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            card_ids = [request.review_input.identity.card_id for request in requests]
            self.full_card_ids.append(card_ids)
            return [
                RwkvReviewPrediction(
                    retrievability=0.10 * card_id,
                    interval_overrides=RwkvIntervalOverride(
                        again=1,
                        hard=2,
                        good=10 + card_id,
                        easy=20 + card_id,
                    ),
                )
                for card_id in card_ids
            ]

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            card_ids = [request.review_input.identity.card_id for request in requests]
            self.retrievability_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    candidate = RwkvReviewCandidate(
        reviewer=reviewer,
        card=_rwkv_card(card_id=2, note_id=20, duration_millis=2345),
    )

    scores = rwkv_scheduler._rwkv_review_scores_for_candidates(
        [candidate],
        batch_size=512,
    )
    prediction = backend.predict_reviews([candidate])[0]

    assert scores == [(2, pytest.approx(0.20))]
    assert prediction is not None
    assert prediction.interval_overrides.good == 12
    assert runtime.retrievability_card_ids == [[2]]
    assert runtime.full_card_ids == [[2]]


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
        (2, 2, ("deck", 100, 2)),
        (2, 2, ("deck", 100, 2)),
    ]
    assert current_reviewer_retrievability(reviewer, card_b) == pytest.approx(0.65)
    assert runtime.answered_inputs[0].day_offset == 40
    assert runtime.answered_inputs[0].current_elapsed_seconds == -1
    assert runtime.answered_inputs[1].current_elapsed_seconds == 90_000
    assert runtime.answered_inputs[1].current_elapsed_days == 1
    assert runtime.answered_inputs[1].card_type == 3
    assert runtime.answered_inputs[1].duration_millis == 2345


def test_historical_rwkv_inputs_can_use_card_creation_for_first_review_elapsed() -> (
    None
):
    first_review = (40 * 86_400 + 100) * 1000
    card_id = first_review - 3 * 86_400 * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    reviewer = _rwkv_reviewer(
        historical_review_rows=[
            (first_review, card_id, 10, 100, 2, 1234, 1, 3, 2500),
            (second_review, card_id, 10, 100, 3, 2345, 2, 5, 2400),
        ],
    )

    missing = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
    card_creation = rwkv_scheduler._historical_rwkv_review_inputs(
        reviewer,
        first_review_elapsed_source=rwkv_scheduler.RwkvFirstReviewElapsedSource.CARD_CREATION,
    )
    deck_config = rwkv_scheduler._historical_rwkv_review_inputs(
        _rwkv_reviewer(
            rwkv_review_first_review_elapsed_from_card_creation=True,
            historical_review_rows=[
                (first_review, card_id, 10, 100, 2, 1234, 1, 3, 2500),
                (second_review, card_id, 10, 100, 3, 2345, 2, 5, 2400),
            ],
        )
    )

    assert missing.reviews[0].current_elapsed_seconds == -1
    assert missing.reviews[0].current_elapsed_days == -1
    assert card_creation.reviews[0].current_elapsed_seconds == 3 * 86_400
    assert card_creation.reviews[0].current_elapsed_days == 3
    assert card_creation.reviews[1].current_elapsed_seconds == 90_000
    assert card_creation.reviews[1].current_elapsed_days == 1
    assert deck_config.reviews[0].current_elapsed_seconds == 3 * 86_400
    assert deck_config.reviews[0].current_elapsed_days == 3


def test_historical_rwkv_inputs_use_scheduler_days_for_elapsed_days() -> None:
    first_review = (40 * 86_400 + 86_300) * 1000
    second_review = (41 * 86_400 + 100) * 1000
    reviewer = _rwkv_reviewer(
        historical_review_rows=[
            (first_review, 1, 10, 100, 3, 1234, 1, 3, 2500),
            (second_review, 1, 10, 100, 3, 2345, 1, 5, 2500),
        ],
    )

    history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)

    assert history.reviews[1].current_elapsed_seconds == 200
    assert history.reviews[1].current_elapsed_days == 1


def test_compare_rwkv_first_review_elapsed_metrics_reports_logloss_change() -> None:
    class ElapsedRuntime:
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
            if review_input.ease is None:
                return RwkvReviewTransition(
                    prediction=RwkvReviewPrediction(
                        retrievability=(
                            0.8 if review_input.current_elapsed_seconds == -1 else 0.2
                        ),
                    ),
                )

            return RwkvReviewTransition()

    first_review = (40 * 86_400 + 100) * 1000
    card_id = first_review - 3 * 86_400 * 1000
    set_reviewer_backend(RwkvStatefulReviewerBackend(ElapsedRuntime()))
    reviewer = _rwkv_reviewer(
        historical_review_rows=[
            (first_review, card_id, 10, 100, 1, 1234, 1, 3, 2500),
        ],
    )

    comparison = rwkv_scheduler.compare_rwkv_first_review_elapsed_metrics(
        reviewer.mw,
    )

    assert comparison["available"] is True
    missing = comparison["missing"]
    card_creation = comparison["cardCreation"]
    assert isinstance(missing, dict)
    assert isinstance(card_creation, dict)
    assert missing["count"] == 1
    assert card_creation["count"] == 1
    assert missing["logLoss"] > card_creation["logLoss"]


def test_reviewer_rwkv_warmup_uses_historical_interval_split_rules() -> None:
    first_review = (39 * 86_400 + 100) * 1000
    second_review = (40 * 86_400 + 100) * 1000
    third_review = (41 * 86_400 + 100) * 1000
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(
        rwkv_review_dynamic_preset_replay=True,
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


def test_reviewer_rwkv_warmup_pins_resolved_preset_without_dynamic_replay() -> None:
    first_review = (39 * 86_400 + 100) * 1000
    second_review = (40 * 86_400 + 100) * 1000
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(
        resolved_preset_id=None,
        historical_review_rows=[
            (first_review, 1, 10, 100, 2, 1234, 1, 20, 2500),
            (second_review, 1, 10, 100, 3, 2345, 1, 30, 2400),
        ],
    )
    resolved_card_ids: list[int] = []

    def resolve_preset(card_id: int) -> SimpleNamespace:
        resolved_card_ids.append(card_id)
        return SimpleNamespace(id="addon:test:current")

    reviewer.mw.col.fsrs_preset_for_card = resolve_preset
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
        _expected_preset_hash("addon:test:current"),
        _expected_preset_hash("addon:test:current"),
    ]
    assert resolved_card_ids == [1]


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


def test_reviewer_rwkv_answer_does_not_store_cache_after_answer(
    tmp_path,
) -> None:
    review_id = (42 * 86_400 + 1000) * 1000
    rows = [(review_id, 1, 10, 100, 3, 1234, 1, 5, 2500)]
    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    key = rwkv_scheduler._reviewer_backend_warmup_key(reviewer)
    assert key is not None
    rwkv_scheduler._reviewer_backend_warmup_keys.add(key)
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    update_reviewer_scheduling_states(states, reviewer, card)
    record_reviewer_answer(reviewer, card, ease=3)

    assert runtime.reviewed == [(1, 3)]
    assert reviewer.mw.col.rwkv_retrievability_rows == []


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


def test_rust_rwkv_warmup_chunk_size_fills_state_only_wavefront() -> None:
    assert _rust_warmup_chunk_size(0) == 1
    assert _rust_warmup_chunk_size(2) == 2
    assert _rust_warmup_chunk_size(4000) == 4000
    assert _rust_warmup_chunk_size(50000) == 50_000
    assert _rust_warmup_chunk_size(200000) == 131_072


def test_rust_rwkv_calibration_chunk_size_preserves_progress_chunks() -> None:
    assert _rust_warmup_chunk_size(0, record_predictions=True) == 1
    assert _rust_warmup_chunk_size(2, record_predictions=True) == 1
    assert _rust_warmup_chunk_size(4000, record_predictions=True) == 40
    assert _rust_warmup_chunk_size(50000, record_predictions=True) == 16_384


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


def test_historical_rwkv_review_inputs_keeps_collection_scope_for_count(
    monkeypatch,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    second_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 0, 3, 2500),
        (second_review, 2, 20, 200, 3, 2345, 0, 5, 2400),
    ]
    count_calls: list[tuple[int, int | None]] = []

    monkeypatch.setattr(
        rwkv_scheduler,
        "_timing_today",
        lambda reviewer: SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_rows",
        lambda reviewer, *, after_review_id=None, deck_id=None: rows,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        lambda reviewer: False,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_deck_config_ids_by_card",
        lambda reviewer, rows, deck_configs_by_deck_id=None: {1: 1000, 2: 2000},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_resolved_fsrs_preset_ids",
        lambda reviewer, card_ids: {},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_deck_config_for_deck_id",
        lambda reviewer, deck_id: {"id": deck_id * 10},
    )

    def count_through(
        reviewer: object,
        last_review_id: int,
        *,
        deck_id: int | None = None,
    ) -> int:
        count_calls.append((last_review_id, deck_id))
        return 2

    monkeypatch.setattr(
        rwkv_scheduler,
        "_historical_rwkv_review_count_through",
        count_through,
    )

    history = rwkv_scheduler._historical_rwkv_review_inputs(SimpleNamespace())

    assert count_calls == []
    assert history.deck_id is None
    assert history.review_count == 2
    assert [review.identity.preset_id for review in history.reviews] == [1000, 2000]


def test_historical_rwkv_review_inputs_skips_full_scan_when_cache_is_current(
    monkeypatch,
) -> None:
    queries: list[tuple[str, tuple[object, ...]]] = []

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            queries.append((sql, args))
            return []

    monkeypatch.setattr(
        rwkv_scheduler,
        "_timing_today",
        lambda reviewer: pytest.fail("unchanged history should return before timing"),
    )
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(db=DB())))

    history = rwkv_scheduler._historical_rwkv_review_inputs(
        reviewer,
        after_review_id=1234,
        previous_review_id_by_card={1: 1234},
        previous_interval_days_by_card={1: 10},
        review_count_by_card={1: 3, 2: 2},
    )

    assert len(queries) == 1
    sql, args = queries[0]
    assert "r.id > ?" in sql
    assert "limit 1" in sql
    assert args == (1234,)
    assert history.reviews == []
    assert history.review_ids == []
    assert history.last_review_id == 1234
    assert history.review_count == 5


def test_historical_rwkv_review_rows_do_not_repeat_note_payloads() -> None:
    captured_sql: list[str] = []

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            captured_sql.append(sql)
            return []

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(db=DB())))

    assert rwkv_scheduler._historical_rwkv_review_rows(reviewer) == []

    sql = captured_sql[0].lower()
    assert "join cards" in sql
    assert "join notes" not in sql
    assert "n.tags" not in sql
    assert "n.flds" not in sql


def test_historical_rwkv_review_count_excludes_deleted_cards() -> None:
    captured_sql: list[str] = []

    class DB:
        def scalar(self, sql: str, *args: object) -> int:
            captured_sql.append(sql)
            assert args == (1234,)
            return 2

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(db=DB())))

    assert rwkv_scheduler._historical_rwkv_review_count_through(reviewer, 1234) == 2

    sql = captured_sql[0].lower()
    assert "from revlog r" in sql
    assert "join cards c on c.id = r.cid" in sql
    assert "e.id <= ?" in sql


def test_rwkv_state_cache_refuses_to_persist_deck_scoped_history(
    tmp_path,
    caplog,
) -> None:
    review_id = (40 * 86_400 + 100) * 1000
    reviewer = _rwkv_cache_reviewer(
        profile_folder=tmp_path,
        rows=[(review_id, 1, 10, 100, 2, 1234, 1, 3, 2500)],
    )
    history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer, deck_id=100)

    with caplog.at_level("WARNING", logger="aqt.rwkv_scheduler"):
        rwkv_scheduler._save_reviewer_backend_cache(reviewer, history)
        rwkv_scheduler._append_rwkv_state_cache_deltas(
            reviewer,
            history,
            snapshot_review_id=history.last_review_id,
        )

    cache_dir = tmp_path / "rwkv-state-cache"
    assert not (cache_dir / "snapshot-v1.bin").exists()
    assert not (cache_dir / "deltas-v1.log").exists()
    assert (
        "refusing to save scoped RWKV state cache history: deck_id=100" in caplog.text
    )
    assert (
        "refusing to append deltas scoped RWKV state cache history: deck_id=100"
        in caplog.text
    )


def test_rwkv_delta_writer_batches_small_records(
    monkeypatch,
    tmp_path,
) -> None:
    class RecordingAppendFile:
        def __init__(self) -> None:
            self.writes: list[bytes] = []
            self.flushed = False

        def write(self, data: bytes | bytearray) -> int:
            self.writes.append(bytes(data))
            return len(data)

        def flush(self) -> None:
            self.flushed = True

        def fileno(self) -> int:
            return -1

        def __enter__(self) -> "RecordingAppendFile":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    class RecordingAppendPath:
        def __init__(self) -> None:
            self.file = RecordingAppendFile()

        def exists(self) -> bool:
            return False

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=0)

        def open(self, mode: str) -> RecordingAppendFile:
            assert mode == "ab"
            return self.file

    fsynced: list[int] = []
    monkeypatch.setattr(
        rwkv_scheduler.os,
        "fsync",
        lambda fileno: fsynced.append(fileno),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_RWKV_STATE_CACHE_DELTA_WRITE_BUFFER_SIZE",
        1024 * 1024,
    )

    review_ids = [100, 200, 300]
    reviews = [
        _rwkv_review_input(card_id=card_id, note_id=card_id + 1000)
        for card_id in (1, 2, 3)
    ]
    path = RecordingAppendPath()

    rwkv_scheduler._append_rwkv_delta_records(cast(Any, path), review_ids, reviews)

    assert len(path.file.writes) == 1
    assert path.file.flushed
    assert fsynced == [-1]
    delta_path = tmp_path / "deltas-v1.log"
    delta_path.write_bytes(path.file.writes[0])
    assert rwkv_scheduler._read_rwkv_delta_records(
        delta_path,
        after_review_id=0,
        until_review_id=300,
    ) == list(zip(review_ids, reviews))


def test_reviewer_rwkv_warmup_preserves_rated_manual_review_state(
    monkeypatch,
    tmp_path,
) -> None:
    first_review = (40 * 86_400 + 100) * 1000
    manual_review = (41 * 86_400 + 3_700) * 1000
    rows = [
        (first_review, 1, 10, 100, 2, 1234, 1, 3, 2500),
        (manual_review, 1, 10, 100, 1, 2345, 4, 5, 2400),
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
    assert runtime.reviewed == [(1, 2), (1, 1)]
    manual_input = runtime.answered_inputs[1]
    assert manual_input.card_type == int(rwkv_scheduler.RwkvReviewState.MANUAL)
    assert manual_input.card_queue == int(rwkv_scheduler.QUEUE_TYPE_REV)
    assert manual_input.current_state_kind == "normal"
    assert manual_input.current_normal_state_kind == "review"
    assert manual_input.current_elapsed_seconds == 90_000
    assert [
        (review_id, prediction, source)
        for review_id, prediction, source, *_ in reviewer.mw.col.rwkv_retrievability_rows
    ] == []


def test_rwkv_model_cache_key_uses_model_content_not_install_path(
    monkeypatch,
    tmp_path,
) -> None:
    first_model_path = (
        tmp_path / "first-install" / "rwkv_inference" / "RWKV_trained_on_5000_10000.bin"
    )
    second_model_path = (
        tmp_path
        / "second-install"
        / "rwkv_inference"
        / "RWKV_trained_on_5000_10000.bin"
    )
    first_model_path.parent.mkdir(parents=True)
    second_model_path.parent.mkdir(parents=True)
    first_model_path.write_bytes(b"same model")
    second_model_path.write_bytes(b"same model")

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: first_model_path,
    )
    first_key = rwkv_scheduler._rwkv_model_cache_key()
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: second_model_path,
    )
    second_key = rwkv_scheduler._rwkv_model_cache_key()

    assert first_key == second_key
    assert first_key is not None
    assert first_key["sha256"] == hashlib.sha256(b"same model").hexdigest()
    assert "path" not in first_key
    assert "mtimeNs" not in first_key


def test_rwkv_model_cache_key_changes_when_model_content_changes(
    monkeypatch,
    tmp_path,
) -> None:
    model_path = tmp_path / "RWKV_trained_on_5000_10000.bin"

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: model_path,
    )

    model_path.write_bytes(b"model one")
    first_key = rwkv_scheduler._rwkv_model_cache_key()
    model_path.write_bytes(b"model two")
    second_key = rwkv_scheduler._rwkv_model_cache_key()

    assert first_key is not None
    assert second_key is not None
    assert first_key["size"] == second_key["size"]
    assert first_key["sha256"] != second_key["sha256"]


def test_rwkv_state_cache_restores_legacy_embedded_model_key_after_reinstall(
    monkeypatch,
    tmp_path,
) -> None:
    model_bytes = b"same bundled model"
    first_model_path = (
        tmp_path / "first-install" / "rwkv_inference" / "RWKV_trained_on_5000_10000.bin"
    )
    second_model_path = (
        tmp_path
        / "second-install"
        / "rwkv_inference"
        / "RWKV_trained_on_5000_10000.bin"
    )
    first_model_path.parent.mkdir(parents=True)
    second_model_path.parent.mkdir(parents=True)
    first_model_path.write_bytes(model_bytes)
    second_model_path.write_bytes(model_bytes)
    rows = [
        ((40 * 86_400 + 100) * 1000, 1, 10, 100, 2, 1234, 1, 3, 2500),
        ((41 * 86_400 + 3_700) * 1000, 1, 10, 100, 3, 2345, 2, 5, 2400),
    ]

    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: first_model_path,
    )

    profile_folder = tmp_path / "profile"
    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=profile_folder, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    assert runtime.reviewed == [(1, 2), (1, 3)]

    cache_dir = profile_folder / "rwkv-state-cache"
    metadata_path = cache_dir / "state-v1.meta.json"
    snapshot_path = cache_dir / "snapshot-v1.bin"
    legacy_model_key = {
        "path": str(first_model_path),
        "size": first_model_path.stat().st_size,
        "mtimeNs": first_model_path.stat().st_mtime_ns,
    }
    current_metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert current_metadata is not None
    legacy_metadata = {**current_metadata, "model": legacy_model_key}
    snapshot_metadata, snapshot, history = (
        rwkv_scheduler._decode_rwkv_state_cache_snapshot_file(
            snapshot_path.read_bytes()
        )
    )
    assert snapshot_metadata["model"] == current_metadata["model"]
    snapshot_path.write_bytes(
        rwkv_scheduler._encode_rwkv_state_cache_snapshot_file(
            metadata=legacy_metadata,
            snapshot=snapshot,
            history=history,
        )
    )
    metadata_path.write_text(
        json.dumps(legacy_metadata, separators=(",", ":"), sort_keys=True),
        encoding="utf8",
    )

    monkeypatch.setattr(
        "aqt.rwkv_scheduler.embedded_rwkv_model_path",
        lambda: second_model_path,
    )
    restored_runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(restored_runtime))

    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True
    assert restored_runtime.reviewed == []
    assert restored_runtime.restored_cache_states == [b"runtime-cache"]

    compacted_metadata = rwkv_scheduler._read_rwkv_state_cache_metadata(reviewer)
    assert compacted_metadata is not None
    assert compacted_metadata["model"] == rwkv_scheduler._rwkv_model_cache_key()
    compacted_snapshot_metadata, _, _ = (
        rwkv_scheduler._decode_rwkv_state_cache_snapshot_file(
            snapshot_path.read_bytes()
        )
    )
    assert compacted_snapshot_metadata["model"] == compacted_metadata["model"]


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
    prewarm_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "prewarm_reviewer_queue_score_cache",
        lambda _reviewer, **kwargs: prewarm_calls.append(kwargs),
    )

    set_reviewer_backend(RwkvStatefulReviewerBackend(_CacheRuntime()))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    taskman, progress_updates = _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.build_rwkv_state_cache_with_progress(reviewer.mw)

    assert taskman.with_progress_kwargs is not None
    assert taskman.with_progress_kwargs["immediate"] is True
    assert taskman.with_progress_kwargs["uses_collection"] is True
    assert taskman.with_progress_kwargs["title"] == "RWKV State Cache"
    assert prewarm_calls == [
        {
            "reason": "state cache build",
            "include_parent_scope": False,
        }
    ]
    assert rwkv_scheduler.rwkv_state_cache_usable(reviewer.mw) is True
    assert any(
        update["value"] == 0
        and update["max"] == 2
        and str(update["label"]).startswith("Preparing RWKV review inputs: 0/2 reviews")
        for update in progress_updates
    )
    assert any(
        update["value"] == 2
        and update["max"] == 2
        and str(update["label"]).startswith("Preparing RWKV review inputs: 2/2 reviews")
        for update in progress_updates
    )
    assert any(
        update["value"] == 2
        and update["max"] == 2
        and str(update["label"]).startswith(
            "Building RWKV state cache: 2/2 reviews | elapsed: "
        )
        and str(update["label"]).endswith(" | remaining: 0s")
        for update in progress_updates
    )


def test_rwkv_state_cache_build_skips_review_retrievability_cache_by_default(
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
    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.build_rwkv_state_cache_with_progress(reviewer.mw)

    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert rwkv_scheduler.rwkv_state_cache_usable(reviewer.mw) is True
    assert reviewer.mw.col.rwkv_retrievability_rows == []


def test_rwkv_state_cache_build_backfills_missing_review_retrievability_cache(
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
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    reviewer.mw.col.rwkv_retrievability_rows.clear()
    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.build_rwkv_state_cache_with_progress(
        reviewer.mw,
        record_retrievability_cache=True,
    )

    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert [
        (review_id, prediction, source)
        for review_id, prediction, source, *_ in reviewer.mw.col.rwkv_retrievability_rows
    ] == [
        (first_review, pytest.approx(0.45), "rwkv_state_cache_build"),
        (second_review, pytest.approx(0.45), "rwkv_state_cache_build"),
    ]


def test_rwkv_state_cache_build_backfills_missing_review_cache_when_already_warm(
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

    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler._warm_up_reviewer_backend(reviewer) is True

    reviewer.mw.col.rwkv_retrievability_rows.clear()
    runtime.reviewed.clear()
    _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.build_rwkv_state_cache_with_progress(
        reviewer.mw,
        record_retrievability_cache=True,
    )

    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert [
        (review_id, prediction, source)
        for review_id, prediction, source, *_ in reviewer.mw.col.rwkv_retrievability_rows
    ] == [
        (first_review, pytest.approx(0.45), "rwkv_state_cache_build"),
        (second_review, pytest.approx(0.45), "rwkv_state_cache_build"),
    ]


def test_ensure_rwkv_calibration_data_generates_once_and_restores_state(
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
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    assert rwkv_scheduler.warm_up_rwkv_state(reviewer.mw) is True
    before = backend.cache_snapshot()
    assert reviewer.mw.col.rwkv_retrievability_rows == []
    assert rwkv_scheduler.rwkv_calibration_data_available(reviewer.mw) is False

    runtime.reviewed.clear()
    assert rwkv_scheduler.ensure_rwkv_calibration_data(reviewer.mw) is True
    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert backend.cache_snapshot() == before
    assert rwkv_scheduler.rwkv_calibration_data_available(reviewer.mw) is True
    assert {row[4] for row in reviewer.mw.col.rwkv_retrievability_rows} == {
        rwkv_scheduler._RWKV_RETRIEVABILITY_SAMPLE_ROLE_FINAL_FIT,
        rwkv_scheduler._RWKV_RETRIEVABILITY_SAMPLE_ROLE_TEST_FOLD,
    }

    runtime.reviewed.clear()
    assert rwkv_scheduler.ensure_rwkv_calibration_data(reviewer.mw) is True
    assert runtime.reviewed == []


def test_rwkv_state_cache_build_satisfies_sse_explicit_revlog_contract(
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

    assert (
        rwkv_scheduler.warm_up_rwkv_state(
            reviewer.mw,
            force_rebuild=True,
            require_retrievability_cache=True,
        )
        is True
    )

    assert _rwkv_sse_harness_review_retrievability(
        reviewer.mw.col,
        [first_review, second_review],
    ) == {
        "column": "search_stats_rwkv_review_retrievability",
        "data": [
            (first_review, pytest.approx(0.45)),
            (second_review, pytest.approx(0.45)),
        ],
    }
    assert _rwkv_sse_harness_review_retrievability(
        reviewer.mw.col,
        [first_review, second_review + 1],
    ) == {"column": None, "data": []}


def test_srs_benchmark_state_cache_build_satisfies_sse_explicit_revlog_contract(
    monkeypatch,
    tmp_path,
) -> None:
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

        def process_row(self, row: dict[str, object]) -> object:
            self.answer_rows.append(row)
            return object()

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

    process = Process()
    set_reviewer_backend(SrsBenchmarkRwkvReviewerBackend(process=process))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)

    assert (
        rwkv_scheduler.warm_up_rwkv_state(
            reviewer.mw,
            force_rebuild=True,
            require_retrievability_cache=True,
        )
        is True
    )

    assert [row["rating"] for row in process.query_rows] == [1, 1]
    assert [row["rating"] for row in process.answer_rows] == [2, 3]
    assert _rwkv_sse_harness_review_retrievability(
        reviewer.mw.col,
        [first_review, second_review],
    ) == {
        "column": "search_stats_rwkv_review_retrievability",
        "data": [
            (first_review, pytest.approx(0.72)),
            (second_review, pytest.approx(0.72)),
        ],
    }


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
    assert reviewer.mw.col.rwkv_retrievability_rows == []


def test_stateful_backend_uses_runtime_bulk_warmup_for_empty_state() -> None:
    class BulkWarmUpRuntime:
        def __init__(self) -> None:
            self.reviews: list[RwkvReviewInput] = []

        def warm_up_reviews(
            self,
            reviews: list[RwkvReviewInput],
            *,
            review_ids: list[int] | None,
            prediction_recorder: Any,
            progress: Any,
        ) -> RwkvBackendCacheSnapshot:
            self.reviews = list(reviews)
            if prediction_recorder is not None and review_ids is not None:
                prediction_recorder(review_ids[0], 0.31)
                prediction_recorder(review_ids[1], 0.42)
            if progress is not None:
                progress(RwkvWarmUpProgress(processed_reviews=2, total_reviews=2))

            return RwkvBackendCacheSnapshot(
                card_states={1: b"card-1", 2: b"card-2"},
                note_states={10: b"note-10", 20: b"note-20"},
                deck_states={100: b"deck-100"},
                preset_states={1000: b"preset-1000"},
                global_state=b"global",
                runtime_state=b"runtime",
            )

        def review(self, **kwargs: object) -> RwkvReviewTransition:
            raise AssertionError("bulk warm-up should replace per-review replay")

    first = RwkvReviewInput(
        identity=RwkvReviewIdentity(card_id=1, note_id=10, deck_id=100, preset_id=1000),
        is_query=False,
        ease=2,
        duration_millis=1234,
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
    second = RwkvReviewInput(
        identity=RwkvReviewIdentity(card_id=2, note_id=20, deck_id=100, preset_id=1000),
        is_query=False,
        ease=3,
        duration_millis=2345,
        card_type=2,
        card_queue=2,
        card_due=50,
        interval_days=5,
        ease_factor=2400,
        reps=6,
        lapses=1,
        day_offset=43,
        current_state_kind="normal",
        current_normal_state_kind="review",
        current_elapsed_days=8,
        current_elapsed_seconds=691200,
    )
    runtime = BulkWarmUpRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    recorded: list[tuple[int, float]] = []
    progress_updates: list[RwkvWarmUpProgress] = []

    backend.warm_up(
        [first, second],
        review_ids=[101, 102],
        prediction_recorder=lambda review_id, retrievability: recorded.append(
            (review_id, retrievability)
        ),
        progress=progress_updates.append,
    )

    snapshot = backend.cache_snapshot()
    assert runtime.reviews == [first, second]
    assert recorded == [(101, 0.31), (102, 0.42)]
    assert snapshot.card_states == {1: b"card-1", 2: b"card-2"}
    assert snapshot.note_states == {10: b"note-10", 20: b"note-20"}
    assert snapshot.deck_states == {100: b"deck-100"}
    assert snapshot.preset_states == {1000: b"preset-1000"}
    assert snapshot.global_state == b"global"
    assert snapshot.runtime_state is None
    assert progress_updates[-1] == RwkvWarmUpProgress(
        processed_reviews=2,
        total_reviews=2,
    )


def test_warmup_capable_backend_records_review_retrievability_cache(tmp_path) -> None:
    class Backend:
        def warm_up(
            self,
            reviews: list[RwkvReviewInput],
            *,
            review_ids: list[int] | None = None,
            prediction_recorder: Any = None,
            progress: Any = None,
        ) -> None:
            assert [review.identity.card_id for review in reviews] == [1, 2]
            if prediction_recorder is not None and review_ids is not None:
                prediction_recorder(review_ids[0], 0.21)
                prediction_recorder(review_ids[1], 0.32)
            if progress is not None:
                progress(RwkvWarmUpProgress(processed_reviews=2, total_reviews=2))

    backend = Backend()
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])
    progress_labels: list[str] = []

    rwkv_scheduler._warm_up_rwkv_reviews(
        reviewer,
        backend,
        backend.warm_up,
        [
            _rwkv_review_input(card_id=1, note_id=10),
            _rwkv_review_input(card_id=2, note_id=20),
        ],
        review_ids=[101, 102],
        progress=lambda label, value, max_value: progress_labels.append(label),
        label="Building RWKV state cache",
    )

    assert [
        (review_id, prediction, source)
        for review_id, prediction, source, *_ in reviewer.mw.col.rwkv_retrievability_rows
    ] == [
        (101, pytest.approx(0.21), "rwkv_state_cache_build"),
        (102, pytest.approx(0.32), "rwkv_state_cache_build"),
    ]
    assert any(
        label.startswith("Building RWKV state cache: 2/2 reviews")
        for label in progress_labels
    )


def test_state_cache_build_skips_prediction_metadata_without_cache_rows(
    tmp_path,
) -> None:
    class Backend:
        def warm_up(
            self,
            reviews: list[RwkvReviewInput],
            **kwargs: object,
        ) -> None:
            assert len(reviews) == 2
            assert kwargs.get("prediction_recorder") is None

    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=[])
    backend = Backend()

    rwkv_scheduler._warm_up_rwkv_reviews(
        reviewer,
        backend,
        backend.warm_up,
        [
            _rwkv_review_input(card_id=1, note_id=10),
            _rwkv_review_input(card_id=2, note_id=20),
        ],
        review_ids=[101, 102],
        progress=None,
        label="Building RWKV state cache",
        record_retrievability_cache=False,
    )


def test_embedded_warmup_batches_prediction_recording() -> None:
    from aqt.rwkv_srs_benchmark import _record_warm_up_predictions

    class Recorder:
        def __call__(self, review_id: int, retrievability: float) -> None:
            pytest.fail("batch-capable recorder should not be called per prediction")

        def record_many(self, rows: list[tuple[int, float]]) -> None:
            self.rows.append(rows)

        def __init__(self) -> None:
            self.rows: list[list[tuple[int, float]]] = []

    recorder = Recorder()
    _record_warm_up_predictions(
        recorder,
        [101, 102, 103, 104],
        1,
        [(0, 0.21), (2, 0.43), (9, 0.99)],
    )

    assert recorder.rows == [[(102, 0.21), (104, 0.43)]]


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
    prewarm_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "prewarm_reviewer_queue_score_cache",
        lambda _reviewer, **kwargs: prewarm_calls.append(kwargs),
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
    assert prewarm_calls == [
        {
            "reason": "startup cache load",
            "include_parent_scope": False,
        }
    ]
    assert rwkv_scheduler.rwkv_state_cache_loading(reviewer.mw) is False
    assert any(
        update["label"] == "Loading new RWKV reviews..." for update in progress_updates
    )


def test_deck_browser_counts_wait_for_startup_cache_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[tuple[int, object | None]] = []
    completed: list[bool] = []
    refreshes: list[str] = []

    class Taskman:
        progress_task: Callable[[], bool] | None = None
        progress_done: Callable[[Future[bool]], None] | None = None

        def run_on_main(self, callback: Callable[[], None]) -> None:
            callback()

        def with_progress(
            self,
            task: Callable[[], bool],
            on_done: Callable[[Future[bool]], None],
            **_kwargs: object,
        ) -> None:
            self.progress_task = task
            self.progress_done = on_done

        def run_in_background(
            self,
            task: Callable[[], object],
            on_done: Callable[[Future[object]], None],
            *,
            uses_collection: bool,
        ) -> None:
            future: Future[object] = Future()
            future.set_result(task())
            on_done(future)

    taskman = Taskman()
    mw = SimpleNamespace(
        taskman=taskman,
        state="deckBrowser",
        onRefreshTimer=lambda: refreshes.append("refresh"),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "load_rwkv_state_cache",
        lambda _mw, *, progress=None: True,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_score_prewarm_work_for_deck",
        lambda _reviewer, *, deck_id, reason: None,
    )

    rwkv_scheduler.load_rwkv_state_cache_with_progress(mw)

    assert rwkv_scheduler.rwkv_state_cache_loading(mw) is True
    rwkv_scheduler.prepare_deck_browser_rwkv_counts_incrementally(
        mw,
        [10],
        should_continue=lambda: True,
        on_update=lambda deck_id, tree: updates.append((deck_id, tree)),
        on_done=completed.append,
    )
    assert updates == []
    assert completed == [False]

    assert taskman.progress_task is not None
    assert taskman.progress_done is not None
    future: Future[bool] = Future()
    future.set_result(taskman.progress_task())
    taskman.progress_done(future)

    assert rwkv_scheduler.rwkv_state_cache_loading(mw) is False
    assert refreshes == ["refresh"]


def test_startup_cache_check_reuses_deck_config_scan(monkeypatch) -> None:
    config_reads = 0
    cache_checks: list[bool | None] = []
    load_calls: list[object] = []

    class Decks:
        def all_config(self) -> list[dict[str, object]]:
            nonlocal config_reads
            config_reads += 1
            return [
                {
                    "rwkvReviewEnabled": True,
                    "rwkvReviewDynamicPresetReplay": True,
                }
            ]

    def cache_usable(
        mw: object,
        *,
        dynamic_preset_replay_enabled: bool | None = None,
    ) -> bool:
        cache_checks.append(dynamic_preset_replay_enabled)
        return True

    monkeypatch.setattr(rwkv_scheduler, "rwkv_state_cache_usable", cache_usable)
    monkeypatch.setattr(
        rwkv_scheduler,
        "load_rwkv_state_cache_with_progress",
        load_calls.append,
    )
    mw = SimpleNamespace(col=SimpleNamespace(decks=Decks()))

    rwkv_scheduler.prepare_rwkv_state_cache_on_startup(mw)

    assert config_reads == 1
    assert cache_checks == [True]
    assert load_calls == [mw]


def test_startup_prompt_can_build_rwkv_state_cache_only(
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
    prompt_calls: list[dict[str, object]] = []

    def ask_user_dialog(text: str, **kwargs: object) -> None:
        assert "state cache" in text
        prompt_calls.append(kwargs)
        callback = kwargs["callback"]
        assert callable(callback)
        callback(0)

    monkeypatch.setattr("aqt.utils.ask_user_dialog", ask_user_dialog)

    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    taskman, _progress_updates = _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.prepare_rwkv_state_cache_on_startup(reviewer.mw)

    assert len(prompt_calls) == 1
    assert prompt_calls[0]["default_button"] == 1
    assert prompt_calls[0]["title"] == "RWKV State Cache"
    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert rwkv_scheduler.rwkv_state_cache_usable(reviewer.mw) is True
    assert reviewer.mw.col.rwkv_retrievability_rows == []
    assert taskman.with_progress_kwargs is not None


def test_startup_prompt_can_build_rwkv_state_cache_with_calibration_data(
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

    def ask_user_dialog(text: str, **kwargs: object) -> None:
        assert "state cache" in text
        callback = kwargs["callback"]
        assert callable(callback)
        callback(1)

    monkeypatch.setattr("aqt.utils.ask_user_dialog", ask_user_dialog)

    runtime = _CacheRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    reviewer = _rwkv_cache_reviewer(profile_folder=tmp_path, rows=rows)
    _attach_progress_taskman(reviewer.mw)

    rwkv_scheduler.prepare_rwkv_state_cache_on_startup(reviewer.mw)

    assert runtime.reviewed == [(1, 2), (1, 3)]
    assert [
        (review_id, prediction, source)
        for review_id, prediction, source, *_ in reviewer.mw.col.rwkv_retrievability_rows
    ] == [
        (first_review, pytest.approx(0.45), "rwkv_state_cache_build"),
        (second_review, pytest.approx(0.45), "rwkv_state_cache_build"),
    ]


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

    assert record_collection_undo(_undo_result(counter=2, next_counter=3)) == [2]
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_c)
    assert current_reviewer_retrievability(reviewer, card_c) == pytest.approx(0.55)
    assert runtime.runtime_review_count == 1

    assert record_collection_undo(_undo_result(counter=1, next_counter=4)) == [1]
    update_reviewer_scheduling_states(SchedulingStates(), reviewer, card_c)
    assert current_reviewer_retrievability(reviewer, card_c) == pytest.approx(0.45)
    assert runtime.runtime_review_count == 0


def test_reviewer_rwkv_undo_restores_resident_runtime_state() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.resident_restores: list[
                tuple[RwkvReviewIdentity, RwkvReviewerStateSnapshot]
            ] = []

        def restore_warm_up_state(
            self,
            identity: RwkvReviewIdentity,
            snapshot: RwkvReviewerStateSnapshot,
        ) -> None:
            self.resident_restores.append((identity, snapshot))

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    counter = _UndoCounter(reviewer)
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    counter.set(1)
    record_reviewer_answer(reviewer, card, ease=3)
    record_collection_undo(_undo_result(counter=1, next_counter=2))

    assert len(runtime.resident_restores) == 1
    identity, snapshot = runtime.resident_restores[0]
    assert identity == RwkvReviewIdentity(1, 10, 100, 1000)
    assert snapshot.card_state is None
    assert snapshot.note_state is None
    assert snapshot.deck_state is None
    assert snapshot.preset_state is None
    assert snapshot.global_state is None


def test_reviewer_rwkv_undo_queues_restored_cards_in_reverse_answer_order() -> None:
    reviewer = SimpleNamespace(_answeredIds=[1, 2, 3])

    rwkv_scheduler.queue_reviewer_undo_card_ids(reviewer, [3, 2])

    assert reviewer._answeredIds == [1]
    assert rwkv_scheduler.reviewer_has_undo_card_ids(reviewer)
    assert rwkv_scheduler.pop_reviewer_undo_card_id(reviewer) == 3
    assert rwkv_scheduler.reviewer_has_undo_card_ids(reviewer)
    assert rwkv_scheduler.pop_reviewer_undo_card_id(reviewer) == 2
    assert not rwkv_scheduler.reviewer_has_undo_card_ids(reviewer)
    assert rwkv_scheduler.pop_reviewer_undo_card_id(reviewer) is None


def test_reviewer_rwkv_undo_invalidates_transient_scores() -> None:
    runtime = _SharedReviewRuntime()
    set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    rpc = _RwkvQueueScoreRpc()
    rpc.active_scores[1] = 0.77
    reviewer = _rwkv_reviewer(rpc=rpc)
    reviewer._answeredIds = [1]
    reviewer._rwkv_review_prediction = RwkvReviewerPrediction(
        card_id=1,
        retrievability=0.45,
        review_enabled=True,
        interval_override_used=False,
    )

    rwkv_scheduler._rwkv_review_queue_score_maps[100] = {1: 0.77}
    rwkv_scheduler._rwkv_review_queue_target_maps[100] = {1: 0.90}
    rwkv_scheduler._rwkv_review_queue_score_generations[100] = 12
    rwkv_scheduler._rwkv_review_queue_score_config_keys[100] = (
        (),
        (),
    )
    cache_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=reviewer,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    assert cache_key is not None
    rwkv_scheduler._rwkv_review_input_batch_cache(reviewer)[cache_key] = (
        rwkv_scheduler.RwkvReviewInputBatchBuild(
            inputs_by_batch_size={
                512: [(1, _rwkv_review_input(card_id=1, note_id=10))]
            },
            loaded_rows=1,
            parsed_cards=1,
            cards_with_state=1,
            disabled_config_cards=0,
            eligible_cards=1,
            deck_configs=1,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        )
    )

    rwkv_scheduler.queue_reviewer_undo_card_ids(reviewer, [1])

    assert reviewer._answeredIds == []
    assert rpc.calls[-1] == {"deck_id": 0, "scores": []}
    assert rpc.card_info_calls[-1] == {"card_id": 1, "retrievability": None}
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {}
    assert rwkv_scheduler._rwkv_review_queue_target_maps == {}
    assert rwkv_scheduler._rwkv_review_queue_score_generations == {}
    assert rwkv_scheduler._rwkv_review_queue_score_config_keys == {}
    assert rwkv_scheduler._rwkv_review_input_batch_cache(reviewer) == {}
    rows = rwkv_card_info_rows(
        reviewer=reviewer,
        card=_rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        fallback_source="FSRS",
    )
    assert dict(rows)["RWKV computed R"] == "45%"
    assert runtime.queries == [(1, None, None)]
    assert rpc.active_score_calls == []


def test_reviewer_rwkv_record_undo_does_not_clear_scores_without_reviewer() -> None:
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


def test_rwkv_grade_order_enforcement_defaults_on_and_reads_rwkv_key() -> None:
    assert rwkv_scheduler._rwkv_review_enforce_grade_order_config({}) is True
    assert (
        rwkv_scheduler._rwkv_review_enforce_grade_order_config(
            {
                "jschoreels.rwkv": {
                    "rwkv_review_enforce_grade_order": False,
                }
            }
        )
        is False
    )


def test_reviewer_rwkv_curve_only_uses_curve_prediction_for_grade_intervals() -> None:
    class Backend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def predict_review_retrievability(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("RWKV-Instant-only prediction should not be used")

        def predict_review_uncached(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.calls.append("curve")
            return RwkvReviewPrediction(
                retrievability=0.62,
                interval_overrides=RwkvIntervalOverride(
                    again=1,
                    hard=4,
                    good=9,
                    easy=18,
                ),
            )

        def review_answered(
            self,
            *,
            reviewer: object,
            card: object,
            ease: int,
        ) -> None:
            pass

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert rwkv_review_enabled(reviewer, card) is True
    assert updated.good.normal.review.scheduled_days == 9
    assert backend.calls == ["curve"]
    diagnostics = current_reviewer_diagnostics(
        reviewer,
        card,
        fallback_source="FSRS",
    )
    assert diagnostics is not None
    assert diagnostics.retrievability == pytest.approx(0.62)
    assert diagnostics.retrievability_source == "RWKV"


def test_reviewer_rwkv_instant_only_keeps_fsrs_intervals() -> None:
    class Backend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def predict_review_retrievability(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            self.calls.append("instant")
            return RwkvReviewPrediction(retrievability=0.62)

        def predict_review_uncached(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("RWKV-Curve prediction should not be used")

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer(
        rwkv_review_enabled=False,
        rwkv_review_instant_order_enabled=True,
    )
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    states = SchedulingStates()
    states.good.CopyFrom(_normal_review_state(interval=3, fuzz_delta=3))

    updated = update_reviewer_scheduling_states(states, reviewer, card)

    assert rwkv_review_enabled(reviewer, card) is False
    assert rwkv_scheduler.rwkv_review_active(reviewer, card) is True
    assert updated.good.normal.review.scheduled_days == 3
    assert updated.good.normal.review.fuzz_delta_days == 3
    assert backend.calls == ["instant"]
    diagnostics = current_reviewer_diagnostics(
        reviewer,
        card,
        fallback_source="FSRS",
    )
    assert diagnostics is not None
    assert diagnostics.retrievability == pytest.approx(0.62)
    assert diagnostics.retrievability_source == "RWKV"


def test_prepare_reviewer_queue_order_works_with_rwkv_curve_disabled() -> None:
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
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        rwkv_curve_enabled=False,
    )
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


def test_prepare_reviewer_queue_order_uses_backend_deck_review_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.68 for _ in requests]

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.deck_row_calls.append(
                {
                    "deck_id": deck_id,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
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
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=2,
            )

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            raise AssertionError("queue scoring should use backend deck rows")

    class Decks:
        def get_current_id(self) -> int:
            return 100

        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            raise AssertionError("queue scoring should use backend deck rows")

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1000,
                "rwkvReviewEnabled": True,
                "rwkvReviewInstantOrderEnabled": True,
                "rwkvReviewBatchSize": 64,
                "reviewOrder": 7,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(
                now=42 * 86_400 + 100,
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=rpc,
                db=DB(),
                decks=Decks(),
                sched=Scheduler(),
            )
        )
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.deck_row_calls == [
        {"deck_id": 100, "include_disabled_decks": False, "include_new_cards": False}
    ]
    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    scores = rpc.calls[0]["scores"]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.68)),
    ]
    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        1
    ]


@pytest.mark.parametrize(
    "new_gather_priority",
    [
        rwkv_scheduler._NEW_GATHER_PRIORITY_ASCENDING_RETRIEVABILITY,
        rwkv_scheduler._NEW_GATHER_PRIORITY_DESCENDING_RETRIEVABILITY,
    ],
)
def test_prepare_reviewer_queue_order_scores_new_gather_retrievability(
    monkeypatch: pytest.MonkeyPatch,
    new_gather_priority: int,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.74, 0.51]

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.deck_row_calls.append(
                {
                    "deck_id": deck_id,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=0,
                        card_queue=0,
                        card_due=50,
                        interval_days=0,
                        ease_factor=0,
                        reps=0,
                        lapses=0,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="new",
                        target_retention=0.86,
                        batch_size=512,
                    ),
                    SimpleNamespace(
                        card_id=2,
                        note_id=20,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=2,
                        card_queue=2,
                        card_due=51,
                        interval_days=4,
                        ease_factor=2500,
                        reps=5,
                        lapses=1,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="review",
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    ),
                ],
                loaded_cards=2,
                cards_with_supported_state=2,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=2,
            )

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=0,
        new_gather_priority=new_gather_priority,
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_prepare_reviewer_backend_for_review", lambda reviewer: True
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rwkv_scheduler.reviewer_queue_order_enabled(reviewer)
    assert rpc.deck_row_calls == [
        {"deck_id": 100, "include_disabled_decks": False, "include_new_cards": True}
    ]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in cast(list[object], rpc.calls[0]["scores"])
    ] == [(1, pytest.approx(0.74)), (2, pytest.approx(0.51))]
    assert runtime.query_inputs[0].current_normal_state_kind == "new"


def test_prepare_reviewer_queue_order_reuses_backend_deck_review_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.deck_row_calls.append(
                {
                    "deck_id": deck_id,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
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
                        current_elapsed_days=39,
                        current_elapsed_seconds=39 * 86_400,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            raise AssertionError("queue scoring should use backend deck rows")

    class Decks:
        def get_current_id(self) -> int:
            return 100

        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            raise AssertionError("queue scoring should use backend deck rows")

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1000,
                "rwkvReviewEnabled": True,
                "rwkvReviewInstantOrderEnabled": True,
                "rwkvReviewBatchSize": 64,
                "reviewOrder": 7,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(
                now=42 * 86_400 + 100,
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )

    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=rpc,
                db=DB(),
                decks=Decks(),
                sched=Scheduler(),
            )
        )
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        prepare_reviewer_queue_order(reviewer)
        backend.review_input_answered(
            replace(runtime.query_inputs[0], is_query=False, ease=3)
        )
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.deck_row_calls == [
        {"deck_id": 100, "include_disabled_decks": False, "include_new_cards": False},
    ]
    assert len(rpc.calls) == 3
    assert [
        getattr(cast(list[object], call["scores"])[0], "retrievability")
        for call in rpc.calls
    ] == [pytest.approx(0.45), pytest.approx(0.45), pytest.approx(0.55)]
    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        1,
        1,
    ]


def test_prepare_reviewer_queue_order_refreshes_answered_cached_backend_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(elapsed_days: int) -> SimpleNamespace:
        return SimpleNamespace(
            card_id=1,
            note_id=10,
            deck_id=100,
            preset_id="addon-preset",
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
            current_elapsed_days=elapsed_days,
            current_elapsed_seconds=elapsed_days * 86_400,
            target_retention=0.86,
            batch_size=512,
        )

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.deck_row_calls: list[dict[str, object]] = []
            self.card_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.deck_row_calls.append(
                {
                    "deck_id": deck_id,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[row(39)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

        def rwkv_review_input_rows_for_cards(
            self,
            *,
            card_ids: list[int],
            include_suspended_review: bool,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.card_row_calls.append(
                {
                    "card_ids": list(card_ids),
                    "include_suspended_review": include_suspended_review,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[row(0)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        rwkv_min_intervening_reviews=2,
    )
    reviewer._answeredIds = []
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    dynamic_resolution_sizes: list[int] = []

    def resolve_dynamic_targets(
        reviewer: object,
        input_build: rwkv_scheduler.RwkvReviewInputBatchBuild,
    ) -> rwkv_scheduler.RwkvReviewInputBatchBuild:
        if input_build.dynamic_desired_retentions_resolved:
            return input_build
        dynamic_resolution_sizes.append(
            sum(len(inputs) for inputs in input_build.inputs_by_batch_size.values())
        )
        return replace(input_build, dynamic_desired_retentions_resolved=True)

    monkeypatch.setattr(
        rwkv_scheduler,
        "_resolve_dynamic_desired_retentions_for_input_build",
        resolve_dynamic_targets,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        prepare_reviewer_queue_order(reviewer)
        reviewer._answeredIds.append(1)
        prepare_reviewer_queue_order(reviewer)
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.deck_row_calls == [
        {"deck_id": 100, "include_disabled_decks": False, "include_new_cards": False},
    ]
    assert rpc.card_row_calls == [
        {
            "card_ids": [1],
            "include_suspended_review": False,
            "include_disabled_decks": False,
            "include_new_cards": False,
        }
    ]
    assert [
        review_input.current_elapsed_days for review_input in runtime.query_inputs
    ] == [
        39,
        0,
    ]
    assert dynamic_resolution_sizes == [1, 1]
    scores = cast(list[object], rpc.calls[-1]["scores"])
    assert scores[0].intervening_reviews == 0


def test_cached_queue_inputs_add_newly_eligible_answered_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(card_id: int, elapsed_days: int) -> SimpleNamespace:
        return SimpleNamespace(
            card_id=card_id,
            note_id=card_id * 10,
            deck_id=100,
            preset_id="addon-preset",
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
            current_elapsed_days=elapsed_days,
            current_elapsed_seconds=elapsed_days * 86_400,
            target_retention=0.86,
            batch_size=512,
        )

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.card_row_calls: list[list[int]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                rows=[row(1, 39)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

        def rwkv_review_input_rows_for_cards(
            self,
            *,
            card_ids: list[int],
            include_suspended_review: bool,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.card_row_calls.append(list(card_ids))
            return SimpleNamespace(
                rows=[row(2, 0)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    backend = RwkvStatefulReviewerBackend(_SharedReviewRuntime())
    rpc = Rpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        rwkv_min_intervening_reviews=2,
    )
    reviewer._answeredIds = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "_warm_up_reviewer_backend",
        lambda reviewer: True,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        reviewer._answeredIds.append(2)
        prepare_reviewer_queue_order(reviewer)
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.card_row_calls == [[2]]
    assert set(rpc.active_scores) == {1, 2}


def test_cached_queue_input_refresh_retries_after_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer._answeredIds = [2]
    cache_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=reviewer,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    assert cache_key is not None
    cached = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={512: [(1, _rwkv_review_input(card_id=1, note_id=10))]},
        loaded_rows=1,
        parsed_cards=1,
        cards_with_state=1,
        disabled_config_cards=0,
        eligible_cards=1,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    rwkv_scheduler._rwkv_review_input_batch_cache(reviewer)[cache_key] = cached
    refreshed = replace(
        cached,
        inputs_by_batch_size={512: [(2, _rwkv_review_input(card_id=2, note_id=20))]},
    )
    responses = iter([None, refreshed])
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_input_batches_from_backend_for_ids",
        lambda **kwargs: next(responses),
    )

    first = rwkv_scheduler._cached_rwkv_review_input_batch_build(reviewer, cache_key)
    second = rwkv_scheduler._cached_rwkv_review_input_batch_build(reviewer, cache_key)

    assert first is not None
    assert first.session_answered_ids == ()
    assert second is not None
    assert second.session_answered_ids == (2,)
    assert {
        card_id
        for inputs in second.inputs_by_batch_size.values()
        for card_id, _ in inputs
    } == {1, 2}


def test_cached_queue_input_refresh_excludes_cards_from_other_decks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer = _rwkv_reviewer(rpc=_RwkvQueueScoreRpc())
    reviewer._answeredIds = [2]
    reviewer.mw.col.db = SimpleNamespace(
        list=lambda sql: [] if "from cards" in sql else pytest.fail(sql)
    )
    cache_key = rwkv_scheduler._rwkv_review_input_batch_cache_key(
        reviewer=reviewer,
        deck_id=100,
        batch_size_override=512,
        include_new_cards=False,
    )
    assert cache_key is not None
    cached = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={512: [(1, _rwkv_review_input(card_id=1, note_id=10))]},
        loaded_rows=1,
        parsed_cards=1,
        cards_with_state=1,
        disabled_config_cards=0,
        eligible_cards=1,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    rwkv_scheduler._rwkv_review_input_batch_cache(reviewer)[cache_key] = cached
    outside_input = replace(
        _rwkv_review_input(card_id=2, note_id=20),
        identity=RwkvReviewIdentity(
            card_id=2,
            note_id=20,
            deck_id=200,
            preset_id=2000,
        ),
    )
    refreshed = replace(
        cached,
        inputs_by_batch_size={512: [(2, outside_input)]},
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_input_batches_from_backend_for_ids",
        lambda **kwargs: refreshed,
    )

    result = rwkv_scheduler._cached_rwkv_review_input_batch_build(
        reviewer,
        cache_key,
    )

    assert result is not None
    assert {
        card_id
        for inputs in result.inputs_by_batch_size.values()
        for card_id, _ in inputs
    } == {1}
    assert result.session_answered_ids == (2,)


def test_current_deck_count_scoring_uses_active_reviewer_answered_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(elapsed_days: int) -> SimpleNamespace:
        return SimpleNamespace(
            card_id=1,
            note_id=10,
            deck_id=100,
            preset_id="addon-preset",
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
            current_elapsed_days=elapsed_days,
            current_elapsed_seconds=elapsed_days * 86_400,
            target_retention=0.86,
            batch_size=512,
        )

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.card_row_calls: list[list[int]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                rows=[row(39)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

        def rwkv_review_input_rows_for_cards(
            self,
            *,
            card_ids: list[int],
            include_suspended_review: bool,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.card_row_calls.append(list(card_ids))
            return SimpleNamespace(
                rows=[row(0)],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    reviewer._answeredIds = [1]
    reviewer.mw.reviewer = reviewer
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        rwkv_scheduler.prepare_current_deck_review_queue_scores(
            reviewer.mw,
            reason="overview counts",
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.card_row_calls == [[1]]
    scores = cast(list[object], rpc.calls[-1]["scores"])
    assert scores[0].intervening_reviews == 0
    assert runtime.query_inputs[-1].current_elapsed_days == 0


def test_prepare_reviewer_queue_order_candidate_refresh_scores_stale_window() -> None:
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
            return RwkvReviewPrediction(retrievability=card.id / 1000)

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        batch_size=64,
        card_count=65,
        rwkv_candidate_refresh_enabled=True,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        stale_scores = [(1, 0.99)] + [
            (card_id, card_id / 100) for card_id in range(2, 66)
        ]
        rwkv_scheduler._set_rwkv_review_queue_scores(
            reviewer,
            100,
            stale_scores,
            fresh_for_backend_state=False,
        )
        rpc.calls.clear()

        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert backend.predicted_card_ids == list(range(2, 66))
    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    scores = rpc.calls[0]["scores"]
    score_pairs = [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ]
    assert score_pairs[0] == (1, pytest.approx(0.99))
    assert score_pairs[1] == (2, pytest.approx(0.002))
    assert score_pairs[-1] == (65, pytest.approx(0.065))


def test_prepare_reviewer_queue_order_skips_when_instant_order_disabled() -> None:
    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("RWKV-Instant scoring should be disabled")

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        rwkv_instant_order_enabled=False,
    )
    previous_backend = set_reviewer_backend(Backend())
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    assert rpc.calls[0]["scores"] == []


def test_refresh_answered_card_queue_score_replaces_stale_score() -> None:
    class Backend:
        def __init__(self) -> None:
            self.retrievability_by_card_id = {1: 0.20, 2: 0.40}

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=self.retrievability_by_card_id[card.id]
            )

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
        backend.retrievability_by_card_id[1] = 0.95
        rwkv_scheduler.refresh_answered_card_queue_score(reviewer, reviewer.cards[1])
    finally:
        set_reviewer_backend(previous_backend)

    assert len(rpc.calls) == 1
    assert len(rpc.patch_calls) == 1
    patch = rpc.patch_calls[0]
    assert patch.deck_id == 100
    assert patch.card_id == 1
    assert patch.score.retrievability == pytest.approx(0.95)
    assert rpc.active_scores == {
        1: pytest.approx(0.95),
        2: pytest.approx(0.40),
    }


def test_answered_card_queue_score_patch_can_remove_score() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75)],
    )

    assert rwkv_scheduler._patch_answered_card_rwkv_review_queue_score(
        reviewer,
        100,
        1,
        None,
        target_retention=None,
    )

    assert not rpc.patch_calls[0].HasField("score")
    assert rpc.active_scores == {2: pytest.approx(0.75)}
    assert rwkv_scheduler._rwkv_review_queue_score_map_for_deck(
        reviewer,
        100,
    ) == {2: pytest.approx(0.75)}


def test_invalidate_reviewer_queue_for_card_answer_preserves_queue_scores() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75)],
    )
    rpc.calls.clear()

    rwkv_scheduler.invalidate_reviewer_queue_for_card_answer(
        reviewer,
        reviewer.cards[1],
    )

    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    scores = rpc.calls[0]["scores"]
    assert isinstance(scores, list)
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [(1, pytest.approx(0.25)), (2, pytest.approx(0.75))]


def test_invalidate_reviewer_queue_for_child_card_preserves_current_deck_scores() -> (
    None
):
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    reviewer.cards[1].did = 101

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75)],
    )
    rpc.calls.clear()

    rwkv_scheduler.invalidate_reviewer_queue_for_card_answer(
        reviewer,
        reviewer.cards[1],
    )

    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    scores = rpc.calls[0]["scores"]
    assert isinstance(scores, list)
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [(1, pytest.approx(0.25)), (2, pytest.approx(0.75))]


def test_update_reviewer_queue_intervening_reviews_patches_session_cards_only() -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        card_count=4,
        rwkv_min_intervening_reviews=1,
    )
    reviewer._answeredIds = [1, 3, 1, 2]

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75), (4, 0.50)],
    )
    rpc.calls.clear()

    rwkv_scheduler.update_reviewer_queue_intervening_reviews(
        reviewer,
        reviewer.cards[2],
    )

    assert rpc.calls == []
    assert len(rpc.intervening_calls) == 1
    request = rpc.intervening_calls[0]
    assert request.deck_id == 100
    assert [(item.card_id, item.intervening_reviews) for item in request.items] == [
        (1, 1),
        (2, 0),
    ]


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


def test_set_rwkv_review_queue_scores_prefers_raw_backend() -> None:
    class Rpc:
        def __init__(self) -> None:
            self.raw_calls: list[scheduler_pb2.RwkvReviewQueueScoresRequest] = []

        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            request = scheduler_pb2.RwkvReviewQueueScoresRequest()
            request.ParseFromString(message)
            self.raw_calls.append(request)
            return b""

        def set_rwkv_review_queue_scores(
            self,
            *,
            deck_id: int,
            scores: list[object],
        ) -> None:
            raise AssertionError("raw queue score setter should be used")

    rpc = Rpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=rpc)))

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75)],
    )

    assert len(rpc.raw_calls) == 1
    request = rpc.raw_calls[0]
    assert request.deck_id == 100
    assert [(score.card_id, score.retrievability) for score in request.scores] == [
        (1, pytest.approx(0.25)),
        (2, pytest.approx(0.75)),
    ]


def test_set_rwkv_review_queue_scores_includes_session_intervening_reviews() -> None:
    class Rpc:
        def __init__(self) -> None:
            self.raw_calls: list[scheduler_pb2.RwkvReviewQueueScoresRequest] = []

        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            request = scheduler_pb2.RwkvReviewQueueScoresRequest()
            request.ParseFromString(message)
            self.raw_calls.append(request)
            return b""

    rpc = Rpc()
    reviewer = SimpleNamespace(
        _answeredIds=[1, 3, 1, 2],
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=rpc,
                decks=SimpleNamespace(
                    config_dict_for_deck_id=lambda deck_id: {
                        "rwkvReviewMinInterveningReviews": 2,
                    }
                ),
            )
        ),
    )

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75), (3, 0.60), (4, 0.50)],
    )

    request = rpc.raw_calls[0]
    scores_by_card_id = {score.card_id: score for score in request.scores}
    assert scores_by_card_id[1].intervening_reviews == 1
    assert scores_by_card_id[2].intervening_reviews == 0
    assert not scores_by_card_id[3].HasField("intervening_reviews")
    assert not scores_by_card_id[4].HasField("intervening_reviews")


def test_set_rwkv_review_queue_scores_includes_revlog_intervening_reviews_for_deck_tree() -> (
    None
):
    class Rpc:
        def __init__(self) -> None:
            self.raw_calls: list[scheduler_pb2.RwkvReviewQueueScoresRequest] = []

        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            request = scheduler_pb2.RwkvReviewQueueScoresRequest()
            request.ParseFromString(message)
            self.raw_calls.append(request)
            return b""

    class DB:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def all(self, sql: str, *args: object) -> list[tuple[int]]:
            self.calls.append((sql, args))
            assert "from revlog r" in sql
            assert "join cards c on c.id = r.cid" in sql
            assert "c.did in (100,101)" in sql
            assert args == (3,)
            return [(2,), (1,), (2,)]

    class Decks:
        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            assert deck_id == 100
            return [100, 101]

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {"rwkvReviewMinInterveningReviews": 3}

    rpc = Rpc()
    db = DB()
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=rpc,
                db=db,
                decks=Decks(),
            )
        )
    )

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75), (3, 0.50)],
    )

    assert len(db.calls) == 1
    request = rpc.raw_calls[0]
    scores_by_card_id = {score.card_id: score for score in request.scores}
    assert scores_by_card_id[2].intervening_reviews == 0
    assert scores_by_card_id[1].intervening_reviews == 1
    assert not scores_by_card_id[3].HasField("intervening_reviews")


def test_set_rwkv_review_queue_scores_includes_target_retention() -> None:
    class Rpc:
        def __init__(self) -> None:
            self.raw_calls: list[scheduler_pb2.RwkvReviewQueueScoresRequest] = []

        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            request = scheduler_pb2.RwkvReviewQueueScoresRequest()
            request.ParseFromString(message)
            self.raw_calls.append(request)
            return b""

    rpc = Rpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=rpc)))

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25), (2, 0.75)],
        target_retentions_by_card_id={1: 0.50, 2: 1.25, 3: 0.40},
    )

    request = rpc.raw_calls[0]
    scores_by_card_id = {score.card_id: score for score in request.scores}
    assert scores_by_card_id[1].target_retention == pytest.approx(0.50)
    assert not scores_by_card_id[2].HasField("target_retention")
    target_map = rwkv_scheduler._rwkv_review_queue_target_map_for_deck(
        reviewer,
        100,
    )
    assert target_map is not None
    assert set(target_map) == {1}
    assert target_map[1] == pytest.approx(0.50)


def test_set_rwkv_review_queue_scores_replaces_cached_deck_scores() -> None:
    class Rpc:
        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            return b""

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=Rpc())))

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25)],
    )
    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        200,
        [(2, 0.75)],
    )

    assert rwkv_scheduler._rwkv_review_queue_score_map_for_deck(reviewer, 100) is None
    deck_scores = rwkv_scheduler._rwkv_review_queue_score_map_for_deck(reviewer, 200)
    assert deck_scores is not None
    assert deck_scores[2] == pytest.approx(0.75)
    fresh_scores = rwkv_scheduler._fresh_rwkv_review_queue_score_map(reviewer)
    assert fresh_scores[2] == pytest.approx(0.75)
    assert list(fresh_scores) == [2]


def test_clear_rwkv_review_queue_scores_clears_cached_deck_scores() -> None:
    class Rpc:
        def set_rwkv_review_queue_scores_raw(self, message: bytes) -> bytes:
            return b""

    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=Rpc())))

    rwkv_scheduler._set_rwkv_review_queue_scores(
        reviewer,
        100,
        [(1, 0.25)],
    )
    rwkv_scheduler._clear_rwkv_review_queue_scores(reviewer, deck_id=200)

    assert rwkv_scheduler._rwkv_review_queue_score_map_for_deck(reviewer, 100) is None
    assert rwkv_scheduler._fresh_rwkv_review_queue_score_map(reviewer) == {}


def test_install_async_reviewer_queue_order_discards_stale_generation() -> None:
    class Backend:
        def state_generation(self) -> int:
            return 1

    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=rpc)))
    result = rwkv_scheduler.RwkvReviewQueueOrderAsyncResult(
        context=rwkv_scheduler.RwkvReviewQueueContext(
            collection_key=(1, 2),
            selected_deck_id=100,
            deck_id=100,
            deck_scope=(100,),
            days_elapsed=42,
            next_day_at=43 * 86_400,
            config_key="",
            dynamic_desired_retention_generation=0,
            study_queue_generation=0,
        ),
        deck_id=100,
        reason="review queue",
        state_generation=0,
        scores=((1, 0.25),),
        input_build=rwkv_scheduler.RwkvReviewInputBatchBuild(
            inputs_by_batch_size={},
            loaded_rows=0,
            parsed_cards=0,
            cards_with_state=0,
            disabled_config_cards=0,
            eligible_cards=0,
            deck_configs=0,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        ),
        cache_hits=0,
        runtime_requests=1,
        warmup_elapsed_ms=0.0,
        build_elapsed_ms=0.0,
        score_elapsed_ms=0.0,
    )
    previous_backend = set_reviewer_backend(Backend())
    try:
        installed = rwkv_scheduler.install_reviewer_queue_order_async_result(
            reviewer,
            result,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert installed is False
    assert rpc.calls == []


def test_async_reviewer_queue_order_scores_resident_inputs() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.resident_card_ids: list[list[int]] = []

        def predict_retrievability_many_from_warm_up(
            self,
            review_inputs: list[RwkvReviewInput],
        ) -> list[float]:
            card_ids = [review_input.identity.card_id for review_input in review_inputs]
            self.resident_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    inputs = (
        (2, _rwkv_review_input(card_id=2, note_id=20)),
        (3, _rwkv_review_input(card_id=3, note_id=30)),
    )
    work = rwkv_scheduler.RwkvReviewQueueOrderAsyncWork(
        context=rwkv_scheduler.RwkvReviewQueueContext(
            collection_key=(1, 2),
            selected_deck_id=100,
            deck_id=100,
            deck_scope=(100,),
            days_elapsed=42,
            next_day_at=43 * 86_400,
            config_key="",
            dynamic_desired_retention_generation=0,
            study_queue_generation=0,
        ),
        deck_id=100,
        reason="review queue",
        batch_size=512,
        state_generation=0,
        input_build=rwkv_scheduler.RwkvReviewInputBatchBuild(
            inputs_by_batch_size={512: list(inputs)},
            loaded_rows=2,
            parsed_cards=2,
            cards_with_state=2,
            disabled_config_cards=0,
            eligible_cards=2,
            deck_configs=1,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        ),
        inputs_by_card_id=inputs,
        predictions=(None, None),
        requests_by_index=(),
        resident_inputs_by_index=((0, inputs[0][1]), (1, inputs[1][1])),
        cache_hits=0,
        warmup_elapsed_ms=0.0,
        build_elapsed_ms=0.0,
    )

    result = rwkv_scheduler.score_reviewer_queue_order_async_work(work)
    backend.cache_review_input_predictions(result.prediction_cache_entries)
    cached = backend.cached_retrievability_inputs_from_warm_up(
        [(0, inputs[0][1]), (1, inputs[1][1])]
    )

    assert result.scores == ((2, pytest.approx(0.20)), (3, pytest.approx(0.30)))
    assert result.runtime_requests == 2
    assert cached is not None
    cached_predictions, misses, cache_hits = cached
    assert [
        prediction.retrievability for prediction in cached_predictions if prediction
    ] == [
        pytest.approx(0.20),
        pytest.approx(0.30),
    ]
    assert misses == []
    assert cache_hits == 2

    overlapping_inputs = (
        *inputs,
        (4, _rwkv_review_input(card_id=4, note_id=40)),
    )
    overlapping_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={512: list(overlapping_inputs)},
        loaded_rows=3,
        parsed_cards=3,
        cards_with_state=3,
        disabled_config_cards=0,
        eligible_cards=3,
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
        dynamic_desired_retentions_resolved=True,
    )
    overlapping_work = rwkv_scheduler._rwkv_review_queue_async_work_from_input_build(
        reviewer=SimpleNamespace(),
        deck_id=10,
        reason="parent prewarm",
        batch_size=512,
        state_generation=0,
        context=replace(work.context, deck_id=10),
        input_build=overlapping_build,
        warmup_elapsed_ms=0.0,
        build_start=time.monotonic(),
        fresh_for_backend_state=False,
    )

    assert overlapping_work is not None
    assert overlapping_work.cache_hits == 2
    assert len(overlapping_work.resident_inputs_by_index) == 1
    overlapping_result = rwkv_scheduler.score_reviewer_queue_order_async_work(
        overlapping_work
    )
    assert overlapping_result.scores == (
        (2, pytest.approx(0.20)),
        (3, pytest.approx(0.30)),
        (4, pytest.approx(0.40)),
    )
    assert runtime.resident_card_ids == [[2, 3], [4]]


def test_prewarm_reviewer_queue_score_cache_scores_parent_scope() -> None:
    class Runtime(_SharedReviewRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.retrievability_card_ids: list[list[int]] = []

        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            card_ids = [request.review_input.identity.card_id for request in requests]
            self.retrievability_card_ids.append(card_ids)
            return [0.10 * card_id for card_id in card_ids]

    cards = {
        1: _rwkv_card(card_id=1, note_id=10, duration_millis=1234),
        2: _rwkv_card(card_id=2, note_id=20, duration_millis=1234),
        3: _rwkv_card(card_id=3, note_id=30, duration_millis=1234),
    }
    cards[1].did = 100
    cards[2].did = 101
    cards[3].did = 200

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            assert "queue = ?" in sql
            assert args == (2,)
            did_start = sql.index("did in (") + len("did in (")
            did_end = sql.index(")", did_start)
            deck_ids = {int(deck_id) for deck_id in sql[did_start:did_end].split(",")}
            return [
                card.id
                for card in cards.values()
                if card.did in deck_ids and card.queue == 2
            ]

        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            assert "from cards" in sql
            id_start = sql.index("id in (") + len("id in (")
            id_end = sql.index(")", id_start)
            card_ids = {int(card_id) for card_id in sql[id_start:id_end].split(",")}
            data = json.dumps({"lrt": 4 * 86_400})
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
                    data,
                )
                for card in cards.values()
                if card.id in card_ids
            ]

    class Decks:
        def get_current_id(self) -> int:
            return 100

        def get(self, deck_id: int) -> dict[str, object]:
            names = {
                10: "Parent",
                100: "Parent::Child",
                101: "Parent::Child::Grandchild",
                200: "Parent::Sibling",
            }
            return {"id": deck_id, "name": names[deck_id]}

        def id_for_name(self, name: str, create: bool = True) -> int | None:
            return {"Parent": 10, "Parent::Child": 100}.get(name)

        def all_names_and_ids(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(id=deck_id) for deck_id in (10, 100, 101, 200)]

        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            if deck_id == 100:
                return [100, 101]
            if deck_id == 10:
                return [10, 100, 101, 200]
            raise AssertionError(f"unexpected deck {deck_id}")

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return {
                "id": deck_id * 10,
                "rwkvReviewEnabled": True,
                "rwkvReviewInstantOrderEnabled": True,
                "reviewOrder": 7,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    col = SimpleNamespace(
        _backend=_RwkvQueueScoreRpc(),
        db=DB(),
        decks=Decks(),
        sched=Scheduler(),
    )
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    previous_backend = set_reviewer_backend(backend)
    try:
        rwkv_scheduler._reviewer_backend_warmup_keys.add((id(backend), id(col)))
        assert rwkv_scheduler._rwkv_score_prewarm_deck_ids(
            reviewer,
            include_parent_scope=False,
        ) == [100]
        prewarm_reviewer_queue_score_cache(reviewer, reason="test")
    finally:
        set_reviewer_backend(previous_backend)

    assert runtime.retrievability_card_ids == [[1, 2], [1, 2, 3]]
    assert rwkv_scheduler._rwkv_review_queue_score_maps == {}


def test_async_score_prewarm_releases_collection_while_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []
    collection_flags: list[bool] = []
    cached_deck_ids: list[int] = []
    finished: list[bool] = []

    def prepare(
        reviewer: object,
        *,
        deck_id: int,
        reason: str,
    ) -> SimpleNamespace | None:
        events.append(("prepare", deck_id))
        if deck_id == 100:
            return None
        return SimpleNamespace(
            deck_id=deck_id,
            input_build=SimpleNamespace(searched_rows=3),
        )

    def score(work: SimpleNamespace) -> SimpleNamespace:
        events.append(("score", work.deck_id))
        return SimpleNamespace(
            deck_id=work.deck_id,
            scores=((1, 0.5), (2, 0.6)),
        )

    def cache_predictions(
        reviewer: object,
        result: SimpleNamespace,
    ) -> bool:
        cached_deck_ids.append(result.deck_id)
        return True

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], object],
            on_done: Callable[[Future[object]], None],
            *,
            uses_collection: bool,
        ) -> None:
            collection_flags.append(uses_collection)
            future: Future[object] = Future()
            try:
                future.set_result(task())
            except Exception as error:
                future.set_exception(error)
            on_done(future)

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_score_prewarm_work_for_deck", prepare)
    monkeypatch.setattr(rwkv_scheduler, "score_reviewer_queue_order_async_work", score)
    monkeypatch.setattr(
        rwkv_scheduler,
        "cache_reviewer_queue_order_async_result_predictions",
        cache_predictions,
    )

    rwkv_scheduler._prewarm_rwkv_review_scores_for_decks_async(
        SimpleNamespace(),
        [100, 10],
        reason="test",
        taskman=Taskman(),
        on_done=lambda: finished.append(True),
    )

    assert events == [("prepare", 100), ("prepare", 10), ("score", 10)]
    assert collection_flags == [True, True, False, True]
    assert cached_deck_ids == [10]
    assert finished == [True]


def test_deck_browser_count_scopes_are_disjoint_and_prioritize_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = SimpleNamespace(
        children=[
            SimpleNamespace(
                deck_id=10,
                children=[SimpleNamespace(deck_id=11, children=[])],
            ),
            SimpleNamespace(
                deck_id=20,
                children=[SimpleNamespace(deck_id=21, children=[])],
            ),
        ]
    )
    enabled_decks = {10, 21}
    monkeypatch.setattr(rwkv_scheduler, "_current_deck_id", lambda reviewer: 11)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_deck_config_for_deck_id",
        lambda reviewer, deck_id: {
            "rwkvReviewEnabled": deck_id in enabled_decks,
            "rwkvReviewInstantOrderEnabled": deck_id in enabled_decks,
            "reviewOrder": 0,
        },
    )

    assert rwkv_scheduler.deck_browser_rwkv_count_scope_ids(
        SimpleNamespace(),
        tree,
    ) == (10, 21)


def test_deck_browser_counts_score_off_collection_and_update_incrementally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []
    collection_flags: list[bool] = []
    updates: list[tuple[int, int | None]] = []
    installed_scores: list[int] = []
    due_tree_calls = 0
    context = object()

    def prepare(
        reviewer: object,
        *,
        deck_id: int,
        reason: str,
    ) -> SimpleNamespace | None:
        events.append(("prepare", deck_id))
        if deck_id == 20:
            return None
        return SimpleNamespace(deck_id=deck_id, context=context)

    def score(work: SimpleNamespace) -> SimpleNamespace:
        events.append(("score", work.deck_id))
        return SimpleNamespace(
            deck_id=work.deck_id,
            context=work.context,
            state_generation=0,
            scores=((work.deck_id, 0.5),),
            target_retentions_by_card_id={},
        )

    def install(
        reviewer: object,
        deck_id: int,
        scores: Sequence[tuple[int, float]],
        *,
        target_retentions_by_card_id: Mapping[int, float],
    ) -> None:
        installed_scores.append(deck_id)

    class Scheduler:
        def deck_due_tree(self) -> SimpleNamespace:
            nonlocal due_tree_calls
            due_tree_calls += 1
            return SimpleNamespace(version=due_tree_calls)

    class Taskman:
        def run_in_background(
            self,
            task: Callable[[], object],
            on_done: Callable[[Future[object]], None],
            *,
            uses_collection: bool,
        ) -> None:
            collection_flags.append(uses_collection)
            future: Future[object] = Future()
            try:
                future.set_result(task())
            except Exception as error:
                future.set_exception(error)
            on_done(future)

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_score_prewarm_work_for_deck", prepare)
    monkeypatch.setattr(rwkv_scheduler, "score_reviewer_queue_order_async_work", score)
    monkeypatch.setattr(rwkv_scheduler, "_set_rwkv_deck_count_scores", install)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend_state_generation", lambda: 0)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_queue_context",
        lambda reviewer, deck_id: context,
    )
    mw = SimpleNamespace(
        col=SimpleNamespace(sched=Scheduler()),
        taskman=Taskman(),
    )

    rwkv_scheduler.prepare_deck_browser_rwkv_counts_incrementally(
        mw,
        [10, 20, 30],
        should_continue=lambda: True,
        on_update=lambda deck_id, tree: updates.append(
            (deck_id, tree.version if tree is not None else None)
        ),
    )

    assert events == [
        ("prepare", 10),
        ("score", 10),
        ("prepare", 20),
        ("prepare", 30),
        ("score", 30),
    ]
    assert collection_flags == [True, False, True, True, True, False, True]
    assert installed_scores == [10, 30]
    assert updates == [(10, 1), (20, None), (30, 2)]


def test_deck_browser_pending_rwkv_scopes_render_review_counts_as_ellipsis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "")
    from aqt.deckbrowser import DeckBrowser

    child = SimpleNamespace(
        deck_id=11,
        new_count=3,
        learn_count=4,
        review_count=5,
        children=[],
    )
    pending_scope = SimpleNamespace(
        deck_id=10,
        new_count=1,
        learn_count=2,
        review_count=60,
        children=[child],
    )
    ready_scope = SimpleNamespace(
        deck_id=20,
        new_count=6,
        learn_count=7,
        review_count=8,
        children=[],
    )
    tree = SimpleNamespace(children=[pending_scope, ready_scope])
    scripts: list[str] = []
    browser = DeckBrowser.__new__(DeckBrowser)
    browser.web = SimpleNamespace(eval=scripts.append)
    browser._render_data = SimpleNamespace(tree=tree)
    browser._rwkv_pending_deck_ids = browser._deck_ids_in_rwkv_scopes(tree, [10])

    browser._render_rwkv_deck_counts()

    assert browser._rwkv_pending_deck_ids == {10, 11}
    assert "[[10, 1, 2, null], [11, 3, 4, null], [20, 6, 7, 8]]" in scripts[0]


def test_deck_browser_keeps_pending_counts_when_cache_load_is_deferred() -> None:
    from aqt.deckbrowser import DeckBrowser

    renders: list[set[int]] = []
    browser = DeckBrowser.__new__(DeckBrowser)
    browser.mw = SimpleNamespace(state="deckBrowser")
    browser._rwkv_count_generation = 2
    browser._rwkv_pending_deck_ids = {10, 11}
    browser._render_rwkv_deck_counts = lambda: renders.append(
        set(browser._rwkv_pending_deck_ids)
    )

    browser._finish_rwkv_count_refresh(2, clear_pending=False)
    browser._finish_rwkv_count_refresh(2, clear_pending=True)

    assert renders == [{10, 11}, set()]


def test_overview_renders_pending_rwkv_review_count_as_ellipsis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt.overview import Overview
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "")
    scheduler = SimpleNamespace(
        counts=lambda: (1, 2, 4_000),
        deck_due_tree=lambda _deck_id: SimpleNamespace(
            new_count=1,
            learn_count=2,
            review_count=4_000,
        ),
    )
    overview = Overview.__new__(Overview)
    overview.mw = SimpleNamespace(
        col=SimpleNamespace(
            sched=scheduler,
            decks=SimpleNamespace(get_current_id=lambda: 10),
            v3_scheduler=lambda: True,
        ),
        button=lambda *args, **kwargs: "",
    )
    overview._rwkv_counts_pending = True

    table = overview._table()

    assert "<span class=review-count>…</span>" in table
    assert "4000" not in table


def test_backend_row_failure_logs_exception_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Backend:
        def rwkv_review_input_rows_for_cards_raw(self, message: bytes) -> bytes:
            raise RuntimeError("backend row failure")

    with caplog.at_level("DEBUG", logger="aqt.rwkv_scheduler"):
        response = rwkv_scheduler._rwkv_review_input_rows_backend_response(
            Backend(),
            card_ids=[1],
            include_suspended_review=False,
            include_new_cards=False,
        )

    assert response is None
    record = next(
        record
        for record in caplog.records
        if record.message == "failed to load RWKV review input rows from backend"
    )
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError


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
            if "from revlog" in sql:
                assert "ease between 1 and 4" in sql
                assert "type = 4" in sql
                return []
            assert "from cards" in sql
            assert "id in (1,2,3,4,5)" in sql
            assert "type = 2 and queue in (2, -1)" in sql
            assert "type = 1 and queue in (1, 3)" in sql
            assert "type = 3 and queue in (1, 3)" in sql
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
    assert rpc.preset_id_calls == [[1, 3, 4, 5]]
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


def test_stats_graph_scores_build_direct_review_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.64 for _ in requests]

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1000,
                "rwkvReviewEnabled": True,
                "rwkvReviewFirstReviewElapsedFromCardCreation": True,
                "desiredRetention": 0.86,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            if "from revlog" in sql:
                return []
            assert "from cards" in sql
            data = json.dumps({"lrt": 4 * 86_400})
            return [(1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data)]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=SimpleNamespace(),
                db=DB(),
                decks=Decks(),
                sched=Scheduler(),
            )
        )
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_stats_graph_scheduling_states",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stats scoring should build RWKV inputs directly")
        ),
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        scores = rwkv_scheduler._rwkv_stats_graph_scores(
            reviewer=reviewer, card_ids=[1]
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert scores == [(1, pytest.approx(0.64))]
    assert len(runtime.query_inputs) == 1
    review_input = runtime.query_inputs[0]
    assert review_input.identity == RwkvReviewIdentity(
        card_id=1,
        note_id=10,
        deck_id=100,
        preset_id=1000,
    )
    assert review_input.current_state_kind == "normal"
    assert review_input.current_normal_state_kind == "review"
    assert review_input.current_elapsed_days == 39
    assert review_input.target_retentions == (0.86, 0.86, 0.86, 0.86)


def test_stats_graph_scores_build_direct_new_card_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 42 * 86_400 + 100
    card_id = (now - 90_000) * 1000

    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.83 for _ in requests]

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return {
                "id": 1000,
                "rwkvReviewEnabled": True,
                "rwkvReviewFirstReviewElapsedFromCardCreation": True,
                "desiredRetention": 0.86,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(
                now=now,
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            if "from revlog" in sql:
                return []
            assert "from cards" in sql
            return [(card_id, 10, 100, 0, 0, 0, 50, 0, 0, 0, 0, 0, "")]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=SimpleNamespace(),
                db=DB(),
                decks=Decks(),
                sched=Scheduler(),
            )
        )
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        scores = rwkv_scheduler._rwkv_stats_graph_scores(
            reviewer=reviewer,
            card_ids=[card_id],
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert scores == [(card_id, pytest.approx(0.83))]
    assert len(runtime.query_inputs) == 1
    review_input = runtime.query_inputs[0]
    assert review_input.current_state_kind == "normal"
    assert review_input.current_normal_state_kind == "new"
    assert review_input.current_elapsed_days == 1
    assert review_input.current_elapsed_seconds == 90_000


def test_stats_graph_scores_backend_card_rows_can_include_new_cards() -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.83 for _ in requests]

    class Rpc:
        def rwkv_review_input_rows_for_cards(
            self,
            *,
            card_ids: list[int],
            include_suspended_review: bool,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            assert card_ids == [1]
            assert include_suspended_review is True
            assert include_disabled_decks is False
            assert include_new_cards is True
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
                        card_type=0,
                        card_queue=0,
                        card_due=50,
                        interval_days=0,
                        ease_factor=0,
                        reps=0,
                        lapses=0,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="new",
                        current_elapsed_days=1,
                        current_elapsed_seconds=90_000,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(
                now=42 * 86_400 + 100,
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=Rpc(),
                db=object(),
                sched=Scheduler(),
            )
        )
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        scores = rwkv_scheduler._rwkv_stats_graph_scores(
            reviewer=reviewer,
            card_ids=[1],
            include_new_cards=True,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert scores == [(1, pytest.approx(0.83))]
    assert len(runtime.query_inputs) == 1
    assert runtime.query_inputs[0].current_normal_state_kind == "new"


def test_stats_graph_scores_use_backend_review_input_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.71 for _ in requests]

    class Rpc:
        def rwkv_review_input_rows_for_cards(
            self,
            *,
            card_ids: list[int],
            include_suspended_review: bool,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            assert card_ids == [1]
            assert include_suspended_review is True
            assert include_disabled_decks is False
            assert include_new_cards is False
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
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
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
            )

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            raise AssertionError("backend row path should not query cards directly")

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=Rpc(),
                db=DB(),
                sched=Scheduler(),
            )
        )
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_stats_graph_scheduling_states",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("backend rows should build RWKV inputs directly")
        ),
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        scores = rwkv_scheduler._rwkv_stats_graph_scores(
            reviewer=reviewer,
            card_ids=[1],
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert scores == [(1, pytest.approx(0.71))]
    assert len(runtime.query_inputs) == 1
    review_input = runtime.query_inputs[0]
    assert review_input.identity == RwkvReviewIdentity(
        card_id=1,
        note_id=10,
        deck_id=100,
        preset_id=rwkv_scheduler._stable_preset_id("addon-preset"),
    )
    assert review_input.current_state_kind == "normal"
    assert review_input.current_normal_state_kind == "review"
    assert review_input.current_elapsed_days == 39
    assert review_input.current_elapsed_seconds is None
    assert review_input.target_retentions == (0.86, 0.86, 0.86, 0.86)


def test_prepare_stats_uses_backend_search_review_input_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.69 for _ in requests]

    class Rpc(_RwkvQueueScoreRpc):
        def __init__(self) -> None:
            super().__init__()
            self.search_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_search(
            self,
            *,
            search: str,
            include_suspended_review: bool,
            include_disabled_decks: bool,
        ) -> SimpleNamespace:
            self.search_row_calls.append(
                {
                    "search": search,
                    "include_suspended_review": include_suspended_review,
                    "include_disabled_decks": include_disabled_decks,
                }
            )
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
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
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=2,
            )

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class Collection:
        def __init__(self, rpc: Rpc) -> None:
            self._backend = rpc
            self.db = object()
            self.sched = Scheduler()

        def find_cards(self, search: str, order: bool = False) -> list[int]:
            raise AssertionError("stats graph should use backend search rows")

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_stats",
        lambda reviewer: True,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.search_row_calls == [
        {
            "search": "rated:7",
            "include_suspended_review": True,
            "include_disabled_decks": False,
        }
    ]
    assert len(rpc.stats_calls) == 1
    scores = rpc.stats_calls[0]["scores"]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.69)),
    ]
    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        1
    ]


def test_prepare_stats_reuses_fresh_review_queue_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.69 for _ in requests]

    class Rpc(_RwkvQueueScoreRpc):
        def rwkv_review_input_rows_for_search(
            self,
            *,
            search: str,
            include_suspended_review: bool,
            include_disabled_decks: bool,
        ) -> SimpleNamespace:
            assert search == "rated:7"
            assert include_suspended_review is True
            assert include_disabled_decks is False
            rows = []
            for card_id in (1, 2):
                rows.append(
                    SimpleNamespace(
                        card_id=card_id,
                        note_id=card_id * 10,
                        deck_id=100,
                        preset_id="addon-preset",
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
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    )
                )
            return SimpleNamespace(
                rows=rows,
                loaded_cards=2,
                cards_with_supported_state=2,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=2,
            )

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class Collection:
        def __init__(self, rpc: Rpc) -> None:
            self._backend = rpc
            self.db = object()
            self.sched = Scheduler()

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_stats",
        lambda reviewer: True,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        rwkv_scheduler._set_rwkv_review_queue_scores(reviewer, 100, [(1, 0.42)])
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)

    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        2
    ]
    scores = rpc.stats_calls[0]["scores"]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.42)),
        (2, pytest.approx(0.69)),
    ]


def test_prepare_stats_ignores_stale_review_queue_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.69 for _ in requests]

    class Rpc(_RwkvQueueScoreRpc):
        def rwkv_review_input_rows_for_search(
            self,
            *,
            search: str,
            include_suspended_review: bool,
            include_disabled_decks: bool,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="addon-preset",
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
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=512,
                    )
                ],
                loaded_cards=1,
                cards_with_supported_state=1,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=1,
            )

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class Collection:
        def __init__(self, rpc: Rpc) -> None:
            self._backend = rpc
            self.db = object()
            self.sched = Scheduler()

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_stats",
        lambda reviewer: True,
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        rwkv_scheduler._set_rwkv_review_queue_scores(
            reviewer,
            100,
            [(1, 0.42)],
            fresh_for_backend_state=False,
        )
        prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)

    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        1
    ]
    scores = rpc.stats_calls[0]["scores"]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [
        (1, pytest.approx(0.69)),
    ]


def test_stats_graph_scores_filters_disabled_decks_in_sql() -> None:
    class Runtime(_SharedReviewRuntime):
        def predict_retrievability_many(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[float]:
            self.query_inputs.extend(request.review_input for request in requests)
            return [0.73 for _ in requests]

    class Decks:
        def all_names_and_ids(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(id=100), SimpleNamespace(id=200)]

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return {
                "id": deck_id * 10,
                "rwkvReviewEnabled": deck_id == 100,
                "desiredRetention": 0.86,
            }

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class Rpc:
        def __init__(self) -> None:
            self.preset_id_calls: list[list[int]] = []

        def get_fsrs_preset_ids_for_cards(
            self,
            card_ids: list[int],
        ) -> SimpleNamespace:
            self.preset_id_calls.append(card_ids)
            return SimpleNamespace(
                items=[
                    SimpleNamespace(card_id=card_id, preset_id="1000")
                    for card_id in card_ids
                ]
            )

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            if "from revlog" in sql:
                return []
            assert "from cards" in sql
            assert "id in (1,2)" in sql
            assert "case when odid != 0 then odid else did end" in sql
            assert "in (100)" in sql
            data = json.dumps({"lrt": 4 * 86_400})
            return [(1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data)]

    runtime = Runtime()
    backend = RwkvStatefulReviewerBackend(runtime)
    rpc = Rpc()
    reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=rpc,
                db=DB(),
                decks=Decks(),
                sched=Scheduler(),
            )
        )
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        scores = rwkv_scheduler._rwkv_stats_graph_scores(
            reviewer=reviewer,
            card_ids=[1, 2],
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert scores == [(1, pytest.approx(0.73))]
    assert [review_input.identity.card_id for review_input in runtime.query_inputs] == [
        1
    ]
    assert rpc.preset_id_calls == [[1]]


def test_rwkv_reschedule_items_use_current_interval_and_s90() -> None:
    class Backend:
        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            raise AssertionError("reschedule should batch predictions")

        def predict_reviews(
            self,
            candidates: list[RwkvReviewCandidate],
        ) -> list[RwkvReviewPrediction]:
            return [
                RwkvReviewPrediction(
                    retrievability=0.50,
                    current_interval=11 + candidate.card.id,
                    current_s90=21 + candidate.card.id,
                    interval_overrides=RwkvIntervalOverride(
                        again=1,
                        hard=2,
                        good=999,
                        easy=4,
                    ),
                    s90_overrides=RwkvIntervalOverride(
                        again=5,
                        hard=6,
                        good=999,
                        easy=8,
                    ),
                )
                for candidate in candidates
            ]

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            if deck_id == 100:
                return {
                    "id": 1000,
                    "rwkvReviewEnabled": True,
                    "rwkvReviewBatchSize": 64,
                }
            if deck_id == 200:
                return {
                    "id": 2000,
                    "rwkvReviewEnabled": False,
                    "rwkvReviewInstantOrderEnabled": True,
                }
            raise AssertionError(f"unexpected deck {deck_id}")

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            if "from revlog" in sql:
                return []
            assert "from cards" in sql
            assert "id in (1,2,3)" in sql
            data = json.dumps({"lrt": 4 * 86_400})
            return [
                (1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data),
                (2, 20, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data),
                (3, 30, 200, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data),
            ]

    class Collection:
        def __init__(self, rpc: _RwkvQueueScoreRpc) -> None:
            self._backend = rpc
            self.db = DB()
            self.decks = Decks()
            self.sched = Scheduler()

    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    previous_backend = set_reviewer_backend(Backend())
    try:
        items = rwkv_scheduler._rwkv_review_reschedule_items(reviewer, [1, 2, 3])
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.preset_id_calls == [[1, 2, 3]]
    assert [
        (item.card_id, item.interval_days, item.elapsed_days, item.s90)
        for item in items
    ] == [
        (1, 12, 39, 22),
        (2, 13, 39, 23),
    ]


def test_rwkv_reschedule_uses_fixed_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = [
        (card_id, _rwkv_review_input(card_id=card_id, note_id=card_id + 1000))
        for card_id in range(1, 130)
    ]
    input_build = rwkv_scheduler.RwkvReviewInputBatchBuild(
        inputs_by_batch_size={8192: inputs},
        loaded_rows=len(inputs),
        parsed_cards=len(inputs),
        cards_with_state=len(inputs),
        disabled_config_cards=0,
        eligible_cards=len(inputs),
        deck_configs=1,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
    )
    prediction_batches: list[tuple[int, int]] = []

    def predict(
        inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
        *,
        batch_size: int,
    ) -> list[RwkvReviewPrediction]:
        prediction_batches.append((len(inputs_by_card_id), batch_size))
        return [
            RwkvReviewPrediction(
                retrievability=0.5,
                current_interval=10,
                current_s90=20,
            )
            for _ in inputs_by_card_id
        ]

    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_review_predictions_for_inputs",
        predict,
    )

    items = rwkv_scheduler._rwkv_review_reschedule_items_from_input_build(input_build)

    assert len(items) == 129
    assert prediction_batches == [(128, 128), (1, 128)]


def test_rwkv_reschedule_card_ids_use_requested_deck_tree() -> None:
    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            assert "did in (100,101)" in sql
            assert "queue = ?" in sql
            assert args == (2,)
            return [1, 2]

    class Decks:
        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            assert deck_id == 100
            return [100, 101]

    mw = SimpleNamespace(col=SimpleNamespace(db=DB(), decks=Decks()))

    assert rwkv_scheduler._rwkv_review_reschedule_card_ids(mw, deck_id=100) == [
        1,
        2,
    ]


def test_rwkv_reschedule_items_use_backend_deck_review_rows() -> None:
    class Backend:
        def __init__(self) -> None:
            self.requests: list[RwkvReviewPredictionRequest] = []

        def cached_review_input_predictions(
            self,
            inputs_by_index: list[tuple[int, RwkvReviewInput]],
        ) -> tuple[
            list[RwkvReviewPrediction | None],
            list[tuple[int, RwkvReviewPredictionRequest]],
            int,
        ]:
            return (
                [None] * len(inputs_by_index),
                [
                    (
                        index,
                        RwkvReviewPredictionRequest(review_input=review_input),
                    )
                    for index, review_input in inputs_by_index
                ],
                0,
            )

        def predict_review_requests(
            self,
            requests: list[RwkvReviewPredictionRequest],
        ) -> list[RwkvReviewPrediction]:
            self.requests.extend(requests)
            return [
                RwkvReviewPrediction(
                    retrievability=0.50,
                    current_interval=10 + request.review_input.identity.card_id,
                    current_s90=20 + request.review_input.identity.card_id,
                )
                for request in requests
            ]

    class Rpc:
        def __init__(self) -> None:
            self.deck_row_calls: list[dict[str, object]] = []

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            self.deck_row_calls.append(
                {
                    "deck_id": deck_id,
                    "include_disabled_decks": include_disabled_decks,
                    "include_new_cards": include_new_cards,
                }
            )
            return SimpleNamespace(
                rows=[
                    SimpleNamespace(
                        card_id=1,
                        note_id=10,
                        deck_id=100,
                        preset_id="1000",
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
                        current_elapsed_days=39,
                        target_retention=0.86,
                        batch_size=64,
                    ),
                    SimpleNamespace(
                        card_id=2,
                        note_id=20,
                        deck_id=101,
                        preset_id="1000",
                        card_type=2,
                        card_queue=2,
                        card_due=52,
                        interval_days=6,
                        ease_factor=2400,
                        reps=7,
                        lapses=0,
                        day_offset=42,
                        current_state_kind="normal",
                        current_normal_state_kind="review",
                        current_elapsed_days=36,
                        target_retention=0.86,
                        batch_size=64,
                    ),
                ],
                loaded_cards=2,
                cards_with_supported_state=2,
                disabled_config_cards=0,
                deck_configs=1,
                searched_cards=3,
            )

    rpc = Rpc()

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return {
                "id": deck_id * 10,
                "rwkvReviewEnabled": deck_id == 100,
                "rwkvReviewInstantOrderEnabled": deck_id == 101,
            }

    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(_backend=rpc, decks=Decks()))
    )
    backend = Backend()
    previous_backend = set_reviewer_backend(backend)
    try:
        items = rwkv_scheduler._rwkv_review_reschedule_items_for_deck(
            reviewer,
            100,
        )
    finally:
        set_reviewer_backend(previous_backend)

    assert rpc.deck_row_calls == [
        {"deck_id": 100, "include_disabled_decks": False, "include_new_cards": False}
    ]
    assert [request.review_input.identity.card_id for request in backend.requests] == [
        1,
    ]
    assert [
        (
            item.card_id,
            item.interval_days,
            item.elapsed_days,
            item.s90,
            item.target_retention,
        )
        for item in items or []
    ] == [
        (1, 11, 39, 21, pytest.approx(0.86)),
    ]


def test_apply_rwkv_review_reschedule_includes_target_retention() -> None:
    class Rpc:
        def __init__(self) -> None:
            self.requests: list[scheduler_pb2.RwkvReviewRescheduleRequest] = []

        def apply_rwkv_review_reschedule_raw(self, message: bytes) -> bytes:
            request = scheduler_pb2.RwkvReviewRescheduleRequest()
            request.ParseFromString(message)
            self.requests.append(request)
            return b""

    rpc = Rpc()
    mw = SimpleNamespace(col=SimpleNamespace(_backend=rpc))

    rwkv_scheduler._apply_rwkv_review_reschedule(
        mw,
        [
            rwkv_scheduler.RwkvReviewRescheduleItem(
                card_id=1,
                interval_days=12,
                elapsed_days=4,
                s90=9.5,
                target_retention=0.50,
            )
        ],
    )

    item = rpc.requests[0].items[0]
    assert item.card_id == 1
    assert item.target_retention == pytest.approx(0.50)


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
            if "from revlog" in sql:
                assert "ease between 1 and 4" in sql
                assert "type = 4" in sql
                return []
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


def test_prepare_stats_retrievability_scores_reports_pending_after_warmup_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=SimpleNamespace(_backend=rpc)))
    previous_backend = set_reviewer_backend(object())
    monkeypatch.setattr(
        rwkv_scheduler,
        "_prepare_reviewer_backend_for_stats",
        lambda reviewer: False,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend_warmup_pending",
        lambda reviewer: True,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_wait_for_reviewer_backend_warmup",
        lambda reviewer, *, timeout_secs: False,
    )

    try:
        status = prepare_stats_retrievability_scores(reviewer, "rated:7")
    finally:
        set_reviewer_backend(previous_backend)

    assert status == rwkv_scheduler.RwkvStatsPreparationStatus.PENDING
    assert len(rpc.stats_calls) == 1
    assert rpc.stats_calls[0]["search"] == "rated:7"
    assert rpc.stats_calls[0]["scores"] == []


def test_prepare_stats_retrievability_scores_reuses_in_flight_prepare() -> None:
    class Backend:
        def __init__(self) -> None:
            self.predict_calls = 0
            self.started = threading.Event()
            self.release = threading.Event()

        def predict_reviews(
            self,
            candidates: list[RwkvReviewCandidate],
        ) -> list[RwkvReviewPrediction]:
            self.predict_calls += 1
            self.started.set()
            assert self.release.wait(timeout=2)
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

        def state_generation(self) -> int:
            return 7

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
            data = json.dumps({"lrt": 4 * 86_400})
            return [(1, 10, 100, 0, 2, 2, 50, 0, 4, 2500, 5, 1, data)]

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

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=Collection(rpc)))
    previous_backend = set_reviewer_backend(backend)
    errors: list[BaseException] = []

    def prepare() -> None:
        try:
            prepare_stats_retrievability_scores(reviewer, "rated:7")
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=prepare)
    second = threading.Thread(target=prepare)
    try:
        first.start()
        assert backend.started.wait(timeout=2)
        second.start()
        time.sleep(0.01)
        backend.release.set()
        first.join(timeout=2)
        second.join(timeout=2)
    finally:
        backend.release.set()
        set_reviewer_backend(previous_backend)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert backend.predict_calls == 1
    assert len(rpc.stats_calls) == 1
    scores = rpc.stats_calls[0]["scores"]
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in scores
    ] == [(1, pytest.approx(0.64))]


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


def test_prepare_reviewer_queue_order_uses_unknown_elapsed_without_history() -> None:
    class Backend:
        def __init__(self) -> None:
            self.inputs_by_card_id: dict[int, RwkvReviewInput] = {}

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            identity = rwkv_review_identity(reviewer, card)
            assert identity is not None
            self.inputs_by_card_id[card.id] = rwkv_review_input(
                reviewer=reviewer,
                card=card,
                identity=identity,
                ease=None,
            )
            return RwkvReviewPrediction(retrievability=0.50)

        def review_answered(self, *, reviewer: object, card: object, ease: int) -> None:
            raise AssertionError("unexpected answer update")

    backend = Backend()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(
        rpc=rpc,
        review_order=7,
        latest_review_elapsed_days_by_card={},
    )
    previous_backend = set_reviewer_backend(backend)
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert {
        card_id: review_input.current_elapsed_days
        for card_id, review_input in backend.inputs_by_card_id.items()
    } == {1: None, 2: None}
    assert {
        card_id: review_input.current_state_kind
        for card_id, review_input in backend.inputs_by_card_id.items()
    } == {1: None, 2: None}


def test_prepare_reviewer_queue_order_supports_due_day_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _SharedReviewRuntime()
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=0)
    monkeypatch.setattr(
        rwkv_scheduler, "_warm_up_reviewer_backend", lambda reviewer: True
    )
    previous_backend = set_reviewer_backend(RwkvStatefulReviewerBackend(runtime))
    try:
        prepare_reviewer_queue_order(reviewer)
    finally:
        set_reviewer_backend(previous_backend)

    assert rwkv_scheduler.reviewer_queue_order_enabled(reviewer)
    assert len(rpc.calls) == 1
    assert rpc.calls[0]["deck_id"] == 100
    assert [
        (getattr(score, "card_id"), getattr(score, "retrievability"))
        for score in cast(list[object], rpc.calls[0]["scores"])
    ] == [(1, pytest.approx(0.45)), (2, pytest.approx(0.45))]


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
        (
            "RWKV Curve Next S90",
            "Again:3d Hard:4d Good:6d Easy:9d",
        ),
        NEXT_S90_UNAVAILABLE_ROWS[1],
        *RWKV_AFTER_REVIEW_UNAVAILABLE_ROWS,
    ]
    assert runtime.query_inputs[0].current_normal_state_kind == "review"
    assert runtime.query_inputs[0].current_elapsed_days is None
    assert rpc.card_info_calls == [
        {"card_id": 1, "retrievability": pytest.approx(0.45)}
    ]


def test_card_info_reports_rwkv_retrievability_after_review() -> None:
    expected_snapshot = RwkvBackendCacheSnapshot(
        card_states={},
        note_states={},
        deck_states={},
        preset_states={},
        global_state=None,
        runtime_state=b"runtime",
    )

    class Backend:
        def __init__(self) -> None:
            self.answers: list[RwkvReviewInput] = []
            self.queries: list[RwkvReviewInput] = []
            self.call_count = 0

        def predict_review(
            self,
            *,
            reviewer: object,
            card: object,
        ) -> RwkvReviewPrediction:
            return RwkvReviewPrediction(
                retrievability=0.45,
                button_probabilities=(0.55, 0.10, 0.20, 0.15),
            )

        def cache_snapshot(self) -> RwkvBackendCacheSnapshot:
            return expected_snapshot

        def predict_retrievability_after_reviews(
            self,
            *,
            answers: Sequence[RwkvReviewInput],
            inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
            snapshot: RwkvBackendCacheSnapshot,
        ) -> list[list[tuple[int, float]]]:
            self.call_count += 1
            assert snapshot is expected_snapshot
            score_batches: list[list[tuple[int, float]]] = []
            for answer in answers:
                self.answers.append(answer)
                self.queries.extend(
                    review_input for _, review_input in inputs_by_card_id
                )
                assert answer.ease is not None
                score_batches.append(
                    [
                        (
                            card_id,
                            0.6
                            + answer.ease * 0.05
                            + (0.01 if review_input.current_elapsed_seconds else 0),
                        )
                        for card_id, review_input in inputs_by_card_id
                    ]
                )
            return score_batches

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    rwkv_scheduler._reviewer_backend_warmup_keys.add((id(backend), id(reviewer.mw.col)))
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)
    card.time_taken = lambda capped=True: (_ for _ in ()).throw(
        AssertionError("Card Info after-review predictions should not read answer time")
    )

    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    ) == [
        ("RWKV computed R", "45%"),
        RWKV_BUTTON_PROBABILITY_ROW,
        ("Retrievability source", "RWKV"),
        *NEXT_S90_UNAVAILABLE_ROWS,
        (
            "RWKV : R After Review",
            "Again:65% Hard:70% Good:75% Easy:80%",
        ),
        (
            "RWKV : R After 10min",
            "Again:66% Hard:71% Good:76% Easy:81%",
        ),
    ]
    assert backend.call_count == 1
    assert [answer.ease for answer in backend.answers] == [1, 2, 3, 4]
    assert all(query.is_query for query in backend.queries)
    assert all(query.ease is None for query in backend.queries)
    assert [
        (query.current_elapsed_days, query.current_elapsed_seconds)
        for query in backend.queries
    ] == [(0, 0), (0, 600)] * 4


def test_card_info_reports_rwkv_and_fsrs_next_s90_for_filtered_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Backend:
        def __init__(self) -> None:
            self.review_inputs: list[RwkvReviewInput] = []

        def predict_reviews(
            self,
            candidates: list[RwkvReviewCandidate],
        ) -> list[RwkvReviewPrediction]:
            candidate = candidates[0]
            identity = rwkv_review_identity(candidate.reviewer, candidate.card)
            assert identity is not None
            self.review_inputs.append(
                rwkv_review_input(
                    reviewer=candidate.reviewer,
                    card=candidate.card,
                    identity=identity,
                    ease=None,
                )
            )
            return [
                RwkvReviewPrediction(
                    retrievability=0.45,
                    s90_overrides=RwkvIntervalOverride(
                        again=2,
                        hard=5,
                        good=10,
                        easy=20,
                    ),
                )
            ]

    backend = Backend()
    set_reviewer_backend(backend)
    reviewer = _rwkv_reviewer()
    now = 42 * 86_400 + 100
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: float(now))
    states = reviewer.mw.col.sched.states
    states.current.filtered.rescheduling.original_state.review.elapsed_days = 7
    states.again.filtered.rescheduling.original_state.relearning.learning.memory_state.stability = 1.25
    states.hard.normal.learning.memory_state.stability = 2.5
    states.good.normal.review.memory_state.stability = 3.75
    states.easy.filtered.preview.scheduled_secs = 600
    rwkv_scheduler._reviewer_backend_warmup_keys.add((id(backend), id(reviewer.mw.col)))

    card = _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=1234,
        last_review_time=now - 30,
    )

    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    ) == [
        ("RWKV computed R", "45%"),
        ("Retrievability source", "RWKV"),
        (
            "RWKV Curve Next S90",
            "Again:2d Hard:5d Good:10d Easy:20d",
        ),
        (
            "FSRS Next S90",
            "Again:1.25d Hard:2.5d Good:3.75d Easy:Unavailable",
        ),
        *RWKV_AFTER_REVIEW_UNAVAILABLE_ROWS,
    ]
    assert len(backend.review_inputs) == 1
    assert backend.review_inputs[0].card_type == 4
    assert backend.review_inputs[0].current_state_kind == "filtered"
    assert backend.review_inputs[0].current_normal_state_kind is None
    assert backend.review_inputs[0].current_elapsed_days == 0
    assert backend.review_inputs[0].current_elapsed_seconds == 30


def test_future_prediction_snapshot_only_includes_referenced_states() -> None:
    first = _rwkv_review_input(card_id=1, note_id=10)
    second = replace(
        _rwkv_review_input(card_id=2, note_id=20),
        identity=RwkvReviewIdentity(
            card_id=2,
            note_id=20,
            deck_id=200,
            preset_id=2000,
        ),
    )
    snapshot = RwkvBackendCacheSnapshot(
        card_states={1: b"card-1", 2: b"card-2", 3: b"unused-card"},
        note_states={10: b"note-10", 20: b"note-20", 30: b"unused-note"},
        deck_states={100: b"deck-100", 200: b"deck-200", 300: b"unused-deck"},
        preset_states={
            1000: b"preset-1000",
            2000: b"preset-2000",
            3000: b"unused-preset",
        },
        global_state=b"global",
        runtime_state=b"runtime",
    )

    assert _workload_snapshot_for_review_inputs(snapshot, (first, second)) == (
        [(1, b"card-1"), (2, b"card-2")],
        [(10, b"note-10"), (20, b"note-20")],
        [(100, b"deck-100"), (200, b"deck-200")],
        [(1000, b"preset-1000"), (2000, b"preset-2000")],
        b"global",
        b"runtime",
    )


def test_bulk_card_load_qualifies_cards_data_column() -> None:
    queries: list[str] = []

    class DB:
        def all(self, sql: str) -> list[tuple[object, ...]]:
            queries.append(sql)
            return []

    reviewer = SimpleNamespace(
        mw=SimpleNamespace(col=SimpleNamespace(db=DB())),
    )

    assert (
        rwkv_scheduler._rwkv_card_rows_for_ids(
            reviewer,
            [1],
            reason="test",
        )
        == []
    )
    assert len(queries) == 1
    assert "cards.data" in queries[0]


def test_card_info_refreshes_after_global_rwkv_state_changes() -> None:
    runtime = _SharedReviewRuntime()
    backend = RwkvStatefulReviewerBackend(runtime)
    set_reviewer_backend(backend)
    rpc = _RwkvQueueScoreRpc()
    rpc.active_scores[1] = 0.67
    reviewer = _rwkv_reviewer(rpc=rpc)
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    first_rows = rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    )

    backend.review_answered(
        reviewer=reviewer,
        card=_rwkv_card(card_id=2, note_id=20, duration_millis=1234),
        ease=1,
    )
    second_rows = rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source="FSRS",
    )

    assert dict(first_rows)["RWKV computed R"] == "45%"
    assert dict(second_rows)["RWKV computed R"] == "55%"
    assert runtime.queries == [
        (1, None, None),
        (1, 1, ("deck", 100, 1)),
    ]
    assert rpc.active_score_calls == []
    assert rpc.card_info_calls == [
        {"card_id": 1, "retrievability": pytest.approx(0.45)},
        {"card_id": 1, "retrievability": pytest.approx(0.55)},
    ]


def test_card_info_uses_shared_card_row_context_for_rwkv_query() -> None:
    class Backend:
        def __init__(self) -> None:
            self.review_inputs: list[RwkvReviewInput] = []

        def predict_reviews(
            self,
            candidates: list[RwkvReviewCandidate],
        ) -> list[RwkvReviewPrediction]:
            candidate = candidates[0]
            identity = rwkv_review_identity(candidate.reviewer, candidate.card)
            assert identity is not None
            self.review_inputs.append(
                rwkv_review_input(
                    reviewer=candidate.reviewer,
                    card=candidate.card,
                    identity=identity,
                    ease=None,
                )
            )
            return [
                RwkvReviewPrediction(
                    retrievability=0.61,
                    interval_overrides=RwkvIntervalOverride(
                        again=1,
                        hard=2,
                        good=4,
                        easy=8,
                    ),
                )
            ]

    backend = Backend()
    set_reviewer_backend(backend)
    rpc = _RwkvQueueScoreRpc()
    reviewer = _rwkv_queue_reviewer(rpc=rpc, review_order=7)
    rwkv_scheduler._reviewer_backend_warmup_keys.add((id(backend), id(reviewer.mw.col)))

    assert rwkv_card_info_rows(
        reviewer=reviewer,
        card=reviewer.cards[2],
        fallback_source="FSRS",
    ) == [
        ("RWKV computed R", "61%"),
        ("Retrievability source", "RWKV"),
        *NEXT_S90_UNAVAILABLE_ROWS,
        *RWKV_AFTER_REVIEW_UNAVAILABLE_ROWS,
    ]
    assert backend.review_inputs[0].current_normal_state_kind == "review"
    assert backend.review_inputs[0].current_elapsed_days == 1
    assert backend.review_inputs[0].card_due == 45
    assert rpc.card_info_calls == [
        {"card_id": 2, "retrievability": pytest.approx(0.61)}
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
        ("Retrievability source", "RWKV"),
        *NEXT_S90_UNAVAILABLE_ROWS,
        *RWKV_AFTER_REVIEW_UNAVAILABLE_ROWS,
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
        *NEXT_S90_UNAVAILABLE_ROWS,
        *RWKV_AFTER_REVIEW_UNAVAILABLE_ROWS,
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
                interval_overrides=RwkvIntervalOverride(
                    again=1,
                    hard=2,
                    good=4,
                    easy=8,
                ),
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
        *NEXT_S90_UNAVAILABLE_ROWS,
        *RWKV_AFTER_REVIEW_UNAVAILABLE_ROWS,
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


def test_srs_benchmark_backend_builds_query_and_answer_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    now = 42 * 86_400 + 100
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: now)
    card = _rwkv_card(
        card_id=1,
        note_id=10,
        duration_millis=1234,
        last_review_time=now - 7 * 86_400,
    )

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
    assert process.answer_rows[0]["duration"] == pytest.approx(1234.0)
    assert process.answer_rows[0]["rating"] == 3


def test_srs_benchmark_backend_warmup_processes_historical_rows() -> None:
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

        def process_row(self, row: dict[str, object]) -> object:
            self.answer_rows.append(row)
            return object()

    process = Process()
    backend = SrsBenchmarkRwkvReviewerBackend(process=process)
    recorded: list[tuple[int, float]] = []
    progress: list[RwkvWarmUpProgress] = []
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
        ],
        review_ids=[123],
        prediction_recorder=lambda review_id, retrievability: recorded.append(
            (review_id, retrievability)
        ),
        progress=progress.append,
    )

    assert recorded == [(123, pytest.approx(0.72))]
    assert progress == [
        RwkvWarmUpProgress(processed_reviews=0, total_reviews=1),
        RwkvWarmUpProgress(processed_reviews=1, total_reviews=1),
    ]
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
    assert len(process.answer_rows) == 1
    assert process.answer_rows[0]["card_id"] == 1
    assert process.answer_rows[0]["rating"] == 3
    assert process.answer_rows[0]["duration"] == pytest.approx(1234.0)


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


def test_srs_benchmark_backend_predict_review_retrievability_skips_curve() -> None:
    from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

    class Probability:
        def __init__(self, value: float) -> None:
            self.value = value

        def item(self) -> float:
            return self.value

    class Process:
        def imm_predict(self, row: dict[str, object]) -> Probability:
            return Probability(0.75)

        def predict_func(self, curve: object, elapsed_seconds: int) -> Probability:
            raise AssertionError("RWKV-Curve should not be used for live grades")

    backend = SrsBenchmarkRwkvReviewerBackend(process=Process())
    reviewer = _rwkv_reviewer()
    card = _rwkv_card(card_id=1, note_id=10, duration_millis=1234)

    prediction = backend.predict_review_retrievability(reviewer=reviewer, card=card)

    assert prediction is not None
    assert prediction.retrievability == pytest.approx(0.75)
    assert prediction.current_interval is None
    assert prediction.current_s90 is None
    assert prediction.interval_overrides == RwkvIntervalOverride()
    assert prediction.s90_overrides == RwkvIntervalOverride()


def test_srs_benchmark_backend_uses_ahead_curve_for_interval_overrides() -> None:
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
    assert before.current_interval is None
    assert before.current_s90 is None
    assert before.interval_overrides == RwkvIntervalOverride()
    assert before.s90_overrides == RwkvIntervalOverride()
    assert after is not None
    assert after.retrievability == pytest.approx(0.80)
    assert after.current_interval == 4
    assert after.current_s90 == 4
    assert after.interval_overrides == RwkvIntervalOverride(
        again=4,
        hard=4,
        good=4,
        easy=4,
    )
    assert after.s90_overrides == RwkvIntervalOverride(
        again=4,
        hard=4,
        good=4,
        easy=4,
    )


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
        ) -> list[
            tuple[
                float,
                int | None,
                int | None,
                tuple[int | None, ...],
                tuple[int | None, ...],
                tuple[float, float, float, float],
            ]
        ]:
            self.requests.append(requests)
            return [
                (0.25, 9, 19, (1, 3, 7, 14), (2, 4, 17, 28), (0.75, 0.05, 0.15, 0.05)),
                (
                    0.75,
                    None,
                    None,
                    (None, None, None, None),
                    (None, None, None, None),
                    (0.25, 0.10, 0.50, 0.15),
                ),
            ]

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
    first, second = predictions
    assert first is not None
    assert first.current_interval == 9
    assert first.current_s90 == 19
    assert first.interval_overrides == RwkvIntervalOverride(
        again=1,
        hard=3,
        good=7,
        easy=14,
    )
    assert first.s90_overrides == RwkvIntervalOverride(
        again=2,
        hard=4,
        good=17,
        easy=28,
    )
    assert first.button_probabilities == pytest.approx((0.75, 0.05, 0.15, 0.05))
    assert second is not None
    assert second.current_interval is None
    assert second.current_s90 is None
    assert second.interval_overrides == RwkvIntervalOverride()
    assert second.s90_overrides == RwkvIntervalOverride()
    assert second.button_probabilities == pytest.approx((0.25, 0.10, 0.50, 0.15))
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
                None,
                None,
                None,
                None,
                True,
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
                None,
                None,
                None,
                None,
                True,
                b"card-2",
                b"note-20",
                b"deck-100",
                b"preset-1000",
                b"global",
            ),
        ]
    ]


def test_embedded_rust_runtime_batches_retrievability_bridge_predictions() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        def __init__(self) -> None:
            self.requests: list[list[tuple[object, ...]]] = []

        def predict_retrievability_many(
            self,
            requests: list[tuple[object, ...]],
        ) -> list[float]:
            self.requests.append(requests)
            return [0.25, 0.75]

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

    retrievabilities = runtime.predict_retrievability_many(requests)

    assert retrievabilities == [pytest.approx(0.25), pytest.approx(0.75)]
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
                None,
                None,
                None,
                None,
                True,
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
                None,
                None,
                None,
                None,
                True,
                b"card-2",
                b"note-20",
                b"deck-100",
                b"preset-1000",
                b"global",
            ),
        ]
    ]


def test_embedded_rust_runtime_prefers_tuple_retrievability_bridge() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        def __init__(self) -> None:
            self.requests: list[list[tuple[object, ...]]] = []

        def predict_retrievability_many_packed(
            self,
            requests: bytes,
            state_columns: tuple[
                list[bytes | None],
                list[bytes | None],
                list[bytes | None],
                list[bytes | None],
                list[bytes | None],
            ],
        ) -> list[float]:
            raise AssertionError("packed path should not be used")

        def predict_retrievability_many(
            self,
            requests: list[tuple[object, ...]],
        ) -> list[float]:
            self.requests.append(requests)
            return [0.25, 0.75]

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

    retrievabilities = runtime.predict_retrievability_many(requests)

    assert retrievabilities == [pytest.approx(0.25), pytest.approx(0.75)]
    assert len(process.requests) == 1
    assert [request[0] for request in process.requests[0]] == [1, 2]


def _warm_up_review_input(*, card_id: int, note_id: int, ease: int) -> RwkvReviewInput:
    return replace(
        _rwkv_review_input(card_id=card_id, note_id=note_id),
        is_query=False,
        ease=ease,
        duration_millis=1234,
    )


def test_embedded_rust_runtime_prefers_packed_warm_up_reviews() -> None:
    import struct

    from aqt.rwkv_srs_benchmark import (
        _PACKED_PREDICTION_REQUEST_HEADER,
        _PACKED_PREDICTION_REQUEST_ROW,
        _PACKED_WARM_UP_REVIEW_MAGIC,
        _RustRwkvRuntime,
    )

    class Process:
        def __init__(self) -> None:
            self.payloads: list[bytes] = []
            self.record_flags: list[bool] = []

        def warm_up_reviews_packed(
            self,
            reviews: bytes,
            record_predictions: bool,
        ) -> list[tuple[int, float]]:
            self.payloads.append(reviews)
            self.record_flags.append(record_predictions)
            return [(0, 0.31 if len(self.payloads) == 1 else 0.42)]

        def warm_up_reviews(self, *args: object) -> list[tuple[int, float]]:
            raise AssertionError("packed warm-up path should be used")

        def warm_up_snapshot(
            self,
        ) -> tuple[object, object, object, object, bytes | None, bytes]:
            return (
                [(1, b"card-1"), (2, b"card-2")],
                [(10, b"note-10")],
                [(100, b"deck-100")],
                [(1000, b"preset-1000")],
                b"global",
                b"runtime",
            )

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process
    recorded: list[tuple[int, float]] = []

    snapshot = runtime.warm_up_reviews(
        [
            _warm_up_review_input(card_id=1, note_id=10, ease=2),
            _warm_up_review_input(card_id=2, note_id=20, ease=3),
        ],
        review_ids=[101, 102],
        prediction_recorder=lambda review_id, retrievability: recorded.append(
            (review_id, retrievability)
        ),
    )

    # Small histories chunk at the progress interval, so each review arrives
    # in its own packed call.
    assert process.record_flags == [True, True]
    assert recorded == [(101, pytest.approx(0.31)), (102, pytest.approx(0.42))]
    assert snapshot.card_states == {1: b"card-1", 2: b"card-2"}
    assert snapshot.runtime_state == b"runtime"

    # note/deck/preset/ease/duration/card_type/day_offset/elapsed presence
    # bits 0-8 set, target retention bits 9-12 clear.
    presence = 0b1_1111_1111
    header = _PACKED_PREDICTION_REQUEST_HEADER.pack(_PACKED_WARM_UP_REVIEW_MAGIC, 1)
    assert process.payloads == [
        header
        + _PACKED_PREDICTION_REQUEST_ROW.pack(
            presence,
            1,
            10,
            100,
            1000,
            0,
            2,
            1234,
            2,
            42,
            7,
            604800,
            0.0,
            0.0,
            0.0,
            0.0,
            1,
        ),
        header
        + _PACKED_PREDICTION_REQUEST_ROW.pack(
            presence,
            2,
            20,
            100,
            1000,
            0,
            3,
            1234,
            2,
            42,
            7,
            604800,
            0.0,
            0.0,
            0.0,
            0.0,
            1,
        ),
    ]
    header_size = struct.calcsize("<8sI")
    assert len(process.payloads[0]) == header_size + _PACKED_PREDICTION_REQUEST_ROW.size


def test_embedded_rust_runtime_warm_up_falls_back_to_tuple_rows() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        def __init__(self) -> None:
            self.calls: list[tuple[list[tuple[object, ...]], bool]] = []

        def warm_up_reviews(
            self,
            rows: list[tuple[object, ...]],
            record_predictions: bool,
        ) -> list[tuple[int, float]]:
            self.calls.append((list(rows), record_predictions))
            return []

        def warm_up_snapshot(
            self,
        ) -> tuple[object, object, object, object, bytes | None, bytes]:
            return ([], [], [], [], None, b"runtime")

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process

    runtime.warm_up_reviews(
        [
            _warm_up_review_input(card_id=1, note_id=10, ease=2),
            _warm_up_review_input(card_id=2, note_id=20, ease=3),
        ]
    )

    # State-only replay keeps a small history together so the native
    # wavefront can schedule all available review chunks.
    assert len(process.calls) == 1
    assert all(record_predictions is False for _, record_predictions in process.calls)
    rows = [row for chunk_rows, _ in process.calls for row in chunk_rows]
    assert [row[0] for row in rows] == [1, 2]
    assert [row[5] for row in rows] == [2, 3]


def test_embedded_rust_runtime_serializes_retrievability_bridge_calls() -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    class Process:
        def __init__(self) -> None:
            self.active = False
            self.entered = threading.Event()
            self.release = threading.Event()
            self.calls: list[list[int]] = []
            self.overlapped = False

        def predict_retrievability_many(
            self,
            requests: list[tuple[object, ...]],
        ) -> list[float]:
            if self.active:
                self.overlapped = True
                raise RuntimeError("Already borrowed")

            self.active = True
            self.calls.append([int(request[0]) for request in requests])
            try:
                if len(self.calls) == 1:
                    self.entered.set()
                    assert self.release.wait(2)
                return [0.25 for _ in requests]
            finally:
                self.active = False

    def request(card_id: int) -> RwkvReviewPredictionRequest:
        return RwkvReviewPredictionRequest(
            review_input=_rwkv_review_input(card_id=card_id, note_id=card_id * 10),
        )

    process = Process()
    runtime = _RustRwkvRuntime.__new__(_RustRwkvRuntime)
    runtime._process = process
    errors: list[Exception] = []
    outputs: list[list[float]] = []

    def predict(card_id: int) -> None:
        try:
            outputs.append(
                list(runtime.predict_retrievability_many([request(card_id)]))
            )
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=lambda: predict(1))
    first.start()
    assert process.entered.wait(2)

    second = threading.Thread(target=lambda: predict(2))
    second.start()
    time.sleep(0.05)
    assert second.is_alive()

    process.release.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert outputs == [[pytest.approx(0.25)], [pytest.approx(0.25)]]
    assert process.calls == [[1], [2]]
    assert not process.overlapped


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
                    interval_overrides=RwkvIntervalOverride(
                        again=2 + review_count,
                        hard=3 + review_count,
                        good=5 + review_count,
                        easy=8 + review_count,
                    ),
                    s90_overrides=RwkvIntervalOverride(
                        again=3 + review_count,
                        hard=4 + review_count,
                        good=6 + review_count,
                        easy=9 + review_count,
                    ),
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
        self.patch_calls: list[
            scheduler_pb2.RwkvAnsweredCardQueueScorePatchRequest
        ] = []
        self.intervening_calls: list[
            scheduler_pb2.RwkvReviewQueueInterveningReviewsRequest
        ] = []
        self.stats_calls: list[dict[str, object]] = []
        self.card_info_calls: list[dict[str, object]] = []
        self.preset_id_calls: list[list[int]] = []
        self.active_scores: dict[int, float] = {}
        self.active_score_calls: list[int] = []

    def set_rwkv_review_queue_scores(
        self,
        *,
        deck_id: int,
        scores: list[object],
    ) -> None:
        self.calls.append({"deck_id": deck_id, "scores": scores})
        if not scores:
            self.active_scores.clear()
        else:
            self.active_scores = {
                getattr(score, "card_id"): getattr(score, "retrievability")
                for score in scores
            }

    def patch_answered_card_rwkv_review_queue_score_raw(
        self,
        message: bytes,
    ) -> bytes:
        request = scheduler_pb2.RwkvAnsweredCardQueueScorePatchRequest()
        request.ParseFromString(message)
        self.patch_calls.append(request)
        if request.HasField("score"):
            self.active_scores[request.card_id] = request.score.retrievability
        else:
            self.active_scores.pop(request.card_id, None)
        return b""

    def update_rwkv_review_queue_intervening_reviews_raw(
        self,
        message: bytes,
    ) -> bytes:
        request = scheduler_pb2.RwkvReviewQueueInterveningReviewsRequest()
        request.ParseFromString(message)
        self.intervening_calls.append(request)
        return b""

    def set_rwkv_stats_graph_scores(
        self,
        *,
        search: str,
        scores: list[object],
    ) -> None:
        self.stats_calls.append({"search": search, "scores": scores})

    def set_rwkv_card_info_score(self, message: Any) -> None:
        card_id = getattr(message, "card_id")
        retrievability = (
            getattr(message, "retrievability")
            if message.HasField("retrievability")
            else None
        )
        self.card_info_calls.append(
            {
                "card_id": card_id,
                "retrievability": retrievability,
            }
        )
        if retrievability is None:
            self.active_scores.pop(card_id, None)
        else:
            self.active_scores[card_id] = retrievability

    def _active_score_response(
        self,
        card_id: int,
    ) -> scheduler_pb2.RwkvRetrievabilityScoreResponse:
        self.active_score_calls.append(card_id)
        score = self.active_scores.get(card_id)
        response = scheduler_pb2.RwkvRetrievabilityScoreResponse()
        if score is not None:
            response.retrievability = score
        return response

    def get_rwkv_retrievability_score_raw(self, message: bytes) -> bytes:
        request = cards_pb2.CardId()
        request.ParseFromString(message)
        return self._active_score_response(request.cid).SerializeToString()

    def get_rwkv_retrievability_score(self, card_id: int) -> object:
        return self._active_score_response(card_id)

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
    new_gather_priority: int | None = None,
    batch_size: int | None = None,
    card_count: int = 2,
    rwkv_config_in_other: bool = False,
    rwkv_curve_enabled: bool = True,
    rwkv_instant_order_enabled: bool = True,
    rwkv_candidate_refresh_enabled: bool = False,
    rwkv_min_intervening_reviews: int = 0,
    latest_review_elapsed_days_by_card: dict[int, int] | None = None,
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

    if latest_review_elapsed_days_by_card is None:
        latest_review_elapsed_days_by_card = {1: 4, 2: 1}

    class DB:
        def list(self, sql: str, *args: object) -> list[int]:
            assert "did in (100,101)" in sql
            assert "queue = ?" in sql
            assert args == (2,)
            return list(cards)

        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            assert args == ()
            id_prefix = "cid in (" if "from revlog" in sql else "id in ("
            requested_ids_start = sql.index(id_prefix) + len(id_prefix)
            requested_ids_end = sql.index(")", requested_ids_start)
            requested_ids = {
                int(card_id)
                for card_id in sql[requested_ids_start:requested_ids_end].split(",")
            }
            assert requested_ids <= set(cards)
            if "from revlog" in sql:
                assert "ease between 1 and 4" in sql
                assert "type in (0, 1, 2, 3, 4, 5)" in sql
                assert "not (type = 3 and factor = 0)" in sql
                return [
                    (
                        card_id,
                        (43 - elapsed_days) * 86_400 * 1000,
                    )
                    for card_id, elapsed_days in latest_review_elapsed_days_by_card.items()
                    if card_id in requested_ids
                ]

            assert "from cards" in sql
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
                if card.id in requested_ids
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
            if new_gather_priority is not None:
                config["newCardGatherPriority"] = new_gather_priority
            if rwkv_config_in_other:
                nested: dict[str, object] = {"rwkv_review_enabled": rwkv_curve_enabled}
                nested["rwkv_review_instant_order_enabled"] = rwkv_instant_order_enabled
                nested["rwkv_review_min_intervening_reviews"] = (
                    rwkv_min_intervening_reviews
                )
                nested["rwkv_review_min_elapsed_secs"] = 0
                if rwkv_candidate_refresh_enabled:
                    nested["rwkv_review_candidate_refresh_enabled"] = True
                if batch_size is not None:
                    nested["rwkv_review_batch_size"] = batch_size
                config["other"] = {"jschoreels.rwkv": nested}
            else:
                config["rwkvReviewEnabled"] = rwkv_curve_enabled
                config["rwkvReviewInstantOrderEnabled"] = rwkv_instant_order_enabled
                config["rwkvReviewMinInterveningReviews"] = rwkv_min_intervening_reviews
                config["rwkvReviewMinElapsedSecs"] = 0
                if rwkv_candidate_refresh_enabled:
                    config["rwkvReviewCandidateRefreshEnabled"] = True
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
    return SimpleNamespace(mw=SimpleNamespace(col=col), cards=cards)


def _rwkv_reviewer(
    *,
    rwkv_review_enabled: bool = True,
    rwkv_review_enforce_grade_order: bool = True,
    rwkv_review_instant_order_enabled: bool = False,
    rwkv_review_dynamic_preset_replay: bool = False,
    rwkv_review_first_review_elapsed_from_card_creation: bool = False,
    resolved_preset_id: str | None = "1000",
    preset_desired_retention: float | None = None,
    deck_desired_retention: float | None = None,
    rpc: _RwkvQueueScoreRpc | None = None,
    historical_review_rows: (list[tuple[object, ...]] | None) = None,
) -> SimpleNamespace:
    if historical_review_rows is not None:
        historical_review_rows = _benchmark_valid_historical_rows(
            historical_review_rows
        )
    states = SchedulingStates()
    states.current.normal.review.elapsed_days = 7

    class Scheduler:
        def __init__(self) -> None:
            self.states = states

        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(
                now=42 * 86_400 + 100,
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )

        def get_scheduling_states(self, card_id: int) -> SchedulingStates:
            return self.states

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return self._config(deck_id)

        def all_config(self) -> list[dict[str, object]]:
            return [self._config(10)]

        def _config(self, deck_id: int) -> dict[str, object]:
            config: dict[str, object] = {
                "id": deck_id * 10,
                "rwkvReviewEnabled": rwkv_review_enabled,
                "rwkvReviewEnforceGradeOrder": rwkv_review_enforce_grade_order,
                "rwkvReviewInstantOrderEnabled": (rwkv_review_instant_order_enabled),
                "rwkvReviewDynamicPresetReplay": rwkv_review_dynamic_preset_replay,
                "rwkvReviewFirstReviewElapsedFromCardCreation": (
                    rwkv_review_first_review_elapsed_from_card_creation
                ),
            }
            if deck_desired_retention is not None:
                config["desiredRetention"] = deck_desired_retention
            return config

    col = SimpleNamespace(
        sched=Scheduler(),
        decks=Decks(),
    )
    if rpc is not None:
        col._backend = rpc
    if historical_review_rows is not None:

        class DB:
            def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
                assert "from revlog r" in sql
                assert "join cards c" in sql
                assert args == ()
                return historical_review_rows

            def execute(self, sql: str) -> None:
                pytest.fail(f"unexpected DB execute: {sql}")

            def executemany(
                self,
                sql: str,
                rows: list[tuple[int, float, str, int, str, int]],
            ) -> None:
                pytest.fail(f"unexpected DB executemany: {sql}, {rows}")

        col.db = DB()
    if resolved_preset_id is not None:
        col.fsrs_preset_for_card = lambda card_id: SimpleNamespace(
            id=resolved_preset_id,
            desired_retention=preset_desired_retention,
        )

    return SimpleNamespace(
        _v3=SimpleNamespace(states=states),
        mw=SimpleNamespace(col=col),
    )


def _rwkv_cache_reviewer(
    *,
    profile_folder: Path,
    rows: list[tuple[int, ...]],
) -> SimpleNamespace:
    rows[:] = cast(list[tuple[int, ...]], _benchmark_valid_historical_rows(rows))
    states = SchedulingStates()
    rwkv_retrievability_rows: list[tuple[int, float, str, int, str, int]] = []
    saved_deck_configs: list[dict[str, object]] = []

    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[int, ...]]:
            if "PRAGMA table_info" in sql:
                if "revlog" in sql:
                    return [
                        (0, "id"),
                        (1, "cid"),
                        (2, "ease"),
                        (3, "ivl"),
                        (4, "lastIvl"),
                        (5, "time"),
                        (6, "factor"),
                        (7, "type"),
                    ]
                return [
                    (0, "revlog_id"),
                    (1, "prediction"),
                    (2, "source"),
                    (3, "updated_at"),
                    (4, "sample_role"),
                    (5, "fold_index"),
                ]
            if "FROM search_stats_rwkv_review_retrievability cache" in sql:
                requested_ids = _sql_integers_inside_first_in_clause(sql)
                return [
                    (review_id, prediction)
                    for review_id, prediction, *_ in rwkv_retrievability_rows
                    if review_id in requested_ids
                ]
            assert "from revlog r" in sql
            assert "join cards c" in sql
            if args:
                assert len(args) == 1
                after_review_id = args[0]
                assert isinstance(after_review_id, int)
                return [row for row in rows if row[0] > after_review_id]
            return list(rows)

        def scalar(self, sql: str, *args: object) -> int | None:
            if "select crt from col" in sql:
                assert args == ()
                return 12345
            if "sample_role =" in sql:
                assert len(args) == 2
                last_review_id, sample_role = args
                assert isinstance(last_review_id, int)
                return next(
                    (
                        1
                        for review_id, prediction, _, _, row_sample_role, _ in rwkv_retrievability_rows
                        if review_id <= last_review_id
                        and 0 <= prediction <= 1
                        and row_sample_role == sample_role
                    ),
                    None,
                )
            assert "from revlog" in sql
            assert len(args) == 1
            if "order by id desc" in sql:
                card_id = args[0]
                assert isinstance(card_id, int)
                review_ids = [
                    row[0]
                    for row in rows
                    if row[1] == card_id
                    and 1 <= row[4] <= 4
                    and (row[6] in (0, 1, 2, 3) or row[6] == 4)
                ]
                return max(review_ids, default=None)

            last_review_id = args[0]
            assert isinstance(last_review_id, int)
            if "search_stats_rwkv_review_retrievability" in sql:
                review_ids = {
                    row[0]
                    for row in rows
                    if row[0] <= last_review_id
                    and 1 <= row[4] <= 4
                    and (row[6] in (0, 1, 2, 3) or row[6] == 4)
                }
                return sum(
                    1
                    for review_id, prediction, *_ in rwkv_retrievability_rows
                    if review_id in review_ids and 0 <= prediction <= 1
                )
            return sum(1 for row in rows if row[0] <= last_review_id)

        def execute(self, sql: str) -> None:
            pytest.fail(f"unexpected DB execute: {sql}")

        def executemany(
            self,
            sql: str,
            cache_rows: list[tuple[int, float, str, int, str, int]],
        ) -> None:
            pytest.fail(f"unexpected DB executemany: {sql}, {cache_rows}")

    class Backend:
        def set_rwkv_review_retrievability_cache_rows(
            self,
            *,
            source: str,
            rows: Sequence[object],
        ) -> None:
            existing = {row[0]: row for row in rwkv_retrievability_rows}
            for row in rows:
                review_id = getattr(row, "revlog_id")
                prediction = getattr(row, "prediction")
                sample_role = getattr(row, "sample_role", "final_fit")
                fold_index = getattr(row, "fold_index", -1)
                existing[review_id] = (
                    review_id,
                    prediction,
                    source,
                    0,
                    sample_role,
                    fold_index,
                )
            rwkv_retrievability_rows[:] = [
                existing[review_id] for review_id in sorted(existing)
            ]

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42, next_day_at=43 * 86_400)

        def get_scheduling_states(self, card_id: int) -> SchedulingStates:
            return states

    class Decks:
        def all_config(self) -> list[dict[str, object]]:
            return [self.config_dict_for_deck_id(100)]

        def deck_and_child_ids(self, deck_id: int) -> list[int]:
            assert deck_id == 100
            return [100]

        def get_config(self, config_id: int) -> dict[str, object] | None:
            if config_id == 1000:
                return self.config_dict_for_deck_id(100)
            return None

        def update_config(self, config: dict[str, object]) -> None:
            saved_deck_configs.append(config)

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            return {
                "id": deck_id * 10,
                "rwkvReviewEnabled": True,
            }

    col = SimpleNamespace(
        db=DB(),
        sched=Scheduler(),
        decks=Decks(),
        _backend=Backend(),
        path=str(profile_folder / "collection.anki2"),
        fsrs_preset_for_card=lambda card_id: SimpleNamespace(id="1000"),
        rwkv_retrievability_rows=rwkv_retrievability_rows,
        saved_deck_configs=saved_deck_configs,
    )
    mw = SimpleNamespace(
        col=col,
        pm=SimpleNamespace(profileFolder=lambda: str(profile_folder)),
    )
    return SimpleNamespace(_v3=SimpleNamespace(states=states), mw=mw)


def _benchmark_valid_historical_rows(
    rows: Sequence[tuple[object, ...]],
) -> list[tuple[object, ...]]:
    seen_cards: set[int] = set()
    normalized: list[tuple[object, ...]] = []
    for row in rows:
        if len(row) < 7 or not isinstance(row[1], int):
            normalized.append(row)
            continue
        card_id = row[1]
        if card_id in seen_cards:
            normalized.append(row)
            continue
        seen_cards.add(card_id)
        normalized.append((*row[:6], 0, *row[7:]))
    return normalized


def _rwkv_sse_harness_review_retrievability(
    col: SimpleNamespace,
    revlog_ids: list[int],
) -> dict[str, object]:
    requested_review_ids = list(dict.fromkeys(revlog_ids))
    if not requested_review_ids:
        return {"column": None, "data": []}

    table = "search_stats_rwkv_review_retrievability"
    reviews = ",".join(str(revlog_id) for revlog_id in requested_review_ids)
    predictions_by_revlog_id = {
        row[0]: row[1]
        for row in col.db.all(f"""
        SELECT cache.revlog_id, cache.prediction
        FROM {table} cache
        WHERE cache.revlog_id IN ({reviews})
        ORDER BY cache.revlog_id
        """)
        if row[0] in requested_review_ids
        and _rwkv_sse_harness_valid_probability(row[1])
    }

    if set(requested_review_ids) - set(predictions_by_revlog_id):
        return {"column": None, "data": []}

    return {
        "column": table,
        "data": sorted(predictions_by_revlog_id.items()),
    }


def _rwkv_sse_harness_valid_probability(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 <= value <= 1
    )


def _sql_integers_inside_first_in_clause(sql: str) -> set[int]:
    _, _, after_in = sql.partition(" IN (")
    inside, _, _ = after_in.partition(")")
    return {int(value.strip()) for value in inside.split(",") if value.strip()}


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
    last_review_time: int | None = None,
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
        last_review_time=last_review_time,
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
