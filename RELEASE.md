# Fork Release Notes

This file tracks changes specific to the Anki FSRS7 fork.
The machine-readable application version remains [`.version`](./.version).
Upstream Anki changes are inherited when the fork is synchronized, but are not
repeated here unless they materially affect a fork feature.

## Maintenance

- Add user-visible fork changes to **Unreleased** in the same commit as the
  change.
- Split each release into **User facing changes** and **Technical details**.
  Describe practical outcomes in the first section. Put implementation context,
  upstream references, compatibility notes, measurements, and verification in
  the second section.
- Include concise, verified before/after measurements where available and useful. Prefer these figures to illustrative scenarios; do not force an example onto every change or invent measurements.
- Keep release-note paragraphs on one source line and let GitHub wrap them.
  Use line breaks for Markdown structure, not a fixed prose width.
- Include fixes, new behavior, compatibility changes, migrations, and notable
  performance or security changes. Omit routine formatting, CI-only changes,
  and upstream synchronization without a notable impact.
- Before publishing, rename **Unreleased** to the intended application version
  and date, then add a new empty **Unreleased** section above it. Release build
  numbers may be recorded separately when useful.
- Treat [`.version`](./.version) as authoritative if this file and the build
  version ever disagree.
- The release workflow includes the matching version section (or **Unreleased**
  for an unsigned draft build) before GitHub's generated commit list. Keep that
  section complete for the exact release commit; an empty section blocks release
  creation.

## Unreleased

Changes since [build 99](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.99).

### User facing changes

- **Choose the scheduler in one place.** Segmented buttons select FSRS-6, FSRS-7, RWKV-Curve, or RWKV-Instant for each preset. Instant adds a Fallback : Due-Date Calculator for mobile sync and statistics. The visible Machine Learning Based scheduling switch still applies to the whole collection.
- **Clearer scheduler settings.** FSRS and RWKV controls appear in matching cards below Scheduler, with the review scheduler first and its fallback next. Shared desired retention stays above them, and only the relevant model controls appear. Blue stars recommend RWKV-Instant for reviews and FSRS-7 for fallback due dates; short model descriptions explain the choices and link to further reading.
- **Clearer FSRS workload estimates.** One line shows the estimated workload relative to the initial desired retention, with a note that it covers FSRS interval-based workload only. Instant's FSRS fallback does not show this estimate.
- **Compact, consistent RWKV repeat spacing.** Set the minimum other reviews and elapsed seconds in one row, with units inside the fields. Instant follows the shared same-day review setting. Learning and relearning cards also respect both minimums, including already-due steps and cards offered through Learn ahead, while their saved intervals and learning steps are preserved.
- **Simpler RWKV options.** The interval-order option spells out Again ≤ Hard ≤ Good ≤ Easy, and review-order guidance recommends Ascending Retrievability or Random. Creation-time predictions for new cards remain enabled without a toggle. The exit-refresh switch is hidden while preserving its saved behavior. Dynamic Preset support is controlled by the add-on's global RWKV support option.
- **No pause after editing a card during RWKV reviews.** Editing fields no longer causes a roughly two-second freeze on the next answer, and the edited card keeps its RWKV intervals on the answer buttons.

### Technical details

- **Scheduler compatibility:** the selectors reuse the existing FSRS version and RWKV configuration fields, retain each model's parameters, and enable collection-wide machine learning scheduling when a model is selected. FSRS-4.5 and FSRS-5 are no longer offered, but older saved choices remain effective until explicitly changed. Keyed card ordering follows the selected models while retaining editing state and the initial retention baseline. Per-preset SM2/FSRS coexistence is unchanged.
- **RWKV learning selection:** learning and review paths share the repeat-spacing calculation. Learning eligibility reads current answer times and recent answers in the selected deck tree, filters reviewer counts consistently, and preserves undo/redo queue snapshots. Learning eligibility uses its configured or generated interval without adding Instant's recall threshold.
- **RWKV configuration:** creation-time prediction ignores the retired deck-option flag in desktop and native query paths. The synced `rwkvDynamicPresetReplay` collection boolean overrides legacy deck-preset replay flags; until explicitly saved in the add-on, the old choice remains effective. Existing cache identities detect changes in effective replay behavior.
- **RWKV state after editor saves:** reconciliation markers are queued per save, and edited cards' FSRS preset-cache entries are refreshed in place. Overlapping saves no longer lose a marker or expose a transient cache miss that discarded warm resident state.
- **Verification:** `just check` and the full local browser suite passed (47 tests passed, one existing test skipped), including FSRS optimization, live card ordering, retained editing state, and suppression of workload calculations when FSRS is Instant's fallback. Additional renders checked dark and light themes, recommendation placement, and the single-line workload estimate.
- **Packaging:** the draft workflow builds unsigned installer and portable downloads for macOS, Windows, and Linux on ARM64 and x64. The application version remains `26.09.3+fsrs7`; GitHub assigns the draft a new build suffix.

## [26.09.3+fsrs7.build.99](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.99) — 2026-10-07

Based on [Anki 26.09.3](https://github.com/ankitects/anki/releases/tag/26.09.3).
Changes since [build 97](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.97).

### User facing changes

- **Restored user interface sizing.** The User interface size preference takes effect again after restarting Anki.
- **Faster Browser selections.** Selecting or inverting thousands of rows and resizing columns with a large selection are more responsive (Select All: ~434 ms → ~0.9 ms in a 50,000-row synthetic test).
- **Lower memory use during full collection downloads**, especially for large collections.
- **Faster media scans** when files added locally or downloaded from AnkiWeb have not changed.
- **Faster RWKV calibration refreshes after FSRS parameter changes** when RWKV already has predictions for the complete review history (~21 s → ~6 s on a 224,000-review collection).
- **Fewer interruptions when adding cards through add-ons.** AnkiConnect and Yomitan mining no longer cause an RWKV recovery progress window and tooltip after every card, including while Anki is in the background.
- **More reliable RWKV calibration graphs.** Recomputing calibration removes outdated cached predictions that could otherwise appear in the graphs.
- **Correct Deck Options help links** for daily limits and leeches.

### Technical details

- **UI scaling:** backported [Anki #5686](https://github.com/ankitects/anki/pull/5686), fixing [#5676](https://github.com/ankitects/anki/issues/5676). The saved scale factor is applied before Qt creates the application, with a startup regression test.
- **Browser:** backported Anki [#5768](https://github.com/ankitects/anki/pull/5768) and [#5771](https://github.com/ankitects/anki/pull/5771). The column header uses an empty selection model to avoid scanning selected rows when painting. Menu actions count changed selection ranges, while preserving the fork's fallback for add-ons overriding model flags.
- **Browser measurements:** on 50,000 synthetic rows with offscreen Qt on Apple Silicon, median Select All time fell from 434 ms to 0.91 ms, Invert Selection from 416 ms to 0.85 ms, and header painting from 90 ms to 0.59 ms. These measure individual operations, with existing Clanki improvements present before and after. See the [backport audit](https://github.com/JSchoreels/anki/blob/cd6ed9a13558d2b89d52460d6970d228b09754bd/docs/upstream-performance-backports.MD) for methodology and regression coverage.
- **Full sync:** backported [Anki #5717](https://github.com/ankitects/anki/pull/5717). Downloads stream through a buffered temporary file, are flushed and checked for integrity, then atomically replace the local collection.
- **Media sync:** adapted [Anki #5654](https://github.com/ankitects/anki/pull/5654), open at the 2026-10-07 review. Scans, local additions, and downloads use millisecond modification timestamps. Older timestamps require one checksum scan before using the fast path; the database schema is unchanged.
- **RWKV calibration:** complete cached predictions allow FSRS fold assignments to be refreshed without replaying review history (about 6 s instead of 21 s on a 224,000-review collection). After a full recompute, each answer stores its RWKV prediction. Rebuilt or recovered state, unseen synced reviews, or a different model still require a full recompute. Recomputes also remove superseded fold assignments and leftover training rows.
- **RWKV state:** add-on and legacy resets check whether the resident state still matches review history and retain it when unchanged, avoiding a later disk reload and recovery notification.
- **Verification and packaging:** `just check`, the online Rust tests, and 212 permanent Browser regression tests passed locally. Remote CI passed on Linux, Windows, and macOS, including Linux browser end-to-end tests. Build 99 provides 12 unsigned installer and portable downloads across macOS, Windows, and Linux on ARM64 and x64.

## [26.09.3+fsrs7.build.97](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.97) — 2026-10-06

Based on [Anki 26.09.3](https://github.com/ankitects/anki/releases/tag/26.09.3).
Changes since [build 96](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.96).

### Fixed

- Anki no longer freezes and shows “Creating backup…” every few minutes while
  reviewing. Build 96 re-verified the latest backup on every 5-minute check;
  backups are now fully verified only when written and before older ones are
  removed, and are still created at the configured interval.

### Changed

- “Allow same day review for (re)learning steps” now also controls FSRS-7 and
  RWKV-Curve short-term scheduling. With it off, empty or exhausted steps
  schedule generated intervals as reviews of at least one day; explicitly
  configured learning/relearning steps still apply.
- The option now defaults to on, including the reset control in Deck Options.
  Choices explicitly saved as off remain off. The manual notes that queue
  skipping bypasses manual steps while keeping their saved values.

### Diagnostics

- When RWKV scoring prevents a filtered-deck rebuild, the log names the deck,
  both filters and the reason, making reports easier to diagnose.

All 12 installer and portable downloads are available for macOS, Windows and
Linux, on ARM64 and x64. This build is unsigned.

[Full commit comparison](https://github.com/JSchoreels/anki/compare/26.09.3%2Bfsrs7.build.96...26.09.3%2Bfsrs7.build.97)

## [26.09.3+fsrs7.build.96](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.96) — 2026-10-04

Based on [Anki 26.09.3](https://github.com/ankitects/anki/releases/tag/26.09.3).
Changes since the last normal release,
[build 92](https://github.com/JSchoreels/anki/releases/tag/26.09b3%2Bfsrs7.build.92),
including prerelease 94 and the unpublished drafts.

### Improved

- Faster Browser selections, table painting, sidebar filtering and field
  searches; faster Empty Cards scans, deck counts and finished study screens.
  Built-in web assets are reused within each session.
- Faster RWKV startup, history preparation, sync refresh and preset-rule matching,
  plus faster model steps and review scoring on Apple Silicon.
- Lower memory use for RWKV/FSRS workload comparisons, RWKV simulations and
  calibration; calibration also avoids a pause when restoring the resident state.
- Updated FSRS-7 optimization with faster x86 training and repeatable evaluation,
  while retaining better existing parameters. FSRS preset saves recompute memory
  states faster.
- Reduce Windows mpv command-response delays.

### Fixed

- RWKV-Instant respects the same-day review setting even when learning queues
  are skipped. Repeated same-day answers no longer add lapses or trigger leeches
  across schedulers, including filtered decks and Grade Now; Again on the day's
  first review answer still counts.
- Preserve new/review mixing with RWKV disabled, and show the reviewer’s due
  total before daily limits alongside the limited count.
- Recover FSRS-7 memory state stripped by AnkiWeb sync, preserving due dates and
  intervals without uploading repair-only changes.
- Retain RWKV state when sync brings older reviews, refresh post-sync deck counts
  and preset assignments, and recover after deck/preset changes. Card Info
  reflects the current preset and refreshes when RWKV becomes ready.
- Wait for concurrent RWKV scoring in filtered decks; preserve undo after
  calibration and advance an undo-restored card on redo.
- Prevent interrupted writes, filename collisions and corrupt files from
  displacing valid automatic backups; retry failed backups.
- Correct field-name wildcard searches and match non-ASCII letters regardless
  of case.
- Refresh edited answers correctly, prevent blank web pages and errors from
  callbacks to closed windows, and shut down quietly.
- Correct average intervals in notes mode and notify add-ons once at scheduler
  day rollover.

Performance depends on the collection and hardware. The
[benchmarks](docs/clanki-improvements-benchmark.MD) found no clear overall
Check Database speedup; Windows audio timings use a simulated transport.

All 12 installer and portable downloads are available for macOS, Windows and
Linux, on ARM64 and x64. This build is unsigned.

[Full commit comparison](https://github.com/JSchoreels/anki/compare/26.09b3%2Bfsrs7.build.92...5bd75435436f9badb38ef9ef6433bdd6ec3d8601)
· Source: `5bd75435436f`.

## [26.09.3+fsrs7.build.94](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.94) — 2026-10-01

Current application version: `26.09.3+fsrs7`

### Fixed

- Preserve “Mix with reviews” ordering when RWKV ordering is disabled, instead
  of repeatedly restarting the queue and showing long runs of new cards.
- Keep FSRS evaluation RMSE and model-comparison metrics identical across
  repeated runs with the same inputs.
- Use far less memory when comparing RWKV with FSRS in the deck options, which
  simulates every card of the preset and could run out of memory on large
  collections. Simulating 13,800 cards peaked at 2.8 GB instead of 8.3 GB,
  with identical results; the default RWKV workload simulation also uses less.
- Find notes where any of the fields named by a field-name wildcard matches,
  such as `word*:foo`. When the named fields were next to each other, exact
  searches found nothing and wildcard searches could match text spread across
  two fields.
- Restore deck options and other web pages that could open blank after a build
  ran alongside the web checks.
- Include parent RWKV daily review minimums in subdeck counts when parent
  limits apply, so the deck list reflects the reviews available after opening
  a subdeck.
- Show the next card immediately after deleting a note during RWKV reviews,
  instead of rescoring the whole deck first.
- Keep RWKV scheduling active after deleting a card that has review history.
  The RWKV state is rebuilt without the deleted reviews at the next launch
  instead of being discarded immediately.
- Stop the local RWKV state cache from growing each time Anki starts with new
  reviews, for example after syncing reviews done on another device. Space
  already taken by this is reclaimed automatically at the next start; one
  desktop cache shrank from 3.1 GB to 1.4 GB.
- Keep the RWKV state when a sync brings in reviews older than 8 days, as
  intended, instead of failing and rebuilding the whole state (about 20s on a
  223,000-review collection). Reviews synced from another device are now
  recognized as such even though they also update their cards.
- Update deck list RWKV counts once the RWKV state has been refreshed after a
  sync, instead of leaving them pending until the next refresh.
- Apply FSRS preset rules to notes, cards, tags and decks changed by a sync
  right away; previously cards could keep their earlier preset until another
  change on this device.

### Improved

- Update FSRS-7 optimization to use the training settings from its accuracy
  benchmarks and start from the default parameters. With fewer than 64 training
  targets, it no longer fits initial stabilities separately. Existing parameters
  are still kept when they predict better. FSRS-7 optimization also uses a faster
  kernel on x86.
- Search specific note fields faster, in the browser, filtered decks and FSRS
  preset rules. On a 16,000-note collection with large notes, `Front:_` took
  about 25ms instead of 85ms, and `Front:nc:*a*` took 80ms instead of 2.2s.
  Field searches now ignore case for all letters, like regular expression
  searches, so `Front:école` also finds `École`; previously only a-z were
  matched regardless of case.
- Prepare RWKV review history faster when rebuilding the state cache. On a
  223,000-review collection, preparation took 1.4s instead of 3.1s, with
  identical replay inputs and history identity.
- Resolve FSRS preset rules faster at RWKV startup when rules search specific
  note fields. On a 223,000-review collection, the first preset-matching pass
  took about 185ms instead of 290ms, with identical matches and history checks.
- Match all FSRS preset rules in a single pass over the cards instead of one
  search per rule, checking a rule's deck, tag and card conditions before its
  note field conditions. With 11 rules on a 31,000-card collection, and the
  faster field searches above, the first preset-matching pass takes about 70ms
  instead of 230ms, with identical matches.
- Show ordinary statistics while the RWKV model is warming up, then refresh
  the RWKV graphs when scores become available.
- Load the saved RWKV state faster at startup; restoring a 1.3 GB state took
  0.5s instead of 2.2s when it was not already cached by the system.
- Speed up the RWKV Desired Retention workload simulator and reduce its memory
  use, with identical results; a default simulation of a large collection took
  47s instead of 120s and peaked at 2.3 GB instead of 5.5 GB.
- Get RWKV scheduling ready sooner after launch. The review-history check
  that guards the saved RWKV state is about 3x faster (0.23s instead of 0.66s
  for 223,000 reviews), and the state now loads while that check runs instead
  of after it.
- Refresh RWKV deck list counts faster for decks with many subdecks; a deck
  with 26 subdecks took 45ms instead of 110ms.
- Score RWKV decks faster when the Dynamic Desired Retention add-on is
  installed. Anki passes card ids to add-on versions that accept them instead
  of loading every card, and the add-on reads only the note fields its rules
  use; resolving desired retention for a 12,300-card deck took 0.23s instead
  of 0.9s.
- Update the RWKV state faster after a sync. Reviews downloaded from another
  device are now added to the saved state instead of re-reading the whole
  review history; on a 223,000-review collection, syncing 50 reviews took
  1.2s instead of 5.3s, and a sync without new reviews 1.2s instead of 1.6s.
- Keep FSRS preset rule matches when unrelated settings change, such as the
  selected deck, instead of re-running every rule search; the next RWKV
  history check on a 223,000-review collection took 0.22s instead of 0.49s.
- Speed up RWKV model steps on Apple Silicon. A 180-day workload simulation
  of 250 cards took 65s instead of 80s, with identical results, and scoring
  an 8,900-card deck's RWKV review queue took 78ms instead of 97ms, with
  retrievabilities within 0.0000002 and an unchanged card order.
- Rescore the RWKV review queue faster after each answer on Apple Silicon,
  with identical scores and card order; scoring an 8,900-card deck took about
  82ms instead of 93ms.
- Recompute RWKV calibration data with less overhead and memory. The current
  RWKV state is now set aside during the replay instead of being copied; on a
  223,000-review collection, this took about 1ms instead of 1.2s, used 1.3 GB
  less memory at its peak, and no longer froze Anki for up to 0.4s while the
  state was put back. Undoing an answer given before the recompute rolls back
  its RWKV state directly again.

## [26.09b3+fsrs7.build.91](https://github.com/JSchoreels/anki/releases/tag/26.09b3%2Bfsrs7.build.91) — 2026-09-21

Current application version: `26.09b3+fsrs7`

The following entries collect the fork changes released in builds 89–91.

### Added

- Let add-ons batch compact card details and selected note fields, reusing card
  reads for current FSRS metrics without transferring complete notes to Python.
- Let add-ons fetch current FSRS metrics in batches without reconstructing each
  card's review-history statistics, while retaining the full Card Info API.

- Add a daily-limits option that lets same-day repeats bypass the maximum
  reviews/day limit without reducing the number of ordinary reviews available.

### Fixed

- Update the upstream FSRS engine to validate FSRS-7 fast stability, while
  preserving FSRS-6 defaults for older presets and legacy evaluation calls.
- Evaluate an unoptimized FSRS-7 preset with the bundled FSRS-7 defaults, so
  the optimization comparison honors the selected same-day review setting.
- Preserve Japanese readings and other text committed just before leaving an
  editor field, including when closing the editor during a review.
- Recover unavailable RWKV state and refresh an empty review queue before ending
  the session, so collection changes do not prematurely return to the overview.
- Make RWKV deck-list counts match opening each subdeck when parent and child
  decks have different same-day repeat rules or intervening review histories.
- Reduce pauses when rebuilding retrievability-sorted review queues with large
  due-card backlogs by reading candidate state and resolving FSRS presets in
  batches, decoding only the needed scheduling columns, avoiding redundant
  sorting and allocations, and reusing models and immutable preset data.
- Keep S90 and FSRS-7's internal stability distinct when converting legacy
  schedules, serving add-on interval requests, rescheduling, and accepting
  cards from clients that do not preserve FSRS-7's extra state fields.
- Make FSRS-7 use exact fractional elapsed time and unrounded sub-day answer
  intervals, with a consistent 12-hour cutoff across review and learning cards.
- Keep RWKV-Curve answer intervals and S90 values fractional until scheduling,
  and locate target-retention crossings accurately on the predicted curve.
- Use the bundled FSRS-7 defaults when a preset has not been optimized for
  FSRS-7, instead of falling back to parameters from an older FSRS version.
- Prevent editing a card during RWKV reviews from prematurely ending the study
  session when only RWKV-selected cards remain.
- Prevent an empty pre-refresh RWKV queue from prematurely ending the study
  session when a deferred post-answer refresh can provide more cards.
- Recover a cold RWKV state once with explicit progress after leaving reviews,
  while keeping deck and overview counts responsive and preventing count
  refreshes from silently replaying the full review history.
- Keep RWKV queue state warm when reviewed cards are reset to New, explain that
  their previous reviews remain in use, and defer starting their RWKV history
  from scratch until the next explicit state rebuild.

## 26.09b3+fsrs7 — 2026-09-11

Current application version: `26.09b3+fsrs7`

This release aligns the fork with the
[official Anki 26.09b3 beta](https://github.com/ankitects/anki/releases/tag/26.09b3)
while retaining the fork's FSRS-7, Dynamic Desired Retention, RWKV scheduling,
performance, portable-build, and reviewer-editing enhancements.

### Added

- Show the total due reviews beside limited deck-list counts, with a tooltip
  explaining daily review limits.

- Publish portable editions for macOS, Windows, and Linux alongside the normal
  installers in GitHub releases.

### Fixed

- Make Browser `prop:rwkv:r` and `prop:rwkv-curve:r` searches calculate their
  own current scores instead of depending on scores cached by another screen.

- Explain how to move the macOS portable folder when Gatekeeper starts it from
  a read-only temporary location instead of failing with an opaque filesystem
  error.

- Compute built-in FSRS-7 retrievability and Relative Overdueness from the full
  dual-trace memory state and selected preset, including Browser/search,
  filtered decks, review queues, Card Info, and deterministic queue ties.

- Apply both ascending and descending retrievability order globally across due
  review, interday-learning, and due-now intraday cards before review limits.

- Restore aggregate progress, ETA, active preset bars, and completion/skip
  details while **Optimize All Presets** runs concurrently.

- Correct FSRS-7 Dynamic DR intervals to use the full memory state, including
  fast stability.

- Draw Random reviews with fresh randomness before deck limits, including during
  RWKV queue refreshes, instead of favouring cards through a stable ID/time order.

- Keep undo responsive when an RWKV rollback frame is unavailable, and block
  review input until the restored card is displayed.

- Restore FSRS protection against Good/Easy intervals shrinking through review fuzz.
- Restore cached FSRS scheduling flags during reviews while keeping config changes
  and undo reflected in the active queue.

- Prevent card previews from freezing when MathJax is already loaded.
- Speed up macOS RWKV queue-scoring normalization and decay calculations.
- Reduce note-loading overhead during Dynamic DR preparation by reusing note-type
  field-map entries while preserving field edits and undo behavior.
- Reduce Python overhead when validating RWKV review history during cache recovery.
- Advance to the next card when burying an RWKV review card restored by Undo.
- Preserve pending IME text and wait for blur-triggered field saves when closing
  the editor during a review.
- Ensure in-app update checks select the normal installer when portable downloads
  are published alongside it.
- Include the release build number in every installer and portable archive filename.

## 26.09b1+fsrs7 — 2026-08-28

Current application version: `26.09b1+fsrs7`

This release aligns the fork with the
[official Anki 26.09b1 beta](https://github.com/ankitects/anki/releases/tag/26.09b1).
It merges that exact upstream tag, excluding later work from upstream `main`,
while retaining the fork's FSRS7/RWKV scheduling, performance, portable-build,
and reviewer-editing changes. The published release tag adds the fork's
monotonically increasing GitHub Actions build number.

### Added

- Check for and install updates published by the Anki FSRS7 fork during both
  automatic startup checks and manual **Check for Updates** checks.
- Add a portable macOS edition that keeps profiles, collections, media, add-ons,
  backups, preferences, logs, and temporary files beside the app, and can run at
  the same time as a normal Anki installation without sharing local state.

### Fixed

- Save complete IME text when editing the current review card instead of
  reloading the editor during text composition.
- Align RWKV calibration test rows and fold indices with FSRS validation folds,
  so UM+ compares both models on the same review samples instead of narrowing
  the comparison to RWKV's independent 30% holdout.
- Speed up RWKV state-cache validation after unrelated collection changes by
  fingerprinting retained review history in Rust instead of transferring and
  materializing the complete history in Python.
- Keep the resident RWKV state after append-only Grade Now operations, including
  excluded cram-only batches, and retain grouped rollback snapshots for their
  undo/redo, avoiding unnecessary full-history cache validation.
- Preserve resident RWKV history across current-deck selection, deck-tree
  collapse, deck rename/reparent, new note/deck/note-type creation, current
  note-type selection, RWKV rescheduling, bulk content edits, and safely
  reconciled scheduling or filtered-deck mutations—including their undo/redo—
  when canonical history routing remains unchanged.
- Keep creating or deleting unreviewed cards responsive while background RWKV
  prediction work is still running.
- Fall back to canonical RWKV recovery when a normal answer starts a replacement
  learning sequence or changes dynamic preset routing.
- Explicitly close RWKV state-cache database readers before publication, retry
  when Windows temporarily locks an existing cache file, and warn when rebuilt
  state is usable only for the current session because it could not be saved for
  the next launch.
- Keep fast consecutive answers, undo, and redo responsive while an RWKV queue
  refresh is still running, without discarding the resident model state.
- Keep review responsive with a cold RWKV state by keeping cache restoration off
  the card-rendering path and avoiding repeated full-history validation.
- Avoid rebuilding the full RWKV resident state after editor saves that leave
  the card's FSRS preset unchanged, including after undo.
- Avoid editor and incremental RWKV refresh stalls by resolving small FSRS
  preset overlay requests directly while retaining batch resolution for larger
  card sets.
- Restore undone cards on a rebuilt RWKV queue so the correct due counts and
  next-card order remain visible, without forcing a full refresh before the
  configured queue-update interval is due.
- Avoid an exit-time UI stall by updating undo actions before reviewer completion
  callbacks can start RWKV deck-browser prewarming.
- Avoid logging an RWKV deck-count error when its background preparation finishes
  after the collection has closed.
- Keep the reviewer open at an RWKV queue boundary when concurrent state work
  temporarily invalidates the first score refresh.

## [26.05+fsrs7.build.78](https://github.com/JSchoreels/anki/releases/tag/26.05%2Bfsrs7.build.78) — 2026-08-03

Changes since
[`26.05+fsrs7.build.73`](https://github.com/JSchoreels/anki/releases/tag/26.05%2Bfsrs7.build.73)
(2026-07-29):

### Fixed

- Use card creation time only to predict RWKV retrievability before a new card's
  first learning review, without carrying it into later predictions, and clarify
  the corresponding Deck Options setting.
- Remove a deleted card from the review screen immediately while its RWKV
  review queue is refreshed, preventing stale answer attempts and error dialogs.
- Refresh RWKV review ordering before moving from the final queued review to new
  or relearning cards, so newly eligible reviews retain their configured priority.
- Avoid redundant scheduling-state calculations and collection-wide RWKV
  snapshots when opening Card Info.
- Keep the resident RWKV history and cancel stale deck-count work when switching
  decks, avoiding repeated history restoration, duplicate overview refreshes,
  collection-lock UI stalls, and unnecessary cache validation on the next launch.
- Make RWKV Relative Overdueness consistently rank cards by retrievability
  relative to each card's current Dynamic Desired Retention target.
- Keep the resident RWKV state when editing a note leaves its resolved FSRS
  preset unchanged, avoiding a full history validation when returning to review.
- Remove the RWKV prediction batch-size and estimated-memory controls from Deck
  Options; Anki manages scoring batches internally.
- Speed up unchanged RWKV state-cache startup loads by reusing the previously
  validated collection history instead of rebuilding every historical input.
- Reduce RWKV state-cache rebuild and post-sync recovery peak memory by
  storing historical checkpoints as transactional deltas instead of repeated
  full snapshots, loading only the newest usable recovery checkpoint, releasing
  historical database rows as they are converted, and generating checkpoint
  metadata only when each checkpoint is written. Keep recurrent state owned by
  the embedded Rust runtime instead of retaining a second Python copy, reuse the
  final checkpoint as the effective cache state, and bound state-only replay to
  16,384-review chunks. Skip post-sync reconciliation when sync downloaded no
  collection changes, and preserve the reconciled state across the following UI
  reset. Keep one full recovery base eight days behind the current delta head;
  review-only sync changes older than that retain the prior state and display
  the number excluded until a manual rebuild includes them. Existing local
  caches rebuild once into the new compact format. Stream large checkpoint
  deltas across SQLite rows so large collections do not exceed the single-BLOB
  limit. Reuse checkpoint history metadata prepared during the original
  chronological pass, and use larger SQLite pages to reduce full-rebuild
  hashing and storage overhead. Stream recurrent states into those rows without
  allocating whole-state byte buffers, and reuse one SQLite connection across
  the base and final checkpoint. Process four review rows per projection with
  an exact NEON kernel on Apple Silicon.

## [26.05+fsrs7.build.73](https://github.com/JSchoreels/anki/releases/tag/26.05%2Bfsrs7.build.73) — 2026-07-29

Changes since
[`26.05+fsrs7.build.72`](https://github.com/JSchoreels/anki/releases/tag/26.05%2Bfsrs7.build.72)
(2026-07-22):

### Added

- Split retrievability searches by model: `prop:r` uses FSRS,
  `prop:rwkv:r` uses RWKV-Instant, and `prop:rwkv-curve:r` uses the current
  RWKV-Curve.
- Added `is:rwkv:due` and `is:rwkv-curve:due` filtered-deck searches for
  explicit RWKV-Instant eligibility and current RWKV-Curve due timing.
- Pre-score RWKV-dependent searches and retrievability ordering before
  rebuilding filtered decks.
- Added **RWKV → Reschedule All Decks** to deck cogwheel menus, allowing
  eligible review cards across every RWKV-enabled deck to be rescheduled in
  one operation.

### Improved

- Open RWKV retrievability searches when clicking Stats graph bars for
  RWKV-enabled cards; hold Shift while clicking to search FSRS retrievability.
- Use one consistent priority across RWKV score sources: Card Info, review
  queue, statistics, then background deck counts.
- Refresh resident RWKV state after sync. Reviews inserted into past history
  now restore an exponentially spaced checkpoint and replay only the affected
  suffix when possible.
- Improve RWKV performance and reliability across startup, review, sync,
  undo/redo, statistics, rescheduling, study queues, Card Info, and diagnostics.
  Anki now avoids repeated cache and history work and duplicate startup
  rebuilds, prevents temporary or concurrent calculations from publishing stale
  or partial scores after state changes, and evaluates head fine-tuning probes
  against the correct deck and preset history with lower peak work. Undo redraws
  keep the restored resident state, reuse the restored card's full curve
  prediction, and retain the queue score map as an incremental refresh base
  instead of repeating a full history restore, prediction, or deck rescore.

### Compatibility

- Existing desktop-local RWKV state caches rebuild once to add historical
  recovery checkpoints.

### Fixed

- Avoid back-to-back RWKV state-cache operations at profile startup by keeping
  deck-count preparation pending while automatic sync finishes, then restoring
  or rebuilding resident state once.
- Retain RWKV history for reviewed cards without a recorded Learning start,
  such as cards introduced through Grade Now.
- Keep every card in a filtered deck counted as due on the deck list instead of
  reapplying RWKV eligibility and showing only the daily minimum.
- Count outstanding filtered-deck reviews toward their original deck's RWKV
  daily minimum instead of pulling additional normal-queue cards.
- Validate RWKV state-cache prefixes from canonical review content and replay
  configuration, including the original deck of filtered cards.
- Bind resumable RWKV Memorised results to the same canonical history and
  replay-semantics identities, rejecting changed prefixes with unchanged IDs.
- Keep failed post-sync refreshes unready, propagate the real result to
  concurrent Stats waiters, and discard results from stale RWKV generations.
- Retry RWKV Card Retrievability data when concurrent Stats requests temporarily
  contend for prediction access.
- Keep Stats graph filtering bound to its exact score snapshot instead of
  allowing Card Info or review-queue scores to select different cards.
- Carry grade-order configuration through the Rust/Python boundary and recover
  missing last-review timestamps from eligible revlogs.
- Clamp future and out-of-range review timestamps instead of wrapping elapsed
  time, and preserve long-horizon/BF16 inputs in both RWKV runners.
- Put the optional legacy `srs-benchmark` runner in evaluation mode and bound
  its residual interpolation without truncating the forgetting-curve horizon.

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
