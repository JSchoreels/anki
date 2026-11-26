# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Answer-time predictions of an RWKV model's optional answer-time head (every
size since the time heads for the family (SM117)). The answer time is the whole
time on the card, front and back: question shown -> grade press, capped at the
preset's maximum answer seconds.

The native runtime computes them from the frozen model at the query and the
review history; this module holds the result and its Card Info rows. Scheduling
never reads them.

Once the native runtime has a fit of the rolling recalibration of the timed
answer-time probabilities (SM175), it also returns, per threshold, the
recalibrated chance of a recalled, a Good or Easy and an Easy answer within
that time; the recall-by-answer-time chart then shows those. Until the user
has 1,000 time-labelled reviews before the current 7-day block, or while the
fit runs, the chart shows the raw values.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# History features the native runtime expects per query.
ANSWER_TIME_FEATURES = 26
GRADE_LABELS = ("Again", "Hard", "Good", "Easy")
ANSWER_TIME_THRESHOLDS_SECONDS = (2, 4, 6, 8, 10, 15, 20, 30)
# Upper edge of the head's seventh time bin (bins 0..6): 3,891 ms.
FAST_ANSWER_SECONDS = 3.9
# The head's last bin holds every answer of 60 s or more (Anki's time cap).
CAPPED_ANSWER_SECONDS = 60.0


@dataclass(frozen=True)
class RwkvAnswerTimePrediction:
    median_seconds: tuple[float, float, float, float]
    fast_probabilities: tuple[float, float, float, float]
    mixed_median_seconds: float
    # For each requested threshold: P(time < threshold | button), in grade order.
    under_probabilities: tuple[tuple[float, float, float, float], ...] = ()
    # For each requested threshold, recalibrated (SM175): P(recalled and
    # within), P(Good or Easy and within), P(Easy and within); empty without
    # a recalibration.
    recalibrated_within: tuple[tuple[float, float, float], ...] = ()


def answer_time_prediction_from_native(
    value: object,
) -> RwkvAnswerTimePrediction | None:
    """Parse one native `predict_answer_times` result."""
    if not isinstance(value, tuple) or len(value) not in (4, 5, 6):
        return None
    medians, fast, mixed, _buttons = value[:4]
    medians = _four_floats(medians)
    fast = _four_floats(fast)
    if medians is None or fast is None or not _finite(mixed):
        return None
    if not all(value > 0 for value in medians) or float(mixed) <= 0:
        return None
    if not all(0.0 <= value <= 1.0 + 1e-6 for value in fast):
        return None
    under: tuple[tuple[float, float, float, float], ...] = ()
    if len(value) >= 5:
        raw_under = value[4]
        if not isinstance(raw_under, Sequence) or len(raw_under) != len(
            ANSWER_TIME_THRESHOLDS_SECONDS
        ):
            return None
        parsed = [_four_floats(row) for row in raw_under]
        if any(row is None for row in parsed):
            return None
        under = tuple(row for row in parsed if row is not None)
        if not all(
            0 <= probability <= 1.0 + 1e-6 for row in under for probability in row
        ):
            return None
        if any(
            later[grade] + 1e-6 < earlier[grade]
            for earlier, later in zip(under, under[1:], strict=False)
            for grade in range(4)
        ):
            return None
    recalibrated: tuple[tuple[float, float, float], ...] = ()
    if len(value) == 6 and value[5] is not None:
        parsed_recalibrated = _recalibrated_within(value[5])
        if parsed_recalibrated is None:
            return None
        recalibrated = parsed_recalibrated
    return RwkvAnswerTimePrediction(
        median_seconds=medians,
        fast_probabilities=fast,
        mixed_median_seconds=float(mixed),
        under_probabilities=under,
        recalibrated_within=recalibrated,
    )


def _recalibrated_within(
    values: object,
) -> tuple[tuple[float, float, float], ...] | None:
    """The recalibrated events per threshold: in [0, 1], non-decreasing in
    the threshold and Easy <= Good or Easy <= recalled (the native repair)."""
    if not isinstance(values, Sequence) or len(values) != len(
        ANSWER_TIME_THRESHOLDS_SECONDS
    ):
        return None
    rows: list[tuple[float, float, float]] = []
    for row in values:
        if (
            not isinstance(row, Sequence)
            or len(row) != 3
            or not all(_finite(value) for value in row)
        ):
            return None
        recalled, good_or_easy, easy = (float(value) for value in row)
        if not (
            0.0 <= easy <= good_or_easy + 1e-6
            and good_or_easy <= recalled + 1e-6
            and recalled <= 1.0 + 1e-6
        ):
            return None
        rows.append((recalled, good_or_easy, easy))
    if any(
        later[index] + 1e-6 < earlier[index]
        for earlier, later in zip(rows, rows[1:])
        for index in range(3)
    ):
        return None
    return tuple(rows)


def answer_time_card_info_rows(
    prediction: RwkvAnswerTimePrediction | None,
) -> list[tuple[str, str]]:
    if prediction is None:
        return []
    by_button = " ".join(
        f"{label}:{format_answer_seconds(seconds)}"
        for label, seconds in zip(GRADE_LABELS, prediction.median_seconds, strict=True)
    )
    fast_good = round(prediction.fast_probabilities[2] * 100)
    rows = [
        ("RWKV : Answer Time by Button", by_button),
        (
            "RWKV : Expected Answer Time",
            f"{format_answer_seconds(prediction.mixed_median_seconds)} "
            f"(under {FAST_ANSWER_SECONDS:g}s if Good: {fast_good}%)",
        ),
    ]
    if prediction.under_probabilities:
        under_good = " ".join(
            f"{seconds}s:{round(probabilities[2] * 100)}%"
            for seconds, probabilities in zip(
                ANSWER_TIME_THRESHOLDS_SECONDS,
                prediction.under_probabilities,
                strict=True,
            )
        )
        rows.append(("RWKV : Time Under (if Good)", under_good))
    return rows


def answer_time_recall_curve(
    prediction: RwkvAnswerTimePrediction | None,
    button_probabilities: Sequence[float] | None,
) -> list[tuple[int, float, float, float]]:
    """Chance of success, Good, and Easy within each time threshold; the
    recalibrated values (SM175) when the runtime has a fit."""
    if prediction is not None and prediction.recalibrated_within:
        points = [(0, 0.0, 0.0, 0.0)]
        for seconds, (recalled, good_or_easy, easy) in zip(
            ANSWER_TIME_THRESHOLDS_SECONDS, prediction.recalibrated_within, strict=True
        ):
            points.append((seconds, recalled, max(0.0, good_or_easy - easy), easy))
        return points
    if prediction is None or not prediction.under_probabilities:
        return []
    buttons = _four_floats(button_probabilities)
    if buttons is None or not all(0 <= value <= 1 for value in buttons):
        return []
    if not math.isclose(sum(buttons), 1.0, abs_tol=0.01):
        return []
    points = [(0, 0.0, 0.0, 0.0)]
    for seconds, under in zip(
        ANSWER_TIME_THRESHOLDS_SECONDS, prediction.under_probabilities, strict=True
    ):
        good_in_time = buttons[2] * under[2]
        easy_in_time = buttons[3] * under[3]
        recalled_in_time = buttons[1] * under[1] + good_in_time + easy_in_time
        points.append((seconds, recalled_in_time, good_in_time, easy_in_time))
    return points


def format_answer_seconds(seconds: float) -> str:
    if seconds >= CAPPED_ANSWER_SECONDS - 1e-3:
        return f"{CAPPED_ANSWER_SECONDS:.0f}s+"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _four_floats(values: object) -> tuple[float, float, float, float] | None:
    if not isinstance(values, Sequence) or len(values) != 4:
        return None
    if not all(_finite(value) for value in values):
        return None
    a, b, c, d = (float(value) for value in values)
    return (a, b, c, d)


def _finite(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )
