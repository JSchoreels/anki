# RWKV model-size speed test, 2026-11-13 (SM111)

Quiet run, Friday 14:57–15:21, on the user's own collection copy (13,980 cards, 13,205 notes, 225,132 reviews; aggregate numbers only). No training, scoring or export was running. Other CPU load 5.8–9.4% (10 s averages before and after; 3–8% during the runs). The GPU read 7–8% from desktop apps only (no compute process), so the script's GPU guard was overridden with `-AllowBusy`; the CPU guard (< 10%) was met. Five runs per cell, medians. Precision: the app default (cards int4, notes int8). The Rust side is measured; the app estimate adds the measured Python side of a build (1.07 s: parse 22 ms, convert 743 ms, pack 225 ms).

The Tiny two-head model (SM106) ran as `tiny-one=sm101@<SM106 weights>`: its `architecture.json` is byte-identical to the Tiny Instant-Only model (SM101)'s, so the kind only selects the shape; the weights are SM106's.

## State build (what the user waits for after a size switch or when no cache matches)

| Size | Models | Build s (cold) | App estimate s | Replay reviews/s | Store MB | Card + note state MB |
|---|---|---|---|---|---|---|
| Big | both: show-time big model (SM91s2) + big Instant-Only (SM99) | 22.06 | 23.1 | 11,168 | 611 | 507 |
| Big | one model: show-time big model (SM91s2) | 13.15 | 14.2 | 19,634 | 306 | 253 |
| Big | RWKV-Instant only: big Instant-Only (SM99) | 13.16 | 14.2 | 19,593 | 306 | 253 |
| Small | both: default small model (SM63) + Small Instant-Only (SM98) | 6.75 | 7.8 | 43,809 | 115 | 92 |
| Small | one model: show-time retrain (SM92) | 4.86 | 5.9 | 67,223 | 58 | 46 |
| Small | RWKV-Curve only: default small model (SM63) | 4.87 | 5.9 | 66,878 | 58 | 46 |
| Small | Small Instant-Only alone (SM98) (no longer a default path) | 4.88 | 6.0 | 66,751 | 58 | 46 |
| Tiny | both: default small model (SM63) + Tiny Instant-Only (SM101) | 6.37 | 7.4 | 47,033 | 103 | 80 |
| Tiny | one model: Tiny two-head (SM106) | 4.52 | 5.6 | 74,419 | 46 | 34 |
| Tiny / Small | RWKV-Instant only: Tiny Instant-Only (SM101) | 4.54 | 5.6 | 73,787 | 46 | 34 |

Warm runs (second build in the same process) are 1–3% slower than cold. About 1.5 s of every build is history reading, the same for every size, so the small sizes are close to that floor: Tiny alone saves only ~0.3 s over Small alone.

Bytes per card / note: Big 9,271 / 10,283 per model; Small 1,963 / 1,567; Tiny 1,055 / 1,567. A pair stores both models.

## Load and score

| Size | Load a stored state s | Score all cards s |
|---|---|---|
| Big pair / one | 2.41 / 1.90 | 0.49 |
| Small pair / one | 1.53 / 1.44 | 0.13 |
| Tiny pair / one | 1.50 / 1.43 | 0.12 |

Peak RAM of the test process: 1.8 GB during a build (the history), 0.7 GB after a load.

## Against the user's own app timings

The user measured Big ~12–15 s and Small ~4–6 s in the app. The estimates here (Big one model 14.2 s, Small one model 5.9 s) fall in those ranges, so the harness is realistic. Not covered: the Qt progress dialog and the deck-list refresh after it closes, and a full app build (no Node/MSYS2 on this computer).

Raw data: `.local/speed-final/results.jsonl`, `summary.md`, `run.log` (git-ignored: collection-derived).
