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
- For cumulative release notes, use the latest normal (non-prerelease) release as the baseline, include all intervening prerelease and draft changes, and verify coverage against the exact release commit.
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

Changes since the last normal release, [build 102](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.102). The application version remains `26.09.3+fsrs7`.

### User facing changes

- **Draft builds no longer offer older published updates.** Automatic checks and Tools → Check for Updates now compare the full fork build number, preventing a newer draft from prompting to install an older or equal published build with the same application version. Newer builds continue to be offered.

- **SM56 is the default small RWKV model, with all four state-storage modes.** SM56 powers both RWKV-Instant and RWKV-Curve with f32, INT8, INT4, or mixed INT4 cards + INT8 notes (the default). Saved storage precision is preserved; weights and computation remain f32. On the upstream matched 723-user comparison, Curve / Instant LogLoss is 0.148% / 0.061% lower than SM55, within 0.34% / 0.47% of the big model. These many-user figures use upstream INT8 weights and INT8 states, rather than evaluating each desktop precision. Turn on “Use Smaller Model” in Deck Options → RWKV → Advanced, save and reopen the profile to switch to SM56; turning it off keeps Legacy available.

- **Remove the one-minute RWKV-Curve fallback.** When predicted recall is already at or below the target at one second, Curve now returns a one-second interval and S90 instead of one minute. This also prevents an immediately due answer from appearing later than another sub-minute answer. Configured minimum intervals and learning/relearning steps still apply.

- **Simpler RWKV Deck Options.** A collapsed Advanced section now groups the “Use Smaller Model” switch, approximate queue updates, queue refresh frequency and comparison controls. The switch is on by default for SM56; turning it off selects Legacy. A compact legend directly below the switch explains both models and links to the Model Factory results and matched backup comparison, replacing the help button and separate model-name line. Instant descriptions use tighter spacing, the minimum-reviews row aligns with the other controls, and the sorting recommendation line is removed. Storage precision is hidden and preserved. Previously saved SM40, SM36 or SM55 choices become SM56 after a successful Deck Options save; discarding leaves them unchanged.

- **Learn ahead works with FSRS review sorting.** Relative Overdueness and ascending/descending retrievability now respect the Learn ahead limit for learning and relearning cards once no other cards remain to study. A limit of zero continues to require the full delay.

- **Automatic updates for portable builds.** Portable Anki now offers automatic notifications and Tools → Check for Updates. Accepting downloads and prepares the matching archive, closes Anki normally, replaces the application, and restarts it with the existing data folder in place. Failed installation or early bootstrap restores the previous application. Older portable builds need one manual upgrade to gain this flow.

- **Preserve literal comparison characters in searches.** Escape `<`, `>`, or `=` to search for text such as `x\<5` without triggering a numeric field comparison. Numeric comparisons, including equality, and the existing bracket range syntax continue to work; normalizing a search preserves its meaning.

- **Card Info recovers from a busy RWKV backend.** Temporarily unavailable RWKV values retry automatically with bounded delays, so reopening the window is no longer needed after background inference finishes.

- **Store RWKV cards at INT4 while keeping notes at INT8.** RWKV defaults to “int4 cards + int8 notes” storage for all five bundled models when no supported precision choice is saved. On the measured 224,022-answer collection, the SM40 cache shrank from 67.4 MB with INT8 to 53.1 MB with mixed storage (21% smaller), while rebuilding and saving took about the same time (2.96 s versus 2.92 s). Explicitly saved precision choices are preserved; the storage selector is now hidden.

- **Separate RWKV state rebuilding from calibration preparation.** Build Cache now rebuilds only RWKV review state, avoiding the second historical prediction replay (21.1 s in the observed run) and FSRS calibration rebuild. Use Prepare Calibration Data to refresh both models’ historical predictions and validation folds; it preserves the active RWKV review state.

- **Restore rebuilt RWKV caches correctly.** Desktop cache validation now accepts the schema written by both native models, fixing the “state-cache store identity mismatch” warning and avoiding needless rebuilds after reopening a profile. Rebuild logs also identify the active model and state precision during state and calibration replay.

- **Choose the RWKV model for each profile.** The model switch in Deck Options → RWKV → Advanced applies to all decks in the local profile and does not sync. Reopen the profile after saving a different model to rebuild RWKV state from review history.

- **Smaller, faster bundled RWKV model.** The experimental SM40 integration rebuilds the complete native state and final cache for the measured 224,022-answer collection in 3.64 s with f32 state versus 10.86 s for the legacy model with f32 state (11.92 s with legacy INT8); cache size falls from 1211.6 MB to 192.0 MB. Existing profile precision choices are preserved; SM40 INT8 uses a 67.4 MB cache and rebuilds in 3.91 s. Instant log loss is 0.060% higher than legacy f32 on matched interday reviews, with improved ordinary calibration; the legacy model still wins the paired UM+ comparisons.

- **Correct dynamic FSRS presets during Deck Options recalculation.** Bulk memory-state updates now use each card's first matching Dynamic Preset Selection rule and its model parameters, retention settings, and history cutoff. Previously, every card used the home-deck preset. Optional rescheduling uses the same matched model; recalculating without rescheduling still preserves intervals and due dates.

- **Choose before a filtered deck needs a full RWKV state rebuild.** Building or rebuilding a filtered deck that needs RWKV scores when the state is not ready (for example, with no cache after cancelling the startup prompt) now asks first: build the RWKV state with a progress window and then the deck, build the deck now in FSRS order when RWKV is used only for sorting, or cancel. Before, Anki silently replayed the review history inside "Processing..." for minutes and looked frozen. After you cancel the startup prompt or choose FSRS order, Anki also stops starting the full rebuild on its own for the rest of the session (after deck changes, leaving the reviewer or opening a deck) until you build the state.

- **Faster filtered-deck builds and new-card answers.** Fixed a long "Processing..." wait when building filtered decks sorted by retrievability or relative overdueness (especially with FSRS preset overlay rules), and a short stall after answering each new card on large collections. Filtered decks sorted by RWKV retrievability also build faster on collections with many suspended cards, with the same card order.

- **Fewer full RWKV state rebuilds after deck edits.** Adding or importing decks, adding presets, moving never-reviewed decks to another preset, or deleting never-reviewed decks no longer discards the RWKV state cache and triggers a full "Building RWKV state cache" replay. Moving a deck that has reviews to another preset still rebuilds.

- **Clickable add-on updates after RWKV startup.** Add-on update prompts wait until the RWKV question and state-cache progress window close. Repeated checks share one active update flow, and closing a profile removes its old update window.

- **Faster, quieter quit and profile switch with RWKV.** The sync on close no longer runs "Updating RWKV state after sync..." (the next profile open reconciles the state), which also avoids an error dialog when quitting from the deck list. Closing a profile cancels running RWKV Memorised-history and workload-simulation jobs. Tools → Add-ons → Check for Updates while an update window is already open brings that window forward instead of doing nothing, and the Anki update check still runs if a collection import replaced the collection during the startup add-on check.

- **Correct RWKV predictions after Forget.** Forgetting (resetting) a reviewed card, from the Browser or the reviewer, now rebuilds the RWKV state without that card's pre-reset reviews. Before, the old reviews stayed in the card, note, deck and preset state, survived restarts, and later caused an unexpected full rebuild. Grade Now on a forgotten card is fixed by the same change. Answering a forgotten card after Set Due Date no longer measures elapsed time from a review before the reset, so the live state matches a rebuild.

- **Suspend, Forget and Set Due Date work on a card restored by Undo.** After undoing an RWKV answer, suspending, forgetting, rescheduling, or moving the shown card (from the reviewer or the Browser) now advances to the next card. Before, the card stayed on screen; answering it silently un-suspended it or failed with "card was modified". A card being shown, or restored by Undo, also stays at the top of the queue when the queue refreshes itself for a newly eligible RWKV repeat.

- **The last learning cards are no longer withheld by RWKV-Instant.** When only a few learning or relearning cards remain and the "other reviews" minimum is all that holds them back, they are now shown once their seconds minimum has passed. Before, they could stay hidden behind the Congratulations screen indefinitely, even into the next day.

- **RWKV predictions stay the same after a restart.** Several cases made the live RWKV state drift from what a restart rebuilds from the review history. Preview answers in filtered decks without rescheduling no longer update the RWKV state, because a rebuild ignores them. A learning card answered in a rescheduling filtered deck without FSRS memory state is now logged as Learning, as in upstream Anki, instead of a Filtered entry that a rebuild, the statistics and the FSRS optimizer treated as cramming. Showing or querying a never-reviewed card (or undoing its first answer) no longer changes the ID features later new cards receive. With the Dynamic Preset Selection add-on's RWKV support, each answer now updates the preset the rebuild would choose for it, even when the card's current interval or repetition count routes it elsewhere, without triggering a state recovery.

- **RWKV cache fixes: stale calibration data, corrupt cache file, slower startups over time.** After an RWKV model or precision change, the calibration graph, the FSRS-vs-RWKV comparison and Search Stats Extended now recompute their RWKV predictions instead of showing the previous model's. A damaged retrievability cache file no longer stops the profile from opening: it is recreated (a cache held by another running Anki still reports "Anki already open"). Startup no longer slows down as reviews pile up since the last full state save: once 10,000 reviews are waiting to be replayed, they are written into the state store (about 3.5s per 50,000 reviews before). FSRS calibration rows are rebuilt correctly when two presets share a name, and adding a preset no longer discards the RWKV Memorised history cache.

- **Choose when due same-day learning appears without changing review sorting.** Deck Options → Display Order now offers Intraday learning/review order with Show before reviews, Mix with reviews, and Show after reviews. Before and After move due same-day learning/relearning as a group while retaining ascending/descending retrievability or Relative Overdueness within each group. Mix preserves the shared sorted order and is the default. The choice syncs with the deck preset.
- **Updated security dependencies.** This build includes patched HTML sanitization, asynchronous I/O, TLS and ZIP libraries, addressing newly reported CSS URL filtering, Unix file-descriptor handling, TLS handling, distrusted certificate authorities and archive extraction issues.

### Technical details

- **Tokio security update:** raise the workspace minimum and lock Tokio from 1.52.1 to 1.53.3, addressing [RUSTSEC-2026-0386](https://github.com/RustSec/advisory-db/blob/main/crates/tokio/RUSTSEC-2026-0386.md). The Linux release CI detected the unsafe file-descriptor constructor advisory during `cargo deny check`; retain the audit policy and all existing checks.

- **RWKV settings layout:** reuse the existing profile settings save contract and preset queue fields, with no new schema or precision changes. Ordinary save requests translate SM40/SM36/SM55 to SM56 without mutating pending state; parameter-only optimization is excluded, and the desktop still persists choices only after successful configuration saves. Runtime support for older models remains available.

- **SM56 integration:** import the verified [SM56 release](https://github.com/JSchoreels/anki-rwkv/releases/tag/small-model-sm56-2026-10-24), trained for ten base epochs on all 8,000 users followed by the 8,000-user Muon decay. Architecture and reference code are byte-identical to SM55; the native engines, features and state codecs are unchanged. Upstream matched-user scores use w8+s8, while desktop computation retains f32 weights with the saved state precision (mixed INT4 cards / INT8 notes by default). On the existing backup’s 176,943 interday reviews at the desktop precision, SM56 versus SM55 is +0.051% Curve / −0.013% Instant LogLoss; the upstream gain is not uniform on this collection. See the [SM56 integration report](docs/rwkv-sm56-2026-10-24.MD) for provenance, parity, cache checks and the five-model comparison.

- **SM56 state precision and verification:** f32 stores card/note matrices at full precision; INT8 and INT4 quantize both card and note matrices; mixed storage uses INT4 cards and INT8 notes. These policies apply in RAM and on disk, while weights, arithmetic and shared scopes stay f32. Quantized modes retain f16 shifts and f32 row scales. All four policies are covered by native restore tests and desktop checkpoint, answer/undo and workload round trips. Native f32/INT8 weight-container parity is a separate reference check; it is not an INT4-weight compute mode. The complete [CI matrix](https://github.com/JSchoreels/anki/actions/runs/40092590192) and [release workflow](https://github.com/JSchoreels/anki/actions/runs/40093773358) passed for build 107, commit `95396c9ca1ae570601e977e851f2ab82e5d2fc98`, producing all 12 installer/portable packages.

- **SM55 integration:** import the verified [SM55 release](https://github.com/JSchoreels/anki-rwkv/releases/tag/small-model-sm55-2026-10-22), trained with an 8,000-user Muon final decay. Its architecture is byte-identical to SM36 and SM40, so it shares the existing native specialization and state codecs. Preserve f32 compute weights and state precision. The upstream scores use w8+s8 on matched held-out users; the desktop uses f32 weights and mixed INT4-card/INT8-note state by default. On one backup's 176,943 matched interday reviews, SM55's Curve / Instant losses are 0.023% / 0.009% higher than SM36's, while both remain below SM40's. See the [integration report](docs/rwkv-sm55-2026-10-22.MD) for provenance, native/reference parity and the matched collection comparison.

- **SM36 integration:** import the verified [SM36 Muon release](https://github.com/JSchoreels/anki-rwkv/releases/tag/small-model-sm36-2026-10-22), reuse SM40's identical native architecture and preserve f32 compute weights with the existing state codecs. Model selection and weight-content identity invalidate prior checkpoints when switching models. Verify the released fp32/int8 vectors, all model/precision checkpoint and undo/workload roundtrips, and the real Qt model selector. The upstream many-user scores use w8+s8; they are not a new many-user evaluation of the desktop's f32-weight mixed-state mode. See the [integration report](docs/rwkv-sm36-2026-10-22.MD).

- **FSRS learn-ahead queue:** retain the shared ordering of due cards, then admit future intraday learning/relearning cards within the configured learn-ahead window after the main queue is exhausted. RWKV-Instant selection and repeat-spacing guards remain unchanged.

- **Portable update routing:** retain opt-in asset selection and verified downloads under the current portable data folder. Add fresh-directory ZIP/TAR-Zstandard extraction, an application-file manifest, and a staged-runtime helper that waits for normal shutdown, retains rollback files, and checks restart acknowledgement. Keep profile data and unrelated top-level files in place. Cover edition/CPU selection, archive validation, cancellation, native extraction, helper bootstrap, process handoff, rollback and Qt controls. See [portable update behavior and recovery](docs/portable-updates.MD).

- **Mixed RWKV state precision:** quantize card matrices at INT4 and note matrices at INT8, retain f16 shifts and f32 shared state, and reuse the existing state codec for both models. The timing comparison uses the same prepared history in three alternating fresh optimized processes per mode, measuring native replay plus the final durable cache save; it excludes history preparation, calibration and intermediate recovery checkpoints. These timings do not measure accuracy.

- **Numeric search compatibility:** recognize an unescaped comparator only with a nonempty field and a finite numeric right-hand side. Preserve escaped literal comparators through search normalization, numeric equality, case-insensitive field matching, and the fork's bracket range syntax.

- Route both Deck Options state-cache endpoints through state-only preparation. Move historical cache clearing, FSRS prediction/fold rebuilding and RWKV calibration replay into the dedicated preparation action. Keep the synchronous RWKV-only calibration APIs unchanged and cover separate request routing, failure handling, cache preservation and browser interaction.

- Align the desktop RWKV store reader with native schema 5. Add native-to-desktop checkpoint integration coverage for Legacy and SM40, including metadata, parent chains, orphan pruning and fresh-runtime prediction restoration.

- Port the 331,511-parameter SM40 model to the native scalar, batched and bulk runtimes, including its interleaved layers, power-mixture curve, one-layer note state and exported f32/int8 weight containers. Compile both specialized native architectures from shared engine source, dispatch per profile, tag cache and undo state by model, and verify both published reference users plus matched reconstruction, calibration and UM+ measurements. The Cargo feature only selects the primary offline benchmark architecture; desktop builds support both models. The [dual-model follow-up](docs/rwkv-dual-model-2026-10-20.MD) confirms bit-identical full-history predictions for all four model/precision combinations. See [the SM40 report](docs/rwkv-sm40-2026-10-20.MD) for definitions, limits and provenance.

- **Deck Options FSRS routing:** resolve affected non-new cards in a batch before writes, then group them by the resolved preset for the existing memory-state replay and rescheduler. Preserve home-deck interval/fuzz limits, unmatched-card retention overrides, disable-FSRS behavior, transaction rollback, and undo/redo. Regression coverage mixes FSRS-6 and FSRS-7 overlays with overlapping interval rules and a home-deck fallback.

- **RWKV replay identity:** the replay semantics key no longer hashes collection-wide deck-to-preset routing or per-preset first-review config, because per-review deck and preset ids are already covered by the chained history hash. The first launch after updating rebuilds once because the key changes.

- **RWKV state and calibration caches:** calibration availability now also requires the calibration epoch (store generation, segment, model, replay key) to match. A restore whose `deltas-v1.log` holds at least 10,000 validated reviews takes the checkpoint-recovery path, which writes the eight-day root and current delta, empties the log and carries a matching calibration epoch to the new segment. Promoting a newly built store removes the live file's `-journal`/`-wal`/`-shm` first, so a hot journal from a crash cannot be rolled back into it. Opening the collection recreates an unreadable retrievability sidecar (busy/locked still fails) and skips an unreadable legacy sidecar instead of failing. The FSRS retrievability rebuild selects decks by preset id, and the Memorised identity (version 4) drops the per-preset first-review field.

- **RWKV live/replay consistency:** `record_reviewer_answer` skips answers whose new revlog row is `type = 3 and factor = 0` (no state change, no undo frame, so undo/redo are no-ops). `set_answer_rwkv_metadata` writes raw kind 0 (live state LEARN_START/LEARNING) for rescheduling-filtered new/learning answers whose next learning state has no FSRS memory state. ID encodings are committed only by `store_review`; `features_for` previews unseen ids from a cloned generator, so replay output and the state-cache format are unchanged (no replay version bump). Live undo snapshots (`state_for_review`) restore the ID generator and the answer's four encodings. With dynamic preset replay, the live answer is checked against `simulator_rules` evaluated on the card's previous retained interval and retained count (searches restricted to the card), and that replay preset is used for the answer's state update, calibration prediction and undo/redo frames (live-vs-live routing check unchanged); recovery happens only when the replay inputs cannot be read.

- **Add-on update dialog lifecycle:** deliver update results through progress-safe timers, defer them while another modal dialog is active, and cancel results belonging to a closed profile or replaced collection. Close the chooser before starting downloads and complete cancelled or disabled update checks. Qt regression coverage exercises RWKV question/progress ordering, button clicks, repeated checks, and profile reloads.

- **Intraday queue ordering:** group admitted due intraday cards before or after the remaining queue after exact scoring, daily-limit admission, and sibling selection; Mix keeps the shared ordering. Future steps outside the configured learn-ahead window still wait. Store `intraday_learning_mix` in deck-config extra data under `jschoreels.deckoptions`, using the existing ReviewMix values and no new protobuf field. Earlier enabled toggles map to Before and migrate on the next Deck Options save. Regression coverage includes all three modes with FSRS 6/7, current-card preservation, limits, new-card mixing, opaque-client preset edits, sync round trips, and browser save/reload.
- **Undo-restored reviewer card:** the reviewer snapshots the restored card's scheduling fields (deck, original deck, queue, type, due, original due, interval, remaining steps) and stops ignoring study-queue refreshes once they change; flag-only edits keep the card. The backend re-pins the shown card when it rebuilds an existing queue for a ready deferred RWKV review or an intraday re-sort, and `Scheduler.counts()` fetches counts only, so it no longer marks a different card as shown.
- **Dependency security:** update Ammonia from 4.1.4 to 4.1.8, rustls from 0.23.45 to 0.23.46, rustls-webpki from 0.103.15 to 0.103.16, webpki-roots from 1.0.7 to 1.0.10, and ZIP from 4.6.1 to 9.0.5. These include fixes for [Ammonia CSS URL matching](https://rustsec.org/advisories/RUSTSEC-2026-0373.html), [rustls session debug output](https://rustsec.org/advisories/RUSTSEC-2026-0346.html), [ECH rejection](https://rustsec.org/advisories/RUSTSEC-2026-0347.html), [certificate decompression](https://rustsec.org/advisories/RUSTSEC-2026-0348.html), [key-exchange downgrade](https://rustsec.org/advisories/RUSTSEC-2026-0349.html), distrusted emSign certificates in [rustls-webpki](https://rustsec.org/advisories/RUSTSEC-2026-0382.html) and [webpki-roots](https://rustsec.org/advisories/RUSTSEC-2026-0381.html), and ZIP extraction [permission bits](https://rustsec.org/advisories/RUSTSEC-2026-0367.html), [symlink targets](https://rustsec.org/advisories/RUSTSEC-2026-0368.html) and [permission changes through symlinks](https://rustsec.org/advisories/RUSTSEC-2026-0369.html). Ammonia also includes the earlier [CSS URL escaping](https://rustsec.org/advisories/RUSTSEC-2021-0157.html) and [SVG animation filtering](https://rustsec.org/advisories/RUSTSEC-2021-0158.html) fixes. Anki's sanitization helpers retain the default tag and attribute policy.

## 26.09.3+fsrs7.build.102 — 2026-10-12

Changes since the last normal release, [build 100](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.100). The application version remains `26.09.3+fsrs7`.

### User facing changes

- **Working daily-limit help links.** New Cards/Day and Maximum Reviews/Day help now open the matching sections in the current Anki manual.

- **Reset cards no longer repeatedly recover RWKV state after Build Cache.** Forgetting cards, repositioning them, and rebuilding the cache now excludes their old learning history immediately. Their first new reviews no longer trigger another collection-wide replay, including after deleting the state cache.

- **Consistent RWKV history after resetting cards.** RWKV rebuilds now always exclude reviews before each card's latest reset, including after restarting Anki or recovering a cache. The old desktop preservation override and its misleading Forget notice are removed. The review log remains available in Anki.

- **Clearer scheduling help.** Machine learning scheduling and Review scheduler help now separate the RWKV and FSRS explanations with headings and group the references and manual link below the text. Short paragraphs no longer run together.

- **A lighter Scheduler panel.** Compact outlined model buttons replace the filled segments, and Apply Schedule to All Presets appears as an inline header action. Desired Retention keeps its Preset / This deck selector beside the input. Question-mark buttons open the descriptions and documentation links. Help and Apply actions stay flat without raised borders or shadows, including on macOS.

- **Rebuild historical predictions with Build Cache.** The Deck Options action now clears and rebuilds both RWKV and FSRS retrievability caches along with RWKV state. Cache database files live inside `rwkv-state-cache`, keeping the profile folder tidy.

- **Apply scheduling choices to every preset.** The Scheduler panel now has an Apply Schedule to All Presets button. Choose Scheduler, Desired Retention, Fallback, and Settings in a dialog, with all four checked by default, then Save to persist the changes. Deck-specific retention overrides stay intact.
- **Choose RWKV state precision in Preferences → Review.** int8 is now the default for card/note state in memory and on disk; f32 restores the original full-precision behavior, and experimental int4 uses still less memory with a larger accuracy tradeoff. On a 223,832-review backup, int8 reduced the native cache from 1,247 MB to 395 MB and fresh-load peak process memory from 1,326 MB to 528 MB, with effectively unchanged log loss. Rebuild plus save rose from 9.65 s to 10.97 s. Restart after changing the setting; the next RWKV preparation rebuilds from review history, including when returning to f32.
- **Interday learning priority works with FSRS sorting.** Show before reviews and Show after reviews now place due interday learning cards before or after the other due cards when sorting by ascending/descending retrievability or Relative Overdueness. Mix keeps the shared sorted order, and each group retains the selected sort.
- **No immediate RWKV rebuild after deleting a deck.** Deck deletion and its undo/redo keep the current session's model state, including when deleting an empty deck or a deck containing only new cards. Removed reviews are excluded at the next canonical restore, normally after restarting Anki.
- **Shorter pauses while statistics prepare FSRS calibration.** Historical FSRS calibration releases the collection during input conversion and training, and saves in short batches. On a 269,694-review backup, the longest concurrent collection-read wait fell from 1,048 ms to 243 ms, while final-fit generation fell from 1.84 s to 1.54 s. Cancel now works during preparation, prediction and saving.
- **RWKV uses much less memory.** On a 224,000-review collection, building the RWKV state peaks 29% lower (2.01 → 1.43 GB) and recomputing RWKV calibration data peaks 44% lower (2.56 → 1.44 GB). On Linux, Anki also returns about 2 GB of freed memory to the system after these jobs instead of keeping it until restart. This mainly helps computers with 8 GB of RAM, where these steps could push the system into swap and freeze it.
- **RWKV Memorised statistics pause during heavier RWKV work.** While RWKV calibration data is recomputed or the RWKV state is rebuilt, the Memorised history build pauses and then continues from where it stopped, instead of running both at once.

### Technical details

- **RWKV history omission experiments:** added offline benchmarks that omit whole-card histories or individual answers at 25%, 50%, and 75% of the historical prefix, with fixed seeds, common evaluation answers, and paired card-cluster intervals. The [method](docs/rwkv-history-ablation.MD), [223,832-review report](docs/rwkv-history-ablation-2026-10-10.MD), and [Primarius report](docs/rwkv-history-ablation-primarius-2026-10-10.MD) document the accuracy impact. A separate [sibling-note memory report](docs/rwkv-sibling-note-memory-2026-10-10.MD) records an isolated experiment; its test-only memory policy is not part of the release.

- **RWKV documentation:** clarified how load balancing and review fuzz apply to Curve's day-based answers, Instant's fallback due-date calculator, intraday intervals, and bulk rescheduling. The [RWKV FAQ](RWKV_FAQ.md) describes existing behavior.

- **RWKV reset-history accuracy benchmark:** add copied-collection replay recipes that compare retaining all eligible answers with discarding answers through each card's latest Forget operation. Score identical post-reset answers and non-reset cards, with fixed identity encodings, f32/int8 state, and paired card-cluster intervals. The [two-collection report](docs/rwkv-reset-history-2026-10-11.MD) shows mixed reset-card results and measurable shared-state effects; this tooling does not change scheduling or reset policy.

- **RWKV reset boundaries:** native and Python history loading always honor the latest non-answer Forget revlog entry, including requests carrying old preservation cutoffs. The fallback history query preserves Anki's undo stack. Replay semantics version 5 forces one rebuild of existing caches; the state-store schema remains version 12. Regression coverage exercises stale markers, reset, undo/redo, reposition, normal/forced rebuild, first answers, cache upgrades, and restart with both history loaders and with the cache retained or deleted, plus native fingerprint and manual-review boundary checks.

- **FSRS calibration:** use the per-review prediction API merged in [fsrs-rs #458](https://github.com/open-spaced-repetition/fsrs-rs/pull/458) for final-fit and validation-fold cache rows, removing Anki's duplicate history walk and prediction-source data. Prediction runs also honor cancellation between cards.

- **Historical cache maintenance:** checkpoint and relocate the adjacent retrievability SQLite database on collection open when the new location is absent. Manual cache builds clear both prediction tables, rebuild FSRS final-fit and validation-fold rows with current deck-preset settings, and replay RWKV calibration against those folds. Automatic reviewer recovery retains its state-only behavior; review history and scheduling are unchanged.

- **Bulk schedule editing:** reuse the existing Deck Options save request and modified-preset tracking. Copy only selected model settings and known FSRS auxiliary options, preserving unrelated preset and add-on data. Instant's fallback can be copied independently of its review model.
- **RWKV compact state:** use row-scaled symmetric int8/int4 matrices and f16 shift vectors for card/note modules, with f32 inference and full-precision shared scopes. Quantization happens after every answer; normal queries, batched queries, preview answers, undo, simulation, checkpoint continuation and stash spills read the packed state. The per-runtime mode enters replay/cache identity. Existing low-level three-argument inference constructors remain f32 for benchmark and API compatibility. Checkpoint schema v12 remains unchanged; module encoding v2 carries the compact precision and is validated on load. The [native benchmark](docs/rwkv-state-precision-2026-10-10.MD) reports int8 RMSE(bins) +0.0094%, versus experimental int4 log loss +0.0676% and RMSE(bins) +5.70%; fresh-process load times include validation, and process memory excludes the Anki GUI.
- **FSRS queue presentation:** apply the existing interday learning/review setting after exact scoring, daily-limit admission, and sibling selection. Only interday learning cards move between priority groups; intraday eligibility, ordering within groups, and New/review order are preserved. Queue counts and preserved current cards include the interday group.
- **RWKV deck deletion:** reuse collection mutation reconciliation with the affected deck tree's cards, including cards borrowed by filtered decks. Removed historical cards make the resident cache identity unknown, preventing that state from being persisted as canonical; deleting a filtered deck preserves state when its cards keep their historical identities.
- **FSRS calibration locking and memory:** `ComputeFsrsReviewRetrievabilityCalibration` reads a revlog snapshot under the collection mutex, converts inputs and predicts outside it, prepares one validation fold at a time, then saves sorted rows in batches of up to 10,000 with cancellation and collection-session checks per batch. Interrupted jobs can leave partial derived rows; SSE records incomplete jobs and retries them. In a probe cancelled at the first training iteration, peak RSS fell from 463 MiB to 247 MiB; this is a preparation measurement, not full five-fold training. The completed final-fit benchmark stored 247,394 identical predictions. See [methodology and results](docs/sse-calibration-performance.MD).
- **RWKV calibration history reuse:** the ensure path passes its prepared history into the calibration replay instead of loading and retaining a second complete history. Generation, replay settings and native history fingerprints are checked before reuse; temporary reviewer state restoration is preserved.
- **RWKV state store page cache:** the bundled SQLite shares a page-count budget across connections, so the 64 KiB-page state store could use the collection connection's budget during full checkpoints (about 480–650 MB). The store now reapplies its cache budget after setting the page size and releases unused pages around chunk allocation and after each commit. The on-disk format is unchanged.
- **Calibration stash spill:** calibration recompute streams the stashed card and note states to `rwkv-state-cache/stash-spill.bin` (count- and BLAKE3-checked) and reads them back after the replay, so only one full state is resident. The restored state is bit-identical. If spilling fails, the in-memory stash is used. If restoring fails, the stash is discarded and the state recovers from the persistent store plus newer revlogs. Historical warm-up removes spill files left by a crash.
- **Freed memory:** after full warm-up, calibration recompute, Memorised history and background state drops, Anki calls `malloc_trim(0)` on glibc Linux and `malloc_zone_pressure_relief` on macOS. In an 8 GB Linux container on a 63,000-card collection, this released 2.0–2.6 GB of retained RSS per job.
- **Memorised history preemption:** calibration replays and full warm-up rebuilds mark a heavy replay. The Memorised job checkpoints at the next day boundary, drops its private runtime, waits, and resumes through the existing checkpoint restore, with identical final values.
- Measurements are malloc bytes in use on macOS, release builds, on a 224,000-review collection (build 100 vs this change).

- **Verification:** `just check` and all eight targeted reset/rebuild/relearning cases passed locally. The [complete CI run](https://github.com/JSchoreels/anki/actions/runs/38243095076) passed on the exact release commit, [fb7876ea9](https://github.com/JSchoreels/anki/commit/fb7876ea9ae8e9cb2350e244c3dd6baf24d63457), including Linux browser tests. The [build 102 release workflow](https://github.com/JSchoreels/anki/actions/runs/38250828803) completed successfully across all release platforms.
- **Packaging:** build 102 provides 12 unsigned installer and portable downloads for macOS, Windows, and Linux on ARM64 and x64. Application versions and wheel versions keep the base `26.09.3+fsrs7` version; release tags and download filenames carry `.build.102`.

[Full commit comparison since build 100](https://github.com/JSchoreels/anki/compare/29120cdd1ac762fe3e8a371445390952879d54ec...fb7876ea9ae8e9cb2350e244c3dd6baf24d63457)

## [26.09.3+fsrs7.build.100](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.100) — 2026-10-08

Changes since the last normal release, [build 96](https://github.com/JSchoreels/anki/releases/tag/26.09.3%2Bfsrs7.build.96), including the changes from builds 97–99.

### User facing changes

- **Choose the scheduler in one place.** Segmented buttons select FSRS-6, FSRS-7, RWKV-Curve, or RWKV-Instant for each preset. Instant adds a Fallback : Due-Date Calculator for mobile sync and statistics. The visible Machine Learning Based scheduling switch still applies to the whole collection.
- **Clearer scheduler settings.** FSRS and RWKV controls appear in matching cards below Scheduler, with the review scheduler first and its fallback next. Shared desired retention stays above them, and only the relevant model controls appear. Blue stars recommend RWKV-Instant for reviews and FSRS-7 for fallback due dates; short model descriptions explain the choices and link to further reading.
- **Clearer FSRS workload estimates.** One line shows the estimated workload relative to the initial desired retention, with a note that it covers FSRS interval-based workload only. Instant's FSRS fallback does not show this estimate.
- **Compact, consistent RWKV repeat spacing.** Set the minimum other reviews and elapsed seconds in one row, with units inside the fields. Instant follows the shared same-day review setting. Learning and relearning cards also respect both minimums, including already-due steps and cards offered through Learn ahead, while their saved intervals and learning steps are preserved.
- **Simpler RWKV options.** The interval-order option spells out Again ≤ Hard ≤ Good ≤ Easy, and review-order guidance recommends Ascending Retrievability or Random. Creation-time predictions for new cards remain enabled without a toggle. The exit-refresh switch is hidden while preserving its saved behavior. Dynamic Preset support is controlled by the add-on's global RWKV support option.
- **No pause after editing a card during RWKV reviews.** Editing fields no longer causes a roughly two-second freeze on the next answer, and the edited card keeps its RWKV intervals on the answer buttons.
- **No periodic backup freeze while reviewing.** The five-minute backup check no longer extracts and integrity-checks the latest backup every time, avoiding the repeated “Creating backup…” pause. Backups are still fully verified when written and before older backups are removed.
- **Consistent same-day short-term scheduling.** “Allow same day review for (re)learning steps” also controls FSRS-7 and RWKV-Curve generated intervals. Turning it off makes those intervals at least one day; configured learning/relearning steps still apply. The option defaults to on, while explicitly saved off choices remain off.
- **Restored user interface sizing.** The User interface size preference takes effect again after restarting Anki.
- **Faster Browser selections.** Selecting or inverting thousands of rows and resizing columns with a large selection are more responsive (Select All: ~434 ms → ~0.9 ms in a 50,000-row synthetic test).
- **Lower memory use during full collection downloads**, especially for large collections.
- **Faster media scans** when files added locally or downloaded from AnkiWeb have not changed.
- **Faster RWKV calibration refreshes after FSRS parameter changes** when RWKV already has predictions for the complete review history (~21 s → ~6 s on a 224,000-review collection).
- **Fewer interruptions when adding cards through add-ons.** AnkiConnect and Yomitan mining no longer cause an RWKV recovery progress window and tooltip after every card, including while Anki is in the background.
- **More reliable RWKV calibration graphs.** Recomputing calibration removes outdated cached predictions that could otherwise appear in the graphs.
- **Correct Deck Options help links** for daily limits and leeches.

https://github.com/user-attachments/assets/06f02fb4-e474-458c-b22f-b998d5b47893

### Technical details

- **Scheduler compatibility:** the selectors reuse the existing FSRS version and RWKV configuration fields, retain each model's parameters, and enable collection-wide machine learning scheduling when a model is selected. FSRS-4.5 and FSRS-5 are no longer offered, but older saved choices remain effective until explicitly changed. Keyed card ordering follows the selected models while retaining editing state and the initial retention baseline. Per-preset SM2/FSRS coexistence is unchanged.
- **RWKV learning selection:** learning and review paths share the repeat-spacing calculation. Learning eligibility reads current answer times and recent answers in the selected deck tree, filters reviewer counts consistently, and preserves undo/redo queue snapshots. Learning eligibility uses its configured or generated interval without adding Instant's recall threshold.
- **RWKV configuration:** creation-time prediction ignores the retired deck-option flag in desktop and native query paths. The synced `rwkvDynamicPresetReplay` collection boolean overrides legacy deck-preset replay flags; until explicitly saved in the add-on, the old choice remains effective. Existing cache identities detect changes in effective replay behavior.
- **RWKV state after editor saves:** reconciliation markers are queued per save, and edited cards' FSRS preset-cache entries are refreshed in place. Overlapping saves no longer lose a marker or expose a transient cache miss that discarded warm resident state.
- **Backup verification:** periodic recent-backup checks read the archive structure and SQLite header. Full validation remains mandatory when writing backups and before thinning permits a backup to displace older ones.
- **Same-day scheduling compatibility:** generated intervals below 12 hours use exact intraday timing only when same-day reviews and learning queues are enabled. Configured learning/relearning steps retain precedence; queue skipping bypasses those delays without deleting the saved step lists.
- **Filtered-deck diagnostics:** failed RWKV preparation logs the deck, both search filters, and the scoring failure reason to make rebuild failures easier to diagnose.
- **UI scaling:** backported [Anki #5686](https://github.com/ankitects/anki/pull/5686), fixing [#5676](https://github.com/ankitects/anki/issues/5676). The saved scale factor is applied before Qt creates the application, with a startup regression test.
- **Browser:** backported Anki [#5768](https://github.com/ankitects/anki/pull/5768) and [#5771](https://github.com/ankitects/anki/pull/5771). The column header uses an empty selection model to avoid scanning selected rows when painting. Menu actions count changed selection ranges, while preserving the fork's fallback for add-ons overriding model flags.
- **Browser measurements:** on 50,000 synthetic rows with offscreen Qt on Apple Silicon, median Select All time fell from 434 ms to 0.91 ms, Invert Selection from 416 ms to 0.85 ms, and header painting from 90 ms to 0.59 ms. These measure individual operations, with existing Clanki improvements present before and after. See the [backport audit](https://github.com/JSchoreels/anki/blob/cd6ed9a13558d2b89d52460d6970d228b09754bd/docs/upstream-performance-backports.MD) for methodology and regression coverage.
- **Full sync:** backported [Anki #5717](https://github.com/ankitects/anki/pull/5717). Downloads stream through a buffered temporary file, are flushed and checked for integrity, then atomically replace the local collection.
- **Media sync:** adapted [Anki #5654](https://github.com/ankitects/anki/pull/5654), open at the 2026-10-07 review. Scans, local additions, and downloads use millisecond modification timestamps. Older timestamps require one checksum scan before using the fast path; the database schema is unchanged.
- **RWKV calibration:** complete cached predictions allow FSRS fold assignments to be refreshed without replaying review history (about 6 s instead of 21 s on a 224,000-review collection). After a full recompute, each answer stores its RWKV prediction. Rebuilt or recovered state, unseen synced reviews, or a different model still require a full recompute. Recomputes also remove superseded fold assignments and leftover training rows.
- **RWKV state:** add-on and legacy resets check whether the resident state still matches review history and retain it when unchanged, avoiding a later disk reload and recovery notification.
- **Verification:** `just check` and the full local browser suite passed (47 tests passed, one existing test skipped), including FSRS optimization, live card ordering, retained editing state, and suppression of workload calculations when FSRS is Instant's fallback. Additional renders checked dark and light themes, recommendation placement, and the single-line workload estimate.
- **Packaging:** build 100 provides 12 unsigned installer and portable downloads for macOS, Windows, and Linux on ARM64 and x64. The application version remains `26.09.3+fsrs7`.

[Full commit comparison](https://github.com/JSchoreels/anki/compare/26.09.3%2Bfsrs7.build.96...26.09.3%2Bfsrs7.build.100)

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
