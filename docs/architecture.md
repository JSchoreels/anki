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
3. Build samples `(R, Grade, taken_millis)` for review-kind entries.
   `R` is computed from the active FSRS model (`FSRS::current_retrievability`),
   so FSRS-7 uses its native forgetting-curve mixture instead of a single
   scalar decay approximation.
4. Aggregate samples into buckets:
   - `R`: 5% bands (`95-100`, `90-95`, ...).
   - `Grade`: Again/Hard/Good/Easy.
5. Resolve missing buckets by averaging nearby `R` buckets; if still empty,
   fall back to grade-level mean.
6. During `simulate_workload`, each simulated review uses
   `time(R_bucket, Grade)` to accumulate `daily_time_cost`.
7. The workload response includes a flattened matrix for UI inspection:
   - `review_time_fail_seconds`
   - `review_time_pass_seconds`
   - `review_time_sample_counts` (raw per-cell sample counts)
   with bucket dimensions:
   - `review_time_r_bucket_count`
   - `review_time_s_bucket_count` (fixed to `1`, UI compatibility)

Scope:

- This is currently applied only to the Help Me Decide workload simulation path.
- Normal review simulation and scheduler behavior are unchanged.
- During the DR sweep (`1..99`), existing simulated cards are re-bound to the
  current sweep DR before each run so `count/time/memorized` curves actually
  reflect the selected DR on all cards (not only newly introduced cards).

## FSRS Parameter Source

Deck options include an explicit FSRS version selector (`4.5/5/6/7`) stored in
`deck_config.config.fsrs_version`. Parameter editing and optimization target the
selected version's parameter array (`fsrs_params_4/5/6/7`).

Deck options also expose the global FSRS short-term toggle (`same-day review`
behavior for learning/relearning paths) backed by
`BoolKey::FsrsShortTermWithStepsEnabled`.

Runtime parameter lookup uses the selected version first; if that array is not
usable (`17/19/21/35` length with finite values), it falls back to best
available parameters for compatibility with existing collections.

Optimization writes newly computed parameters to `fsrs_params_7`.

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
