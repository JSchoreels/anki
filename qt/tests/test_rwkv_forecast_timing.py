# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anki import deck_config_pb2, decks_pb2, scheduler_pb2
from aqt import rwkv_history, rwkv_scheduler
from aqt.rwkv_forecast_timing import review_timing_patterns
from aqt.rwkv_srs_benchmark import _RustRwkvRuntime


def test_review_timing_patterns_keep_real_order_and_complete_days() -> None:
    today = 400
    today_start = 2_000_000_000
    rows = [
        ((today_start - 86_400 + 800) * 1000, 399, 2_500),
        ((today_start - 86_400 + 200) * 1000, 399, 1_500),
        ((today_start - 2 * 86_400 + 350) * 1000, 398, 3_000),
        ((today_start + 100) * 1000, 400, 4_000),
        ((today_start - 366 * 86_400 + 100) * 1000, 34, 4_000),
    ]
    assert review_timing_patterns(
        rows, today=today, next_day_at=today_start + 86_400
    ) == [
        (398, [(350_000, 3_000)]),
        (399, [(200_000, 1_500), (800_000, 2_500)]),
    ]


def test_review_timing_patterns_skip_invalid_samples() -> None:
    assert (
        review_timing_patterns(
            [(0, 99, 1_000), (1_000, 99, -1)], today=100, next_day_at=86_400
        )
        == []
    )


def test_review_timing_patterns_preserve_subsecond_review_intervals() -> None:
    today_start = 2_000_000_000
    day_start = (today_start - 86_400) * 1_000
    assert review_timing_patterns(
        [(day_start + 10_820, 99, 400), (day_start + 10_210, 99, 300)],
        today=100,
        next_day_at=today_start + 86_400,
    ) == [(99, [(10_210, 300), (10_820, 400)])]


def test_forecast_uses_canonical_history_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today_start = 2_000_000_000
    rows = [
        scheduler_pb2.RwkvHistoricalReviewInputsResponse.Review(
            review_id=(today_start - 86_400 + 120) * 1_000,
            day_offset=99,
            deck_id=1,
            card_id=10,
            ease=3,
            duration_millis=1_234,
        ),
        scheduler_pb2.RwkvHistoricalReviewInputsResponse.Review(
            review_id=(today_start - 86_400 + 180) * 1_000,
            day_offset=99,
            deck_id=1,
            card_id=11,
            ease=2,
            duration_millis=2_345,
        ),
        scheduler_pb2.RwkvHistoricalReviewInputsResponse.Review(
            review_id=(today_start - 86_400 + 200) * 1_000,
            day_offset=99,
            deck_id=1,
            ease=0,
            duration_millis=1_000,
        ),
    ]
    backend = SimpleNamespace(
        rwkv_historical_review_inputs=lambda request: SimpleNamespace(reviews=rows)
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_timing_today",
        lambda reviewer: SimpleNamespace(
            days_elapsed=100, next_day_at=today_start + 86_400
        ),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_collection",
        lambda reviewer: SimpleNamespace(_backend=backend),
    )
    monkeypatch.setattr(
        rwkv_history,
        "history_fingerprint_request",
        lambda reviewer, ignored: (
            scheduler_pb2.RwkvHistoricalReviewFingerprintRequest()
        ),
    )
    history = rwkv_scheduler._rwkv_forecast_review_history(
        object(), deck_ids={1}, recent_count=2
    )
    assert history.patterns == [(99, [(120_000, 1_234), (180_000, 2_345)])]
    assert history.review_model.bucket_probabilities[10] == (0.0, 0.5, 0.5, 0.0)
    assert history.success_shares_by_day[99] == (0.5, 0.5, 0.0)
    assert history.recent_card_ids == [11, 10]


def test_forecast_reports_missing_real_timing_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rwkv_scheduler,
        "_timing_today",
        lambda reviewer: SimpleNamespace(days_elapsed=100, next_day_at=2_000_086_400),
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_collection", lambda reviewer: SimpleNamespace()
    )
    with pytest.raises(ValueError, match="review history is unavailable"):
        rwkv_scheduler._rwkv_forecast_review_history(
            object(), deck_ids={1}, recent_count=5
        )


def test_instant_scope_keeps_independent_deck_trees() -> None:
    decks = {
        1: decks_pb2.Deck(
            id=1, name="Alpha", normal=decks_pb2.Deck.Normal(config_id=1)
        ),
        2: decks_pb2.Deck(
            id=2, name="Alpha::Child", normal=decks_pb2.Deck.Normal(config_id=1)
        ),
        3: decks_pb2.Deck(id=3, name="Zeta", normal=decks_pb2.Deck.Normal(config_id=3)),
    }
    configs = {
        1: deck_config_pb2.DeckConfig(
            id=1,
            config=deck_config_pb2.DeckConfig.Config(
                rwkv_review_instant_order_enabled=True
            ),
        ),
        3: deck_config_pb2.DeckConfig(id=3, config=deck_config_pb2.DeckConfig.Config()),
    }
    manager = SimpleNamespace(
        selected=lambda: 1,
        all_names_and_ids=lambda **_: [
            SimpleNamespace(id=deck.id, name=deck.name) for deck in decks.values()
        ],
    )
    col = SimpleNamespace(
        decks=manager,
        _backend=SimpleNamespace(
            get_deck=decks.__getitem__, get_deck_config=configs.__getitem__
        ),
        get_config=lambda _key, default=None: default,
    )
    scope = rwkv_scheduler._rwkv_instant_forecast_scope(
        col,
        [SimpleNamespace(group=2), SimpleNamespace(group=3)],  # type: ignore[list-item]
    )
    assert scope.study_deck_id == 1
    assert scope.instant_decks == {2}
    assert scope.curve_decks == set()
    assert [decks_pb2.Deck.FromString(item).id for item in scope.decks] == [1, 2, 3]

    # A whole-collection search can contain an Instant subdeck under a
    # non-Instant root while a different deck is selected.
    configs[1].config.rwkv_review_instant_order_enabled = False
    decks[2].normal.config_id = 2
    configs[2] = deck_config_pb2.DeckConfig(
        id=2,
        config=deck_config_pb2.DeckConfig.Config(
            rwkv_review_instant_order_enabled=True
        ),
    )
    manager.selected = lambda: 3
    scope = rwkv_scheduler._rwkv_instant_forecast_scope(
        col,
        [SimpleNamespace(group=2), SimpleNamespace(group=3)],  # type: ignore[list-item]
    )
    assert scope.study_deck_id == 3
    assert scope.instant_decks == {2}

    # An Instant parent does not score a child with RWKV disabled.
    configs[1].config.rwkv_review_instant_order_enabled = True
    configs[2].config.rwkv_review_instant_order_enabled = False
    manager.selected = lambda: 1
    scope = rwkv_scheduler._rwkv_instant_forecast_scope(
        col,
        [SimpleNamespace(group=2)],  # type: ignore[list-item]
    )
    assert scope.instant_decks == set()


def test_forecast_dispatches_instant_with_history_and_scheduler_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day_start = 2_000_000_000
    preset = deck_config_pb2.DeckConfig(
        id=1,
        config=deck_config_pb2.DeckConfig.Config(
            rwkv_review_instant_order_enabled=True,
            rwkv_review_min_intervening_reviews=2,
            rwkv_review_min_elapsed_secs=30,
            reviews_per_day=17,
            rwkv_review_minimum_reviews_per_day=4,
            review_order=7,
        ),
    )
    deck = decks_pb2.Deck(id=1, name="Study", normal=decks_pb2.Deck.Normal(config_id=1))
    legacy_config = {
        "reviewOrder": 7,
        "rev": {"perDay": 17},
        "new": {"perDay": 2},
        "jschoreels.rwkv": {
            "rwkv_review_instant_order_enabled": True,
            "rwkv_review_refresh_interval": 5,
            "rwkv_review_candidate_refresh_enabled": True,
            "rwkv_review_batch_size": 64,
            "rwkv_review_precompute_next_card": True,
        },
    }
    rows = [
        scheduler_pb2.RwkvHistoricalReviewInputsResponse.Review(
            review_id=(day_start - 86_400 + second) * 1000,
            day_offset=99,
            deck_id=1,
            card_id=card,
            ease=grade,
            duration_millis=2000,
        )
        for second, card, grade in [(100, 7, 3), (120, 8, 2)]
    ]
    calls: list[dict[str, Any]] = []

    class Backend:
        model_name = "sm57"

        def supports_resident_workload_simulation(self) -> bool:
            return True

        def forecast_workload(self, **_kwargs: Any) -> object:
            raise AssertionError("Instant forecast must not call the Curve forecast")

        def forecast_instant_workload(self, **kwargs: Any) -> object:
            calls.append(kwargs)
            kwargs["progress"](1, 1)
            band = (1.0,) * 6
            return (1, [(0, band, band, band, band, band)], [], 0)

    backend = Backend()
    manager = SimpleNamespace(
        selected=lambda: 1,
        all_config=lambda: [legacy_config],
        all_names_and_ids=lambda **_: [SimpleNamespace(id=1, name="Study")],
        config_dict_for_deck_id=lambda _did: legacy_config,
        get=lambda _did, default=False: {"id": 1},
    )
    collection_backend = SimpleNamespace(
        get_deck=lambda _did: deck,
        get_deck_config=lambda _id: preset,
        rwkv_historical_review_inputs=lambda _request: SimpleNamespace(reviews=rows),
    )
    col = SimpleNamespace(
        decks=manager,
        _backend=collection_backend,
        sched=SimpleNamespace(
            today=100,
            _timing_today=lambda: SimpleNamespace(
                days_elapsed=100, next_day_at=day_start + 86_400
            ),
        ),
        get_config=lambda _key, default=None: default,
    )
    review = rwkv_scheduler.RwkvReviewInput(
        identity=rwkv_scheduler.RwkvReviewIdentity(
            card_id=7, note_id=70, deck_id=1, preset_id=1
        ),
        is_query=True,
        ease=None,
        duration_millis=None,
        card_type=2,
        card_queue=2,
        card_due=105,
        interval_days=10,
        ease_factor=2500,
        reps=4,
        lapses=0,
        day_offset=100,
        current_state_kind="normal",
        current_normal_state_kind="review",
        current_elapsed_days=2,
        current_elapsed_seconds=90_000,
    )
    learning = replace(
        review,
        identity=rwkv_scheduler.RwkvReviewIdentity(
            card_id=8, note_id=80, deck_id=1, preset_id=1
        ),
        card_type=1,
        card_queue=1,
        card_due=day_start + 500,
        remaining_steps=1,
    )
    build = SimpleNamespace(
        inputs_by_batch_size={64: [(7, review), (8, learning)]},
        elapsed_since_secs_by_card_id={7: day_start - 86_400 + 200, 8: day_start + 100},
    )
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", backend)
    monkeypatch.setattr(
        rwkv_scheduler, "configure_reviewer_backend_from_environment", lambda: True
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_reviewer_backend_warmed_up", lambda _reviewer: True
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_review_input_batches_for_search", lambda **_: build
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_resolve_dynamic_desired_retentions_for_input_build",
        lambda _reviewer, value: value,
    )
    monkeypatch.setattr(
        rwkv_history,
        "history_fingerprint_request",
        lambda *_: scheduler_pb2.RwkvHistoricalReviewFingerprintRequest(),
    )
    monkeypatch.setattr(rwkv_scheduler.time, "time", lambda: day_start + 200)

    @contextmanager
    def operation(*_args: Any, **_kwargs: Any) -> Any:
        yield (SimpleNamespace(require_current=lambda: None), None)

    monkeypatch.setattr(
        rwkv_scheduler, "_temporary_reviewer_backend_operation", operation
    )
    result = rwkv_scheduler.forecast_rwkv_workload(
        mw=SimpleNamespace(col=col), days=1, paths=1
    )
    (call,) = calls
    assert result["model"] == "instant"
    assert len(call["cards"]) == 2
    assert call["cards"][0].review_input.current_elapsed_seconds == 86_400
    assert call["cards"][0].review_input.current_elapsed_days == 1
    assert call["cards"][1].review_input.current_elapsed_seconds == 100
    assert call["cards"][1].review_input.current_elapsed_days is None
    assert call["timing_patterns"] == [[(100_000, 2000), (120_000, 2000)]]
    assert call["timing_pattern_success_shares"] == [(0.5, 0.5, 0.0)]
    assert call["intervening_reviews"] == {8: 0, 7: 1}
    assert call["new_cards_ignore_review_limit"] is False
    assert call["refresh_interval"] == 5
    assert call["candidate_refresh_enabled"] is True
    assert call["candidate_refresh_limit"] == 64
    assert call["precompute_next_card"] is True
    assert call["min_intervening_reviews"] == 2
    assert call["min_elapsed_secs"] == 30


def test_instant_native_bridge_simulates_without_mutating_warm_state() -> None:
    model = (
        Path(__file__).parents[1]
        / "aqt"
        / "rwkv_inference"
        / "sm57"
        / "weights.f32.bin"
    )
    runtime = _RustRwkvRuntime(
        model_path=model, target_retention=0.9, max_interval_days=36500, model="sm57"
    )
    answer = rwkv_scheduler.RwkvReviewInput(
        identity=rwkv_scheduler.RwkvReviewIdentity(
            card_id=100, note_id=101, deck_id=1, preset_id=1
        ),
        is_query=False,
        ease=3,
        duration_millis=5000,
        card_type=2,
        card_queue=2,
        card_due=100,
        interval_days=5,
        ease_factor=2500,
        reps=3,
        lapses=0,
        day_offset=100,
        current_state_kind="normal",
        current_normal_state_kind="review",
        current_elapsed_days=5,
        current_elapsed_seconds=432_000,
    )
    runtime.warm_up_reviews([answer], return_snapshot=False)
    query = replace(
        answer, is_query=True, ease=None, duration_millis=None, card_due=130
    )
    initial_state = runtime.warm_up_state(query)
    initial_cache = runtime.cache_state()
    card = rwkv_scheduler.RwkvForecastCardInput(
        review_input=query,
        due_day=30,
        group=1,
        desired_retention=0.01,
        scheduler=rwkv_scheduler.RWKV_FORECAST_SCHEDULER_RWKV,
    )
    deck = decks_pb2.Deck(id=1, name="Study", normal=decks_pb2.Deck.Normal(config_id=1))
    preset = deck_config_pb2.DeckConfig(
        id=1,
        config=deck_config_pb2.DeckConfig.Config(
            rwkv_review_instant_order_enabled=True,
            rwkv_review_minimum_reviews_per_day=1,
            reviews_per_day=1,
            new_per_day=0,
            maximum_review_interval=36500,
            initial_ease=2.5,
        ),
    )
    review_model = SimpleNamespace(
        grade_seconds=(1.0, 1.0, 1.0, 1.0), bucket_probabilities={}
    )
    args = dict(
        cards=[card],
        decks=[deck.SerializeToString()],
        deck_configs=[preset.SerializeToString()],
        study_deck_id=1,
        instant_decks={1},
        timing_patterns=[[(10_000, 2000), (15_000, 2000)]],
        timing_pattern_success_shares=[(0.0, 1.0, 0.0)],
        intervening_reviews={},
        latest_answers_today={},
        days=1,
        paths=2,
        seed=0,
        today=100,
        new_cards_ignore_review_limit=False,
        fsrs_enabled=False,
        fsrs_learning_queues_disabled=False,
        fsrs_short_term_with_steps_enabled=True,
        learn_ahead_secs=0,
        day_start_secs=0,
        now_millis=0,
        same_day_allowed=False,
        min_intervening_reviews=0,
        min_elapsed_secs=0,
        refresh_interval=1,
        candidate_refresh_enabled=True,
        candidate_refresh_limit=64,
        precompute_next_card=True,
        review_order=7,
        review_model=review_model,
    )
    output = runtime.forecast_instant_workload(**args)
    assert output[1][0][2][0] == 1.0
    assert runtime.forecast_instant_workload(**args) == output

    progress_calls = 0

    def cancel_after_start(_current: int, _total: int) -> None:
        nonlocal progress_calls
        progress_calls += 1
        if progress_calls > 1:
            raise InterruptedError("forecast cancelled for test")

    with pytest.raises(Exception, match="forecast cancelled for test"):
        runtime.forecast_instant_workload(**args, progress=cancel_after_start)
    assert progress_calls > 1
    assert runtime.warm_up_state(query) == initial_state
    assert runtime.cache_state() == initial_cache
