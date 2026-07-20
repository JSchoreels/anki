# RWKV Review Scheduler

The RWKV review scheduler is a desktop-only review-time scheduler. When **Use
RWKV-Curve for answer intervals** is enabled, answer buttons use RWKV-Curve
intervals and store RWKV-Curve S90 values. RWKV-Instant retrievability is
recorded on live review answers and can separately be used for review queue
selection when **Use RWKV-Instant to choose review cards** is enabled. To use
RWKV-Curve without RWKV-Instant ordering, enable the first option and leave the
second disabled. To use FSRS intervals with RWKV-Instant queue selection, leave
the first option disabled and enable the second. Mobile clients do not load the
RWKV backend and continue to schedule with FSRS/SM2.

## Settings And Storage

Desktop-only RWKV review settings are stored in deck config `other` under the
`jschoreels.rwkv` key. This includes `rwkv_review_enabled` and
`rwkv_review_batch_size`, which defaults to `512` for queue and statistics
scoring, and
`rwkv_review_refresh_interval`, which defaults to `1`,
`rwkv_review_refresh_on_exit`, which defaults to `false`, and
`rwkv_review_allow_same_day_review`, which defaults to `true`, and
`rwkv_review_min_intervening_reviews`, which defaults to `5`, and
`rwkv_review_min_elapsed_secs`, which defaults to `30`, and
`rwkv_review_instant_order_enabled`, which defaults to `false`, and
`rwkv_review_candidate_refresh_enabled`, which defaults to `false`, and
`rwkv_review_dynamic_preset_replay`, which defaults to `false`, and
`rwkv_review_first_review_elapsed_from_card_creation`, which defaults to
`true`, and `rwkv_review_enforce_grade_order`, which defaults to `true`.

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
processed revlog id, and a count of benchmark-retained historical answers
through that checkpoint. Historical replay drops cards without a Learning
start and retains only the sequence beginning at each card's latest Learning
start, matching the dataset builder. A later card reset therefore invalidates
any cache checkpoint that still contains the older retained sequence.
The persisted state cache is collection-wide; deck- or preset-scoped replay
histories are transient and must not be written to the profile-local cache.
Reinstalling or moving the app does not invalidate the cache when the bundled
RWKV weights are unchanged.
Cache size scales with the number of reviewed cards and notes that have RWKV
state, so it can be much larger than the embedded model file on large
collections.
The cache also stores RWKV ID-feature encodings and the deterministic generator
state used to assign future encodings, so restored desktop sessions keep using
the same benchmark-compatible ID features for cards, notes, decks, and presets.

Answered-event state uses the same numeric encoding as the RWKV training
dataset, not Anki's raw revlog kind: learning `1`, review `2`, relearning `3`,
filtered `4`, manual `5`, and rescheduled `6`. The retained learning-sequence
start is `0` in the training data. RWKV subtracts `2` from this value for the
historical `scaled_state` feature; query rows always use `0` for that feature.
Changes to this encoding require a state-cache version bump and a full
recurrent-state cache rebuild. Historical `elapsed_days` likewise follows the
training data's difference between Anki scheduler-day offsets;
`elapsed_seconds` remains the wall-clock difference between answers.

### Review Type Contract

Anki's raw revlog kinds and the answered-event states consumed by RWKV are
related but not interchangeable:

| Answer context                  | Raw `ReviewKind` | RWKV dataset state |
| ------------------------------- | ---------------: | -----------------: |
| First retained learning answer  |     Learning `0` |                `0` |
| Later learning answer           |     Learning `0` |                `1` |
| Normal scheduled review         |       Review `1` |                `2` |
| Relearning step                 |   Relearning `2` |                `3` |
| Filtered/out-of-schedule answer |     Filtered `3` |                `4` |
| Rated manual answer             |       Manual `4` |                `5` |
| Reschedule entry                |  Rescheduled `5` |                `6` |

The dataset builder identifies state `0` from the first answer of the retained
learning sequence; it is not a separate raw revlog kind. Unrated reschedule
entries and filtered entries with `factor = 0` are not answer tokens. Live
answered inputs must use the scheduler state at answer time, before card state
mutation, instead of copying the persistent card type. When RWKV synthesizes a
same-day state outside Anki's learning queues, Qt places the corresponding raw
kind on `CardAnswer`; the Rust answer transaction validates and writes that kind
to the revlog, so undo and redo keep the card update and review type atomic.

Synthetic scheduling that does not use Anki's learning queues must still track
the equivalent state transition explicitly:

- A new or reset card starts in Learning. Further answers stay Learning until
  the simulated step graduates.
- A normal inter-day review is Review even when RWKV schedules it earlier than
  a legacy FSRS/SM2 due date. Being early relative to another scheduler does not
  make a model-scheduled answer Filtered.
- Review Again followed by another answer on the same scheduler day enters
  Relearning. Further answers remain Relearning while the simulated relearning
  step schedules another step.
- Once Learning or Relearning graduates, another otherwise unsupported
  same-day answer is Filtered. In particular, Relearning Easy graduates, so a
  later same-day answer cannot still be Relearning.
- A same-day repeat after a successful Review answer is Filtered when no normal
  Review, Learning, or Relearning transition can produce it in the training
  workflow. On a later scheduler day, an RWKV-scheduled answer is Review.
- Manual and Rescheduled are reserved for the corresponding Anki operations;
  they must not be used as generic fallbacks for an unknown state.

`just rwkv-review-type-repair` processes each card chronologically while
tracking scheduler-day boundaries and the prior effective state/rating. It
rewrites only `revlog.type`; review id/time, card id, rating, interval, factor,
duration, and due information remain unchanged. Card-reset entries break the
inferred sequence, and explicit Filtered answers are preserved. When a real
Filtered answer hides an original queue state that the revlog cannot
reconstruct, the row is reported as ambiguous instead of being changed.

The command accepts only an explicit `--collection-copy`, requires the IANA
timezone that defines historical scheduler days, and defaults to a dry-run:

```sh
just rwkv-review-type-repair \
  --collection-copy /path/to/copied/collection.anki2 \
  --timezone Europe/Brussels \
  --rollover-hour 4
```

`--apply-to-copy` makes a sibling backup, applies all `revlog.type` changes in
one transaction, clears collection-local RWKV retrievability rows, and writes a
JSON audit containing every changed row and rule. It never locates or opens a
live profile database. Before using a repaired copy, remove that profile's
desktop-local `rwkv-state-cache` directory so the recurrent state is rebuilt.

The local state cache is stored as a binary base snapshot plus an append-only
delta log. A full cache build writes `snapshot-v1.bin` and resets
`deltas-v1.log`. Later startup loads restore the snapshot, replay any delta-log
reviews, then replay newer revlogs from the collection. Newer revlogs are
appended to the delta log instead of rewriting the full snapshot. The manifest
in `state-v1.meta.json` records both the base snapshot revlog id and the latest
effective revlog id. Rebuilding the RWKV state cache from deck options writes a
fresh full snapshot and compacts the delta log.

## Queue Scoring

When `rwkv_review_instant_order_enabled` is enabled, independently of
`rwkv_review_enabled`, the desktop reviewer may install a transient per-card
RWKV-Instant retrievability map for the current deck tree before building the
review queue. The map is scoped to the selected deck and scheduler day. For
every review sort order, the RWKV scores determine which scored review cards
are eligible. Ascending and descending retrievability additionally use the
RWKV score as their sort key; the other review orders retain their normal
sorting behavior after RWKV eligibility is applied. This can include review
cards whose stored due day is still in the future when RWKV considers them
due. A scored review card is
considered due when its RWKV retrievability is at or below the card's current
target retention; cards above that threshold are not admitted through either the
RWKV ordering path or the normal due fallback until a later prediction puts them
back below target. When
`rwkv_review_allow_same_day_review` is disabled, scored review cards that were
already answered during the current scheduler day are also withheld until the
next day. When same-day RWKV re-review is enabled,
`rwkv_review_min_intervening_reviews` can require a number of other answered
reviews in the selected deck tree before the same card is admitted again, and
`rwkv_review_min_elapsed_secs` can require that many seconds since the card's
stored last review time. If both values are nonzero, both guards must be
satisfied. Due review cards without an RWKV score remain eligible through the
normal due-card path. When the new-card gather order is `Ascending
retrievability (RWKV)` or `Descending retrievability (RWKV)`, queue scoring also
requests new-card inputs and the new queue gathers finite scored new cards by
RWKV retrievability in the selected direction. New cards without a current RWKV
score remain after scored new cards in ascending-position fallback order. These
gather modes are desktop-only in practice, because the RWKV score map is
transient desktop state.

By default, RWKV uses the time since a card was created for new-card inputs and
for the first historical review during state replay. Disable
`rwkv_review_first_review_elapsed_from_card_creation` to treat elapsed time
before a card's first review as unknown instead.

If RWKV-Curve is enabled but `rwkv_review_instant_order_enabled` is disabled,
review-order retrievability settings do not use FSRS exact retrievability as a
fallback. Queue gathering treats the review order as normal due-day ordering
until RWKV-Instant ordering is explicitly enabled. This does not affect
answer-button scheduling: RWKV-Curve intervals are still used. If both options
are disabled, FSRS interval and queue behavior is unchanged.

When `rwkv_review_candidate_refresh_enabled` is enabled, queue refreshes can
reuse the existing transient RWKV queue score map as a stale priority queue.
After the first full score map has been installed, later refreshes score only
the best stale RWKV candidates and merge those fresh scores back into the map.
Descending retrievability prioritizes the highest scores; all other review
orders prioritize the lowest scores. The candidate window is the RWKV batch size.
This accelerates frequent review-time refreshes while preserving the full
installed score map for cards outside the refreshed window. It is approximate:
cards outside the refreshed window keep their previous RWKV score until a later
candidate refresh or full queue rebuild scores them again.

Deck tree review counts use the same eligibility rules when a current-day RWKV
queue score map is installed. Scored review cards above target retention, or
same-day reviewed cards blocked by same-day or repeat-spacing guards, are
removed from review counts even if FSRS says they are due. Scored future review
cards below target retention are added to review counts. Before a RWKV queue
score map has been prepared, deck tree counts continue to show the normal FSRS
due counts. The selected-deck overview prepares and installs the current deck's
RWKV queue score map before rendering its counts. The deck browser renders its
initial tree without waiting for model inference, then scores disjoint
RWKV-enabled deck scopes one at a time. Model scoring runs without collection
access, and each completed scope is installed in a separate transient
count-only cache so the visible counts can be updated incrementally. The
currently selected scope is processed first. While a scope is pending, its
review due count and the review due counts of its descendants display an
ellipsis instead of the stored FSRS count. A completed scope reveals its RWKV
count; a skipped or failed scope falls back to its stored count. Leaving or
refreshing the deck browser cancels later UI updates, and preparing a reviewer
queue clears the count-only cache. This keeps startup responsive while allowing
every displayed RWKV scope to converge to its RWKV eligibility counts before
the reviewer is opened.

When the desktop Dynamic Desired Retention provider is available, RWKV review
input batches are resolved through its bulk API before queue scoring or RWKV
rescheduling predictions are made. The resolved target retention is carried with
each transient queue score, so Rust queue gathering and deck counts compare RWKV
retrievability against the same target the desktop scorer used. RWKV reschedule
requests also carry the resolved target and persist it to the card's desired
retention field together with the new interval and S90 value.

## Stats And Search

When the desktop stats page requests graph data, Anki may install transient
per-card RWKV retrievability scores for cards in the stats search. Current-day
RWKV scores are stored in one collection cache. Stats retrievability graphs and
Browser searches that use `prop:r` read the same active score from that cache.
If more than one consumer scope has refreshed the same card, the card-specific
Card Info score takes precedence, then the active review-queue score, then the
broader stats graph score. Cards without a current RWKV score continue to use
exact FSRS retrievability.

Card Info refreshes its score through the RWKV review-candidate prediction path
each time it renders instead of reusing a score installed by Card Info, review
queue ordering, or stats graphs. It then installs the refreshed card-specific
score in the shared active cache, so Browser `prop:r` and other consumers use
the latest Card Info value. Queue and stats requests batch many cards; Card Info
sends a one-card batch. When current scheduling states are available, Card Info
uses them directly so Filtered cards report the same Curve and S90 values as
their rendered answer buttons; bulk card-state reconstruction is only a
fallback. The stateful prediction memo may reuse that query only
while the RWKV state and the card's complete query input remain unchanged; an
answered review clears the memo before the next Card Info refresh.

Current-card Review, Relearning, and Filtered RWKV queries derive both elapsed
days and elapsed seconds from the card's stored `last_review_time`, so an early
review cannot combine its scheduled interval with its wall-clock elapsed time.
If that value is missing, the backend falls back to the scheduler's latest
eligible revlog lookup for the card. Rated manual review rows are treated like
normal review rows; reset rows, reschedule rows, and other non-answer rows are
not used as the last review. If neither source exists, the query leaves current
elapsed unknown instead of inferring it from `due - ivl`.

Card Info also simulates Again, Hard, Good, and Easy independently from one
shared RWKV state snapshot. **R After Review** queries the simulated state with
zero elapsed days and seconds. **R After 10min** queries the same simulated
state with zero elapsed days and 600 elapsed seconds. These rows report
RWKV-Instant predictions after each hypothetical answer; they are separate from
the RWKV-Curve predictions used for answer-button intervals.

For RWKV-Curve-enabled cards, **RWKV Curve Next S90** reports the Curve S90
predicted for each hypothetical answer, while **FSRS Next S90** reports Anki's
FSRS S90 from the unmodified scheduling states for the same four answers.
Learning and relearning steps may therefore show a short answer-button delay
even when their underlying next S90 is much longer.

Search Stats Extended can also request an isolated historical RWKV Memorised
build through `aqt.rwkv_scheduler`. The build uses a separate embedded runtime,
replays existing-card answers in scheduler-day order, and batch-queries active
cards after each completed day. It exposes card-day progress and selected-search
daily aggregates while running, so the add-on can extend its RWKV graph from
oldest to newest without mutating the active reviewer runtime. The add-on owns
the compact per-card UInt16 cache in the profile-local retrievability sidecar;
Anki returns the completed packed series and a producer identity covering the
model, history checkpoint, scheduler day, and relevant RWKV feature settings.
When that producer configuration is unchanged and review history has only been
appended, Anki can reuse the completed cache through the day before the first
new answer. It reconstructs recurrent state by replaying answers through the
reused prefix without repeating the cached card-day predictions, then replaces
the affected day and computes later days. Model or feature-setting changes, or
non-append changes within the cached review prefix, still require a full build.

## Answer Button Intervals And S90

When RWKV-Curve is enabled and the desktop RWKV backend is available, the
reviewer computes each answer button by simulating that rating from the current
RWKV state. The embedded runtime evaluates the current query plus hypothetical
Again, Hard, Good, and Easy answer states. Each hypothetical answer state
returns an RWKV-Curve. Anki evaluates its monotonic exponential-mixture base and
uses that curve to find the interval at the target retention for that grade.
The model's experimental piecewise `ahead_logits` correction is deliberately
ignored because it can make predicted retrievability increase with elapsed
time. The interval is the first sampled downward crossing of the target; if the
curve never reaches the target, the configured maximum interval is used.

**Keep RWKV intervals in answer order** is enabled by default. It keeps
`Again <= Hard <= Good <= Easy` for both the target-retention intervals and the
per-grade S90 values. When neighboring RWKV predictions conflict, Anki replaces
the conflicting estimates with their shared average until they are in order,
then chooses the intervals from those adjusted estimates. This is the Pool
Adjacent Violators Algorithm (PAVA). With per-grade Dynamic DR targets, Anki
applies the same balancing to each prediction's distance from its own target.
Disabling the option uses the raw, independently calculated RWKV-Curve
crossings.

The target retention is the scheduler's desired retention for the card. If
Dynamic Desired Retention is active for the scheduling states, RWKV receives the
per-grade Dynamic DR targets and uses the matching target for Again, Hard, Good,
and Easy. Otherwise it uses the resolved preset desired retention, with
deck-config desired retention and `0.9` as fallbacks.

The same per-grade RWKV-Curve is also evaluated at `90%` retrievability. That
interval is the RWKV S90 equivalent. The selected answer's S90 is written to the
card memory-state payload as `memory_state.stability`, which is the user-facing
S90 field consumed by stats and Browser stability views. Existing FSRS
`memory_state.stability_internal` values are not overwritten by the RWKV S90
write.

The desktop backend also supports a maintenance operation that reschedules RWKV
review cards and updates their stored RWKV memory state. The operation first
warms or restores the local RWKV state cache, then queries RWKV for normal
review cards in the requested deck tree, filtered to RWKV-enabled deck configs.
Full interval/S90 predictions use a fixed batch size of `128`, independent of
the deck option `rwkv_review_batch_size`, to bound the transient recurrent-state
memory used by this operation. It uses the current/query RWKV-Curve, not a
hypothetical answer-button curve, to compute the target-retention interval and
the current S90 value. The backend applies those values to the card without
writing a review-log entry: `memory_state.stability` is updated to the RWKV S90,
existing `memory_state.stability_internal` is preserved, the review interval and
due day are recomputed from the card's last review day. New, learning,
relearning, suspended, and filtered cards are not changed by this maintenance
action.

Deck Options also exposes an experimental RWKV Desired Retention "Help Me
Decide" workload simulator. It is desktop-only and requires a usable local RWKV
state cache. The Deck Options action opens an FSRS/RWKV comparison that overlays
both curves in one chart. FSRS and RWKV use distinct colors, and the chart hover
reports both values plus RWKV's relative performance. Both runs are built from
the same exact per-preset request, including days, new cards/day, reviews/day,
maximum interval, review order, whether new cards ignore the review limit, and
leech suspension. The simulator runs a fixed target-retention sweep over RWKV-enabled
new and review cards matched by the selected deck/preset search, snapshots and
restores the recurrent RWKV state for each target, and reports the result through the
same workload chart shape as the FSRS Help Me Decide tool. The embedded Rust
runtime runs the whole sweep behind one bridge call: it restores the snapshot
for each target, predicts due reviews, chooses deterministic simulated grades,
and applies answer-state transitions sequentially inside Rust. Development
backends without that embedded simulator fall back to the older Python loop.
Simulated grade sampling uses the same deterministic per-card quantiles across
target retentions, and reported review counts are normalized to be nondecreasing
as target retention rises, reducing adjacent-DR sawtooth noise in the Reviews
view.
The Efficiency view reports net memorized gain per hour relative to the
reviewless baseline, so it can be negative when simulated reviews leave the
final predicted retrievability sum below the no-review trajectory.
When Smooth graph is enabled, RWKV workload curves use a centered moving window
over neighboring target-retention points to reduce approximate-simulation noise.
While the sweep is running, Deck Options polls a desktop-local progress value
that advances over the reviewless baseline and each target-retention point.
For large collections, the RWKV simulator exposes approximate controls: the
sample cap limits how many active review cards are simulated and scales the
result back up, the DR step skips intermediate target-retention points, and the
state stride updates the simulated recurrent RWKV state only every Nth simulated
review. When sampling is active, the daily review limit is scaled down for the
sample before results are scaled back up, and reported review totals are capped
at the configured daily limit multiplied by the simulated day count. The
standalone RWKV workload form defaults to a sample cap of `250`, a DR step of
`1`, a max target retention of `99%`, and a state stride of `10`. The
combined comparison defaults to exact settings so its RWKV and FSRS curves
cover the same target-retention points without sampling or skipped state
updates.
Exact mode is still available by setting the sample cap to `0`, DR step to `1`,
and state stride to `1`.
Existing new cards are introduced up to the shared daily limit and transition
to review scheduling after their first simulated answer. The RWKV path does not
simulate separate learning/relearning steps, additional synthetic new cards,
Dynamic DR/ADR, or easy days. It uses cached historical RWKV retrievability
samples when available to estimate grade mix and review time, with a
deterministic fallback model otherwise.

After an answer is applied on desktop, the reviewer updates the in-memory RWKV
state. Every `rwkv_review_refresh_interval` answered reviews, it refreshes the
transient RWKV score map and invalidates the active queue so the next queue
build admits cards from the refreshed score map. With candidate refresh
disabled, that refresh scores the full deck-tree candidate set. With candidate
refresh enabled and an existing score map available, it scores only the current
candidate window and keeps the remaining installed scores. This matters when
the first queue was built before RWKV scores were ready, or when refreshed
scores make a card outside the previous review-limit slice more urgent. It does
not synchronously refresh the full deck tree after every answer when the refresh
interval is greater than `1`, as that can be too expensive for large deck
trees. Between interval refreshes, Anki refreshes only the answered card's
installed queue score from the updated RWKV state, so a stale pre-answer low
score cannot immediately re-admit the same card. When a due post-answer refresh
is deferred until after the next question is shown, Anki patches only the
needed session-local intervening-review metadata, capped at the configured
threshold, on the installed score map before selecting that next question.
Installed score requests also record
intervening-review counts for recently answered cards in the selected deck tree
from the revlog, plus cards already answered in the current reviewer session,
which lets the backend enforce `rwkv_review_min_intervening_reviews` without
storing a synced per-card counter.
Due post-answer refreshes show the next question after answer operation
bookkeeping, then refresh the
queue-score map for later queue builds. The refresh is split into a short
collection task that snapshots candidate inputs, a model-scoring task that does
not hold the collection executor, and a short collection task that installs the
scores only if the RWKV state generation has not advanced. The first card after
the answer may still be selected from the previously installed map. For
example, with refresh interval `2`, after the second answered review Anki shows
the third question first, then starts the queue-score refresh while the front
side is visible. If that refresh finishes before the third answer is submitted,
later queue builds can use the refreshed map; otherwise Anki keeps using the
previously installed map until a later refresh completes. Skipped post-answer
interval refreshes fetch the next card after the answered-card score update.
When the answered card was the last queued card, Anki uses the same split
prepare/score/install pipeline before requesting the next card. If the backend
cannot provide the immutable input snapshot, Anki continues with normal queue
order instead of falling back to a long Python-side full-deck scan. This is
especially important after undo restores RWKV state and invalidates predictions
that were computed for the now-undone answer.

With candidate refresh disabled, due interval refreshes remain exact. Anki does
not precompute post-answer queue maps for all possible answer buttons while the
answer is shown; that can require scoring the same deck-tree candidates multiple
times before the user has picked an answer. Instead, queue scoring uses the
actual answered ease after the next question has been displayed.

When `rwkv_review_refresh_on_exit` is enabled, leaving the reviewer after at
least one answered card also starts a background RWKV queue-score refresh. This
allows large `rwkv_review_refresh_interval` values during review while still
refreshing a stale transient score map after the review session ends. If the
latest review-time refresh has already installed a score map for the current
RWKV state generation, Anki skips the redundant exit refresh.

Reviewer queue refreshes do not start an additional prediction-memo prewarm.
The installed queue-score map already serves later queue builds, and another
current/parent-deck pass would immediately rescore the same large scope. The
memo is cleared whenever the stateful RWKV backend advances or restores review
state, so post-answer predictions are not reused across a changed RWKV state;
they are rebuilt on demand or after a later state-cache load or build.

When entering the reviewer with a cold RWKV backend, Anki shows the first card
without waiting for historical warm-up and full queue scoring. RWKV warm-up and
score preparation start in the background after that first card is displayed, so
review startup stays responsive. The first card may therefore use the existing
queue order; later queue fetches can use RWKV scores once preparation completes.
Review-time RWKV retrievability queries and Card Info diagnostics may restore a
valid local state cache and replay only newer revlogs before querying. They do
not start a synchronous full historical replay. If no warmed or restorable cache
is available, review-time RWKV retrievability and Card Info RWKV diagnostics
stay unavailable until the background warm-up has completed.

When RWKV review is enabled and a usable local cache exists, desktop Anki starts
loading it after profile open with a progress dialog. This restores the saved
state, replays any cache deltas, and replays only newer revlogs, so stats and
Card Info can use RWKV without waiting for their first on-demand query. When a
local cache load finishes successfully, Anki prewarms only the current deck
scope to keep automatic startup work bounded. An explicit cache build also
prewarms the closest parent deck scope. When no usable local cache exists,
desktop Anki prompts once per session after profile open to build the RWKV state
cache. This startup build, and the same manual
build from the RWKV section of deck options, run as a modal progress operation.
When the startup prompt appears, users can choose a faster state-only build or
the slower state build that also records historical retrievability rows for
calibration/stat consumers. The expensive replay work runs off
the UI thread, but Anki blocks review/deck interaction until the cache build
finishes so the saved state is current when the operation completes. During
replay, the progress dialog shows how many historical reviews have been applied
to the cache, elapsed time, and estimated time remaining.

RWKV review state and historical calibration data are built separately. The
manual **Rebuild RWKV State** action replays answers and persists recurrent
state without computing
a pre-answer prediction for every historical review. Live review answers still
write their pre-answer prediction with source `rwkv_review` and role
`post_optimization`.

Historical calibration predictions live in the profile-local
`search_stats_rwkv_review_retrievability` table, attached from
`collection.retrievability-cache.sqlite`; they are not synced. The table is a
derived cache, separate from the profile-local recurrent state cache and the
short-lived in-memory query memo. Rows are keyed by `revlog_id`, `sample_role`,
`fold_index`, and `source`.

The deck options action **Calculate Calibration Graph Data** replays historical
queries only for this derived table, uses a chronological 70% train / 30% test
split, writes training rows with role `final_fit`, writes held-out rows with
role `test_fold`, and restores the active RWKV backend state afterward.
Calibration graph consumers should ignore `final_fit` rows and use only
`test_fold` plus future `post_optimization` rows.
Add-ons that need these rows can call
`aqt.rwkv_scheduler.ensure_rwkv_calibration_data(mw)` from a background task.
The synchronous API returns immediately when current role-aware data is
complete; otherwise it performs the same calibration-only replay while
snapshotting and restoring active reviewer state.

## Inference Runtime

Desktop RWKV inference uses Anki's embedded Rust inference-only runner and
default RWKV checkpoint. `ANKI_RWKV_MODEL_PATH` overrides the embedded Rust
weight file while keeping the embedded runner. `ANKI_RWKV_BENCHMARK_PATH` plus
`ANKI_RWKV_MODEL_PATH` selects the legacy PyTorch-based `srs-benchmark` bridge
for local development or retrained models.
The embedded runner uses NEON helpers on `aarch64`, runtime-detected AVX2/FMA
helpers on `x86_64`, and scalar helpers as the fallback path.

Queue and stats scoring send retrievability-only query predictions to the
embedded runner in batches using the deck option `rwkv_review_batch_size`. The
R-only path may raise the effective batch size from the default `512` to `2048`
because it evaluates one query state per card instead of the full per-grade
answer set; explicit non-default batch-size settings are still respected. The
RWKV-Curve maintenance reschedule does not use this option; its full
interval/S90 predictions use the fixed batch size documented above. The
embedded Rust runner evaluates those independent query predictions on Rayon
worker lanes without evaluating the four hypothetical answer states. Reviewer
current-card prediction additionally evaluates the four hypothetical answer
states needed for RWKV-Curve intervals and S90 values. Both paths avoid
per-card Python/Rust calls, and the R-only path avoids serialization of
next-state blobs that query predictions do not use. Answered reviews still
update recurrent state sequentially, preserving review-history order.

After warm-up or cache restore, the embedded runner keeps deserialized
recurrent-state maps resident in Rust. Queue and stats R-only scoring can send
one batch of primitive review-input rows and resolve card, note, deck, preset,
and global state inside Rust instead of cloning and deserializing five state
blobs for every card. On macOS, it processes cache-local query chunks on Rayon
worker lanes and uses Accelerate for dense projections. Other platforms retain
the existing per-card Rayon query path. The macOS query-only WKV kernel computes
the retrievability output without retaining transient next-state rows. Answered
reviews, cache restores, undo, and redo keep the resident maps synchronized
with the serialized desktop cache.

Legacy R-only request callers also use the resident path when their supplied
state snapshots match the current warmed state. Requests for another snapshot,
and full answer-state predictions that need interval and S90 heads, retain the
serialized path.

The embedded runner also exposes a native after-review R-only path for exact
post-answer queue preparation. It restores a supplied snapshot into a temporary
worker, applies one answer state to that worker, and scores the requested query
inputs from that temporary state. The live reviewer state is not advanced by
this call; the normal review-answer path remains the only path that mutates the
stateful backend.

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
- `RWKV review inputs scored from resident state`: the embedded scorer resolved
  recurrent state inside Rust and avoided per-card serialized state requests.
- `RWKV stateful resident retrievability inputs predicted`: reports the
  resident batch's `cache_hits`, `runtime_requests`, and total elapsed time.
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
cache checkpoint. Rated manual review rows are included as review answers. If
no valid cache exists, Anki replays historical revlog answer rows for existing
cards in chronological order and then writes a fresh local cache snapshot.
UI-time prediction hooks only use that warmed state after it is ready.

Historical replay in the embedded Rust runtime uses a bulk sequence-mode path.
Pointwise work (norms, lerp mixes, projections, loras, heads) runs in parallel
across reviews, while recurrent-state updates stay sequential within each card,
note, deck, preset, and global stream. State-only replay additionally schedules
review chunks and scope modules as a dependency-safe wavefront: while the
global module processes one chunk, preset, note, deck, and card can process
newer chunks concurrently. Answer projection stages run through a blocked
`Linear` kernel that preserves the per-review accumulation order, so persisted
states and recall curves remain bit-identical. Optional pre-answer query
predictions use the original module-at-a-time path and run as a second read-only
lane over the same pre-review states; on macOS, large query-only projections use
Accelerate/BLAS and may differ by tiny floating-point rounding amounts without
affecting persisted state. On 2026-07-02, a full state-cache
build over a 207,805-review desktop collection measured about 5x faster than
per-review replay: 153.7s to 30.5s with calibration recording, and 80.3s to
14.9s without.

The desktop bridge sends warm-up reviews to the embedded runtime as a packed
binary payload (`warm_up_reviews_packed`, magic `ARWKVWU1`, same row layout as
the packed prediction-request format) instead of per-review Python tuples,
falling back to tuple rows when the packed binding is unavailable. The pyo3
bindings release the GIL for the duration of each warm-up chunk and while
serializing the warm-up snapshot, so Python-side work such as UI progress
updates no longer stalls behind multi-second replay calls.

### RWKV Performance Investigation Notes

This work log captures the 2026-07-02 RWKV warm-up performance investigation.
Future work should start from these measured facts instead of re-checking the
same SRS-benchmark and scan assumptions.

The local baseline before these performance changes was already the Rust bulk
sequence-mode path: layer-at-a-time replay over card, deck, note, preset, and
global streams. On a 207,805-review desktop collection, that path was measured
against the older per-review replay at about 5x faster: 153.7s to 30.5s with
calibration recording, and 80.3s to 14.9s without calibration recording.

SRS-benchmark was checked first because its GPU path uses the same conceptual
state scopes. The relevant path there is `get_result.py` ->
`model/srs_model.py` -> `model/rwkv_model.py` -> CUDA kernel. The local
SRS-benchmark checkpoint matched the bundled Anki `.pth` checkpoint, but the
full SRS-benchmark LMDB test databases were not present locally, so synthetic
stream probes were used. `SrsRWKV.forward_batch()` matched an explicit
one-stream-at-a-time replay through the same card, deck, note, preset, and
global modules to small floating-point tolerance:

- `ahead_logits` max abs diff: `1.31e-06`
- `w` max abs diff: `1.04e-07`
- `w_log_p` max abs diff: `9.06e-06`
- `p_logits` max abs diff: `1.67e-06`

This means SRS-benchmark batching is still stream-sequential per identity. Its
extra speed opportunity is the custom CUDA time-parallel scan inside long
streams, not a different state-application order.

The CUDA path could not be exercised on the local macOS/Apple Silicon machine:
Torch reported `torch.version.cuda=None`, `cuda_available=False`, and no CUDA
devices. MPS was available and ran synthetic SRS batches correctly, but it was
slower than CPU and did not use the custom CUDA op. Synthetic SRS forward
timings were about 54.6 ms CPU vs 84.9 ms MPS for 64 rows, and 98.6 ms CPU vs
297.0 ms MPS for 256 rows. The Darwin extension imported but did not register
the `torch.ops.rwkv.rwkv7_*` CUDA ops.

The Rust scan/reduce path was also checked and rejected for CPU. The ignored
`rwkv_scan_collection_benchmark` in `rslib/src/rwkv/mod.rs` captures
time-mixer coefficients from the global stream and compares normal sequential
recurrence with a CPU affine scan/reduce prototype. On a synthetic 50,000-review
collection with `RAYON_NUM_THREADS=8`, the normal global recurrence took about
114 ms while the best scan/reduce case took about 1.1 s. This prototype is not
the next CPU win; only revisit it for a real GPU implementation or a much
cheaper transform representation.

The synthetic benchmark collections used for these measurements were copied or
scratch SQLite collections under `/private/tmp`; replace the path or recreate
the same shapes if those files are gone. Do not run these benchmarks directly
against a live profile database. The shapes were:

- `rwkv-synthetic-50k.anki2`: 5,000 cards with 10 reviews each.
- `rwkv-synthetic-50k-long-cards.anki2`: 50 cards with 1,000 reviews each.
- `rwkv-synthetic-50k-one-stream.anki2`: one card with 50,000 reviews.

The main bulk warm-up benchmark is:

```sh
rtk env RAYON_NUM_THREADS=8 \
  ANKI_RWKV_BULK_BENCH_COLLECTION=/private/tmp/rwkv-synthetic-50k.anki2 \
  ANKI_RWKV_BULK_BENCH_MODEL=/Users/jschoreels/workspace/anki/qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin \
  ANKI_RWKV_BULK_BENCH_PROFILE=0 \
  cargo test -p anki --release rwkv_bulk_collection_benchmark -- --ignored --nocapture
```

Useful variants:

- Add `ANKI_RWKV_BULK_BENCH_PROFILE=1` to print feature MLP, per-module, head,
  linear, norm, and single-timestep buckets. Linear, norm, and single-timestep
  timings are cumulative across Rayon workers, so they can exceed wall-clock
  time and should be used as relative hotspots.
- Add `ANKI_RWKV_BULK_BENCH_RECORD_PREDICTIONS=1` to exercise the calibration
  query lane.
- Add `ANKI_RWKV_BULK_BENCH_CHUNK_ROWS=<n>` to force an internal chunk size.

The scan/reduce benchmark command is:

```sh
rtk env RAYON_NUM_THREADS=8 \
  ANKI_RWKV_SCAN_BENCH_COLLECTION=/private/tmp/rwkv-synthetic-50k.anki2 \
  ANKI_RWKV_SCAN_BENCH_MODEL=/Users/jschoreels/workspace/anki/qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin \
  ANKI_RWKV_SCAN_BENCH_BULK_SIZES=512,2048,8192,32768,50000 \
  cargo test -p anki --release rwkv_scan_collection_benchmark -- --ignored --nocapture
```

Measured CPU findings and changes:

- The existing Rayon bulk path was about 5x faster than single-thread replay on
  the broad synthetic 50,000-review collection: about 2.9-3.0 s with eight
  Rayon threads versus about 15.7 s with one thread.
- Long card streams were only modestly slower before the deeper changes:
  `rwkv-synthetic-50k-long-cards.anki2` took about 3.17 s and
  `rwkv-synthetic-50k-one-stream.anki2` took about 3.97 s. The bottleneck is
  therefore not just card-stream recurrence length.
- Python's `_RustRwkvRuntime.warm_up_reviews()` previously split backend calls
  by progress-report interval, which was only 500 reviews for a 50,000-review
  history. A chunk sweep on the broad synthetic collection took about 2,928 ms
  with the default 4,096-row Rust chunking, 3,484 ms with 1,000-row backend
  calls, and 3,053 ms with 8,192-row calls. Large Rust warm-ups now call the
  backend in at least 4,096-review chunks while preserving the existing small
  history progress cadence. The regression test is
  `test_rust_rwkv_warmup_chunk_size_preserves_small_progress_chunks`.
- An in-place recurrence-state update experiment was reverted because it was
  neutral to slightly slower in release benchmarks. Recurrence allocation was
  not the main CPU cost.
- The kept deeper CPU change is allocation/layout reduction in the bulk
  time/channel path. `Linear`, `Norm`, and lora helpers now have `apply_into()`
  variants, and `rslib/src/rwkv/bulk.rs` uses fixed per-row time-mix parts
  instead of allocating short vectors for every norm, projection, lora, and row
  output. Parity is covered by the bulk warm-up tests, which compare bulk replay
  with the per-review reference path.

After the allocation/layout change, broad synthetic clean release samples were
3,082 ms, 2,628 ms, and 2,711 ms. The profiled broad run moved from 3,600 ms
before the change to 3,228 ms after it. The long-card shape had one noisy
6,877 ms sample, then repeated at 2,748 ms, and the one-stream shape was
3,868 ms. With `ANKI_RWKV_BULK_BENCH_RECORD_PREDICTIONS=1`, the broad synthetic
collection took 5,570 ms. Treat these as local Apple Silicon measurements; the
important signal is that module and linear hotspot time moved in the intended
direction.

Before the allocation/layout change, a profiled broad synthetic run showed
`warmup_ms=3599.667`, with module buckets card/deck/note/preset/global at about
688.6/753.8/367.4/538.6/1114.1 ms, cumulative `linear_ms=12812.4`,
`norm_ms=1123.7`, and `single_timestep_ms=2339.4`. After the change, the same
profile shape showed `warmup_ms=3228.209`, module buckets
557.8/675.2/330.6/500.4/1020.4 ms, cumulative `linear_ms=12148.1`,
`norm_ms=1083.7`, and `single_timestep_ms=2290.1`.

Verification run during this work:

```sh
rtk cargo test -p anki bulk_warm_up -- --nocapture
rtk cargo fmt --all
rtk just test-py
```

`bulk_warm_up` passed. `cargo fmt --all` completed but printed the repository's
existing nightly-rustfmt-option warnings. `just test-py` collected and ran the
RWKV scheduler tests, but the overall command failed in unrelated installer
tests.

Likely next targets:

- A true batched dense-projection path, such as row-batched GEMM for the many
  small `Linear` calls, is more promising than another recurrence-only CPU
  tweak. The profile remains projection/module dominated.
- A real GPU implementation would need to cover the SRS-benchmark-style
  time-parallel scan and the projection-heavy pointwise work. MPS through Torch
  was not enough on the local machine.
- The CPU scan/reduce prototype should stay as a benchmark and correctness
  reference, not as production direction, until the transform representation or
  execution device changes.

A follow-up pass later on 2026-07-02 implemented the row-batched projection
idea as a bit-exact blocked kernel (`Linear::apply_block_into` plus blocked
lora helpers, used by `rslib/src/rwkv/bulk.rs` with struct-of-arrays part
buffers and 32-row blocks). Because the blocked kernel preserves the
per-output-element accumulation order, the existing bulk parity tests still
pass bit-for-bit. Measured effects on the broad synthetic collection with
eight Rayon threads: cumulative `linear_ms` fell from about 12.1s to 10.2s and
linear call count fell from millions of per-row calls to about 509k block
calls, but wall time was unchanged (about 2.7-2.9s), and a 208,152-review
desktop collection replay was also unchanged (14.2-15.5s unrecorded, about
28.5s recorded). The explanation is that wall time is bounded by the serial
recurrence sweeps of single-stream modules: on the synthetic shape the deck,
preset, and global modules each have one stream, and their sweeps account for
roughly 1.9s of the 2.8s wall. Cores idle during every sweep, so making the
already-parallel pointwise stages faster does not move the wall until sweeps
overlap with pointwise work.

A later prototype moved module replay to a layer-major chunk order and
pipelines each chunk's recurrence/post stages with the next chunk's time-mixer
pointwise stage. The dependency is legal because sweep state for chunk k+1
only needs pointwise `x_norm` data; the stored time `x_shift` is pointwise and
does not depend on the recurrence matrix output. The prototype preserves the
per-review arithmetic order inside the blocked projection and recurrence
kernels, so the bulk parity tests remain bit-exact. On the same 208,502-review
copied desktop collection used for the 2026-07-04 real-data benchmark, release
bulk replay improved from 16.32s to 12.98s without prediction recording, and
from 33.52s to 27.56s with prediction recording. Only after this overlap exists
would a register-tiled multi-row NEON kernel (reusing one weight load across
several row accumulators, still bit-exact) be expected to pay off at the
wall-clock level.

On 2026-07-14, an exact four-row AVX2/FMA projection kernel was measured on a
Windows 11 Intel Core i7-9700K with eight Rayon workers. The kernel keeps four
output rows in registers across the input-column loop, reuses each weight-vector
load, and handles two input columns per inner-loop iteration without changing
any row's accumulation order. The first register-tiled version reduced a
deterministic 50,000-review, 5,000-card replay by 4.61%. Two-column unrolling
then reduced its median from 3.604s to 3.453s in five alternating pairs (4.20%
by separate medians, 4.50% by the paired median). A direct original-to-final
comparison reduced the median from 4.007s to 3.690s (7.90%, 1.086x); the final
build won four of five pairs, with one run reversed by a large background-load
spike. The isolated projection benchmark showed a 16-20% unrolling gain across
the representative 16/32/64/128/256-wide model shapes. The awkward-dimension
unit test covers zero skips and vector, scalar, input-column, and row tails
bit-for-bit. Prediction-enabled synthetic runs also report logloss, and bulk
parity now rejects an absolute sequential-vs-bulk logloss change above 1e-7.

Further i7-9700K experiments retained three more changes. First, the 32-wide
per-head recurrent-state update now uses a runtime-detected AVX2 implementation
that preserves the scalar multiply/subtract/add order and is bit-exact. It was
about 25% faster in isolation and reduced the 50,000-row replay by a paired
median 18.0% (separate medians 2.835s to 2.311s). Fusing the following output
calculation into that kernel was slower and was not retained. Second, sweeping
2,048/4,096/8,192/16,384/32,768-row chunks selected 8,192 rows: its three-run
median was 2.230s versus 2.337s at 16,384 rows, with every 8,192-row sample
faster. Third, when predictions are requested, answer and query normalization
and projection stages now enter the Rayon pool together. On 20,000 prediction
rows this won four of five alternating pairs, improving the paired median by
1.8%, while every run reported the identical logloss
`0.728417944047116`.

Several plausible x86 changes were rejected by end-to-end measurements. A
four-column projection unroll won its isolated microbenchmark but regressed the
full replay by a paired median 20.8%; an eight-row tile similarly turned a
0.1-4.3% isolated win into a 5.6% replay regression. A BLAS-style
`matrixmultiply` backend for disposable query rows was 1.5% slower than the
exact AVX2 kernel, despite preserving the measured logloss, so adding oneDNN or
oneMKL packing and deployment overhead is not justified for these 32-row
matrices. A `target-cpu=native` build was 4.4% faster than the portable runtime-
dispatched build and preserved synthetic logloss exactly, but remains a useful
machine-specific build option rather than a distributable binary assumption.

Across the original Windows baseline and the retained portable changes, five
alternating no-warmup 50,000-row runs reduced the separate median from 3.117s
to 2.211s (29.0%, 1.409x); the paired median reduction was 28.0%, and the final
build won all five pairs. The original and final collection benchmark both
reported logloss `1.228385120180924`. The final deterministic 5,000-row
prediction benchmark reports logloss `1.130982198417313`.

A final set of 9700K experiments did not justify further production changes.
Treating answer and query as one 64-row projection block, so an eight-row
kernel could share weight loads between them, ranged from 0.7% slower to 4.4%
faster in isolated representative shapes; most large shapes were effectively
flat, and the equivalent eight-row full-model kernel had already regressed by
5.6%. A matched Rust 1.97 MSVC PGO build was trained on both 50,000-row
state-only and 20,000-row prediction workloads. Against an uninstrumented build
from the same compiler, its state-only paired median was 1.1% slower amid large
background-load outliers, and its prediction paired median was effectively
flat (-0.06%); prediction logloss was identical at `0.728417944047116`.
Finally, a three-round Rayon sweep on 50,000 rows measured medians of
4.082/3.080/2.815/2.664s for 4/6/7/8 workers, respectively, confirming that all
eight physical cores should remain active. Prediction logloss was identical at
`1.130982198417313` for every tested worker count.

The same follow-up pass also moved the desktop warm-up bridge to a packed
binary payload (`warm_up_reviews_packed`) and released the GIL during warm-up
chunks and snapshot serialization; these help the Python-side build pipeline
and UI responsiveness, which the pure-Rust benchmarks above do not measure. A
copy of the desktop collection used for the replay measurements lives at
`/private/tmp/rwkv-main-profile-copy.anki2`; refresh or recreate it before
trusting new numbers, and check machine load first: a concurrent heavy
process inflated replay samples by 1.5-2x during this session.

On 2026-07-11, state-only replay gained a second level of pipelining across
scope modules. A `(chunk, module)` node depends on the previous module for that
chunk and the previous chunk for that module; nodes on the same anti-diagonal
therefore run concurrently without changing chronological state application or
floating-point operation order. On the copied 208,502-review desktop
collection, the profiled one-call replay fell from 12.44s to 9.88s. Clean
samples were 9.04s and 9.44s. The cross-call/cross-chunk parity test compares
the final state snapshot and runtime cache with sequential replay exactly.

The desktop bridge must expose several internal 16,384-row chunks to one native
call for this wavefront to fill. On the same collection, simulated bridge calls
of 16,384/65,536/131,072 rows measured 13.48s/10.58s/10.26s. State-only bridge
calls therefore use up to 131,072 reviews, while calibration recording retains
the existing 16,384-row calls. The larger state-only call still releases the
Python GIL, and a roughly 208,000-review rebuild reports progress between its
two native calls.

When replaying historical reviews, Anki resolves each card's current FSRS preset
once, including any matching add-on preset overlay, and uses that preset for the
card's retained review history. This approximates the card as staying in its
currently resolved preset for its lifetime without repeatedly evaluating add-on
routing rules.

If `rwkv_review_dynamic_preset_replay` is enabled on any RWKV-enabled preset,
and the current FSRS preset overlay includes structured `simulator_rules`, as
produced by the dynamic preset add-on for interval-split presets, RWKV
re-evaluates those rules against historical replay facts for every review. In
particular, interval bounds use the card's previous historical interval at the
time of the review, so a card that crosses an `ivl` threshold during replay can
move from the young preset stream to the mature preset stream. Rules that depend
on mutable present-day searches, note fields, tags, or deck membership are still
evaluated from the current collection state and remain an approximation for old
reviews.

The cache also stores the previous review id per card, because incremental
replay needs the elapsed time from each card's prior review. It also stores the
previous interval and review count per card so interval-split dynamic preset
rules can be applied consistently to newer revlogs after a restart. If the
count of historical revlogs through the cached checkpoint changes, if the
configured RWKV model content changes, or if the local cache schema changes, the
cache is treated as stale and rebuilt from history. Changing
`rwkv_review_dynamic_preset_replay` also makes the cache stale, because the same
historical reviews may be replayed into different preset streams.

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

When an RWKV undo frame is applied in the reviewer, the rollback frame returns
the restored card id. The reviewer queues those ids and shows them before
falling back to the normal review queue, so repeated undo restores answered
cards in reverse review order. Transient RWKV queue score maps are not cleared
by undo itself; later queue refreshes can replace them from the restored state.

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
