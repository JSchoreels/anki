# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Time the desktop's Python work around a full RWKV state build.

The native harness (`rslib/src/rwkv/app_speed_bench.rs`) times the Rust
side. Between the native history preparation and the native replay the
desktop also parses the history response, turns every row into a
`RwkvReviewInput` (`aqt.rwkv_history.full_review_inputs`), packs each
16,384-review chunk (`_packed_warm_up_reviews`) and encodes the checkpoint
history maps. This runs exactly those functions on the response bytes the
harness wrote with `ANKI_SPEED_DUMP_HISTORY`, without Qt, a collection or
the native bridge.

    python -I qt/tools/rwkv_speed_python_overhead.py HISTORY.bin [--repeat 5]

Run it with a Python environment that can import `aqt` (the build's
`out/pyenv`) and with `pylib`, `out/pylib`, `qt` and `out/qt` on
`--path`. Prints one JSON line per repeat.
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("history", type=Path)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--path", action="append", default=[])
    args = parser.parse_args()
    sys.path[:0] = args.path

    import_started = time.perf_counter()
    from anki import scheduler_pb2
    from aqt import rwkv_history
    from aqt import rwkv_scheduler as scheduler
    from aqt.rwkv_srs_benchmark import (
        _RUST_STATE_ONLY_WARMUP_CHUNK_SIZE,
        _packed_warm_up_reviews,
    )

    import_ms = (time.perf_counter() - import_started) * 1000
    data = args.history.read_bytes()
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=None))

    for repeat in range(args.repeat):
        gc.collect()
        started = time.perf_counter()
        parse_ms = 0.0

        def build(request: object) -> scheduler_pb2.RwkvHistoricalReviewInputsResponse:
            nonlocal parse_ms
            del request
            parsed_at = time.perf_counter()
            response = scheduler_pb2.RwkvHistoricalReviewInputsResponse()
            response.ParseFromString(data)
            parse_ms = (time.perf_counter() - parsed_at) * 1000
            return response

        history = rwkv_history.full_review_inputs(
            reviewer,
            build,
            replay_key="speed-test",
            first_review_elapsed_source=scheduler.RwkvFirstReviewElapsedSource.DECK_CONFIG,
            ignored_review_ids=frozenset(),
            prepare_recovery_checkpoint=True,
            progress=None,
        )
        assert history is not None
        convert_ms = (time.perf_counter() - started) * 1000 - parse_ms

        packed_at = time.perf_counter()
        packed_bytes = 0
        reviews = history.reviews
        for offset in range(0, len(reviews), _RUST_STATE_ONLY_WARMUP_CHUNK_SIZE):
            chunk = reviews[offset : offset + _RUST_STATE_ONLY_WARMUP_CHUNK_SIZE]
            packed_bytes += len(_packed_warm_up_reviews(chunk))
        pack_ms = (time.perf_counter() - packed_at) * 1000

        maps_at = time.perf_counter()
        for prefix in [*history.prepared_checkpoint_histories.values(), history]:
            scheduler._encode_rwkv_state_cache_history_maps(prefix)
        maps_ms = (time.perf_counter() - maps_at) * 1000

        print(
            json.dumps(
                {
                    "op": "python-overhead",
                    "repeat": repeat,
                    "reviews": len(reviews),
                    "import_ms": round(import_ms, 1),
                    "parse_ms": round(parse_ms, 1),
                    "convert_ms": round(convert_ms, 1),
                    "pack_ms": round(pack_ms, 1),
                    "maps_ms": round(maps_ms, 1),
                    "total_ms": round(parse_ms + convert_ms + pack_ms + maps_ms, 1),
                    "packed_bytes": packed_bytes,
                }
            ),
            flush=True,
        )
        del history, reviews


if __name__ == "__main__":
    main()
