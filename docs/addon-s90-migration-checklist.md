# Add-on S90 Migration Checklist

This tracks add-on updates needed after `card.data.s` became the externally
visible S90 value and `card.data.s_int` became the FSRS internal stability used
by scheduling math.

## Rules

- Use `card.data.s` / `memory_state.stability` when displaying or exposing
  `prop:s`.
- Use `card.data.s_int` / `memory_state.stability_internal` for the internal slow
  stability. Exact FSRS-7 retrievability, interval, and next-state math must also
  preserve `card.data.s_fast` / `memory_state.stability_fast` and difficulty,
  and use the card's selected 34-parameter preset.
- Fall back from internal stability to S90 only when reading legacy data that
  does not have `s_int`.
- Do not rederive S90 from `s` in add-ons; Anki now writes S90 directly to `s`.
- Treat Anki's existing scalar-stability FSRS helper APIs as compatibility
  approximations for FSRS-7; they assume difficulty 5 and equal stability
  traces.

## FSRS Helper

- [x] Add a shared internal-stability accessor for Python `memory_state`
      objects.
- [x] Add a shared SQL expression/helper for `s_int` with fallback to `s`.
- [x] Update card template `fsrs-S` display to use stored S90 directly.
- [x] Update card template `fsrs-R` and Target R column to pass internal
      stability into the scalar compatibility math.
- [ ] Decide whether FSRS Helper should adopt a future full-state Anki API for
      exact FSRS-7 R and target intervals.
- [x] Update reschedule/recompute code to preserve internal stability when
      writing `FSRSMemoryState`.
- [x] Update postpone/advance/flatten/disperse/schedule-break paths that pass
      stability into FSRS interval or retrievability helpers.
- [x] Update FSRS Helper docs/tests for `s` as S90 and `s_int` as internal S.

## Search Stats Extended

- [x] Extend parsed card extra data with `s_int`.
- [x] Update FSRS math inputs to use `s_int ?? s`.
- [x] Leave S90 display/current stability graphs on `s`.
- [x] Remove now-stale S90 backend conversions where the source is already
      stored `s`, or pass internal stability when conversion is still needed.
- [x] Update `docs/FSRS_STABILITY.MD` to state the new card-data contract.
- [x] Add tests covering cards with both `s` and `s_int`.

## Dynamic Desired Retention

- [x] Update workload card stability extraction to prefer
      `memory_state.stability_internal`.
- [x] Keep `memory_state.stability` fallback for older builds.
- [x] Review grade-state adjuster separately: scheduling-state memory values
      may still be internal before card storage.
- [x] Add workload tests with distinct S90/internal values.

## AnkiConnect Extended

- [x] No code change expected: exported `prop:s` should remain S90.
- [ ] Optionally update README wording to clarify `prop:s` is S90 on this fork.

## Change FSRS Version for Presets

- [x] No code change expected: this add-on only manipulates deck config data.
