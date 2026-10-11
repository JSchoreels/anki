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
