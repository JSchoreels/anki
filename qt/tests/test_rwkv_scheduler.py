# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from anki.scheduler.v3 import SchedulingState, SchedulingStates
from aqt.rwkv_scheduler import (
    RwkvIntervalOverride,
    RwkvRecallPoint,
    RwkvReviewInput,
    RwkvReviewPrediction,
    RwkvReviewTransition,
    RwkvStatefulReviewerBackend,
    apply_review_interval_overrides,
    configure_reviewer_backend_from_environment,
    current_reviewer_diagnostics,
    current_reviewer_retrievability,
    interval_from_recall_curve,
    record_reviewer_answer,
    rwkv_card_info_rows,
    rwkv_review_enabled,
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
    reviewer = _rwkv_reviewer()
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


def test_configure_reviewer_backend_from_environment(monkeypatch) -> None:
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


class _SharedReviewRuntime:
    def __init__(self) -> None:
        self.reviewed: list[tuple[int, int]] = []
        self.queries: list[tuple[int, object | None, object | None]] = []
        self.query_inputs: list[RwkvReviewInput] = []
        self.answered_inputs: list[RwkvReviewInput] = []

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
        return RwkvReviewTransition(
            card_state=("card", identity.card_id, ease),
            note_state=("note", identity.note_id, ease),
            deck_state=("deck", identity.deck_id, review_count + 1),
            preset_state=("preset", identity.preset_id, review_count + 1),
            global_state=review_count + 1,
        )


def _rwkv_reviewer(
    *,
    rwkv_review_enabled: bool = True,
    resolved_preset_id: str | None = "1000",
) -> SimpleNamespace:
    states = SchedulingStates()
    states.current.normal.review.elapsed_days = 7

    class Scheduler:
        def __init__(self) -> None:
            self.states = states

        def _timing_today(self) -> SimpleNamespace:
            return SimpleNamespace(days_elapsed=42)

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
    if resolved_preset_id is not None:
        col.fsrs_preset_for_card = lambda card_id: SimpleNamespace(
            id=resolved_preset_id
        )

    return SimpleNamespace(
        _v3=SimpleNamespace(states=states),
        mw=SimpleNamespace(col=col),
    )


def _expected_preset_hash(preset_id: str) -> int:
    digest = hashlib.blake2b(preset_id.encode("utf8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def _rwkv_card(
    *,
    card_id: int,
    note_id: int,
    duration_millis: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=card_id,
        nid=note_id,
        did=100,
        type=2,
        queue=2,
        due=50,
        ivl=4,
        factor=2500,
        reps=5,
        lapses=1,
        time_taken=lambda capped=True: duration_millis,
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
