# RWKV Review Scheduler

The RWKV review scheduler is a desktop-only retrievability scorer. Mobile
clients do not load the RWKV backend and continue to schedule with FSRS/SM2.

## Settings And Storage

Desktop-only RWKV review settings are stored in deck config `other` under the
`jschoreels.rwkv` key. This includes `rwkv_review_enabled` and
`rwkv_review_batch_size`, which defaults to `512`, and
`rwkv_review_refresh_interval`, which defaults to `1`, and
`rwkv_review_refresh_on_exit`, which defaults to `false`.

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
metadata includes the collection identity, RWKV model identity, last processed
revlog id, and a count of historical revlogs through that checkpoint.
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

When RWKV review is enabled and the selected review order is retrievability,
the desktop reviewer may install a transient per-card RWKV retrievability map
for the current deck tree before building the review queue. The map is scoped
to the selected deck and scheduler day. Queue building uses those RWKV scores
for review-card ordering and can include review cards whose stored due day is
still in the future; due review cards without an RWKV score remain eligible
through the normal due-card path.

## Stats And Search

When the desktop stats page requests graph data, Anki may install a transient
per-card RWKV retrievability map for cards in the stats search. The stats
retrievability graph uses RWKV scores for cards present in that map and falls
back to FSRS for other cards. Browser searches that use `prop:r` also consult
current-day transient RWKV scores when they are available. If more than one
transient RWKV source has scored the same card, the card-specific Card Info
score takes precedence, then the active review-queue score, then the broader
stats graph score. Cards without a current RWKV score continue to use exact
FSRS retrievability.

After an answer is applied on desktop, the reviewer updates the in-memory RWKV
state. Every `rwkv_review_refresh_interval` answered reviews, it refreshes the
transient RWKV score map and invalidates the active queue so the next queue
build admits cards from the full scored candidate set. This matters when the
first queue was built before RWKV scores were ready, or when a refreshed score
map makes a card outside the previous review-limit slice more urgent. It does
not synchronously refresh scores after every answer when the refresh interval
is greater than `1`, as that can be too expensive for large deck trees. Due
post-answer refreshes run as a background collection task after Anki has
finished the answer operation bookkeeping, and the next card is fetched only
after that refresh completes. Skipped post-answer refreshes fetch the next card
immediately.

When `rwkv_review_refresh_on_exit` is enabled, leaving the reviewer after at
least one answered card also starts a background RWKV queue-score refresh. This
allows large `rwkv_review_refresh_interval` values during review while still
refreshing the transient score map after the review session ends.

When entering the reviewer with a cold RWKV backend, Anki shows the first card
without waiting for historical warm-up and full queue scoring. RWKV warm-up and
score preparation start in the background after that first card is displayed, so
review startup stays responsive. The first card may therefore use the existing
queue order; later queue fetches can use RWKV scores once preparation completes.
While warm-up is pending, answer-button interval overrides do not start a
synchronous historical replay; they continue to use the current scheduler
values. Card Info RWKV diagnostics may restore a valid local state cache and
replay only newer revlogs before querying. If no warmed or restorable cache is
available, Card Info shows RWKV as unavailable until the background warm-up has
completed.

When RWKV review is enabled and a usable local cache exists, desktop Anki starts
loading it after profile open with a progress dialog. This restores the saved
state, replays any cache deltas, and replays only newer revlogs, so stats and
Card Info can use RWKV without waiting for their first on-demand query. When no
usable local cache exists, desktop Anki prompts once per session after profile
open to build the RWKV state cache. This startup build, and the same manual
build from the RWKV button in the Advanced Settings section of FSRS deck
options, run as a modal progress operation. The expensive replay work runs off
the UI thread, but Anki blocks review/deck interaction until the cache build
finishes so the saved state is current when the operation completes. During
replay, the progress dialog shows how many historical reviews have been applied
to the cache, elapsed time, and estimated time remaining.

While building or incrementally updating the RWKV state cache, Anki also writes
pre-answer review-time predictions to the local collection table
`search_stats_rwkv_review_retrievability`. Rows are keyed by `revlog.id` and
store the query retrievability before the historical answer is applied. The
table is a derived local cache for calibration/stat consumers; it is separate
from the profile-local recurrent state cache and from the short-lived in-memory
query memo.

## Inference Runtime

Desktop RWKV inference uses Anki's embedded Rust inference-only runner and
default RWKV checkpoint. `ANKI_RWKV_MODEL_PATH` overrides the embedded Rust
weight file while keeping the embedded runner. `ANKI_RWKV_BENCHMARK_PATH` plus
`ANKI_RWKV_MODEL_PATH` selects the legacy PyTorch-based `srs-benchmark` bridge
for local development or retrained models.

Queue scoring sends query predictions to the embedded runner in batches using
the deck option `rwkv_review_batch_size`. The embedded Rust runner evaluates
those independent query predictions on Rayon worker lanes and returns only
retrievability and the optional Good interval override, avoiding per-card
Python/Rust calls and avoiding serialization of next-state blobs that query
predictions do not use. Answered reviews still update recurrent state
sequentially, preserving review-history order.

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

When replaying historical reviews, Anki uses the current FSRS preset overlay as
an approximation of the preset routing that existed at review time. If the
overlay includes structured `simulator_rules`, as produced by the dynamic
preset add-on for interval-split presets, RWKV evaluates those rules against
historical replay facts where available. In particular, interval bounds use the
card's previous historical interval at the time of the review, so a card that
crosses an `ivl` threshold during replay can move from the young preset stream
to the mature preset stream. Rules that depend on mutable present-day searches,
note fields, tags, or deck membership are still evaluated from the current
collection state and remain an approximation for old reviews.

The cache also stores the previous review id per card, because incremental
replay needs the elapsed time from each card's prior review. It also stores the
previous interval and review count per card so interval-split dynamic preset
rules can be applied consistently to newer revlogs after a restart. If the
count of historical revlogs through the cached checkpoint changes, if the
configured RWKV model changes, or if the local cache schema changes, the cache
is treated as stale and rebuilt from history.

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
