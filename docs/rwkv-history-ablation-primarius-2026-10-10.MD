# RWKV History Omission — Primarius, 2026-10-10

On the Primarius collection, omitting 75% of historical prefix reviews increased ongoing-review log loss by **2.44% when omitting whole-card histories** and **0.20% with random holes**. These are relative changes in binary log loss, not changes in recall accuracy percentage points.

## Ongoing-review results

Complete-history baseline: **0.327009**. Lower is better. Every condition scores the same **357,634 reviews across 55,298 cards**, with original elapsed time of at least one scheduler day. One omission sample per condition, seed `20261010`.

| Prefix reviews omitted | Whole-card log loss | Increase | Random-hole log loss | Increase |
| --- | ---: | ---: | ---: | ---: |
| 25% | 0.330159 | +0.96% | 0.327173 | +0.05% |
| 50% | 0.332417 | +1.65% | 0.327344 | +0.10% |
| 75% | 0.334994 | +2.44% | 0.327660 | +0.20% |

Partial history was less harmful than discarding entire card histories in every tested condition. The tiny change at 25% random omission has a confidence interval that includes zero. These results describe the sampled omissions; they are not adversarial worst-case bounds.

## Data and replay

Used a temporary copy of `/Users/jschoreels/Downloads/PRIMARIUS COLLECTION.apkg`. The archive was imported into a fresh temporary collection through Anki's supported collection-package API; the original package and live collection were not modified. The copied archive was named `primarius.colpkg` to meet the existing preparer's suffix check; its bytes were unchanged.

The package contains 3,785,024 raw review-log rows. Production canonical replay filtering yields **3,513,712 inputs across 78,176 cards**. Replay and scoring cover all **758 decks**. The chronological earlier 80% is the historical prefix (**2,810,969 reviews**); the common last 20% is the evaluation tail (**702,743 reviews across 55,824 cards**, 2026-04-12 through 2026-10-10).

The existing pretrained weights and F32 memory precision remain fixed. Whole-card omission removes complete prefix histories until the omitted review count is closest to the target; actual rates are 25.000738%, 49.996282%, and 74.999973%. Random holes remove individual prefix answers at the requested percentages, rounded down to a whole answer. Elapsed times are recomputed across missing answers, and random identity encodings are fixed across conditions. Each evaluation answer is predicted before updating memory with that answer. All evaluation answers are retained, so the aggregate includes recovery through subsequent reviews. This benchmark does not test continuous omission of future answers or retraining with less data. See the [method](rwkv-history-ablation.MD).

## Uncertainty

Paired card-cluster bootstrap intervals use 400 resamples and keep all answers from a sampled card together. They describe sampling of evaluation cards, not variation across omission samples or users.

| Method | Prefix omitted | Log-loss difference | 95% interval for difference |
| --- | ---: | ---: | ---: |
| Whole cards | 25% | +0.003150 | +0.002877 to +0.003421 |
| Whole cards | 50% | +0.005408 | +0.005084 to +0.005773 |
| Whole cards | 75% | +0.007984 | +0.007543 to +0.008390 |
| Random holes | 25% | +0.000163 | -0.000004 to +0.000307 |
| Random holes | 50% | +0.000335 | +0.000084 to +0.000550 |
| Random holes | 75% | +0.000651 | +0.000361 to +0.000963 |

## All evaluation answers

Including within-day answers, the complete-history baseline is **0.373703** across 702,743 answers. This is a secondary metric; the ongoing-review subset above is the requested focus.

| Prefix reviews omitted | Whole-card log loss | Change | Random-hole log loss | Change |
| --- | ---: | ---: | ---: | ---: |
| 25% | 0.375453 | +0.468% | 0.373688 | -0.004% |
| 50% | 0.376263 | +0.685% | 0.373466 | -0.063% |
| 75% | 0.376910 | +0.858% | 0.373155 | -0.147% |

## Reproduction and verification

```sh
rtk proxy just rwkv-history-ablation-prepare \
  --backup-copy /private/tmp/rwkv-primarius-ablation-2026-10-10/primarius.colpkg \
  --output /private/tmp/rwkv-primarius-ablation-2026-10-10/inputs.json \
  --deck-match=

rtk proxy just rwkv-history-ablation-bench \
  /private/tmp/rwkv-primarius-ablation-2026-10-10/inputs.json \
  /private/tmp/rwkv-primarius-ablation-2026-10-10/results-one-seed.json 1
```

The ignored release-mode benchmark passed: baseline plus six conditions, about 13.5 minutes of test execution. The restarted baseline exactly reproduced all metrics from the earlier run. Verified common scoring counts, paired baseline values, finite losses, omission-rate rounding, and unchanged package/model hashes. No source changes were needed for this collection run. An initial three-sample run was stopped when the user selected one sample per condition; its additional partial sample is excluded here.

Repository formatting checks passed. The final `just check` failed only at `check:minilints` because existing files under `tmp/fsrs-pr458-benchmark-2026-10-09/` lack standard copyright headers; the other build/check tasks completed. Those unrelated files were left unchanged. Automatic approval review rejected repository-wide formatting because it could rewrite concurrent changes; formatting was corrected only in the benchmark JSON artifacts.

Package SHA-256: `1dd3defdfa4c852b2b6df0339e5903c83d52565c5345041bbbb7209340078ea1`. Model SHA-256: `749ad1ad18d5b6ef8c4185802fa0c1e4a14e67cd7874f3efeaae355fffb1b9ec`. The [aggregate result JSON](rwkv-history-ablation-primarius-2026-10-10.json) includes exact metrics, seeds, omission counts, and source/executable hashes recorded for the run. Private per-review inputs remain under `/private/tmp/rwkv-primarius-ablation-2026-10-10/` and are not stored in the repository. Deck names are omitted from the aggregate artifact.
