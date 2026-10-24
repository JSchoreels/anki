# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import math

import pytest
from tools.rwkv_um_plus import um_plus_pair


def test_um_plus_groups_disagreement_and_matches_javascript_half_rounding():
    result = um_plus_pair(
        [1.0, 0.0, 1.0, 0.0],
        [0.875, 0.5, 0.625, 0.375],
        [0.75, 0.5, 0.75, 0.5],
    )
    assert result["count"] == 4
    assert [(b["index"], b["count"]) for b in result["bins"]] == [
        (18, 2),
        (20, 1),
        (23, 1),
    ]
    assert result["first_score"] == math.sqrt((0.125**2 + 0.5**2) / 4)
    assert result["second_score"] == math.sqrt((0.25**2 + 0.5**2 + 2 * 0.125**2) / 4)


def test_um_plus_keeps_small_bins_and_skips_invalid_pairs():
    result = um_plus_pair([1.0, math.nan], [0.6, 0.9], [0.7, 0.9])
    assert result["count"] == 1
    assert result["first_score"] == pytest.approx(0.4)
    assert result["second_score"] == pytest.approx(0.3)


def test_um_plus_requires_matching_sample_lengths():
    with pytest.raises(ValueError, match="aligned"):
        um_plus_pair([1.0], [0.8, 0.9], [0.7])
