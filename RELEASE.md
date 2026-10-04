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

## 26.09.3+fsrs7 — 2026-10-04

Changes since [build 94](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.94),
including the fixes prepared in draft build 95.

### Improved

- Restore and count large Browser selections faster, and reduce work while
  painting the table or filtering the sidebar.
- Speed up Empty Cards scans, deck counts and the finished study screen, while
  preserving same-day review limit exemptions. Reduce repeated reads during
  Check Database.
- Save FSRS preset changes faster when memory states need to be recomputed,
  while preserving FSRS-6/7 results, rescheduling order and undo.
- Reuse built-in scripts, styles and images within the webview session cache.
  Development builds still reload rebuilt assets.
- Reduce command-response delays in the Windows mpv audio transport.

### Fixed

- Respect “Allow same day review for (re)learning steps” in RWKV-Instant, so
  disabling it prevents cards from repeating that day even when learning queues
  are skipped.
- Prevent repeated same-day reviews from adding lapses or triggering leech
  handling across schedulers, with or without queue skipping, including
  rescheduling filtered decks and Grade Now. Again on a review card's first
  answer of a scheduler day still counts; later answers that day do not, even
  after an earlier successful answer.
- Show the final text after editing a card while its answer is displayed; the
  reviewer could keep an earlier, half-typed version (such as furigana with an
  empty reading) until the next card.
- Finish shutdown quietly when a development launcher's console output pipe
  closes, including on Windows, and avoid an unnecessary warning when stopping
  the local web server.
- Protect automatic backups from interrupted writes and filename collisions,
  allow retry after a failed backup, and prevent corrupt backups from displacing
  valid daily, weekly, or monthly backups or delaying the next backup.
- Show the due-review total before daily limits beside the green count in the
  reviewer, matching the deck list's `157 (/959)` display.
- Recover RWKV state after changing a deck or its preset during a session,
  including while reviewing. Open Card Info windows refresh when RWKV becomes
  ready and immediately reflect whether the card's current preset enables it.
- Restore FSRS-7 memory state after AnkiWeb sync removes its internal stability
  fields, including cards already affected. Recovery preserves existing due
  dates and intervals and does not queue repair-only changes for upload.
- Wait for concurrent RWKV calculations when creating or rebuilding filtered
  decks, instead of reporting that retrievability scores could not be prepared.
- Advance from an undo-restored RWKV card after redoing its answer.
- Notify add-ons once when the scheduler day changes, including while reviewing.
- Avoid errors when delayed geometry updates or notifications refer to a closed
  window.
- Display the correct average interval in notes mode when long card intervals
  would overflow the previous calculation.

### Benchmark results

The [Clanki improvement benchmarks](docs/clanki-improvements-benchmark.MD)
record the before/after results for each improvement, workloads, correctness
checks and measurement limits. The [raw results](docs/clanki-improvements-benchmark.json)
retain sample timings and quartiles for future release notes.

Browser measurements isolate the changed operations. Full Check Database
showed no clear overall speedup, and the Windows audio results use a simulated
transport. Keep these qualifications when using the results in a changelog.

## 26.09.3+fsrs7 — 2026-10-03

Changes since [build 94](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.94).

### Fixed

- Respect “Allow same day review for (re)learning steps” in RWKV-Instant, so
  disabling it prevents cards from repeating that day even when learning queues
  are skipped.
- Prevent repeated same-day reviews from adding lapses or triggering leech
  handling across schedulers, with or without queue skipping, including
  rescheduling filtered decks and Grade Now. Again on a review card's first
  answer of a scheduler day still counts; later answers that day do not, even
  after an earlier successful answer.
- Show the final text after editing a card while its answer is displayed; the
  reviewer could keep an earlier, half-typed version (such as furigana with an
  empty reading) until the next card.

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
