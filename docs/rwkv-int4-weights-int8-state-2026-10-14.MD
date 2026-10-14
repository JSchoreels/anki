# RWKV Int4 Weights + Int8 State POC — 2026-10-14

Packing dense projection weights to int4 shrinks the bundled model from **11.083 MB to 1.923 MB (−82.65%)**. Combined with current card/note-only int8 state, this saves **9.160 MB (2.51%)** of the model-file plus final-state payload on this collection. Relative to f32 weights with the same int8 state, log loss increases **0.142%** and calibration RMSE increases **4.25%**. Individual recall predictions can change by **13.06 percentage points**.

This is an offline storage and numerical-accuracy POC. The experimental file is decoded to f32 before native inference; runtime weight RAM savings and packed-kernel speed have not been measured or implemented. Desktop loading, trained model files, profile precision settings, and live caches are unchanged.

## Controlled Comparison

| Weights | Card/note state | Model file | Final state payload | Combined storage payload | Log loss | RMSE(bins) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| f32 | f32 | 11.083 MB | 1,192.819 MB | 1,203.902 MB | 0.45583339 | 0.01880904 |
| f32 | int8/f16 (current) | 11.083 MB | 353.391 MB | 364.474 MB | 0.45583223 | 0.01881054 |
| int4 projections | f32 | 1.923 MB | 1,192.819 MB | 1,194.742 MB | 0.45647720 | 0.01961060 |
| int4 projections | int8/f16 | 1.923 MB | 353.391 MB | 355.314 MB | 0.45647842 | 0.01961010 |

Deck, preset, and global state remain f32 in all conditions. Arithmetic, activations, biases, normalization parameters, and non-projection mixing parameters also remain f32. The current int8 state format uses int8 recurrent matrices and f16 shift vectors, rounded after every answer.
All sizes use decimal MB (`1 MB = 1,000,000 bytes`).

Relative changes use `100 × (int4-weight/int8-state metric / current f32-weight/int8-state metric − 1)`. Mean absolute prediction change is **1.391 pp**; the 99th-percentile absolute change is **6.952 pp**; maximum change is **13.056 pp**, all relative to current int8. The JSON records the exact values and also comparisons with the full-f32 reference.

The int4-weight/f32-state and int4-weight/int8-state results are nearly identical in aggregate: the weight rounding accounts for most of the accuracy change. This scheme has a much smaller measured accuracy cost than the [shared-state int8 experiment](rwkv-shared-state-int8-2026-10-14.MD), but it still changes individual predictions substantially. These results support evaluating an experimental model-storage option, rather than changing the default or assuming a memory/performance benefit.

## Weight Format And Boundaries

- Quantized **2,719,744 of 2,762,884 parameters (98.44%)**: tensors ending in `.weight` with rank two, covering dense projections in the input network, all RWKV modules, low-rank branches, and output heads. Biases, normalization parameters, scalar/vector parameters, and mixing tensors such as `rkvdag_lerp` retain their original f32 values.
- Used symmetric signed int4 codes `−7…7`, with one f32 absolute-maximum scale per group of up to **32 input weights within one output row**. Groups never cross projection rows. Codes use nearest-even rounding, and two codes occupy each byte. Partial groups and odd row lengths have explicit zero padding. This is direct post-training rounding; it uses no calibration fitting, retraining, or quantization-aware training.
- The test-only `ARWKVINT4POC1` file includes tensor names/shapes, f32 scales, packed codes, and untouched f32 tensors. Its measured size includes all that overhead. It is not a production Anki model format. The original `.bin` remains unchanged; the artifact is at `/private/tmp/rwkv-weight-int4-2026-10-14/model.int4.bin`.
- The POC reads the written artifact back through its decoder, constructs the existing native model from the decoded `WeightMap`, and replays reviews using the normal optimized backend. Decoded projection values remain rounded to the int4 grid even though they occupy f32 buffers during inference. The private `SrsModel::from_weights()` extraction is shared with normal model loading; it does not change the production loader's file format or precision.
- Model size is small compared with resident card/note state in this collection. The **2.51% combined saving is storage accounting**, not measured process RSS: it adds model-file bytes to final serialized module bytes. It excludes map overhead, history, feature encodings, curves, metadata, and retained checkpoint segments. A packed runtime would need separate kernel, scratch-memory, and latency measurements.

## Method And Verification

Reused the prepared canonical inputs from the copied `backup-2026-10-14-06.22.29.colpkg` described in the shared-state report. Every condition replays **223,954 retained answers** and scores the same **176,835 interday outcomes**. The live collection and profile caches were not opened or modified. Both f32-weight controls reproduce the previous experiment's loss, calibration, and payload values exactly.

The four conditions share the same retained history, first-review timing policy, preset routing, deterministic ID-feature assignment, and metric implementation. Log loss uses natural logs, recall outcome `rating != 1`, and probabilities clipped to `[1e-6, 1 − 1e-6]`. RMSE(bins) uses the elapsed/review/lapse calibration bins documented in the [shared-state report](rwkv-shared-state-int8-2026-10-14.MD). All retained answers advance state, including first and same-day answers excluded from scoring.

One full prediction-enabled replay ran per condition. The run overlapped repository checks, and all weights were decoded before the replay. Recorded times therefore do not establish an int4 speed gain. Startup, real reviewer latency, process RSS, and changes to scheduled intervals were not measured.

- `rtk proxy just rwkv-weight-precision-tests --release`: **four tests passed**. They cover signed codes, even rounding, zero and odd rows, group boundaries, exact preservation of untouched tensors, corrupted/truncated files, and real-model loading with int8 card state plus f32 deck state.
- `rtk proxy just rwkv-weight-precision-bench ...`: the four-condition full-history experiment passed with complete prediction coverage and finite probabilities.
- `rtk proxy just check`: passed after correcting a Clippy warning in the new helper's path argument. Existing state precision, replay, query, undo, checkpoint, and spill tests also remain in the full suite.

The [aggregate results](rwkv-int4-weights-int8-state-2026-10-14.json) include model sizes, parameter counts, final per-scope payloads, accuracy, probability changes, recorded timings, and input/original-model/packed-model/backup fingerprints. No raw history or card identities are included.

## Reproduction

Prepare copied backup inputs with the existing recipe, or reuse the exact prepared file from the shared-state experiment. Create a dedicated output directory, then run:

```sh
rtk proxy just rwkv-weight-precision-tests --release
rtk proxy just rwkv-weight-precision-bench /private/tmp/rwkv-shared-int8-2026-10-14/inputs.json /private/tmp/rwkv-weight-int4-2026-10-14/results.json /private/tmp/rwkv-weight-int4-2026-10-14/model.int4.bin
```

Keep raw replay inputs and experimental model binaries outside version control. Changing the model, backup, quantization scheme, or retained-history/timing policy requires new matched controls. These results do not establish the behavior of another int4 method or another collection.
