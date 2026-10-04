# Stats performance implementation — 27 September 2026

Implemented in the local Anki and Search Stats Extended repositories. SSE was
built, packaged, and synchronized to installed add-on `1339555413`. Its settings,
metadata, and user files were verified unchanged. Fully quit and reopen Anki to
load the Python changes. Core Anki changes are in the local development build.

## Production benchmark results

Three alternating pairs per scenario on the same copied backup used in the
initial audit: 31,665 cards and 233,797 reviews belonging to existing cards.
Each comparison checked complete calculation output, including sparse arrays
and their custom keys. No live collection was opened or modified.

| Measurement                                                  |         Original |   Implemented |
| ------------------------------------------------------------ | ---------------: | ------------: |
| One-year history: longest browser stall (median)             |         3,863 ms |         84 ms |
| One-year history: calculation completion                     |         3,863 ms |      4,165 ms |
| All history: longest browser stall (median)                  |         7,831 ms |        208 ms |
| All history: calculation completion                          |         7,831 ms |      8,624 ms |
| Cold preset endpoint                                         |        16,544 ms |        769 ms |
| Preset response size                                         | 16,488,760 bytes | 266,061 bytes |
| Normal native graph requests for SSE                         |                2 |             1 |
| Native requests when SSE forces all history                  |                3 |             2 |
| Script injections across ten stats refreshes                 |               55 |            10 |
| First ordinary graphs with simulated two-second RWKV warm-up |         2,733 ms |        652 ms |

Worker calculations keep the browser responsive; copying inputs/results adds
some completion time. Preset timing describes a cold collection-level resolver
cache; the original audit found a smaller benefit when another graph request had
already populated that cache. The RWKV experiment uses real graph aggregation
with a controlled warm-up double, not a measurement of model inference. RWKV
scores finish appearing after approximately 2.95 seconds because the page keeps
its existing two-second retry interval.

## Changes

- Anki serves ordinary statistics while RWKV warms up. Score-dependent searches,
  Browser searches, and filtered-deck preparation retain their waiting behavior.
- SSE calculates history, FSRS Memorised series, and calibration bootstrap bins
  in cancellable workers served by Anki's add-on endpoint.
- SSE derives interval histograms from already-loaded cards, preserving review,
  relearning, buried, and suspended-card semantics.
- Presets are prepared in one native batch and transported as complete unique
  settings plus a per-card index, preserving card/deck overrides.
- Asynchronous stores ignore obsolete responses. Pending RWKV retries reuse
  current history data; ordinary refreshes reload it. RWKV calibration reloads
  predictions when warm-up completes.
- Each stats window owns one refresh callback. Build/package rules include the
  worker asset.

## Verification

- Anki `just check`: passed, including relevant Python regression tests.
- Anki browser suite: 38 passed, one skipped. Ordinary graphs remained visible
  while the pending RWKV response was retried and the loading indicator cleared.
- SSE build and type checking: passed. The existing vendored Anki SvelteKit
  configuration warning remains non-fatal.
- SSE unit suite: 197 passed across 33 suites, including concurrent daily-graph
  work preserved in the shared repository.
- Production worker: exact result parity in all six paired history comparisons;
  same-origin content security policy and cancellation checks passed.
- Production SSE interface smoke: 80 cards, 2,750 reviews, nine completed worker
  jobs, rendered graphs, no browser errors. A pending retry reused cards/history;
  a fresh refresh reloaded them; RWKV calibration refreshed newly available data.
- Production refresh callback: ten loads produced ten injections and retained
  one owned Qt signal connection.
- Archive and installed runtime bytes match; settings and user files are intact.

Detailed production scripts, raw results, logs, and the UI screenshot are saved
in `/private/tmp/anki-stats-implementation-20260927/`. Earlier methodology and
baseline measurements remain in [benchmark-results.md](benchmark-results.md).

No commits or pushes were made. Unrelated reviewer and SSE daily-graph changes
were preserved.
