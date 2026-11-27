# Copyright: Anki Software, LLC and contributors
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
        ("RWKV : Expected Answer Time", "6.4s (under 3.9s if Good: 31%)"),
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


def test_answer_time_thresholds_and_recall_within_time() -> None:
    # Each row is P(under threshold | Again, Hard, Good, Easy).
    under = [
        (0.02, 0.05, 0.10, 0.20),
        (0.10, 0.20, 0.30, 0.40),
        (0.20, 0.35, 0.50, 0.65),
        (0.35, 0.50, 0.65, 0.75),
        (0.50, 0.60, 0.75, 0.85),
        (0.60, 0.75, 0.85, 0.92),
        (0.70, 0.85, 0.90, 0.96),
        (0.90, 0.95, 0.98, 0.99),
    ]
    prediction = answer_time_prediction_from_native((*NATIVE, under))
    assert prediction is not None
    assert answer_time_card_info_rows(prediction)[-1] == (
        "RWKV : Time Under (if Good)",
        "2s:10% 4s:30% 6s:50% 8s:65% 10s:75% 15s:85% 20s:90% 30s:98%",
    )

    # Success includes Hard, Good and Easy; the other curves show only the
    # named button, weighted by its own answer probability.
    curve = answer_time_recall_curve(prediction, (0.2, 0.1, 0.6, 0.1))
    assert [point[0] for point in curve] == [0, 2, 4, 6, 8, 10, 15, 20, 30]
    assert [point[1] for point in curve] == pytest.approx(
        [0, 0.085, 0.24, 0.4, 0.515, 0.595, 0.677, 0.721, 0.782]
    )
    assert [point[2] for point in curve] == pytest.approx(
        [0, 0.06, 0.18, 0.3, 0.39, 0.45, 0.51, 0.54, 0.588]
    )
    assert [point[3] for point in curve] == pytest.approx(
        [0, 0.02, 0.04, 0.065, 0.075, 0.085, 0.092, 0.096, 0.099]
    )
    assert all(
        point[1:] == (0, 0, 0)
        for point in answer_time_recall_curve(prediction, (1, 0, 0, 0))
    )
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


def test_recalibrated_answer_times_drive_the_recall_curve() -> None:
    # The native runtime's sixth element (SM175): per threshold, recalibrated
    # P(recalled / Good or Easy / Easy and answered within S s).
    under = [(0.2, 0.4, 0.5, 0.7)] * 8
    recalibrated = [
        (0.10, 0.08, 0.01),
        (0.30, 0.25, 0.03),
        (0.45, 0.38, 0.05),
        (0.55, 0.47, 0.06),
        (0.62, 0.53, 0.07),
        (0.70, 0.60, 0.08),
        (0.74, 0.64, 0.09),
        (0.78, 0.67, 0.09),
    ]
    prediction = answer_time_prediction_from_native((*NATIVE, under, recalibrated))
    assert prediction is not None
    assert prediction.recalibrated_within == tuple(recalibrated)
    curve = answer_time_recall_curve(prediction, (0.2, 0.1, 0.6, 0.1))
    assert [point[0] for point in curve] == [0, 2, 4, 6, 8, 10, 15, 20, 30]
    assert [point[1] for point in curve] == pytest.approx(
        [0, *(row[0] for row in recalibrated)]
    )
    # The chart's Good series is Good alone: Good or Easy minus Easy.
    assert [point[2] for point in curve] == pytest.approx(
        [0, *(row[1] - row[2] for row in recalibrated)]
    )
    assert [point[3] for point in curve] == pytest.approx(
        [0, *(row[2] for row in recalibrated)]
    )
    # Without a fit the sixth element is None and the raw curve is kept.
    raw = answer_time_prediction_from_native((*NATIVE, under, None))
    assert raw is not None and raw.recalibrated_within == ()
    assert answer_time_recall_curve(raw, (0.2, 0.1, 0.6, 0.1))[1][1] == pytest.approx(
        0.1 * 0.4 + 0.6 * 0.5 + 0.1 * 0.7
    )
    # Incoherent recalibrated values are rejected.
    for invalid in (
        recalibrated[:7],
        [(0.1, 0.2, 0.0)] + recalibrated[1:],
        recalibrated[:7] + [(0.70, 0.60, 0.08)],
        [(math.nan, 0.0, 0.0)] + recalibrated[1:],
    ):
        assert answer_time_prediction_from_native((*NATIVE, under, invalid)) is None


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
    assert info.answer_time_recall[0] == (0, 0, 0, 0)
    assert info.answer_time_recall[1][0] == 2
    assert info.answer_time_recall[1][1] == pytest.approx(0.31)
    assert info.answer_time_recall[1][2:] == pytest.approx((0.2, 0.07))
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
