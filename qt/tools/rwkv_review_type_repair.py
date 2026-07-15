#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RWKV_CACHE_TABLE = "search_stats_rwkv_review_retrievability"


class RepairError(Exception):
    pass


@dataclass(frozen=True)
class ReviewRow:
    review_id: int
    card_id: int
    ease: int
    review_kind: int
    factor: int


@dataclass(frozen=True)
class ReviewTypeRepair:
    review_id: int
    card_id: int
    previous_review_id: int
    previous_kind: int
    previous_ease: int
    old_kind: int
    new_kind: int
    scheduler_day: str
    rule: str


@dataclass(frozen=True)
class ReviewTypeAmbiguity:
    review_id: int
    card_id: int
    previous_review_id: int
    previous_kind: int
    previous_ease: int
    current_kind: int
    scheduler_day: str
    reason: str


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit and optionally repair RWKV synthetic review kinds in a copied "
            "Anki collection. Dry-run is the default."
        )
    )
    parser.add_argument(
        "--collection-copy",
        type=Path,
        required=True,
        help="Path to a copied collection database. Never pass a live profile DB.",
    )
    parser.add_argument(
        "--timezone",
        required=True,
        help="IANA timezone used when the reviews were recorded, such as Europe/Brussels.",
    )
    parser.add_argument(
        "--rollover-hour",
        type=int,
        default=4,
        choices=range(24),
        metavar="0-23",
        help="Anki scheduler rollover hour (default: 4).",
    )
    parser.add_argument(
        "--apply-to-copy",
        action="store_true",
        help="Back up and modify the supplied copy in one transaction.",
    )
    parser.add_argument(
        "--audit-output",
        type=Path,
        help="Audit JSON path. Applying defaults to a sibling .audit.json file.",
    )
    args = parser.parse_args(argv)

    try:
        timezone = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError as err:
        raise SystemExit(f"unknown timezone: {args.timezone}") from err

    try:
        report = repair_collection_copy(
            args.collection_copy,
            timezone=timezone,
            rollover_hour=args.rollover_hour,
            apply=args.apply_to_copy,
            audit_output=args.audit_output,
        )
    except RepairError as err:
        print(json.dumps({"error": str(err)}, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def repair_collection_copy(
    collection_copy: Path,
    *,
    timezone: ZoneInfo,
    rollover_hour: int,
    apply: bool,
    audit_output: Path | None = None,
) -> dict[str, object]:
    collection_copy = collection_copy.expanduser().resolve()
    if not collection_copy.is_file():
        raise RepairError(f"collection copy does not exist: {collection_copy}")

    rows = _read_review_rows(collection_copy)
    repairs, ambiguities = infer_review_type_audit(
        rows,
        timezone=timezone,
        rollover_hour=rollover_hour,
    )
    rule_counts = Counter(repair.rule for repair in repairs)
    backup_path: Path | None = None
    invalidated_cache_rows = 0

    if apply:
        backup_path = _backup_path(collection_copy)
        shutil.copy2(collection_copy, backup_path)
        invalidated_cache_rows = _apply_repairs(collection_copy, repairs)
        if audit_output is None:
            audit_output = collection_copy.with_suffix(
                collection_copy.suffix + ".rwkv-review-type-audit.json"
            )

    report: dict[str, object] = {
        "collectionCopy": str(collection_copy),
        "mode": "applied-to-copy" if apply else "dry-run",
        "timezone": str(timezone),
        "rolloverHour": rollover_hour,
        "reviewRows": len(rows),
        "repairs": len(repairs),
        "ambiguities": len(ambiguities),
        "rules": dict(sorted(rule_counts.items())),
        "changes": [asdict(repair) for repair in repairs],
        "ambiguousRows": [asdict(ambiguity) for ambiguity in ambiguities],
        "backup": str(backup_path) if backup_path is not None else None,
        "invalidatedRwkvRetrievabilityRows": invalidated_cache_rows,
        "profileStateCacheAction": (
            "Delete the profile-local rwkv-state-cache directory before using "
            "this repaired copy."
            if apply
            else None
        ),
    }
    if audit_output is not None:
        audit_output = audit_output.expanduser().resolve()
        audit_output.parent.mkdir(parents=True, exist_ok=True)
        audit_output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf8",
        )
        report["auditOutput"] = str(audit_output)

    return report


def infer_review_type_repairs(
    rows: Sequence[ReviewRow],
    *,
    timezone: ZoneInfo,
    rollover_hour: int,
) -> list[ReviewTypeRepair]:
    repairs, _ambiguities = infer_review_type_audit(
        rows,
        timezone=timezone,
        rollover_hour=rollover_hour,
    )
    return repairs


def infer_review_type_audit(
    rows: Sequence[ReviewRow],
    *,
    timezone: ZoneInfo,
    rollover_hour: int,
) -> tuple[list[ReviewTypeRepair], list[ReviewTypeAmbiguity]]:
    previous_by_card: dict[int, tuple[ReviewRow, bool]] = {}
    repairs: list[ReviewTypeRepair] = []
    ambiguities: list[ReviewTypeAmbiguity] = []

    for row in rows:
        if row.review_kind == 4 and row.factor == 0 and row.ease == 0:
            previous_by_card.pop(row.card_id, None)
            continue
        if not 1 <= row.ease <= 4:
            continue
        if row.review_kind == 3 and row.factor == 0:
            continue

        previous_entry = previous_by_card.get(row.card_id)
        effective_row = row
        effective_row_is_synthetic = False
        if previous_entry is not None and _scheduler_day(
            (previous := previous_entry[0]).review_id, timezone, rollover_hour
        ) == _scheduler_day(row.review_id, timezone, rollover_hour):
            previous_is_synthetic = previous_entry[1]
            target = _same_day_target_kind(
                previous,
                row,
                previous_is_synthetic=previous_is_synthetic,
            )
            if (
                target is None
                and row.review_kind == 1
                and previous.review_kind == 3
                and not previous_is_synthetic
            ):
                ambiguities.append(
                    ReviewTypeAmbiguity(
                        review_id=row.review_id,
                        card_id=row.card_id,
                        previous_review_id=previous.review_id,
                        previous_kind=previous.review_kind,
                        previous_ease=previous.ease,
                        current_kind=row.review_kind,
                        scheduler_day=_scheduler_day(
                            row.review_id, timezone, rollover_hour
                        ).isoformat(),
                        reason="filtered_original_state_not_reconstructable",
                    )
                )
            if target is not None and target != row.review_kind:
                rule = _repair_rule(previous, target)
                repairs.append(
                    ReviewTypeRepair(
                        review_id=row.review_id,
                        card_id=row.card_id,
                        previous_review_id=previous.review_id,
                        previous_kind=previous.review_kind,
                        previous_ease=previous.ease,
                        old_kind=row.review_kind,
                        new_kind=target,
                        scheduler_day=_scheduler_day(
                            row.review_id, timezone, rollover_hour
                        ).isoformat(),
                        rule=rule,
                    )
                )
                effective_row = ReviewRow(
                    review_id=row.review_id,
                    card_id=row.card_id,
                    ease=row.ease,
                    review_kind=target,
                    factor=row.factor,
                )
                effective_row_is_synthetic = True
        previous_by_card[row.card_id] = (effective_row, effective_row_is_synthetic)

    return repairs, ambiguities


def _same_day_target_kind(
    previous: ReviewRow,
    current: ReviewRow,
    *,
    previous_is_synthetic: bool,
) -> int | None:
    if current.review_kind != 1:
        return None
    if previous.review_kind == 1:
        return 2 if previous.ease == 1 else 3
    if previous.review_kind == 2:
        return 2 if previous.ease in (1, 2) else 3
    if previous.review_kind == 0:
        return 3
    if previous.review_kind == 3 and previous_is_synthetic:
        return 3
    return None


def _repair_rule(previous: ReviewRow, target_kind: int) -> str:
    previous_name = {
        0: "learning",
        1: "review",
        2: "relearning",
        3: "filtered",
    }.get(previous.review_kind, "other")
    target_name = "relearning" if target_kind == 2 else "filtered"
    return f"same_day_after_{previous_name}_{previous.ease}_to_{target_name}"


def _scheduler_day(
    review_id: int,
    timezone: ZoneInfo,
    rollover_hour: int,
) -> date:
    local = datetime.fromtimestamp(review_id / 1000, timezone)
    return (local - timedelta(hours=rollover_hour)).date()


def _read_review_rows(path: Path) -> list[ReviewRow]:
    uri = f"file:{quote(str(path))}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as db:
            rows = db.execute(
                """
select id, cid, ease, type, factor
from revlog
order by cid, id
"""
            ).fetchall()
    except sqlite3.Error as err:
        raise RepairError(f"failed to read copied collection: {err}") from err
    return [ReviewRow(*map(int, row)) for row in rows]


def _apply_repairs(path: Path, repairs: Sequence[ReviewTypeRepair]) -> int:
    try:
        with sqlite3.connect(path) as db:
            db.execute("begin immediate")
            changes_before = db.total_changes
            db.executemany(
                "update revlog set type = ? where id = ? and type = ?",
                [
                    (repair.new_kind, repair.review_id, repair.old_kind)
                    for repair in repairs
                ],
            )
            if db.total_changes - changes_before != len(repairs):
                raise RepairError(
                    "a revlog row changed during repair; transaction aborted"
                )
            invalidated = 0
            if db.execute(
                "select 1 from sqlite_master where type = 'table' and name = ?",
                (RWKV_CACHE_TABLE,),
            ).fetchone():
                invalidated = int(
                    db.execute(f"select count() from {RWKV_CACHE_TABLE}").fetchone()[0]
                )
                db.execute(f"delete from {RWKV_CACHE_TABLE}")
            db.commit()
            return invalidated
    except RepairError:
        raise
    except sqlite3.Error as err:
        raise RepairError(f"failed to apply repair transaction: {err}") from err


def _backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.before-rwkv-type-repair-{stamp}.bak")
    index = 1
    while candidate.exists():
        candidate = path.with_name(
            f"{path.name}.before-rwkv-type-repair-{stamp}-{index}.bak"
        )
        index += 1
    return candidate


if __name__ == "__main__":
    raise SystemExit(main())
