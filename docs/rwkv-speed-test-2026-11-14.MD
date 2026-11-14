# RWKV speed test, family wiring (2026-11-14)

Rerun of the speed test of 2026-11-13 (`docs/rwkv-speed-test-2026-11-13.MD`) on the fork code that runs each size's own models (the family wiring (SM114), fork main 001440b03, merged into `speed-test` at 92767322b, harness d6344d874). New: the Tiny pair, the Tiny two-head model (SM106) beside the Tiny Instant-Only model (SM101), and the Small pair with the show-time retrain (SM92) instead of the default small model (SM63).

Conditions: same collection copy (13,980 cards, 13,205 notes, 225,132 reviews; aggregate numbers only), release test binary, default precision (int4 cards + int8 notes), 5 runs per cell, medians. Started 14:22 Sat after the GPU queue went idle. CPU load before 8% (10 s average), after 13.4%; other load during the runs 3-7%. The GPU read 8-10% from desktop apps only (no compute process, queue idle), so the run used `-AllowBusy`, as on 11-13. App estimate = cold build + 1.11 s measured Python side (parse 22 ms, convert 782 ms, pack 228 ms).

## State build per size and mode

| Size | Presets use | Models | Build s (cold) | App estimate s | Replay reviews/s | Stored state MB | Bytes per card + note |
|---|---|---|---|---|---|---|---|
| Big | both schedulers | the show-time big model (SM91s2) + the show-time instant-only big model (SM99) | 22.15 | 23.3 | 11,127 | 611 | 18,561 + 20,585 |
| Big | one model, or one scheduler | the show-time big model (SM91s2) or the show-time instant-only big model (SM99) | 13.02 | 14.1 | 19,819 | 306 | 9,271 + 10,283 |
| Small | both schedulers | the show-time retrain (SM92) + the Small Instant-Only model (SM98) | 6.69 | 7.8 | 43,990 | 115 | 3,945 + 3,153 |
| Small | one model, or one scheduler | the show-time retrain (SM92) or the Small Instant-Only model (SM98) | 4.80 | 5.9 | 67,870 | 58 | 1,963 + 1,567 |
| Tiny | both schedulers | the Tiny two-head model (SM106) + the Tiny Instant-Only model (SM101) | 6.08 | 7.2 | 49,641 | 90 | 2,129 + 3,153 |
| Tiny | one model, or one scheduler | the Tiny two-head model (SM106) or the Tiny Instant-Only model (SM101) | 4.44-4.48 | 5.6 | 75,310-75,957 | 46 | 1,055 + 1,567 |

Load of a stored state: 1.40-1.48 s (Small, Tiny), 1.85-2.40 s (Big). Scoring every card: 0.12-0.13 s (Small, Tiny), 0.46-0.48 s (Big). Everything measured on 11-13 matches within 0.1 s. About 1.5 s of every build is reading the history, the same for every size, so the Tiny pair saves 0.6 s over the Small pair and the one-model Tiny 0.3 s over the one-model Small.

## Full summary (from `qt/tools/rwkv_speed_summary.py`)

| Runtime | Operation | Cold / warm | Runs | Median s | Min-max s | Replay reviews/s | Process cores | Other load % | Peak RAM MB | Store MB | App estimate s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| big-pair | build | cold | 5 | 22.15 | 21.81-22.27 | 11,127 | 8.8 | 6.7 | 1,813 | 611 | 23.27 |
| big-pair | build | warm | 5 | 22.23 | 21.87-22.41 | 11,098 | 8.3 | 6.4 | 1,794 | 611 | 23.34 |
| big-pair | load | cold | 5 | 2.40 | 2.38-2.42 |  | 1.0 | 4.1 | 706 | 611 |  |
| big-pair | load | warm | 5 | 2.40 | 2.40-2.41 |  | 1.0 | 5.5 | 710 | 611 |  |
| big-pair | score after build | cold | 5 | 0.47 | 0.47-0.48 |  | 15.0 | 6.2 | 1,813 |  |  |
| big-pair | score after load | cold | 5 | 0.47 | 0.46-0.47 |  | 14.3 | 4.7 | 724 |  |  |
| big-one | build | cold | 5 | 13.02 | 12.98-13.05 | 19,819 | 7.3 | 5.9 | 1,813 | 306 | 14.13 |
| big-one | build | warm | 5 | 13.44 | 13.41-13.46 | 19,338 | 7.0 | 6.4 | 1,794 | 306 | 14.55 |
| big-one | load | cold | 5 | 1.92 | 1.88-1.95 |  | 1.0 | 4.6 | 724 | 306 |  |
| big-one | load | warm | 5 | 1.87 | 1.85-1.88 |  | 1.0 | 3.7 | 710 | 306 |  |
| big-one | score after build | cold | 5 | 0.47 | 0.47-0.48 |  | 14.6 | 6.1 | 1,813 |  |  |
| big-one | score after load | cold | 5 | 0.46 | 0.46-0.47 |  | 14.9 | 3.3 | 724 |  |  |
| big-instant | build | cold | 5 | 13.02 | 12.93-13.07 | 19,816 | 7.5 | 6.2 | 1,813 | 306 | 14.13 |
| big-instant | build | warm | 5 | 13.40 | 13.26-13.54 | 19,398 | 7.1 | 6.2 | 1,794 | 306 | 14.51 |
| big-instant | load | cold | 5 | 1.90 | 1.89-1.90 |  | 1.0 | 3.8 | 724 | 306 |  |
| big-instant | load | warm | 5 | 1.85 | 1.83-1.88 |  | 1.0 | 3.7 | 710 | 306 |  |
| big-instant | score after build | cold | 5 | 0.48 | 0.47-0.48 |  | 15.2 | 5.0 | 1,813 |  |  |
| big-instant | score after load | cold | 5 | 0.46 | 0.46-0.47 |  | 14.5 | 4.7 | 724 |  |  |
| small-pair | build | cold | 5 | 6.69 | 6.67-6.73 | 43,990 | 6.7 | 7.2 | 1,813 | 115 | 7.80 |
| small-pair | build | warm | 5 | 7.01 | 6.94-7.04 | 42,129 | 6.5 | 6.7 | 1,794 | 115 | 8.12 |
| small-pair | load | cold | 5 | 1.52 | 1.51-1.54 |  | 1.0 | 4.9 | 724 | 115 |  |
| small-pair | load | warm | 5 | 1.51 | 1.51-1.54 |  | 1.0 | 4.3 | 710 | 115 |  |
| small-pair | score after build | cold | 5 | 0.13 | 0.13-0.13 |  | 14.3 | 5.3 | 1,813 |  |  |
| small-pair | score after load | cold | 5 | 0.13 | 0.13-0.13 |  | 14.1 | 5.4 | 724 |  |  |
| small-one | build | cold | 5 | 4.80 | 4.78-4.85 | 67,870 | 4.8 | 6.3 | 1,813 | 58 | 5.91 |
| small-one | build | warm | 5 | 4.99 | 4.96-5.04 | 66,070 | 4.7 | 6.1 | 1,794 | 58 | 6.10 |
| small-one | load | cold | 5 | 1.44 | 1.42-1.46 |  | 1.0 | 6.3 | 724 | 58 |  |
| small-one | load | warm | 5 | 1.43 | 1.42-1.44 |  | 1.0 | 4.3 | 710 | 58 |  |
| small-one | score after build | cold | 5 | 0.13 | 0.13-0.13 |  | 14.9 | 4.9 | 1,813 |  |  |
| small-one | score after load | cold | 5 | 0.13 | 0.13-0.13 |  | 13.2 | 4.5 | 724 |  |  |
| small-instant | build | cold | 5 | 4.78 | 4.75-4.84 | 68,213 | 4.8 | 5.6 | 1,813 | 58 | 5.89 |
| small-instant | build | warm | 5 | 4.97 | 4.96-5.02 | 66,170 | 4.7 | 5.9 | 1,794 | 58 | 6.08 |
| small-instant | load | cold | 5 | 1.44 | 1.43-1.45 |  | 1.0 | 3.9 | 724 | 58 |  |
| small-instant | load | warm | 5 | 1.43 | 1.42-1.43 |  | 1.0 | 3.7 | 710 | 58 |  |
| small-instant | score after build | cold | 5 | 0.13 | 0.13-0.13 |  | 13.3 | 5.3 | 1,813 |  |  |
| small-instant | score after load | cold | 5 | 0.13 | 0.13-0.13 |  | 13.2 | 4.5 | 724 |  |  |
| tiny-pair | build | cold | 5 | 6.08 | 6.05-6.12 | 49,641 | 6.8 | 6.2 | 1,813 | 90 | 7.19 |
| tiny-pair | build | warm | 5 | 6.35 | 6.32-6.36 | 47,831 | 6.7 | 7.1 | 1,794 | 90 | 7.46 |
| tiny-pair | load | cold | 5 | 1.48 | 1.47-1.50 |  | 1.0 | 2.9 | 724 | 90 |  |
| tiny-pair | load | warm | 5 | 1.47 | 1.46-1.47 |  | 1.0 | 5.3 | 710 | 90 |  |
| tiny-pair | score after build | cold | 5 | 0.12 | 0.12-0.12 |  | 14.3 | 4.5 | 1,813 |  |  |
| tiny-pair | score after load | cold | 5 | 0.12 | 0.12-0.13 |  | 15.7 | 3.4 | 724 |  |  |
| tiny-one | build | cold | 5 | 4.48 | 4.44-4.49 | 75,310 | 4.6 | 6.7 | 1,813 | 46 | 5.59 |
| tiny-one | build | warm | 5 | 4.58 | 4.56-4.75 | 74,144 | 4.3 | 5.7 | 1,794 | 46 | 5.69 |
| tiny-one | load | cold | 5 | 1.43 | 1.42-1.44 |  | 1.0 | 4.0 | 724 | 46 |  |
| tiny-one | load | warm | 5 | 1.41 | 1.40-1.42 |  | 1.0 | 3.3 | 710 | 46 |  |
| tiny-one | score after build | cold | 5 | 0.12 | 0.12-0.13 |  | 14.2 | 4.7 | 1,813 |  |  |
| tiny-one | score after load | cold | 5 | 0.12 | 0.12-0.14 |  | 13.7 | 3.1 | 724 |  |  |
| tiny-instant | build | cold | 5 | 4.44 | 4.42-4.47 | 75,957 | 4.5 | 6.6 | 1,813 | 46 | 5.55 |
| tiny-instant | build | warm | 5 | 4.58 | 4.53-4.62 | 73,937 | 4.7 | 6.1 | 1,794 | 46 | 5.69 |
| tiny-instant | load | cold | 5 | 1.42 | 1.40-1.43 |  | 1.0 | 3.8 | 724 | 46 |  |
| tiny-instant | load | warm | 5 | 1.40 | 1.40-1.41 |  | 1.0 | 4.7 | 710 | 46 |  |
| tiny-instant | score after build | cold | 5 | 0.12 | 0.12-0.13 |  | 13.3 | 5.4 | 1,813 |  |  |
| tiny-instant | score after load | cold | 5 | 0.12 | 0.12-0.12 |  | 14.2 | 3.0 | 724 |  |  |

| Runtime | Cards | Notes | Bytes per card | Bytes per note | Card + note MB |
| --- | --- | --- | --- | --- | --- |
| big-pair | 13,980 | 13,205 | 18,561 | 20,585 | 507 |
| big-one | 13,980 | 13,205 | 9,271 | 10,283 | 253 |
| big-instant | 13,980 | 13,205 | 9,271 | 10,283 | 253 |
| small-pair | 13,980 | 13,205 | 3,945 | 3,153 | 92 |
| small-one | 13,980 | 13,205 | 1,963 | 1,567 | 46 |
| small-instant | 13,980 | 13,205 | 1,963 | 1,567 | 46 |
| tiny-pair | 13,980 | 13,205 | 2,129 | 3,153 | 68 |
| tiny-one | 13,980 | 13,205 | 1,055 | 1,567 | 34 |
| tiny-instant | 13,980 | 13,205 | 1,055 | 1,567 | 34 |

Python side of a build (median of 5): 1.11 s (parse 22 ms, convert 782 ms, pack 228 ms) for 225,132 reviews.
