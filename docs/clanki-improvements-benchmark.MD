# Clanki improvements: implementation and benchmarks

Measured on 2026-10-04 with an Apple M1 Max, 64 GiB RAM, macOS, Python
3.13.13 and release builds. The baseline is fork commit
`47466a3f7a3f91a37f088f8b978bad4ce2a12993`; the comparison source is
[Clanki at 20a4e77a9](https://github.com/Expertium/Clanki/tree/20a4e77a9119c71cc4a8d51c2febe09c2f75e426).
The after measurements include the changes accompanying this report.

## Results

Values are medians in milliseconds. Browser operations use 21 samples with
actual Qt classes and synthetic cached data. Backend operations use seven
fresh collection copies each, with 31,665 cards and 16,209 notes. Preset saving
affects 11,740 cards, of which 10,172 have memory states. Asset and simulated
audio operations use 21 samples.

| Improvement / operation | Before | After | Result |
| --- | ---: | ---: | --- |
| Browser selected-row lookup: 20,000 rows, 5,000 selected IDs | 359.00 | 0.50 | 724× faster |
| Browser selection restoration, including current item | 364.50 | 0.74 | 492× faster |
| Modified-selection count: 5,000 rows × 8 columns | 125.39 | 0.09 | 1,393× faster |
| Browser cell metadata and flags: 41 rows × 8 columns | 1.79 | 1.29 | 1.39× faster |
| Browser delegate painting, including image reset and hash | 24.58 | 20.61 | 1.19× faster |
| Sidebar flags: 1,400 items | 3.16 | 0.56 | 5.66× faster |
| Sidebar section-root checks: 1,400 items | 4.77 | 0.12 | 39.88× faster |
| Sidebar filtering and expansion: 1,400 items | 10.38 | 4.83 | 2.15× faster |
| Deck due tree | 10.10 | 2.29 | 4.40× faster |
| Finished-study / congratulations information | 1.96 | 0.22 | 9.05× faster |
| Empty Cards scan, including shared card-generation improvements | 227.19 | 203.22 | 1.12× faster |
| Full Check Database, including batched card reads | 5,254.24 | 5,319.43 | No clear end-to-end gain |
| FSRS-7 preset save: memory computation and batched card reads | 306.64 | 262.37 | 1.17× faster |
| Representative warm webview page with six built-in assets | 23.30 | 11.66 | 2.00× faster; six asset requests → zero |
| Windows mpv command latency, simulated transport | 63.84 | 10.67 | 5.98× faster in the simulation |

The correctness fixes have behavioral outcomes rather than useful speed ratios:

| Fix | Verification |
| --- | --- |
| RWKV redo after undo | Redo clears the restored-card guard and requests the next card; unrelated redo leaves the current card alone. |
| Day rollover | Reviewer refresh and the add-on hook run on rollover; repeated checks on the same day do not repeat the hook. |
| Closed-window callbacks and notifications | Real offscreen widgets are deleted before the callback; geometry work exits safely and notifications fall back to an available window. |
| Browser average interval overflow | Two review cards with 30,000- and 32,000-day intervals produce the correct 31,000-day average. |

## Equivalence and compatibility

All eight Browser result checksums match, including the painted pixels. All
five backend operations return matching normalized outputs and retain the
same note/card counts. Preset saving produces identical persisted card-state
checksums, including FSRS-7 internal and fast stability, and unchanged
intervals, due dates and queues. Rescheduling is disabled for that benchmark.
Separate regression tests cover FSRS-6/7 serial versus parallel batches,
callback order, undo/redo and cancellation after an earlier batch has written.

The indexed deck-count implementation retains this fork's newer same-day
limit exemptions. Tests compare the original full query with the replacement,
including duplicate reviews, scheduler-day boundaries, non-answer entries
and filtered reviews without rescheduling. Maintenance tests retain duplicate
card repair, missing templates, card generation, note ordering and special-field
shadowing. Browser selection counting falls back to Qt when add-ons override
the model's flags.

Development assets, HTML pages, media and add-on files retain their existing
cache behavior and content security policy. The real browser experiment uses
the same off-the-record session profile for one cold load and subsequent warm
loads. Each warm load originally requested six assets and transferred 441,423
bytes; the modified server receives zero asset requests and transfers zero
asset bytes. The page itself is always fetched.

## Limits of the measurements

- Browser timings isolate the changed methods; they exclude collection
  search, complete window layout, add-ons and visible interaction latency.
- Backend fixture import, copying, opening, request construction and checksums
  are outside the timer. Fresh collection connections retain the OS filesystem
  cache. Each mutating operation receives a fresh closed-fixture copy.
- The final backend comparisons were repeated after a concurrent build ended.
  Full Check Database still varied substantially: the before interquartile
  range was 5,158–6,263 ms, and after was 4,936–5,495 ms. The 1% median difference
  is within that variation. Fewer per-note reads do not establish an overall
  speedup on this fixture.
- Preset saving has a modest measured gain and overlapping interquartile
  ranges: before 278–334 ms, after 238–283 ms. It measures recomputation without
  rescheduling, rather than every possible deck-options save.
- `ANKI_BENCH_NOW=1791064800` is identical in both runs. Only the deck-tree API
  accepts an explicit clock. Scheduler-day rollover is rejected; the
  congratulations checksum excludes the clock-dependent next-learning delay.
- Web timings cover an isolated representative page, not a complete Anki
  screen. Warm medians exclude neither startup outliers nor image/hash work
  where those are part of the measured operation.
- The Windows test uses the actual Python mpv reader and request-routing code
  with a simulated named pipe whose replies become ready after 10 ms. Requests
  are spread across the previous 100 ms polling window. All 21 responses route
  correctly. This is not native Windows or real mpv playback timing.

## Reproducing

Build the baseline and modified checkouts separately with `just build` using
`RELEASE=1`. A copied `out/` directory contains absolute build paths: regenerate
its build configuration before using it in another checkout. Backend benchmarks
reject a native library loaded from the wrong checkout.

Run these recipes from the modified checkout, replacing `SOURCE` with each
built checkout in turn. `BACKUP_COPY` must be an already-copied `.colpkg` under
`/private/tmp` or the workspace; the harness never opens a live profile.

```sh
just clanki-gui-bench --source-root SOURCE --output /private/tmp/gui.json --samples 21
ANKI_BENCH_NOW=1791064800 just clanki-collection-bench --source-root SOURCE --backup BACKUP_COPY --work-dir /private/tmp/clanki-bench --output /private/tmp/collection.json --samples 7
just clanki-asset-audio-bench --source-root SOURCE --output /private/tmp/assets.json --samples 21
```

The asset recipe needs macOS webview IPC access. If Chromium cannot start, it
records a headers-only fallback and does not claim measured browser speed.
The [benchmark summaries](clanki-improvements-benchmark.json) retain sample
times where available, quartiles and equivalence checks without collection
content or profile names.

## Verification

`just check` passed against the modified checkout, including the Rust,
Python and TypeScript test suites, type checks, lint and formatting checks.
The Qt regression suite exercises real deleted widgets offscreen; the cache
experiment uses a real QWebEngine page and local HTTP server. The Windows
transport remains covered by simulated pipe tests rather than native Windows.
