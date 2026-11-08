# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import threading
import time
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


def test_forecast_calibration_compares_history_with_modeled_answer_noise() -> None:
    targets = {1: (98, 1), 2: (98, 1), 3: (99, 0), 4: (99, 0)}
    predictions = [(i, 0.5) for i in range(4)]
    calibrated = rwkv_scheduler._rwkv_forecast_history_calibration(
        targets, [1, 2, 3, 4], predictions
    )
    assert calibrated is not None
    assert calibrated.days == 2
    assert calibrated.observed_sigma == pytest.approx(0.5**0.5)
    assert calibrated.modeled_sigma == pytest.approx(0.125**0.5)
    assert calibrated.scale == pytest.approx((3 / 2.625) ** 0.5)
    equal = {1: (98, 1), 2: (98, 0), 3: (99, 1), 4: (99, 0)}
    shrunk = rwkv_scheduler._rwkv_forecast_history_calibration(
        equal, [1, 2, 3, 4], predictions
    )
    assert shrunk is not None
    assert shrunk.scale == pytest.approx((20 / 21) ** 0.5)
    assert (
        rwkv_scheduler._rwkv_forecast_history_calibration(equal, [1], predictions)
        is None
    )
    assert (
        rwkv_scheduler._rwkv_forecast_history_calibration(
            equal, [1, 2, 3, 4], [(i, float(i % 2)) for i in range(4)]
        )
        is None
    )
    # Variation already explained by the model must not be counted twice.
    outcomes = [(1, 1), (1, 0), (0, 1), (1, 0), (0, 0)]
    matching = {
        day * 2 + index: (day, answer)
        for day, answers in enumerate(outcomes)
        for index, answer in enumerate(answers)
    }
    matching_result = rwkv_scheduler._rwkv_forecast_history_calibration(
        matching, list(range(10)), [(i, 0.5) for i in range(10)]
    )
    assert matching_result is not None
    assert matching_result.scale == pytest.approx(1.0)


def test_forecast_review_time_fits_recall_and_grade() -> None:
    recalls = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    history: Any = SimpleNamespace(
        patterns=[(99, [(1, 10_000), (2, 14_000)])],
        calibration_targets={i: (99, 1) for i in range(6)},
        calibration_review_ids=list(range(6)),
        calibration_reviews=[
            SimpleNamespace(ease=3, duration_millis=round((4 + 10 * (1 - r)) * 1000))
            for r in recalls
        ],
    )
    predictions = list(enumerate(recalls))
    model = rwkv_scheduler._rwkv_forecast_review_time_model(history, predictions)
    assert model is not None
    assert model.reference_seconds == 12.0
    assert model.samples == 6
    assert (
        model.grades[0] is None and model.grades[1] is None and model.grades[3] is None
    )
    grade = model.grades[2]
    assert grade is not None
    assert grade[:4] == pytest.approx((4.0, 10.0, 0.3, 0.8))


def test_forecast_scenario_result_preserves_path_identities() -> None:
    band = (10.0, 8.0, 9.0, 10.0, 11.0, 12.0)
    output = (
        3,
        [(0, band, band, band, band, band)],
        [],
        0,
        [[[1, 2, 3, 4, 5]], [[9, 8, 7, 6, 5]], [[4, 3, 2, 1, 0]]],
    )
    result = rwkv_scheduler._rwkv_forecast_result(output, [], days=1, seed=0)
    assert result["scenario_daily"] == [
        {
            "day": 0,
            "due": [1.0, 9.0, 4.0],
            "done": [2.0, 8.0, 3.0],
            "new": [3.0, 7.0, 2.0],
            "lapses": [4.0, 6.0, 1.0],
            "seconds": [5.0, 5.0, 0.0],
        }
    ]
    assert "scenario_daily" not in rwkv_scheduler._rwkv_forecast_result(
        output[:4], [], days=1, seed=0
    )


@pytest.mark.parametrize(
    "metadata", [{"scenario_sigma": 0.04}, {"scenario_kind": "stratified"}]
)
def test_partial_forecast_waits_for_center_and_keeps_missing_bounds(
    metadata: dict[str, object],
) -> None:
    partial = rwkv_scheduler._RwkvForecastPartial(
        metadata={"days": 3, "paths": 3, "cards": 5, **metadata},
        rows={0: {0: (5, 4, 3, 2, 1)}},
    )
    assert rwkv_scheduler._rwkv_partial_forecast_result(partial)["daily"] == []
    partial.rows[0][1] = (9, 8, 7, 6, 5)
    partial.rows[1] = {1: (11, 10, 9, 8, 7)}
    result = rwkv_scheduler._rwkv_partial_forecast_result(partial)
    assert result["partial"] is True
    assert [entry["day"] for entry in result["daily"]] == [0, 1]
    assert result["scenario_daily"][0]["due"] == [5, 9, None]
    assert result["scenario_daily"][1]["due"] == [None, 11, None]


def test_partial_forecast_job_reports_days_while_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported = threading.Event()
    release = threading.Event()

    def forecast(**kwargs: Any) -> dict[str, object]:
        callback = kwargs["partial_progress"]
        callback(
            {"days": 2, "paths": 3, "cards": 1, "scenario_sigma": 0.04},
            1,
            0,
            [9, 8, 7, 6, 5],
        )
        reported.set()
        assert release.wait(timeout=5)
        return {"version": 1, "daily": []}

    monkeypatch.setattr(rwkv_scheduler, "forecast_rwkv_workload", forecast)
    job_id = rwkv_scheduler.start_rwkv_workload_forecast(SimpleNamespace())["job_id"]
    try:
        assert reported.wait(timeout=5)
        state = rwkv_scheduler.rwkv_workload_forecast_progress(job_id)
        assert state["phase"] == "running"
        assert (state["current"], state["total"], state["unit"]) == (1, 6, "days")
        assert state["partial_result"]["scenario_daily"][0]["due"] == [None, 9.0, None]
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = rwkv_scheduler.rwkv_workload_forecast_progress(job_id)
        if state["done"]:
            break
        time.sleep(0.01)
    assert state["phase"] == "complete"
    assert state["partial_result"] is None
    assert state["unit"] == "days"


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
            review_kind=1,
            duration_millis=1_234,
        ),
        scheduler_pb2.RwkvHistoricalReviewInputsResponse.Review(
            review_id=(today_start - 86_400 + 180) * 1_000,
            day_offset=99,
            deck_id=1,
            card_id=11,
            ease=2,
            review_kind=1,
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
    assert history.calibration_reviews == []

    rows.extend(
        scheduler_pb2.RwkvHistoricalReviewInputsResponse.Review(
            review_id=(today_start - 2 * 86_400 + second) * 1000,
            day_offset=98,
            deck_id=deck_id,
            card_id=card,
            ease=3,
            review_kind=1,
            duration_millis=1_000,
        )
        for second, deck_id, card in [(100, 1, 20), (130, 1, 21), (160, 2, 22)]
    )
    calibrated = rwkv_scheduler._rwkv_forecast_review_history(
        object(), deck_ids={1}, recent_count=2, include_calibration=True
    )
    assert len(calibrated.calibration_targets) == 4
    assert len(calibrated.calibration_reviews) == len(rows)
    assert len(calibrated.calibration_review_ids) == len(rows)
    assert rows[-1].review_id not in calibrated.calibration_targets
    # The other deck is still replayed because it affects shared model state.
    assert calibrated.calibration_reviews[-1].identity.deck_id == 2


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
            if "day_progress" in kwargs:
                kwargs["day_progress"](1, 0, [1, 1, 1, 1, 1])
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
    streamed: list[dict[str, object]] = []
    stratified = rwkv_scheduler.forecast_rwkv_workload(
        mw=SimpleNamespace(col=col),
        days=1,
        paths=3,
        partial_progress=lambda metadata, *_: streamed.append(metadata),
    )
    assert calls[-1]["stratified_answers"] is True
    assert calls[-1]["stratified_scale"] == 1.0
    assert "scenario_sigma" not in calls[-1]
    assert calls[-1]["refresh_interval"] == 5
    assert calls[-1]["candidate_refresh_enabled"] is True
    assert stratified["scenario_kind"] == "stratified"
    assert stratified["scenario_calibration"] == "insufficient_history"
    assert streamed[0]["scenario_kind"] == "stratified"
    calibration = rwkv_scheduler._RwkvForecastCalibration(0.8, 40, 0.04, 0.05)
    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_forecast_calibration", lambda *_: calibration
    )
    stratified = rwkv_scheduler.forecast_rwkv_workload(
        mw=SimpleNamespace(col=col),
        days=1,
        paths=3,
        partial_progress=lambda metadata, *_: streamed.append(metadata),
    )
    assert calls[-1]["stratified_scale"] == 0.8
    assert stratified["scenario_history_days"] == 40
    assert streamed[-1]["scenario_history_days"] == 40


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
    time_model = (1.0, [(10.0, 0.0, 0.0, 1.0, 10.0)] * 4)
    timed = runtime.forecast_instant_workload(**args, review_time_model=time_model)
    assert timed[1][0][2][0] == 1.0
    assert timed[1][0][5][0] == pytest.approx(20.0)
    r_model = (1.0, [(1.0, 10.0, 0.0, 1.0, 5.0)] * 4)
    shifted_times = runtime.forecast_instant_workload(
        **{**args, "paths": 3},
        scenario_sigma=1.0,
        review_time_model=r_model,
    )
    assert shifted_times[4][0][0][4] == pytest.approx(shifted_times[4][1][0][4])
    assert shifted_times[4][2][0][4] == pytest.approx(shifted_times[4][1][0][4])
    with pytest.raises(Exception, match="four grades"):
        runtime.forecast_instant_workload(**args, review_time_model=(1.0, []))
    with pytest.raises(Exception, match="review-time model"):
        runtime.forecast_instant_workload(**args, review_time_model=(0.0, [None] * 4))

    single = runtime.forecast_instant_workload(**{**args, "paths": 1})
    scenarios = runtime.forecast_instant_workload(
        **{**args, "paths": 3}, scenario_sigma=0.0
    )
    assert len(scenarios) == 5
    assert scenarios[4][0] == scenarios[4][1] == scenarios[4][2]
    assert scenarios[4][1][0] == pytest.approx(
        [single[1][0][i][0] for i in range(1, 6)]
    )
    day_events: list[tuple[int, int, list[float]]] = []
    shifted = runtime.forecast_instant_workload(
        **{**args, "paths": 3},
        scenario_sigma=0.3,
        day_progress=lambda path, day, values: day_events.append((path, day, values)),
    )
    assert shifted[4][1] == scenarios[4][1]
    assert sorted(path for path, _day, _values in day_events) == [0, 1, 2]
    for path, day, values in day_events:
        assert values == pytest.approx(shifted[4][path][day])

    stratified_events: list[tuple[int, int, list[float]]] = []
    stratified = runtime.forecast_instant_workload(
        **{**args, "paths": 3},
        stratified_answers=True,
        day_progress=lambda path, day, values: stratified_events.append(
            (path, day, values)
        ),
    )
    assert len(stratified) == 5
    assert sorted(path for path, _day, _values in stratified_events) == [0, 1, 2]
    for path, day, values in stratified_events:
        assert values == pytest.approx(stratified[4][path][day])

    new_preset = deck_config_pb2.DeckConfig()
    new_preset.CopyFrom(preset)
    new_preset.config.rwkv_review_minimum_reviews_per_day = 0
    new_preset.config.reviews_per_day = 0
    new_preset.config.new_per_day = 10
    new_cards = [
        replace(
            card,
            due_day=0,
            review_input=replace(
                query,
                identity=rwkv_scheduler.RwkvReviewIdentity(
                    card_id=200 + index, note_id=300 + index, deck_id=1, preset_id=1
                ),
                card_type=0,
                card_queue=0,
                card_due=index,
                reps=0,
                current_elapsed_days=None,
                current_elapsed_seconds=None,
            ),
        )
        for index in range(10)
    ]
    new_only = runtime.forecast_instant_workload(
        **{
            **args,
            "cards": new_cards,
            "deck_configs": [new_preset.SerializeToString()],
            "paths": 3,
            "new_cards_ignore_review_limit": True,
        },
        stratified_answers=True,
    )
    assert all(path[0][2] == 10 for path in new_only[4])

    def cancel_day(_path: int, _day: int, _values: list[float]) -> None:
        raise InterruptedError("day stream cancelled for test")

    with pytest.raises(Exception, match="day stream cancelled for test"):
        runtime.forecast_instant_workload(
            **{**args, "paths": 3}, stratified_answers=True, day_progress=cancel_day
        )
    assert runtime.warm_up_state(query) == initial_state
    assert runtime.cache_state() == initial_cache
    no_minimum = deck_config_pb2.DeckConfig()
    no_minimum.CopyFrom(preset)
    no_minimum.config.rwkv_review_minimum_reviews_per_day = 0
    no_minimum.config.reviews_per_day = 0
    extreme = runtime.forecast_instant_workload(
        **{
            **args,
            "paths": 3,
            "cards": [replace(card, desired_retention=0.5)],
            "deck_configs": [no_minimum.SerializeToString()],
        },
        scenario_sigma=1.0,
    )
    # With no answers, changing outcome probability must not affect which
    # cards the real R makes due.
    assert extreme[4][0][0][0] == extreme[4][1][0][0] == extreme[4][2][0][0]
    assert all(path[0][1] == 0 for path in extreme[4])

    # When the configured minimum selects the card, the outcome varies while
    # its initial queue eligibility continues to use the same predicted R.
    answered = runtime.forecast_instant_workload(
        **{**args, "paths": 3}, scenario_sigma=1.0
    )
    assert answered[4][0][0][3] == 1
    assert answered[4][2][0][3] == 0
    with pytest.raises(Exception, match="scenario sigma"):
        runtime.forecast_instant_workload(**args, scenario_sigma=0.3)
    with pytest.raises(Exception, match="stratified answers"):
        runtime.forecast_instant_workload(**args, stratified_answers=True)
    with pytest.raises(Exception, match="stratified answers"):
        runtime.forecast_instant_workload(
            **{**args, "paths": 3}, scenario_sigma=0.1, stratified_answers=True
        )
    with pytest.raises(Exception, match="stratified answers"):
        runtime.forecast_instant_workload(
            **{**args, "paths": 3}, stratified_answers=True, stratified_scale=-1
        )

    predictions = runtime.forecast_history_predictions([answer])
    assert len(predictions) == 1
    assert 0 <= predictions[0][1] <= 1
    assert runtime.warm_up_state(query) == initial_state
    assert runtime.cache_state() == initial_cache

    def cancel_replay(_current: int, _total: int) -> None:
        raise InterruptedError("calibration cancelled for test")

    with pytest.raises(Exception, match="calibration cancelled for test"):
        runtime.forecast_history_predictions([answer], progress=cancel_replay)
    assert runtime.warm_up_state(query) == initial_state
    assert runtime.cache_state() == initial_cache

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


def test_instant_forecast_precomputed_card_respects_remaining_daily_limit() -> None:
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
            card_id=100, note_id=1000, deck_id=1, preset_id=1
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
    answers = [
        replace(
            answer,
            identity=rwkv_scheduler.RwkvReviewIdentity(
                card_id=100 + index, note_id=1000 + index, deck_id=1, preset_id=1
            ),
            ease=1 + index % 4,
            duration_millis=1000 + index * 100,
            current_elapsed_days=1 + index % 10,
            current_elapsed_seconds=86_400 * (1 + index % 10),
        )
        for index in range(30)
    ]
    runtime.warm_up_reviews(answers, return_snapshot=False)
    cards = [
        rwkv_scheduler.RwkvForecastCardInput(
            review_input=replace(
                review, is_query=True, ease=None, duration_millis=None
            ),
            due_day=0,
            group=1,
            desired_retention=0.99,
            scheduler=rwkv_scheduler.RWKV_FORECAST_SCHEDULER_RWKV,
        )
        for review in answers
    ]
    # The deck's limit overrides the preset; two reviews were already done.
    deck = decks_pb2.Deck(
        id=1,
        name="Study",
        normal=decks_pb2.Deck.Normal(config_id=1, review_limit=5),
        common=decks_pb2.Deck.Common(last_day_studied=100, review_studied=2),
    )
    preset = deck_config_pb2.DeckConfig(
        id=1,
        config=deck_config_pb2.DeckConfig.Config(
            rwkv_review_instant_order_enabled=True,
            reviews_per_day=200,
            new_per_day=0,
            maximum_review_interval=36500,
            initial_ease=2.5,
        ),
    )
    output = runtime.forecast_instant_workload(
        cards=cards,
        decks=[deck.SerializeToString()],
        deck_configs=[preset.SerializeToString()],
        study_deck_id=1,
        instant_decks={1},
        timing_patterns=[[(10_000, 2000), (15_000, 2000)]],
        timing_pattern_success_shares=[(0.0, 1.0, 0.0)],
        intervening_reviews={},
        latest_answers_today={},
        days=1,
        paths=1,
        seed=3,
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
        candidate_refresh_enabled=False,
        candidate_refresh_limit=64,
        precompute_next_card=True,
        review_order=7,
        review_model=SimpleNamespace(grade_seconds=(1.0,) * 4, bucket_probabilities={}),
    )
    assert output[1][0][2][0] == 3.0
