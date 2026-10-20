# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import array
import json
import math

import pytest
from tools.rwkv_model_bench import calibration, compare, read_predictions


def test_calibration_uses_every_outcome_and_review_weighted_bins() -> None:
    result = calibration([0.0, 1.0, 1.0], [0.25, 0.75, 0.75])
    assert result["log_loss"] == pytest.approx(-math.log(0.75))
    assert result["brier"] == pytest.approx(0.0625)
    assert result["ece_20"] == pytest.approx(0.25)
    assert result["rmse_bins_20"] == pytest.approx(0.25)
    assert result["bias"] == pytest.approx(-1 / 12)
    assert [b["count"] for b in result["bins"]] == [1, 2]


def test_model_comparison_scores_previous_answers_on_identical_rows(tmp_path) -> None:
    rows = [
        [i, card, 1, 1, 1, ease, 1000, 2, 10, days, 86400, True]
        for i, (card, ease, days) in enumerate(
            [(1, 3, 1), (2, 1, 1), (1, 3, 0), (2, 1, 1), (1, 3, 1)]
        )
    ]
    for model in ("legacy_f32", "legacy_int8", "sm40_f32", "sm40_int8"):
        (tmp_path / model).mkdir()
        for head in ("curve", "instant"):
            (tmp_path / model / f"{head}.f32").write_bytes(
                array.array("f", [0.5, 0.5, 0.8, 0.2, 0.8]).tobytes()
            )
    result = compare(rows, tmp_path)
    assert result["interday"]["reviews"] == 2
    assert result["all_repeated"]["reviews"] == 3
    assert result["interday"]["metrics"]["curve"]["sm40_f32"][
        "log_loss"
    ] == pytest.approx(-math.log(0.8))
    assert (
        result["interday"]["um_plus"]["instant"]["legacy_int8 vs sm40_f32"]["count"]
        == 2
    )
    assert (
        result["interday"]["um_plus"]["instant"]["legacy_f32 vs sm40_f32"]["count"] == 2
    )
    assert json.loads(json.dumps(result))["interday"]["reviews"] == 2


def test_incomplete_predictions_cannot_be_reported(tmp_path) -> None:
    path = tmp_path / "truncated.f32"
    path.write_bytes(array.array("f", [0.9]).tobytes())
    with pytest.raises(ValueError, match="Incomplete"):
        read_predictions(path, 2)
