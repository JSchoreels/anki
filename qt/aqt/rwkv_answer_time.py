# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Answer-time predictions of the small RWKV model's optional answer-time head.

The native runtime computes them from the frozen model at the query and the
review history; this module holds the result and its Card Info rows. Scheduling
never reads them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# History features the native runtime expects per query.
ANSWER_TIME_FEATURES = 26
GRADE_LABELS = ("Again", "Hard", "Good", "Easy")
# Upper edge of the head's seventh time bin (bins 0..6): 3,891 ms.
FAST_ANSWER_SECONDS = 3.9
# The head's last bin holds every answer of 60 s or more (Anki's time cap).
CAPPED_ANSWER_SECONDS = 60.0


@dataclass(frozen=True)
class RwkvAnswerTimePrediction:
    median_seconds: tuple[float, float, float, float]
    fast_probabilities: tuple[float, float, float, float]
    mixed_median_seconds: float


def answer_time_prediction_from_native(
    value: object,
) -> RwkvAnswerTimePrediction | None:
    """Parse one native `predict_answer_times` result."""
    if not isinstance(value, tuple) or len(value) != 4:
        return None
    medians, fast, mixed, _buttons = value
    medians = _four_floats(medians)
    fast = _four_floats(fast)
    if medians is None or fast is None or not _finite(mixed):
        return None
    if not all(value > 0 for value in medians) or float(mixed) <= 0:
        return None
    if not all(0.0 <= value <= 1.0 + 1e-6 for value in fast):
        return None
    return RwkvAnswerTimePrediction(
        median_seconds=medians,
        fast_probabilities=fast,
        mixed_median_seconds=float(mixed),
    )


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
    return [
        ("RWKV : Answer Time by Button", by_button),
        (
            "RWKV : Expected Answer Time",
            f"{format_answer_seconds(prediction.mixed_median_seconds)} "
            f"(Good under {FAST_ANSWER_SECONDS:g}s: {fast_good}%)",
        ),
    ]


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
