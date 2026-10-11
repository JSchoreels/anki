# RWKV Missing-History Accuracy — 2026-10-10

Random holes were substantially less harmful than discarding whole-card histories on this backup. Omitting 75% of historical prefix reviews increased subsequent aggregate log loss by **3.17% with random holes** and **10.59% with whole-card omission**. The first prediction per card worsened more: **7.52%** and **27.28%**, respectively. These are relative increases in binary log loss, not percentage-point changes in recall accuracy.

## Data and experiment

Used a copy of `backup-2026-10-10-14.56.35.colpkg`, with 223,832 canonical reviews across 13,965 cards. The prefix contains 179,065 reviews; the common evaluation tail contains 44,767 reviews from 2026-03-25 through 2026-10-09. Replay includes the entire collection. Scoring covers `Japan::0. Kanji::Yomitan Kanji` and `Japan::1. Vocabulary::1.2 Normal Scheduling::Yomitan`: **33,630 reviews across 9,139 cards**, including 32,483 reviews separated by at least one scheduler day.

The pretrained model weights remain fixed. Remove 25%, 50%, or 75% of prefix reviews using complete card histories or randomly selected answers, with three reproducible seeds per condition. Recalculate elapsed time across missing answers and fix random ID encodings across variants. Every tail review is predicted before its answer updates the state. See the [full method and reproduction commands](rwkv-history-ablation.MD).

## Aggregate evaluation loss

Complete-history baseline: **0.572256**. Lower is better. Condition values are means across three omission samples. The range shows the smallest and largest relative increase across those three samples, not a confidence interval.

| Omission method | Prefix reviews omitted | Mean log loss | Increase | Range across seeds |
| --- | ---: | ---: | ---: | ---: |
| Whole cards | 25% | 0.587659 | +2.69% | +2.54–2.79% |
| Whole cards | 50% | 0.606519 | +5.99% | +5.79–6.28% |
| Whole cards | 75% | 0.632883 | +10.59% | +10.07–11.63% |
| Random holes | 25% | 0.576851 | +0.80% | +0.77–0.82% |
| Random holes | 50% | 0.582135 | +1.73% | +1.70–1.75% |
| Random holes | 75% | 0.590401 | +3.17% | +3.06–3.27% |

The results for reviews at least one scheduler day apart are nearly identical: whole-card omission increases loss by 2.73%, 6.06%, and 10.75%; random holes increase it by 0.81%, 1.74%, and 3.19%. Membership in this subset uses original complete-history elapsed time, so every variant scores the same outcomes.

## First evaluation prediction per card

The complete-history baseline for these 9,139 predictions is **0.567958**. This view reduces the dilution from a card recovering after subsequent answers. It is still chronological: shared state can benefit from other cards’ earlier tail reviews.

| Omission method | Prefix reviews omitted | Mean log loss | Increase | Worst observed seed |
| --- | ---: | ---: | ---: | ---: |
| Whole cards | 25% | 0.602024 | +6.00% | +6.53% |
| Whole cards | 50% | 0.647874 | +14.07% | +14.95% |
| Whole cards | 75% | 0.722903 | +27.28% | +31.10% |
| Random holes | 25% | 0.579171 | +1.97% | +2.12% |
| Random holes | 50% | 0.590724 | +4.01% | +4.25% |
| Random holes | 75% | 0.610641 | +7.52% | +8.00% |

## Interpretation

Keeping at least part of many cards’ own histories is more useful here than keeping complete histories for fewer cards. Random holes do harm accuracy, but the harm is smaller at every tested omission rate. This comparison preserves the actual longer elapsed gap between retained answers; feeding the original short elapsed intervals across holes would misrepresent the retained sequence.

With whole-card omission, the first-prediction loss for cards with surviving prefix history changes by only about +0.06%, +0.49%, and +1.66% at the three rates. Cards without surviving prefix history worsen much more. These subgroup figures use paired baselines for each selected group; group membership differs across conditions and includes genuinely new cards as well as omitted cards.

The largest observed aggregate increase was **11.63%** for whole-card omission at 75%; the largest first-per-card increase was **31.10%** in that same condition. These are the worst of three random samples, not worst-case bounds. Per-seed paired card-cluster bootstrap intervals are available in the [aggregate result JSON](rwkv-history-ablation-2026-10-10.json); every condition’s aggregate interval was above zero.

This experiment removes history at a rebuild, then includes every future review. It does not test continuously skipping future answers. It also omits affected reviews from shared note/deck/preset/global states, keeps baseline historical preset routing fixed, and measures one collection with one pretrained model. No production replay policy was changed.

## Reproducibility and verification

The result JSON records exact seeds, omission counts, baseline comparisons, confidence intervals, model and backup SHA-256 hashes, and the repository base commit. Whole-card omission achieved 24.99–25.00%, 49.99–50.00%, and 75.00–75.02% of prefix reviews; random holes achieved the requested percentages to rounding by one review. Private per-review inputs remain in `/private/tmp/rwkv-history-ablation-2026-10-10/`.

The 19-condition release-mode benchmark passed. A fresh canonical export reproduced every input row, history identity, and replay policy. Rust tests, Python tests, lint, and formatting passed through the repository recipes. The final full `just check` is blocked by existing missing copyright headers under `tmp/fsrs-pr458-benchmark-2026-10-09/`; those unrelated files were left unchanged. Benchmark changes remain uncommitted while that required check is failing.
