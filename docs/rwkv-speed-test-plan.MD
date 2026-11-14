# RWKV model-size speed test: harness, calibration and plan — 2026-11-12

Goal: time the Big, Small and Tiny model sizes the way the user waits for them in the desktop app, so the numbers are not misleading. The user's own timings in the app: Big about 12–15 s, Small about 4–6 s.

## Which operation takes 12–15 s / 4–6 s

The **state build**: the full replay of the collection's review history into a fresh RWKV state. It runs, behind the modal "RWKV State Cache" progress dialog, whenever no usable cache matches the current runtime: after a model-size switch in Deck Options (the state cache key holds the model key, the instant model and the weights' SHA-256, so the next use rebuilds), after the one-model switch or a state-precision change, and on the first profile open of a new runtime.

Code path (`qt/aqt/rwkv_scheduler.py`): `build_rwkv_state_cache_with_progress` / `request_rwkv_state_cache_recovery` → `warm_up_rwkv_state` → `_warm_up_reviewer_backend`:

1. `_restore_reviewer_backend_cache` finds no matching cache;
2. `_historical_rwkv_review_inputs` → `aqt/rwkv_history.py::full_review_inputs` → native `Collection::rwkv_historical_review_inputs` (`rslib/src/scheduler/rwkv/history.rs`), then one Python `RwkvReviewInput` per row;
3. `_warm_up_rwkv_reviews` → `EmbeddedRwkvReviewerBackend.warm_up_reviews` (`aqt/rwkv_srs_benchmark.py`): chunks of 16,384 reviews, each packed in Python (`_packed_warm_up_reviews`) and replayed by `RwkvRuntime::warm_up_reviews` (`pylib/rsbridge/lib.rs` → `rslib/src/rwkv/runtime.rs`; a pair replays both models in parallel with `rayon::join`), state only;
4. at the review eight days before the last one a full checkpoint, and after the last review a delta checkpoint, both `write_warm_up_state_checkpoint` into a temporary `state-v12.sqlite3`, then `_save_reviewer_backend_state_store` renames it.

The other candidates are much shorter on this collection (measured below): the cache **load** at profile open (`load_rwkv_state_cache_with_progress`: history fingerprint check + `restore_warm_up_state_checkpoint`) takes 1.6–2.5 s for every size, 1.4–1.5 s of it the size-independent fingerprint; **scoring** every reviewed card from the resident state takes 0.15 s (Small pair) to 0.55 s (Big pair); the per-card prediction when a card is shown is a single query.

## The harness

- `rslib/src/rwkv/app_speed_bench.rs` (`cargo test --release`, ignored test `rwkv_app_speed_benchmark`): runs steps 2–4 with the app's request, chunk size, checkpoints, `target_retention` 0.9, `max_interval_days` 36,500, default state precision (int4 cards + int8 notes) and Rayon's default pool (one thread per logical CPU, as in the app), on a collection copy. Operations `build`, `load`, `score` and `state` (stored bytes per card and note); every run prints a JSON line with phase times, replay reviews/s, this process's CPU cores, the machine's other load, and peak working set / private bytes (kernel32 counters, no extra crate).
- **Any model file:** `ANKI_SPEED_RUNTIMES` takes the named runtimes below or `name=kind[@path][+kind[@path]]`, for example `tiny-one-new=sm106@D:/sm106/weights.f32.bin` or `tiny-pair-new=sm72e25+sm106@...`. A path replaces the bundled weights of a model kind; a model with a new architecture or input rule also needs its `ModelKind` in `runtime.rs` (the fork adds one whenever it ships a model), after which it is one more spec.
- `qt/tools/rwkv_speed_python_overhead.py`: the Python side of a build (proto parse, row conversion, packing, checkpoint map encoding) run with the app's own functions on the history bytes the harness dumps (`ANKI_SPEED_DUMP_HISTORY`, collection data, scratch folder only).
- `qt/tools/rwkv_speed_test.ps1`: the final run (all sizes × modes, cold and warm, 5 repeats, peak RAM, state bytes, Python side), with a quiet-machine check; `qt/tools/rwkv_speed_summary.py` turns the JSON lines into medians.

Named runtimes (`qt/aqt/rwkv_model.py::runtime_models`, 2026-11-12):

| Key | Size and mode | Models |
| --- | --- | --- |
| `big-pair` | Big, RWKV-Instant and RWKV-Curve (default) | the show-time big model (SM91s2) + the show-time instant-only big model (SM99) |
| `big-one` | Big, one-model choice; also RWKV-Curve only | the show-time big model (SM91s2) |
| `big-instant` | Big, RWKV-Instant alone | the show-time instant-only big model (SM99) |
| `small-pair` | Small, both schedulers (default) | the default small model (SM63) + the Small Instant-Only model (SM98) |
| `small-one` | Small and Tiny, one-model choice | the show-time retrain (SM92) |
| `small-curve` | Small and Tiny, RWKV-Curve only | the default small model (SM63) |
| `small-instant` | Small, RWKV-Instant alone | the Small Instant-Only model (SM98) |
| `tiny-pair` | Tiny, both schedulers (default) | the default small model (SM63) + the Tiny Instant-Only model (SM101) |
| `tiny-instant` | Tiny, RWKV-Instant alone | the Tiny Instant-Only model (SM101) |

## Calibration (2026-11-12, contended)

Collection copy: 31,673 cards, 16,213 notes, 274,361 answers in the review log; the replay keeps 225,132 reviews of 13,980 cards and 13,205 notes. One fresh process per run, release build, 16 logical CPUs. A GPU training job ran throughout (GPU 77–92%, 14.6 GB): the Windows CPU load was 32–49% before the runs, and other processes used 16–21% of the machine during them.

| Runtime | Build total | History (Rust) | Replay | Checkpoints | Replay reviews/s | Cores used | Peak RAM | State store |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Big pair: the show-time big model (SM91s2) + the show-time instant-only big model (SM99) | 24.3 s | 1.48 s | 22.3 s | 0.53 s | 10,108 | 8.1 | 1,779 MB | 611 MB |
| Big, one model: the show-time big model (SM91s2) | 14.6 s | 1.48 s | 12.9 s | 0.23 s | 17,458 | 6.7 | 979 MB | 306 MB |
| Small pair: the default small model (SM63) + the Small Instant-Only model (SM98) | 7.2 s | 1.54 s | 5.5 s | 0.15 s | 40,950 | 5.9 | 755 MB | 115 MB |
| Small, one model: the default small model (SM63) | 5.2 s | 1.47 s | 3.7 s | 0.08 s | 61,177 | 4.3 | 455 MB | 58 MB |

Python side of a build in the app (3 repeats on the same history): 1.0–1.3 s (row conversion 0.71–0.90 s, packing 0.21–0.31 s, proto parse 0.02 s), serial with the replay. So the app's build is about the harness total + 1.1 s: Big one model ≈ 15.7 s, Small one model ≈ 6.3 s, Big pair ≈ 25 s, Small pair ≈ 8.3 s on this contended machine.

Cache load at profile open (same stores): Big pair 2.47 s, Big one model 2.07 s, Small pair 1.64 s, Small one model 1.59 s, of which the history fingerprint 1.42–1.51 s. Scoring the 13,980 reviewed cards: 0.15 s (Small pair), 0.55 s (Big pair).

**Match with the app:** the one-model builds reproduce the user's numbers — 14.6 s (≈ 15.7 s with the Python side) against 12–15 s, 5.2 s (≈ 6.3 s) against 4–6 s, ratio 2.8 against about 2.7 — slightly above the user's range, as expected with 16–21% of the CPU taken by the training job. The pairs do not match (24 s and 7 s, ratio 3.4): Big has run as a pair only since today (SM109), so the user's Big timing was of the show-time big model (SM91s2) alone; whether their Small timing was a pair or one model depends on their presets and app build. This copy's presets would run the pairs by default (2 presets use both schedulers, 3 RWKV-Instant only). The copy is from 2026-11-03 and the user may have timed on another computer, so the final run should time every mode. Debug builds are ruled out: they would be many times slower than these release numbers, not similar. The harness's history phase is single-threaded SQL (1.5 s for every size); the replay is where sizes differ.

## Plan for the final timing run

When: a quiet window with the GPU queue idle and no other CPU work (probably Friday after 16:00). The script refuses to start if the GPU is above 5% or the CPU load above 10% (override `-AllowBusy`). About 25 minutes for all nine runtimes (the Big ones take most of it).

What it runs, per runtime (all nine above, plus any custom spec):

- **cold build** ×5: a fresh process each, build then score (as after a size switch), so peak RAM is that operation's;
- **cold load** ×5: a fresh process each, load then score (profile open);
- **warm build** ×5 and **warm load** ×5: one process, after one untimed run;
- **state**: stored card and note bytes, bytes per card and per note;
- once: the Python side of a build, 5 repeats.

Medians, min–max, replay reviews/s, cores used, other load, peak RAM, store size and the app estimate (build median + Python side) go to `summary.md`.

Cold means a fresh process (new allocations and thread pool, weights read again). The OS file cache cannot be dropped without an install or admin tools, so the collection and weights are usually already cached; the app's first open after a reboot can be a little slower.

To add later:

- **The Tiny two-head model (SM106)**, result ~02:00 Fri, as Tiny's one-model choice: once the fork has its `ModelKind` (key, say, `sm106`) and bundled weights, add `-Runtimes ...,tiny-one=sm106` (or `tiny-one=sm106@<path>` before it is bundled).
- **RWKV-Instant alone on Tiny by default:** already timed as `tiny-instant`; if the default mapping changes, only the table's labels change.

## What needs a full app build or an install

- The end-to-end wall time in the app: Qt progress updates, the profile-open sequence, the cache metadata JSON, the deck-list refresh with RWKV deck counts after the dialog closes, and the Python side measured inside the running app rather than beside it (needs Node and MSYS2 for a full build).
- The `release-lto` profile of packaged releases (the harness uses `release`; LTO may be a little faster).
- A cold OS file cache (needs RAMMap or a reboot between runs).
