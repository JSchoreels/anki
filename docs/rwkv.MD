# RWKV Review Scheduler

The RWKV review scheduler is a desktop-only review-time scheduler. In RWKV
mode, answer buttons use RWKV-Curve intervals and store RWKV-Curve S90 values.
RWKV-Instant retrievability can also be used for review queue ordering when its
separate deck option is enabled. Mobile clients do not load the RWKV backend
and continue to schedule with FSRS/SM2.

## Settings And Storage

Desktop-only RWKV review settings are stored in deck config `other` under the
`jschoreels.rwkv` key. This includes `rwkv_review_enabled` and
`rwkv_review_batch_size`, which defaults to `512`, and
`rwkv_review_refresh_interval`, which defaults to `1`,
`rwkv_review_refresh_on_exit`, which defaults to `false`, and
`rwkv_review_allow_same_day_review`, which defaults to `false`, and
`rwkv_review_instant_order_enabled`, which defaults to `false`, and
`rwkv_review_dynamic_preset_replay`, which defaults to `false`.

Development code that reads deck configs through the legacy Python
`DeckConfigDict` API should tolerate both shapes:
`other["jschoreels.rwkv"]` and a top-level `jschoreels.rwkv` key. The stored
schema is still the nested `other` key, but legacy conversion can flatten
unknown `other` entries into the returned dict during local development or
while inspecting older configs.

The RWKV queue score map and rollback state are transient desktop state. They
are not stored in the collection and are not synced.

The warmed RWKV runtime state is stored in a desktop-local profile cache under
the profile folder. The cache is not part of deck config, is not stored in the
collection database, and is not synced to mobile or other desktop clients. Cache
metadata includes the collection identity, RWKV model content identity, last
processed revlog id, and a count of historical revlogs through that checkpoint.
Reinstalling or moving the app does not invalidate the cache when the bundled
RWKV weights are unchanged.
Cache size scales with the number of reviewed cards and notes that have RWKV
state, so it can be much larger than the embedded model file on large
collections.

The local state cache is stored as a binary base snapshot plus an append-only
delta log. A full cache build writes `snapshot-v1.bin` and resets
`deltas-v1.log`. Later startup loads restore the snapshot, replay any delta-log
reviews, then replay newer revlogs from the collection. Newer revlogs are
appended to the delta log instead of rewriting the full snapshot. The manifest
in `state-v1.meta.json` records both the base snapshot revlog id and the latest
effective revlog id. Rebuilding the RWKV state cache from deck options writes a
fresh full snapshot and compacts the delta log.

## Queue Scoring

When RWKV review and `rwkv_review_instant_order_enabled` are both enabled, and
the selected review order is retrievability, the desktop reviewer may install a
transient per-card RWKV-Instant retrievability map for the current deck tree
before building the review queue. The map is scoped to the selected deck and
scheduler day. Queue building uses those RWKV scores for review-card ordering
and can include review cards whose stored due day is still in the future when
RWKV considers them due. For RWKV queue ordering, a scored review card is
considered due when its RWKV retrievability is at or below the card's current
target retention; cards above that threshold are not admitted through either the
RWKV ordering path or the normal due fallback until a later prediction puts them
back below target. When
`rwkv_review_allow_same_day_review` is disabled, scored review cards that were
already answered during the current scheduler day are also withheld until the
next day. Due review cards without an RWKV score remain eligible through the
normal due-card path.

If RWKV review is enabled but `rwkv_review_instant_order_enabled` is disabled,
review-order retrievability settings do not use FSRS exact retrievability as a
fallback. Queue gathering treats the review order as normal due-day ordering
until RWKV-Instant ordering is explicitly enabled. This does not affect
answer-button scheduling: RWKV-Curve intervals are still used for buttons while
RWKV review is enabled.

Deck tree review counts use the same eligibility rules when a current-day RWKV
queue score map is installed. Scored review cards above target retention, or
same-day reviewed cards when same-day RWKV re-review is disabled, are removed
from review counts even if FSRS says they are due. Scored future review cards
below target retention are added to review counts. Before a RWKV queue score
map has been prepared, deck tree counts continue to show the normal FSRS due
counts.

## Stats And Search

When the desktop stats page requests graph data, Anki may install transient
per-card RWKV retrievability scores for cards in the stats search. Current-day
RWKV scores are stored in one collection cache. Stats retrievability graphs and
Browser searches that use `prop:r` read the same active score from that cache.
If more than one consumer scope has refreshed the same card, the card-specific
Card Info score takes precedence, then the active review-queue score, then the
broader stats graph score. Cards without a current RWKV score continue to use
exact FSRS retrievability.

Card Info, review queue scoring, and stats graph scoring all use the same active
RWKV score cache. If a current-day score is already installed for a card, Card
Info displays that active score so it matches Browser `prop:r`, review queue
ordering, and stats graphs. If no active score exists, Card Info queries RWKV
through the same review-candidate prediction path and installs the resulting
card-specific score. Queue and stats requests batch many cards; Card Info sends
a one-card batch when it needs to query RWKV.

Current-card RWKV queries derive elapsed time from the card's stored
`last_review_time` when present. If that value is missing, the backend falls
back to the scheduler's latest eligible revlog lookup for the card. Manual
reschedule rows and other non-answer rows are not used as the last review. If
neither source exists, the query leaves current elapsed unknown instead of
inferring it from `due - ivl`.

## Answer Button Intervals And S90

When RWKV review is enabled and the desktop RWKV backend is available, the
reviewer computes each answer button by simulating that rating from the current
RWKV state. The embedded runtime evaluates the current query plus hypothetical
Again, Hard, Good, and Easy answer states. Each hypothetical answer state
returns an RWKV-Curve; Anki uses that curve to find the interval at the target
retention for that grade.

The target retention is the scheduler's desired retention for the card. If
Dynamic Desired Retention is active for the scheduling states, RWKV receives the
per-grade Dynamic DR targets and uses the matching target for Again, Hard,
Good, and Easy. Otherwise it uses the resolved preset desired retention, with
deck-config desired retention and `0.9` as fallbacks.

The same per-grade RWKV-Curve is also evaluated at `90%` retrievability. That
interval is the RWKV S90 equivalent. The selected answer's S90 is written to
the card memory-state payload as `memory_state.stability`, which is the
user-facing S90 field consumed by stats and Browser stability views. Existing
FSRS `memory_state.stability_internal` values are not overwritten by the RWKV
S90 write.

Deck Options also exposes a desktop maintenance action to reschedule RWKV
review cards and update their stored RWKV memory state. The action first warms
or restores the local RWKV state cache, then queries RWKV for normal review
cards in RWKV-enabled deck configs. It uses the current/query RWKV-Curve, not a
hypothetical answer-button curve, to compute the target-retention interval and
the current S90 value. The backend applies those values as a normal reschedule:
`memory_state.stability` is updated to the RWKV S90, existing
`memory_state.stability_internal` is preserved, the review interval and due day
are recomputed from the card's last review day, and a rescheduled revlog entry
is written. New, learning, relearning, suspended, and filtered cards are not
changed by this maintenance action.

After an answer is applied on desktop, the reviewer updates the in-memory RWKV
state. Every `rwkv_review_refresh_interval` answered reviews, it refreshes the
transient RWKV score map and invalidates the active queue so the next queue
build admits cards from the full scored candidate set. This matters when the
first queue was built before RWKV scores were ready, or when a refreshed score
map makes a card outside the previous review-limit slice more urgent. It does
not synchronously refresh scores after every answer when the refresh interval
is greater than `1`, as that can be too expensive for large deck trees. Between
full refreshes, Anki refreshes only the answered card's installed queue score
from the updated RWKV state, so a stale pre-answer low score cannot immediately
re-admit the same card. Due post-answer refreshes run as a background collection
task after Anki has finished the answer operation bookkeeping, and the next card
is fetched only after that refresh completes. Skipped post-answer full refreshes
fetch the next card after the answered-card score update.

When `rwkv_review_refresh_on_exit` is enabled, leaving the reviewer after at
least one answered card also starts a background RWKV queue-score refresh. This
allows large `rwkv_review_refresh_interval` values during review while still
refreshing the transient score map after the review session ends.

After a full queue-score refresh finishes, Anki also starts an opportunistic
background prewarm of the local RWKV prediction memo for the current deck tree
and its closest parent deck tree. This prewarm does not install or replace
transient queue/stat score maps; it only makes a later explicit queue or stats
score request for the same current-day RWKV state cheaper. The memo is cleared
whenever the stateful RWKV backend advances or restores review state, so
post-answer predictions are not reused across a changed RWKV state.

When entering the reviewer with a cold RWKV backend, Anki shows the first card
without waiting for historical warm-up and full queue scoring. RWKV warm-up and
score preparation start in the background after that first card is displayed, so
review startup stays responsive. The first card may therefore use the existing
queue order; later queue fetches can use RWKV scores once preparation completes.
Review-time button predictions and Card Info diagnostics may restore a valid
local state cache and replay only newer revlogs before querying. They do not
start a synchronous full historical replay. If no warmed or restorable cache is
available, RWKV intervals and Card Info RWKV diagnostics stay unavailable until
the background warm-up has completed.

When RWKV review is enabled and a usable local cache exists, desktop Anki starts
loading it after profile open with a progress dialog. This restores the saved
state, replays any cache deltas, and replays only newer revlogs, so stats and
Card Info can use RWKV without waiting for their first on-demand query. When a
local cache load or cache build finishes successfully, Anki also starts the same
opportunistic current/parent deck prediction-memo prewarm in the background.
When no usable local cache exists, desktop Anki prompts once per session after profile
open to build the RWKV state cache. This startup build, and the same manual
build from the RWKV button in the Advanced Settings section of FSRS deck
options, run as a modal progress operation. The expensive replay work runs off
the UI thread, but Anki blocks review/deck interaction until the cache build
finishes so the saved state is current when the operation completes. During
replay, the progress dialog shows how many historical reviews have been applied
to the cache, elapsed time, and estimated time remaining.

While building, incrementally updating, or applying an answered review to the
RWKV state cache, Anki also writes pre-answer review-time predictions to the
local collection table
`search_stats_rwkv_review_retrievability`. Rows are keyed by `revlog.id` and
store the query retrievability before the historical answer is applied. The
table is a derived local cache for calibration/stat consumers; it is separate
from the profile-local recurrent state cache and from the short-lived in-memory
query memo. Historical replay writes rows with source `rwkv_state_cache_build`;
live review answers write rows with source `rwkv_review`.

## Inference Runtime

Desktop RWKV inference uses Anki's embedded Rust inference-only runner and
default RWKV checkpoint. `ANKI_RWKV_MODEL_PATH` overrides the embedded Rust
weight file while keeping the embedded runner. `ANKI_RWKV_BENCHMARK_PATH` plus
`ANKI_RWKV_MODEL_PATH` selects the legacy PyTorch-based `srs-benchmark` bridge
for local development or retrained models.

Queue and stats scoring send retrievability-only query predictions to the
embedded runner in batches using the deck option `rwkv_review_batch_size`. The
R-only path may raise the effective batch size from the default `512` to `2048`
because it evaluates one query state per card instead of the full per-grade
answer set; explicit non-default batch-size settings are still respected. The
embedded Rust runner evaluates those independent query predictions on Rayon
worker lanes without evaluating the four hypothetical answer states. Reviewer
answer-button prediction still uses the full batch path that returns current
retrievability plus the per-grade interval and S90 overrides. Both paths avoid
per-card Python/Rust calls and avoid serialization of next-state blobs that
query predictions do not use. Answered reviews still update recurrent state
sequentially, preserving review-history order.

For cold queue and stats scoring, desktop Anki asks the backend for primitive
RWKV review-input rows before it calls the embedded runner. Queue scoring can
request rows directly for the selected deck tree's review queue; fallback queue
paths can still request rows for explicit card ids. Stats preparation can pass
the graph search string directly so Rust builds the search table and filters
candidates before returning rows. The backend applies supported-state and
RWKV-enabled deck-config filtering, resolves the scheduler's FSRS preset id,
target retention, current deck id, batch size, and current elapsed fields, then
Python only materializes `RwkvReviewInput` objects and batches R-only
predictions. If the backend method is unavailable, desktop Anki falls back to
the older Python SQL row builder.

### Query Prediction Memoization

RWKV maintains two different caches that solve different problems:

- The persisted profile-local state cache stores warmed recurrent state after
  historical review replay. It avoids replaying all old revlogs after restart.
- The in-memory query prediction memo stores recent query predictions for the
  current stateful desktop backend. It avoids recomputing R for the same card
  input while the warmed RWKV state has not changed.

The query prediction memo is intentionally short-lived. It is not written to
disk, not stored in the collection, and not synced. Entries are keyed by the
full `RwkvReviewInput`, not by card id alone. That means the key includes card
identity, deck/preset routing, queue/type/due/interval metadata, scheduler-day
offset, and the current scheduling-state elapsed time. This is deliberately
conservative: if those inputs differ, Anki asks RWKV again instead of reusing a
possibly stale R.

The memo is cleared whenever the stateful backend moves to a different RWKV
state:

- restoring the profile-local RWKV state cache
- applying an answered review
- replaying newer revlogs during cache restore or warm-up
- restoring an undo/redo snapshot

This means repeated stats graph loads, Card Info queries, or queue scoring over
the same warmed state can reuse predictions, but any real review-state change
forces fresh predictions. The cache also has a bounded size
(`_RWKV_REVIEW_PREDICTION_CACHE_LIMIT`) so large graph searches cannot grow it
without limit.

For troubleshooting, enable debug logs for `aqt.rwkv_scheduler` and look for
these messages:

- `RWKV stateful prediction cache split`: the stateful backend checked the
  whole candidate set for memo hits before runtime batching. `cache_hits` are
  reusable predictions; `misses` are the candidates that still need inference.
- `RWKV review prediction candidates scored`: the outer scorer finished a
  candidate set. `runtime_requests` is the number of candidates sent through
  inference after cache splitting.
- `RWKV review prediction candidates scored from cache`: the whole candidate
  set was served from memoized predictions, so no runtime batch was needed.
- `RWKV stateful request batch predict_many started`: `requests` is the number
  of cards sent to the runtime for that miss batch after cache splitting.
- `RWKV stateful batch predict_many started`: direct backend batch calls still
  use this older log label; `requests` excludes memoized cache hits.
- `RWKV stateful batch predicted from cache`: the whole batch was served from
  memoized predictions. This can still appear for direct backend batch calls,
  but queue/stat scoring normally avoids those calls after splitting cache hits.
- `RWKV stateful prediction cache hit`: a single-card query reused a memoized
  prediction.
- `RWKV stateful prediction cache cleared`: an invalidation happened; the
  `reason` field explains whether state advanced, state was restored, or the
  profile-local state cache was restored.

If repeated scoring still shows `cache_hits=0`, check whether an answer,
undo/redo, state-cache restore, scheduler day rollover, card metadata change,
or different search/context caused the input key or RWKV state to change. Also
check that the process was not restarted, because the query prediction memo is
memory-only.

Before RWKV queue scoring for a collection, Anki warms the in-memory RWKV state.
If a valid local cache exists, Anki restores the saved snapshot, replays local
delta-log rows, and replays only revlog answer rows newer than the effective
cache checkpoint. If no valid cache exists, Anki replays historical revlog
answer rows for existing cards in chronological order and then writes a fresh
local cache snapshot. UI-time prediction hooks only use that warmed state after
it is ready.

When replaying historical reviews, Anki uses each card's current deck config as
an approximation of the preset routing that existed at review time. By default,
it does not resolve add-on preset overlays or re-evaluate dynamic preset routing
for every historical review, which keeps RWKV state-cache building faster for
collections that do not route a card between presets during its lifetime.

If `rwkv_review_dynamic_preset_replay` is enabled on any RWKV-enabled preset,
RWKV first resolves each historical card's current FSRS preset, including any
matching add-on preset overlay. If the current FSRS preset overlay includes
structured `simulator_rules`, as produced by the dynamic preset add-on for
interval-split presets, RWKV evaluates those rules against historical replay
facts where available. In particular, interval bounds use the card's previous
historical interval at the time of the review, so a card that crosses an `ivl`
threshold during replay can move from the young preset stream to the mature
preset stream. Rules that depend on mutable present-day searches, note fields,
tags, or deck membership are still evaluated from the current collection state
and remain an approximation for old reviews.

The cache also stores the previous review id per card, because incremental
replay needs the elapsed time from each card's prior review. It also stores the
previous interval and review count per card so interval-split dynamic preset
rules can be applied consistently to newer revlogs after a restart. If the
count of historical revlogs through the cached checkpoint changes, if the
configured RWKV model content changes, or if the local cache schema changes, the
cache is treated as stale and rebuilt from history. Changing
`rwkv_review_dynamic_preset_replay` also makes the cache stale, because the
same historical reviews may be replayed into different preset streams.

## Review Undo And Redo

RWKV review state participates in Anki's review undo model. An answered review
can change five recurrent state scopes:

- card
- note
- deck
- preset
- global

Undoing a review must restore every mutated scope. Restoring only card state is
incorrect, because the shared note, deck, preset, or global streams would still
remember the undone review and could affect later predictions.

The desktop stateful backend keeps a bounded rollback queue aligned with
Anki's undo counters. Each answered review records a rollback frame containing
the state immediately before and after the review. Undo restores the pre-review
snapshot and moves the frame to the redo side. Redo restores the post-review
snapshot and moves the frame back to the undo side. The embedded Rust runtime
also snapshots its small per-card runtime state, including feature counters and
the cached recall curve, so hidden runtime bookkeeping rolls back with the five
serialized RWKV state blobs.

Rollback state is transient desktop memory. It is not stored in the collection
and is not synced. If the desktop backend is restarted, Anki reconstructs RWKV
state from the local cache plus any newer revlog rows when possible, and only
reviews answered after that warm-up are available in the live rollback queue.

The current serialized state size is about `278,810` bytes for all five scopes.
Because redo support stores both pre-review and post-review snapshots, a
rollback frame is about `557,620` bytes plus small metadata and runtime-state
overhead. The default 30-review rollback depth therefore needs about `16.7 MB`
for serialized RWKV state; a `25 MB` memory budget leaves room for Python
object overhead and normal Anki undo objects.

When an RWKV undo or redo frame is applied, any transient RWKV queue score map
is cleared. The next queue refresh can then rebuild scores from the restored
state instead of reusing scores derived from a state that has been rolled back.

## Rust/NEON Parity Coverage

RWKV Rust/NEON inference parity is covered by replaying Torch-generated trace
files through the Rust runner. On 2026-06-26, a copied collection with 200,000
chronological usable revlog answers was traced with Torch in eval mode and then
replayed with the embedded Rust/NEON implementation using the exact same feature
vectors.

Scalar probability drift stayed at float-noise level:

- Max absolute error: `0.000001073`
- Mean absolute error: `0.000000085`
- RMSE: `0.000000119`
- p99 absolute error: `0.000000358`
- p999 absolute error: `0.000000536`

The 200,000th checkpoint printed zero probability error to nine decimal places,
which means the final scalar probability matched at that precision; it does not
prove that every hidden-state element was identical. This parity check covers
inference math and recurrent state accumulation over long review histories, but
it intentionally does not cover Rust feature construction or deterministic ID
encoding differences.
