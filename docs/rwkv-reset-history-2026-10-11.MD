# RWKV reset-history accuracy comparison — 2026-10-11

Keeping the earlier reviews clearly improves predictions on Tomato's reset
cards. K's reset cards have slightly lower average loss after discarding their
earlier reviews, but the paired uncertainty interval includes zero. Outside
reset cards, discarding that history changes Tomato's overall loss very little
and raises K's loss by about 0.66%. These two collections do not establish a
universal accuracy benefit from treating Forget as deletion of RWKV history.

## Main results

These results use the collections' existing first-review elapsed settings and
the current default int8 card/note state precision. Lower natural-log binary
loss is better. Positive change means discarding pre-reset history is worse.

| Collection / evaluated cards | Answers | Cards | Keep all reviews | Discard pre-reset reviews | Relative change |
| --- | ---: | ---: | ---: | ---: | ---: |
| Tomato / after latest reset | 2,684 | 436 | 0.447680 | 0.490602 | +9.59% |
| Tomato / never reset | 91,278 | 14,201 | 0.470059 | 0.470265 | +0.044% |
| K / after latest reset | 2,819 | 288 | 0.470591 | 0.460743 | −2.09% |
| K / never reset | 95,849 | 8,657 | 0.443675 | 0.446606 | +0.661% |

The paired 95% card-cluster bootstrap intervals for absolute loss change are:

| Collection / evaluated cards | Discard minus keep | 95% interval |
| --- | ---: | ---: |
| Tomato / after latest reset | +0.042922 | [+0.031411, +0.054923] |
| Tomato / never reset | +0.000206 | [−0.000123, +0.000521] |
| K / after latest reset | −0.009848 | [−0.020970, +0.000298] |
| K / never reset | +0.002931 | [+0.002561, +0.003249] |

The reset-card intervals use 10,000 resamples. The much larger non-reset samples
use 400. Cards are sampled with replacement, keeping a card's answers together
and comparing the two policies on identical outcomes. This estimates variation
over cards within each collection. It does not account for every dependence
between cards sharing recurrent state or measure variation across users.

## First answers and subsequent reviews

| Collection / reset-card subset | Answers | Keep | Discard | Relative change |
| --- | ---: | ---: | ---: | ---: |
| Tomato / first answer after reset | 436 | 0.618937 | 0.772040 | +24.74% |
| Tomato / later answers | 2,248 | 0.414465 | 0.436017 | +5.20% |
| Tomato / interday answers | 1,957 | 0.450832 | 0.469628 | +4.17% |
| K / first answer after reset | 288 | 0.669081 | 0.655322 | −2.06% |
| K / later answers | 2,531 | 0.448004 | 0.438602 | −2.10% |
| K / interday answers | 1,565 | 0.543383 | 0.539036 | −0.80% |

Tomato's advantage from retaining history persists after excluding the first
post-reset answer. K's later-answer improvement remains small, with its paired
95% interval also including zero. The effect is therefore not explained solely
by the first prediction of a newly reset card.

For cards without a reset, the interday-only results differ somewhat from the
all-answer averages:

| Collection | Interday answers | Keep | Discard | Relative change |
| --- | ---: | ---: | ---: | ---: |
| Tomato | 66,453 | 0.469866 | 0.468328 | −0.327% |
| K | 60,231 | 0.465575 | 0.464350 | −0.263% |

Discarding history slightly improves these non-reset interday samples in both
collections, while first and same-day answers offset that benefit in the
all-answer averages. Shared-state effects therefore depend on the review type.
An interday answer must have a previous retained answer at least one scheduler
day earlier; a first query's time since card creation does not qualify.

## Replay and evaluation

Source data was the supplied Tomato `.colpkg` and K's extracted collection
package. Both were copied into separate temporary directories; K's copied
package was wrapped in a `.colpkg`. Anki's supported import API created fresh
temporary collections. Analysis never opened a live Anki profile database.

Each policy replays the whole collection chronologically with fixed bundled
weights. The complete policy keeps every eligible answer. The reset policy
removes answers at or before each existing card's latest recorded Forget
operation (`type = 4`, `ease = 0`, `factor = 0`). Neither policy consumes
non-answer reset rows. Other cards retain identical histories. Predict before
observing each answer, then update card, note, deck, preset, and global state.

Preassign the same random identity encodings to both policies without consuming
answers. Production input conversion provides review kinds, scheduler-day
timing, and preset routing. Elapsed time is measured from the previous retained
answer. A first retained answer logged as Learning is a Learning start;
subsequent Learning answers remain append-only. Grade Again is failure; Hard, Good, and
Easy are success. Probabilities are clamped to [1e−7, 1−1e−7] for log loss.

All eligible answers after the latest reset are scored on reset cards. Earlier
reset episodes are not independent evaluation samples. The non-reset sample
starts at the first scored post-reset answer and ends at the last eligible
answer in the collection:

| Collection | Evaluation starts | Evaluation ends | Pre-reset answers omitted |
| --- | --- | --- | ---: |
| Tomato | 2025-12-30 18:52:15 | 2026-10-11 17:46:34 | 15,656 |
| K | 2025-10-28 15:20:26 | 2026-10-11 22:39:11 | 9,752 |

Times use Europe/Brussels. The common non-reset sample demonstrates effects
through shared history, because those cards lose no answers themselves.

Tomato contains 453 existing reset cards and 457 reset operations. Seventeen
cards have no answer after their latest reset and are not scored. All 436 scored
cards have eligible pre-reset answers. K contains 288 reset cards and 295 reset
operations; all have later answers, while 20 have no eligible earlier answer.
Those 20 can still receive different predictions through shared state.

## Precision and first-query sensitivity

Repeating the experiment with f32 state gives the same practical conclusions:

| Collection / evaluated cards | f32 keep | f32 discard | Relative change |
| --- | ---: | ---: | ---: |
| Tomato / after latest reset | 0.447677 | 0.490646 | +9.60% |
| Tomato / never reset | 0.470054 | 0.470259 | +0.044% |
| K / after latest reset | 0.470529 | 0.460734 | −2.08% |
| K / never reset | 0.443674 | 0.446607 | +0.661% |

Both collections use elapsed time since card creation for first queries in all
their presets, with dynamic preset replay disabled. A separate sensitivity run
replaces first-query elapsed time with the unknown sentinel (−1):

| Collection / reset-card subset | int8 keep | int8 discard |
| --- | ---: | ---: |
| Tomato / all post-reset answers | 0.447680 | 0.522931 |
| Tomato / first answer | 0.618937 | 0.971059 |
| Tomato / later answers | 0.414465 | 0.436017 |
| K / all post-reset answers | 0.514411 | 0.832788 |
| K / first answer | 1.098006 | 4.296975 |
| K / later answers | 0.448004 | 0.438602 |

The very poor cold first queries in the unknown-elapsed run should not be used
to decide the general reset policy. The upstream Instant training pipeline
scores recall on non-first reviews, and does not train the first new-card
query. The runtime already normalizes a Learning-start **answer** to unknown
elapsed time, independently of the first-query setting. Consequently this
sensitivity changes first-query losses but leaves subsequent answer states and
the reset-card later-answer results unchanged. K's complete-history first-answer
average also changes because 20 reset cards have no eligible pre-reset answers.

Training sources inspected for context: [RWKV training input processing](https://github.com/open-spaced-repetition/srs-benchmark/blob/main/rwkv/data_processing.py), [RWKV training losses](https://github.com/open-spaced-repetition/srs-benchmark/blob/main/rwkv/model/srs_model.py), and [review dataset builder](https://github.com/open-spaced-repetition/anki-revlogs-dataset-builder/blob/main/build_parquet.py). Matching a training retention rule alone does not establish better prediction accuracy on these collections.

## Reproduction and verification

Use the [reset comparison recipes and method](rwkv-history-ablation.MD#reset-history-comparison).
The [aggregate result JSON](rwkv-reset-history-2026-10-11.json) records full
precision metrics, bootstrap intervals, sample counts, source checksums, and
both first-query policies. Individual review predictions, card IDs, deck names,
and source collection copies remain outside Git.

- Base application commit: `c8ef99970f12604af50accfc42773924cb3f9385`.
- Bundled native weights SHA-256: `749ad1ad18d5b6ef8c4185802fa0c1e4a14e67cd7874f3efeaae355fffb1b9ec`.
- Both replay memberships were independently checked against read-only SQL on
  copied collections, including latest reset boundaries and omitted counts.
- The reset variant's production features exactly matched the canonical replay
  export for both collections under their existing settings.
- Scored review IDs, cards, and grades were identical between policies for both
  reset and non-reset samples. Saved probabilities independently reproduced
  reported log losses.
- Regression coverage verifies repeated resets, preservation of non-reset
  cards, a reset without later answers, Learning-start feature conversion, and
  exclusion of first queries from interday-only scoring.
- All 16 replay conditions completed, and the full `just check` passed.

This is a retrospective replay comparison on fixed observed outcomes. It does
not estimate how changing future scheduling would change learning or review
behavior. Persisting the user's intended preserve/discard choice through cache
rebuilds remains a separate correctness issue from choosing an accuracy policy.
