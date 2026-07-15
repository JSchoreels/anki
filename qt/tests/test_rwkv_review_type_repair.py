# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tools import rwkv_review_type_repair as repair


def test_infer_review_type_repairs_follows_same_day_state_contract() -> None:
    rows = [
        _row(2026, 7, 14, 10, 0, card_id=1, ease=1, kind=1),
        _row(2026, 7, 14, 10, 5, card_id=1, ease=3, kind=1),
        _row(2026, 7, 14, 10, 10, card_id=1, ease=3, kind=1),
        _row(2026, 7, 15, 10, 0, card_id=1, ease=3, kind=1),
        _row(2026, 7, 15, 10, 5, card_id=1, ease=3, kind=3),
    ]

    repairs = repair.infer_review_type_repairs(
        rows,
        timezone=ZoneInfo("UTC"),
        rollover_hour=4,
    )

    assert [(item.old_kind, item.new_kind, item.rule) for item in repairs] == [
        (1, 2, "same_day_after_review_1_to_relearning"),
        (1, 3, "same_day_after_relearning_3_to_filtered"),
    ]


def test_reset_breaks_same_day_inference() -> None:
    rows = [
        _row(2026, 7, 14, 10, 0, card_id=1, ease=3, kind=1),
        _row(2026, 7, 14, 10, 1, card_id=1, ease=0, kind=4, factor=0),
        _row(2026, 7, 14, 10, 2, card_id=1, ease=3, kind=0),
    ]

    repairs = repair.infer_review_type_repairs(
        rows,
        timezone=ZoneInfo("UTC"),
        rollover_hour=4,
    )

    assert repairs == []


def test_filtered_original_state_is_reported_as_ambiguous() -> None:
    rows = [
        _row(2026, 7, 14, 10, 0, card_id=1, ease=1, kind=3),
        _row(2026, 7, 14, 10, 5, card_id=1, ease=3, kind=1),
    ]

    repairs, ambiguities = repair.infer_review_type_audit(
        rows,
        timezone=ZoneInfo("UTC"),
        rollover_hour=4,
    )

    assert repairs == []
    assert len(ambiguities) == 1
    assert ambiguities[0].reason == "filtered_original_state_not_reconstructable"


def test_apply_repairs_backs_up_copy_and_invalidates_rwkv_cache(tmp_path: Path) -> None:
    collection = tmp_path / "collection-copy.anki2"
    first = _row(2026, 7, 14, 10, 0, card_id=1, ease=1, kind=1)
    second = _row(2026, 7, 14, 10, 5, card_id=1, ease=3, kind=1)
    with sqlite3.connect(collection) as db:
        db.execute(
            "create table revlog(id integer primary key, cid, ease, type, factor)"
        )
        db.execute(
            "create table search_stats_rwkv_review_retrievability(revlog_id, prediction)"
        )
        db.executemany(
            "insert into revlog values (?, ?, ?, ?, ?)",
            [
                (
                    row.review_id,
                    row.card_id,
                    row.ease,
                    row.review_kind,
                    row.factor,
                )
                for row in (first, second)
            ],
        )
        db.execute(
            "insert into search_stats_rwkv_review_retrievability values (?, ?)",
            (first.review_id, 0.5),
        )

    report = repair.repair_collection_copy(
        collection,
        timezone=ZoneInfo("UTC"),
        rollover_hour=4,
        apply=True,
    )

    assert report["repairs"] == 1
    assert Path(str(report["backup"])).exists()
    assert Path(str(report["auditOutput"])).exists()
    with sqlite3.connect(collection) as db:
        assert db.execute(
            "select type from revlog where id = ?", (second.review_id,)
        ).fetchone() == (2,)
        assert db.execute(
            "select count() from search_stats_rwkv_review_retrievability"
        ).fetchone() == (0,)


def _row(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    *,
    card_id: int,
    ease: int,
    kind: int,
    factor: int = 2500,
) -> repair.ReviewRow:
    review_id = int(
        datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("UTC")).timestamp()
        * 1000
    )
    return repair.ReviewRow(review_id, card_id, ease, kind, factor)
