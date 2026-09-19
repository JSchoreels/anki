# FSRS review queue performance

Run `just fsrs-queue-bench` to measure queue construction and answer-to-next-card
backend work on synthetic, in-memory collections. The recipe runs an ignored
Rust benchmark test in the release profile; it never opens a user profile.
Run `just fsrs-queue-profile` for instrumented stage timings and paired
comparisons of candidate reads, preset allocation, sorting algorithms, and
load-balancer setup.
Run `just fsrs-queue-sample` for 200 consecutive FSRS-7 ascending-R rebuilds of
a 100,000-card fixture. It prints `queue_sampling_ready,pid,<PID>` after setup;
attach a native CPU sampler to that PID. On macOS, a five-second `sample` run at
the default one-millisecond interval is enough to collect several thousand
stacks. Exclude the idle Rust test-runner thread when interpreting percentages.

## Workload

- 1,000, 10,000, 50,000, and 100,000 due review cards in one deck, with distinct
  notes and varied elapsed time, difficulty, slow stability, and fast stability.
- FSRS-6 and FSRS-7; due-date, random, ascending R, and descending R ordering.
- A daily limit of 200, default bury/mixing settings, and no
  add-on preset overlays or RWKV scores.
- One initial queue build and the median of five subsequent complete builds.
- The median of 25 Good answers followed by fetching the next card with its
  scheduling states. Setup and restoring cards to the due queue are untimed.
- A separate preparation breakdown for 100,000 cards, including card loading,
  per-card versus batch preset resolution, model construction, and FSRS-7
  retrievability math with a reused model. These are single-pass diagnostics,
  not additive timings of an instrumented queue build.

The collection is reused across sort/version cases. Original memory states,
due dates, and daily counters are restored between cases, and the initial queue
length is asserted. Every R-sort build scores the entire requested backlog,
then keeps the allowed 200 cards. The initial-build measurement is not a cold
disk-cache benchmark. The measurements exclude Qt rendering, Python/add-on
hooks, sync, and disk-backed transaction costs.

## Why pauses can occur between reviews

Ordinary answers update and retain the queue. They do not rescore the entire
backlog after every answer. With R ordering, however, a future intraday learning
card becoming due triggers a complete queue rebuild so it can join the other
due cards in R order. Queue-invalidating edits and day rollover also cause
rebuilds. Large backlogs can therefore cause intermittent pauses between
otherwise fast reviews.

In this fork, both selected FSRS versions use the exact Rust scoring path for
R-based review ordering. Switching to FSRS-6 does not switch this queue back to
the historical SQL approximation.

## First optimization: 2026-09-19

Apple M1 Max, 64 GB RAM, release profile. Baseline source: local commit
`6a504b350`; optimized source: this change. Values below are median complete
queue rebuild times from five samples, in milliseconds.

Fixture correction discovered during follow-up profiling: this first run
requested a 1,000,000-card preset limit, which Anki normalized to its default
200 because the supported preset range ends at 9,999. Daily counters and
updated memory states also carried across cases. All due cards were scored,
but at most 200 entered the queue, with fewer slots left in later cases. The
table remains a backlog-scoring comparison, not an unlimited-queue benchmark.
The current recipes use supported limits, assert queue lengths, and restore
fixture state between answer benchmarks.

| Due cards | Ascending R before | Ascending R after | Descending R before | Descending R after |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 8.67 | 2.37 | 8.77 | 2.28 |
| 10,000 | 95.95 | 24.34 | 87.94 | 24.81 |
| 50,000 | 488.18 | 139.57 | 480.91 | 139.45 |
| 100,000 | 919.61 | 294.12 | 935.46 | 298.49 |

These FSRS-7 workloads improve by approximately 3.1–3.9 times. The 100,000-card
FSRS-6 ascending/descending cases improve from 1,004.78/990.48 ms to
303.50/297.47 ms. Due-date ordering at 100,000 cards remains around 68–70 ms.
Median answer-to-next-card backend work with a retained queue is around
0.07–0.08 ms both before and after; this is not a measurement of visible UI
latency or disk-backed answer commits.

The separate 100,000-card preparation diagnostic measured:

| Operation | Time (ms) |
| --- | ---: |
| Load every card once | 97.14 |
| Resolve presets individually | 587.25 |
| Resolve presets in a batch | 32.26 |
| Construct a model for every card | 9.03 |
| Compute FSRS-7 R with one reused model | 5.37 |

Preset resolution dominates the original scoring path, rather than FSRS-7's
forgetting-curve math. Results will vary with hardware, storage, backlog shape,
and overlay complexity. This run does not quantify performance with real
user collections or complex overlay rules.

## Implementation

Each queue build reads the due candidates and their required scheduling state
together using an 11-column projection of the scheduling index query. Separate
day and second cutoffs preserve review/interday
and intraday/preview eligibility. The SQL query does not first sort by due date,
and scoring does not fetch every card again by ID. Future intraday cards remain
in the separate learning queue. The build resolves presets through the existing
batch resolver and creates one FSRS model per distinct preset. It
shares immutable preset data within the batch, deriving it once per home deck
or add-on preset rather than copying the full preset for each card. It
uses the full stored memory state and fractional elapsed time. Both R directions
and relative overdueness preserve the previous finite-value checks, fallback
for cards without memory state, hash/id tie-breaks, sibling handling, and limits
applied after sorting. A dedicated queue-state type avoids constructing partial
`Card` objects. Preset inputs contain only card ID and home-deck ID. The shared
card-data JSON decoder retains its existing malformed-data defaults.
The model map lives only for the current build; no score
or model cache needs invalidation across reviews or configuration changes.

An attempted bulk card read using the existing order-preserving temporary
search table reduced the benefit at 100,000 cards. Loading each card once via
the cached read statement was the first optimization. The current direct
scheduling-index read avoids both the temporary table and individual lookups.

## Follow-up profiling and bulk-read optimization

The corrected general benchmark, with a 200-card daily limit and restored
fixture state between cases, produced these FSRS-7 median rebuild times. All
three orders below were measured in the same run with the bulk-read change,
before the shared-preset follow-up below:

| Due cards | Random (ms) | Ascending R (ms) | Descending R (ms) |
| ---: | ---: | ---: | ---: |
| 1,000 | 0.65 | 1.42 | 1.42 |
| 10,000 | 5.38 | 13.25 | 13.30 |
| 50,000 | 27.89 | 71.46 | 73.68 |
| 100,000 | 57.37 | 152.64 | 155.78 |

At that stage, Random was approximately 2.2–2.7 times faster at rebuilding this
workload. At 100,000 cards, exact R ordering added roughly 95–98 ms per rebuild.
Random uses
SQL `ORDER BY random()` and avoids full-state decoding, preset resolution, and
FSRS scoring for every candidate. It still scans/orders the due backlog and
shares queue preparation costs, so it is not constant-time. Due-date ordering
in the same FSRS-7 100,000-card run took 66.28 ms. FSRS-6 produced similar
100,000-card results: 57.77 ms random, 151.81 ms ascending R, and 152.23 ms
descending R.

Random also keeps newly due intraday learning cards in the separate learning
queue; their arrival does not trigger the full retrievability re-sort described
above. This comparison measures rebuild cost, not how often rebuilds occur
during a real review session.

Retained-queue answer-to-next-card work remained around 0.07–0.08 ms. The
general benchmark and the separate profiling workload have different setup
and allocation histories; their numbers are not interchangeable exact totals.

The profiler emits nested, per-build timings for preparation, candidate
gathering, score preparation/calculation, final sorting, and enqueueing. It
checks the actual retained queue size at both 200 and 9,999 reviews/day. The
table compares the first optimization with the subsequent direct bulk read;
values are medians of five complete FSRS-7 ascending-R builds in milliseconds.

| Due cards | Daily limit | Per-card read version | Bulk-read version |
| ---: | ---: | ---: | ---: |
| 10,000 | 200 | 22.43 | 13.19 |
| 10,000 | 9,999 | 23.19 | 13.94 |
| 100,000 | 200 | 291.18 | 151.95 |
| 100,000 | 9,999 | 263.72 | 153.01 |

For 100,000 due cards at the 200-card limit, the measured stages were:

| Stage | Per-card read version (ms) | Bulk-read version (ms) |
| --- | ---: | ---: |
| Queue preparation | 25.30 | 25.04 |
| Candidate gathering | 61.68 | 64.69, including card decoding |
| Individual card lookups/decoding | 139.91 | Removed |
| Copy stateful cards for preset resolution | 3.64 | 2.62 |
| Batch preset resolution | 27.11 | 25.77 |
| Score computation, including model lookup | 23.51 | 20.32 |
| Final R sorting | 6.99 | 7.57 |
| Apply limits and enqueue | 0.28 | 0.27 |

Stage medians do not sum exactly to the total. Allocation cleanup, final queue
construction, and trace output also contribute. These are elapsed-time phase
measurements, not a statistical CPU-sampling profile.

The paired candidate-read experiment alternates the two methods over ten
samples and compares every returned card field. In the final run, 100,000
cards took 227.67 ms with the previous gather-plus-individual-read method and
69.40 ms with the direct bulk read. An earlier paired run measured
176.23/62.46 ms; the whole-build results above are more useful than treating
either isolated measurement as a fixed prediction.

Two other experiments:

- Stable versus unstable sorting of the actual scored candidate records:
  7.39 versus 6.67 ms for 100,000 cards, with identical resulting IDs. The
  approximately 0.7 ms saving was initially left out, then adopted in the
  small-optimization follow-up below.
- Queue preparation with load balancing enabled versus disabled: 27.34 versus
  5.30 ms. This attributes roughly 22 ms to load-balancer initialization on this
  workload. Load balancing remains enabled in the full benchmarks and normal
  behavior is unchanged.

## Shared presets and smaller allocation improvements

A fresh profile compared the bulk-read version with an additional small set of
changes: reserve the preset result map's capacity, derive built-in presets once
per home deck, share immutable preset data across cards using `Arc`, and use
unstable sorting. The existing hash/card-ID tie-breaks give distinct cards a
total order, so unstable sorting produces the same card sequence. The shared
preset map is local to each request; no scores or built-in presets are cached
across builds. Deck-specific retention overrides and original/home deck routing
are preserved.

The first trial reserved capacity and reused derivation but still copied each
full preset. This saved about 4 ms in an alternating preset-only comparison,
without a consistent whole-build improvement. Sharing the preset itself removed
the repeated parameter/string allocations; measured score computation time also
fell from about 17 to 11 ms. It is the material improvement in the final variant.

Complete FSRS-7 ascending-R builds, median of five, in milliseconds:

| Due cards | Daily limit | Before shared presets | After shared presets |
| ---: | ---: | ---: | ---: |
| 10,000 | 200 | 12.71 | 10.64 |
| 10,000 | 9,999 | 13.92 | 11.93 |
| 100,000 | 200 | 145.56 | 112.98 |
| 100,000 | 9,999 | 146.16 | 114.16 |

This reduced whole-build time by about 22% at 100,000 cards. The alternating
ten-sample comparison of old preset construction and the final resolver measured
20.75 versus 5.22 ms for 100,000 cards, and 1.71 versus 0.43 ms for 10,000 cards.
The fixture has no overlay rules; both paths return the same preset IDs,
parameters, and retention targets. Stable versus unstable sorting measured
6.45 versus 5.75 ms, with identical card IDs. These isolated phase savings are
not additive predictions for every collection.

The shared-preset general benchmark measured these FSRS-7 rebuild medians, with the
same 200-card daily limit and restored fixture state:

| Due cards | Random (ms) | Ascending R (ms) | Descending R (ms) |
| ---: | ---: | ---: | ---: |
| 1,000 | 0.66 | 1.20 | 1.21 |
| 10,000 | 5.32 | 10.57 | 10.76 |
| 50,000 | 28.32 | 55.72 | 57.01 |
| 100,000 | 57.26 | 114.73 | 118.95 |

Compared with the preceding general benchmark, this is a roughly 24–25%
reduction at 100,000 cards. R rebuilds now cost about twice Random on this
fixture, instead of 2.7 times. Retained-queue answer-to-next-card medians remain
around 0.07–0.08 ms. These are backend measurements, not visible UI latency.

## CPU sampling and further allocation reductions

A fresh five-second native sample captured 4,038 stacks on the active benchmark
thread. The main Rust test thread was waiting and is excluded from these shares:

| Work | Active-thread samples | Share |
| --- | ---: | ---: |
| Full candidate read and decoding, inclusive | 2,196 | 54.4% |
| JSON column conversion, inside that read | 773 | 19.1% |
| Load-balancer initialization, inclusive | 665 | 16.5% |
| SQLite VM execution, self time across queries | 883 | 21.9% |
| `powf`, self time | 107 | 2.6% |

Rows overlap and must not be added. Optimized symbols contain inlined and
deduplicated functions, so this is an approximate attribution. It agrees with
elapsed-time profiling: full candidate reads take about 63 ms, including roughly
22 ms of JSON decoding, while queue preparation takes about 23 ms before the
following allocation changes.

Two small changes were retained:

- Load-balancer initialization moves each already-grouped card vector into its
  day cache and builds the sibling-note set with a known iterator length,
  avoiding a second card-vector allocation and incremental set growth. Queue
  preparation dropped from 22.99 ms to 21.09 ms in the first comparison; a later
  run measured 22.10 ms. Day loads and sibling removal behavior are covered by
  a regression test spanning multiple decks and presets.
- When every due candidate has FSRS state, score preparation borrows the
  existing card slice. Mixed collections still select only stateful cards for
  preset resolution and preserve the memoryless fallback. The selection/copy
  phase dropped from 2.42 ms to 0.30 ms on the all-stateful fixture.

An alternating ten-sample JSON experiment compared parsing bytes with validating
UTF-8 once and parsing text. The first run measured 22.38/21.75 ms; a repeat
measured 22.43/22.50 ms. That alternative was not adopted because it showed no
reliable benefit. The production JSON parser and its malformed-data behavior are
unchanged.

General benchmark after the allocation changes above, median of five complete
FSRS-7 builds with a 200-card
daily limit, in milliseconds:

| Due cards | Random | Ascending R | Descending R |
| ---: | ---: | ---: | ---: |
| 1,000 | 0.64 | 1.28 | 1.19 |
| 10,000 | 5.02 | 10.19 | 10.33 |
| 50,000 | 26.37 | 52.46 | 53.87 |
| 100,000 | 56.36 | 109.94 | 112.83 |

The preceding general run measured 114.73/118.95 ms for R at 100,000 cards, so
this run is about 4–5% lower. This is a modest change with visible run-to-run
variation: the separate instrumented 200-card-limit profile measured
113.51 ms before these changes and 115.75 ms after; at a 9,999-card limit it
measured 115.10/113.24 ms. Reduced copying is clearer than the exact end-to-end
speedup, and these timings do not support another large performance claim.
Retained-queue answers remained around 0.07 ms.

## Narrow scheduling-state reads

The next change reduces the candidate query from 18 columns to 11. A dedicated
`DueCardWithState` keeps queue metadata, interval, original due date, full memory
state, desired retention, and last-review time. It uses the unchanged `CardData`
JSON decoder, including defaults for malformed fields and whole invalid JSON.
Preset resolution now takes compact card-ID/home-deck inputs; existing full-card
callers use a wrapper. Memoryless candidates are still excluded from preset
resolution, and filtered cards still route through their original deck.

The candidate-read benchmark now compares the previous full-card bulk read with
this narrower read, alternating order over ten samples and asserting every
retained scheduling field against the full-card projection:

| Due cards | Full-card read (ms) | Narrow read (ms) | Reduction |
| ---: | ---: | ---: | ---: |
| 10,000 | 6.16 | 5.15 | 16% |
| 100,000 | 63.47 | 52.10 | 18% |

Instrumented ascending-R complete builds measured 9.40/10.87 ms at 10,000 cards
and 99.32/99.56 ms at 100,000 cards, with daily limits of 200/9,999 respectively.
At 100,000, candidate gathering took about 52.74 ms, compact preset input
selection 0.23 ms, preset resolution 5.04 ms, score calculation 10.74 ms, and
sorting 6.58 ms. These are nested phase medians across both limits, not additive
independent costs. The full-card comparison above is paired within the same run;
whole-build comparisons with earlier sections come from separate runs.

Complete FSRS-7 build medians from the general benchmark, with a 200-card daily
limit, in milliseconds:

| Due cards | Random | Ascending R | Descending R |
| ---: | ---: | ---: | ---: |
| 1,000 | 0.64 | 1.08 | 1.10 |
| 10,000 | 5.04 | 9.42 | 9.54 |
| 50,000 | 26.97 | 48.77 | 50.21 |
| 100,000 | 57.04 | 101.34 | 103.76 |

Compared with the previous 109.94/112.83 ms R medians at 100,000, these are
7.8%/8.0% lower. Random remains about 1.8 times faster for complete rebuilds,
with exact R adding roughly 44–47 ms. Normal retained-queue answer transitions
still take around 0.07–0.08 ms of backend work. The tests use synthetic in-memory
collections, so these are not visible UI or real-collection latency estimates.

Regression coverage compares narrow and full decoding of complete/legacy FSRS
state, invalid JSON/fields, numeric casts and defaults, and all eligible queue
kinds. Queue-order tests cover both R directions and relative overdueness with
mixed FSRS versions, overlays, child decks, original-deck retention overrides,
card retention overrides, and memoryless fallback. Existing queue tests cover
limits, siblings, and learning-card handling.

A fresh five-second native sample of 200 consecutive 100,000-card rebuilds
contained 4,079 active benchmark-thread samples (excluding the idle test-runner
thread):

| Work | Active-thread samples | Share |
| --- | ---: | ---: |
| Narrow candidate read and decoding, inclusive | 2,188 | 53.6% |
| JSON column conversion, inside that read | 911 | 22.3% |
| Load-balancer initialization, inclusive | 666 | 16.3% |
| SQLite VM execution, self time across queries | 924 | 22.7% |
| `powf`, self time | 122 | 3.0% |

These overlapping sample counts identify remaining CPU hotspots; they do not
measure absolute savings or sum to a total. The paired read timings establish
the gain. SQL execution, row decoding, and JSON still dominate the workload.

## Further work

JSON decoding remains material: the same run measured 21.91 ms for 100,000
card-data values in isolation. A different JSON library or custom parser should
only follow a clear measured advantage and compatibility checks.

Another target is reusing load-balancer state when a queue rebuild
only admits newly due learning cards. Such reuse must still account for
answers, other card changes, deck/configuration changes, and rollover. A real
collection profile with its actual preset overlays would establish whether
this roughly 17–19 ms synthetic initialization cost is material before
introducing another cache. The fixture puts every review due today, so it counts
all of them in load balancing; a backlog overdue before today can have a lower
load-balancer cost.

Reusing R scores across rebuilds or only inserting newly due learning cards
needs a separate behavior decision: elapsed time changes R, and FSRS-7 curves
with different states can change their relative order. A stale score cache is
not an exact replacement for the current full rescore. Likewise, applying the
daily limit before scoring can select the wrong cards.
