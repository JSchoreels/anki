# SSE calibration responsiveness benchmark

Measured on 2026-10-09, macOS arm64, with optimized native builds. The baseline
was local Anki commit `82c081c05`, which already trained FSRS outside the collection
lock. Its Python sources, generated wrappers and native library were preserved
before these changes. The same backup copy was imported into a fresh temporary
collection for every sample. The live profile was not opened or modified.

The backup contains 31,665 cards and 269,694 rated reviews. Each case uses three
fresh processes, alternating baseline and updated code. The table shows medians;
[raw samples](sse-calibration-benchmark-2026-10-09.json) include process peak RSS,
prediction checksums and cancellation results.

| Measurement | Before | After | Practical effect |
| --- | ---: | ---: | --- |
| Longest collection-read wait during final-fit generation | 1,048 ms | 243 ms | 77% shorter wait for a foreground collection request |
| Complete final-fit generation | 1,838 ms | 1,543 ms | 16% faster, about 0.3 seconds saved |
| Peak RSS during final-fit generation | 457 MiB | 407 MiB | 11% lower process peak |
| Peak RSS through the first validation training iteration | 463 MiB | 247 MiB | 47% lower preparation peak |
| Collection-read wait in that validation preparation probe | 605 ms | 232 ms | Conversion and fold copies no longer hold the mutex |
| Early-cancel job duration, abort requested at about 25 ms | 1,808 ms, completed anyway | 240 ms, interrupted | Preparation no longer clears the abort request; zero rows saved |
| Qt heartbeat gap with a controlled 350 ms fingerprint query | 355 ms | 18 ms | The completion query runs on the worker |

The final-fit runs all stored 247,394 rows, with identical prediction checksums
including review IDs, roles, folds and sources. Timestamps are excluded. This
case does not train validation folds. The validation probe cancels when native
progress reports its first training iteration; it measures preparation and early
memory use, not complete five-fold training time or its eventual peak memory.

Collection reads poll `card_count()` on a second thread about every 10 ms. The
reported wait is the longest observed request, not an average or a guarantee for
every collection. The revlog snapshot still holds the collection lock; this
accounts for the remaining roughly quarter-second wait on this backup. Disk speed,
history distribution and ongoing work can change these measurements.

The Qt case runs Anki's real `TaskManager`, `QueryOp` and Qt event loop offscreen.
Its SQL stub sleeps for 350 ms; it is a controlled regression probe, not a claim
that real cache queries always take that long. The baseline queried on Qt's main
thread; the updated code queried on the collection worker.

RWKV regression tests also verify one live prepared history instead of two,
unchanged calibration roles, restored reviewer state, and rejection of histories
whose generation, settings, reviews or ignored IDs changed. No RWKV full-replay
speedup is claimed: the attempted native baseline warmup was stopped after several
minutes, before a completed comparable pair was available.

## Reproducing

Build each version using its normal `just` workflow, and preserve its complete
Python/generated/native output before rebuilding the other version. Supply an
already copied `.colpkg` under `/private/tmp` or the workspace. Each invocation
imports into a fresh directory and writes a JSON report.

```sh
rtk just sse-calibration-bench \
  --source-root /path/to/preserved/build \
  --backup /private/tmp/calibration/backup.colpkg \
  --work-dir /private/tmp/calibration/runs \
  --output /private/tmp/calibration/final-fit.json \
  --scenario final-fit
```

Repeat in fresh processes for `early-abort` and `fold-start`. For `sse-ui`, also
pass `--sse-module /path/to/fsrs_calibration.py`. The tool rejects live-profile
backup/work/output paths. Run baseline and updated samples sequentially, avoiding
other benchmarks and builds during the measured cases.

Small initial save batches greatly increased transaction overhead. The final
implementation uses up to 10,000 rows per transaction and sorts them by review ID
before saving, avoiding repeated cache-page writes for scattered card histories.
Each batch still releases the mutex and rechecks cancellation and collection
session. SSE's pending record ensures interrupted partial batches remain retryable.

## Verification

The core `just check` passed, including Rust regressions, Clippy, Python tests,
type checks and repository formatting/lint checks. An existing ignored third-party
benchmark scratch directory under `tmp/` was temporarily moved outside lint
traversal and restored afterward; its contents were preserved. SSE's complete
Jest suite passed: 49 suites, 376 tests. Its archive was checked against the changed
Python source and synced using `copy_to_anki.sh`; installed configuration and
metadata hashes were unchanged.
