# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import math
from types import SimpleNamespace

import pytest

from aqt.rwkv_answer_time import (
    RwkvAnswerTimePrediction,
    answer_time_card_info_rows,
    answer_time_prediction_from_native,
    answer_time_recall_curve,
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


def test_answer_time_thresholds_and_conditional_recall() -> None:
    # Each row is P(under threshold | Again, Hard, Good, Easy). In this
    # deliberately simple example they are equal at all eight thresholds.
    under = [(0.2, 0.4, 0.5, 0.7)] * 8
    prediction = answer_time_prediction_from_native((*NATIVE, under))
    assert prediction is not None
    assert answer_time_card_info_rows(prediction)[-1] == (
        "RWKV : Time Under (if Good)",
        "2s:50% 4s:50% 6s:50% 8s:50% 10s:50% 15s:50% 20s:50% 30s:50%",
    )

    # The chart conditions on a fast answer: (Hard + Good + Easy) divided by
    # all answers under the threshold, using the active button probabilities.
    curve = answer_time_recall_curve(prediction, (0.4, 0.1, 0.4, 0.1))
    assert [seconds for seconds, _ in curve] == [2, 4, 6, 8, 10, 15, 20, 30]
    assert all(math.isclose(recall, 31 / 39) for _, recall in curve)
    assert answer_time_recall_curve(prediction, None) == []
    assert answer_time_recall_curve(prediction, (0.0, 0.0, 0.0, 0.0)) == []
    assert (
        answer_time_recall_curve(
            answer_time_prediction_from_native(NATIVE), (0.4, 0.1, 0.4, 0.1)
        )
        == []
    )


def test_invalid_or_decreasing_threshold_probabilities_are_rejected() -> None:
    assert answer_time_prediction_from_native((*NATIVE, [(0.1,) * 4])) is None
    assert (
        answer_time_prediction_from_native((*NATIVE, [(0.1,) * 4] * 7 + [(1.1,) * 4]))
        is None
    )
    assert (
        answer_time_prediction_from_native((*NATIVE, [(0.5,) * 4] * 7 + [(0.4,) * 4]))
        is None
    )


def test_card_info_graph_uses_the_active_rwkv_button_probabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt import rwkv_scheduler

    prediction = answer_time_prediction_from_native(
        (*NATIVE, [(0.2, 0.4, 0.5, 0.7)] * 8)
    )
    diagnostics = rwkv_scheduler.RwkvReviewerDiagnostics(
        retrievability=0.6,
        retrievability_source="RWKV",
        button_probabilities=(0.4, 0.1, 0.4, 0.1),
    )
    reviewer = object()
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", object())
    monkeypatch.setattr(rwkv_scheduler, "rwkv_review_active", lambda *_args: True)
    monkeypatch.setattr(rwkv_scheduler, "rwkv_review_enabled", lambda *_args: False)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_card_info_review_candidate",
        lambda *_args: SimpleNamespace(reviewer=reviewer),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_queried_card_info_diagnostics",
        lambda *_args, **_kw: diagnostics,
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_card_info_answer_time_prediction",
        lambda *_args, **_kw: prediction,
    )
    info = rwkv_scheduler.rwkv_card_info(
        reviewer=reviewer,
        card=SimpleNamespace(id=123),
        fallback_source="FSRS",
        include_after_review=False,
    )
    assert info.answer_time_recall[0][0] == 2
    assert info.answer_time_recall[0][1] == pytest.approx(31 / 39)
    assert any(label == "RWKV : Time Under (if Good)" for label, _ in info.rows)


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
