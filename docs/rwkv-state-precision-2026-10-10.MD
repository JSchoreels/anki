# Native RWKV State Precision Benchmark — 2026-10-10

Selectable int8 state cuts the native state store by **68.35%** and fresh-load
peak process memory by **60.19%** on this collection, with effectively unchanged
aggregate accuracy. Experimental int4 cuts them by **79.75%** and **71.72%**, but
increases RMSE(bins) by **5.70%**. Compact storage adds CPU conversion work: this
implementation makes a roughly ten-second rebuild slightly slower.

## Native Rebuild And Storage

These are medians of three full replays per mode, in rotating order. Save is
included in total; the median total need not equal the sum of phase medians.

| Mode | Replay | Save | Rebuild + save | Fresh load | Native store | Load peak process RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| f32 | 8.39 s | 1.524 s | 9.65 s | 0.357 s | 1,247 MB | 1,326 MB |
| int8 | 10.74 s | 0.244 s | 10.97 s | 0.383 s | 395 MB | 528 MB |
| int4 | 11.05 s | 0.173 s | 11.22 s | 0.502 s | 253 MB | 375 MB |

For int8, saving falls from 1.524 s to 0.244 s, but replay rises from 8.39 s to
10.74 s. Total rebuild plus save rises by **13.68%**, from 9.65 s to 10.97 s.
For int4, total rises by **16.26%**, to 11.22 s. Neither mode improves
fresh-load time here: validating and unpacking compact state consumes CPU. The
int8 difference is about 26 ms; int4 adds about 145 ms.

The practical benefit is less memory while RWKV state is resident and much
smaller state files. Initial preparation is slightly slower. The selectable f32
fallback keeps the original state behavior. Startup GUI time and ordinary
per-answer latency are not measured by this benchmark.

## Accuracy After Every Answer

All **223,832 retained reviews** update the state. Metrics score the
**176,722 reviews separated by at least one scheduler day**. Compact card
and note state is rounded after every answer, and later predictions consume
that rounded state. This includes the f16 shifts used by both integer modes;
shared deck, preset, and global state and computation remain f32.

| Mode | Log loss | Relative log-loss increase | RMSE(bins) | Relative RMSE increase | Largest prediction change |
| --- | ---: | ---: | ---: | ---: | ---: |
| f32 | 0.45573652 | +0.000000% | 0.01855497 | +0.0000% | 0.000 pp |
| int8 | 0.45573628 | -0.000052% | 0.01855671 | +0.0094% | 0.679 pp |
| int4 | 0.45604446 | +0.067569% | 0.01961238 | +5.6988% | 14.086 pp |

Lower loss and RMSE are better. Relative change is
`100 × (compact metric / f32 metric − 1)`. The tiny negative int8 log-loss change
is effectively zero on this sample; it does not establish an accuracy benefit.
Individual int4 predictions can change considerably despite the small aggregate
log-loss increase. These results describe this collection and model, not a bound
for every user.

Log loss uses natural logarithms and recall outcome `rating != 1`, clipping
probabilities to `[1e-6, 1 − 1e-6]`. RMSE(bins) is the count-weighted root mean
square difference between predicted and observed recall within fixed bins of
elapsed days, prior long-term review count plus one, and prior lapses. The bins
use the existing calibration helpers in
[the history probe](../qt/tools/rwkv_head_finetune_probe.py) and
[the temporal probe](../qt/tools/rwkv_temporal_calibration_probe.py), with
`(multiplier, base)` values `(248, 3.62)`, `(1.99, 1.89)`, and `(1.65, 1.73)`.
Zero prior lapses uses bin zero. This is calibration RMSE, not per-review binary
outcome RMSE or the RMSE of differences from f32.

## Method And Controls

- Used prepared canonical history from a copied 2026-10-10 collection backup,
  never the live profile: **13,965 cards**, **13,190 notes**, and
  **984 review days**.
- Ran the actual optimized desktop Rust/PyO3 backend on a MacBookPro18,2 with
  10 logical CPUs and 64 GiB RAM, using the bundled trained RWKV model and
  `RwkvInference(model_path, 0.9, 36500, mode)`.
- Built history in chunks of 16,384 reviews. Wrote a native recovery root after
  **223,584 reviews** and a final parent-linked delta after the remaining
  **248 reviews**, then finalized and fsynced the native SQLite state
  store. History maps used minimal placeholders; full profile metadata is not
  included in these file sizes.
- Loaded each completed state store into a fresh process through
  `restore_warm_up_state_checkpoint()`, with a warm OS file cache. Model startup,
  Python history preparation, collection SQL, manifest work, and the Anki GUI
  are excluded from the timings. Fresh-load peak RSS includes the interpreter,
  imported libraries, model, and loaded native state. It is not steady-state
  Anki GUI RSS or peak memory during a full rebuild. MB is decimal.
- Paused the competing Primarius benchmark for all nine timing rounds and
  resumed it before the accuracy replays. Accuracy results are unchanged by CPU
  contention; the integer/f16 conversion and rounding rules are deterministic.
- The final f32 state matched the independent pre-feature full-history snapshot
  byte for byte: SHA-256
  `78f1d94e75b19a3e34409c07535969d692c1bbf885924e71115749471e93f089`.
- Prediction-enabled replays verified complete prediction coverage. The full
  history int8/int4 metrics exactly matched the earlier independent per-answer
  quantization experiment.
- Regression tests compare bulk replay against one-answer-at-a-time execution
  across chunk boundaries in all three modes. Seven real checkpoint/reload
  sessions per mode produce exactly the same final state as uninterrupted
  replay. Compact matrices preserve their last integer codes and scales, so
  saving and loading adds no extra quantization.
- The real Preferences widget save path, mode changes requiring full historical
  replay, packed scalar/batch queries, answer undo, and spill restoration are
  covered by tests. The full `just check` passed in an isolated worktree; the
  optimized desktop build passed.

The [aggregate results](rwkv-state-precision-2026-10-10.json) include all timing
rounds, exact metrics, and input/model/source/backend hashes. They contain no raw
review history.
