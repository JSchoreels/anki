# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Measure review overhead using canonical history from an explicit collection copy."""

from __future__ import annotations

import argparse
import cProfile
import gc
import hashlib
import importlib
import json
import logging
import pstats
import statistics
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypeVar, cast
from unittest.mock import patch

from anki.collection import Collection
from aqt import rwkv_scheduler as scheduler
from aqt.rwkv_srs_benchmark import (
    _packed_warm_up_reviews,
    _review_input_row,
    _RustRwkvRuntime,
)

if TYPE_CHECKING:
    from anki.cards import CardId

_T = TypeVar("_T")


def _timed(call: Callable[[], _T]) -> tuple[_T, float]:
    start = time.perf_counter()
    result = call()
    return result, (time.perf_counter() - start) * 1000


def _benchmark_dynamic_dr(
    args: argparse.Namespace,
    reviewer: object,
    inputs: list[tuple[int, scheduler.RwkvReviewInput]],
) -> tuple[dict[str, list[float]], str]:
    """Load an explicitly supplied provider copy without registering GUI hooks."""
    sys.path.insert(0, str(args.dynamic_dr_addon.resolve()))
    api = importlib.import_module("dynamic_desired_retention.api")
    addon_module = importlib.import_module("dynamic_desired_retention.addon")
    config_module = importlib.import_module("dynamic_desired_retention.config")
    config = json.loads(args.dynamic_dr_config.read_text())
    logger = logging.getLogger("rwkv_review_performance_bench")
    provider = addon_module.DynamicDesiredRetentionAddon(
        module="benchmark", logger=logger
    )
    provider._rules = config_module.load_rules(config, logger)
    provider._field_rules = config_module.load_field_rules(config, logger)
    provider._grade_rules = config_module.load_grade_rules(config, logger)
    provider._matcher = addon_module.DesiredRetentionMatcher(
        provider._rules, provider._field_rules, logger, provider._grade_rules
    )
    api.set_desired_retention_provider(provider)
    samples: dict[str, list[float]] = {"dynamic_dr_cold": [], "dynamic_dr_warm": []}
    try:
        for _ in range(args.rounds):
            provider._matcher.clear_cache()
            cold, elapsed = _timed(
                lambda: scheduler._resolve_dynamic_desired_retentions_for_inputs(
                    reviewer, inputs
                )
            )
            samples["dynamic_dr_cold"].append(elapsed)
            warm, elapsed = _timed(
                lambda: scheduler._resolve_dynamic_desired_retentions_for_inputs(
                    reviewer, inputs
                )
            )
            samples["dynamic_dr_warm"].append(elapsed)
            if cold != warm:
                raise ValueError(
                    "Dynamic DR targets changed between cold and warm runs"
                )
        if args.profile_dynamic_dr:
            provider._matcher.clear_cache()
            profiler = cProfile.Profile()
            profiler.runcall(
                scheduler._resolve_dynamic_desired_retentions_for_inputs,
                reviewer,
                inputs,
            )
            with args.profile_dynamic_dr.open("w") as output:
                pstats.Stats(profiler, stream=output).strip_dirs().sort_stats(
                    "cumulative"
                ).print_stats(40)
    finally:
        api.set_desired_retention_provider(None)
        sys.path.pop(0)
    targets = [
        (card_id, review_input.target_retentions) for card_id, review_input in cold
    ]
    target_hash = hashlib.sha256(json.dumps(targets).encode()).hexdigest()
    return samples, target_hash


def benchmark(args: argparse.Namespace) -> dict[str, object]:
    col = Collection(str(args.collection_copy))
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    samples: dict[str, list[float]] = {}
    dynamic_dr_target_hash = None

    def measure(name: str, call: Callable[[], _T]) -> _T:
        result, elapsed = _timed(call)
        samples.setdefault(name, []).append(elapsed)
        return result

    try:
        print("Building canonical history...", flush=True)
        history = measure(
            "history_build",
            lambda: scheduler._historical_rwkv_review_inputs(
                reviewer, prepare_recovery_checkpoint=args.compare_history
            ),
        )
        identity = scheduler._RwkvHistoryPrefixIdentity(
            history.last_review_id, history.review_count, history.history_hash
        )
        for _ in range(args.history_rounds):
            fingerprint = measure(
                "history_fingerprint",
                lambda: scheduler._rwkv_historical_review_fingerprint(
                    reviewer, expected_identity=identity
                ),
            )
            if fingerprint is None or fingerprint.identity != identity:
                raise ValueError("Native and Python history identities differ")
            if not fingerprint.history_is_valid:
                raise ValueError("Native history prefix validation failed")
        for _ in range(args.history_rounds - 1):
            history = measure(
                "history_build",
                lambda: scheduler._historical_rwkv_review_inputs(
                    reviewer, prepare_recovery_checkpoint=args.compare_history
                ),
            )
            if history.history_hash != identity.history_hash:
                raise ValueError("Python history identity changed between rounds")

        if args.compare_history:
            for _ in range(args.history_rounds):
                # Explicit prefix maps select Python's existing replay path.
                # Empty maps still rebuild the full history for a like-for-like
                # comparison with the default native path.
                python_history = measure(
                    "python_history_build",
                    lambda: scheduler._historical_rwkv_review_inputs(
                        reviewer,
                        previous_review_id_by_card={},
                        previous_interval_days_by_card={},
                        review_count_by_card={},
                        prepare_recovery_checkpoint=True,
                    ),
                )
                if python_history != history:
                    raise ValueError(
                        "Native and Python replay inputs or metadata differ"
                    )
                # These maps are excluded from the dataclass's normal equality.
                if (
                    python_history.prepared_checkpoint_histories
                    != history.prepared_checkpoint_histories
                ):
                    raise ValueError("Native and Python recovery checkpoints differ")
            del python_history

        if args.profile_history:
            profiler = cProfile.Profile()
            profiler.runcall(scheduler._historical_rwkv_review_inputs, reviewer)
            with args.profile_history.open("w") as output:
                pstats.Stats(profiler, stream=output).strip_dirs().sort_stats(
                    "cumulative"
                ).print_stats(45)

        input_build = measure(
            "deck_input_build",
            lambda: scheduler._rwkv_review_input_batches_for_deck_review_queue(
                reviewer=reviewer,
                deck_id=args.deck_id,
                batch_size_override=args.queries,
                include_new_cards=False,
            ),
        )
        if input_build is None:
            raise ValueError("No backend review inputs available for the selected deck")
        inputs = scheduler._rwkv_review_input_build_inputs(input_build)[: args.queries]
        if not inputs:
            raise ValueError("Selected deck has no enabled RWKV review inputs")
        prediction_reference = None
        if args.prediction_reference:
            prediction_reference = json.loads(args.prediction_reference.read_text())
            if prediction_reference["history_hash"] != identity.history_hash:
                raise ValueError("Prediction reference uses different review history")
            inputs = [
                (
                    card_id,
                    scheduler.RwkvReviewInput(
                        **{
                            **row,
                            "identity": scheduler.RwkvReviewIdentity(**row["identity"]),
                            "target_retentions": tuple(row["target_retentions"]),
                        }
                    ),
                )
                for card_id, row in prediction_reference["inputs"]
            ]
        queries = [review_input for _, review_input in inputs]

        # This isolates the card-loading portion of Dynamic DR preparation. No
        # add-on resolver is installed here, so it does not time provider rules.
        for _ in range(args.rounds):
            measure(
                "dynamic_dr_card_load",
                lambda: [col.get_card(cast("CardId", cid)) for cid, _ in inputs],
            )
        if args.dynamic_dr_addon:
            print("Comparing cold and warm Dynamic DR provider calls...", flush=True)
            provider_samples, dynamic_dr_target_hash = _benchmark_dynamic_dr(
                args, reviewer, inputs
            )
            samples.update(provider_samples)

        print(
            f"Warming {history.review_count} reviews; queries={len(queries)}...",
            flush=True,
        )
        runtime = _RustRwkvRuntime(
            model_path=args.weights, target_retention=0.9, max_interval_days=36_500
        )
        measure(
            "resident_warmup", lambda: runtime.warm_up_reviews_in_place(history.reviews)
        )
        review_count = history.review_count
        checkpoint_prefix_counts = sorted(history.prepared_checkpoint_histories)
        del history
        gc.collect()

        def predict(mode: str, *, record: bool) -> list[float]:
            def run(name: str, call: Callable[[], _T]) -> _T:
                return measure(f"{mode}_{name}", call) if record else call()

            started = time.perf_counter()
            if mode == "tuple":
                rows = run(
                    "payload", lambda: [_review_input_row(query) for query in queries]
                )
                values = run(
                    "bridge",
                    lambda: runtime._process.predict_retrievability_many_from_warm_up(
                        rows
                    ),
                )
            else:
                payload = run("payload", lambda: _packed_warm_up_reviews(queries))
                packed = run(
                    "bridge",
                    lambda: (
                        runtime._process.predict_retrievability_many_from_warm_up_packed(
                            payload
                        )
                    ),
                )
                values = run(
                    "decode", lambda: list(struct.unpack(f"<{len(queries)}f", packed))
                )
            if record:
                samples.setdefault(f"{mode}_total", []).append(
                    (time.perf_counter() - started) * 1000
                )
            return values

        reference = predict("tuple", record=False)
        reference_max_delta = None
        reference_rank_changes = None
        if prediction_reference is not None:
            expected = prediction_reference["predictions"]
            reference_max_delta = max(
                abs(left - right)
                for left, right in zip(expected, reference, strict=True)
            )
            if reference_max_delta > 1e-6:
                raise ValueError(
                    f"Prediction reference delta exceeds 1e-6: {reference_max_delta}"
                )
            old_order = sorted(range(len(expected)), key=lambda i: (expected[i], i))
            new_order = sorted(range(len(reference)), key=lambda i: (reference[i], i))
            reference_rank_changes = sum(a != b for a, b in zip(old_order, new_order))
        if args.prediction_output:
            args.prediction_output.write_text(
                json.dumps(
                    {
                        "history_hash": identity.history_hash,
                        "inputs": [(card_id, asdict(row)) for card_id, row in inputs],
                        "predictions": reference,
                    }
                )
            )
        if predict("packed", record=False) != reference:
            raise ValueError("Packed and tuple predictions differ")
        print("Comparing bridges in alternating order...", flush=True)
        for round_index in range(args.rounds):
            for mode in (
                ("tuple", "packed") if round_index % 2 == 0 else ("packed", "tuple")
            ):
                if predict(mode, record=True) != reference:
                    raise ValueError(f"Prediction parity changed: {mode}")

        backend = scheduler.RwkvStatefulReviewerBackend(runtime)
        indexed_inputs = list(enumerate(queries))
        for _ in range(args.rounds):
            entries = measure(
                "prediction_objects",
                lambda: [
                    (query, scheduler.RwkvReviewPrediction(retrievability=value))
                    for query, value in zip(queries, reference, strict=True)
                ],
            )
            measure(
                "memo_miss_scan",
                lambda: [backend._cached_prediction(query) for query in queries],
            )
            measure(
                "memo_install", lambda: backend.cache_review_input_predictions(entries)
            )
            hits = measure(
                "memo_hit_scan",
                lambda: [
                    backend._cached_prediction(query) for _, query in indexed_inputs
                ],
            )
            if not all(hit for hit, _ in hits):
                raise ValueError("Prediction memo did not retain all inputs")
            # At the next answer, the async result no longer owns predictions.
            # Include their destruction in the clear measurement.
            del entries, hits
            measure("memo_clear", lambda: backend._clear_prediction_cache("benchmark"))

        return {
            "collection_copy": str(args.collection_copy.resolve()),
            "reviews": review_count,
            "queries": len(queries),
            "rounds": args.rounds,
            "prediction_parity": "exact",
            "prediction_reference_max_abs_delta": reference_max_delta,
            "prediction_reference_rank_changes": reference_rank_changes,
            "history_identity_parity": "exact",
            "history_inputs_parity": "exact" if args.compare_history else None,
            "history_checkpoint_prefix_counts": checkpoint_prefix_counts,
            "history_hash": identity.history_hash,
            "dynamic_dr_provider_loaded": args.dynamic_dr_addon is not None,
            "dynamic_dr_target_hash": dynamic_dr_target_hash,
            "uncached_field_maps": args.uncached_field_maps,
            "milliseconds": {
                name: {"median": statistics.median(values), "samples": values}
                for name, values in samples.items()
            },
        }
    finally:
        col.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-copy", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--deck-id", type=int, required=True)
    parser.add_argument("--queries", type=int, default=8803)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--history-rounds", type=int, default=3)
    parser.add_argument("--profile-history", type=Path)
    parser.add_argument(
        "--compare-history",
        action="store_true",
        help="Compare replay inputs, metadata, and recovery checkpoints with the Python builder.",
    )
    parser.add_argument("--dynamic-dr-addon", type=Path)
    parser.add_argument("--dynamic-dr-config", type=Path)
    parser.add_argument("--profile-dynamic-dr", type=Path)
    parser.add_argument(
        "--uncached-field-maps",
        action="store_true",
        help="Use the original field-map implementation as a comparison baseline.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prediction-output", type=Path)
    parser.add_argument("--prediction-reference", type=Path)
    args = parser.parse_args()
    if min(args.queries, args.rounds, args.history_rounds) < 1:
        parser.error("queries and round counts must be positive")
    if not args.collection_copy.is_file():
        parser.error("collection-copy must be an existing extracted collection copy")
    if bool(args.dynamic_dr_addon) != bool(args.dynamic_dr_config):
        parser.error("dynamic-dr-addon and dynamic-dr-config must be supplied together")
    if args.uncached_field_maps:
        with patch(
            "anki.models.ModelManager.field_map",
            lambda self, notetype: {f["name"]: (f["ord"], f) for f in notetype["flds"]},
        ):
            result = benchmark(args)
    else:
        result = benchmark(args)
    report = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
