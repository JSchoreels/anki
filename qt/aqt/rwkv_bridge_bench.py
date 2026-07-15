# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import argparse
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from aqt.rwkv_scheduler import (
    _RWKV_REVIEW_INPUT_BATCH_CACHE_ATTR,
    RwkvBackendCacheSnapshot,
    RwkvReviewCandidate,
    RwkvReviewIdentity,
    RwkvReviewInput,
    RwkvReviewInputBatchBuild,
    RwkvReviewPredictionRequest,
    RwkvStatefulReviewerBackend,
    RwkvStatsGraphCard,
    _rwkv_review_input_batches_for_deck_review_queue,
    _rwkv_review_input_batches_for_ids,
    _rwkv_review_input_for_stats_graph_card,
    _rwkv_review_scores_for_inputs,
    _rwkv_state_fields_for_stats_graph_card,
    _stats_graph_card_from_row,
    _stats_graph_reviewer_context,
    _stats_graph_scheduling_states,
    rwkv_review_identity,
    rwkv_review_input,
    set_reviewer_backend,
)
from aqt.rwkv_srs_benchmark import (
    _packed_prediction_requests,
    _prediction_request_row,
    _review_input_row,
    _RustRwkvRuntime,
)

_SECONDS_PER_DAY = 86_400
_DeckQueueInputSourceRow = tuple[
    int, int, int, int, int, int, int, int, int, int, int | None
]


def main() -> None:
    args = _parse_args()
    if args.candidate_mode == "score-cache":
        _run_score_cache_benchmark(args)
        return
    if args.candidate_mode == "deck-input-cache":
        _run_deck_input_cache_benchmark(args)
        return
    if args.candidate_mode == "future-score":
        _run_future_score_benchmark(args)
        return
    if args.candidate_mode is not None:
        _run_candidate_benchmark(args)
        return

    load_start = time.monotonic()
    runtime = _RustRwkvRuntime(
        model_path=args.weights,
        target_retention=args.target_retention,
        max_interval_days=args.max_interval_days,
    )
    load_ms = _elapsed_ms(load_start)

    workload_start = time.monotonic()
    workload = _CollectionWorkload.load(
        args.collection,
        args.warmup_reviews,
        args.queries,
        args.target_retention,
    )
    workload_ms = _elapsed_ms(workload_start)

    warmup_start = time.monotonic()
    snapshot = _warm_up_snapshot(runtime, workload.warmup_reviews)
    warmup_ms = _elapsed_ms(warmup_start)

    total_request_build_ms = 0.0
    total_payload_build_ms = 0.0
    total_bridge_ms = 0.0
    total_predictions = 0
    checksum = 0.0
    total_start = time.monotonic()

    for _ in range(args.repeat):
        for offset in range(0, len(workload.query_inputs), args.batch_size):
            query_inputs = workload.query_inputs[offset : offset + args.batch_size]

            request_build_start = time.monotonic()
            requests = _prediction_requests(query_inputs, snapshot)
            total_request_build_ms += _elapsed_ms(request_build_start)

            payload_build_start = time.monotonic()
            if args.mode == "packed":
                packed_payload, state_columns = _packed_prediction_requests(requests)
            else:
                row_payload = [_prediction_request_row(request) for request in requests]
            total_payload_build_ms += _elapsed_ms(payload_build_start)

            bridge_start = time.monotonic()
            if args.mode == "packed":
                outputs = runtime._process.predict_retrievability_many_packed(
                    packed_payload,
                    state_columns,
                )
            else:
                outputs = runtime._process.predict_retrievability_many(row_payload)
            total_bridge_ms += _elapsed_ms(bridge_start)

            total_predictions += len(outputs)
            checksum += sum(float(retrievability) for retrievability in outputs)

    total_ms = _elapsed_ms(total_start)
    print(f"weights={args.weights}")
    print(f"collection={args.collection}")
    print(f"queries={len(workload.query_inputs)}")
    print(f"batch_size={args.batch_size}")
    print(f"warmup_reviews={len(workload.warmup_reviews)}")
    print(f"repeat={args.repeat}")
    print(f"bridge_mode={args.mode}")
    print(f"target_retention={args.target_retention:.6f}")
    print(f"max_interval_days={args.max_interval_days}")
    print(f"load_ms={load_ms:.3f}")
    print(f"workload_ms={workload_ms:.3f}")
    print(f"warmup_ms={warmup_ms:.3f}")
    print(f"request_build_ms={total_request_build_ms:.3f}")
    print(f"payload_build_ms={total_payload_build_ms:.3f}")
    print(f"bridge_ms={total_bridge_ms:.3f}")
    print(f"total_ms={total_ms:.3f}")
    print(f"per_query_bridge_ms={total_bridge_ms / max(total_predictions, 1):.6f}")
    print(f"per_query_total_ms={total_ms / max(total_predictions, 1):.6f}")
    print(f"predictions={total_predictions}")
    print(f"checksum={checksum:.9f}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark the Python/Rust RWKV retrievability bridge.",
    )
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument(
        "--collection",
        type=Path,
        required=True,
        help="Copied collection. Do not point this at a live Anki profile.",
    )
    parser.add_argument("--queries", type=int, default=4096)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument(
        "--warmup-reviews",
        type=int,
        default=4096,
        help="0 replays all eligible review history from the collection copy.",
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--mode",
        choices=("tuple", "packed"),
        default="tuple",
    )
    parser.add_argument(
        "--candidate-mode",
        choices=(
            "python-filter",
            "sql-filter",
            "wrapper-input",
            "direct-input",
            "row-input",
            "row-input-no-deck-sql",
            "score-cache",
            "deck-input-cache",
            "future-score",
        ),
        default=None,
        help="Benchmark stats-card candidate filtering instead of RWKV inference.",
    )
    parser.add_argument(
        "--enabled-deck-limit",
        type=int,
        default=0,
        help=(
            "In candidate row-input modes, simulate only the first N current "
            "deck ids as RWKV-enabled. 0 keeps all decks enabled."
        ),
    )
    parser.add_argument("--target-retention", type=float, default=0.9)
    parser.add_argument("--max-interval-days", type=int, default=36_500)
    args = parser.parse_args()

    if args.queries <= 0:
        parser.error("--queries must be greater than zero")
    if args.batch_size <= 0:
        parser.error("--batch-size must be greater than zero")
    if args.warmup_reviews < 0:
        parser.error("--warmup-reviews must be greater than or equal to zero")
    if args.repeat <= 0:
        parser.error("--repeat must be greater than zero")

    return args


def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _run_candidate_benchmark(args: argparse.Namespace) -> None:
    timing = _BenchTiming.today()
    uri = f"file:{args.collection}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as db:
        card_ids_start = time.monotonic()
        card_ids = [
            card_id
            for (card_id,) in db.execute(
                "select id from cards order by id limit ?",
                (args.queries,),
            )
        ]
        card_ids_ms = _elapsed_ms(card_ids_start)

        load_start = time.monotonic()
        if args.candidate_mode in ("row-input", "row-input-no-deck-sql"):
            rows = []
            load_ms = 0.0
            parse_ms = 0.0
            revlog_ms = 0.0
            apply_ms = 0.0
            state_ms = 0.0
            input_start = time.monotonic()
            input_build = _rwkv_review_input_batches_for_ids(
                reviewer=_benchmark_reviewer(
                    db,
                    args.target_retention,
                    enabled_deck_limit=args.enabled_deck_limit,
                ),
                card_ids=card_ids,
                timing=SimpleNamespace(
                    days_elapsed=timing.days_elapsed,
                    next_day_at=timing.next_day_at,
                ),
                reason="stats graph benchmark",
                include_suspended_review=True,
                supported_state_filter=True,
                use_enabled_deck_filter=args.candidate_mode == "row-input",
            )
            input_ms = _elapsed_ms(input_start)
            cards = []
            missing_review_time_ids = []
            latest_review_times = {}
            supported_cards = []
            review_inputs = (
                [
                    review_input
                    for inputs_by_card_id in input_build.inputs_by_batch_size.values()
                    for _, review_input in inputs_by_card_id
                ]
                if input_build is not None
                else []
            )
            row_input_loaded_rows = input_build.loaded_rows if input_build else 0
            row_input_parsed_cards = input_build.parsed_cards if input_build else 0
            row_input_supported_cards = (
                input_build.cards_with_state if input_build else 0
            )
            row_input_load_ms = input_build.load_elapsed_ms if input_build else 0.0
            row_input_candidate_ms = (
                input_build.candidate_elapsed_ms if input_build else input_ms
            )
        else:
            rows = list(
                db.execute(
                    _stats_card_load_sql(
                        card_ids,
                        sql_filter=args.candidate_mode
                        in ("sql-filter", "wrapper-input", "direct-input"),
                    )
                )
            )
            load_ms = _elapsed_ms(load_start)

            parse_start = time.monotonic()
            cards = [card for row in rows if (card := _stats_graph_card_from_row(row))]
            parse_ms = _elapsed_ms(parse_start)

            revlog_start = time.monotonic()
            missing_review_time_ids = [
                card.id for card in cards if card.last_review_time is None
            ]
            latest_review_times = _latest_review_times(db, missing_review_time_ids)
            revlog_ms = _elapsed_ms(revlog_start)

            apply_start = time.monotonic()
            if latest_review_times:
                cards = [
                    replace(card, last_review_time=latest_review_times[card.id])
                    if card.last_review_time is None and card.id in latest_review_times
                    else card
                    for card in cards
                ]
            apply_ms = _elapsed_ms(apply_start)

            state_start = time.monotonic()
            scheduler_timing = SimpleNamespace(
                days_elapsed=timing.days_elapsed,
                next_day_at=timing.next_day_at,
            )
            supported_cards = _supported_stats_cards(cards, scheduler_timing)
            state_ms = _elapsed_ms(state_start)

            input_start = time.monotonic()
            if args.candidate_mode == "wrapper-input":
                review_inputs = _wrapper_review_inputs(
                    supported_cards,
                    scheduler_timing,
                    args.target_retention,
                )
            elif args.candidate_mode == "direct-input":
                review_inputs = _direct_review_inputs(
                    supported_cards,
                    scheduler_timing,
                    args.target_retention,
                )
            else:
                review_inputs = []
            input_ms = _elapsed_ms(input_start)
            row_input_loaded_rows = len(rows)
            row_input_parsed_cards = len(cards)
            row_input_supported_cards = len(supported_cards)
            row_input_load_ms = load_ms
            row_input_candidate_ms = input_ms

    total_ms = (
        card_ids_ms + load_ms + parse_ms + revlog_ms + apply_ms + state_ms + input_ms
    )
    print(f"collection={args.collection}")
    print(f"candidate_mode={args.candidate_mode}")
    print(f"requested_cards={len(card_ids)}")
    print(f"loaded_rows={row_input_loaded_rows}")
    print(f"parsed_cards={row_input_parsed_cards}")
    print(f"missing_review_time_cards={len(missing_review_time_ids)}")
    print(f"latest_review_time_rows={len(latest_review_times)}")
    print(f"supported_cards={row_input_supported_cards}")
    print(f"card_ids_ms={card_ids_ms:.3f}")
    print(f"load_ms={row_input_load_ms:.3f}")
    print(f"parse_ms={parse_ms:.3f}")
    print(f"revlog_ms={revlog_ms:.3f}")
    print(f"apply_ms={apply_ms:.3f}")
    print(f"state_filter_ms={state_ms:.3f}")
    print(f"review_inputs={len(review_inputs)}")
    print(f"input_build_ms={row_input_candidate_ms:.3f}")
    print(f"total_ms={total_ms:.3f}")


def _run_score_cache_benchmark(args: argparse.Namespace) -> None:
    load_start = time.monotonic()
    runtime = _RustRwkvRuntime(
        model_path=args.weights,
        target_retention=args.target_retention,
        max_interval_days=args.max_interval_days,
    )
    load_ms = _elapsed_ms(load_start)

    workload_start = time.monotonic()
    workload = _CollectionWorkload.load(
        args.collection,
        args.warmup_reviews,
        args.queries,
        args.target_retention,
    )
    workload_ms = _elapsed_ms(workload_start)

    warmup_start = time.monotonic()
    snapshot = _warm_up_snapshot(runtime, workload.warmup_reviews)
    warmup_ms = _elapsed_ms(warmup_start)

    timing = _BenchTiming.today()
    uri = f"file:{args.collection}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as db:
        card_ids = [
            card_id
            for (card_id,) in db.execute(
                "select id from cards order by id limit ?",
                (args.queries,),
            )
        ]
        input_build_start = time.monotonic()
        input_build = _rwkv_review_input_batches_for_ids(
            reviewer=_benchmark_reviewer(
                db,
                args.target_retention,
                enabled_deck_limit=args.enabled_deck_limit,
            ),
            card_ids=card_ids,
            timing=SimpleNamespace(
                days_elapsed=timing.days_elapsed,
                next_day_at=timing.next_day_at,
            ),
            reason="score cache benchmark",
            include_suspended_review=True,
            supported_state_filter=True,
        )
        input_build_ms = _elapsed_ms(input_build_start)

    inputs_by_batch_size = (
        input_build.inputs_by_batch_size if input_build is not None else {}
    )
    backend = RwkvStatefulReviewerBackend(runtime)
    backend.restore_cache_snapshot(snapshot)
    previous_backend = set_reviewer_backend(backend)
    try:
        cold_start = time.monotonic()
        cold_scores = _score_input_batches(inputs_by_batch_size)
        cold_score_ms = _elapsed_ms(cold_start)

        cached_start = time.monotonic()
        cached_scores = _score_input_batches(inputs_by_batch_size)
        cached_score_ms = _elapsed_ms(cached_start)
    finally:
        set_reviewer_backend(previous_backend)

    print(f"weights={args.weights}")
    print(f"collection={args.collection}")
    print(f"candidate_mode={args.candidate_mode}")
    print(f"requested_cards={len(card_ids)}")
    print(f"loaded_rows={input_build.loaded_rows if input_build else 0}")
    print(f"review_inputs={sum(len(value) for value in inputs_by_batch_size.values())}")
    print(f"warmup_reviews={len(workload.warmup_reviews)}")
    print(f"load_ms={load_ms:.3f}")
    print(f"workload_ms={workload_ms:.3f}")
    print(f"warmup_ms={warmup_ms:.3f}")
    print(f"input_build_ms={input_build_ms:.3f}")
    print(f"cold_score_ms={cold_score_ms:.3f}")
    print(f"cached_score_ms={cached_score_ms:.3f}")
    print(f"cold_scores={len(cold_scores)}")
    print(f"cached_scores={len(cached_scores)}")


def _run_deck_input_cache_benchmark(args: argparse.Namespace) -> None:
    timing = _BenchTiming.today()
    uri = f"file:{args.collection}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as db:
        rows = _deck_queue_input_rows(
            db,
            limit=args.queries,
            target_retention=args.target_retention,
            batch_size=args.batch_size,
            timing=timing,
        )
        searched_cards = _deck_queue_review_card_count(db)
        deck_configs = len({row.deck_id for row in rows})
        reviewer = _deck_input_cache_benchmark_reviewer(
            rows=rows,
            searched_cards=searched_cards,
            deck_configs=deck_configs,
        )

        cold_ms = 0.0
        cached_ms = 0.0
        cold_inputs = 0
        cached_inputs = 0
        for _ in range(args.repeat):
            if hasattr(reviewer, _RWKV_REVIEW_INPUT_BATCH_CACHE_ATTR):
                delattr(reviewer, _RWKV_REVIEW_INPUT_BATCH_CACHE_ATTR)
            cold_start = time.monotonic()
            cold_build = _rwkv_review_input_batches_for_deck_review_queue(
                reviewer=reviewer,
                deck_id=100,
                batch_size_override=args.batch_size,
                include_new_cards=False,
            )
            cold_ms += _elapsed_ms(cold_start)
            cold_inputs += _input_build_input_count(cold_build)

            cached_start = time.monotonic()
            cached_build = _rwkv_review_input_batches_for_deck_review_queue(
                reviewer=reviewer,
                deck_id=100,
                batch_size_override=args.batch_size,
                include_new_cards=False,
            )
            cached_ms += _elapsed_ms(cached_start)
            cached_inputs += _input_build_input_count(cached_build)

    print(f"collection={args.collection}")
    print(f"candidate_mode={args.candidate_mode}")
    print(f"requested_cards={args.queries}")
    print(f"loaded_rows={len(rows)}")
    print(f"searched_cards={searched_cards}")
    print(f"repeat={args.repeat}")
    print(f"cold_input_build_ms={cold_ms:.3f}")
    print(f"cached_input_build_ms={cached_ms:.3f}")
    print(f"per_cold_input_build_ms={cold_ms / args.repeat:.3f}")
    print(f"per_cached_input_build_ms={cached_ms / args.repeat:.3f}")
    print(f"cold_inputs={cold_inputs}")
    print(f"cached_inputs={cached_inputs}")
    print(f"backend_row_calls={reviewer.mw.col._backend.deck_row_calls}")


def _run_future_score_benchmark(args: argparse.Namespace) -> None:
    load_start = time.monotonic()
    runtime = _RustRwkvRuntime(
        model_path=args.weights,
        target_retention=args.target_retention,
        max_interval_days=args.max_interval_days,
    )
    load_ms = _elapsed_ms(load_start)

    workload_start = time.monotonic()
    workload = _CollectionWorkload.load(
        args.collection,
        args.warmup_reviews,
        args.queries,
        args.target_retention,
    )
    workload_ms = _elapsed_ms(workload_start)

    warmup_start = time.monotonic()
    snapshot = _warm_up_snapshot(runtime, workload.warmup_reviews)
    warmup_ms = _elapsed_ms(warmup_start)

    timing = _BenchTiming.today()
    uri = f"file:{args.collection}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as db:
        card_ids = [
            card_id
            for (card_id,) in db.execute(
                "select id from cards order by id limit ?",
                (args.queries,),
            )
        ]
        input_build = _rwkv_review_input_batches_for_ids(
            reviewer=_benchmark_reviewer(
                db,
                args.target_retention,
                enabled_deck_limit=args.enabled_deck_limit,
            ),
            card_ids=card_ids,
            timing=SimpleNamespace(
                days_elapsed=timing.days_elapsed,
                next_day_at=timing.next_day_at,
            ),
            reason="future score benchmark",
            include_suspended_review=True,
            supported_state_filter=True,
        )

    inputs_by_card_id = (
        _input_build_inputs(input_build) if input_build is not None else []
    )
    if not inputs_by_card_id:
        raise ValueError("future score benchmark needs at least one query input")

    answer_card_id, answer_source = inputs_by_card_id[0]
    card_info_inputs_by_card_id = [(answer_card_id, answer_source)]
    inputs_by_card_id = inputs_by_card_id[1:]
    answer_input = replace(
        answer_source,
        is_query=False,
        ease=3,
        duration_millis=1234,
    )

    backend = RwkvStatefulReviewerBackend(runtime)
    backend.restore_cache_snapshot(snapshot)
    previous_backend = set_reviewer_backend(backend)
    try:
        baseline_ms = 0.0
        baseline_scores: list[tuple[int, float]] = []
        for _ in range(args.repeat):
            backend.restore_cache_snapshot(snapshot)
            baseline_start = time.monotonic()
            backend.review_input_answered(answer_input)
            baseline_scores = _score_input_batches(
                _inputs_by_batch_size(inputs_by_card_id, args.batch_size)
            )
            baseline_ms += _elapsed_ms(baseline_start)

        native_ms = 0.0
        native_scores: list[tuple[int, float]] | None = []
        for _ in range(args.repeat):
            native_start = time.monotonic()
            native_scores = backend.predict_retrievability_after_review(
                answer=answer_input,
                inputs_by_card_id=inputs_by_card_id,
                snapshot=snapshot,
            )
            native_ms += _elapsed_ms(native_start)

        four_grade_ms = 0.0
        for _ in range(args.repeat):
            four_grade_start = time.monotonic()
            for ease in (1, 2, 3, 4):
                backend.predict_retrievability_after_review(
                    answer=replace(answer_input, ease=ease),
                    inputs_by_card_id=inputs_by_card_id,
                    snapshot=snapshot,
                )
            four_grade_ms += _elapsed_ms(four_grade_start)

        batched_four_grade_ms = 0.0
        batched_four_grade_scores: list[list[tuple[int, float]]] | None = []
        for _ in range(args.repeat):
            batched_four_grade_start = time.monotonic()
            batched_four_grade_scores = backend.predict_retrievability_after_reviews(
                answers=[replace(answer_input, ease=ease) for ease in (1, 2, 3, 4)],
                inputs_by_card_id=inputs_by_card_id,
                snapshot=snapshot,
            )
            batched_four_grade_ms += _elapsed_ms(batched_four_grade_start)

        card_info_four_grade_ms = 0.0
        card_info_four_grade_scores: list[list[tuple[int, float]]] | None = []
        for _ in range(args.repeat):
            card_info_four_grade_start = time.monotonic()
            card_info_four_grade_scores = backend.predict_retrievability_after_reviews(
                answers=[replace(answer_input, ease=ease) for ease in (1, 2, 3, 4)],
                inputs_by_card_id=card_info_inputs_by_card_id,
                snapshot=snapshot,
            )
            card_info_four_grade_ms += _elapsed_ms(card_info_four_grade_start)

        card_info_with_snapshot_ms = 0.0
        for _ in range(args.repeat):
            card_info_with_snapshot_start = time.monotonic()
            backend.predict_retrievability_after_reviews(
                answers=[replace(answer_input, ease=ease) for ease in (1, 2, 3, 4)],
                inputs_by_card_id=card_info_inputs_by_card_id,
                snapshot=backend.cache_snapshot(),
            )
            card_info_with_snapshot_ms += _elapsed_ms(card_info_with_snapshot_start)
    finally:
        set_reviewer_backend(previous_backend)

    native_scores = native_scores or []
    print(f"weights={args.weights}")
    print(f"collection={args.collection}")
    print(f"candidate_mode={args.candidate_mode}")
    print(f"requested_cards={len(card_ids)}")
    print(f"review_inputs={len(inputs_by_card_id)}")
    print(f"warmup_reviews={len(workload.warmup_reviews)}")
    print(f"repeat={args.repeat}")
    print(f"load_ms={load_ms:.3f}")
    print(f"workload_ms={workload_ms:.3f}")
    print(f"warmup_ms={warmup_ms:.3f}")
    print(f"baseline_after_answer_score_ms={baseline_ms:.3f}")
    print(f"native_after_answer_score_ms={native_ms:.3f}")
    print(f"four_grade_native_score_ms={four_grade_ms:.3f}")
    print(f"batched_four_grade_native_score_ms={batched_four_grade_ms:.3f}")
    print(f"card_info_four_grade_native_score_ms={card_info_four_grade_ms:.3f}")
    print(f"card_info_four_grade_with_snapshot_ms={card_info_with_snapshot_ms:.3f}")
    print(f"per_baseline_after_answer_score_ms={baseline_ms / args.repeat:.3f}")
    print(f"per_native_after_answer_score_ms={native_ms / args.repeat:.3f}")
    print(f"per_four_grade_native_score_ms={four_grade_ms / args.repeat:.3f}")
    print(
        "per_batched_four_grade_native_score_ms="
        f"{batched_four_grade_ms / args.repeat:.3f}"
    )
    print(
        "per_card_info_four_grade_native_score_ms="
        f"{card_info_four_grade_ms / args.repeat:.3f}"
    )
    print(
        "per_card_info_four_grade_with_snapshot_ms="
        f"{card_info_with_snapshot_ms / args.repeat:.3f}"
    )
    print(f"baseline_scores={len(baseline_scores)}")
    print(f"native_scores={len(native_scores)}")
    print(f"baseline_checksum={_score_checksum(baseline_scores):.9f}")
    print(f"native_checksum={_score_checksum(native_scores):.9f}")
    print(f"max_score_delta={_max_score_delta(baseline_scores, native_scores):.9f}")
    print(
        "batched_four_grade_scores="
        f"{sum(len(scores) for scores in batched_four_grade_scores or [])}"
    )
    print(
        "card_info_four_grade_scores="
        f"{sum(len(scores) for scores in card_info_four_grade_scores or [])}"
    )


def _score_input_batches(
    inputs_by_batch_size: dict[int, list[tuple[int, RwkvReviewInput]]],
) -> list[tuple[int, float]]:
    scores: list[tuple[int, float]] = []
    for batch_size, inputs_by_card_id in inputs_by_batch_size.items():
        batch_scores = _rwkv_review_scores_for_inputs(
            inputs_by_card_id,
            batch_size=batch_size,
        )
        if batch_scores is not None:
            scores.extend(batch_scores)
    return scores


def _input_build_inputs(
    input_build: RwkvReviewInputBatchBuild,
) -> list[tuple[int, RwkvReviewInput]]:
    return [
        item
        for inputs_by_card_id in input_build.inputs_by_batch_size.values()
        for item in inputs_by_card_id
    ]


def _inputs_by_batch_size(
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
    batch_size: int,
) -> dict[int, list[tuple[int, RwkvReviewInput]]]:
    return {batch_size: list(inputs_by_card_id)}


def _score_checksum(scores: Sequence[tuple[int, float]]) -> float:
    return sum(retrievability for _, retrievability in scores)


def _max_score_delta(
    left: Sequence[tuple[int, float]],
    right: Sequence[tuple[int, float]],
) -> float:
    right_by_card_id = dict(right)
    deltas = [
        abs(retrievability - right_by_card_id[card_id])
        for card_id, retrievability in left
        if card_id in right_by_card_id
    ]
    return max(deltas, default=float("nan"))


def _benchmark_reviewer(
    db: sqlite3.Connection,
    target_retention: float,
    enabled_deck_limit: int,
) -> SimpleNamespace:
    class DB:
        def all(self, sql: str, *args: object) -> list[tuple[object, ...]]:
            return list(db.execute(sql, args))

    class Decks:
        def __init__(self) -> None:
            self._deck_ids = _current_deck_ids(db)
            self._enabled_deck_ids = (
                set(self._deck_ids[:enabled_deck_limit])
                if enabled_deck_limit > 0
                else set(self._deck_ids)
            )

        def all_names_and_ids(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(id=deck_id) for deck_id in self._deck_ids]

        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            deck_config = _benchmark_deck_config(deck_id, target_retention)
            deck_config["rwkvReviewEnabled"] = deck_id in self._enabled_deck_ids
            return deck_config

    return SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=SimpleNamespace(),
                db=DB(),
                decks=Decks(),
            )
        )
    )


def _deck_input_cache_benchmark_reviewer(
    *,
    rows: Sequence[SimpleNamespace],
    searched_cards: int,
    deck_configs: int,
) -> SimpleNamespace:
    class Backend:
        def __init__(self) -> None:
            self.deck_row_calls = 0

        def rwkv_review_input_rows_for_deck_review_queue(
            self,
            *,
            deck_id: int,
            include_disabled_decks: bool,
            include_new_cards: bool,
        ) -> SimpleNamespace:
            del deck_id, include_disabled_decks, include_new_cards
            self.deck_row_calls += 1
            return SimpleNamespace(
                rows=rows,
                loaded_cards=len(rows),
                cards_with_supported_state=len(rows),
                disabled_config_cards=0,
                deck_configs=deck_configs,
                searched_cards=searched_cards,
            )

    class Scheduler:
        def _timing_today(self) -> SimpleNamespace:
            timing = _BenchTiming.today()
            return SimpleNamespace(
                days_elapsed=timing.days_elapsed,
                next_day_at=timing.next_day_at,
            )

    return SimpleNamespace(
        mw=SimpleNamespace(
            col=SimpleNamespace(
                _backend=Backend(),
                sched=Scheduler(),
            )
        )
    )


def _deck_queue_review_card_count(db: sqlite3.Connection) -> int:
    (count,) = db.execute(
        "select count() from cards where type = 2 and queue = 2"
    ).fetchone()
    return int(count)


def _deck_queue_input_rows(
    db: sqlite3.Connection,
    *,
    limit: int,
    target_retention: float,
    batch_size: int,
    timing: _BenchTiming,
) -> list[SimpleNamespace]:
    rows = cast(
        list[_DeckQueueInputSourceRow],
        list(
            db.execute(
                """
select
  c.id,
  c.nid,
  case when c.odid != 0 then c.odid else c.did end as current_did,
  c.type,
  c.queue,
  c.due,
  c.ivl,
  c.factor,
  c.reps,
  c.lapses,
  max(r.id) as last_review_id
from cards c
left join revlog r on r.cid = c.id
  and r.ease between 1 and 4
  and r.type in (0, 1, 2, 3, 4, 5)
  and not (r.type = 3 and r.factor = 0)
where c.type = 2
  and c.queue = 2
group by c.id
order by c.id
limit ?
""",
                (limit,),
            )
        ),
    )
    return [
        _deck_queue_input_row(
            row,
            target_retention=target_retention,
            batch_size=batch_size,
            timing=timing,
        )
        for row in rows
    ]


def _deck_queue_input_row(
    row: _DeckQueueInputSourceRow,
    *,
    target_retention: float,
    batch_size: int,
    timing: _BenchTiming,
) -> SimpleNamespace:
    (
        card_id,
        note_id,
        deck_id,
        card_type,
        card_queue,
        card_due,
        interval_days,
        ease_factor,
        reps,
        lapses,
        last_review_id,
    ) = row
    elapsed_seconds = (
        max(timing.now_secs - last_review_id // 1000, 0)
        if last_review_id is not None
        else None
    )
    elapsed_days = (
        elapsed_seconds // _SECONDS_PER_DAY if elapsed_seconds is not None else None
    )
    return SimpleNamespace(
        card_id=card_id,
        note_id=note_id,
        deck_id=deck_id,
        preset_id=str(deck_id),
        card_type=card_type,
        card_queue=card_queue,
        card_due=card_due,
        interval_days=interval_days,
        ease_factor=ease_factor,
        reps=reps,
        lapses=lapses,
        day_offset=timing.days_elapsed,
        current_state_kind="normal",
        current_normal_state_kind="review",
        current_elapsed_days=elapsed_days,
        current_elapsed_seconds=elapsed_seconds,
        target_retention=target_retention,
        batch_size=batch_size,
    )


def _input_build_input_count(input_build: RwkvReviewInputBatchBuild | None) -> int:
    if input_build is None:
        return 0
    return sum(
        len(inputs_by_card_id)
        for inputs_by_card_id in input_build.inputs_by_batch_size.values()
    )


def _current_deck_ids(db: sqlite3.Connection) -> list[int]:
    return [
        deck_id
        for (deck_id,) in db.execute(
            """
select distinct case when odid != 0 then odid else did end as current_did
from cards
order by current_did
"""
        )
    ]


def _supported_stats_cards(
    cards: Sequence[RwkvStatsGraphCard],
    timing: object,
) -> list[RwkvStatsGraphCard]:
    return [
        card
        for card in cards
        if _stats_graph_scheduling_states(
            card,
            timing,
            include_suspended_review=True,
        )
        is not None
    ]


def _wrapper_review_inputs(
    cards: Sequence[RwkvStatsGraphCard],
    timing: object,
    target_retention: float,
) -> list[RwkvReviewInput]:
    review_inputs = []
    for card in cards:
        deck_config = _benchmark_deck_config(card.current_deck_id(), target_retention)
        states = _stats_graph_scheduling_states(
            card,
            timing,
            include_suspended_review=True,
        )
        if states is None:
            continue
        candidate = RwkvReviewCandidate(
            reviewer=_stats_graph_reviewer_context(
                deck_config=deck_config,
                states=states,
                timing=timing,
            ),
            card=card,
        )
        identity = rwkv_review_identity(candidate.reviewer, candidate.card)
        if identity is None:
            continue
        review_inputs.append(
            rwkv_review_input(
                reviewer=candidate.reviewer,
                card=candidate.card,
                identity=identity,
                ease=None,
            )
        )
    return review_inputs


def _direct_review_inputs(
    cards: Sequence[RwkvStatsGraphCard],
    timing: object,
    target_retention: float,
) -> list[RwkvReviewInput]:
    review_inputs = []
    for card in cards:
        deck_config = _benchmark_deck_config(card.current_deck_id(), target_retention)
        review_input = _rwkv_review_input_for_stats_graph_card(
            card=card,
            deck_config=deck_config,
            timing=timing,
            include_suspended_review=True,
            state_fields=_rwkv_state_fields_for_stats_graph_card(
                card,
                timing,
                include_suspended_review=True,
            ),
        )
        if review_input is not None:
            review_inputs.append(review_input)
    return review_inputs


def _benchmark_deck_config(deck_id: int, target_retention: float) -> dict[str, object]:
    return {
        "id": deck_id * 10,
        "rwkvReviewEnabled": True,
        "desiredRetention": target_retention,
    }


def _stats_card_load_sql(card_ids: Sequence[int], *, sql_filter: bool) -> str:
    filter_sql = (
        """
  and (
    (type = 2 and queue in (2, -1))
    or (type = 1 and queue in (1, 3))
    or (type = 3 and queue in (1, 3))
  )
"""
        if sql_filter
        else ""
    )
    return f"""
select id, nid, did, odid, type, queue, due, odue, ivl, factor, reps, lapses, data
from cards
where id in ({",".join(str(card_id) for card_id in card_ids)})
{filter_sql}
"""


def _latest_review_times(
    db: sqlite3.Connection,
    card_ids: Sequence[int],
) -> dict[int, int]:
    if not card_ids:
        return {}

    return {
        card_id: max(0, review_id // 1000)
        for card_id, review_id in db.execute(
            f"""
select cid, max(id)
from revlog
where cid in ({",".join(str(card_id) for card_id in card_ids)})
  and ease between 1 and 4
  and type in (0, 1, 2, 3, 4, 5)
  and not (type = 3 and factor = 0)
group by cid
"""
        )
    }


@dataclass(frozen=True)
class _BenchTiming:
    now_secs: int
    days_elapsed: int
    next_day_at: int

    @classmethod
    def today(cls) -> _BenchTiming:
        now_secs = int(time.time())
        days_elapsed = now_secs // _SECONDS_PER_DAY
        return cls(
            now_secs=now_secs,
            days_elapsed=days_elapsed,
            next_day_at=(days_elapsed + 1) * _SECONDS_PER_DAY,
        )


@dataclass(frozen=True)
class _CollectionWorkload:
    warmup_reviews: list[RwkvReviewInput]
    query_inputs: list[RwkvReviewInput]

    @classmethod
    def load(
        cls,
        path: Path,
        warmup_limit: int,
        query_limit: int,
        target_retention: float,
    ) -> _CollectionWorkload:
        uri = f"file:{path}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as db:
            timing = _BenchTiming.today()
            return cls(
                warmup_reviews=_collection_warmup_reviews(
                    db,
                    warmup_limit,
                    timing,
                ),
                query_inputs=_collection_query_inputs(
                    db,
                    query_limit,
                    target_retention,
                    timing,
                ),
            )


def _collection_warmup_reviews(
    db: sqlite3.Connection,
    limit: int,
    timing: _BenchTiming,
) -> list[RwkvReviewInput]:
    limit_clause = "" if limit == 0 else f" limit {limit}"
    rows = db.execute(
        f"""
with eligible as (
  select
    r.id,
    r.cid,
    c.nid,
    c.did,
    r.ease,
    r.time,
    r.type,
    lag(r.type) over (partition by r.cid order by r.id) as previous_type
  from revlog r
  join cards c on c.id = r.cid
  where r.ease between 1 and 4
    and r.type in (0, 1, 2, 3, 4, 5)
    and not (r.type = 3 and r.factor = 0)
), retained_starts as (
  select cid, max(id) as start_id
  from eligible
  where type = 0 and (previous_type is null or previous_type != 0)
  group by cid
)
select
  e.id,
  e.cid,
  e.nid,
  e.did,
  e.ease,
  e.time,
  e.type,
  e.id = s.start_id
from eligible e
join retained_starts s on s.cid = e.cid
where e.id >= s.start_id
order by e.id, e.cid
{limit_clause}
"""
    )

    previous_review_id_by_card: dict[int, int] = {}
    reviews = []
    for (
        review_id,
        card_id,
        note_id,
        deck_id,
        ease,
        duration_millis,
        review_kind,
        is_learning_start,
    ) in rows:
        previous_review_id = previous_review_id_by_card.get(card_id)
        previous_review_id_by_card[card_id] = review_id
        day_offset = _historical_day_offset(review_id, timing)
        elapsed_seconds = (
            max((review_id - previous_review_id) // 1000, 0)
            if previous_review_id is not None
            else -1
        )
        elapsed_days = (
            max(day_offset - _historical_day_offset(previous_review_id, timing), 0)
            if previous_review_id is not None
            else -1
        )
        reviews.append(
            _review_input(
                card_id=card_id,
                note_id=note_id,
                deck_id=deck_id,
                is_query=False,
                ease=ease,
                duration_millis=duration_millis,
                card_type=_historical_state(
                    review_kind,
                    is_learning_start=bool(is_learning_start),
                ),
                day_offset=day_offset,
                current_elapsed_days=elapsed_days,
                current_elapsed_seconds=elapsed_seconds,
                target_retention=0.9,
            )
        )

    return reviews


def _collection_query_inputs(
    db: sqlite3.Connection,
    limit: int,
    target_retention: float,
    timing: _BenchTiming,
) -> list[RwkvReviewInput]:
    rows = db.execute(
        f"""
select
  c.id,
  c.nid,
  case when c.odid != 0 then c.odid else c.did end as current_did,
  max(r.id) as last_review_id
from cards c
left join revlog r on r.cid = c.id
  and r.ease between 1 and 4
  and r.type in (0, 1, 2, 3, 4, 5)
  and not (r.type = 3 and r.factor = 0)
where c.type = 2
  and c.queue = 2
group by c.id
order by c.id
limit {limit}
"""
    )

    query_inputs = []
    for card_id, note_id, deck_id, last_review_id in rows:
        elapsed_seconds = (
            max(timing.now_secs - last_review_id // 1000, 0)
            if last_review_id is not None
            else -1
        )
        elapsed_days = (
            elapsed_seconds // _SECONDS_PER_DAY if elapsed_seconds >= 0 else -1
        )
        query_inputs.append(
            _review_input(
                card_id=card_id,
                note_id=note_id,
                deck_id=deck_id,
                is_query=True,
                ease=None,
                duration_millis=None,
                card_type=2,
                day_offset=timing.days_elapsed,
                current_elapsed_days=elapsed_days,
                current_elapsed_seconds=elapsed_seconds,
                target_retention=target_retention,
            )
        )

    return query_inputs


def _review_input(
    *,
    card_id: int,
    note_id: int,
    deck_id: int,
    is_query: bool,
    ease: int | None,
    duration_millis: int | None,
    card_type: int,
    day_offset: int,
    current_elapsed_days: int,
    current_elapsed_seconds: int,
    target_retention: float,
) -> RwkvReviewInput:
    return RwkvReviewInput(
        identity=RwkvReviewIdentity(
            card_id=card_id,
            note_id=note_id,
            deck_id=deck_id,
            preset_id=deck_id,
        ),
        is_query=is_query,
        ease=ease,
        duration_millis=duration_millis,
        card_type=card_type,
        card_queue=None,
        card_due=None,
        interval_days=None,
        ease_factor=None,
        reps=None,
        lapses=None,
        day_offset=day_offset,
        current_state_kind=None,
        current_normal_state_kind=None,
        current_elapsed_days=current_elapsed_days,
        current_elapsed_seconds=current_elapsed_seconds,
        target_retentions=(
            target_retention,
            target_retention,
            target_retention,
            target_retention,
        ),
    )


def _prediction_requests(
    inputs: Sequence[RwkvReviewInput],
    snapshot: RwkvBackendCacheSnapshot,
) -> list[RwkvReviewPredictionRequest]:
    return [
        RwkvReviewPredictionRequest(
            review_input=review_input,
            card_state=snapshot.card_states.get(review_input.identity.card_id),
            note_state=snapshot.note_states.get(review_input.identity.note_id),
            deck_state=snapshot.deck_states.get(review_input.identity.deck_id),
            preset_state=snapshot.preset_states.get(review_input.identity.preset_id),
            global_state=snapshot.global_state,
        )
        for review_input in inputs
    ]


def _warm_up_snapshot(
    runtime: _RustRwkvRuntime,
    reviews: Sequence[RwkvReviewInput],
) -> RwkvBackendCacheSnapshot:
    runtime._process.warm_up_reviews(
        [_review_input_row(review_input) for review_input in reviews],
        False,
    )
    (
        card_states,
        note_states,
        deck_states,
        preset_states,
        global_state,
        runtime_state,
    ) = runtime._process.warm_up_snapshot()
    return RwkvBackendCacheSnapshot(
        card_states=dict(card_states),
        note_states=dict(note_states),
        deck_states=dict(deck_states),
        preset_states=dict(preset_states),
        global_state=global_state,
        runtime_state=runtime_state,
    )


def _historical_state(review_kind: int, *, is_learning_start: bool = False) -> int:
    return 0 if is_learning_start else review_kind + 1


def _historical_day_offset(review_id: int, timing: _BenchTiming) -> int:
    review_secs = review_id // 1000
    days_before_today = max(timing.next_day_at - 1 - review_secs, 0) // _SECONDS_PER_DAY
    return max(timing.days_elapsed - days_before_today, 0)


if __name__ == "__main__":
    main()
