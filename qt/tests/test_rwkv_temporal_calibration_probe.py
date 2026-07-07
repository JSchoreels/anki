# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tools import rwkv_temporal_calibration_probe as probe


def test_samples_use_local_review_start_hour_and_session_duration() -> None:
    timezone = ZoneInfo("UTC")
    rows = [
        (_timestamp_millis(2024, 1, 1, 10, 0, 10), 1, 1, 10_000, 0.25),
        (_timestamp_millis(2024, 1, 1, 10, 5, 5), 1, 3, 5_000, 0.75),
        (_timestamp_millis(2024, 1, 1, 11, 10, 5), 1, 4, 5_000, 0.80),
    ]

    samples = probe._samples_from_rows(
        rows,
        timezone=timezone,
        session_gap_seconds=30 * 60,
        answer_time_cap_seconds=300,
    )

    assert samples[0].outcome == 0
    assert samples[1].outcome == 1
    assert math.isclose(
        samples[0].features["hour_sin"], math.sin(2 * math.pi * 10 / 24)
    )
    assert math.isclose(
        samples[0].features["hour_cos"], math.cos(2 * math.pi * 10 / 24)
    )
    assert samples[0].features["session_log"] == 0
    assert math.isclose(samples[1].features["session_log"], math.log1p(5 * 60))
    assert samples[2].features["session_log"] == 0
    assert samples[0].features["pred_bin_2"] == 1.0
    assert samples[1].features["pred_bin_7"] == 1.0
    assert samples[1].features["recent_residual_ewma"] < 0
    assert samples[2].features["prior_lapses_log"] == 0


def test_samples_count_total_and_unique_cards_between_same_card_reviews() -> None:
    timezone = ZoneInfo("UTC")
    rows = [
        (_timestamp_millis(2024, 1, 1, 10, 0, 0), 1, 3, 1_000, 0.70),
        (_timestamp_millis(2024, 1, 1, 10, 1, 0), 2, 3, 1_000, 0.70),
        (_timestamp_millis(2024, 1, 1, 10, 2, 0), 3, 3, 1_000, 0.70),
        (_timestamp_millis(2024, 1, 1, 10, 3, 0), 2, 3, 1_000, 0.70),
        (_timestamp_millis(2024, 1, 1, 10, 4, 0), 1, 3, 1_000, 0.70),
    ]

    samples = probe._samples_from_rows(
        rows,
        timezone=timezone,
        session_gap_seconds=30 * 60,
        answer_time_cap_seconds=300,
    )

    assert samples[0].features["cards_between_total_log"] == 0
    assert samples[0].features["cards_between_unique_log"] == 0
    assert samples[3].features["cards_between_total_log"] == pytest.approx(
        math.log1p(1)
    )
    assert samples[3].features["cards_between_unique_log"] == pytest.approx(
        math.log1p(1)
    )
    assert samples[4].features["cards_between_total_log"] == pytest.approx(
        math.log1p(3)
    )
    assert samples[4].features["cards_between_unique_log"] == pytest.approx(
        math.log1p(2)
    )


def test_temporal_calibrator_improves_when_hour_explains_residual() -> None:
    samples = [
        probe.ReviewSample(
            review_id=index,
            review_start_seconds=index,
            outcome=1 if index % 2 == 0 else 0,
            prediction=0.5,
            recall_bin=(0, 2, 0),
            features={
                "hour_sin": 1.0 if index % 2 == 0 else -1.0,
                "hour_cos": 0.0,
                "session_log": 0.0,
            },
        )
        for index in range(120)
    ]
    split = probe._split_samples(samples, train_fraction=0.70, validation_fraction=0.15)
    prepared = probe._prepare_model_data(split, feature_names=["hour_sin"])

    result = probe._fit_additive_calibrator(prepared, l2=0.01, max_iterations=50)

    baseline = probe._metrics_for_samples(split[2])
    assert result["test"]["log_loss"] < baseline["log_loss"]
    assert result["test"]["brier"] < baseline["brier"]
    assert result["weights"]["hour_sin"] > 0


def test_temporal_calibration_report_reads_cached_predictions(tmp_path: Path) -> None:
    db_path = tmp_path / "collection.anki2"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            f"""
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
            CREATE TABLE cards (
                id INTEGER PRIMARY KEY,
                did INTEGER NOT NULL
            );
            CREATE TABLE {probe.RWKV_CACHE_TABLE} (
                revlog_id INTEGER PRIMARY KEY,
                prediction REAL NOT NULL,
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        for index in range(40):
            review_id = _timestamp_millis(2024, 1, 1, 8, index, 10)
            ease = 3 if index % 2 == 0 else 1
            prediction = 0.45 if index % 2 == 0 else 0.55
            connection.execute(
                "INSERT INTO cards (id, did) VALUES (?, 1)",
                (index,),
            )
            connection.execute(
                """
                INSERT INTO revlog
                    (id, cid, ease, ivl, lastIvl, factor, time, type)
                VALUES (?, ?, ?, 1, 1, 2500, 10000, 1)
                """,
                (review_id, index, ease),
            )
            connection.execute(
                f"""
                INSERT INTO {probe.RWKV_CACHE_TABLE}
                    (revlog_id, prediction, source, updated_at)
                VALUES (?, ?, 'test', 0)
                """,
                (review_id, prediction),
            )
        connection.commit()
    finally:
        connection.close()

    report = probe.temporal_calibration_report(
        db_path,
        copied_from=None,
        timezone=ZoneInfo("UTC"),
        deck_match=None,
        session_gap_seconds=30 * 60,
        answer_time_cap_seconds=300,
        train_fraction=0.70,
        validation_fraction=0.15,
        limit=0,
        l2=0.01,
        max_iterations=50,
        min_samples=20,
    )

    assert report["input"]["used_reviews"] == 40
    assert "rwkv_plus_hour_session" in report["models"]
    assert "rwkv_platt" in report["models"]
    assert "rwkv_confidence_bins" in report["models"]
    assert "rwkv_recent_performance" in report["models"]
    assert "rwkv_maturity_lapse" in report["models"]
    assert "rwkv_cards_between" in report["models"]
    assert "rwkv_maturity_lapse_cards_between" in report["models"]
    assert "rwkv_platt_recent_90d" in report["models"]
    assert (
        report["models"]["rwkv_cards_between"]["description"]
        == "Add both total and distinct intervening-card counts."
    )
    assert (
        "recent training reviews"
        in report["models"]["rwkv_platt_recent_90d"]["description"]
    )
    assert report["split"]["test"]["count"] == 6
    assert "rmse_bins" in report["models"]["rwkv_plus_hour_session"]["test"]


def test_temporal_calibration_report_reads_adjacent_cache_db(tmp_path: Path) -> None:
    db_path = tmp_path / "collection.anki2"
    cache_path = tmp_path / probe.RWKV_CACHE_DB_FILENAME
    connection = sqlite3.connect(db_path)
    cache_connection = sqlite3.connect(cache_path)
    try:
        connection.executescript(
            """
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
            CREATE TABLE cards (
                id INTEGER PRIMARY KEY,
                did INTEGER NOT NULL
            );
            """
        )
        cache_connection.executescript(
            f"""
            CREATE TABLE {probe.RWKV_CACHE_TABLE} (
                revlog_id INTEGER PRIMARY KEY,
                prediction REAL NOT NULL,
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        for index in range(30):
            review_id = _timestamp_millis(2024, 1, 1, 8, index, 10)
            connection.execute(
                "INSERT INTO cards (id, did) VALUES (?, 1)",
                (index,),
            )
            connection.execute(
                """
                INSERT INTO revlog
                    (id, cid, ease, ivl, lastIvl, factor, time, type)
                VALUES (?, ?, 3, 1, 1, 2500, 10000, 1)
                """,
                (review_id, index),
            )
            cache_connection.execute(
                f"""
                INSERT INTO {probe.RWKV_CACHE_TABLE}
                    (revlog_id, prediction, source, updated_at)
                VALUES (?, 0.7, 'test', 0)
                """,
                (review_id,),
            )
        connection.commit()
        cache_connection.commit()
    finally:
        connection.close()
        cache_connection.close()

    report = probe.temporal_calibration_report(
        db_path,
        copied_from=None,
        timezone=ZoneInfo("UTC"),
        deck_match=None,
        session_gap_seconds=30 * 60,
        answer_time_cap_seconds=300,
        train_fraction=0.70,
        validation_fraction=0.15,
        limit=0,
        l2=0.01,
        max_iterations=50,
        min_samples=20,
    )

    assert report["input"]["used_reviews"] == 30


def test_temporal_calibration_report_filters_by_deck_match(tmp_path: Path) -> None:
    db_path = tmp_path / "collection.anki2"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            f"""
            CREATE TABLE decks (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE cards (
                id INTEGER PRIMARY KEY,
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
                (2, "Yomitan\x1fMining"),
                (3, "Other"),
            ],
        )
        for index in range(45):
            deck_id = 2 if index < 30 else 3
            review_id = _timestamp_millis(2024, 1, 1, 9, index, 10)
            connection.execute(
                "INSERT INTO cards (id, did) VALUES (?, ?)",
                (index, deck_id),
            )
            connection.execute(
                """
                INSERT INTO revlog
                    (id, cid, ease, ivl, lastIvl, factor, time, type)
                VALUES (?, ?, 3, 1, 1, 2500, 10000, 1)
                """,
                (review_id, index),
            )
            connection.execute(
                f"""
                INSERT INTO {probe.RWKV_CACHE_TABLE}
                    (revlog_id, prediction, source, updated_at)
                VALUES (?, 0.7, 'test', 0)
                """,
                (review_id,),
            )
        connection.commit()
    finally:
        connection.close()

    report = probe.temporal_calibration_report(
        db_path,
        copied_from=None,
        timezone=ZoneInfo("UTC"),
        deck_match="yomitan",
        session_gap_seconds=30 * 60,
        answer_time_cap_seconds=300,
        train_fraction=0.70,
        validation_fraction=0.15,
        limit=0,
        l2=0.01,
        max_iterations=50,
        min_samples=20,
    )

    assert report["input"]["used_reviews"] == 30


def _timestamp_millis(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
) -> int:
    return int(
        datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=ZoneInfo("UTC"),
        ).timestamp()
        * 1000
    )
