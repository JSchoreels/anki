# Fork Release Notes

This file tracks user-visible changes specific to the Anki FSRS7 fork.
The machine-readable application version remains [`.version`](./.version).
Upstream Anki changes are inherited when the fork is synchronized, but are not
repeated here unless they materially affect a fork feature.

## Maintenance

- Add user-visible fork changes to **Unreleased** in the same commit as the
  change.
- Describe outcomes for users rather than implementation details or commit
  titles.
- Include fixes, new behavior, compatibility changes, migrations, and notable
  performance or security changes. Omit formatting, tests, CI-only changes, and
  routine upstream synchronization.
- Before publishing, rename **Unreleased** to the intended application version
  and date, then add a new empty **Unreleased** section above it. Release build
  numbers may be recorded separately when useful.
- Treat [`.version`](./.version) as authoritative if this file and the build
  version ever disagree.

## Unreleased

Current application version: `26.05+fsrs7`

Changes since
[`26.05+fsrs7.build.72`](https://github.com/JSchoreels/anki/releases/tag/26.05%2Bfsrs7.build.72)
(2026-07-22):

### Added

- Added `prop:fsrs:r` for FSRS-only retrievability searches and `prop:rwkv:r`
  for RWKV-only searches. Existing `prop:r` retains its hybrid RWKV-first,
  FSRS-fallback behavior.
- Pre-score RWKV-dependent searches and retrievability ordering before
  rebuilding filtered decks, avoiding an unintended FSRS fallback.

### Improved

- Use one consistent priority across RWKV score sources: Card Info, review
  queue, statistics, then background deck counts.

### Fixed

- Keep every card in a filtered deck counted as due on the deck list instead of
  reapplying RWKV eligibility and showing only the daily minimum.

## [26.05+fsrs7.build.72](https://github.com/JSchoreels/anki/releases/tag/26.05%2Bfsrs7.build.72) — 2026-07-22

### Added

- Added a configurable minimum number of daily RWKV reviews, including
  parent/subdeck targets.

### Improved

- Split RWKV queue refreshes into smaller asynchronous stages, reject stale
  results, preserve the visible card, and refresh counts after the next
  question appears.
- Refresh RWKV targets, scores, queues, and due counts after Dynamic Desired
  Retention rules change.
- Keep overview and deck-browser counts pending while resident state is restored
  instead of briefly displaying stale values.

### Fixed

- Prevent deleted cards from remaining visible after **Undo → Delete**, and
  prevent the previous card's front from appearing when flipping the current
  card.
- Keep queue rebuilds scoped correctly when rebuilding filtered decks or moving
  between filtered and normal decks.
- Handle malformed deck hierarchies and cached preset assignments without
  `deck not found in limits map` failures.
- Keep RWKV state replay, rescheduling, and due-count refreshes synchronized.

### Security

- Updated `ammonia` to address `RUSTSEC-2026-0213`.

## [26.05b1+fsrs7.build.65](https://github.com/JSchoreels/anki/releases/tag/26.05b1%2Bfsrs7.build.65) — 2026-07-18

### Added

- Added optional RWKV-Curve answer-button intervals, independently configurable
  from RWKV-Instant queue selection.
- Added resumable, cached, day-by-day Memorised history replay.
- Added RWKV/FSRS S90 comparisons in Card Info and support for the paired UM+
  comparison graph in Search Stats Extended.
- Added FSRS/RWKV workload comparisons and **Reschedule with RWKV Curve**.

### Improved

- Improved workload simulation for new cards, daily limits, and leech
  suspension.
- Made queue refreshes and deck-list counts more responsive.
- Accelerated Memorised replay on AVX2/FMA-capable x86 processors.

### Compatibility

- Existing RWKV state and Memorised caches rebuild once after upgrading.
- Same-day repeats default to five intervening reviews and a 30-second minimum.
- Removed the experimental tag-state, Japanese feature-state, and
  self-correction controls.
- RWKV remains desktop-only; other clients continue using FSRS or SM-2.

## [26.05b1+fsrs7.build.61](https://github.com/JSchoreels/anki/releases/tag/26.05b1%2Bfsrs7.build.61) — 2026-07-12

### Added

- Added RWKV review ordering by predicted retrievability with configurable
  scoring batches, refresh frequency, candidate refreshes, and repeat spacing.
- Added FSRS grade scheduling from RWKV retrievability so desktop RWKV reviews
  remain compatible with mobile FSRS scheduling.
- Added after-review RWKV predictions in Card Info and tools for preparing,
  rebuilding, comparing, and applying RWKV state and intervals.

### Improved

- Reduced pauses during queue scoring, state rebuilding, replay, calibration,
  and Card Info predictions.
- Expanded RWKV statistics, workload analysis, and historical calibration.

### Fixed

- Fixed ascending retrievability order and queue counts affected by RWKV
  eligibility or repeat spacing.
- Fixed answer-side image rendering, Intel Mac audio, whitespace handling in
  searches, and empty-card detection with special-field conditions.
- Fixed list shortcuts stealing text focus, **Optimize All Presets** closing
  Deck Options, interface language handling, and several Windows installer
  upgrade cases.

### Compatibility

- Raised the minimum supported macOS version to macOS 13.

## [26.05b1+fsrs7.build.55](https://github.com/JSchoreels/anki/releases/tag/26.05b1%2Bfsrs7.build.55) — 2026-07-07

- Published the second RWKV beta together with the FSRS7 update.
- The GitHub release contains a
  [full commit comparison](https://github.com/JSchoreels/anki/compare/26.05b1%2Bfsrs7.build.47...26.05b1%2Bfsrs7.build.55);
  detailed per-change release notes were not published for this build.

## [26.05b1+fsrs7.build.47](https://github.com/JSchoreels/anki/releases/tag/26.05b1%2Bfsrs7.build.47) — 2026-07-03

- Published the first substantial RWKV beta and its initial deck-option
  controls.
- Noted that initial state building was still slow outside macOS, with x86 SIMD
  optimization planned.

## [26.05b1+fsrs7.build.41](https://github.com/JSchoreels/anki/releases/tag/26.05b1%2Bfsrs7.build.41) — 2026-06-20

- Fixed FSRS-enabled decks failing to open reviews on AnkiMobile with
  `invalid parameters provided`.
- Preserved tiny FSRS stability values in a mobile-compatible form, and repaired
  existing zero-stability cards with **Check Database**.
