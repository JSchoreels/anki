# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import threading
import time
from collections.abc import Callable
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


def test_forecast_calibration_measures_systematic_bias_and_review_variance() -> None:
    def calibrate(answers: int, successes: Callable[[int], int]) -> Any:
        # 40 days, every prediction is 0.7 (model variance 0.21 per answer).
        targets = {
            day * answers + index: (day, int(index < successes(day)))
            for day in range(40)
            for index in range(answers)
        }
        total = 40 * answers
        return rwkv_scheduler._rwkv_forecast_history_calibration(
            targets, list(range(total)), [(i, 0.7) for i in range(total)]
        )

    # Alternating 6 and 8 successes out of 10 average 0.7: only sampling error.
    unbiased = calibrate(10, lambda day: 6 + 2 * (day % 2))
    assert unbiased is not None
    assert unbiased.review_variance == pytest.approx(0.21)
    assert unbiased.bias_sd < 0.02
    # A constant +0.05 residual is part of the root-mean-square bias.
    biased = calibrate(20, lambda _day: 15)
    assert biased is not None
    assert biased.bias_sd == pytest.approx(0.05)
    # Bias and sampling error combine in quadrature.
    noisy_bias = calibrate(20, lambda day: 14 + 2 * (day % 2))
    assert noisy_bias is not None
    assert noisy_bias.bias_sd > 0.05
    # A single cluster cannot estimate a standard error.
    assert rwkv_scheduler._rwkv_forecast_bias_sd({0: (0.4, 10.0)}) == 0.0
    # Days are clusters when fewer than four weeks have data.
    daily = rwkv_scheduler._rwkv_forecast_bias_sd(
        {0: (1.0, 10.0), 1: (-1.0, 10.0), 2: (1.0, 10.0)}
    )
    mean = 1.0 / 30
    spread = sum((e - mean * 10) ** 2 for e in (1.0, -1.0, 1.0))
    assert daily == pytest.approx((3 / 2 * spread / 30**2 + mean**2) ** 0.5)
    # With four weeks the errors are clustered by week: a weekly-correlated
    # error is not averaged away as if the days were independent.
    weekly = {day: (0.5 if day // 7 % 2 else -0.5, 1.0) for day in range(56)}
    independent = {day: (0.5 if day % 2 else -0.5, 1.0) for day in range(56)}
    assert rwkv_scheduler._rwkv_forecast_bias_sd(weekly) > 0.15
    assert rwkv_scheduler._rwkv_forecast_bias_sd(independent) < 0.05


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
    # A two-answer day is pulled toward its mean duration (1789.5 ms) with a
    # prior of 30 answers: round((2 * d + 30 * 1789.5) / 32).
    assert history.patterns == [(99, [(120_000, 1_755), (180_000, 1_824)])]
    assert history.review_model.bucket_probabilities[10] == (0.0, 0.5, 0.5, 0.0)
    assert history.success_shares_by_day[99] == (0.5, 0.5, 0.0)
    assert history.recent_card_ids == [11, 10]
    assert history.calibration_replay is None

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
    assert calibrated.calibration_replay is not None
    assert len(calibrated.calibration_replay.rows) == len(rows)
    assert rows[-1].review_id not in calibrated.calibration_targets
    # The other deck is still replayed because it affects shared model state.
    assert calibrated.calibration_replay.rows[-1].deck_id == 2


def _history_from_days(
    monkeypatch: pytest.MonkeyPatch, days: dict[int, list[tuple[int, int]]]
) -> Any:
    """Build forecast history from {day: [(ease, duration_millis), ...]}."""
    today_start = 2_000_000_000
    rows = [
        scheduler_pb2.RwkvHistoricalReviewInputsResponse.Review(
            review_id=(today_start - (100 - day) * 86_400 + 10 * index + 1) * 1_000,
            day_offset=day,
            deck_id=1,
            card_id=day * 1_000 + index,
            ease=ease,
            review_kind=1,
            duration_millis=duration,
        )
        for day, answers in days.items()
        for index, (ease, duration) in enumerate(answers)
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
    return rwkv_scheduler._rwkv_forecast_review_history(
        object(), deck_ids={1}, recent_count=0
    )


def test_forecast_samples_days_with_enough_answers_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_day = [(3, 1_000)] * 10
    short_day = [(3, 1_000)] * 3
    days = {day: long_day for day in range(90, 97)} | {97: short_day}
    history = _history_from_days(monkeypatch, days)
    assert [day for day, _ in history.patterns] == list(range(90, 97))
    assert sorted(history.success_shares_by_day) == list(range(90, 97))
    # Fewer than seven long days: days with at least two answers are used.
    days = {day: long_day for day in range(90, 96)} | {97: short_day, 98: [(3, 1_000)]}
    history = _history_from_days(monkeypatch, days)
    assert [day for day, _ in history.patterns] == [*range(90, 96), 97]


def test_forecast_samples_recent_timing_days_with_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = rwkv_scheduler._RWKV_FORECAST_TIMING_DAYS
    old_day = [(3, 20_000)] * 10
    recent_day = [(3, 5_000)] * 10
    # Seven qualifying recent days: older days are not sampled.
    days = {day: old_day for day in range(1, 8)} | {
        day: recent_day for day in range(100 - 7, 100)
    }
    history = _history_from_days(monkeypatch, days)
    assert [day for day, _ in history.patterns] == list(range(93, 100))
    assert history.timing_window_start == 93
    # The window is measured back from today (day 100).
    assert all(day >= 100 - window for day, _ in history.patterns)
    # Too few recent days: the whole year's pool is kept.
    days = {day: old_day for day in range(1, 8)} | {
        day: recent_day for day in range(100 - 6, 100)
    }
    history = _history_from_days(monkeypatch, days)
    assert [day for day, _ in history.patterns] == [*range(1, 8), *range(94, 100)]
    assert history.timing_window_start == 1


def test_forecast_review_time_model_uses_the_timing_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _replay_rows(days=5, per_day=8)
    for row in rows:
        row.duration_millis = 3_000
    history = _replay_history(monkeypatch, rows)
    source = history.calibration_replay
    assert source is not None
    backend = _ReplayBackend()
    replayed = rwkv_scheduler._rwkv_forecast_replay_predictions(
        backend, backend.forecast_history_predictions, source, lambda *_: None, 3
    )
    whole = rwkv_scheduler._rwkv_forecast_review_time_model(history, replayed)
    assert whole is not None and whole.samples == 40
    # A window of the last three days (97..99) keeps 6 answers per grade.
    recent = rwkv_scheduler._rwkv_forecast_review_time_model(
        history._replace(timing_window_start=97), replayed
    )
    assert recent is not None and recent.samples == 24
    # Two days leave 4 per grade: no grade can be fitted, so sampled durations
    # are used unchanged.
    assert (
        rwkv_scheduler._rwkv_forecast_review_time_model(
            history._replace(timing_window_start=98), replayed
        )
        is None
    )


def test_forecast_shrinks_short_sampled_days_toward_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = {day: [(3, 1_000)] * 30 for day in range(90, 93)} | {
        93: [(4, 5_000), (4, 5_000)]
    }
    history = _history_from_days(monkeypatch, days)
    patterns = dict(history.patterns)
    # Days with 30 answers or more keep their own durations.
    assert patterns[90] == [(second, 1_000) for second, _ in patterns[90]]
    assert len(patterns[90]) == 30
    # A tiny day is pulled toward the pooled mean duration; times are kept.
    mean = (90 * 1_000 + 2 * 5_000) / 92
    expected = round((2 * 5_000 + 30 * mean) / 32)
    assert [duration for _, duration in patterns[93]] == [expected, expected]
    assert 1_000 < expected < 5_000
    # Its Easy-only shares are pulled toward the pooled shares (Good 90, Easy 2).
    hard, good, easy = history.success_shares_by_day[93]
    assert hard == 0.0
    assert good == pytest.approx((20 * 90 / 92) / 22)
    assert easy == pytest.approx((2 + 20 * 2 / 92) / 22)
    assert hard + good + easy == pytest.approx(1.0)
    assert history.success_shares_by_day[90] == pytest.approx(
        (0.0, (30 + 20 * 90 / 92) / 50, (20 * 2 / 92) / 50)
    )


def test_forecast_shrunk_durations_are_at_least_one_millisecond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = {day: [(3, 0)] * 30 for day in range(90, 93)} | {93: [(3, 0), (3, 0)]}
    history = _history_from_days(monkeypatch, days)
    assert [duration for _, duration in dict(history.patterns)[93]] == [1, 1]


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
    assert calls[-1]["stratified_bias_sd"] == 0.0
    assert calls[-1]["stratified_review_variance"] == 0.0
    assert "scenario_sigma" not in calls[-1]
    assert calls[-1]["refresh_interval"] == 5
    assert calls[-1]["candidate_refresh_enabled"] is True
    assert stratified["scenario_kind"] == "stratified"
    assert stratified["scenario_calibration"] == "insufficient_history"
    assert streamed[0]["scenario_kind"] == "stratified"
    calibration = rwkv_scheduler._RwkvForecastCalibration(
        0.8, 40, 0.04, 0.05, 0.03, 0.2
    )
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
    assert calls[-1]["stratified_bias_sd"] == 0.03
    assert calls[-1]["stratified_review_variance"] == 0.2
    assert stratified["scenario_history_days"] == 40
    assert stratified["scenario_bias_sd"] == 0.03
    assert streamed[-1]["scenario_history_days"] == 40
    assert streamed[-1]["scenario_bias_sd"] == 0.03


@pytest.fixture(autouse=True)
def _empty_forecast_replay_cache() -> Any:
    rwkv_scheduler._clear_rwkv_forecast_replay_cache()
    yield
    rwkv_scheduler._clear_rwkv_forecast_replay_cache()


_REPLAY_TODAY_START = 2_000_000_000


def _replay_rows(days: int = 5, per_day: int = 4) -> list[Any]:
    return [
        scheduler_pb2.RwkvHistoricalReviewInputsResponse.Review(
            review_id=(_REPLAY_TODAY_START - (100 - day) * 86_400 + 10 * index + 1)
            * 1_000,
            day_offset=day,
            deck_id=1,
            card_id=day * 1_000 + index,
            ease=1 + (day + index) % 4,
            review_kind=1,
            duration_millis=1_000,
        )
        for day in range(95, 95 + days)
        for index in range(per_day)
    ]


_replay_current_rows: list[list[Any]] = [[]]
# One profile for the whole test: the collection's identity is part of the key.
_REPLAY_COLLECTION = SimpleNamespace(
    path="/profiles/a/collection.anki2",
    _backend=SimpleNamespace(
        rwkv_historical_review_inputs=lambda request: SimpleNamespace(
            reviews=_replay_current_rows[0]
        )
    ),
)


class _ReplayBackend:
    """Replays deterministic predictions and counts the whole-history replays."""

    model_name = "sm57"
    instant_model_name = None
    state_precision = "f32"

    def __init__(self) -> None:
        self.calls = 0

    def forecast_history_predictions(
        self, reviews: Any, progress: Any = None
    ) -> list[tuple[int, float]]:
        self.calls += 1
        if progress is not None:
            progress(1, 1)
        return [
            (index, 0.3 + 0.1 * ((review.identity.card_id * 7 + index) % 6))
            for index, review in enumerate(reviews)
        ]


def _replay_history(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[Any],
    *,
    col: Any | None = None,
    today: int = 100,
) -> Any:
    if col is None:
        col = _REPLAY_COLLECTION
        _replay_current_rows[:] = [rows]
    monkeypatch.setattr(
        rwkv_scheduler,
        "_timing_today",
        lambda reviewer: SimpleNamespace(
            days_elapsed=today, next_day_at=_REPLAY_TODAY_START + 86_400
        ),
    )
    monkeypatch.setattr(rwkv_scheduler, "_collection", lambda reviewer: col)
    monkeypatch.setattr(
        rwkv_history,
        "history_fingerprint_request",
        lambda reviewer, ignored: (
            scheduler_pb2.RwkvHistoricalReviewFingerprintRequest()
        ),
    )
    return rwkv_scheduler._rwkv_forecast_review_history(
        object(), deck_ids={1}, recent_count=0, include_calibration=True
    )


def _calibrate(history: Any, backend: _ReplayBackend) -> Any:
    return rwkv_scheduler._rwkv_forecast_calibration(
        backend, history, lambda _current, _total: None, 3
    )


def _uncached_calibration(history: Any, backend: _ReplayBackend) -> Any:
    """The calibration from the full, unfiltered replay of the whole history."""
    source = history.calibration_replay
    reviews = [rwkv_history.review_input_from_row(row) for row in source.rows]
    return rwkv_scheduler._rwkv_forecast_history_calibration(
        history.calibration_targets,
        [row.review_id for row in source.rows],
        backend.forecast_history_predictions(reviews),
    )


def test_forecast_calibration_replay_is_cached_and_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _replay_rows()
    backend = _ReplayBackend()
    history = _replay_history(monkeypatch, rows)
    expected = _uncached_calibration(history, backend)
    assert expected is not None
    backend.calls = 0

    missed = _calibrate(history, backend)
    assert backend.calls == 1
    hit = _calibrate(_replay_history(monkeypatch, rows), backend)
    assert backend.calls == 1
    assert missed == hit == expected

    # A search or horizon change builds the same history again: still a hit,
    # and the next day (no new answers) only moves the 365-day window.
    next_day = _replay_history(monkeypatch, rows, today=101)
    assert _calibrate(next_day, backend) == _uncached_calibration(next_day, backend)
    assert backend.calls == 2  # one extra call, from the uncached reference
    # Going back in time needs predictions the cache dropped: it misses.
    rwkv_scheduler._clear_rwkv_forecast_replay_cache()
    _calibrate(_replay_history(monkeypatch, rows, today=101), backend)
    backend.calls = 0
    _calibrate(_replay_history(monkeypatch, rows, today=100), backend)
    assert backend.calls == 1


def test_forecast_review_time_reuses_replay_and_learns_grade_and_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _replay_rows(days=5, per_day=8)
    for index, row in enumerate(rows):
        recall = 0.3 + 0.1 * ((row.card_id * 7 + index) % 6)
        row.duration_millis = round((4.0 + 10.0 * (1.0 - recall)) * 1000)
    backend = _ReplayBackend()
    history = _replay_history(monkeypatch, rows)
    source = history.calibration_replay
    assert source is not None
    replayed = rwkv_scheduler._rwkv_forecast_replay_predictions(
        backend, backend.forecast_history_predictions, source, lambda *_: None, 3
    )
    model = rwkv_scheduler._rwkv_forecast_review_time_model(history, replayed)
    assert model is not None
    assert model.samples == 40
    for grade in model.grades:
        assert grade is not None
        assert grade[0] == pytest.approx(4.0, abs=0.01)
        assert grade[1] == pytest.approx(10.0, abs=0.01)
        assert grade[2:4] == pytest.approx((0.3, 0.7))
    assert (
        rwkv_scheduler._rwkv_forecast_calibration(
            backend, history, lambda *_: None, 3, replayed
        )
        is not None
    )
    assert backend.calls == 1


def test_forecast_calibration_replay_cache_holds_two_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _ReplayBackend()
    for count in (5, 6, 7):
        _calibrate(_replay_history(monkeypatch, _replay_rows(count)), backend)
    assert backend.calls == 3
    assert len(rwkv_scheduler._rwkv_forecast_replay_cache) == 2
    _calibrate(_replay_history(monkeypatch, _replay_rows(7)), backend)
    assert backend.calls == 3
    _calibrate(_replay_history(monkeypatch, _replay_rows(5)), backend)
    assert backend.calls == 4


def test_forecast_calibration_replay_cache_misses_when_anything_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _replay_rows()
    backend = _ReplayBackend()
    _calibrate(_replay_history(monkeypatch, rows), backend)
    assert backend.calls == 1

    # An extra review.
    extra = _replay_rows()
    extra.append(
        scheduler_pb2.RwkvHistoricalReviewInputsResponse.Review(
            review_id=(_REPLAY_TODAY_START + 5) * 1_000,
            day_offset=100,
            deck_id=1,
            card_id=99,
            ease=3,
            review_kind=1,
        )
    )
    _calibrate(_replay_history(monkeypatch, extra), backend)
    assert backend.calls == 2

    # Same ids and size, one changed ease.
    changed = _replay_rows()
    changed[3].ease = 1 if changed[3].ease != 1 else 2
    _calibrate(_replay_history(monkeypatch, changed), backend)
    assert backend.calls == 3

    # A field only the replay reads (the preset) changed.
    preset = _replay_rows()
    preset[0].preset_id = 5
    _calibrate(_replay_history(monkeypatch, preset), backend)
    assert backend.calls == 4

    # The cache only keeps two entries: the original history was evicted.
    _calibrate(_replay_history(monkeypatch, rows), backend)
    assert backend.calls == 5

    # Another profile with the very same history.
    other = SimpleNamespace(
        path="/profiles/b/collection.anki2",
        _backend=SimpleNamespace(
            rwkv_historical_review_inputs=lambda request: SimpleNamespace(reviews=rows)
        ),
    )
    _calibrate(_replay_history(monkeypatch, rows, col=other), backend)
    assert backend.calls == 6

    # The original history is still cached for the original profile.
    _calibrate(_replay_history(monkeypatch, rows), backend)
    assert backend.calls == 6

    # A different model, precision or backend instance.
    original = _replay_history(monkeypatch, rows)
    for change in (
        {"model_name": "sm40"},
        {"instant_model_name": "sm57-ft"},
        {"state_precision": "f16"},
    ):
        different = _ReplayBackend()
        for name, value in change.items():
            setattr(different, name, value)
        _calibrate(original, different)
        assert different.calls == 1
    fresh = _ReplayBackend()
    _calibrate(original, fresh)
    assert fresh.calls == 1
    assert backend.calls == 6


def test_forecast_calibration_replay_cache_follows_resident_state_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _replay_rows()
    backend = _ReplayBackend()
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", backend)
    col = SimpleNamespace(
        path="/profiles/a/collection.anki2",
        _backend=SimpleNamespace(
            rwkv_historical_review_inputs=lambda request: SimpleNamespace(reviews=rows)
        ),
    )
    key = (id(backend), id(col))
    generations = dict(rwkv_scheduler._reviewer_backend_warmup_generations)
    monkeypatch.setattr(
        rwkv_scheduler, "_reviewer_backend_warmup_generations", generations
    )
    _calibrate(_replay_history(monkeypatch, rows, col=col), backend)
    assert backend.calls == 1

    # The state changed after the history was read: neither read nor stored.
    stale = _replay_history(monkeypatch, rows, col=col)
    generations[key] = 1
    _calibrate(stale, backend)
    assert backend.calls == 2
    assert len(rwkv_scheduler._rwkv_forecast_replay_cache) == 1

    # A history read in the new epoch is a miss, then cached.
    _calibrate(_replay_history(monkeypatch, rows, col=col), backend)
    assert backend.calls == 3
    _calibrate(_replay_history(monkeypatch, rows, col=col), backend)
    assert backend.calls == 3

    # Invalidating the resident state drops the cached predictions.
    rwkv_scheduler._invalidate_reviewer_backend_state(
        SimpleNamespace(mw=SimpleNamespace(col=col)), reason="test"
    )
    assert not rwkv_scheduler._rwkv_forecast_replay_cache


def test_forecast_calibration_without_a_runtime_identity_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _ReplayBackend()
    backend.state_precision = None  # type: ignore[assignment]
    rows = _replay_rows()
    _calibrate(_replay_history(monkeypatch, rows), backend)
    _calibrate(_replay_history(monkeypatch, rows), backend)
    assert backend.calls == 2
    assert not rwkv_scheduler._rwkv_forecast_replay_cache


def test_forecast_calibration_with_too_little_history_never_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _ReplayBackend()
    history = _replay_history(monkeypatch, _replay_rows(days=1))
    assert history.calibration_replay is None
    assert _calibrate(history, backend) is None
    assert backend.calls == 0


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
    # A one-answer forecast preserves its queue but scales the sampled two
    # seconds by the model's 10-second prediction / one-second reference.
    time_model = (1.0, [(10.0, 0.0, 0.0, 1.0, 10.0)] * 4)
    timed = runtime.forecast_instant_workload(**args, review_time_model=time_model)
    assert timed[1][0][2][0] == 1.0
    assert timed[1][0][5][0] == pytest.approx(20.0)
    r_model = (1.0, [(1.0, 10.0, 0.0, 1.0, 5.0)] * 4)
    shifted_times = runtime.forecast_instant_workload(
        **{**args, "paths": 3}, scenario_sigma=1.0, review_time_model=r_model
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
