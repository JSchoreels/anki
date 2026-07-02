#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    from tools import rwkv_temporal_calibration_probe as temporal
except ModuleNotFoundError:
    import rwkv_temporal_calibration_probe as temporal  # type: ignore[import-not-found, no-redef]

RWKV_CACHE_TABLE = temporal.RWKV_CACHE_TABLE
FIELD_SEPARATOR = "\x1f"
DEFAULT_FRONT_FIELD = "Front"
DEFAULT_READING_FIELD = "Reading"
DEFAULT_FREQUENCY_FIELD = "Frequency"
DEFAULT_STRATEGIES = (
    "kanji_list",
    "kanji_count",
    "frequency",
    "kanji_reading_pair",
)
FREQUENCY_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

ProbeError = temporal.ProbeError


@dataclass(frozen=True)
class ContentReviewSample:
    review_id: int
    card_id: int
    note_id: int
    review_start_seconds: float
    outcome: int
    prediction: float
    recall_bin: tuple[int, int, int]
    front: str
    reading: str
    frequency_text: str
    frequency_rank: float | None
    kanji: tuple[str, ...]
    kanji_count: int


@dataclass(frozen=True)
class StrategySpec:
    name: str
    description: str
    key: Callable[[ContentReviewSample], str]
    parameters: Mapping[str, Any]


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    copy_dir: Path | None = None
    try:
        db_path, copied_from, copy_dir = _analysis_db_path(args)
        report = content_calibration_report(
            db_path,
            copied_from=copied_from,
            deck_match=args.deck_match,
            front_field=args.front_field,
            reading_field=args.reading_field,
            frequency_field=args.frequency_field,
            train_fraction=args.train_fraction,
            answer_time_cap_seconds=args.answer_time_cap_seconds,
            limit=args.limit,
            min_samples=args.min_samples,
            min_group_reviews=args.min_group_reviews,
            group_l2=args.group_l2,
            max_iterations=args.max_iterations,
            frequency_bins=args.frequency_bins,
            strategy_names=args.strategy,
        )
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
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
            "Compare content-conditioned calibration strategies over cached "
            "RWKV pre-answer predictions."
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
        "--deck-match",
        help=(
            "Restrict samples to cards currently in decks whose human-readable "
            "name matches this text, eg Yomitan."
        ),
    )
    parser.add_argument(
        "--front-field",
        default=DEFAULT_FRONT_FIELD,
        help="Field containing the Japanese word. Defaults to Front.",
    )
    parser.add_argument(
        "--reading-field",
        default=DEFAULT_READING_FIELD,
        help="Field containing the reading. Defaults to Reading.",
    )
    parser.add_argument(
        "--frequency-field",
        default=DEFAULT_FREQUENCY_FIELD,
        help="Field containing the frequency rank. Defaults to Frequency.",
    )
    parser.add_argument(
        "--strategy",
        action="append",
        choices=DEFAULT_STRATEGIES,
        help="Strategy to compare. Can be passed more than once; defaults to all.",
    )
    parser.add_argument(
        "--train-fraction",
        type=float,
        default=0.70,
        help="Chronological fraction used for fitting. Defaults to 0.70.",
    )
    parser.add_argument(
        "--answer-time-cap-seconds",
        type=float,
        default=300.0,
        help=(
            "Maximum revlog.time subtracted from revlog.id when estimating "
            "review start. Defaults to 300 seconds."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit eligible cached reviews for faster diagnosis. 0 means no limit.",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=200,
        help="Minimum cached reviews with required fields. Defaults to 200.",
    )
    parser.add_argument(
        "--min-group-reviews",
        type=int,
        default=3,
        help=(
            "Minimum training reviews needed before a content group gets its "
            "own calibration offset. Defaults to 3."
        ),
    )
    parser.add_argument(
        "--group-l2",
        type=float,
        default=2.0,
        help="L2 penalty for each group logit offset. Defaults to 2.0.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=25,
        help="Maximum Newton iterations for each group offset. Defaults to 25.",
    )
    parser.add_argument(
        "--frequency-bins",
        type=int,
        default=10,
        help="Quantile bins for numeric Frequency values. Defaults to 10.",
    )
    return parser


def content_calibration_report(  # noqa: PLR0913
    db_path: Path,
    *,
    copied_from: Path | None,
    deck_match: str | None,
    front_field: str,
    reading_field: str,
    frequency_field: str,
    train_fraction: float,
    answer_time_cap_seconds: float,
    limit: int,
    min_samples: int,
    min_group_reviews: int,
    group_l2: float,
    max_iterations: int,
    frequency_bins: int,
    strategy_names: Sequence[str] | None,
) -> dict[str, Any]:
    _validate_options(
        train_fraction=train_fraction,
        min_samples=min_samples,
        min_group_reviews=min_group_reviews,
        group_l2=group_l2,
        max_iterations=max_iterations,
        frequency_bins=frequency_bins,
    )
    requested_fields = (front_field, reading_field, frequency_field)
    samples, raw_count, skipped_missing_fields = _load_content_samples(
        db_path,
        deck_match=deck_match,
        requested_fields=requested_fields,
        answer_time_cap_seconds=answer_time_cap_seconds,
        limit=limit,
    )
    if len(samples) < min_samples:
        raise ProbeError(
            f"only {len(samples)} cached RWKV review predictions with required "
            f"fields available; need at least {min_samples}"
        )

    train, test = _split_train_test(samples, train_fraction=train_fraction)
    selected_strategy_names = tuple(strategy_names or DEFAULT_STRATEGIES)
    specs = _strategy_specs(
        train,
        selected_strategy_names,
        frequency_bins=frequency_bins,
    )
    baseline = {
        "train": _metrics_for_samples(train),
        "test": _metrics_for_samples(test),
    }
    strategies: dict[str, Any] = {
        "rwkv_baseline": {
            "description": "Raw cached RWKV pre-answer prediction.",
            "train": baseline["train"],
            "test": baseline["test"],
        }
    }

    for spec in specs:
        offsets, total_groups, trained_groups = _fit_group_offsets(
            train,
            key=spec.key,
            min_group_reviews=min_group_reviews,
            l2=group_l2,
            max_iterations=max_iterations,
        )
        train_metrics = _metrics_for_predictions(
            train,
            _calibrated_predictions(train, key=spec.key, offsets=offsets),
        )
        test_metrics = _metrics_for_predictions(
            test,
            _calibrated_predictions(test, key=spec.key, offsets=offsets),
        )
        fallback_test_reviews = sum(
            1 for sample in test if spec.key(sample) not in offsets
        )
        strategies[spec.name] = {
            "description": spec.description,
            "parameters": {
                **dict(spec.parameters),
                "group_l2": group_l2,
                "min_group_reviews": min_group_reviews,
            },
            "groups": {
                "training_total": total_groups,
                "trained": trained_groups,
                "fallback_test_reviews": fallback_test_reviews,
            },
            "train": train_metrics,
            "test": test_metrics,
            "test_delta_vs_baseline": _metric_delta(
                test_metrics,
                baseline["test"],
            ),
        }

    comparable = {
        name: result
        for name, result in strategies.items()
        if result["test"]["count"] > 0
    }
    return {
        "collection": str(db_path),
        "copied_from": str(copied_from) if copied_from is not None else None,
        "input": {
            "eligible_cached_reviews": raw_count,
            "used_reviews": len(samples),
            "deck_match": deck_match,
            "fields": {
                "front": front_field,
                "reading": reading_field,
                "frequency": frequency_field,
            },
            "skipped_missing_fields": skipped_missing_fields,
            "frequency_parseable_reviews": sum(
                1 for sample in samples if sample.frequency_rank is not None
            ),
            "answer_time_cap_seconds": answer_time_cap_seconds,
        },
        "split": {
            "train": _split_summary(train),
            "test": _split_summary(test),
        },
        "strategies": strategies,
        "best_by_test_log_loss": min(
            comparable,
            key=lambda name: comparable[name]["test"]["log_loss"],
        ),
        "best_by_test_rmse_bins": min(
            comparable,
            key=lambda name: comparable[name]["test"]["rmse_bins"],
        ),
    }


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

    copy_dir = Path(tempfile.mkdtemp(prefix="rwkv-content-probe-"))
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


def _load_content_samples(
    db_path: Path,
    *,
    deck_match: str | None,
    requested_fields: Sequence[str],
    answer_time_cap_seconds: float,
    limit: int,
) -> tuple[list[ContentReviewSample], int, dict[str, int]]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not temporal._table_columns(connection, RWKV_CACHE_TABLE):
            raise ProbeError(f"missing RWKV cache table: {RWKV_CACHE_TABLE}")
        deck_ids = temporal._matched_deck_ids(connection, deck_match)
        field_ords = _field_ord_maps(connection, requested_fields)
        rows = _cached_content_rows(connection, deck_ids=deck_ids, limit=limit)
    finally:
        connection.close()

    samples, skipped_missing_fields = _content_samples_from_rows(
        rows,
        field_ords=field_ords,
        requested_fields=requested_fields,
        answer_time_cap_seconds=answer_time_cap_seconds,
    )
    return samples, len(rows), skipped_missing_fields


def _cached_content_rows(
    connection: sqlite3.Connection,
    *,
    deck_ids: Sequence[int] | None,
    limit: int,
) -> list[tuple[int, int, int, int, int, int, float, str]]:
    deck_clause = ""
    params: list[int] = []
    if deck_ids is not None:
        if not deck_ids:
            return []
        deck_clause = f"AND c.did IN ({temporal._placeholders(deck_ids)})"
        params.extend(deck_ids)

    sql = f"""
    SELECT r.id, r.cid, c.nid, n.mid, r.ease, r.time, cache.prediction, n.flds
    FROM revlog r
    JOIN cards c ON c.id = r.cid
    JOIN notes n ON n.id = c.nid
    JOIN {RWKV_CACHE_TABLE} cache ON cache.revlog_id = r.id
    WHERE r.ease BETWEEN 1 AND 4
      AND (r.type IN (0, 1, 2, 3) OR r.type = 4)
      AND cache.prediction >= 0
      AND cache.prediction <= 1
      {deck_clause}
    ORDER BY r.id
    """
    if limit > 0:
        sql += "\nLIMIT ?"
        params.append(limit)

    return [
        (
            int(review_id),
            int(card_id),
            int(note_id),
            int(notetype_id),
            int(ease),
            int(duration_millis),
            float(prediction),
            str(note_fields),
        )
        for (
            review_id,
            card_id,
            note_id,
            notetype_id,
            ease,
            duration_millis,
            prediction,
            note_fields,
        ) in connection.execute(sql, params)
    ]


def _field_ord_maps(
    connection: sqlite3.Connection,
    requested_fields: Sequence[str],
) -> dict[int, dict[str, int]]:
    if temporal._table_columns(connection, "fields"):
        return _schema15_field_ord_maps(connection, requested_fields)
    return _schema11_field_ord_maps(connection, requested_fields)


def _schema15_field_ord_maps(
    connection: sqlite3.Connection,
    requested_fields: Sequence[str],
) -> dict[int, dict[str, int]]:
    requested_by_key = {name.casefold(): name for name in requested_fields}
    field_ords: dict[int, dict[str, int]] = {}
    for notetype_id, ord_, field_name in connection.execute(
        "SELECT ntid, ord, name FROM fields ORDER BY ntid, ord"
    ):
        requested = requested_by_key.get(str(field_name).casefold())
        if requested is not None:
            field_ords.setdefault(int(notetype_id), {})[requested] = int(ord_)
    return field_ords


def _schema11_field_ord_maps(
    connection: sqlite3.Connection,
    requested_fields: Sequence[str],
) -> dict[int, dict[str, int]]:
    row = connection.execute("SELECT models FROM col LIMIT 1").fetchone()
    if row is None:
        return {}

    requested_by_key = {name.casefold(): name for name in requested_fields}
    field_ords: dict[int, dict[str, int]] = {}
    for notetype_id, notetype in json.loads(str(row[0])).items():
        fields = notetype.get("flds", [])
        if not isinstance(fields, list):
            continue
        for default_ord, field in enumerate(fields):
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name", ""))
            requested = requested_by_key.get(field_name.casefold())
            if requested is not None:
                ord_ = int(field.get("ord", default_ord))
                field_ords.setdefault(int(notetype_id), {})[requested] = ord_
    return field_ords


def _content_samples_from_rows(
    rows: Sequence[tuple[int, int, int, int, int, int, float, str]],
    *,
    field_ords: Mapping[int, Mapping[str, int]],
    requested_fields: Sequence[str],
    answer_time_cap_seconds: float,
) -> tuple[list[ContentReviewSample], dict[str, int]]:
    samples: list[ContentReviewSample] = []
    skipped_missing_fields = {field_name: 0 for field_name in requested_fields}
    previous_review_id_by_card: dict[int, int] = {}
    long_term_review_counts_by_card: dict[int, int] = {}
    prior_lapse_counts_by_card: dict[int, int] = {}

    for (
        review_id,
        card_id,
        note_id,
        notetype_id,
        ease,
        duration_millis,
        prediction,
        note_fields,
    ) in rows:
        extracted = _extract_requested_fields(
            note_fields,
            field_ords=field_ords.get(notetype_id, {}),
            requested_fields=requested_fields,
        )
        if extracted is None:
            for field_name in requested_fields:
                if field_name not in field_ords.get(notetype_id, {}):
                    skipped_missing_fields[field_name] += 1
            continue

        previous_review_id = previous_review_id_by_card.get(card_id)
        elapsed_seconds = (
            max(0, (review_id - previous_review_id) // 1000)
            if previous_review_id is not None
            else -1
        )
        elapsed_days = (
            elapsed_seconds // temporal.DAY_SECONDS if elapsed_seconds >= 0 else -1
        )
        is_long_term_review = elapsed_days >= 1
        prior_long_term_reviews = long_term_review_counts_by_card.get(card_id, 0)
        prior_lapses = prior_lapse_counts_by_card.get(card_id, 0)
        long_term_reviews = prior_long_term_reviews + int(is_long_term_review)
        outcome = 0 if ease == 1 else 1
        answer_seconds = review_id / 1000
        review_start_seconds = answer_seconds - temporal._capped_answer_seconds(
            duration_millis,
            answer_time_cap_seconds,
        )

        front = _plain_text(extracted[requested_fields[0]])
        reading = _plain_text(extracted[requested_fields[1]])
        frequency_text = _plain_text(extracted[requested_fields[2]])
        samples.append(
            ContentReviewSample(
                review_id=review_id,
                card_id=card_id,
                note_id=note_id,
                review_start_seconds=review_start_seconds,
                outcome=outcome,
                prediction=temporal._clamp_probability(prediction),
                recall_bin=(
                    temporal._fsrs_delta_t_bin(elapsed_days),
                    temporal._fsrs_count_bin(long_term_reviews + 1.0, 1.99, 1.89),
                    0
                    if prior_lapses == 0
                    else temporal._fsrs_count_bin(prior_lapses, 1.65, 1.73),
                ),
                front=front,
                reading=reading,
                frequency_text=frequency_text,
                frequency_rank=_parse_frequency_rank(frequency_text),
                kanji=_unique_kanji(front),
                kanji_count=_kanji_count(front),
            )
        )

        previous_review_id_by_card[card_id] = review_id
        if is_long_term_review:
            long_term_review_counts_by_card[card_id] = long_term_reviews
            if ease == 1:
                prior_lapse_counts_by_card[card_id] = prior_lapses + 1

    return samples, skipped_missing_fields


def _extract_requested_fields(
    note_fields: str,
    *,
    field_ords: Mapping[str, int],
    requested_fields: Sequence[str],
) -> dict[str, str] | None:
    if any(field_name not in field_ords for field_name in requested_fields):
        return None

    split_fields = note_fields.split(FIELD_SEPARATOR)
    return {
        field_name: split_fields[ord_] if ord_ < len(split_fields) else ""
        for field_name, ord_ in field_ords.items()
        if field_name in requested_fields
    }


def _strategy_specs(
    train: Sequence[ContentReviewSample],
    selected_names: Sequence[str],
    *,
    frequency_bins: int,
) -> list[StrategySpec]:
    unknown = sorted(set(selected_names) - set(DEFAULT_STRATEGIES))
    if unknown:
        raise ProbeError(f"unknown strategies: {', '.join(unknown)}")

    frequency_edges = _frequency_edges(train, frequency_bins=frequency_bins)
    specs: dict[str, StrategySpec] = {
        "kanji_list": StrategySpec(
            name="kanji_list",
            description="Group by the sorted unique Kanji characters in Front.",
            key=_kanji_list_key,
            parameters={},
        ),
        "kanji_count": StrategySpec(
            name="kanji_count",
            description="Group by the number of Kanji characters in Front.",
            key=lambda sample: str(sample.kanji_count),
            parameters={},
        ),
        "frequency": StrategySpec(
            name="frequency",
            description="Group by training-set quantile bins of the Frequency field.",
            key=lambda sample: _frequency_key(sample, frequency_edges),
            parameters={
                "frequency_bins": frequency_bins,
                "frequency_edges": frequency_edges,
            },
        ),
        "kanji_reading_pair": StrategySpec(
            name="kanji_reading_pair",
            description="Group by Front plus Reading.",
            key=lambda sample: f"{sample.front}\t{sample.reading}",
            parameters={},
        ),
    }
    return [specs[name] for name in selected_names]


def _fit_group_offsets(
    samples: Sequence[ContentReviewSample],
    *,
    key: Callable[[ContentReviewSample], str],
    min_group_reviews: int,
    l2: float,
    max_iterations: int,
) -> tuple[dict[str, float], int, int]:
    grouped: dict[str, list[ContentReviewSample]] = {}
    for sample in samples:
        grouped.setdefault(key(sample), []).append(sample)

    offsets: dict[str, float] = {}
    for group_key, group_samples in grouped.items():
        if len(group_samples) >= min_group_reviews:
            offsets[group_key] = _fit_group_offset(
                group_samples,
                l2=l2,
                max_iterations=max_iterations,
            )
    return offsets, len(grouped), len(offsets)


def _fit_group_offset(
    samples: Sequence[ContentReviewSample],
    *,
    l2: float,
    max_iterations: int,
) -> float:
    offset = 0.0
    for _iteration in range(max_iterations):
        gradient = l2 * offset
        hessian = l2
        for sample in samples:
            prediction = temporal._sigmoid(temporal._logit(sample.prediction) + offset)
            gradient += prediction - sample.outcome
            hessian += prediction * (1 - prediction)
        if hessian <= 0:
            break

        step = gradient / hessian
        offset -= step
        if abs(step) < 1e-8:
            break
    return offset


def _calibrated_predictions(
    samples: Sequence[ContentReviewSample],
    *,
    key: Callable[[ContentReviewSample], str],
    offsets: Mapping[str, float],
) -> list[float]:
    return [
        temporal._sigmoid(
            temporal._logit(sample.prediction) + offsets.get(key(sample), 0.0)
        )
        for sample in samples
    ]


def _metrics_for_samples(
    samples: Sequence[ContentReviewSample],
) -> dict[str, float | int]:
    return temporal._metrics(
        (sample.prediction, sample.outcome, sample.recall_bin) for sample in samples
    )


def _metrics_for_predictions(
    samples: Sequence[ContentReviewSample],
    predictions: Sequence[float],
) -> dict[str, float | int]:
    return temporal._metrics(
        (prediction, sample.outcome, sample.recall_bin)
        for prediction, sample in zip(predictions, samples, strict=True)
    )


def _metric_delta(
    metrics: Mapping[str, float | int],
    baseline: Mapping[str, float | int],
) -> dict[str, float]:
    return {
        "log_loss": float(metrics["log_loss"]) - float(baseline["log_loss"]),
        "rmse_bins": float(metrics["rmse_bins"]) - float(baseline["rmse_bins"]),
    }


def _split_train_test(
    samples: Sequence[ContentReviewSample],
    *,
    train_fraction: float,
) -> tuple[list[ContentReviewSample], list[ContentReviewSample]]:
    train_end = max(1, int(len(samples) * train_fraction))
    train_end = min(train_end, len(samples) - 1)
    return list(samples[:train_end]), list(samples[train_end:])


def _split_summary(samples: Sequence[ContentReviewSample]) -> dict[str, Any]:
    if not samples:
        return {"count": 0}
    return {
        "count": len(samples),
        "first_review_id": samples[0].review_id,
        "last_review_id": samples[-1].review_id,
        "first_review_start": datetime.fromtimestamp(
            samples[0].review_start_seconds
        ).isoformat(),
        "last_review_start": datetime.fromtimestamp(
            samples[-1].review_start_seconds
        ).isoformat(),
    }


def _frequency_edges(
    samples: Sequence[ContentReviewSample],
    *,
    frequency_bins: int,
) -> list[float]:
    ranks = sorted(
        sample.frequency_rank for sample in samples if sample.frequency_rank is not None
    )
    if not ranks:
        return []

    edges: list[float] = []
    for index in range(1, frequency_bins):
        rank_index = math.ceil(len(ranks) * index / frequency_bins) - 1
        edge = ranks[max(0, min(rank_index, len(ranks) - 1))]
        if not edges or edge > edges[-1]:
            edges.append(edge)
    return edges


def _frequency_key(sample: ContentReviewSample, edges: Sequence[float]) -> str:
    if sample.frequency_rank is None:
        return "missing"
    return f"bin:{bisect.bisect_left(edges, sample.frequency_rank)}"


def _kanji_list_key(sample: ContentReviewSample) -> str:
    return "".join(sample.kanji) or "none"


def _unique_kanji(text: str) -> tuple[str, ...]:
    return tuple(sorted({char for char in text if _is_kanji(char)}))


def _kanji_count(text: str) -> int:
    return sum(1 for char in text if _is_kanji(char))


def _is_kanji(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0x2CEB0 <= codepoint <= 0x2EBEF
        or 0x30000 <= codepoint <= 0x3134F
        or 0x31350 <= codepoint <= 0x323AF
    )


def _parse_frequency_rank(text: str) -> float | None:
    match = FREQUENCY_RE.search(text)
    if match is None:
        return None
    value = float(match.group(0).replace(",", ""))
    return value if math.isfinite(value) else None


def _plain_text(value: str) -> str:
    parser = _PlainTextParser()
    parser.feed(value)
    parser.close()
    return " ".join(unescape("".join(parser.parts)).split())


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _validate_options(
    *,
    train_fraction: float,
    min_samples: int,
    min_group_reviews: int,
    group_l2: float,
    max_iterations: int,
    frequency_bins: int,
) -> None:
    if not 0 < train_fraction < 1:
        raise ProbeError("--train-fraction must be between 0 and 1")
    if min_samples < 2:
        raise ProbeError("--min-samples must be at least 2")
    if min_group_reviews < 1:
        raise ProbeError("--min-group-reviews must be at least 1")
    if group_l2 < 0:
        raise ProbeError("--group-l2 must not be negative")
    if max_iterations < 1:
        raise ProbeError("--max-iterations must be at least 1")
    if frequency_bins < 2:
        raise ProbeError("--frequency-bins must be at least 2")


if __name__ == "__main__":
    sys.exit(main())
