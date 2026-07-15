#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

RWKV_CACHE_TABLE = "search_stats_rwkv_review_retrievability"
RWKV_CACHE_DB_FILENAME = "collection.retrievability-cache.sqlite"
RWKV_CACHE_SCHEMA = "rwkv_cache"
DAY_SECONDS = 60 * 60 * 24
SQLITE_CHUNK_SIZE = 900
PROBABILITY_EPSILON = 1e-6
CONFIDENCE_BIN_COUNT = 10
RECENT_PERFORMANCE_ALPHA = 0.05
RECENCY_TAU_DAYS = (30, 90, 180, 365)


@dataclass(frozen=True)
class ReviewSample:
    review_id: int
    review_start_seconds: float
    outcome: int
    prediction: float
    recall_bin: tuple[int, int, int]
    features: Mapping[str, float]


@dataclass(frozen=True)
class PreparedModelData:
    names: list[str]
    train_rows: list[tuple[float, int, list[float], tuple[int, int, int]]]
    validation_rows: list[tuple[float, int, list[float], tuple[int, int, int]]]
    test_rows: list[tuple[float, int, list[float], tuple[int, int, int]]]
    means: list[float]
    stds: list[float]


class ProbeError(Exception):
    pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    copy_dir: Path | None = None
    try:
        timezone = _timezone(args.timezone)
        db_path, copied_from, copy_dir = _analysis_db_path(args)
        report = temporal_calibration_report(
            db_path,
            copied_from=copied_from,
            timezone=timezone,
            deck_match=args.deck_match,
            session_gap_seconds=args.session_gap_minutes * 60,
            answer_time_cap_seconds=args.answer_time_cap_seconds,
            train_fraction=args.train_fraction,
            validation_fraction=args.validation_fraction,
            limit=args.limit,
            model_names=args.model,
            l2=args.l2,
            max_iterations=args.max_iterations,
            min_samples=args.min_samples,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
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
            "Train a small additive calibration layer over cached RWKV "
            "pre-answer predictions using local hour and session duration."
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
        "--timezone",
        help=(
            "IANA timezone for hour-of-day features. Defaults to the local "
            "timezone, eg Europe/Brussels."
        ),
    )
    parser.add_argument(
        "--deck-match",
        help=(
            "Restrict samples to cards currently in decks whose human-readable "
            "name matches this text. Exact parent matches include child decks; "
            "otherwise matching falls back to a case-insensitive substring."
        ),
    )
    parser.add_argument(
        "--session-gap-minutes",
        type=float,
        default=30.0,
        help="Gap that starts a new review session. Defaults to 30.",
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
        "--train-fraction",
        type=float,
        default=0.70,
        help="Chronological fraction used for fitting. Defaults to 0.70.",
    )
    parser.add_argument(
        "--validation-fraction",
        type=float,
        default=0.15,
        help="Chronological fraction used for validation. Defaults to 0.15.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit eligible cached reviews for faster diagnosis. 0 means no limit.",
    )
    parser.add_argument(
        "--model",
        action="append",
        help=(
            "Model name to fit. Can be passed more than once. Defaults to all "
            "temporal probe models."
        ),
    )
    parser.add_argument(
        "--l2",
        type=float,
        default=0.01,
        help="L2 penalty for non-bias temporal weights. Defaults to 0.01.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=50,
        help="Maximum Newton iterations for each calibrator. Defaults to 50.",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=200,
        help="Minimum cached reviews required to fit models. Defaults to 200.",
    )
    return parser


def temporal_calibration_report(
    db_path: Path,
    *,
    copied_from: Path | None,
    timezone: tzinfo,
    deck_match: str | None,
    session_gap_seconds: float,
    answer_time_cap_seconds: float,
    train_fraction: float,
    validation_fraction: float,
    limit: int,
    l2: float,
    max_iterations: int,
    min_samples: int,
    model_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    _validate_split(train_fraction, validation_fraction)
    samples, raw_count = _load_samples(
        db_path,
        timezone=timezone,
        deck_match=deck_match,
        session_gap_seconds=session_gap_seconds,
        answer_time_cap_seconds=answer_time_cap_seconds,
        limit=limit,
    )
    if len(samples) < min_samples:
        raise ProbeError(
            f"only {len(samples)} cached RWKV review predictions available; "
            f"need at least {min_samples}"
        )

    split = _split_samples(
        samples,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    model_specs = _temporal_model_specs()
    selected_model_names = _selected_model_names(model_specs, model_names)
    baseline = _metrics_for_samples(samples)
    split_baseline = {
        name: _metrics_for_samples(part)
        for name, part in zip(
            ("train", "validation", "test"),
            split,
            strict=True,
        )
    }

    models = {}
    for model_name in selected_model_names:
        if model_name not in model_specs:
            continue
        feature_names = model_specs[model_name]
        prepared = _prepare_model_data(
            split,
            feature_names=feature_names,
        )
        result = _fit_additive_calibrator(
            prepared,
            l2=l2,
            max_iterations=max_iterations,
        )
        result["description"] = _temporal_model_description(model_name)
        models[model_name] = result
    for tau_days in RECENCY_TAU_DAYS:
        model_name = f"rwkv_platt_recent_{tau_days}d"
        if model_name not in selected_model_names:
            continue
        prepared = _prepare_model_data(
            split,
            feature_names=["base_logit"],
        )
        result = _fit_additive_calibrator(
            prepared,
            l2=l2,
            max_iterations=max_iterations,
            train_weights=_recency_weights(split[0], tau_days=tau_days),
        )
        result["training"] = {"recency_tau_days": tau_days}
        result["description"] = _temporal_model_description(model_name)
        models[model_name] = result

    best_name = min(
        models,
        key=lambda name: models[name]["validation"]["log_loss"],
    )
    return {
        "collection": str(db_path),
        "copied_from": str(copied_from) if copied_from is not None else None,
        "input": {
            "eligible_cached_reviews": raw_count,
            "used_reviews": len(samples),
            "timezone": str(timezone),
            "deck_match": deck_match,
            "session_gap_minutes": session_gap_seconds / 60,
            "answer_time_cap_seconds": answer_time_cap_seconds,
        },
        "split": {
            "train": _split_summary(split[0]),
            "validation": _split_summary(split[1]),
            "test": _split_summary(split[2]),
        },
        "baseline": {
            "overall": baseline,
            "train": split_baseline["train"],
            "validation": split_baseline["validation"],
            "test": split_baseline["test"],
        },
        "models": models,
        "best_by_validation_log_loss": best_name,
    }


def _temporal_model_specs() -> dict[str, list[str]]:
    confidence_features = [
        f"pred_bin_{index}" for index in range(1, CONFIDENCE_BIN_COUNT)
    ]
    return {
        "rwkv_plus_bias": [],
        "rwkv_platt": ["base_logit"],
        "rwkv_plus_hour": ["hour_sin", "hour_cos"],
        "rwkv_plus_session": ["session_log"],
        "rwkv_plus_hour_session": ["hour_sin", "hour_cos", "session_log"],
        "rwkv_confidence_bins": confidence_features,
        "rwkv_recent_performance": [
            "recent_residual_ewma",
            "recent_abs_residual_ewma",
        ],
        "rwkv_maturity_lapse": [
            "elapsed_days_log",
            "prior_long_term_reviews_log",
            "prior_lapses_log",
            "is_long_term_review",
        ],
        "rwkv_cards_between_total": ["cards_between_total_log"],
        "rwkv_cards_between_unique": ["cards_between_unique_log"],
        "rwkv_cards_between": [
            "cards_between_total_log",
            "cards_between_unique_log",
        ],
        "rwkv_maturity_lapse_cards_between": [
            "elapsed_days_log",
            "prior_long_term_reviews_log",
            "prior_lapses_log",
            "is_long_term_review",
            "cards_between_total_log",
            "cards_between_unique_log",
        ],
    }


def _temporal_model_description(model_name: str) -> str:
    if model_name.startswith("rwkv_platt_recent_"):
        return (
            "Fit a Platt-style recalibration of RWKV logits with recent training "
            "reviews weighted more heavily."
        )

    descriptions = {
        "rwkv_plus_bias": "Fit only a global logit offset on top of raw RWKV predictions.",
        "rwkv_platt": "Fit a global slope and offset for the raw RWKV prediction logit.",
        "rwkv_plus_hour": "Add local hour-of-day sine/cosine features.",
        "rwkv_plus_session": "Add elapsed time within the current review session.",
        "rwkv_plus_hour_session": "Combine local hour-of-day and session-duration features.",
        "rwkv_confidence_bins": "Add one-hot buckets for the raw RWKV confidence range.",
        "rwkv_recent_performance": "Add exponentially weighted recent residual signals.",
        "rwkv_maturity_lapse": "Add elapsed-days, prior-review, lapse, and long-term-review signals.",
        "rwkv_cards_between_total": "Add the total number of reviews since this card was last reviewed.",
        "rwkv_cards_between_unique": "Add the number of distinct other cards reviewed since this card was last reviewed.",
        "rwkv_cards_between": "Add both total and distinct intervening-card counts.",
        "rwkv_maturity_lapse_cards_between": (
            "Combine maturity/lapse signals with total and distinct "
            "intervening-card counts."
        ),
    }
    return descriptions[model_name]


def _selected_model_names(
    model_specs: Mapping[str, object],
    requested_names: Sequence[str] | None,
) -> list[str]:
    recency_model_names = [
        f"rwkv_platt_recent_{tau_days}d" for tau_days in RECENCY_TAU_DAYS
    ]
    available_names = set(model_specs).union(recency_model_names)
    if requested_names is None:
        return [*model_specs, *recency_model_names]

    selected_names = list(dict.fromkeys(requested_names))
    unknown_names = sorted(set(selected_names) - available_names)
    if unknown_names:
        raise ProbeError(f"unknown models: {', '.join(unknown_names)}")
    if not selected_names:
        raise ProbeError("at least one --model value is required")
    return selected_names


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

    copy_dir = Path(tempfile.mkdtemp(prefix="rwkv-temporal-probe-"))
    copied_db = copy_dir / "collection.anki2"
    _copy_sqlite_db_with_sidecars(live_db, copy_dir)
    _copy_sqlite_db_with_sidecars(profile_folder / RWKV_CACHE_DB_FILENAME, copy_dir)
    if not copied_db.exists():
        raise ProbeError(f"failed to copy collection: {live_db}")
    return copied_db, live_db, copy_dir


def _copy_sqlite_db_with_sidecars(source: Path, destination_dir: Path) -> None:
    for source_path in (
        source,
        source.with_name(f"{source.name}-wal"),
        source.with_name(f"{source.name}-shm"),
    ):
        if source_path.exists():
            shutil.copy2(source_path, destination_dir / source_path.name)


def _has_open_handles(path: Path) -> bool:
    result = subprocess.run(
        ["lsof", str(path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _timezone(name: str | None) -> tzinfo:
    if not name:
        return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except Exception as err:
        raise ProbeError(f"invalid timezone: {name}") from err


def _load_samples(
    db_path: Path,
    *,
    timezone: tzinfo,
    deck_match: str | None,
    session_gap_seconds: float,
    answer_time_cap_seconds: float,
    limit: int,
) -> tuple[list[ReviewSample], int]:
    connection = sqlite3.connect(_sqlite_readonly_uri(db_path), uri=True)
    try:
        cache_table = _rwkv_cache_table_ref(connection, db_path)
        deck_ids = _matched_deck_ids(connection, deck_match)
        rows = _cached_review_rows(
            connection,
            cache_table=cache_table,
            deck_ids=deck_ids,
            limit=limit,
        )
    finally:
        connection.close()

    samples = _samples_from_rows(
        rows,
        timezone=timezone,
        session_gap_seconds=session_gap_seconds,
        answer_time_cap_seconds=answer_time_cap_seconds,
    )
    return samples, len(rows)


def _cached_review_rows(
    connection: sqlite3.Connection,
    *,
    cache_table: str,
    deck_ids: Sequence[int] | None,
    limit: int,
) -> list[tuple[int, int, int, int, float]]:
    deck_clause = ""
    params: list[int] = []
    if deck_ids is not None:
        if not deck_ids:
            return []
        deck_clause = f"AND c.did IN ({_placeholders(deck_ids)})"
        params.extend(deck_ids)

    sql = f"""
    SELECT r.id, r.cid, r.ease, r.time, cache.prediction
    FROM revlog r
    JOIN cards c ON c.id = r.cid
    JOIN {cache_table} cache ON cache.revlog_id = r.id
    WHERE r.ease BETWEEN 1 AND 4
      AND r.type IN (0, 1, 2, 3, 4, 5)
      AND NOT (r.type = 3 AND r.factor = 0)
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
            int(ease),
            int(duration_millis),
            float(prediction),
        )
        for review_id, card_id, ease, duration_millis, prediction in connection.execute(
            sql,
            params,
        )
    ]


def _matched_deck_ids(
    connection: sqlite3.Connection,
    deck_match: str | None,
) -> list[int] | None:
    if not deck_match:
        return None

    query = _normalize_deck_name(deck_match)
    if not query:
        raise ProbeError("--deck-match must not be empty")

    rows = [
        (int(deck_id), _normalize_deck_name(str(name).replace("\x1f", "::")))
        for deck_id, name in connection.execute("SELECT id, name FROM decks")
    ]
    exact_prefixes = [
        name
        for _deck_id, name in rows
        if name == query or name.startswith(f"{query}::")
    ]
    matched = exact_prefixes or [name for _deck_id, name in rows if query in name]
    if not matched:
        raise ProbeError(f"no decks matched --deck-match={deck_match!r}")

    matched_prefixes = tuple(f"{name}::" for name in matched)
    return sorted(
        deck_id
        for deck_id, name in rows
        if name in matched or name.startswith(matched_prefixes)
    )


def _samples_from_rows(
    rows: Sequence[tuple[int, int, int, int, float]],
    *,
    timezone: tzinfo,
    session_gap_seconds: float,
    answer_time_cap_seconds: float,
) -> list[ReviewSample]:
    samples: list[ReviewSample] = []
    session_start: float | None = None
    previous_review_start: float | None = None
    previous_review_id_by_card: dict[int, int] = {}
    previous_row_index_by_card: dict[int, int] = {}
    long_term_review_counts_by_card: dict[int, int] = {}
    prior_lapse_counts_by_card: dict[int, int] = {}
    recent_residual_ewma = 0.0
    recent_abs_residual_ewma = 0.0
    last_occurrence_index = _FenwickTree(len(rows))

    for row_index, (review_id, card_id, ease, duration_millis, prediction) in enumerate(
        rows
    ):
        prediction = _clamp_probability(prediction)
        previous_review_id = previous_review_id_by_card.get(card_id)
        elapsed_seconds = (
            max(0, (review_id - previous_review_id) // 1000)
            if previous_review_id is not None
            else -1
        )
        elapsed_days = elapsed_seconds // DAY_SECONDS if elapsed_seconds >= 0 else -1
        previous_row_index = previous_row_index_by_card.get(card_id)
        if previous_row_index is None:
            cards_between_total = 0
            cards_between_unique = 0
        else:
            cards_between_total = max(0, row_index - previous_row_index - 1)
            cards_between_unique = last_occurrence_index.range_sum(
                previous_row_index + 1,
                row_index - 1,
            )
        is_long_term_review = elapsed_days >= 1
        prior_long_term_reviews = long_term_review_counts_by_card.get(card_id, 0)
        prior_lapses = prior_lapse_counts_by_card.get(card_id, 0)
        long_term_reviews = prior_long_term_reviews + int(is_long_term_review)

        answer_seconds = review_id / 1000
        review_start_seconds = answer_seconds - _capped_answer_seconds(
            duration_millis,
            answer_time_cap_seconds,
        )
        if (
            session_start is None
            or previous_review_start is None
            or review_start_seconds - previous_review_start > session_gap_seconds
        ):
            session_start = review_start_seconds

        session_elapsed_seconds = max(0.0, review_start_seconds - session_start)
        local_time = datetime.fromtimestamp(review_start_seconds, tz=timezone)
        hour = local_time.hour + local_time.minute / 60 + local_time.second / 3600
        hour_radians = 2 * math.pi * hour / 24
        outcome = 0 if ease == 1 else 1
        prediction_bin = min(
            CONFIDENCE_BIN_COUNT - 1,
            int(prediction * CONFIDENCE_BIN_COUNT),
        )
        prediction_bin_features = {
            f"pred_bin_{index}": 1.0 if prediction_bin == index else 0.0
            for index in range(1, CONFIDENCE_BIN_COUNT)
        }
        samples.append(
            ReviewSample(
                review_id=review_id,
                review_start_seconds=review_start_seconds,
                outcome=outcome,
                prediction=prediction,
                recall_bin=(
                    _fsrs_delta_t_bin(elapsed_days),
                    _fsrs_count_bin(long_term_reviews + 1.0, 1.99, 1.89),
                    0
                    if prior_lapses == 0
                    else _fsrs_count_bin(prior_lapses, 1.65, 1.73),
                ),
                features={
                    "base_logit": _logit(prediction),
                    "hour_sin": math.sin(hour_radians),
                    "hour_cos": math.cos(hour_radians),
                    "session_log": math.log1p(session_elapsed_seconds),
                    "recent_residual_ewma": recent_residual_ewma,
                    "recent_abs_residual_ewma": recent_abs_residual_ewma,
                    "elapsed_days_log": math.log1p(max(0, elapsed_days)),
                    "prior_long_term_reviews_log": math.log1p(prior_long_term_reviews),
                    "prior_lapses_log": math.log1p(prior_lapses),
                    "is_long_term_review": float(is_long_term_review),
                    "cards_between_total_log": math.log1p(cards_between_total),
                    "cards_between_unique_log": math.log1p(cards_between_unique),
                    **prediction_bin_features,
                },
            )
        )
        previous_review_start = review_start_seconds
        if previous_row_index is not None:
            last_occurrence_index.add(previous_row_index, -1)
        last_occurrence_index.add(row_index, 1)
        previous_row_index_by_card[card_id] = row_index
        previous_review_id_by_card[card_id] = review_id
        if is_long_term_review:
            long_term_review_counts_by_card[card_id] = long_term_reviews
            if ease == 1:
                prior_lapse_counts_by_card[card_id] = prior_lapses + 1
        residual = outcome - prediction
        recent_residual_ewma = (
            1 - RECENT_PERFORMANCE_ALPHA
        ) * recent_residual_ewma + RECENT_PERFORMANCE_ALPHA * residual
        recent_abs_residual_ewma = (
            1 - RECENT_PERFORMANCE_ALPHA
        ) * recent_abs_residual_ewma + RECENT_PERFORMANCE_ALPHA * abs(residual)

    return samples


class _FenwickTree:
    def __init__(self, size: int) -> None:
        self._values = [0 for _ in range(size + 1)]

    def add(self, index: int, delta: int) -> None:
        index += 1
        while index < len(self._values):
            self._values[index] += delta
            index += index & -index

    def range_sum(self, start: int, end: int) -> int:
        if end < start:
            return 0
        return self._prefix_sum(end) - self._prefix_sum(start - 1)

    def _prefix_sum(self, index: int) -> int:
        if index < 0:
            return 0
        total = 0
        index += 1
        while index > 0:
            total += self._values[index]
            index -= index & -index
        return total


def _capped_answer_seconds(
    duration_millis: int,
    answer_time_cap_seconds: float,
) -> float:
    duration_seconds = max(0.0, duration_millis / 1000)
    return min(duration_seconds, answer_time_cap_seconds)


def _split_samples(
    samples: Sequence[ReviewSample],
    *,
    train_fraction: float,
    validation_fraction: float,
) -> tuple[list[ReviewSample], list[ReviewSample], list[ReviewSample]]:
    train_end = max(1, int(len(samples) * train_fraction))
    validation_end = max(
        train_end + 1,
        int(len(samples) * (train_fraction + validation_fraction)),
    )
    validation_end = min(validation_end, len(samples) - 1)
    return (
        list(samples[:train_end]),
        list(samples[train_end:validation_end]),
        list(samples[validation_end:]),
    )


def _prepare_model_data(
    split: tuple[list[ReviewSample], list[ReviewSample], list[ReviewSample]],
    *,
    feature_names: Sequence[str],
) -> PreparedModelData:
    means, stds = _feature_stats(split[0], feature_names)
    return PreparedModelData(
        names=["bias", *feature_names],
        train_rows=_prepared_rows(split[0], feature_names, means, stds),
        validation_rows=_prepared_rows(split[1], feature_names, means, stds),
        test_rows=_prepared_rows(split[2], feature_names, means, stds),
        means=means,
        stds=stds,
    )


def _feature_stats(
    samples: Sequence[ReviewSample],
    names: Sequence[str],
) -> tuple[list[float], list[float]]:
    means: list[float] = []
    stds: list[float] = []
    for name in names:
        values = [sample.features[name] for sample in samples]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        means.append(mean)
        stds.append(math.sqrt(variance) or 1.0)
    return means, stds


def _recency_weights(samples: Sequence[ReviewSample], *, tau_days: int) -> list[float]:
    if not samples:
        return []
    latest = samples[-1].review_start_seconds
    return [
        math.exp(
            -max(0.0, latest - sample.review_start_seconds) / DAY_SECONDS / tau_days
        )
        for sample in samples
    ]


def _prepared_rows(
    samples: Sequence[ReviewSample],
    names: Sequence[str],
    means: Sequence[float],
    stds: Sequence[float],
) -> list[tuple[float, int, list[float], tuple[int, int, int]]]:
    rows: list[tuple[float, int, list[float], tuple[int, int, int]]] = []
    for sample in samples:
        standardized = [
            (sample.features[name] - mean) / std
            for name, mean, std in zip(names, means, stds, strict=True)
        ]
        rows.append(
            (
                _logit(sample.prediction),
                sample.outcome,
                [1.0, *standardized],
                sample.recall_bin,
            )
        )
    return rows


def _fit_additive_calibrator(
    data: PreparedModelData,
    *,
    l2: float,
    max_iterations: int,
    train_weights: Sequence[float] | None = None,
) -> dict[str, Any]:
    if train_weights is not None and len(train_weights) != len(data.train_rows):
        raise ProbeError("train weight count does not match training row count")
    weights = [0.0 for _ in data.names]
    train_loss = _objective(data.train_rows, weights, l2, row_weights=train_weights)
    for iteration in range(max_iterations):
        gradient, hessian = _gradient_and_hessian(
            data.train_rows,
            weights,
            l2,
            row_weights=train_weights,
        )
        step = _solve_linear_system(hessian, gradient)
        if max(abs(value) for value in step) < 1e-8:
            break

        accepted = False
        step_scale = 1.0
        while step_scale >= 1e-4:
            candidate = [
                weight - step_scale * delta
                for weight, delta in zip(weights, step, strict=True)
            ]
            candidate_loss = _objective(
                data.train_rows,
                candidate,
                l2,
                row_weights=train_weights,
            )
            if candidate_loss <= train_loss:
                weights = candidate
                train_loss = candidate_loss
                accepted = True
                break
            step_scale *= 0.5

        if not accepted:
            break
    else:
        iteration = max_iterations - 1

    return {
        "features": data.names,
        "weights": {
            name: weight for name, weight in zip(data.names, weights, strict=True)
        },
        "standardization": {
            name: {"mean": mean, "std": std}
            for name, mean, std in zip(
                data.names[1:], data.means, data.stds, strict=True
            )
        },
        "iterations": iteration + 1,
        "train": _metrics_for_prepared_rows(data.train_rows, weights),
        "validation": _metrics_for_prepared_rows(data.validation_rows, weights),
        "test": _metrics_for_prepared_rows(data.test_rows, weights),
    }


def _gradient_and_hessian(
    rows: Sequence[tuple[float, int, Sequence[float], tuple[int, int, int]]],
    weights: Sequence[float],
    l2: float,
    row_weights: Sequence[float] | None = None,
) -> tuple[list[float], list[list[float]]]:
    width = len(weights)
    gradient = [0.0 for _ in range(width)]
    hessian = [[0.0 for _ in range(width)] for _ in range(width)]
    weight_sum = 0.0
    for row_index, (base_logit, outcome, features, _recall_bin) in enumerate(rows):
        row_weight = row_weights[row_index] if row_weights is not None else 1.0
        weight_sum += row_weight
        prediction = _sigmoid(base_logit + _dot(weights, features))
        error = prediction - outcome
        curvature = prediction * (1 - prediction)
        for i, feature_i in enumerate(features):
            gradient[i] += row_weight * error * feature_i
            for j, feature_j in enumerate(features):
                hessian[i][j] += row_weight * curvature * feature_i * feature_j

    row_count = weight_sum if weight_sum > 0 else len(rows)
    for i in range(width):
        gradient[i] /= row_count
        for j in range(width):
            hessian[i][j] /= row_count

    for i in range(1, width):
        gradient[i] += l2 * weights[i]
        hessian[i][i] += l2
    hessian[0][0] += 1e-8
    return gradient, hessian


def _objective(
    rows: Sequence[tuple[float, int, Sequence[float], tuple[int, int, int]]],
    weights: Sequence[float],
    l2: float,
    row_weights: Sequence[float] | None = None,
) -> float:
    pairs = (
        (
            _sigmoid(base_logit + _dot(weights, features)),
            outcome,
            recall_bin,
            row_weights[row_index] if row_weights is not None else 1.0,
        )
        for row_index, (base_logit, outcome, features, recall_bin) in enumerate(rows)
    )
    log_loss = _weighted_log_loss(pairs)
    penalty = 0.5 * l2 * sum(weight * weight for weight in weights[1:])
    return log_loss + penalty


def _metrics_for_samples(samples: Sequence[ReviewSample]) -> dict[str, float | int]:
    return _metrics(
        (_clamp_probability(sample.prediction), sample.outcome, sample.recall_bin)
        for sample in samples
    )


def _metrics_for_prepared_rows(
    rows: Sequence[tuple[float, int, Sequence[float], tuple[int, int, int]]],
    weights: Sequence[float],
) -> dict[str, float | int]:
    return _metrics(
        (_sigmoid(base_logit + _dot(weights, features)), outcome, recall_bin)
        for base_logit, outcome, features, recall_bin in rows
    )


def _metrics(
    pairs: Iterable[tuple[float, int, tuple[int, int, int]]],
) -> dict[str, float | int]:
    count = 0
    positives = 0
    log_loss = 0.0
    brier = 0.0
    bin_totals: dict[tuple[int, int, int], list[float]] = {}
    for prediction, outcome, recall_bin in pairs:
        prediction = _clamp_probability(prediction)
        count += 1
        positives += outcome
        log_loss -= outcome * math.log(prediction) + (1 - outcome) * math.log(
            1 - prediction
        )
        brier += (prediction - outcome) ** 2
        value = bin_totals.setdefault(recall_bin, [0.0, 0.0, 0.0])
        value[0] += prediction
        value[1] += outcome
        value[2] += 1.0

    if count == 0:
        return {
            "count": 0,
            "positives": 0,
            "recall_rate": 0.0,
            "log_loss": 0.0,
            "brier": 0.0,
            "rmse": 0.0,
            "bins": 0,
            "rmse_bins": 0.0,
        }

    rmse_bins = _rmse_bins(bin_totals)
    return {
        "count": count,
        "positives": positives,
        "recall_rate": positives / count,
        "log_loss": log_loss / count,
        "brier": brier / count,
        "rmse": math.sqrt(brier / count),
        "bins": len(bin_totals),
        "rmse_bins": rmse_bins,
    }


def _weighted_log_loss(
    pairs: Iterable[tuple[float, int, tuple[int, int, int], float]],
) -> float:
    loss = 0.0
    weight_sum = 0.0
    for prediction, outcome, _recall_bin, weight in pairs:
        prediction = _clamp_probability(prediction)
        weight_sum += weight
        loss -= weight * (
            outcome * math.log(prediction) + (1 - outcome) * math.log(1 - prediction)
        )
    return loss / weight_sum if weight_sum > 0 else 0.0


def _split_summary(samples: Sequence[ReviewSample]) -> dict[str, Any]:
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


def _solve_linear_system(
    matrix: Sequence[Sequence[float]], rhs: Sequence[float]
) -> list[float]:
    size = len(rhs)
    augmented = [list(row) + [value] for row, value in zip(matrix, rhs, strict=True)]

    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            augmented[column][column] += 1e-6
            pivot = column
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]

        pivot_value = augmented[column][column]
        for index in range(column, size + 1):
            augmented[column][index] /= pivot_value

        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0:
                continue
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]

    return [augmented[row][size] for row in range(size)]


def _rwkv_cache_table_ref(connection: sqlite3.Connection, db_path: Path) -> str:
    if _table_columns(connection, RWKV_CACHE_TABLE):
        return _quote_identifier(RWKV_CACHE_TABLE)

    cache_path = db_path.with_name(RWKV_CACHE_DB_FILENAME)
    if cache_path.exists():
        connection.execute(
            f"ATTACH DATABASE ? AS {_quote_identifier(RWKV_CACHE_SCHEMA)}",
            (_sqlite_readonly_uri(cache_path),),
        )
        if _table_columns(connection, RWKV_CACHE_TABLE, schema=RWKV_CACHE_SCHEMA):
            return (
                f"{_quote_identifier(RWKV_CACHE_SCHEMA)}."
                f"{_quote_identifier(RWKV_CACHE_TABLE)}"
            )

    raise ProbeError(f"missing RWKV cache table: {RWKV_CACHE_TABLE}")


def _sqlite_readonly_uri(path: Path) -> str:
    return f"{path.expanduser().resolve().as_uri()}?mode=ro"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
    *,
    schema: str | None = None,
) -> set[str]:
    if schema is None and "." in table:
        schema, table = table.split(".", 1)
    if schema is None:
        pragma = f"PRAGMA table_info({_quote_identifier(table)})"
    else:
        pragma = (
            f"PRAGMA {_quote_identifier(schema)}.table_info({_quote_identifier(table)})"
        )
    return {row[1] for row in connection.execute(pragma)}


def _rmse_bins(bin_totals: Mapping[tuple[int, int, int], Sequence[float]]) -> float:
    weight_sum = sum(value[2] for value in bin_totals.values())
    if weight_sum == 0:
        return 0.0

    squared_error_sum = 0.0
    for predicted_sum, actual_sum, count in bin_totals.values():
        predicted = predicted_sum / count
        actual = actual_sum / count
        squared_error_sum += (predicted - actual) ** 2 * count
    return math.sqrt(squared_error_sum / weight_sum)


def _fsrs_delta_t_bin(delta_t: int) -> int:
    if delta_t <= 0:
        return 0
    return _fsrs_count_bin(delta_t, 248.0, 3.62)


def _fsrs_count_bin(value: float, multiplier: float, base: float) -> int:
    if value <= 0:
        return 0
    binned = multiplier * base ** math.floor(math.log(value, base))
    return round(binned) if math.isfinite(binned) and binned >= 0 else 0


def _placeholders(values: Sequence[object]) -> str:
    return ",".join("?" for _ in values)


def _normalize_deck_name(name: str) -> str:
    return name.strip().casefold()


def _validate_split(train_fraction: float, validation_fraction: float) -> None:
    if not 0 < train_fraction < 1:
        raise ProbeError("--train-fraction must be between 0 and 1")
    if not 0 <= validation_fraction < 1:
        raise ProbeError("--validation-fraction must be between 0 and 1")
    if train_fraction + validation_fraction >= 1:
        raise ProbeError("--train-fraction + --validation-fraction must be below 1")


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1 / (1 + z)
    z = math.exp(value)
    return z / (1 + z)


def _logit(probability: float) -> float:
    probability = _clamp_probability(probability)
    return math.log(probability / (1 - probability))


def _clamp_probability(value: float) -> float:
    return min(1 - PROBABILITY_EPSILON, max(PROBABILITY_EPSILON, value))


if __name__ == "__main__":
    sys.exit(main())
