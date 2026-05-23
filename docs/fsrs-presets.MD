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
    ]
}
```

Add-on preset ids must start with `addon:`. Each rule's `preset_id` must refer
to a preset in the same overlay. Rules are evaluated in order, and the first
matching rule assigns the card's FSRS preset. Cards that do not match any rule
use the built-in `deck-config:<id>` FSRS preset derived from their home deck
config.

During review, an explicit desired retention override supplied by the reviewer
scheduling hook takes precedence over the matched preset's desired retention.
The overlay value is used only when no review-time override is provided.

Rule searches must not use FSRS metric properties (`prop:r`, `prop:s`, or
`prop:d`). Those properties already depend on FSRS state or need an FSRS preset
to compute, so allowing them in source-selection rules would couple source
resolution to derived FSRS data.

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
ignore-before timestamp, and same-day review training flag. It does not write
deck configs, cards, review logs, or `fsrsPresetOverlay`; callers receive the
optimized params and decide where to store them.

The batch optimizer reuses the same prepared FSRS training data path as
single-preset optimization. After preparation, jobs are balanced by training
target count and run on Rayon worker lanes. Progress is reported with the same
`ComputeAllParamsProgress` payload used by deck-options Optimize All, including
per-preset review counts and iteration progress.
