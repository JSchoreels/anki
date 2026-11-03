# FSRS prediction and optimization follow-up profile

On 2026-11-03, release-LTO macOS arm64 builds were measured against the same
copied Yomitan vocabulary deck as the [compact-history benchmark](fsrs-compact-performance.MD).
The deck has 11,744 cards, 188,183 rated reviews and 173,683 eligible final-fit
targets. The separate Yomitan Kanji deck is excluded. Every run applies one
FSRS-7 Yomitan parameter vector to the whole deck, although 2,354 of its cards
use other presets in the copied profile; routing among presets is not measured.
Same-day reviews are included, with one relearning step, scheduling penalties
and Dynamic DR disabled. Each sample uses a fresh process and a fresh copy of
the imported collection. Timing excludes copying and opening it, while peak RSS
includes the whole process and collection open.
In that copied profile, Dynamic DR was disabled in the resolved presets of all
11,744 cards in the deck, checked with `fsrs_preset_for_card()` on a separate
temporary collection copy.

## Original histories, Anki adapter and fsrs-rs prediction API

The original build predates #469 and the Anki compact-history adapter. The
adapter and fsrs-rs API paths run in the same instrumented binary; the original
path uses its preserved earlier binary. Each figure is the median of six runs,
with execution order rotated and reversed. The API's incremental effect is the
comparison with the adapter; the original-to-API comparison includes both #469
and Anki's adoption of compact histories.

| Workload | Original time | Adapter time | API time | Original peak RSS | Adapter peak RSS | API peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Final-fit prediction rebuild | 799.79 ms | 756.76 ms | 764.80 ms | 173.29 MiB | 111.79 MiB | 111.91 MiB |
| Final fit and five validation folds | 1,630.24 ms | 1,525.68 ms | 1,520.14 ms | 236.62 MiB | 125.16 MiB | 127.09 MiB |

The final-fit and validation-fold outputs have identical ordered checksums
across all paths, with 173,683 and 318,419 rows respectively. The original-to-API
memory difference is substantial; these samples do not establish a material
end-to-end improvement for the API over the adapter. Timings were variable: for
example, final-fit API samples ranged from 707 to 1,284 ms. Cache writes remain
the largest measured phase of the rebuild, as in the
[earlier prediction profile](fsrs-compact-prediction-profile-2026-11-03.MD).

Five additional instrumented API runs put a final-fit call at a median of
41.75 ms: validation 0.28 ms, grouping 2.17 ms, sorting 0.35 ms, and walking
the card histories 38.29 ms. Per-card timers add some overhead. The grouping
vectors reserved about 3.97 MiB and the prediction vector about 0.66 MiB;
these are vector capacities, not total process memory or allocator overhead.
The walk dominates the API call, but the call is a small part of the complete
rebuild. Sorting or grouping alone therefore has limited end-to-end potential.

## Optimizer memory experiments

Two switches were compared in the same instrumented binary. One skips building
simulator statistics when Dynamic DR is disabled; the other records the health
check's target counts before moving the expanded items into time-series
evaluation, avoiding a second full copy. Each result is the median of six fresh
processes in rotated order. All optimization response digests, including the
health-check result, matched across the compared paths.

| Workload | Existing time | Both changes | Existing peak RSS | Both changes peak RSS |
| --- | ---: | ---: | ---: | ---: |
| Optimize, health check off | 371.06 ms | 326.18 ms | 166.87 MiB | 115.93 MiB |
| Optimize, health check on | 629.90 ms | 578.80 ms | 360.84 MiB | 287.49 MiB |

With the health check off, only the simulator change applies. With it on, the
simulator change alone measured 592.06 ms and 326.00 MiB; removing the extra
health-check copy alone measured 624.07 ms and 319.97 MiB. These are whole
process peaks, not sums of independently measured allocations. The existing
health-check path still expands histories and creates several time-series
splits, so a compact cross-validation API is a possible further investigation.

The changes above are Anki-side changes. They are separate from the
fsrs-rs prediction API and must not be attributed to that API or to #469 alone.
Dynamic DR's enabled path continues to derive and shape simulator statistics.

### Verification against the production build

The finished Anki changes were then compared with the preserved build from
`3c4a61048`, which already included #469 and the compact Anki adapter. The
release-LTO builds were run in alternating order for seven fresh-process pairs
per workload; no profiling switches were enabled. Each pair's complete
optimization response, including the optional health result, was byte-identical.

| Workload | Before time | After time | Before peak RSS | After peak RSS |
| --- | ---: | ---: | ---: | ---: |
| Optimize, health check off | 364.75 ms | 315.51 ms | 166.66 MiB | 114.19 MiB |
| Optimize, health check on | 619.14 ms | 560.84 ms | 361.42 MiB | 289.58 MiB |

The same Dynamic DR, scheduling-penalty, same-day and preset qualifications at
the top of this report apply. The Rust test suite also verifies that Dynamic DR
still receives review-derived simulator settings when enabled.

## Artifacts and reproducibility

[Raw samples](fsrs-optimization-profile-2026-11-03.json) contain timings,
peak RSS, stage measurements and output digests, without parameters or review
IDs. The original and compact histories were built from the same backup and
configuration described in the earlier benchmark. The compact prediction
comparison uses the same [profiling patch](fsrs-compact-prediction-profile.patch)
and branch `49fe84eaaced63bc5382e2d19a6ec5160b9df772` as the previous profile.

For the optimization experiment, apply [its profiling patch](fsrs-optimization-profile.patch)
to an isolated Anki checkout at `3c4a61048`, point its fsrs dependency to the
local compact-prediction branch, and build with release LTO. Set
`ANKI_FSRS_PROFILE_OPTIMIZATION=1` for stage output;
`ANKI_FSRS_SKIP_SIMULATOR=1` and `ANKI_FSRS_HEALTH_MOVE=1` select the two
experimental changes. For the prediction-stage breakdown, also apply
[the fsrs-rs profiling patch](fsrs-compact-api-phase-profile.patch) to the
compact-prediction branch and set `ANKI_FSRS_PROFILE_COMPACT_API=1`,
`ANKI_FSRS_PROFILE=1` and `ANKI_FSRS_UPSTREAM_PREDICTION=1`.
These patches and environment switches are for profiling, not production.
Use `just fsrs-compact-bench` on a copied collection and compare output
digests before interpreting performance.
