# RWKV History Omission Accuracy Benchmark

This offline experiment measures the accuracy cost of rebuilding RWKV memory
from incomplete review history, using the existing trained weights. It does not
change scheduling, train a model, or modify the live collection or state cache.

## Method

Export the production canonical collection-wide replay history from a copied
backup. Production filtering, latest-learning-sequence retention, elapsed-time
policy, and preset routing determine the baseline inputs. The earlier 80% of
reviews is the historical prefix; the last 20% is a common chronological
evaluation period. The split covers the whole collection. Score only the
requested decks, while replaying all decks so note, deck, preset, and global
states retain their normal context.

Compare a complete-history baseline with 25%, 50%, and 75% prefix omissions:

- **Whole cards:** shuffle prefix cards, then remove complete prefix histories
  until the number of omitted reviews is closest to the target. The actual
  percentage can differ slightly because a card's history is indivisible.
- **Random holes:** shuffle prefix review indices, then remove exactly the
  requested number of individual answers, rounded down to a whole review.

Each condition uses three fixed seeds by default. The same shuffled order is
used for all percentages within each mode and seed. Selection covers the full
collection, independently of the selected scoring decks.

Recompute elapsed seconds and scheduler days from each card's last retained
answer. If no answer survives, retain the baseline's first-review policy:
elapsed from its original creation-time anchor, or missing elapsed time. Keep
the original answer state and routed identities; do not synthesize Learning
starts or rerun dynamic preset routing after omission. Preassign the baseline's
random ID encodings to every condition using identities alone. This isolates
the missing review information from changes in random feature assignment.

Replay the surviving prefix without scoring, then predict every evaluation
answer before observing its outcome. Every evaluation answer updates the state
after prediction. Thus aggregate evaluation loss includes recovery through
subsequent reviews. Also report the first evaluation prediction per card,
including separate results for cards with and without surviving prefix history.
These first predictions can still use other cards' earlier evaluation answers;
they are not a frozen snapshot taken simultaneously at the split.

Binary natural-log loss treats Again as failure and Hard/Good/Easy as success.
Report all target reviews and the subset originally at least one scheduler day
after the previous answer. Subset membership always comes from the complete
baseline, so enlarging elapsed gaps cannot change which outcomes are scored.
Higher loss is worse. Differences are paired against the complete-history
prediction for the same answer. Each result includes a 95% paired card-cluster
bootstrap interval (400 resamples), preserving correlations between answers
from a card. These intervals describe outcome sampling, while spread across
seeds describes variation in omission choices. They do not quantify uncertainty
across users or model training datasets.

## Reproduction

First copy an existing `.colpkg` backup into a fresh directory under
`/private/tmp` or the workspace. The preparer accepts only an already-copied
backup in those locations and imports it into a new temporary collection through
Anki's supported API. Never point it at the live profile.

```sh
rtk proxy just rwkv-history-ablation-prepare \
  --backup-copy /private/tmp/rwkv-ablation/backup.colpkg \
  --output /private/tmp/rwkv-ablation/inputs.json \
  --deck-match Yomitan

rtk proxy just rwkv-history-ablation-bench \
  /private/tmp/rwkv-ablation/inputs.json \
  /private/tmp/rwkv-ablation/results.json 3
```

The input JSON contains private replay rows and should remain outside Git. The
result JSON contains aggregate metrics and is updated after each completed
condition. The Rust benchmark is an ignored test and runs only when explicitly
requested. Ordinary tests cover omission boundaries, preservation of evaluation
answers and whole-card histories, and elapsed-time reconstruction across holes.

This measures incomplete memory at a rebuild followed by complete future
reviews. It does not measure continuous omission of future answers, discarded
card-only states while retaining their shared states, retraining on less data,
or loss of old history with the most recent contiguous history preserved.

## Reset-history comparison

The reset benchmark answers a different question: whether predictions on reset
cards improve when their earlier review history remains available. Export two
collection-wide histories from the same copied backup:

- **All reviews:** every eligible answer, including answers before resets.
- **After reset only:** the same answers, except each card with a recorded
  Forget operation loses all answers through its latest reset. Cards without
  reset markers keep their complete history. Reset cards with no subsequent
  answer contribute no card history.

Both variants score exactly the same eligible answers after each card's latest
reset, including the first answer. The marker is an Anki Forget row
(`type = 4`, `ease = 0`, `factor = 0`); manual due-date changes and non-answer
rows are not scored. Normal answer eligibility excludes non-rescheduling cram
reviews. Only existing cards with retained review logs can be compared.

Use the production Python input adapter for scheduler-day timing and preset
routing. Membership is supplied explicitly, and later Learning answers remain
append-only so the usual latest-sequence filter cannot discard experimental
rows. A first retained answer logged as Learning is a Learning start; later
Learning answers are continuations. Other answers retain their actual review
kind. Recompute elapsed time between retained answers.
The default first-review elapsed policy comes from the copied collection's deck
settings. Pass `--first-review-elapsed missing` to repeat with the training
dataset's unknown-elapsed sentinel instead of time since card creation.

Preassign the same random ID encodings to both variants using identities alone.
Replay chronologically, predict before consuming each answer, and update all
card/note/deck/preset/global states afterward. Weights remain fixed. No later
answer trains the model or influences an earlier prediction. The reset boundaries
define the two retrospective replay datasets; this is an offline replay-policy
comparison, not an estimate of scheduling's effect on subsequent learning.

Run both f32 and int8 state precision. Report natural-log binary loss on all
post-reset answers, first answers, later answers, and interday answers. Interday
answers require a previous retained answer at least one scheduler day earlier;
elapsed since creation on a first query does not qualify. Membership comes from
the reset variant and is shared across policies. Also score cards without any
reset marker from the first scored post-reset answer
through the end of the collection in both variants. Their paired outcomes and
history membership are identical; differences reflect shared-state effects.
Paired 95% card-cluster bootstrap intervals use the method described above, with
10,000 resamples for reset cards and 400 for the larger non-reset sample. Private
per-review predictions remain in the result JSON outside Git.

Learning-start answer features always use unknown elapsed time inside the
runtime, matching training. The first-review elapsed setting changes the query
before that first answer; it does not change the learned answer state. First
answers are included here because they were explicitly requested, although the
upstream training pipeline does not train Instant predictions on new-card
queries. The later-answer subset therefore matters when interpreting the
unknown-elapsed sensitivity run.

```sh
rtk proxy just rwkv-reset-history-prepare \
  --backup-copy /private/tmp/rwkv-reset/backup.colpkg \
  --output /private/tmp/rwkv-reset/inputs.json

rtk proxy just rwkv-reset-history-bench \
  /private/tmp/rwkv-reset/inputs.json \
  /private/tmp/rwkv-reset/results.json
```

This comparison changes both card-specific and shared historical state. It does
not remove only a card's recurrent state while leaving its earlier reviews in
the shared scopes. With multiple resets, only answers after the latest reset
are scored; earlier episodes are not separate evaluation samples.
