# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tools import rwkv_content_calibration_probe as probe


def test_content_calibration_report_compares_yomitan_strategies(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "collection.anki2"
    _write_content_probe_db(db_path)

    report = probe.content_calibration_report(
        db_path,
        copied_from=None,
        deck_match="Yomitan",
        front_field="Front",
        reading_field="Reading",
        frequency_field="Frequency",
        train_fraction=0.70,
        answer_time_cap_seconds=300,
        limit=0,
        min_samples=20,
        min_group_reviews=2,
        group_l2=0.01,
        max_iterations=25,
        frequency_bins=4,
        strategy_names=None,
    )

    baseline = report["strategies"]["rwkv_baseline"]["test"]
    kanji_list = report["strategies"]["kanji_list"]["test"]
    frequency = report["strategies"]["frequency"]["test"]
    pair = report["strategies"]["kanji_reading_pair"]["test"]

    assert report["input"]["used_reviews"] == 60
    assert report["input"]["eligible_cached_reviews"] == 60
    assert report["split"]["train"]["count"] == 42
    assert report["split"]["test"]["count"] == 18
    assert kanji_list["log_loss"] < baseline["log_loss"]
    assert kanji_list["rmse_bins"] < baseline["rmse_bins"]
    assert frequency["log_loss"] < baseline["log_loss"]
    assert pair["log_loss"] < baseline["log_loss"]
    assert report["strategies"]["kanji_list"]["groups"]["trained"] == 2
    assert (
        report["strategies"]["kanji_reading_pair"]["groups"]["fallback_test_reviews"]
        == 0
    )


def test_content_samples_read_schema11_model_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "collection.anki2"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE col (models TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO col (models) VALUES (?)",
            (
                json.dumps(
                    {
                        "123": {
                            "flds": [
                                {"name": "Front", "ord": 0},
                                {"name": "Reading", "ord": 1},
                                {"name": "Frequency", "ord": 2},
                            ]
                        }
                    }
                ),
            ),
        )

        field_ords = probe._field_ord_maps(
            connection,
            ("Front", "Reading", "Frequency"),
        )
    finally:
        connection.close()

    assert field_ords == {123: {"Front": 0, "Reading": 1, "Frequency": 2}}


def test_content_feature_helpers_extract_requested_signals() -> None:
    assert probe._unique_kanji("日本語かな日本") == ("日", "本", "語")
    assert probe._kanji_count("日本語かな日本") == 5
    assert probe._parse_frequency_rank("freq 12,345") == 12345
    assert probe._plain_text("<span>語&nbsp;彙</span>") == "語 彙"


def _write_content_probe_db(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            f"""
            CREATE TABLE decks (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE fields (
                ntid INTEGER NOT NULL,
                ord INTEGER NOT NULL,
                name TEXT NOT NULL,
                config BLOB NOT NULL,
                PRIMARY KEY (ntid, ord)
            );
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY,
                mid INTEGER NOT NULL,
                flds TEXT NOT NULL
            );
            CREATE TABLE cards (
                id INTEGER PRIMARY KEY,
                nid INTEGER NOT NULL,
                did INTEGER NOT NULL
            );
            CREATE TABLE revlog (
                id INTEGER PRIMARY KEY,
                cid INTEGER NOT NULL,
                ease INTEGER NOT NULL,
                ivl INTEGER NOT NULL,
                lastIvl INTEGER NOT NULL,
                factor INTEGER NOT NULL,
                time INTEGER NOT NULL,
                type INTEGER NOT NULL
            );
            CREATE TABLE {probe.RWKV_CACHE_TABLE} (
                revlog_id INTEGER PRIMARY KEY,
                prediction REAL NOT NULL,
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO decks (id, name) VALUES (?, ?)",
            [
                (1, "Yomitan"),
                (2, "Other"),
            ],
        )
        connection.executemany(
            "INSERT INTO fields (ntid, ord, name, config) VALUES (100, ?, ?, X'')",
            [
                (0, "Front"),
                (1, "Reading"),
                (2, "Frequency"),
            ],
        )

        base_review_id = int(
            datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000
        )
        for index in range(60):
            front, reading, frequency, ease = (
                ("語", "ご", "100", 3) if index % 2 == 0 else ("難", "なん", "5000", 1)
            )
            note_id = 10_000 + index
            card_id = 20_000 + index
            review_id = base_review_id + index * 60_000
            connection.execute(
                "INSERT INTO notes (id, mid, flds) VALUES (?, 100, ?)",
                (note_id, probe.FIELD_SEPARATOR.join([front, reading, frequency])),
            )
            connection.execute(
                "INSERT INTO cards (id, nid, did) VALUES (?, ?, 1)",
                (card_id, note_id),
            )
            connection.execute(
                """
                INSERT INTO revlog
                    (id, cid, ease, ivl, lastIvl, factor, time, type)
                VALUES (?, ?, ?, 1, 1, 2500, 10000, 1)
                """,
                (review_id, card_id, ease),
            )
            connection.execute(
                f"""
                INSERT INTO {probe.RWKV_CACHE_TABLE}
                    (revlog_id, prediction, source, updated_at)
                VALUES (?, 0.3, 'test', 0)
                """,
                (review_id,),
            )
        connection.commit()
    finally:
        connection.close()
