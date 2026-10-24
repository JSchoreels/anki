# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pairwise UM+ matching Search Stats Extended's umPlusData.ts."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TypedDict


class UmPlusBin(TypedDict):
    index: int
    count: int
    disagreement: float
    first_error: float
    second_error: float


class UmPlusPair(TypedDict):
    count: int
    first_score: float
    second_score: float
    bins: list[UmPlusBin]


def um_plus_pair(
    actual: Sequence[float], first: Sequence[float], second: Sequence[float]
) -> UmPlusPair:
    if not (len(actual) == len(first) == len(second)):
        raise ValueError("UM+ requires aligned, equally sized samples")
    counts = [0] * 41
    disagreement = [0.0] * 41
    errors_first = [0.0] * 41
    errors_second = [0.0] * 41
    for y, a, b in zip(actual, first, second, strict=True):
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in (y, a, b)):
            continue
        # JavaScript Math.round: ties round toward positive infinity, including
        # negative disagreements; Python round() would instead use even ties.
        index = max(0, min(40, math.floor((a - b) * 20 + 0.5) + 20))
        counts[index] += 1
        disagreement[index] += a - b
        errors_first[index] += a - y
        errors_second[index] += b - y
    count = sum(counts)
    bins: list[UmPlusBin] = [
        {
            "index": i,
            "count": n,
            "disagreement": disagreement[i] / n,
            "first_error": errors_first[i] / n,
            "second_error": errors_second[i] / n,
        }
        for i, n in enumerate(counts)
        if n
    ]
    return {
        "count": count,
        "first_score": math.sqrt(
            sum(errors_first[i] ** 2 / n for i, n in enumerate(counts) if n) / count
        )
        if count
        else 0.0,
        "second_score": math.sqrt(
            sum(errors_second[i] ** 2 / n for i, n in enumerate(counts) if n) / count
        )
        if count
        else 0.0,
        "bins": bins,
    }
