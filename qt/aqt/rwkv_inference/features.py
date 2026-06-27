# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import math
from typing import Any

CARD_FEATURE_COLUMNS = [
    "scaled_elapsed_days",
    "scaled_elapsed_days_cumulative",
    "scaled_elapsed_seconds",
    "elapsed_seconds_sin",
    "elapsed_seconds_cos",
    "scaled_elapsed_seconds_cumulative",
    "elapsed_seconds_cumulative_sin",
    "elapsed_seconds_cumulative_cos",
    "scaled_duration",
    "rating_1",
    "rating_2",
    "rating_3",
    "rating_4",
    "note_id_is_nan",
    "deck_id_is_nan",
    "preset_id_is_nan",
    "day_offset_diff",
    "day_of_week",
    "diff_new_cards",
    "diff_reviews",
    "cum_new_cards_today",
    "cum_reviews_today",
    "scaled_state",
    "is_query",
]

ID_PLACEHOLDER = 314159265358979323

_STATISTICS = {
    "elapsed_days_mean": 1.51,
    "elapsed_days_std": 1.62,
    "elapsed_days_cumulative_mean": 2.14,
    "elapsed_days_cumulative_std": 2.25,
    "elapsed_seconds_mean": 9.96,
    "elapsed_seconds_std": 5.21,
    "elapsed_seconds_cumulative_mean": 10.86,
    "elapsed_seconds_cumulative_std": 5.8,
    "duration_mean": 8.9,
    "duration_std": 1.07,
    "diff_new_cards_mean": 2.945,
    "diff_new_cards_std": 2.011,
    "diff_reviews_mean": 4.64,
    "diff_reviews_std": 2.59,
    "cum_new_cards_today_mean": 2.55,
    "cum_new_cards_today_std": 1.41,
    "cum_reviews_today_mean": 4.59,
    "cum_reviews_today_std": 1.30,
}


def scale_elapsed_days(x: float) -> float:
    return (_log_elapsed(x) - _STATISTICS["elapsed_days_mean"]) / _STATISTICS[
        "elapsed_days_std"
    ]


def scale_elapsed_days_cumulative(x: float) -> float:
    return (
        _log_elapsed(x) - _STATISTICS["elapsed_days_cumulative_mean"]
    ) / _STATISTICS["elapsed_days_cumulative_std"]


def scale_elapsed_seconds(x: float) -> float:
    return (_log_elapsed(x) - _STATISTICS["elapsed_seconds_mean"]) / _STATISTICS[
        "elapsed_seconds_std"
    ]


def scale_elapsed_seconds_cumulative(x: float) -> float:
    return (
        _log_elapsed(x) - _STATISTICS["elapsed_seconds_cumulative_mean"]
    ) / _STATISTICS["elapsed_seconds_cumulative_std"]


def scale_duration(x: float) -> float:
    return (math.log(10 + x) - _STATISTICS["duration_mean"]) / _STATISTICS[
        "duration_std"
    ]


def scale_diff_new_cards(x: float) -> float:
    return (math.log(3 + x) - _STATISTICS["diff_new_cards_mean"]) / _STATISTICS[
        "diff_new_cards_std"
    ]


def scale_diff_reviews(x: float) -> float:
    return (math.log(3 + x) - _STATISTICS["diff_reviews_mean"]) / _STATISTICS[
        "diff_reviews_std"
    ]


def scale_cum_new_cards_today(x: float) -> float:
    return (math.log(3 + x) - _STATISTICS["cum_new_cards_today_mean"]) / _STATISTICS[
        "cum_new_cards_today_std"
    ]


def scale_cum_reviews_today(x: float) -> float:
    return (math.log(3 + x) - _STATISTICS["cum_reviews_today_mean"]) / _STATISTICS[
        "cum_reviews_today_std"
    ]


def scale_state(x: float) -> float:
    return x - 2


def scale_day_offset_diff(x: float) -> float:
    return math.log(math.log(math.e + x))


def is_missing_id(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _log_elapsed(x: float) -> float:
    return 0 if x == -1 else math.log(1 + 1e-5 + x)
