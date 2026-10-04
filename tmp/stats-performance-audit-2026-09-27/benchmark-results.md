# Before/after benchmarks — Anki statistics and SSE

27 September 2026. These are isolated prototypes; production source and the installed add-on remain unchanged.

## Results

Medians from alternating baseline/prototype runs, on the same copied collection.

| Change and measured quantity                                    |               Current |             Prototype | Interpretation                              |
| --------------------------------------------------------------- | --------------------: | --------------------: | ------------------------------------------- |
| Cold preset resolution and encoding                             |               17.29 s |               0.786 s | **22.0× faster; 95.5% less time**           |
| Preset response size                                            |              16.49 MB |              0.266 MB | **98.4% smaller**                           |
| Browser preset parsing and map reconstruction                   |               23.4 ms |                5.7 ms | **4.1× faster**                             |
| Core graph work, ordinary one-year refresh                      |   932 ms / 2 requests |    668 ms / 1 request | **28.3% less backend work**                 |
| Core graph work, SSE forces all-history                         | 1,651 ms / 3 requests | 1,165 ms / 2 requests | **29.4% less backend work**                 |
| Main-thread pause, one-year calculations                        |              3,995 ms |                 81 ms | **98.0% shorter**                           |
| Main-thread pause, all-history calculations                     |              8,124 ms |                212 ms | **97.4% shorter**                           |
| Script injections across ten deck refreshes                     |                    55 |                    10 | **81.8% fewer**                             |
| Stale final results in 100 deliberately reordered request pairs |                   100 |                     0 | Correct final result in all prototype cases |

The graph-request prototype also needs **1.4 ms** median browser time to derive the interval histogram from already-loaded card data. Adding this to the candidate times does not materially change the percentages. This is core backend work avoided, not a measurement of complete screen load time or RWKV inference savings.

### Worker tradeoff: responsiveness improves, completion takes slightly longer

| History                      | Main-thread completion | Worker completion, including input/output copies | Change |
| ---------------------------- | ---------------------: | -----------------------------------------------: | -----: |
| One year: 89,829 reviews     |                3.995 s |                                          4.355 s |  +9.0% |
| All history: 233,797 reviews |                8.123 s |                                          8.771 s |  +8.0% |

The worker uses the unchanged review, Memorised and calibration calculations. It copies the full fixture into the worker and returns the full calculation results. Worker initialization is measured separately and excluded from these per-request timings; the worker is reused. No dataset residency, compact transfer format or CPU algorithm optimization was added.

The 16 ms main-thread heartbeat never ran during baseline calculations. In the worker variant it ran a median 264 times for the one-year case and 529 times for all history. The reported pause is the median of each run's maximum heartbeat gap. The worst observed worker gaps were 86 ms and 242 ms respectively, so transfer overhead still causes short pauses.

Every numeric and enumerable result property matched exactly, including sparse arrays and card-ID array properties: 1,075,923 compared values for each one-year pair and 2,714,056 for each all-history pair. All six pairs passed.

### Preset tradeoff: cold versus warm

The cold prototype first calls the existing `card_memory_metrics(..., include_retrievability=True)` API, which uses the native batch preset resolver, then performs the existing per-card preset reads and deduplicates the response by complete preset value. The measurement includes that preliminary API call and the subsequent per-card calls. It does **not** claim to benchmark an unimplemented bulk-preset endpoint.

Cold means a freshly opened Collection with an empty resolver cache; filesystem pages can already be cached. There are 13,925 reviewed cards and 13 distinct complete preset values. All reconstructed preset maps matched the baseline exactly.

- Cold baseline range: 16.70–17.89 s. Prototype range: 0.769–0.827 s.
- With the resolver already warm, server work was **315 ms baseline versus 323 ms compact**. Deduplication adds about 8 ms median server work rather than improving it.
- Exact server JSON sizes were 16,488,760 and 266,061 bytes. Browser parsing includes rebuilding the card-to-preset lookup for the compact form; eleven alternating pairs passed equivalence checks.

The large cold gain will not occur on every statistics refresh: the native graph request can already warm the resolver before SSE needs presets. A dedicated bulk-preset API could avoid the remaining per-card calls, but that additional gain has not been measured here.

### RWKV pending state: controlled experiment

This test uses the actual desktop graph handler, preparation control flow, and warm-up waiter, with a **synthetic two-second pending warm-up**. Model/state helpers are controlled substitutes, and core graph aggregation runs against the real backup copy. It does not benchmark real RWKV model loading or scoring.

| Event                                                    | Current blocking preparation | Return pending immediately |
| -------------------------------------------------------- | ---------------------------: | -------------------------: |
| First ordinary graphs available                          |                      2.737 s |                    0.667 s |
| Final ready response with current two-second retry delay |                      2.737 s |                    2.964 s |

The candidate shows ordinary graphs about 2.07 seconds sooner in this scenario, but the existing polling interval makes the final response about 0.23 seconds later. Ordinary card counts and interval histograms matched. Searches requiring RWKV scores were deliberately excluded: returning unscored membership for those searches would be incorrect.

### Refresh and obsolete responses

The Qt probe runs SSE's actual refresh function against a real Qt signal and a recording webview substitute. The prototype disconnects its previous callback before connecting a replacement. Ten refreshes consistently produced 55 versus 10 injections over five pairs, with 10 versus 1 connections remaining. JavaScript parsing/rendering is not timed by this probe, so the injection counts are the meaningful result, not its small Python dispatch time.

The response-order probe bundles the actual SSE card-data store, with only a cleanup/generation guard added in the prototype. It forces the newer search response to arrive before the older one. Across 100 pairs, applied results fell from 200 to 100 and stale final results from 100 to zero. This verifies the guard for that store; the same ownership pattern still needs implementation and testing throughout the complete production dependency chain.

## Method and scope

- Source backup: `backup-2026-09-27-15.21.03.colpkg`, copied and extracted previously under `/private/tmp/anki-stats-audit-eqv1ucpr`.
- Data: 31,665 cards, 282,160 total review rows, 233,797 review rows belonging to existing cards. The one-year browser slice uses a fixed backup timestamp and SSE's rollover-hour offset.
- Browser: the repository's local Chromium headless shell, without artificial CPU throttling. Same calculations and input values in both variants.
- Three alternating pairs for worker and cold-preset comparisons; five for graph-request and warm-preset comparisons; eleven for exact JSON parsing. Timed workloads ran serially.
- Production checkouts: Anki local HEAD `f94280eca7b9853e46022b004c64ca7380334ae8` with pre-existing unrelated changes; SSE local HEAD `64446a93474b1189499e9e86ae32072a69e13148`, clean. This records local state and makes no remote-branch freshness claim.
- Native API timings use the existing compiled Anki backend. It was not rebuilt from the concurrently edited checkout. Browser and orchestration probes use the inspected source directly.
- Warm server figures sum the separately measured resolver and encoder phases, excluding the validation assertion that follows them.
- No live profile, installed add-on or production source was modified. The collection API only opened the disposable backup copy.
- No whole-application total or combined speedup is claimed. Component savings overlap through caching and concurrency and must not simply be added together.
- These are benchmark/equivalence probes. Full repository build/test suites were not run because there is no production patch.

## Recommendation from the measurements

First remove the unnecessary full graph request and fix refresh/result ownership: their benefits are consistent and their scope is small. Deduplicate preset data and use batch resolution where the cache is cold. Move historical computation to a worker to keep the screen responsive, accepting its measured transfer overhead until a compact/resident-data design is tested. Treat early RWKV graph display as a responsiveness change, with polling behavior included in its eventual implementation tests.

## Reproduction and raw data

The local benchmark recipes are in [benchmarks/justfile](/Users/jschoreels/workspace/anki/tmp/stats-performance-audit-2026-09-27/benchmarks/justfile). They depend on the retained temporary input directory and existing local dependencies. Run all workloads serially:

```sh
rtk proxy just --justfile /Users/jschoreels/workspace/anki/tmp/stats-performance-audit-2026-09-27/benchmarks/justfile all
```

Chromium needs permission to launch outside the filesystem sandbox on this machine. Individual recipes are `browser`, `backend`, `payload`, `lifecycle`, `stale`, and `pending`.

Raw results: [browser](/Users/jschoreels/workspace/anki/tmp/stats-performance-audit-2026-09-27/benchmarks/browser-results.json), [backend](/Users/jschoreels/workspace/anki/tmp/stats-performance-audit-2026-09-27/benchmarks/backend-results.json), [exact payload parsing](/Users/jschoreels/workspace/anki/tmp/stats-performance-audit-2026-09-27/benchmarks/payload-results.json), [Qt refreshes](/Users/jschoreels/workspace/anki/tmp/stats-performance-audit-2026-09-27/benchmarks/lifecycle-results.json), [stale responses](/Users/jschoreels/workspace/anki/tmp/stats-performance-audit-2026-09-27/benchmarks/stale-results.json), and [controlled pending state](/Users/jschoreels/workspace/anki/tmp/stats-performance-audit-2026-09-27/benchmarks/pending-results.json).
