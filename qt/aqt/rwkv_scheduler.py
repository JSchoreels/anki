# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import logging
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from anki.scheduler.v3 import SchedulingState, SchedulingStates

logger = logging.getLogger(__name__)

_REVIEWER_PREDICTION_ATTR = "_rwkv_review_prediction"
_reviewer_backend: RwkvReviewerBackend | None = None


@dataclass(frozen=True)
class RwkvRecallPoint:
    elapsed_days: float
    retrievability: float


@dataclass(frozen=True)
class RwkvIntervalOverride:
    again: int | None = None
    hard: int | None = None
    good: int | None = None
    easy: int | None = None


@dataclass(frozen=True)
class RwkvReviewPrediction:
    retrievability: float | None = None
    interval_overrides: RwkvIntervalOverride = RwkvIntervalOverride()


@dataclass(frozen=True)
class RwkvReviewerPrediction:
    card_id: int
    retrievability: float | None
    review_enabled: bool = False
    interval_override_used: bool = False


@dataclass(frozen=True)
class RwkvReviewerDiagnostics:
    retrievability: float | None
    retrievability_source: str


@dataclass(frozen=True)
class RwkvReviewIdentity:
    card_id: int
    note_id: int | None = None
    deck_id: int | None = None
    preset_id: int | None = None


@dataclass(frozen=True)
class RwkvReviewInput:
    identity: RwkvReviewIdentity
    is_query: bool
    ease: int | None
    duration_millis: int | None
    card_type: int | None
    card_queue: int | None
    card_due: int | None
    interval_days: int | None
    ease_factor: int | None
    reps: int | None
    lapses: int | None
    day_offset: int | None
    current_state_kind: str | None
    current_normal_state_kind: str | None
    current_elapsed_days: int | None
    current_elapsed_seconds: int | None


@dataclass(frozen=True)
class RwkvReviewTransition:
    prediction: RwkvReviewPrediction | None = None
    card_state: object | None = None
    note_state: object | None = None
    deck_state: object | None = None
    preset_state: object | None = None
    global_state: object | None = None


class RwkvReviewerBackend(Protocol):
    def predict_review(
        self,
        *,
        reviewer: object,
        card: object,
    ) -> RwkvReviewPrediction | None: ...

    def review_answered(
        self,
        *,
        reviewer: object,
        card: object,
        ease: int,
    ) -> None: ...


class RwkvReviewRuntime(Protocol):
    def review(
        self,
        *,
        review_input: RwkvReviewInput,
        card_state: object | None,
        note_state: object | None,
        deck_state: object | None,
        preset_state: object | None,
        global_state: object | None,
    ) -> RwkvReviewTransition: ...


class RwkvStatefulReviewerBackend:
    def __init__(self, runtime: RwkvReviewRuntime) -> None:
        self._runtime = runtime
        self._card_states: dict[int, object | None] = {}
        self._note_states: dict[int, object | None] = {}
        self._deck_states: dict[int, object | None] = {}
        self._preset_states: dict[int, object | None] = {}
        self._global_state: object | None = None

    def predict_review(
        self,
        *,
        reviewer: object,
        card: object,
    ) -> RwkvReviewPrediction | None:
        identity = rwkv_review_identity(reviewer, card)
        if identity is None:
            return None

        return self._review(
            reviewer=reviewer,
            card=card,
            identity=identity,
            ease=None,
        ).prediction

    def review_answered(
        self,
        *,
        reviewer: object,
        card: object,
        ease: int,
    ) -> None:
        identity = rwkv_review_identity(reviewer, card)
        if identity is None:
            return

        transition = self._review(
            reviewer=reviewer,
            card=card,
            identity=identity,
            ease=ease,
        )
        self._card_states[identity.card_id] = transition.card_state
        _set_entity_state(self._note_states, identity.note_id, transition.note_state)
        _set_entity_state(self._deck_states, identity.deck_id, transition.deck_state)
        _set_entity_state(
            self._preset_states,
            identity.preset_id,
            transition.preset_state,
        )
        self._global_state = transition.global_state

    def _review(
        self,
        *,
        reviewer: object,
        card: object,
        identity: RwkvReviewIdentity,
        ease: int | None,
    ) -> RwkvReviewTransition:
        return self._runtime.review(
            review_input=rwkv_review_input(
                reviewer=reviewer,
                card=card,
                identity=identity,
                ease=ease,
            ),
            card_state=self._card_states.get(identity.card_id),
            note_state=_entity_state(self._note_states, identity.note_id),
            deck_state=_entity_state(self._deck_states, identity.deck_id),
            preset_state=_entity_state(self._preset_states, identity.preset_id),
            global_state=self._global_state,
        )


def set_reviewer_backend(
    backend: RwkvReviewerBackend | None,
) -> RwkvReviewerBackend | None:
    global _reviewer_backend

    previous = _reviewer_backend
    _reviewer_backend = backend
    return previous


def configure_reviewer_backend_from_environment() -> bool:
    if _reviewer_backend is not None:
        return True

    benchmark_path = os.environ.get("ANKI_RWKV_BENCHMARK_PATH")
    model_path = os.environ.get("ANKI_RWKV_MODEL_PATH")
    if not benchmark_path and not model_path:
        return False
    if not benchmark_path or not model_path:
        logger.warning(
            "RWKV scheduler requires both ANKI_RWKV_BENCHMARK_PATH and ANKI_RWKV_MODEL_PATH"
        )
        return False

    try:
        from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

        set_reviewer_backend(
            SrsBenchmarkRwkvReviewerBackend(
                benchmark_path=benchmark_path,
                model_path=model_path,
                device=os.environ.get("ANKI_RWKV_DEVICE", "cpu"),
                dtype=os.environ.get("ANKI_RWKV_DTYPE", "float"),
            )
        )
        return True
    except Exception:
        logger.exception("failed to configure RWKV scheduler backend")
        return False


def update_reviewer_scheduling_states(
    states: SchedulingStates,
    reviewer: object,
    card: object,
) -> SchedulingStates:
    """Apply desktop RWKV predictions before answer buttons are rendered."""

    if _reviewer_backend is None:
        return states

    try:
        review_enabled = rwkv_review_enabled(reviewer, card)
        prediction = _reviewer_backend.predict_review(
            reviewer=reviewer,
            card=card,
        )
        if prediction is None:
            return states

        _validate_prediction(prediction)
        has_interval_overrides = _has_interval_overrides(prediction.interval_overrides)
        _store_reviewer_prediction(
            reviewer,
            card,
            prediction,
            review_enabled=review_enabled,
            interval_override_used=review_enabled and has_interval_overrides,
        )
        if review_enabled and has_interval_overrides:
            return apply_review_interval_overrides(
                states,
                prediction.interval_overrides,
            )
    except Exception:
        logger.exception("RWKV scheduling prediction failed")

    return states


def record_reviewer_answer(
    reviewer: object,
    card: object,
    ease: int,
) -> None:
    """Update desktop RWKV state after a real review has been answered."""

    if _reviewer_backend is None:
        return

    try:
        _reviewer_backend.review_answered(
            reviewer=reviewer,
            card=card,
            ease=ease,
        )
    except Exception:
        logger.exception("RWKV review state update failed")


def current_reviewer_retrievability(
    reviewer: object,
    card: object,
) -> float | None:
    prediction = _current_reviewer_prediction(reviewer, card)
    return prediction.retrievability if prediction else None


def current_reviewer_diagnostics(
    reviewer: object,
    card: object,
    *,
    fallback_source: str,
) -> RwkvReviewerDiagnostics | None:
    prediction = _current_reviewer_prediction(reviewer, card)
    if prediction is None:
        return None

    return RwkvReviewerDiagnostics(
        retrievability=prediction.retrievability,
        retrievability_source=_retrievability_source(prediction, fallback_source),
    )


def has_reviewer_prediction(reviewer: object) -> bool:
    return isinstance(
        getattr(reviewer, _REVIEWER_PREDICTION_ATTR, None), RwkvReviewerPrediction
    )


def rwkv_card_info_rows(
    *,
    reviewer: object,
    card: object,
    fallback_source: str,
) -> list[tuple[str, str]]:
    diagnostics = current_reviewer_diagnostics(
        reviewer,
        card,
        fallback_source=fallback_source,
    )
    if diagnostics is None:
        return []

    return [
        ("RWKV computed R", _format_retrievability(diagnostics.retrievability)),
        ("Retrievability source", diagnostics.retrievability_source),
    ]


def rwkv_review_enabled(
    reviewer: object,
    card: object,
) -> bool:
    deck_id = _deck_id(card)
    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if not isinstance(deck_config, dict):
        return False

    for key in ("rwkvReviewEnabled", "rwkv_review_enabled"):
        value = deck_config.get(key)
        if isinstance(value, bool):
            return value

    return False


def rwkv_review_identity(
    reviewer: object,
    card: object,
) -> RwkvReviewIdentity | None:
    card_id = _int_attr(card, "id")
    if card_id is None:
        return None
    deck_id = _deck_id(card)

    return RwkvReviewIdentity(
        card_id=card_id,
        note_id=_int_attr(card, "nid"),
        deck_id=deck_id,
        preset_id=_preset_id(reviewer, deck_id),
    )


def rwkv_review_input(
    *,
    reviewer: object,
    card: object,
    identity: RwkvReviewIdentity,
    ease: int | None,
) -> RwkvReviewInput:
    current_state = _current_scheduling_state(reviewer)
    state_kind, normal_state_kind = _scheduling_state_kinds(current_state)
    elapsed_days, elapsed_seconds = _scheduling_state_elapsed(current_state)

    return RwkvReviewInput(
        identity=identity,
        is_query=ease is None,
        ease=ease,
        duration_millis=_duration_millis(card, ease),
        card_type=_int_attr(card, "type"),
        card_queue=_int_attr(card, "queue"),
        card_due=_int_attr(card, "due"),
        interval_days=_int_attr(card, "ivl"),
        ease_factor=_int_attr(card, "factor"),
        reps=_int_attr(card, "reps"),
        lapses=_int_attr(card, "lapses"),
        day_offset=_day_offset(reviewer),
        current_state_kind=state_kind,
        current_normal_state_kind=normal_state_kind,
        current_elapsed_days=elapsed_days,
        current_elapsed_seconds=elapsed_seconds,
    )


def interval_from_recall_curve(
    points: Sequence[RwkvRecallPoint],
    target_retention: float,
    *,
    max_interval_days: int,
    nonmonotonic_tolerance: float = 1e-4,
) -> int | None:
    """Return the first interval where projected recall reaches the target."""

    if not _valid_probability(target_retention):
        raise ValueError("target_retention must be between 0 and 1")
    if max_interval_days < 1:
        raise ValueError("max_interval_days must be at least 1")
    if not math.isfinite(nonmonotonic_tolerance) or nonmonotonic_tolerance < 0:
        raise ValueError("nonmonotonic_tolerance must be finite and non-negative")

    ordered_points = sorted(points, key=lambda point: point.elapsed_days)
    _validate_recall_points(ordered_points)
    if not ordered_points:
        return None
    if not _recall_curve_is_monotonic(
        ordered_points,
        tolerance=nonmonotonic_tolerance,
    ):
        return None

    previous = ordered_points[0]
    if previous.retrievability <= target_retention:
        return _clamped_interval(previous.elapsed_days, max_interval_days)

    for point in ordered_points[1:]:
        if point.retrievability <= target_retention:
            return _clamped_interval(
                _interpolated_elapsed_days(previous, point, target_retention),
                max_interval_days,
            )

        previous = point

    return None


def apply_review_interval_overrides(
    states: SchedulingStates,
    overrides: RwkvIntervalOverride,
) -> SchedulingStates:
    """Apply RWKV day intervals to review answers without mutating input states."""

    updated_states = SchedulingStates()
    updated_states.CopyFrom(states)

    for rating, interval in (
        ("again", overrides.again),
        ("hard", overrides.hard),
        ("good", overrides.good),
        ("easy", overrides.easy),
    ):
        if interval is None:
            continue
        _set_review_interval_if_present(
            getattr(updated_states, rating),
            _validated_interval(interval),
        )

    return updated_states


def _validate_prediction(prediction: RwkvReviewPrediction) -> None:
    if prediction.retrievability is not None and not _valid_probability(
        prediction.retrievability
    ):
        raise ValueError("retrievability must be between 0 and 1")


def _store_reviewer_prediction(
    reviewer: object,
    card: object,
    prediction: RwkvReviewPrediction,
    *,
    review_enabled: bool,
    interval_override_used: bool,
) -> None:
    card_id = _card_id(card)
    if card_id is None:
        return

    setattr(
        reviewer,
        _REVIEWER_PREDICTION_ATTR,
        RwkvReviewerPrediction(
            card_id=card_id,
            retrievability=prediction.retrievability,
            review_enabled=review_enabled,
            interval_override_used=interval_override_used,
        ),
    )


def _current_reviewer_prediction(
    reviewer: object,
    card: object,
) -> RwkvReviewerPrediction | None:
    prediction = getattr(reviewer, _REVIEWER_PREDICTION_ATTR, None)
    if not isinstance(prediction, RwkvReviewerPrediction):
        return None
    if prediction.card_id != _card_id(card):
        return None

    return prediction


def _retrievability_source(
    prediction: RwkvReviewerPrediction,
    fallback_source: str,
) -> str:
    if prediction.review_enabled and prediction.interval_override_used:
        return "RWKV"
    if prediction.review_enabled:
        return f"{fallback_source} (RWKV interval unavailable)"
    return f"{fallback_source} (RWKV disabled)"


def _format_retrievability(retrievability: float | None) -> str:
    if retrievability is None:
        return "Unavailable"

    return f"{retrievability * 100:.0f}%"


def _card_id(card: object) -> int | None:
    return _int_attr(card, "id")


def _deck_id(card: object) -> int | None:
    current_deck_id = getattr(card, "current_deck_id", None)
    if callable(current_deck_id):
        try:
            value = current_deck_id()
            if isinstance(value, int):
                return value
        except Exception:
            logger.debug("failed to read current deck id for RWKV review input")

    return _int_attr(card, "did")


def _preset_id(reviewer: object, deck_id: int | None) -> int | None:
    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if isinstance(deck_config, dict):
        value = deck_config.get("id")
        if isinstance(value, int):
            return value

    return None


def _deck_config_for_deck_id(
    reviewer: object,
    deck_id: int | None,
) -> object | None:
    if deck_id is None:
        return None

    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    decks = getattr(col, "decks", None)
    config_dict_for_deck_id = getattr(decks, "config_dict_for_deck_id", None)
    if not callable(config_dict_for_deck_id):
        return None

    try:
        return config_dict_for_deck_id(deck_id)
    except Exception:
        logger.debug("failed to read deck config for RWKV review input")
        return None


def _duration_millis(card: object, ease: int | None) -> int | None:
    if ease is None:
        return None

    time_taken = getattr(card, "time_taken", None)
    if not callable(time_taken):
        return None

    try:
        value = time_taken(capped=False)
    except TypeError:
        value = time_taken(False)
    except Exception:
        logger.debug("failed to read answer duration for RWKV review input")
        return None

    return value if isinstance(value, int) else None


def _day_offset(reviewer: object) -> int | None:
    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    sched = getattr(col, "sched", None)
    timing_today = getattr(sched, "_timing_today", None)
    if not callable(timing_today):
        return None

    try:
        days_elapsed = getattr(timing_today(), "days_elapsed", None)
    except Exception:
        logger.debug("failed to read scheduler timing for RWKV review input")
        return None

    return days_elapsed if isinstance(days_elapsed, int) else None


def _current_scheduling_state(reviewer: object) -> SchedulingState | None:
    v3 = getattr(reviewer, "_v3", None)
    states = getattr(v3, "states", None)
    current = getattr(states, "current", None)
    return current if isinstance(current, SchedulingState) else None


def _scheduling_state_kinds(
    state: SchedulingState | None,
) -> tuple[str | None, str | None]:
    if state is None:
        return None, None

    state_kind = state.WhichOneof("kind")
    normal_state_kind = (
        state.normal.WhichOneof("kind") if state_kind == "normal" else None
    )
    return state_kind, normal_state_kind


def _scheduling_state_elapsed(
    state: SchedulingState | None,
) -> tuple[int | None, int | None]:
    if state is None or state.WhichOneof("kind") != "normal":
        return None, None

    normal_kind = state.normal.WhichOneof("kind")
    if normal_kind == "review":
        return state.normal.review.elapsed_days, None
    if normal_kind == "learning":
        return None, state.normal.learning.elapsed_secs
    if normal_kind == "relearning":
        return (
            state.normal.relearning.review.elapsed_days,
            state.normal.relearning.learning.elapsed_secs,
        )

    return None, None


def _int_attr(instance: object, attr: str) -> int | None:
    value = getattr(instance, attr, None)
    return value if isinstance(value, int) else None


def _entity_state(states: dict[int, object | None], key: int | None) -> object | None:
    return states.get(key) if key is not None else None


def _set_entity_state(
    states: dict[int, object | None],
    key: int | None,
    state: object | None,
) -> None:
    if key is not None:
        states[key] = state


def _has_interval_overrides(overrides: RwkvIntervalOverride) -> bool:
    return any(
        interval is not None
        for interval in (
            overrides.again,
            overrides.hard,
            overrides.good,
            overrides.easy,
        )
    )


def _validate_recall_points(points: Sequence[RwkvRecallPoint]) -> None:
    previous_elapsed_days: float | None = None

    for point in points:
        if not math.isfinite(point.elapsed_days) or point.elapsed_days < 0:
            raise ValueError("elapsed_days must be finite and non-negative")
        if not _valid_probability(point.retrievability):
            raise ValueError("retrievability must be between 0 and 1")
        if (
            previous_elapsed_days is not None
            and point.elapsed_days <= previous_elapsed_days
        ):
            raise ValueError("elapsed_days must be unique")
        previous_elapsed_days = point.elapsed_days


def _recall_curve_is_monotonic(
    points: Sequence[RwkvRecallPoint],
    *,
    tolerance: float,
) -> bool:
    previous = points[0]
    for point in points[1:]:
        if point.retrievability > previous.retrievability + tolerance:
            return False
        previous = point

    return True


def _valid_probability(value: float) -> bool:
    return math.isfinite(value) and 0 <= value <= 1


def _interpolated_elapsed_days(
    previous: RwkvRecallPoint,
    point: RwkvRecallPoint,
    target_retention: float,
) -> float:
    recall_delta = previous.retrievability - point.retrievability
    if recall_delta <= 0:
        return point.elapsed_days

    elapsed_delta = point.elapsed_days - previous.elapsed_days
    target_fraction = (previous.retrievability - target_retention) / recall_delta
    return previous.elapsed_days + elapsed_delta * target_fraction


def _clamped_interval(elapsed_days: float, max_interval_days: int) -> int:
    return min(max(1, math.ceil(elapsed_days)), max_interval_days)


def _validated_interval(interval: int) -> int:
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
        raise ValueError("interval overrides must be positive day counts")
    return interval


def _set_review_interval_if_present(
    state: SchedulingState,
    interval: int,
) -> None:
    if state.WhichOneof("kind") != "normal":
        return
    if state.normal.WhichOneof("kind") != "review":
        return

    state.normal.review.scheduled_days = interval
    state.normal.review.fuzz_delta_days = 0
