# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import argparse
import gc
import json
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar

from anki.collection import Collection
from aqt import rwkv_scheduler

_T = TypeVar("_T")


def _timed(call: Callable[[], _T]) -> tuple[_T, float]:
    started = time.perf_counter()
    result = call()
    return result, (time.perf_counter() - started) * 1000


def benchmark(collection_copy: Path, rounds: int) -> dict[str, object]:
    if not collection_copy.is_file():
        raise FileNotFoundError(collection_copy)
    if rounds < 1:
        raise ValueError("rounds must be positive")

    col = Collection(str(collection_copy))
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    try:
        # Warm caches and prove that both implementations agree before timing.
        python_history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
        expected_identity = rwkv_scheduler._RwkvHistoryPrefixIdentity(
            last_review_id=python_history.last_review_id,
            review_count=python_history.review_count,
            history_hash=python_history.history_hash,
        )
        rust_fingerprint = rwkv_scheduler._rwkv_historical_review_fingerprint(
            reviewer,
            expected_identity=expected_identity,
        )
        if rust_fingerprint is None:
            raise RuntimeError("Rust RWKV history fingerprint is unavailable")
        python_identity = (
            python_history.last_review_id,
            python_history.review_count,
            python_history.history_hash,
        )
        rust_identity = (
            rust_fingerprint.identity.last_review_id,
            rust_fingerprint.identity.review_count,
            rust_fingerprint.identity.history_hash,
        )
        if python_identity != rust_identity or not rust_fingerprint.history_is_valid:
            dynamic_preset_replay = (
                rwkv_scheduler._rwkv_dynamic_preset_replay_enabled_for_collection(
                    reviewer
                )
            )
            raise RuntimeError(
                "Rust/Python RWKV history identity mismatch: "
                f"python={python_identity} rust={rust_identity} "
                f"dynamicPresetReplay={dynamic_preset_replay}"
            )
        del python_history
        gc.collect()

        python_timings = []
        rust_timings = []
        for _round in range(rounds):
            python_history, elapsed_ms = _timed(
                lambda: rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
            )
            python_timings.append(elapsed_ms)
            del python_history
            gc.collect()

            fingerprint, elapsed_ms = _timed(
                lambda: rwkv_scheduler._rwkv_historical_review_fingerprint(
                    reviewer,
                    expected_identity=expected_identity,
                )
            )
            if fingerprint is None:
                raise RuntimeError("Rust RWKV history fingerprint became unavailable")
            rust_timings.append(elapsed_ms)

        python_median = statistics.median(python_timings)
        rust_median = statistics.median(rust_timings)
        return {
            "collectionCopy": str(collection_copy.resolve()),
            "reviews": rust_fingerprint.identity.review_count,
            "queriedReviews": rust_fingerprint.queried_review_count,
            "rounds": rounds,
            "pythonMs": python_timings,
            "rustMs": rust_timings,
            "pythonMedianMs": python_median,
            "rustMedianMs": rust_median,
            "speedup": python_median / rust_median,
        }
    finally:
        col.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare Python and Rust RWKV history fingerprints on an explicit "
            "collection copy. Never pass a live profile database."
        )
    )
    parser.add_argument("--collection-copy", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.collection_copy, args.rounds), indent=2))


if __name__ == "__main__":
    main()
