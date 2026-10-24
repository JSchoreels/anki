# RWKV Shared-State Int8 POC — 2026-10-14

Extending the current int8 format to deck, preset, and global state saves only **1.61 MB (0.46%)** of the final recurrent-state payload on this collection, while increasing log loss by **2.28%** and calibration RMSE by **160.48%** relative to card/note-only int8. Keep the shared scopes in f32 for this model. This offline POC does not change desktop precision defaults or profile caches.

## Additional Savings And Accuracy

All compact scopes use the existing format: row-scaled int8 recurrent matrices, f16 time/channel shift vectors, and f32 arithmetic. Weights, activations, feature context, and recall curves remain f32. Each selected scope is rounded after every answer, so future predictions consume the rounded state.

| Compact scopes | Final state payload | Extra saving vs current int8 | Log loss | Log-loss increase vs current int8 | RMSE(bins) | RMSE increase vs current int8 | Largest prediction change vs f32 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| None (f32 reference) | 1,192.819 MB | — | 0.45583339 | — | 0.01880904 | — | 0.000 pp |
| Card/note (current int8) | 353.391 MB | — | 0.45583223 | — | 0.01881054 | — | 0.680 pp |
| Card/note + deck | 352.310 MB | 1.081 MB (0.3060%) | 0.45667145 | +0.1841% | 0.02147919 | +14.1870% | 13.829 pp |
| Card/note + preset | 352.912 MB | 0.479 MB (0.1356%) | 0.45656906 | +0.1617% | 0.02036155 | +8.2454% | 13.614 pp |
| Card/note + global | 353.342 MB | 0.049 MB (0.0139%) | 0.45764404 | +0.3975% | 0.02480489 | +31.8670% | 13.069 pp |
| All five scopes | 351.782 MB | 1.610 MB (0.4555%) | 0.46622274 | +2.2795% | 0.04899719 | +160.4774% | 31.550 pp |

Lower loss and RMSE are better. Percentage changes use `100 × (experiment / current int8 − 1)`. Largest prediction changes compare individual interday outcomes with the f32 reference, rather than with current int8. They are measured in percentage points.

## Why The Extra Saving Is Small

The final state contains **13,973 card states**, **13,198 note states**, **22 deck states**, **13 preset states**, and **one global state**. Card/note states account for 351.109 MB even after their existing compression. Shared scopes occupy only 2.283 MB in f32, and 0.673 MB when compacted.

| Shared scope | State count | f32 payload | int8/f16 payload | Saving |
| --- | ---: | ---: | ---: | ---: |
| Deck | 22 | 1,533,356 bytes | 452,034 bytes | 1,081,322 bytes |
| Preset | 13 | 679,614 bytes | 200,395 bytes | 479,219 bytes |
| Global | 1 | 69,698 bytes | 20,547 bytes | 49,151 bytes |

These are the exact serialized module bytes of the final resident state, including matrix scales and module headers. They are not measured process RSS or the size of the complete SQLite checkpoint store. They exclude map allocation overhead, metadata, ID encodings, curves, historical checkpoints, and other application memory. Historical checkpoint retention can change disk savings; this POC does not measure that effect.

Shared state advances on reviews across many cards. The observed loss and calibration changes establish that this particular int8/f16 scheme is unsuitable for the shared scopes on this sample. Attributing the degradation specifically to matrix quantization versus f16 shifts would require a separate ablation. Results describe this collection and trained model, rather than every collection or another quantization method.

## Method And Verification

- Copied `backup-2026-10-14-06.22.29.colpkg` into `/private/tmp/rwkv-shared-int8-2026-10-14/` and imported only the copy through the normal Anki backend API. The live collection and profile cache were not opened or modified.
- Prepared **223,954 canonical retained reviews** through the production historical-input adapter, using the collection's configured first-review elapsed policy and preset routing. Replayed all retained answers in every condition, with the same deterministic ID-feature policy. Scored **176,835 answers** with a preceding retained answer for that card and at least one scheduler day elapsed.
- Used the actual optimized native Rust backend and bundled trained model. The test-only loader changes the selected modules' existing `StatePrecision`; production `load_with_precision()` continues to select only card/note. The compact decoder now accepts four-layer modules, needed by deck/global, alongside the existing two- and three-layer modules.
- Computed natural-log binary loss with recall outcome `rating != 1` and probabilities clipped to `[1e-6, 1 − 1e-6]`. Calibration RMSE is the count-weighted error of empirical recall within fixed elapsed/review/lapse bins. Binning follows the head/temporal probe helpers: `multiplier × base^floor(log_base(value))`, rounded to nearest even, with `(248, 3.62)`, `(1.99, 1.89)`, and `(1.65, 1.73)`. Review count is the prior number of answers with elapsed days at least one, plus two; lapse count includes all prior Again answers. Zero lapses use bin zero. First-answer and unselected outcomes still advance state and counters.
- One full prediction-enabled replay ran per condition. Recorded replay times include prediction work and ran alongside repository checks; they do not establish comparative performance. This POC measures accuracy and final state payload, not startup, GUI memory, or per-answer latency.
- Extended the existing precision regression tests with all-scope int8. Bulk replay matches sequential answers byte for byte across chunk sizes 1, 9, and 51. Scalar/batch queries consume the same decoded history, answer undo restores all scopes, and seven checkpoint/reload sessions plus spill restoration preserve exact continuation.
- `rtk proxy just rwkv-state-precision-tests --release`: five tests passed. The full experiment passed. `rtk proxy just check` passed after granting access to normal Cargo/uv build caches; its first sandboxed attempt failed on cache permissions. The suite also covers the benchmark's interday selection and calibration metric calculation.

The [aggregate results](rwkv-shared-state-int8-2026-10-14.json) contain per-scope payloads, accuracy, probability-change summaries, recorded timings, and input/model/backup fingerprints. They contain no raw review history or card identities.

## Reproduction

Copy the chosen backup to a dedicated temporary directory first, then use the existing preparation recipe and the new offline benchmark recipe:

```sh
rtk proxy just rwkv-history-ablation-prepare --backup-copy /private/tmp/rwkv-shared-int8-2026-10-14/backup.colpkg --output /private/tmp/rwkv-shared-int8-2026-10-14/inputs.json --deck-match=
rtk proxy just rwkv-state-scope-bench /private/tmp/rwkv-shared-int8-2026-10-14/inputs.json /private/tmp/rwkv-shared-int8-2026-10-14/results.json
```

Keep prepared input files outside version control. Changing the backup, retained-history policy, first-answer timing, or model requires a fresh baseline; do not compare absolute metrics with the earlier [state precision benchmark](rwkv-state-precision-2026-10-10.MD).
