# Anki statistics performance review — 27 September 2026

The largest measured opportunity is in Search Stats Extended (SSE): its historical calculations block the browser for several seconds. There are also avoidable full graph requests, repeated preset payloads, and lifecycle problems that can multiply work when the search or selected deck changes.

This was a review and profiling exercise. No production source, installed add-on, or live collection was changed.

## Measurements

Input: a temporary copy of `backup-2026-09-27-15.21.03.colpkg`, extracted under `/private/tmp/anki-stats-audit-eqv1ucpr`. It contains 31,665 cards and 282,160 review-log rows. SSE selects the 233,797 rows belonging to existing cards; 13,925 cards have review history.

The browser probe bundled the existing SSE calculation functions without changing them and ran them in local Playwright Chromium. These are computation timings, excluding data transfer, graph rendering, and Qt integration. The one-year sample uses an approximately 365-day cutoff, rather than the endpoint's additional rollover-hour offset. Figures are diagnostic observations, not a promised speedup or complete application load time.

| Browser calculation                            | One-year sample: 89,536 reviews | All history: 233,797 reviews |
| ---------------------------------------------- | ------------------------------: | ---------------------------: |
| Review statistics                              |                          1.34 s |                       2.61 s |
| Memorised history                              |                          2.35 s |                       5.05 s |
| FSRS calibration bins with bootstrap intervals |                          0.24 s |                       0.48 s |
| Total measured computation                     |                      **3.93 s** |                   **8.14 s** |

A 16 ms heartbeat fired **zero times** during either calculation sequence. The Memorised function performed all of its measured work synchronously before returning its promise. The calibration timing covers one FSRS series; an RWKV series is additional work.

The existing locally compiled core backend, called directly through the Collection API, took approximately 0.27 s for one-year graphs and 0.49–0.51 s for all-history graphs after warm-up. The first one-year call took 0.69 s. These core measurements exclude desktop RWKV preparation and HTTP/rendering time; they use the available build, without rebuilding the concurrently edited checkout.

## Recommended changes

### 1. Move SSE history calculation off the browser's main thread

[Review statistics](/Users/jschoreels/workspace/Anki-Search-Stats-Extended/src/ts/revlogGraphs.ts:409) and [Memorised history](/Users/jschoreels/workspace/Anki-Search-Stats-Extended/src/ts/MemorisedBar.ts:367) run directly from [Svelte stores](/Users/jschoreels/workspace/Anki-Search-Stats-Extended/src/ts/stores.ts:141). Declaring the second function `async` does not yield during its normal local calculation path.

Extract the calculations into a worker and discard obsolete jobs when the search changes. This primarily improves responsiveness; it does not by itself reduce CPU time. The CPU profile identifies the per-card, per-day forgetting-curve loop and FSRS7 curve calculation as major costs. Reusing model lookups and reducing repeated per-day bookkeeping are further CPU targets.

Both review statistics and Memorised history replay reviews, but they currently use different preset/filtering paths. Share work only after preserving those semantics; do not simply remove one replay. Keep existing card-level bootstrap semantics and statistical results intact.

### 2. Remove SSE's full graph request for suspended-card filtering

[The fetch interceptor](/Users/jschoreels/workspace/Anki-Search-Stats-Extended/src/ts/root.ts:60) makes **two** full graph requests per ordinary refresh, or **three** when SSE forces all-history while the native page requests one year. An isolated request probe confirmed both counts.

The extra `-is:suspended` response is consumed only for its interval histogram in [IntervalDistributionCategory](/Users/jschoreels/workspace/Anki-Search-Stats-Extended/src/ts/categories/IntervalDistributionCategory.svelte:12). SSE already downloads card type, queue and interval. Deriving that histogram from the existing card data matched the core result exactly on this backup, both with and without suspended cards. A Python comparison took 5–9 ms; that is an equivalence check, not a browser implementation benchmark.

Removing this request avoids another search, review-log scan, graph aggregation and RWKV preparation. Preserve the separate all-history request where its different history range is actually needed.

### 3. Batch preset resolution and deduplicate the response

[`fsrs_preset_data()`](/Users/jschoreels/workspace/Anki-Search-Stats-Extended/__init__.py:178) calls the backend once per card and repeats the complete preset for every card.

For 13,925 reviewed cards, the current JSON response was **16,488,760 bytes**, containing only **13 distinct complete preset values**. A dictionary of those exact values plus card-to-preset assignments was **266,116 bytes**, a **98.4% reduction** without dropping information.

Resolution plus Python conversion took approximately 19.0 s immediately after opening the collection, versus 0.33 s after core graph calls had warmed preset resolution. Actual screen impact depends on request ordering and cache state. The core already has [batch preset resolution](/Users/jschoreels/workspace/anki/rslib/src/scheduler/fsrs/preset.rs:313); expose and use that path for SSE, while retaining card-specific overlay/deck semantics.

### 4. Fix refresh callbacks and obsolete asynchronous results

Two independent problems were reproduced:

- [`new_refresh()`](/Users/jschoreels/workspace/Anki-Search-Stats-Extended/__init__.py:73) connects another `loadFinished` callback on every refresh. Running the actual function against a Qt signal produced 1, 2, 3, then 4 script injections on successive loads. Keep one owned callback per webview and supply current configuration. This probe demonstrates repeated injections; it did not assert that every repeated script successfully mounts another UI.
- [Card/review stores](/Users/jschoreels/workspace/Anki-Search-Stats-Extended/src/ts/stores.ts:120) and subsequent historical/calibration jobs use unguarded `.then(set)`. In a response-order probe using the existing stores, search B displayed card 2, then the older search A response replaced it with card 1. Add generation checks throughout the dependency chain, with cancellation where supported. This avoids incorrect graphs as well as wasted recomputation.

The native page's RWKV retries also pass through SSE's interceptor. Even when the search is unchanged, SSE resets stores, searches cards again, and publishes another card-ID array. Reuse current SSE data for an unchanged request within the same collection/data generation; a text-only permanent cache would become stale after reviews or other mutations.

### 5. Let ordinary core graphs appear while RWKV is pending

[The desktop graphs handler](/Users/jschoreels/workspace/anki/qt/aqt/mediasrv.py:1372) prepares RWKV scores before invoking core graph aggregation. During an existing warm-up, [preparation can wait](/Users/jschoreels/workspace/anki/qt/aqt/rwkv_scheduler.py:6112) for the configured **120 seconds**. This maximum is established from the code, not from a measured two-minute stall.

For ordinary searches, return available graphs promptly and update the RWKV section when scores are ready. The page already supports a pending header and two-second retries, but [currently retries and reapplies the whole response](/Users/jschoreels/workspace/anki/ts/routes/graphs/WithGraphData.svelte:238). Coordinate this with SSE's unchanged-request handling so a retry does not restart historical analysis.

Searches containing RWKV predicates require the correct score snapshot before their membership can be determined. Preserve that constraint, as well as current generation checks and per-search snapshot isolation. Filtered-deck preparation has different correctness requirements and should not inherit a presentation-only shortcut.

## Suggested order

1. Fix the accumulating callback and obsolete-response handling; remove the redundant suspended histogram request.
2. Batch and deduplicate preset data.
3. Move history and bootstrap computation into a worker, then optimize the measured CPU hotspots.
4. Decouple ordinary core graph display from RWKV warm-up and prevent full SSE recomputation on retries.

## Verification and limits

- Ran collection-copy backend timings, payload-size/equivalence checks, a CPU profile, and isolated Chromium calculation/heartbeat measurements.
- Ran the actual SSE refresh function with a Qt signal and existing SSE request/store code with controlled responses.
- Confirmed both suspended-card histogram variants exactly match the core on the copied collection.
- Did not profile a warmed live RWKV model or the user's running stats dialog. The backup does not include the profile-local RWKV sidecar/state cache.
- Did not run build or full test suites: no production source changes were made. Existing unrelated checkout changes were preserved.
- Temporary inputs, bundled diagnostic functions, the browser probe, CPU profile and result JSON files remain in `/private/tmp/anki-stats-audit-eqv1ucpr` for follow-up measurements.
