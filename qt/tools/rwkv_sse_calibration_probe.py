#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

RWKV_CACHE_TABLE = "search_stats_rwkv_review_retrievability"
RWKV_LEGACY_COLUMNS = (
    "rwkv_r",
    "rwkv_review_r",
    "rwkv_review_retrievability",
    "rwkv_r_at_review",
    "rwkv_retrievability",
    "rwkv_retrievability_at_review",
    "rwkvReviewRetrievability",
    "rwkvRetrievability",
    "rwkvR",
)
DAY_SECONDS = 60 * 60 * 24
SQLITE_CHUNK_SIZE = 900


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    copy_dir: Path | None = None
    try:
        db_path, copied_from, copy_dir = _analysis_db_path(args)
        report = _probe(
            db_path,
            copied_from=copied_from,
            revlog_ids=args.revlog_id,
            card_ids=args.card_id,
            day_range=args.day_range,
            rollover_hour=args.rollover_hour,
            limit=args.limit,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1 if report["sse"]["would_show_no_data"] else 0
    except ProbeError as err:
        print(
            json.dumps({"error": str(err)}, indent=2, sort_keys=True), file=sys.stderr
        )
        return 2
    finally:
        if not args.keep_copy and copy_dir is not None:
            shutil.rmtree(copy_dir, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe whether Search Stats Extended would have complete RWKV "
            "calibration retrievability data."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--collection-copy",
        type=Path,
        help="Path to a copied collection.anki2 database to inspect.",
    )
    source.add_argument(
        "--profile-folder",
        type=Path,
        help=(
            "An Anki profile folder to copy before inspection. Anki must be "
            "closed unless --allow-open-profile is passed."
        ),
    )
    parser.add_argument(
        "--allow-open-profile",
        action="store_true",
        help="Allow copying a profile even when lsof reports open handles.",
    )
    parser.add_argument(
        "--keep-copy",
        action="store_true",
        help="Keep the temporary copied database directory and include it in output.",
    )
    parser.add_argument(
        "--revlog-id",
        type=int,
        action="append",
        default=[],
        help="Explicit revlog id to check. Can be passed more than once.",
    )
    parser.add_argument(
        "--card-id",
        type=int,
        action="append",
        default=[],
        help="Card id whose eligible review ids should be checked.",
    )
    parser.add_argument(
        "--day-range",
        type=int,
        default=0,
        help="SSE-style day range filter. 0 means all eligible reviews.",
    )
    parser.add_argument(
        "--rollover-hour",
        type=int,
        default=4,
        help="Collection rollover hour used for --day-range filtering.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit target ids for faster diagnosis. 0 means no limit.",
    )
    return parser


def _analysis_db_path(
    args: argparse.Namespace,
) -> tuple[Path, Path | None, Path | None]:
    if args.collection_copy is not None:
        path = args.collection_copy.expanduser().resolve()
        if not path.exists():
            raise ProbeError(f"collection copy does not exist: {path}")
        return path, None, None

    profile_folder = args.profile_folder.expanduser().resolve()
    live_db = profile_folder / "collection.anki2"
    if not live_db.exists():
        raise ProbeError(f"profile collection does not exist: {live_db}")
    if not args.allow_open_profile and _has_open_handles(live_db):
        raise ProbeError(
            "profile appears to be open; quit Anki or pass --allow-open-profile "
            "for a best-effort copy"
        )

    copy_dir = Path(tempfile.mkdtemp(prefix="rwkv-sse-probe-"))
    copied_db = copy_dir / "collection.anki2"
    for source in (
        live_db,
        live_db.with_name("collection.anki2-wal"),
        live_db.with_name("collection.anki2-shm"),
    ):
        if source.exists():
            shutil.copy2(source, copy_dir / source.name)
    if not copied_db.exists():
        raise ProbeError(f"failed to copy collection: {live_db}")
    return copied_db, live_db, copy_dir


def _has_open_handles(path: Path) -> bool:
    result = subprocess.run(
        ["lsof", str(path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _probe(
    db_path: Path,
    *,
    copied_from: Path | None,
    revlog_ids: Sequence[int],
    card_ids: Sequence[int],
    day_range: int,
    rollover_hour: int,
    limit: int,
) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        revlog_columns = _table_columns(connection, "revlog")
        cache_columns = _table_columns(connection, RWKV_CACHE_TABLE)
        target_ids = _target_review_ids(
            connection,
            revlog_ids=revlog_ids,
            card_ids=card_ids,
            day_range=day_range,
            rollover_hour=rollover_hour,
            limit=limit,
        )
        cache_predictions = _cache_predictions(connection, target_ids)
        legacy_column = next(
            (column for column in RWKV_LEGACY_COLUMNS if column in revlog_columns),
            None,
        )
        legacy_predictions = (
            _legacy_predictions(
                connection,
                target_ids=[
                    review_id
                    for review_id in target_ids
                    if review_id not in cache_predictions
                ],
                column=legacy_column,
            )
            if legacy_column is not None
            else {}
        )
        predictions = {**cache_predictions, **legacy_predictions}
        missing_ids = [
            review_id for review_id in target_ids if review_id not in predictions
        ]
        source_parts = []
        if cache_predictions:
            source_parts.append(RWKV_CACHE_TABLE)
        if legacy_predictions and legacy_column is not None:
            source_parts.append(legacy_column)

        return {
            "collection": str(db_path),
            "copied_from": str(copied_from) if copied_from is not None else None,
            "target": {
                "count": len(target_ids),
                "sample": target_ids[:20],
            },
            "cache_table": {
                "exists": bool(cache_columns),
                "columns": sorted(cache_columns),
                "valid_count": len(cache_predictions),
            },
            "legacy_column": legacy_column,
            "legacy_valid_count": len(legacy_predictions),
            "missing": {
                "count": len(missing_ids),
                "sample": missing_ids[:50],
            },
            "sse": {
                "would_show_no_data": bool(missing_ids),
                "column": "+".join(source_parts)
                if source_parts and not missing_ids
                else None,
                "data_count": 0 if missing_ids else len(predictions),
            },
        }
    finally:
        connection.close()


def _target_review_ids(
    connection: sqlite3.Connection,
    *,
    revlog_ids: Sequence[int],
    card_ids: Sequence[int],
    day_range: int,
    rollover_hour: int,
    limit: int,
) -> list[int]:
    explicit_ids = _positive_unique_ints(revlog_ids)
    if explicit_ids:
        return explicit_ids[:limit] if limit > 0 else explicit_ids

    conditions = ["r.ease BETWEEN 1 AND 4", "r.type IN (0, 1, 2, 3)"]
    params: list[int | float] = []
    positive_card_ids = _positive_unique_ints(card_ids)
    if positive_card_ids:
        conditions.append(f"r.cid IN ({_placeholders(positive_card_ids)})")
        params.extend(positive_card_ids)
    if day_range > 0:
        conditions.append("r.id > ?")
        params.append(_revlog_lower_limit(day_range, rollover_hour))

    sql = f"""
    SELECT r.id
    FROM revlog r
    JOIN cards c ON c.id = r.cid
    WHERE {" AND ".join(conditions)}
    ORDER BY r.id
    """
    if limit > 0:
        sql += "\nLIMIT ?"
        params.append(limit)
    return [row[0] for row in connection.execute(sql, params)]


def _cache_predictions(
    connection: sqlite3.Connection,
    target_ids: Sequence[int],
) -> dict[int, float]:
    if not target_ids or not _table_columns(connection, RWKV_CACHE_TABLE):
        return {}

    predictions: dict[int, float] = {}
    for chunk in _chunks(target_ids, SQLITE_CHUNK_SIZE):
        rows = connection.execute(
            f"""
            SELECT revlog_id, prediction
            FROM {RWKV_CACHE_TABLE}
            WHERE revlog_id IN ({_placeholders(chunk)})
            ORDER BY revlog_id
            """,
            chunk,
        )
        for review_id, prediction in rows:
            if _valid_probability(prediction):
                predictions[int(review_id)] = float(prediction)
    return predictions


def _legacy_predictions(
    connection: sqlite3.Connection,
    *,
    target_ids: Sequence[int],
    column: str,
) -> dict[int, float]:
    predictions: dict[int, float] = {}
    for chunk in _chunks(target_ids, SQLITE_CHUNK_SIZE):
        rows = connection.execute(
            f"""
            SELECT id, {column}
            FROM revlog
            WHERE id IN ({_placeholders(chunk)}) AND {column} IS NOT NULL
            ORDER BY id
            """,
            chunk,
        )
        for review_id, prediction in rows:
            if _valid_probability(prediction):
                predictions[int(review_id)] = float(prediction)
    return predictions


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _revlog_lower_limit(day_range: int, rollover_hour: int) -> float:
    today = (int(time.time()) - (rollover_hour * 60 * 60)) / DAY_SECONDS
    return (today - day_range) * DAY_SECONDS * 1000


def _positive_unique_ints(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value > 0 and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _valid_probability(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 <= value <= 1
    )


def _placeholders(values: Sequence[object]) -> str:
    return ",".join("?" for _ in values)


def _chunks(values: Sequence[int], chunk_size: int) -> Iterable[Sequence[int]]:
    for start in range(0, len(values), chunk_size):
        yield values[start : start + chunk_size]


class ProbeError(Exception):
    pass


if __name__ == "__main__":
    sys.exit(main())
