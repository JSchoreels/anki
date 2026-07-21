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

Rule searches must not use FSRS metric properties (`prop:r`, `prop:s`, or
`prop:d`). Those properties already depend on FSRS state or need an FSRS preset
to compute, so allowing them in source-selection rules would couple source
resolution to derived FSRS data.

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

Stats graph generation resolves presets for searched cards with FSRS memory
state in batches. Each add-on overlay rule is searched once and intersected with
those cards through a temporary card-id table, preserving first-match rule
order. Later rules with an identical search string are skipped, because any card
they could match would already have been consumed by the earlier identical rule.
When a rule uses `firstgrade:`, the first grades for the card-id table are
materialized once and reused across rules. Cards not matched by the overlay are
cached as non-matches and resolved from in-memory deck and deck-config maps for
that graph request, instead of reading each card's deck config separately.

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
