# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Timing samples for a forecast, relative to the scheduler's day boundary."""

from __future__ import annotations

from collections.abc import Iterable

_DAY_SECONDS = 86_400
_HISTORY_DAYS = 365


def review_timing_patterns(
    reviews: Iterable[tuple[int, int, int]],
    *,
    today: int,
    next_day_at: int,
) -> list[tuple[int, list[tuple[int, int]]]]:
    """Group real answers from the last year into complete scheduler days.

    Each input is (review id in milliseconds, scheduler day, duration in
    milliseconds). The output contains (day, [(milliseconds after rollover,
    duration), ...]) in chronological order. Today's incomplete session is
    excluded.
    """

    today_start = next_day_at - _DAY_SECONDS
    by_day: dict[int, list[tuple[int, int, int]]] = {}
    for review_id, day, duration in reviews:
        if not today - _HISTORY_DAYS <= day < today or review_id <= 0:
            continue
        if duration < 0:
            continue
        elapsed_millis = (review_id - today_start * 1000) % (_DAY_SECONDS * 1000)
        by_day.setdefault(day, []).append((review_id, elapsed_millis, duration))
    return [
        (day, [(second, duration) for _, second, duration in sorted(rows)])
        for day, rows in sorted(by_day.items())
    ]
