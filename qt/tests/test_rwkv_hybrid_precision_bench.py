# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import math
import struct
from pathlib import Path

import pytest
from tools.rwkv_hybrid_precision_bench import MODES, comparison


def test_hybrid_comparison_matches_interday_outcomes_before_scoring(tmp_path: Path):
    rows = [
        [1, 10, None, None, None, 1, 0, 0, 0, 4, 0, True],
        [2, 10, None, None, None, 1, 0, 0, 0, 0, 0, True],
        [3, 10, None, None, None, 3, 0, 0, 1, 1, 0, True],
        [4, 20, None, None, None, 1, 0, 0, 1, 1, 0, True],
        [5, 10, None, None, None, 1, 0, 0, 2, 1, 0, False],
    ]
    for mode, probability in zip(MODES, [0.5, 0.75, 0.625], strict=True):
        (tmp_path / f"{mode}.f32").write_bytes(
            struct.pack("<5f", 0, 0, probability, 0, 0)
        )
    result = comparison(rows, tmp_path)
    assert result["reviews"] == 1
    assert result["metrics"]["state_int8"]["log_loss"] == pytest.approx(-math.log(0.5))
    assert result["metrics"]["cards_int4_notes_int8"]["log_loss"] == pytest.approx(
        -math.log(0.75)
    )
    assert result["metrics"]["fsrs7"]["log_loss"] == pytest.approx(-math.log(0.625))
    pair = result["um_plus_pairs"]["state_int8 vs cards_int4_notes_int8"]
    assert pair["count"] == 1
    assert pair["first_score"] == 0.5
    assert pair["second_score"] == 0.25


def test_hybrid_comparison_rejects_incomplete_predictions(tmp_path: Path):
    (tmp_path / "state_int8.f32").write_bytes(struct.pack("<f", 0.5))
    rows = [
        [1, 10, None, None, None, 3, 0, 0, 0, 0, 0, True],
        [2, 10, None, None, None, 3, 0, 0, 1, 1, 0, True],
    ]
    with pytest.raises(ValueError, match="Incomplete predictions"):
        comparison(rows, tmp_path)
