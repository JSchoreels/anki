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

## FSRS Help Me Decide Review-Time Buckets

`Help Me Decide (Experimental)` now estimates review time from bucketed history
instead of a single fixed review cost table.

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
   fitted models and then runs simulation to accumulate `daily_time_cost`.
6. The workload response includes a flattened matrix for UI inspection:
   - `review_time_again_seconds`
   - `review_time_hard_seconds`
   - `review_time_good_seconds`
   - `review_time_easy_seconds`
   - `review_time_sample_counts` (raw per-cell sample counts)
   and the fitted coefficients:
   - `review_time_again_coeffs`
   - `review_time_hard_coeffs`
   - `review_time_good_coeffs`
   - `review_time_easy_coeffs`
   and empirical grade weights:
   - `review_time_grade_weights`
   and grade transition data:
   - `review_time_transition_probs` (4x4, row-major `P(next|current)`)
   - `review_time_transition_counts` (4x4, row-major raw counts)
   and retrievability-conditioned success-grade data:
   - `review_time_success_grade_probs` (R-bucket x 3, row-major `P(Hard/Good/Easy|R)`)
   - `review_time_success_grade_counts` (R-bucket sample counts)
   with bucket dimensions:
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
  low-sample buckets.
  This blended distribution is injected into `config.review_rating_prob`, so
  `Time` and `Memorized/Time` use both R-conditioned and transition-informed
  grade mixes. The blend can be overridden in Deck Options simulator with
  `help_me_decide_transition_blend_alpha` (`0`=R-only, `1`=transition-only).

## FSRS Parameter Source

Deck options include an explicit FSRS version selector (`4.5/5/6/7`) stored in
`deck_config.config.fsrs_version`. Parameter editing and optimization target the
selected version's parameter array (`fsrs_params_4/5/6/7`).

Deck options also expose the global FSRS short-term toggle (`same-day review`
behavior for learning/relearning paths) backed by
`BoolKey::FsrsShortTermWithStepsEnabled`.

The Deck Options "New Card Intervals" preview passes the current unsaved value
of that toggle to backend `GetFsrsNewCardIntervals`, so preview rows update
immediately when toggled (without requiring a save first).

Deck Options "Check Health" now also passes the currently selected unsaved
`fsrs_version` to backend `EvaluateParams`, so split-based logloss/RMSE is
computed with the selected model family (FSRS-7 vs FSRS-6/5/4).

FSRS training-item extraction is model-family-aware:
- FSRS-6 family keeps the legacy training target rule (`delta_t > 0` only).
- FSRS-7 includes same-day (`delta_t == 0`) follow-up targets during
  optimization/evaluation item generation.
- Optimize progress now reports:
  - total training targets,
  - long-term targets (`delta_t >= 1`),
  - same-day/short-term targets (`delta_t < 1`).
- For FSRS-7, target `delta_t` is derived as fractional elapsed days from
  revlog timestamps (with a 1ms floor to keep `delta_t > 0`).
- Deck options expose separate FSRS-7 toggles for optimize (training targets)
  and evaluate/health-check target selection.
  Their per-preset UI state is persisted in `deck_config.config.other` JSON as:
  - `fsrs7IncludeSameDayOptimize`
  - `fsrs7IncludeSameDayEvaluate`
  - `fsrsEvaluationSearch` (separate search expression used by Evaluate /
    Check Health / Optimize comparison metrics; optimize training still uses
    `param_search`)
  - For Check Health specifically:
    - optimization/training uses `param_search`
    - metric evaluation uses `fsrsEvaluationSearch` (or `param_search` if blank)

Runtime parameter lookup uses the selected version first; if that array is not
usable (`17/19/21/35` length with finite values), it falls back to best
available parameters for compatibility with existing collections.

FSRS-7 optimization reads/writes `fsrs_params_7`, and follows the FSRS-7
training objective implemented in `fsrs-rs`.
When optimizer output length does not match the selected preset's current
parameter-family length (for example selected FSRS-6 vs optimizer returning
FSRS-7-length params), deck options keeps the current selected params instead of
cross-writing a different family into that slot.

When `fsrs_params_7` has 35 values (FSRS-7), card-info forgetting-curve
visualization uses the FSRS-7 two-curve mixture (`w[27..34]`). The deck-options
custom single-decay table is disabled for FSRS-7 because there is no single
`decay` parameter to sweep.

When deriving memory state from legacy SM-2 card fields (cards without usable
revlog-derived state), FSRS-7 now uses a numerically-stable conversion path in
`rslib/src/scheduler/fsrs/memory_state.rs`. This avoids save-time failures with
valid 35-parameter sets where the legacy scalar-decay conversion can overflow.

## FSRS Add-on Math APIs

SchedulerService exposes two FSRS math helpers for add-ons:

- `FsrsCurrentRetrievability(card_id, stability, elapsed_days)`
- `FsrsNextInterval(card_id, stability, desired_retention)`

Both read the target card's deck config and use the backend-selected FSRS
parameter array (including FSRS-7 with 35 params). This avoids add-ons
re-implementing forgetting-curve math in Python and keeps results aligned with
backend scheduling/evaluation behavior.

## FSRS Retrievability Reads

Retrievability shown in these backend paths is computed from the selected deck
FSRS parameter array via `FSRS::current_retrievability`:

- Card info stats (`rslib/src/stats/card.rs`)
- Browser `Retrievability` column (`rslib/src/browser_table.rs`)
- Stats retrievability graph (`rslib/src/stats/graphs/retrievability.rs`)

For FSRS-7, this uses the native forgetting-curve mixture (no scalar decay
approximation).

The SQL helper functions in `rslib/src/storage/sqlite.rs` still use per-card
stored scalar decay from `card.data` for ordering/search expressions.

Implication for aggregates:

- Stats retrievability aggregate (`sum_by_card` / average, i.e. `SUM(R)`-style
  view in stats) is model-based and aligned with selected FSRS params.
- SQL-expression-based aggregates or sorts that call the sqlite FSRS helpers
  are still scalar-decay-based, and can diverge (especially with FSRS-7).

Current exact-vs-scalar status:

- Exact model-based ordering in Rust is used for:
  - Browser sort by `Retrievability`
  - Review queue retrievability order (`ascending` / `descending`)
  - Filtered deck retrievability order (`ascending` / `descending`)
- `prop:r` filtering is exact-model-based. Search builds a temporary
  `search_exact_retrievability` table (`cid`, `r`, `s90`) from
  `FSRS::current_retrievability` and `FSRS::interval_at_retrievability(..., 0.9)`.
- `prop:s` filtering is exact-model-based and compares against `s90` (interval
  at 90% retrievability), not raw stored model stability.
- Card Info now shows both raw stored stability (`S`) and `S90`. `S90` is read
  via scheduler helper `fsrsNextInterval(card_id, stability, desired_retention=0.9)`,
  so it always follows the selected FSRS model/version.
- Add-on helper APIs expose exact interval-at-target-retrievability math:
  - `fsrs_interval_at_retrievability(card_id, stability, target_retrievability)`
  - `fsrs_interval_at_retrievability_batch([{card_id, stability}, ...], target_retrievability)`
  - `fsrs_interval_at_retrievability_by_config_batch([{request_index, config_id, stability}, ...], target_retrievability)`
  These call the same per-card selected-parameter path as `prop:s`.
- Legacy sqlite FSRS helper expressions continue to use stored scalar decay, but
  the standard retrievability search/order paths above no longer depend on them.
