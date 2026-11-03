# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import math

from aqt.rwkv_answer_time import (
    RwkvAnswerTimePrediction,
    answer_time_card_info_rows,
    answer_time_prediction_from_native,
    format_answer_seconds,
)

NATIVE = (
    (14.2, 9.84, 6.12, 4.03),
    (0.05, 0.12, 0.31, 0.45),
    6.4,
    (0.1, 0.1, 0.7, 0.1),
)


def test_native_answer_time_parses_and_formats_card_info_rows() -> None:
    prediction = answer_time_prediction_from_native(NATIVE)
    assert prediction == RwkvAnswerTimePrediction(
        median_seconds=(14.2, 9.84, 6.12, 4.03),
        fast_probabilities=(0.05, 0.12, 0.31, 0.45),
        mixed_median_seconds=6.4,
    )
    assert answer_time_card_info_rows(prediction) == [
        ("RWKV : Answer Time by Button", "Again:14s Hard:9.8s Good:6.1s Easy:4.0s"),
        ("RWKV : Expected Answer Time", "6.4s (Good under 3.9s: 31%)"),
    ]


def test_missing_or_invalid_answer_times_add_no_rows() -> None:
    assert answer_time_card_info_rows(None) == []
    assert answer_time_prediction_from_native(None) is None
    assert answer_time_prediction_from_native((1.0, 2.0)) is None
    medians, fast, mixed, buttons = NATIVE
    for invalid in (
        ((math.nan, 1.0, 1.0, 1.0), fast, mixed, buttons),
        ((0.0, 1.0, 1.0, 1.0), fast, mixed, buttons),
        (medians, (0.5, 0.5, 1.5, 0.5), mixed, buttons),
        (medians, fast, math.inf, buttons),
        (medians, fast, True, buttons),
    ):
        assert answer_time_prediction_from_native(invalid) is None


def test_answer_seconds_formatting() -> None:
    assert format_answer_seconds(0.36) == "0.4s"
    assert format_answer_seconds(9.94) == "9.9s"
    assert format_answer_seconds(12.6) == "13s"
    assert format_answer_seconds(60.0) == "60s+"


def test_backend_without_answer_time_head_predicts_no_answer_time() -> None:
    from aqt import rwkv_scheduler

    class RuntimeWithoutHead:
        def has_answer_time_head(self) -> bool:
            return False

        def predict_answer_times(self, requests: object, history: object) -> object:
            raise AssertionError("must not be called without the head")

    for runtime in (object(), RuntimeWithoutHead()):
        backend = rwkv_scheduler.RwkvStatefulReviewerBackend(runtime)  # type: ignore[arg-type]
        assert not backend.has_answer_time_head()
        candidate = rwkv_scheduler.RwkvReviewCandidate(reviewer=None, card=None)
        assert backend.predict_answer_time(candidate, [0.0] * 26) is None
