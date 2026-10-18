# RWKV INT4 cards / INT8 notes comparison — 2026-10-18

Keeping note state in INT8 while reducing card state to INT4 saves **24.29% of serialized recurrent-state payload** on this collection. It increases historical log loss by **0.00020086 (+0.0441%)** versus INT8 cards and notes, and loses their direct paired UM+ comparison. Both RWKV variants outperform the collection's frozen configured FSRS7 parameters on these matched reviews. Keep INT8 as the default; the hybrid is a plausible experimental storage tradeoff.

This is a benchmark-only experiment. The desktop precision options, cache semantics, and running application are unchanged. Serialized payload size is not process RSS or an actual SQLite file size.

## Data and evaluation

- Source: a temporary copy of `backup-2026-10-18-15.21.28.colpkg` from Main Profile's existing backups. The live collection was never opened or queried.
- Replay: **223,972 canonical retained answers**, **13,977 cards**, **13,202 notes**, **22 decks**, and **13 resolved presets**.
- Score: **176,849 identical interday reviews** for all three models, across the entire collection. Each scored review has a previous retained answer and at least one scheduler day of elapsed time. First answers and same-day answers update state but are excluded from scoring. Observed recall is **77.2201%**.
- RWKV: the bundled `RWKV_trained_on_5000_10000.bin`, with identical f32 weights, computation, and deck/preset/global state. Both variants retain f16 shift vectors. Only card recurrent matrices change from INT8 to INT4; note matrices remain INT8.
- FSRS7: all 13 resolved presets use version Seven and 34 parameters. Freeze the parameters already saved in the backup; do not optimize or refit them. Verify that each replay row's preset matches the card's current resolved preset. Reconstruct each card's retained prefix with that preset's parameters and use the exact millisecond timestamp difference divided by 86,400,000, as in Anki's FSRS7 training adapter. The recorded ignore-before dates are empty or 1970-01-01.
- Every prediction consumes only preceding answers. The target answer is applied afterward. FSRS7's first-answer placeholder is never scored.
- Natural-log loss clips probabilities to `[1e-6, 1 - 1e-6]`. UM+ uses the existing 41 disagreement buckets, JavaScript-compatible rounding, and every populated bucket, including small ones. Lower is better for both metrics.

This is a retrospective comparison with existing parameters on one collection, not a held-out-user study or a new cross-validated FSRS fit. It compares the models on the same canonical retained histories rather than separately applying different model-specific history-selection rules.

## Log loss

| Model | Log loss | Difference from RWKV INT8 |
| --- | ---: | ---: |
| RWKV INT8 cards + INT8 notes | 0.45581648 | — |
| RWKV INT4 cards + INT8 notes | 0.45601734 | +0.00020086 |
| Configured FSRS7, frozen parameters | 0.47222674 | +0.01641025 |

The hybrid's log loss is **3.4325% lower than configured FSRS7** on this sample. That difference is specific to these frozen model parameters and these retained histories.

## Paired UM+

UM+ is a pairwise metric: each row below builds disagreement buckets from that particular pair. Scores from different opponent sets are not interchangeable.

| Pair | First model UM+ | Second model UM+ | Lower score |
| --- | ---: | ---: | --- |
| RWKV INT8 / hybrid | 0.00849397 | 0.01000967 | RWKV INT8 |
| RWKV INT8 / FSRS7 | 0.01383849 | 0.07398266 | RWKV INT8 |
| Hybrid / FSRS7 | 0.01425742 | 0.07406177 | Hybrid |

## Individual predictions and state size

Relative to INT8, hybrid predictions change by **0.3957 percentage points on average**, **2.3464 pp at p99**, and **10.7321 pp at maximum**. **63 of 176,849** scored predictions, or **0.03562%**, move by more than five percentage points. The small aggregate loss change does not bound individual changes.

| RWKV mode | Serialized state payload | Difference |
| --- | ---: | ---: |
| INT8 cards + notes | 353,494,289 bytes | — |
| INT4 cards + INT8 notes | 267,619,601 bytes | −85,874,688 bytes (−24.2931%) |

The note payload is identical in size in both variants: 135,756,166 bytes. Card payload falls from 215,455,455 to 129,580,767 bytes. Shared scopes are unchanged. Both complete replays produce finite probabilities; this does not establish stability on every collection or on Pedro's longer history. No process-memory or user-perceived speed claim is made.

## Reproduction and verification

The [aggregate JSON](rwkv-hybrid-precision-2026-10-18.json) records exact metrics, paired bins, input/model/executable hashes, source provenance, and prediction hashes. Private review rows, saved parameter vectors, predictions, the copied backup, and source archives remain under `/private/tmp/rwkv-hybrid-2026-10-18/` and are not committed.

```sh
just rwkv-history-ablation-prepare --backup-copy /private/tmp/rwkv-hybrid-2026-10-18/backup.colpkg --output /private/tmp/rwkv-hybrid-2026-10-18/input.json --deck-match=
just rwkv-hybrid-precision-tests --release
just test-py-target qt/tests/test_rwkv_hybrid_precision_bench.py qt/tests/test_rwkv_um_plus.py
just rwkv-hybrid-precision-bench /private/tmp/rwkv-hybrid-2026-10-18/input.json /private/tmp/rwkv-hybrid-2026-10-18/verified
just check
```

Use a fresh output directory for each comparison. The preparation command requires an already-copied backup. The comparison only opens the extracted temporary collection through Anki's Collection API; it performs no raw SQLite writes. It rejects a live-profile collection path, non-FSRS7 presets, mismatched preset routing, incomplete predictions, non-finite predictions, and source/input/model changes during measurement.

Verification: hybrid sequential/chunked state parity passed; five Python alignment and UM+ tests passed; two complete real-history comparisons produced byte-identical predictions for all three models and identical final RWKV state hashes. `just check` passed with normal access to Cargo and uv caches after an initial sandboxed run could not access those caches. The pinned FSRS source is `fsrs-rs` revision `0a5737406a0c5c7a5259b144738cbe4a0b649d76`; its inference source matches the pinned checkout.
