# FSRS Presets

FSRS presets are a scheduler read model for the FSRS-specific part of deck
options. They are derived from existing deck config rows by default, so no
separate database table or sync payload is required for the built-in behavior.

For each normal deck config, Anki exposes a matching FSRS preset with id
`deck-config:<id>`. The derived FSRS preset contains only FSRS model data:

- selected FSRS version
- selected FSRS parameter array
- desired retention
- historical retention
- ignore-before review-log date

When FSRS-7 is selected but its 34-parameter array is empty or invalid, the
derived preset uses the bundled FSRS-7 defaults. It does not substitute stored
parameters from FSRS-6 or an older model family.

Deck-options evaluation also resolves an empty FSRS-7 parameter field to the
bundled FSRS-7 defaults. The post-optimization comparison evaluates those defaults
and the optimized parameters with the same selected same-day review setting;
evaluating defaults does not fill or save the parameter field.

FSRS-6 and older presets with no usable stored parameters resolve to explicit
FSRS-6 defaults. Add-on presets with empty parameters resolve defaults for their
selected model family. Legacy parameter-only evaluation, memory-state, and
simulation helpers retain their empty-array convention of FSRS-6 defaults; they
do not inherit the upstream library's FSRS-7 empty-array default.

For existing collections, per-deck desired retention overrides are preserved
when resolving the FSRS preset for a card. If no deck-specific desired retention
is set, the resolver uses the deck config's desired retention.

Non-FSRS deck behavior remains owned by the deck config. This includes limits,
burying, timers, learning and relearning steps, interval modifiers, leech
settings, and other deck options.

Scheduler, search, browser, and stats code should resolve an FSRS preset for a
card instead of reading FSRS parameters directly from the card's deck config.
The default resolver maps a card to the FSRS preset derived from its home deck
config. Future add-on sources can provide the same narrow FSRS preset shape
without pretending to be full deck configs.

Python add-ons can read the resolved preset with
`Collection.fsrs_preset_for_card(card_id)`. The returned data is read-only and
uses the same resolver as review scheduling, so it includes add-on overlay
matches from `fsrsPresetOverlay` and falls back to the card's home deck config
when no overlay rule matches.

## Add-on Overlay

Add-ons can provide collection-scoped FSRS presets and ordered card matching
rules through the synced config key `fsrsPresetOverlay`. The value is JSON:

```json
{
    "presets": [
        {
            "id": "addon:example:medical",
            "name": "Medical",
            "fsrs_version": "seven",
            "params": [],
            "desired_retention": 0.9,
            "historical_retention": 0.9,
            "ignore_revlogs_before_date": ""
        }
    ],
    "rules": [
        {
            "search": "tag:medical",
            "preset_id": "addon:example:medical"
        }
    ],
    "simulator_rules": [
        {
            "preset_id": "addon:example:medical",
            "search": "deck:\"Medical\" firstgrade:1",
            "min_reps": 3,
            "max_reps": 7,
            "min_interval_days": 10,
            "max_interval_days": 30
        }
    ]
}
```

Add-on preset ids must start with `addon:`. Each rule's `preset_id` must refer
to a preset in the same overlay. Rules are evaluated in order, and the first
matching rule assigns the card's FSRS preset. Cards that do not match any rule
use the built-in `deck-config:<id>` FSRS preset derived from their home deck
config.

FSRS-7 add-on presets should provide either 34 final FSRS-7 parameters or an
empty parameter array. A 35-value FSRS-7 array from the old FSRS-7 preview is
accepted for compatibility, but Anki ignores those preview values and uses the
final FSRS-7 defaults for that add-on preset. Other invalid parameter counts or
non-finite values still reject the overlay.

If a built-in deck config still has a 35-value `fsrsParams7` array from the old
FSRS-7 preview, Anki warns on collection load. The warning can clear the stale
FSRS-7 parameters from the affected presets and open Deck Options so the user
can run Optimize All Presets. Deck Options also checks Optimize All Presets for
incompatible FSRS parameter arrays and asks before clearing them.

FSRS-7 add-on presets may include Dynamic DR policy fields. Current policies
should include `fsrs_dynamic_desired_retention_fixed_target_weights` and
`fsrs_dynamic_desired_retention_fixed_target_drs`; Anki uses those arrays before
the older `fsrs_dynamic_desired_retention_fsrs_eq_*` fallback when mapping a
fixed desired-retention target to an ADR cost weight.

During review, an explicit desired retention override supplied by the reviewer
scheduling hook takes precedence over the matched preset's desired retention.
The overlay value is used only when no review-time override is provided.

Rule searches must not use derived metric properties (`prop:r`, `prop:rwkv:r`,
`prop:rwkv-curve:r`, `prop:s`, or `prop:d`). Those properties already depend on
FSRS or RWKV state, or need an FSRS preset to compute, so allowing them in
source-selection rules would couple source resolution to derived scheduling
data.

`simulator_rules` are optional, ordered, simulator-only routes that allow the
FSRS simulator to switch add-on presets without calling back into Python or
running collection searches inside the simulation loop. They reference the same
overlay presets as `rules`, and currently support `min_reps`, `max_reps`,
`min_interval_days`, `max_interval_days`, and an optional `search` selector.
The selector is resolved once against the real starting cards, and matching then
uses that fixed card-id set plus the simulated state bounds. Repetition bounds
are evaluated against the simulated card's current repetition count; interval
bounds are evaluated against the card's current simulated interval before that
review is answered, matching persisted interval-style preset discrimination. The
first matching simulator rule supplies the simulated card's FSRS params and, if
available, Dynamic DR policy for that repetition. Cards that do not match a
simulator rule use their starting-card add-on Dynamic DR preset from `rules`,
when one matched, otherwise the simulator request's fallback preset. The normal
`rules` searches are resolved once for the real starting cards; `simulator_rules`
are only for preset changes caused by simulated state changes after that. Add-ons
that discriminate by persisted interval should emit simulator rules by splitting
the stable part into `search`, such as deck and `firstgrade:`, and the dynamic
interval part into `min_interval_days`/`max_interval_days`.

When `fsrsPresetOverlay` is created, updated, deleted, undone, or redone, Anki
invalidates the in-memory FSRS preset overlay cache. The cache is rebuilt from
the synced config value when FSRS preset resolution is next needed.

The cache stores validated add-on presets and parsed ordered rules, not a
collection-wide card-to-preset map. Review-time resolution checks the current
card against the ordered rules and caches that card's result, including
non-matches. Card, note, deck, tag, notetype, and deck-config changes clear only
these per-card matches, so stateful searches such as `firstgrade:` stay correct
without rebuilding every rule for every card after each review.

The shared multi-card resolver selects its strategy automatically. Small card
sets use the card-scoped resolver, avoiding the fixed setup cost of a temporary
search table. Larger sets use batch resolution: the unresolved card ids are
loaded into a temporary card-id table with a single insert, and one scan of
that table returns each card's first matching add-on overlay rule. The rules
become the ordered branches of one SQL `CASE` expression, so SQLite stops at a
card's first matching rule and later rules only examine cards that earlier
rules did not match. Within each rule, AND-joined terms that compare text
against note fields (field, unqualified, and regex searches) run after cheaper
deck, tag, card-state, and similar terms, so the deck part of a rule such as
`Front:re:... deck:Vocabulary` filters cards before any field is matched;
groups containing OR keep their written order. Later rules with an identical
search string are skipped, because any card they could match would already have
been taken by the earlier identical rule. Overlays with more than 256 distinct
rule searches fall back to one search per rule, removing matched cards from the
table between rules, which keeps each SQL statement small. When a rule uses
`firstgrade:`, the first grades for the card-id table are materialized once and
reused across rules. Cards not matched by the overlay are cached as non-matches
and resolved from in-memory deck and deck-config maps for that request, instead
of reading each card's deck config separately. This shared selection applies to
stats, search, reviewer refreshes, and other callers without requiring them to
choose a resolution strategy.

Field-qualified regular expressions inspect only the requested fields. The
SQLite search function splits the borrowed note-field bytes before validating
the selected fields' UTF-8 and stops after the last requested field, so a
nonmatching `Front:` rule does not scan large HTML fields later in the note.
On a copied 223,463-review collection with 11 overlay rules, the initial preset
matching pass fell from about 290 ms to 185 ms; every rule's matching card ids
and the complete RWKV history fingerprint were unchanged. This improves the
first resolution after opening a collection or invalidating its card matches;
later resolutions still reuse the existing per-card cache.

The single-scan batch resolution then halved that pass again. In the opt-in
release benchmark on a copy of the same collection (31,665 cards, 11 rules),
it fell from about 230 ms (38 ms of per-id inserts plus 192 ms of per-rule
searches and deletes) to about 110 ms (7 ms table setup plus a 106 ms scan).
The resolved card-to-preset map was byte-identical to the previous
implementation's, and matched per-card resolution for every card. To repeat
the measurement on a copied collection database:

```sh
ANKI_FSRS_PRESET_BENCH_COLLECTION=/path/to/copied/collection.anki2 \
  cargo test --release -p anki --lib fsrs_preset_overlay_batch_benchmark \
  -- --ignored --nocapture
```

`ANKI_FSRS_PRESET_BENCH_VERIFY=1` additionally compares every card with the
per-card resolver, `ANKI_FSRS_PRESET_BENCH_TRACE=1` prints the resolver's
per-rule debug timings, and `ANKI_FSRS_PRESET_BENCH_DUMP=<file>` writes the
resolved map for comparisons across builds. Most of that scan was the plain
`Front:_` rule, which matched a `LIKE` pattern against the whole note. Plain
field searches now match only the named fields, which brought the same pass to
about 70 ms with the same resolved map.

Large batches derive the built-in preset once per home deck and share immutable
preset data across matching cards, including add-on overlay matches. Home-deck
grouping preserves desired-retention overrides on decks sharing a deck config.
The result map reserves capacity for the batch. Derived built-in presets live
only for that request, so later deck or configuration changes are resolved anew.

Retrievability and relative-overdueness review queue builds use this shared
batch resolver and construct one FSRS model per resolved preset for each build.
Candidate scheduling state is read in bulk from the scheduling index using an
11-column projection, without a preliminary due-date sort or a separate lookup
for each card. An explicit queue-state type keeps only the fields used by queue
construction and exact scoring. The shared card-data JSON decoder still handles
memory state, retention, timestamps, and malformed-data defaults.
Preset resolution accepts card ID and home-deck ID; other callers can continue
to pass full cards through the existing wrapper. The queue selects these small
inputs only for candidates with memory state, preserving the memoryless fallback.
They still score every eligible due card from its complete current memory state
before applying deck limits. No retrievability scores are cached across queue
builds. See [the queue benchmark](fsrs-queue-performance.MD) for measurements and
the reproduction command.

## Batch Optimization

Add-ons that manage FSRS presets can optimize multiple draft presets with
`ComputeFsrsParamsBatch`. The request reads review logs through each item's
search, current parameter array, selected FSRS version, relearning step count,
ignore-before timestamp, same-day review training flag, and scheduling-penalty
flag. The scheduling-penalty flag defaults to disabled. It does not write deck
configs, cards, review logs, or `fsrsPresetOverlay`; callers receive the
optimized params and decide where to store them.

The batch optimizer reuses the same prepared FSRS training data path as
single-preset optimization. After preparation, jobs are balanced by training
target count and run on Rayon worker lanes. Progress is reported with the same
`ComputeAllParamsProgress` payload used by deck-options Optimize All, including
per-preset review counts and iteration progress.

After deck-options Optimize All finishes parameter optimization, Anki reports a
short per-preset save phase while writing the optimized deck configs. It then
updates each affected preset's card memory state from the final saved config.
If "Reschedule cards on change" is enabled, that same pass also updates due
dates and writes rescheduled review-log entries. The progress payload includes
the active preset name, preset index, and per-preset counts so the UI can show
the save and update/reschedule stages in the same multi-lane progress view used
by parameter optimization.

## Scheduler Selection In Deck Options

The Scheduler panel selects the preset's review model: FSRS-6, FSRS-7,
RWKV-Curve, or RWKV-Instant. The selectors offer only these current models.
Instant adds **Fallback : Due-Date Calculator** for its FSRS or Curve companion;
this controls stored due dates while Instant independently selects reviews.
The selectors reuse `fsrs_version`, `rwkv_review_enabled`, and
`rwkv_review_instant_order_enabled` and retain each model's stored parameters.
Opening the panel does not rewrite those fields. Existing FSRS-4.5 and FSRS-5
choices remain saved until a current model is explicitly selected.

The Scheduler panel introduces FSRS and RWKV above the selectors, with links to
their documentation and benchmark. Blue stars recommend RWKV-Instant for review
selection and FSRS-7 for Instant's fallback due-date calculation. These are
guidance labels; opening the panel does not apply the recommendations.

Model settings cards follow the selector order: review scheduler first, then its
fallback. Models that share a card appear together; Instant precedes Curve inside
RWKV. Reordering cards preserves their editing state and retention baseline.

When FSRS selects reviews, retention edits show a compact workload estimate
relative to the initial desired retention. It estimates FSRS interval-based
workload only. The estimate and its calculation are hidden when FSRS is only
RWKV-Instant's fallback, because Instant selects reviews independently of those
intervals.

The visible Machine Learning Based scheduling switch remains collection-wide.
Selecting a model enables it; other presets retain their own selected models.
Per-preset SM2/FSRS coexistence is not introduced by these UI selectors.

## Fork-Owned Storage

Fork-owned FSRS preset fields are stored in deck config `other` under the
`jschoreels.fsrs` key. This includes FSRS-7 params, the selected FSRS version,
minimum FSRS interval, Dynamic DR fields, and legacy deck-level review-fuzz
fields.

Desktop-only RWKV review settings are stored separately in deck config `other`
under the `jschoreels.rwkv` key. See `rwkv.MD` for RWKV queue scoring, warm-up
state, settings, inference runtime, and Rust/NEON parity coverage.

When reading deck configs through the legacy Python `DeckConfigDict` API,
fork-owned `other` entries may appear either under the `other` object or as
top-level keys such as `jschoreels.fsrs` and `jschoreels.rwkv`. Code that
interprets these values should accept both shapes, while writes should continue
to target the nested `other` storage documented above.

Older fork builds wrote those values to numbered `DeckConfig.Config` protobuf
fields `47-65`, `67-74`, `78-79`, and `82-84`. A short-lived fork build also
wrote RWKV fields into `other["jschoreels.fsrs"]`. Current code still reads
those legacy locations, but
normal deck config writes migrate FSRS fields into `other["jschoreels.fsrs"]`,
RWKV fields into `other["jschoreels.rwkv"]`, and clear the numbered fork fields
before encoding. This avoids collisions if upstream later assigns one of those
protobuf field numbers to a different setting.

## Current card metrics

`Collection.card_memory_metrics(card_ids, include_retrievability=True)` reads
current memory state, stored desired retention, and optional current FSRS
retrievability in a single backend call. It omits missing cards and preserves
the order and duplicates of existing cards. Absent values remain absent.
The stored desired retention is not replaced with a preset default.

With `include_retrievability=False`, only stored card values are read. Otherwise
the native full-state calculation uses the same selected presets, fractional
elapsed time, and missing-last-review-time lookup as Card Info. Preset resolution
is batched and request scoped. No historical states are reconstructed, and no
card values or review logs are written. Missing state on a reviewed card remains
absent; consumers requiring Card Info's initialization behavior should fall back
to `card_stats_data()` for that card. The existing full statistics API is unchanged.


`Collection.card_details()` additionally returns compact scheduling fields and
optional selected note fields, reusing the same stored card reads for requested
FSRS metrics. Metrics and note fields are opt-in. Passing `note_fields=None`
skips note access; an empty list returns a present empty field selection. Selected
fields retain their exact names, raw values, and note-type order. Only selected
field values are copied from SQLite's borrowed field text into the response;
full notes are not sent across the Python/Rust bridge. Note and field-selection
caches are scoped to one request. Missing cards are omitted, while absent note
fields signal a missing note, note type, or selected field index so consumers can
retain their original error handling. No collection schema or stored data changes.
