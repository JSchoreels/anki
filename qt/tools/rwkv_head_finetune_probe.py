#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sqlite3
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, cast

import torch  # type: ignore[import-not-found]

try:
    from tools import rwkv_temporal_calibration_probe as temporal
except ModuleNotFoundError:
    import rwkv_temporal_calibration_probe as temporal  # type: ignore[import-not-found, no-redef]

ProbeError = temporal.ProbeError

_PRESET_ID_RESOLUTION_BATCH_SIZE = 10_000


@dataclass(frozen=True)
class ReviewRow:
    review_id: int
    card_id: int
    note_id: int
    deck_id: int
    preset_id: int
    ease: int
    duration_millis: int
    review_kind: int
    is_learning_start: bool
    elapsed_days: int
    elapsed_seconds: int
    day_offset: int
    recall_bin: tuple[int, int, int]


@dataclass(frozen=True)
class HistoricalPresetRule:
    preset_id: str
    card_ids: frozenset[int] | None
    min_reps: int | None
    max_reps: int | None
    min_interval_days: float | None
    max_interval_days: float | None


@dataclass(frozen=True)
class ExtractedSamples:
    features: torch.Tensor
    raw_logits: torch.Tensor
    raw_predictions: torch.Tensor
    labels: torch.Tensor
    recall_bins: list[tuple[int, int, int]]
    review_ids: list[int]


@dataclass(frozen=True)
class SplitData:
    train: ExtractedSamples
    validation: ExtractedSamples
    test: ExtractedSamples


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        report = head_finetune_report(
            collection_path=args.collection_copy,
            model_path=args.model_path,
            deck_match=args.deck_match,
            train_fraction=args.train_fraction,
            validation_fraction=args.validation_fraction,
            limit=args.limit,
            learning_rate=args.learning_rate,
            max_epochs=args.max_epochs,
            patience=args.patience,
            l2_values=args.l2,
            device=args.device,
        )
    except ProbeError as err:
        print(
            json.dumps({"error": str(err)}, indent=2, sort_keys=True), file=sys.stderr
        )
        return 2

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fit a local binary recall head on frozen RWKV hidden states using a "
            "chronological train/validation/test split."
        )
    )
    parser.add_argument(
        "--collection-copy",
        type=Path,
        required=True,
        help="Path to a copied collection.anki2 database to inspect.",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.pth"),
        help="PyTorch RWKV checkpoint. Defaults to the bundled checkpoint.",
    )
    parser.add_argument(
        "--deck-match",
        default="Yomitan",
        help="Deck name match. Defaults to Yomitan.",
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
        help="Limit eligible reviews for faster diagnosis. 0 means no limit.",
    )
    parser.add_argument(
        "--l2",
        type=float,
        action="append",
        default=None,
        help="L2 penalty to try. Can be passed more than once.",
    )
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=0.05,
        help="Adam learning rate. Defaults to 0.05.",
    )
    parser.add_argument(
        "--max-epochs",
        type=int,
        default=200,
        help="Maximum epochs for each local head. Defaults to 200.",
    )
    parser.add_argument(
        "--patience",
        type=int,
        default=20,
        help="Validation patience for early stopping. Defaults to 20.",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Torch device for extraction/training. Defaults to cpu.",
    )
    return parser


def head_finetune_report(
    *,
    collection_path: Path,
    model_path: Path,
    deck_match: str | None,
    train_fraction: float,
    validation_fraction: float,
    limit: int,
    learning_rate: float,
    max_epochs: int,
    patience: int,
    l2_values: Sequence[float] | None,
    device: str,
) -> dict[str, Any]:
    collection_path = collection_path.expanduser().resolve()
    model_path = model_path.expanduser().resolve()
    if not collection_path.exists():
        raise ProbeError(f"collection copy does not exist: {collection_path}")
    if not model_path.exists():
        raise ProbeError(f"model checkpoint does not exist: {model_path}")
    temporal._validate_split(train_fraction, validation_fraction)

    rows = _load_review_rows(
        collection_path,
        deck_match=deck_match,
        limit=limit,
    )
    if len(rows) < 10:
        raise ProbeError(f"only {len(rows)} eligible reviews available")

    started_at = time.monotonic()
    samples = _extract_samples(
        rows,
        model_path=model_path,
        device=torch.device(device),
    )
    extraction_elapsed_ms = (time.monotonic() - started_at) * 1000
    split = _split_samples(
        samples,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    standardized = _standardized_split(split)
    raw_baseline = {
        "train": _metrics_for_samples(split.train, split.train.raw_predictions),
        "validation": _metrics_for_samples(
            split.validation,
            split.validation.raw_predictions,
        ),
        "test": _metrics_for_samples(split.test, split.test.raw_predictions),
    }

    penalties = tuple(l2_values or (0.0, 1e-5, 1e-4, 1e-3, 1e-2))
    heads: dict[str, Any] = {}
    for name, feature_split in {
        "hidden_head": standardized,
        "hidden_plus_raw_logit_head": _with_raw_logit_feature(standardized, split),
    }.items():
        candidates = []
        for l2 in penalties:
            fit = _fit_binary_head(
                feature_split,
                learning_rate=learning_rate,
                max_epochs=max_epochs,
                patience=patience,
                l2=l2,
            )
            candidates.append(fit)
        best = min(candidates, key=lambda result: result["validation"]["log_loss"])
        heads[name] = {
            **best,
            "description": _head_description(name),
        }

    best_head_name = min(
        heads,
        key=lambda name: heads[name]["validation"]["log_loss"],
    )
    return {
        "collection": str(collection_path),
        "model": str(model_path),
        "input": {
            "deck_match": deck_match,
            "eligible_reviews": len(rows),
            "used_reviews": len(samples.review_ids),
            "device": device,
            "extraction_elapsed_ms": extraction_elapsed_ms,
        },
        "split": {
            "train": _split_summary(split.train),
            "validation": _split_summary(split.validation),
            "test": _split_summary(split.test),
        },
        "baseline": raw_baseline,
        "heads": heads,
        "best_by_validation_log_loss": best_head_name,
    }


def _load_review_rows(
    collection_path: Path,
    *,
    deck_match: str | None,
    limit: int,
) -> list[ReviewRow]:
    with _open_analysis_collection_copy(collection_path) as collection:
        return _load_review_rows_from_collection(
            collection,
            deck_match=deck_match,
            limit=limit,
        )


@contextmanager
def _open_analysis_collection_copy(collection_path: Path) -> Iterator[Any]:
    """Open only a disposable second-generation copy with the Anki backend."""
    source = collection_path.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="rwkv-head-finetune-probe-") as temp:
        temporary_dir = Path(temp)
        working_copy = temporary_dir / source.name
        try:
            temporal._copy_sqlite_db_with_sidecars(source, temporary_dir)
        except OSError as err:
            raise ProbeError(f"failed to copy collection copy: {source}") from err
        if not working_copy.exists():
            raise ProbeError(f"failed to copy collection copy: {source}")

        Collection = _anki_collection_type()
        try:
            collection = Collection(str(working_copy))
        except Exception as err:
            raise ProbeError(
                f"Anki backend could not open the temporary collection copy: {err}"
            ) from err
        try:
            yield collection
        finally:
            collection.close()


def _anki_collection_type() -> type[Any]:
    try:
        from anki.collection import Collection
    except ImportError as err:
        raise ProbeError(
            "Anki backend is unavailable; build pylib and run this probe from "
            "Anki's Python environment"
        ) from err
    return Collection


def _load_review_rows_from_collection(
    collection: Any,
    *,
    deck_match: str | None,
    limit: int,
) -> list[ReviewRow]:
    connection = getattr(collection, "db", None)
    if connection is None:
        raise ProbeError("Anki backend collection is not open")
    deck_ids = temporal._matched_deck_ids(
        cast(sqlite3.Connection, connection), deck_match
    )
    raw_rows = _historical_rows(connection, deck_ids=deck_ids, limit=limit)

    timing = _scheduler_timing(collection)
    days_elapsed = timing[0]
    next_day_at = timing[1]
    card_ids = list(dict.fromkeys(row[1] for row in raw_rows))
    preset_id_by_card = _resolved_preset_ids_for_cards(collection, card_ids)
    deck_configs = _deck_configs_for_rows(collection, raw_rows)
    dynamic_preset_replay = _dynamic_preset_replay_enabled(collection)
    historical_preset_rules = (
        _historical_preset_rules(collection, card_ids) if dynamic_preset_replay else []
    )

    previous_review_id_by_card: dict[int, int] = {}
    previous_interval_days_by_card: dict[int, int] = {}
    review_count_by_card: dict[int, int] = {}
    prior_long_term_reviews_by_card: dict[int, int] = {}
    prior_lapses_by_card: dict[int, int] = {}
    rows: list[ReviewRow] = []
    for (
        review_id,
        card_id,
        note_id,
        deck_id,
        ease,
        duration_millis,
        review_kind,
        interval_days,
        is_learning_start,
    ) in raw_rows:
        previous_review_id = previous_review_id_by_card.get(card_id)
        elapsed_seconds = (
            max(0, (review_id - previous_review_id) // 1000)
            if previous_review_id is not None
            else _first_review_elapsed_seconds(
                review_id,
                card_id,
                deck_configs[deck_id],
                is_learning_start=is_learning_start,
            )
        )
        day_offset = _historical_day_offset(
            review_id,
            days_elapsed=days_elapsed,
            next_day_at=next_day_at,
        )
        elapsed_days = (
            max(
                0,
                day_offset
                - _historical_day_offset(
                    previous_review_id,
                    days_elapsed=days_elapsed,
                    next_day_at=next_day_at,
                ),
            )
            if previous_review_id is not None
            else elapsed_seconds // 86_400
            if elapsed_seconds >= 0
            else -1
        )

        review_count = review_count_by_card.get(card_id, 0)
        historical_preset_id = _historical_preset_id_for_review(
            historical_preset_rules,
            card_id=card_id,
            interval_days=previous_interval_days_by_card.get(card_id, 0),
            review_count=review_count,
        )
        resolved_preset_id = (
            historical_preset_id
            if historical_preset_id is not None
            else preset_id_by_card[card_id]
        )
        preset_id = _stable_preset_id(resolved_preset_id)
        is_long_term_review = elapsed_days >= 1
        prior_long_term_reviews = prior_long_term_reviews_by_card.get(card_id, 0)
        prior_lapses = prior_lapses_by_card.get(card_id, 0)
        long_term_reviews = prior_long_term_reviews + int(is_long_term_review)
        rows.append(
            ReviewRow(
                review_id=review_id,
                card_id=card_id,
                note_id=note_id,
                deck_id=deck_id,
                preset_id=preset_id,
                ease=ease,
                duration_millis=duration_millis,
                review_kind=review_kind,
                is_learning_start=is_learning_start,
                elapsed_days=elapsed_days,
                elapsed_seconds=elapsed_seconds,
                day_offset=day_offset,
                recall_bin=(
                    temporal._fsrs_delta_t_bin(elapsed_days),
                    temporal._fsrs_count_bin(long_term_reviews + 1.0, 1.99, 1.89),
                    0
                    if prior_lapses == 0
                    else temporal._fsrs_count_bin(prior_lapses, 1.65, 1.73),
                ),
            )
        )
        previous_review_id_by_card[card_id] = review_id
        previous_interval_days_by_card[card_id] = interval_days
        review_count_by_card[card_id] = review_count + 1
        prior_long_term_reviews_by_card[card_id] = long_term_reviews
        if ease == 1:
            prior_lapses_by_card[card_id] = prior_lapses + 1
    return rows


def _historical_rows(
    connection: object,
    *,
    deck_ids: Sequence[int] | None,
    limit: int,
) -> list[tuple[int, int, int, int, int, int, int, int, bool]]:
    deck_clause = ""
    params: list[int] = []
    effective_deck_sql = "(CASE WHEN c.odid != 0 THEN c.odid ELSE c.did END)"
    if deck_ids is not None:
        if not deck_ids:
            return []
        deck_clause = (
            f"AND {effective_deck_sql} IN ({temporal._placeholders(deck_ids)})"
        )
        params.extend(deck_ids)

    sql = f"""
    WITH eligible AS (
      SELECT
        r.id,
        r.cid,
        c.nid,
        {effective_deck_sql} AS did,
        r.ease,
        r.time,
        r.type,
        CAST(r.ivl AS INTEGER) AS ivl,
        lag(r.type) OVER (PARTITION BY r.cid ORDER BY r.id) AS previous_type
      FROM revlog r
      JOIN cards c ON c.id = r.cid
      WHERE r.ease BETWEEN 1 AND 4
        AND r.type IN (0, 1, 2, 3, 4, 5)
        AND NOT (r.type = 3 AND r.factor = 0)
        {deck_clause}
    ), retained_starts AS (
      SELECT cid, max(id) AS start_id
      FROM eligible
      WHERE type = 0 AND (previous_type IS NULL OR previous_type != 0)
      GROUP BY cid
    )
    SELECT e.id, e.cid, e.nid, e.did, e.ease, e.time, e.type, e.ivl,
           e.id = s.start_id
    FROM eligible e
    JOIN retained_starts s ON s.cid = e.cid
    WHERE e.id >= s.start_id
    ORDER BY e.id, e.cid
    """
    if limit > 0:
        sql += "\nLIMIT ?"
        params.append(limit)

    if isinstance(connection, sqlite3.Connection):
        query_rows = connection.execute(sql, params)
    else:
        execute = getattr(connection, "execute", None)
        if not callable(execute):
            raise ProbeError("Anki backend database query API is unavailable")
        query_rows = execute(sql, *params)

    return [
        (
            int(review_id),
            int(card_id),
            int(note_id),
            int(deck_id),
            int(ease),
            int(duration_millis),
            int(review_kind),
            int(interval_days),
            bool(is_learning_start),
        )
        for (
            review_id,
            card_id,
            note_id,
            deck_id,
            ease,
            duration_millis,
            review_kind,
            interval_days,
            is_learning_start,
        ) in query_rows
    ]


def _scheduler_timing(collection: Any) -> tuple[int, int]:
    scheduler = getattr(collection, "sched", None)
    timing_today = getattr(scheduler, "_timing_today", None)
    if not callable(timing_today):
        raise ProbeError("Anki scheduler timing API is unavailable")
    try:
        timing = timing_today()
    except Exception as err:
        raise ProbeError("failed to read Anki scheduler timing") from err
    days_elapsed = getattr(timing, "days_elapsed", None)
    next_day_at = getattr(timing, "next_day_at", None)
    if not _is_int(days_elapsed) or not _is_int(next_day_at):
        raise ProbeError("Anki scheduler returned invalid timing")
    return int(days_elapsed), int(next_day_at)


def _resolved_preset_ids_for_cards(
    collection: Any,
    card_ids: Sequence[int],
) -> dict[int, str]:
    if not card_ids:
        return {}
    backend = getattr(collection, "_backend", None)
    resolve = getattr(backend, "get_fsrs_preset_ids_for_cards", None)
    if not callable(resolve):
        raise ProbeError("Anki backend preset batch resolver is unavailable")

    resolved: dict[int, str] = {}
    for start in range(0, len(card_ids), _PRESET_ID_RESOLUTION_BATCH_SIZE):
        batch = list(card_ids[start : start + _PRESET_ID_RESOLUTION_BATCH_SIZE])
        try:
            response = resolve(batch)
        except Exception as err:
            raise ProbeError("Anki backend failed to resolve FSRS presets") from err

        items = getattr(response, "items", response)
        if callable(items):
            raise ProbeError("Anki backend returned an invalid FSRS preset response")
        try:
            for item in items:
                card_id = getattr(item, "card_id", None)
                preset_id = getattr(item, "preset_id", None)
                if _is_int(card_id) and isinstance(preset_id, str) and preset_id:
                    resolved[int(card_id)] = preset_id
        except TypeError as err:
            raise ProbeError(
                "Anki backend returned an invalid FSRS preset response"
            ) from err

    missing = [card_id for card_id in card_ids if card_id not in resolved]
    if missing:
        preview = ", ".join(str(card_id) for card_id in missing[:5])
        suffix = "..." if len(missing) > 5 else ""
        raise ProbeError(
            f"Anki backend did not resolve FSRS presets for card ids: {preview}{suffix}"
        )
    return resolved


def _deck_configs_for_rows(
    collection: Any,
    rows: Sequence[Sequence[object]],
) -> dict[int, dict[str, object]]:
    decks = getattr(collection, "decks", None)
    config_for_deck = getattr(decks, "config_dict_for_deck_id", None)
    if not callable(config_for_deck):
        raise ProbeError("Anki deck-config resolver is unavailable")

    configs: dict[int, dict[str, object]] = {}
    for deck_id in sorted({cast(int, row[3]) for row in rows}):
        try:
            config = config_for_deck(deck_id)
        except Exception as err:
            raise ProbeError(
                f"failed to resolve deck config for deck id {deck_id}"
            ) from err
        if not isinstance(config, dict):
            raise ProbeError(f"invalid deck config for deck id {deck_id}")
        configs[deck_id] = config
    return configs


def _dynamic_preset_replay_enabled(collection: Any) -> bool:
    decks = getattr(collection, "decks", None)
    all_config = getattr(decks, "all_config", None)
    if not callable(all_config):
        raise ProbeError("Anki deck-config listing API is unavailable")
    try:
        configs = all_config()
    except Exception as err:
        raise ProbeError("failed to read Anki deck configs") from err
    return any(
        isinstance(config, dict)
        and _rwkv_review_config_active(config)
        and _rwkv_review_dynamic_preset_replay(config)
        for config in configs
    )


def _historical_preset_rules(
    collection: Any,
    candidate_card_ids: Sequence[int],
) -> list[HistoricalPresetRule]:
    if not candidate_card_ids:
        return []

    get_config = getattr(collection, "get_config", None)
    if not callable(get_config):
        raise ProbeError("Anki collection config API is unavailable")
    try:
        overlay = get_config("fsrsPresetOverlay")
    except Exception as err:
        raise ProbeError("failed to read FSRS preset overlay") from err
    if not isinstance(overlay, dict):
        return []
    raw_rules = overlay.get("simulator_rules")
    if not isinstance(raw_rules, list):
        return []

    find_cards = getattr(collection, "find_cards", None)
    candidates = frozenset(candidate_card_ids)
    search_matches: dict[str, frozenset[int]] = {}
    rules: list[HistoricalPresetRule] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            continue
        preset_id = raw_rule.get("preset_id")
        if not isinstance(preset_id, str) or not preset_id:
            continue
        search = raw_rule.get("search")
        search_text = (
            search.strip() if isinstance(search, str) and search.strip() else None
        )
        card_ids: frozenset[int] | None = None
        if search_text is not None:
            if not callable(find_cards):
                raise ProbeError("Anki card-search API is unavailable")
            card_ids = search_matches.get(search_text)
            if card_ids is None:
                try:
                    matched = find_cards(search_text, order=False)
                except Exception as err:
                    raise ProbeError(
                        f"failed to evaluate simulator rule search: {search_text!r}"
                    ) from err
                card_ids = frozenset(
                    int(card_id)
                    for card_id in matched
                    if _is_int(card_id) and card_id in candidates
                )
                search_matches[search_text] = card_ids

        min_reps = _optional_int(raw_rule.get("min_reps"))
        max_reps = _optional_int(raw_rule.get("max_reps"))
        min_interval_days = _optional_float(raw_rule.get("min_interval_days"))
        max_interval_days = _optional_float(raw_rule.get("max_interval_days"))
        if (
            min_reps is None
            and max_reps is None
            and min_interval_days is None
            and max_interval_days is None
        ):
            continue
        rules.append(
            HistoricalPresetRule(
                preset_id=preset_id,
                card_ids=card_ids,
                min_reps=min_reps,
                max_reps=max_reps,
                min_interval_days=min_interval_days,
                max_interval_days=max_interval_days,
            )
        )
    return rules


def _historical_preset_id_for_review(
    rules: Sequence[HistoricalPresetRule],
    *,
    card_id: int,
    interval_days: int,
    review_count: int,
) -> str | None:
    for rule in rules:
        if rule.card_ids is not None and card_id not in rule.card_ids:
            continue
        if rule.min_reps is not None and review_count < rule.min_reps:
            continue
        if rule.max_reps is not None and review_count > rule.max_reps:
            continue
        if (
            rule.min_interval_days is not None
            and interval_days < rule.min_interval_days
        ):
            continue
        if (
            rule.max_interval_days is not None
            and interval_days > rule.max_interval_days
        ):
            continue
        return rule.preset_id
    return None


def _stable_preset_id(preset_id: str) -> int:
    if preset_id.isdecimal():
        return int(preset_id)
    digest = hashlib.blake2b(preset_id.encode("utf8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def _first_review_elapsed_seconds(
    review_id: int,
    card_id: int,
    deck_config: dict[str, object],
    *,
    is_learning_start: bool,
) -> int:
    if (
        not is_learning_start
        or not _rwkv_review_first_review_elapsed_from_card_creation(deck_config)
    ):
        return -1
    return max(0, (review_id - card_id) // 1000)


def _rwkv_review_config_active(deck_config: dict[str, object]) -> bool:
    return _rwkv_review_config_enabled(
        deck_config
    ) or _rwkv_review_instant_order_enabled(deck_config)


def _rwkv_review_config_enabled(deck_config: dict[str, object]) -> bool:
    return _rwkv_config_bool(
        deck_config,
        nested_key="rwkv_review_enabled",
        camel_key="rwkvReviewEnabled",
        default=False,
    )


def _rwkv_review_instant_order_enabled(deck_config: dict[str, object]) -> bool:
    return _rwkv_config_bool(
        deck_config,
        nested_key="rwkv_review_instant_order_enabled",
        camel_key="rwkvReviewInstantOrderEnabled",
        default=False,
    )


def _rwkv_review_dynamic_preset_replay(deck_config: dict[str, object]) -> bool:
    return _rwkv_config_bool(
        deck_config,
        nested_key="rwkv_review_dynamic_preset_replay",
        camel_key="rwkvReviewDynamicPresetReplay",
        default=False,
    )


def _rwkv_review_first_review_elapsed_from_card_creation(
    deck_config: dict[str, object],
) -> bool:
    return _rwkv_config_bool(
        deck_config,
        nested_key="rwkv_review_first_review_elapsed_from_card_creation",
        camel_key="rwkvReviewFirstReviewElapsedFromCardCreation",
        default=True,
    )


def _rwkv_config_bool(
    deck_config: dict[str, object],
    *,
    nested_key: str,
    camel_key: str,
    default: bool,
) -> bool:
    nested = _rwkv_other_config(deck_config)
    if nested is not None:
        value = nested.get(nested_key)
        if isinstance(value, bool):
            return value
    value = deck_config.get(camel_key, deck_config.get(nested_key))
    return value if isinstance(value, bool) else default


def _rwkv_other_config(
    deck_config: dict[str, object],
) -> dict[str, object] | None:
    direct = deck_config.get("jschoreels.rwkv", deck_config.get("jschoreels.fsrs"))
    if isinstance(direct, dict):
        return direct
    other = deck_config.get("other")
    if isinstance(other, dict):
        root = other
    elif isinstance(other, bytes | bytearray):
        root = _json_object(other.decode("utf8", errors="ignore"))
    elif isinstance(other, str):
        root = _json_object(other)
    else:
        return None
    if root is None:
        return None
    value = root.get("jschoreels.rwkv", root.get("jschoreels.fsrs"))
    return value if isinstance(value, dict) else None


def _json_object(text: str) -> dict[str, object] | None:
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _optional_int(value: object) -> int | None:
    return cast(int, value) if _is_int(value) else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _extract_samples(
    rows: Sequence[ReviewRow],
    *,
    model_path: Path,
    device: torch.device,
) -> ExtractedSamples:
    process = _rwkv_inference_process(
        model_path=model_path,
        device=device,
        dtype=torch.float32,
    )
    features: list[torch.Tensor] = []
    raw_logits: list[float] = []
    raw_predictions: list[float] = []
    labels: list[float] = []
    recall_bins: list[tuple[int, int, int]] = []
    review_ids: list[int] = []
    for index, row in enumerate(rows, start=1):
        base_row = _model_row(row)
        hidden, raw_logit, raw_prediction = _query_hidden(process, base_row)
        features.append(hidden.cpu())
        raw_logits.append(raw_logit)
        raw_predictions.append(raw_prediction)
        labels.append(0.0 if row.ease == 1 else 1.0)
        recall_bins.append(row.recall_bin)
        review_ids.append(row.review_id)
        process.process_row(base_row)
        if index % 25_000 == 0:
            print(
                f"extracted {index:,}/{len(rows):,} RWKV head samples",
                file=sys.stderr,
                flush=True,
            )

    return ExtractedSamples(
        features=torch.stack(features).float(),
        raw_logits=torch.tensor(raw_logits, dtype=torch.float32).unsqueeze(1),
        raw_predictions=torch.tensor(raw_predictions, dtype=torch.float32),
        labels=torch.tensor(labels, dtype=torch.float32),
        recall_bins=recall_bins,
        review_ids=review_ids,
    )


def _rwkv_inference_process(
    *,
    model_path: Path,
    device: torch.device,
    dtype: torch.dtype,
) -> Any:
    RwkvInferenceProcess = _rwkv_inference_process_type()
    return RwkvInferenceProcess(
        model_path=model_path,
        device=device,
        dtype=dtype,
    )


def _rwkv_inference_process_type() -> type:
    package_name = "_rwkv_head_finetune_probe_inference"
    if package_name in sys.modules:
        return getattr(sys.modules[package_name], "RwkvInferenceProcess")

    package_dir = Path(__file__).resolve().parents[1] / "aqt" / "rwkv_inference"
    spec = importlib.util.spec_from_file_location(
        package_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ProbeError(f"could not load RWKV inference package from {package_dir}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return getattr(module, "RwkvInferenceProcess")


def _query_hidden(
    process: Any,
    row: Mapping[str, object],
) -> tuple[torch.Tensor, float, float]:
    prepared = process._query_row(row)
    features = process._get_tensor(prepared)
    with torch.inference_mode():
        card_id = _int_row_value(prepared, "card_id")
        note_id = _int_row_value(prepared, "note_id")
        deck_id = _int_row_value(prepared, "deck_id")
        preset_id = _int_row_value(prepared, "preset_id")
        process.card_states.setdefault(card_id, None)
        process.note_states.setdefault(note_id, None)
        process.deck_states.setdefault(deck_id, None)
        process.preset_states.setdefault(preset_id, None)

        card_input = process.rnn.features2card(features)
        card_encoding, _next_card_state = process.rnn.rwkv_modules[0].run(
            card_input,
            process.card_states[card_id],
        )
        deck_encoding, _next_deck_state = process.rnn.rwkv_modules[1].run(
            card_encoding,
            process.deck_states[deck_id],
        )
        note_encoding, _next_note_state = process.rnn.rwkv_modules[2].run(
            deck_encoding,
            process.note_states[note_id],
        )
        preset_encoding, _next_preset_state = process.rnn.rwkv_modules[3].run(
            note_encoding,
            process.preset_states[preset_id],
        )
        global_encoding, _next_global_state = process.rnn.rwkv_modules[4].run(
            preset_encoding,
            process.global_state,
        )
        hidden = process.rnn.prehead_dropout(process.rnn.prehead_norm(global_encoding))
        out_p_logits = process.rnn.p_linear(process.rnn.head_p(hidden).float())
        out_p_probs = torch.softmax(out_p_logits, dim=-1)
        again_probability = out_p_probs[..., 0]
        prediction = 1.0 - again_probability
        raw_logit = torch.logit(prediction.clamp(1e-6, 1 - 1e-6))
        return (
            hidden.squeeze(0).detach(),
            float(raw_logit.item()),
            float(prediction.item()),
        )


def _model_row(row: ReviewRow) -> dict[str, object]:
    return {
        "card_id": row.card_id,
        "note_id": row.note_id,
        "deck_id": row.deck_id,
        "preset_id": row.preset_id,
        "elapsed_days": row.elapsed_days,
        "elapsed_seconds": row.elapsed_seconds,
        "day_offset": row.day_offset,
        "duration": float(row.duration_millis),
        "state": _historical_state(
            row.review_kind,
            is_learning_start=row.is_learning_start,
        ),
        "rating": row.ease,
    }


def _historical_state(review_kind: int, *, is_learning_start: bool = False) -> int:
    return 0 if is_learning_start else review_kind + 1


def _historical_day_offset(
    review_id: int,
    *,
    days_elapsed: int,
    next_day_at: int,
) -> int:
    review_seconds = review_id // 1000
    days_before_today = max(0, next_day_at - 1 - review_seconds) // temporal.DAY_SECONDS
    return max(0, days_elapsed - days_before_today)


def _split_samples(
    samples: ExtractedSamples,
    *,
    train_fraction: float,
    validation_fraction: float,
) -> SplitData:
    train_end = max(1, int(len(samples.review_ids) * train_fraction))
    validation_end = max(
        train_end + 1,
        int(len(samples.review_ids) * (train_fraction + validation_fraction)),
    )
    validation_end = min(validation_end, len(samples.review_ids) - 1)
    return SplitData(
        train=_slice_samples(samples, 0, train_end),
        validation=_slice_samples(samples, train_end, validation_end),
        test=_slice_samples(samples, validation_end, len(samples.review_ids)),
    )


def _slice_samples(samples: ExtractedSamples, start: int, end: int) -> ExtractedSamples:
    return ExtractedSamples(
        features=samples.features[start:end],
        raw_logits=samples.raw_logits[start:end],
        raw_predictions=samples.raw_predictions[start:end],
        labels=samples.labels[start:end],
        recall_bins=samples.recall_bins[start:end],
        review_ids=samples.review_ids[start:end],
    )


def _standardized_split(split: SplitData) -> SplitData:
    mean = split.train.features.mean(dim=0, keepdim=True)
    std = split.train.features.std(dim=0, keepdim=True).clamp_min(1e-6)
    return SplitData(
        train=_replace_features(split.train, (split.train.features - mean) / std),
        validation=_replace_features(
            split.validation,
            (split.validation.features - mean) / std,
        ),
        test=_replace_features(split.test, (split.test.features - mean) / std),
    )


def _with_raw_logit_feature(standardized: SplitData, original: SplitData) -> SplitData:
    raw_mean = original.train.raw_logits.mean(dim=0, keepdim=True)
    raw_std = original.train.raw_logits.std(dim=0, keepdim=True).clamp_min(1e-6)

    def append_raw(
        samples: ExtractedSamples, original_samples: ExtractedSamples
    ) -> ExtractedSamples:
        raw = (original_samples.raw_logits - raw_mean) / raw_std
        return _replace_features(samples, torch.cat([samples.features, raw], dim=1))

    return SplitData(
        train=append_raw(standardized.train, original.train),
        validation=append_raw(standardized.validation, original.validation),
        test=append_raw(standardized.test, original.test),
    )


def _replace_features(
    samples: ExtractedSamples,
    features: torch.Tensor,
) -> ExtractedSamples:
    return ExtractedSamples(
        features=features,
        raw_logits=samples.raw_logits,
        raw_predictions=samples.raw_predictions,
        labels=samples.labels,
        recall_bins=samples.recall_bins,
        review_ids=samples.review_ids,
    )


def _fit_binary_head(
    split: SplitData,
    *,
    learning_rate: float,
    max_epochs: int,
    patience: int,
    l2: float,
) -> dict[str, Any]:
    model = torch.nn.Linear(split.train.features.shape[1], 1)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=l2)
    loss_fn = torch.nn.BCEWithLogitsLoss()
    best_state = {
        name: value.detach().clone() for name, value in model.state_dict().items()
    }
    best_validation = float("inf")
    best_epoch = 0
    stale_epochs = 0

    for epoch in range(1, max_epochs + 1):
        model.train()
        optimizer.zero_grad()
        train_logits = model(split.train.features).squeeze(1)
        loss = loss_fn(train_logits, split.train.labels)
        loss.backward()
        optimizer.step()

        model.eval()
        with torch.no_grad():
            validation_logits = model(split.validation.features).squeeze(1)
            validation_loss = float(loss_fn(validation_logits, split.validation.labels))
        if validation_loss < best_validation - 1e-7:
            best_validation = validation_loss
            best_epoch = epoch
            stale_epochs = 0
            best_state = {
                name: value.detach().clone()
                for name, value in model.state_dict().items()
            }
        else:
            stale_epochs += 1
            if stale_epochs >= patience:
                break

    model.load_state_dict(best_state)
    return {
        "l2": l2,
        "best_epoch": best_epoch,
        "train": _metrics_for_samples(
            split.train, _head_predictions(model, split.train)
        ),
        "validation": _metrics_for_samples(
            split.validation,
            _head_predictions(model, split.validation),
        ),
        "test": _metrics_for_samples(split.test, _head_predictions(model, split.test)),
    }


def _head_predictions(
    model: torch.nn.Module,
    samples: ExtractedSamples,
) -> torch.Tensor:
    model.eval()
    with torch.no_grad():
        return torch.sigmoid(model(samples.features).squeeze(1))


def _metrics_for_samples(
    samples: ExtractedSamples,
    predictions: torch.Tensor,
) -> dict[str, float | int]:
    return temporal._metrics(
        (
            float(prediction),
            int(label),
            recall_bin,
        )
        for prediction, label, recall_bin in zip(
            predictions.tolist(),
            samples.labels.tolist(),
            samples.recall_bins,
            strict=True,
        )
    )


def _split_summary(samples: ExtractedSamples) -> dict[str, int]:
    if not samples.review_ids:
        return {"count": 0}
    return {
        "count": len(samples.review_ids),
        "first_review_id": samples.review_ids[0],
        "last_review_id": samples.review_ids[-1],
    }


def _head_description(name: str) -> str:
    descriptions = {
        "hidden_head": "Train a new binary recall head from the frozen RWKV hidden state.",
        "hidden_plus_raw_logit_head": (
            "Train a new binary recall head from the frozen RWKV hidden state plus "
            "the original RWKV recall logit."
        ),
    }
    return descriptions[name]


def _int_row_value(row: Mapping[str, object], key: str) -> int:
    return int(row[key])  # type: ignore[call-overload]


if __name__ == "__main__":
    sys.exit(main())
