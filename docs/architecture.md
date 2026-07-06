# Anki Architecture

Very brief notes for now.

## Backend/GUI

At the highest level, Anki is logically separated into two parts.

A neat visualization of the file layout is available here:
<https://mango-dune-07a8b7110.1.azurestaticapps.net/?repo=ankitects%2Fanki>
(or go to <https://githubnext.com/projects/repo-visualization#explore-for-yourself> and enter `ankitects/anki`).

### Library (rslib & pylib)

The Python library (pylib) exports "backend" methods - opening collections,
fetching and answering cards, and so on. It is used by Anki’s GUI, and can also
be included in command line programs to access Anki decks without the GUI.

The library is accessible in Python with "import anki". Its code lives in
the `pylib/anki/` folder.

These days, the majority of backend logic lives in a Rust library (rslib, located in `rslib/`). Calls to pylib proxy requests to rslib, and return the results.

pylib contains a private Python module called rsbridge (`pylib/rsbridge/`) that wraps the Rust code, making it accessible in Python.

### GUI (aqt & ts)

Anki's _GUI_ is a mix of Qt (via the PyQt Python bindings for Qt), and
TypeScript/HTML/CSS. The Qt code lives in `qt/aqt/`, and is importable in Python
with "import aqt". The web code is split between `qt/aqt/data/web/` and `ts/`,
with the majority of new code being placed in the latter, and copied into the
former at build time.

## Protobuf

Anki uses Protocol Buffers to define backend methods, and the storage format of
some items in a collection file. The definitions live in `proto/anki/`.

The Python/Rust bridge uses them to pass data back and forth, and some of the
TypeScript code also makes use of them, allowing data to be communicated in a
type-safe manner between the different languages.

At the moment, the protobuf is not considered public API. Some pylib methods
expose a protobuf object directly to callers, but when they do so, they use a
type alias, so callers outside pylib should never need to import a generated
\_pb2.py file.

## Search Query Reads

Search parsing and SQL generation live in `rslib/src/search/`. Arbitrary named
field searches resolve field names against each notetype, so they read from
`notes.flds` by field ordinal instead of relying on `notes.sfld`.

When the Qt browser opens without a configured default search, it searches and
shows `deck:current`, so additional typed terms remain scoped to the currently
selected deck unless the user edits that scope.

Numeric field comparisons such as `Frequency>500 Frequency<1500` use the same
per-notetype field-name resolution. The generated SQL reads the matching field
with `field_at_index(n.flds, ordinal)`, verifies that the trimmed field text is
numeric, and then compares it as a real number. This means the field does not
need to be the notetype's sort field.

The `firstgrade:1..4` card search reads revlog history. It matches cards whose
first user answer button in `revlog` was the requested grade (`1` Again, `2`
Hard, `3` Good, `4` Easy). Manual reschedule entries (`ease = 0`) are ignored
when deciding the first grade. The generated SQL is anchored to the current
card row (`c.id`) so callers that combine it with `cid:<card id>` can resolve a
single card without scanning unrelated revlog rows.

Bracketed ranges such as `Frequency:[500,600]`, `Frequency:[500,600[`, and
`Frequency:]500,600]` are implemented as two comparisons against the same
resolved field value. `[` and `]` at the lower bound mean inclusive and
exclusive respectively; `]` and `[` at the upper bound mean inclusive and
exclusive respectively.

## FSRS Help Me Decide Review-Time Buckets

`Help Me Decide (Experimental)` now estimates review time from bucketed history
instead of a single fixed review cost table.

When opened from deck options, the deck-options payload includes the distinct
deck preset ids used by the selected deck and its descendants. The Help Me
Decide workload modal uses those ids to run one scoped simulation per preset
under the selected deck. Each request searches `deck:"selected deck"
preset:"preset name" -is:suspended` and uses that preset's FSRS parameters and
stored scheduling options for fields that are not exposed in the Help Me Decide
modal. Settings shown in the modal, such as limits, maximum interval, easy days,
review order, and leech suspension, override the preset values for every curve.
Review-time cost is charged from the card's actual simulated retrievability at
the review event, not from the target desired retention being swept.

Data flow:

1. Read revlog entries for the searched cards.
2. Reconstruct review histories and infer pre-review memory state using FSRS.
3. Build samples `(R, S, reps, D, Grade, taken_millis)` for review-kind entries.
   `reps` is filtered to `[2,30]` for regression fitting.
   `R` is computed from the active FSRS model (`FSRS::current_retrievability`),
   so FSRS-7 uses its native forgetting-curve mixture instead of a single
   scalar decay approximation.
4. Fit four linear models from samples:
   - `Again`
   - `Hard`
   - `Good`
   - `Easy`
     using `time = a + b * (1 - R) + c * S + d * reps + e * D`.
5. During `simulate_workload`, each DR sweep updates review costs from those
   fitted models and then runs a summary simulation over DR targets `30..99`.
   The summary path skips daily memorized curve range-filling and computes the
   final retained-knowledge values from final simulated card states.
   It also computes end-of-run weighted retained-knowledge scores,
   `sum(R * f(S))`, where `R` is each final simulated card's retrievability on
   the last simulated day and `f(S) = 1 - exp((-8 / 365) * S)`. The same
   weighted score is computed for the reviewless end state so weighted
   efficiency can use net gain over the no-review baseline.
6. The workload response includes workload summary metrics keyed by swept DR:
   - `memorized`
   - `weighted_memorized`
   - `reviewless_end_weighted_memorized`
   - `cost`
   - `review_count`
     When the workload modal's split-by-preset toggle is enabled, the response
     also includes `preset_workload` entries. Review count, learn count, and time
     cost are attributed to the preset active for the simulated card at each
     review event, so cards that switch preset mid-simulation contribute earlier
     reps to the earlier preset and later reps to the later preset. Memorized and
     weighted memorized are end-state card metrics, so they are attributed to the
     card's final active preset. Efficiency for split curves uses per-preset
     reviewless baselines, so each preset subtracts only the no-review end-state
     contribution of cards attributed to that preset instead of subtracting the
     all-card baseline.
7. The workload response also includes a flattened review-time matrix for UI
   inspection:
   - `review_time_again_seconds`
   - `review_time_hard_seconds`
   - `review_time_good_seconds`
   - `review_time_easy_seconds`
   - `review_time_sample_counts` (raw per-cell sample counts)
   - `review_time_again_coeffs`
   - `review_time_hard_coeffs`
   - `review_time_good_coeffs`
   - `review_time_easy_coeffs`
   - `review_time_grade_weights`
   - `review_time_transition_probs` (4x4, row-major `P(next|current)`)
   - `review_time_transition_counts` (4x4, row-major raw counts)
   - `review_time_success_grade_probs` (R-bucket x 3, row-major `P(Hard/Good/Easy|R)`)
   - `review_time_success_grade_counts` (R-bucket sample counts)
   - `review_time_r_bucket_count`
   - `review_time_s_bucket_count` (fixed to `1`, UI compatibility)

Scope:

- This is currently applied only to the Help Me Decide workload simulation path.
- Normal review simulation and scheduler behavior are unchanged.
- During the DR sweep (`1..99`), existing simulated cards are re-bound to the
  current sweep DR before each run so `count/time/memorized` curves actually
  reflect the selected DR on all cards (not only newly introduced cards).
- Help Me Decide review-time modeling now uses four linear regressions from
  revlog `taken_millis` samples:
  - `Again`
  - `Hard`
  - `Good`
  - `Easy`
    with model form: `time = a + b * (1 - R) + c * S + d * reps + e * D`.
    These predicted costs are also injected into simulator review costs during
    each DR sweep, so `Time` and `Memorized/Time` charts use the same model.
    The workload graph also exposes an absolute `R*f(S)` chart and a
    `Net R*f(S)/t` chart based on weighted score above the reviewless baseline.
    The workload response also exposes fitted coefficients:
  - `review_time_again_coeffs` (`a,b,c,d,e`)
  - `review_time_hard_coeffs` (`a,b,c,d,e`)
  - `review_time_good_coeffs` (`a,b,c,d,e`)
  - `review_time_easy_coeffs` (`a,b,c,d,e`)
    and empirical grade weights from transition-matrix steady-state:
  - `review_time_grade_weights` (`Again,Hard,Good,Easy`).
    Transition matrix (`Again/Hard/Good/Easy -> Again/Hard/Good/Easy`) is
    estimated from consecutive review-kind entries and used to derive
    `P(next_grade|prev_grade)`. During DR sweep, simulator success-grade mix
    (`Hard/Good/Easy`) is computed by blending:
  - `P(Hard/Good/Easy|R-bucket)` (5% R buckets, Laplace-smoothed), and
  - transition-derived prior from `P(next_grade|prev_grade)`
    with reliability-weighted geometric pooling.
    `P(Hard/Good/Easy|R-bucket)` is additionally reliability-shrunk toward a
    distance-weighted neighborhood prior (`w=n/(n+k)`), to stabilize sparse
    low-sample buckets. Optional simulator toggle
    `help_me_decide_enforce_monotonic_success_grade_probs` applies weighted
    isotonic constraints (`Hard` non-decreasing as R decreases, `Easy`
    non-increasing as R decreases), then recomputes `Good`.
    This blended distribution is injected into `config.review_rating_prob`, so
    `Time` and `Memorized/Time` use both R-conditioned and transition-informed
    grade mixes. The blend can be overridden in Deck Options simulator with
    `help_me_decide_transition_blend_alpha` (`0`=R-only, `1`=transition-only).
    Deck Options currently defaults this override to `0` and leaves
    `help_me_decide_enforce_monotonic_success_grade_probs` disabled.

## FSRS Parameter Source

Deck options include an explicit FSRS version selector (`4.5/5/6/7`) stored in
`deck_config.config.fsrs_version`. Parameter editing and optimization target the
selected version's parameter array (`fsrs_params_4/5/6/7`).

Runtime FSRS parameter lookup goes through an FSRS preset read model. Built-in
FSRS presets are derived 1:1 from existing deck config rows, so the current
database and sync representation remains unchanged. The FSRS preset contains
only FSRS-specific data: selected version, selected params, desired retention,
historical retention, and ignore-before date. Existing per-deck desired
retention overrides are preserved during card resolution. Non-FSRS deck behavior
continues to come from the card's home deck config.

Add-on FSRS presets and ordered card-matching rules can be provided through the
synced collection config key `fsrsPresetOverlay`. Add-on preset ids must use the
`addon:` namespace. Rule searches are evaluated in order, but must not use
`prop:r`, `prop:s`, or `prop:d`, because those FSRS metrics depend on derived
FSRS state or the selected FSRS preset.

Deck options also expose the global FSRS short-term toggle (`same-day review`
behavior for learning/relearning paths) backed by
`BoolKey::FsrsShortTermWithStepsEnabled`.

Deck options also expose a global FSRS learning-queue bypass backed by
`BoolKey::FsrsLearningQueuesDisabled`. It defaults to disabled. When enabled
with FSRS active, answering cards schedules review states directly instead of
writing learning/relearning queue states, including configured steps and FSRS
short-term intervals below half a day.

When FSRS computes a failing review interval for `Again`, the scheduler clamps
that interval to the deck preset's minimum lapse interval and maximum review
interval before storing it on the card. This applies both when empty relearning
steps schedule the card directly as a review card and when configured
relearning steps keep the card in the relearning queue with an embedded review
interval.

When FSRS same-day scheduling keeps a card in a learning or relearning queue,
the computed day interval is stored on the card as seconds. Sub-second FSRS
intervals are clamped to the deck preset's FSRS minimum interval so real review
scheduling and Deck Options' "New Card Intervals" preview do not represent them
as a zero-second `(end)` interval.

Configured learning/relearning steps still take precedence while any remain.
After the final configured step, FSRS same-day scheduling may keep the card in
the learning or relearning queue with `remaining_steps = 0`, so later `Hard` and
`Good` answers use FSRS short-term intervals instead of replaying the last
configured step.

When FSRS review order is ascending retrievability, due review,
interday-learning, and due-now intraday learning/relearning cards are gathered
into one exact-retrievability ordering. Intraday cards with a future timestamp
remain hidden until their due time, at which point the queue is rebuilt so they
can be inserted according to their current retrievability. The ordering key is
the card's current FSRS retrievability from its selected deck preset, not its
relative distance from desired retention. New cards are not included in this
FSRS ordering because they do not have FSRS retrievability yet. In desktop RWKV
mode, new-card gather order can instead use ascending or descending RWKV
retrievability when the reviewer has installed a current-day RWKV queue score
map; unscored new cards retain ascending-position fallback order.
Review limits are applied after the shared retrievability sort, so filtered-deck
positions do not decide which cards are admitted before sorting.

The Deck Options "New Card Intervals" preview passes the current unsaved values
of these toggles to backend `GetFsrsNewCardIntervals`, so preview rows update
immediately when toggled (without requiring a save first).

Deck Options "Check Health" now also passes the currently selected unsaved
`fsrs_version` to backend `EvaluateParams`, so split-based logloss/RMSE is
computed with the selected model family (FSRS-7 vs FSRS-6/5/4).

FSRS training-item extraction is model-family-aware:

- FSRS-6 family keeps the legacy training target rule (`delta_t > 0` only).
- FSRS-7 includes same-day (`delta_t == 0`) follow-up targets during
  optimization/evaluation item generation.
- Revlog-derived training items keep an aligned card-id vector after sorting by
  review id. Final FSRS-7 uses the standard tensor optimizer path in `fsrs-rs`;
  the previous windowed analytic optimizer is not used because it was tied to
  the old single-stability parameter layout.
- Optimize progress now reports:
  - total training targets,
  - long-term targets (`delta_t >= 1`),
  - same-day/short-term targets (`delta_t < 1`).
- For FSRS-7, target `delta_t` is derived as fractional elapsed days from
  revlog timestamps (with a 1ms floor to keep `delta_t > 0`).
- Optimize All Presets prepares each preset's read-only training input
  sequentially from the collection, then optimizes those prepared inputs in
  parallel. Optimizer jobs are ordered by review count from largest to smallest
  and assigned to Rayon worker lanes with a greedy review-count balance, so large
  presets start early and total estimated work is spread across lanes. Preset
  parameter writes and `LastFsrsOptimize` updates remain on the collection thread
  after optimization results are collected.
- Optimize All Presets reports aggregate progress plus one progress entry per
  preset, including total, long-term, and same-day/short-term target counts.
  Presets with no training targets are skipped before optimizer threads are
  started and reported as skipped 0-review presets, but they are not included
  in aggregate current/total optimizer progress. The progress dialog shows bars
  for currently active optimizer jobs only, ordered by review count from largest
  to smallest, with completed preset names and a single skipped-preset count
  summarized above those bars. The dialog estimates remaining time from elapsed
  time and review-weighted progress across all non-skipped presets, smooths that
  estimate across progress updates, and logs each completed preset with the time
  observed for that preset's optimizer job.
- Deck options expose per-preset FSRS-7 toggles for same-day review targets and
  scheduling penalties. The same-day review toggle is used by optimize,
  evaluate, and health-check target selection; the scheduling-penalty toggle is
  used by optimization and fold-based health checks.
  Their per-preset UI state is persisted in `deck_config.config.other` JSON as:
  - `fsrs7IncludeSameDayOptimize`
  - `fsrs7EnableSchedulingPenalties` (absent means disabled)
  - `fsrsEvaluationSearch` (separate search expression used by Evaluate /
    Check Health / Optimize comparison metrics; optimize training still uses
    `param_search`)
  - For Check Health specifically:
    - optimization/training uses `param_search`
    - metric evaluation uses `fsrsEvaluationSearch` (or `param_search` if blank)
  - Optimize All Decks reads `fsrs7IncludeSameDayOptimize` and
    `fsrs7EnableSchedulingPenalties` from each preset's stored `other` JSON
    before optimizing that preset.
  - The FSRS options UI also provides a transient same-day "Help Me Decide"
    comparison that optimizes FSRS-7 parameters with and without same-day reviews
    and evaluates both parameter sets on all targets and long-term-only targets.

Runtime parameter lookup uses the selected version first; if that array is not
usable (`17/19/21/34` length with finite values), it falls back to best
available parameters for compatibility with existing collections.

FSRS optimization follows the training objective implemented in `fsrs-rs`. Anki
does not apply the legacy raw-logloss post-filter to optimizer output, because
optimized parameters are selected by the regularized training objective, which
can include L2 and, when enabled, schedule penalty terms depending on the model
family. FSRS-7 scheduling penalties are disabled by default.
FSRS-7 optimization reads/writes `fsrs_params_7`.
When optimizer output length does not match the selected preset's current
parameter-family length (for example selected FSRS-6 vs optimizer returning
FSRS-7-length params), deck options keeps the current selected params instead of
cross-writing a different family into that slot.

When `fsrs_params_7` has 34 values (FSRS-7), card-info forgetting-curve
visualization uses the FSRS-7 mixture curve parameters in `w[23..33]`. The
deck-options custom single-decay table is disabled for FSRS-7 because there is
no single `decay` parameter to sweep.
Deck options treats 35-value FSRS-7 preview parameter sets as outdated and
shows a migration warning instead of sending them to backend preview/evaluation
calls.

When deriving memory state from legacy SM-2 card fields (cards without usable
revlog-derived state), FSRS-7 now uses a numerically-stable conversion path in
`rslib/src/scheduler/fsrs/memory_state.rs`. This avoids save-time failures with
valid 34-parameter sets where the legacy scalar-decay conversion can overflow.

## FSRS Add-on Math APIs

SchedulerService exposes two FSRS math helpers for add-ons:

- `FsrsCurrentRetrievability(card_id, stability, elapsed_days)`
- `FsrsNextInterval(card_id, stability, desired_retention)`

Both read the target card's deck config and use the backend-selected FSRS
parameter array (including FSRS-7 with 34 params). This avoids add-ons
re-implementing forgetting-curve math in Python and keeps results aligned with
backend scheduling/evaluation behavior.

## FSRS Retrievability Reads

Retrievability shown in these backend paths is computed from the selected deck
FSRS parameter array via `FSRS::current_retrievability`, unless a desktop-only
RWKV score map has been prepared:

- Card info stats (`rslib/src/stats/card.rs`)
- Browser `Retrievability` column (`rslib/src/browser_table.rs`)
- Stats retrievability graph (`rslib/src/stats/graphs/retrievability.rs`),
  which uses RWKV scores for cards present in the transient stats score map and
  falls back to FSRS for the rest

For FSRS-7, this uses the native forgetting-curve mixture (no scalar decay
approximation).

FSRS calibration consumers can populate a local review-time prediction cache in
the profile-local retrievability cache database, not in the synced collection
DB, with `ComputeFsrsReviewRetrievabilityCalibration`. The cache table is
`search_stats_fsrs_review_retrievability`, attached to the collection
connection from `collection.retrievability-cache.sqlite`. Rows are keyed by
`revlog.id`, role, fold, and source. `final_fit` rows store the pre-answer
predicted retrievability computed with the supplied parameter array.
`validation_fold` rows store out-of-sample predictions from the expanding
time-series folds used for calibration consumers, and `post_optimization` rows
store pre-answer predictions for later reviews. This table is a local derived
cache for calibration-style consumers; it is not populated by ordinary FSRS
optimization, is not card state, is not synced as part of collection sync/full
upload, and should not be used for current retrievability ordering/search.

The SQL helper functions in `rslib/src/storage/sqlite.rs` still use per-card
stored scalar decay from `card.data` for ordering/search expressions.

Do not store current FSRS retrievability as persistent card state. Upstream's
SQLite review ordering could compute retrievability directly from `card.data`
because it used the legacy scalar-decay curve. Exact FSRS-7 retrievability also
depends on the selected deck preset's full parameter array and elapsed time, so
a stored value would become stale when time passes, cards move decks, filtered
cards return home, deck presets change, or memory states are recomputed. Exact
FSRS-7 ordering/search should compute retrievability on demand, or use a local
temporary table/cache scoped to the current operation, instead of syncing a
derived `R` value in card data.

When cards are moved to another normal deck, their persisted FSRS data in
`card.data` (`s`, `s_int`, `s_fast`, `d`, `dr`, `decay`) is rewritten for the
target deck preset. Non-new cards rebuild memory state from revlog history using
the target deck's FSRS params and ignore-before setting; if no usable revlog
data is available, the state is inferred from the card's current SM-2 fields.
New cards remain without memory state.

Implication for aggregates:

- Stats retrievability aggregate (`sum_by_card` / average, i.e. `SUM(R)`-style
  view in stats) is model-based and aligned with selected FSRS params.
- SQL-expression-based aggregates or sorts that call the sqlite FSRS helpers
  are still scalar-decay-based, and can diverge (especially with FSRS-7).

Current exact-vs-scalar status:

- Exact model-based ordering in Rust is used for:
  - Browser sort by `Retrievability`
  - Review queue retrievability order (`ascending` / `descending`)
  - RWKV-only new-card gather order (`ascending/descending retrievability (RWKV)`)
  - Filtered deck retrievability order (`ascending` / `descending`)
- `prop:r` filtering is exact-model-based. Search builds a temporary
  `search_exact_retrievability` table (`cid`, `r`, `s90`) from
  `FSRS::current_retrievability` and the stored `S90`. If a desktop RWKV score
  map is available for the current scheduler day, `r` is taken from RWKV for
  cards in that map, and from FSRS for the remaining cards. For cards with
  multiple current-day RWKV scores, search prefers the Card Info score, then
  the active review-queue score, then the stats graph score.
- `card.data.s` stores `S90` (the interval at 90% retrievability), so
  `prop:s`, the browser stability column, and Card Info all use the same
  stability value across FSRS-6 and FSRS-7. When a positive FSRS stability value
  would round to zero in card data, it is persisted as `0.0001` so legacy
  clients that only read `s` do not see an invalid zero stability. Check
  Database rewrites existing FSRS memory states with zero `s` through the same
  nonzero persistence rule.
- The scheduler's internal stability is stored separately in `card.data.s_int`
  on new FSRS writes, even when it matches `S90`. Scheduling and retrievability
  math use `s_int`; legacy card data without `s_int` treats `s` as both values.
- FSRS-7's fast stability trace is stored in `card.data.s_fast` on new FSRS
  writes. Legacy card data without `s_fast` treats the internal slow stability
  as the fast trace fallback.
- Python `Collection.compute_memory_state()` exposes both `stability` (`S90`) and
  `stability_internal`, and FSRS-7 states also carry `stability_fast`. Add-ons
  that write `FSRSMemoryState` back to cards must preserve
  `stability_internal` and `stability_fast`; otherwise later scheduling answers
  will start from incomplete model state.
- The Card Info forgetting curve plots retrievability from reconstructed
  review-log memory states. FSRS-7 reconstruction uses fractional same-day
  review deltas from revlog timestamps, matching the scheduler path; FSRS-6
  keeps calendar-day deltas. When the newest user-graded revlog entry matches
  the card's last review time, Card Info uses the current stored card memory
  state for that newest point, so the curve tooltip and the Card Info stability
  row agree after same-day learning/relearning answers.
- Card Info resolves the card's current FSRS preset through the card-level FSRS
  preset resolver before returning preset names, parameters, and retrievability.
  The GUI `card_info_will_add_rows` hook appends display-only `label`/`value`
  rows to the Card Info response after the backend stats have been read; these
  rows are not persisted to collection storage.
- Add-on helper APIs expose exact interval-at-target-retrievability math:
  - `fsrs_interval_at_retrievability(card_id, stability, target_retrievability)`
  - `fsrs_interval_at_retrievability_batch([{card_id, stability}, ...], target_retrievability)`
  - `fsrs_interval_at_retrievability_variable_batch([{card_id, stability, target_retrievability}, ...])`
  - `fsrs_interval_at_retrievability_by_config_batch([{request_index, config_id, stability}, ...], target_retrievability)`
    The card batch helper resolves card presets with the batch preset resolver
    and reuses one FSRS instance per resolved preset. The variable card batch
    helper uses the same preset path while allowing each item to request a
    different target retrievability. The config batch helper reads deck config
    parameters directly.
- Legacy sqlite FSRS helper expressions continue to use stored scalar decay, but
  the standard retrievability search/order paths above no longer depend on them.
