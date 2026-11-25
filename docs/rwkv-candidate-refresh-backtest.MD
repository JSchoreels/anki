# How often does the approximate RWKV-Instant queue find the lowest R?

On four held-out days, the revised 512-card window showed the same next review as the full queue on all 351 decisions when both used precomputation. The 1,024-card window used by the tested preset already reached that result. On those same decisions the precomputed card was among the five lowest R values after the latest answer 233 times (66.38%).

## Results

The following sessions answer the exact lowest-R eligible card at every step. Both policies see the same answers and timestamps. Each row reports the card selected **after the refresh** from the merged queue, which also contains older scores outside the window. For example, the revised 512-card update checks the first 384 cards by saved R and rotates the other 128 slots through the next 1,024 ranks.

| Cards per update | Previous policy, exact | Revised policy, exact | Revised policy, top five | Mean excess R, previous → revised |
| ---: | ---: | ---: | ---: | ---: |
| 64 | 211/351 (60.1%) | 263/351 (74.9%) | 333/351 (94.9%) | 0.245 → 0.181 percentage points |
| 512 | 348/351 (99.1%) | 351/351 (100%) | 351/351 (100%) | 0.0033 → 0 percentage points |
| 1,024 | 351/351 (100%) | 351/351 (100%) | 351/351 (100%) | 0 → 0 percentage points |

At 64 cards the new window **contained** the exact winner 264 times. An older score outside the window outranked it once. Each policy also chose a card above its target once. That counts as a miss for exact and top-five agreement. Score gaps and ranks use picks eligible under the full rescore. The largest gap at 64 increased from 7.21 to 8.07 percentage points even as the average fell. Both policies requested exactly the indicated number of cards per update in these sessions. A full update covered 7,517–7,552 ordinary reviews.

With precomputation enabled, the reviewer can already be showing the next card when the update finishes. At 512 and 1,024 cards, the revised **pre-update** choice matched the post-answer winner 103/351 times (29.3%), fell within the top five 233/351 times (66.4%), and averaged 0.36 percentage points above the lowest R. The largest gap was 3.07 points.

A second replay kept the actual recorded card choices. Both 512- and 1,024-card policies matched all 351 post-update winners, and the revised 64-card window improved from 325 to 337 matches. On each of the first three days the same card stayed lowest throughout that day's recorded answers. Answering the exact winner in the first experiment produced 99, 99, 99 and 54 distinct reference winners across the four days.

## Chance of selecting one of the first five cards

Each column gives the percentage of all 351 decisions where the selected card was within that many places of the front of the reference queue. For example, top 3 includes ranks 1, 2 and 3 at the same decision. Each checkpoint had at least 1,247 eligible cards in the simulated ascending-R sessions and at least 3,521 in the recorded-order replay under the full post-answer rescore. These are observed rates for this deck, model and period.

**What changes when only the faster-updates checkbox changes?**

Here both queues precompute the next card from their last completed update. The reference refreshed every card at that update. The revised 512-card window showed its first choice on 351/351 ascending-R decisions, up from 347/351. At 64 cards, top-five agreement rose from 317/351 to 332/351. An 8,192-card control covered the entire pool and matched the full queue on every decision in both replays.

| Answer order | Window | Policy | Top 1 | Top 2 | Top 3 | Top 4 | Top 5 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Ascending R | 64 | Previous | 63.53% | 77.78% | 83.48% | 88.03% | 90.31% |
| Ascending R | 64 | Revised | 72.93% | 85.47% | 88.89% | 93.45% | 94.59% |
| Ascending R | 512 | Previous | 98.86% | 99.72% | 100% | 100% | 100% |
| Ascending R | 1,024 | Previous | 100% | 100% | 100% | 100% | 100% |
| Ascending R | 512 or 1,024 | Revised | 100% | 100% | 100% | 100% | 100% |
| Recorded | 64 | Previous | 93.45% | 95.44% | 96.87% | 98.01% | 98.58% |
| Recorded | 64 | Revised | 96.87% | 98.86% | 99.43% | 99.43% | 99.43% |
| Recorded | 512 or 1,024 | Either | 100% | 100% | 100% | 100% | 100% |

**Compared with a fresh post-answer rescore in simulated ascending-R sessions**

| Window | Selection | Top 1 | Top 2 | Top 3 | Top 4 | Top 5 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 64 | Previous policy, after update | 60.11% | 77.49% | 84.05% | 86.32% | 90.03% |
| 64 | Revised policy, after update | 74.93% | 87.46% | 91.45% | 92.88% | 94.87% |
| 64 | Revised policy, before update | 24.50% | 41.31% | 51.28% | 57.83% | 63.53% |
| 512 | Previous policy, after update | 99.15% | 100% | 100% | 100% | 100% |
| 1,024 | Previous policy, after update | 100% | 100% | 100% | 100% | 100% |
| 512 or 1,024 | Revised policy, after update | 100% | 100% | 100% | 100% | 100% |
| 512 or 1,024 | Revised policy, before update | 29.34% | 48.15% | 56.98% | 62.11% | 66.38% |

The precomputed picks at 512 and 1,024 hit these cutoffs 103, 169, 200, 218 and 233 times respectively. Reaching 95% on these decisions took the top 21.

**Compared with a fresh post-answer rescore in the recorded-order replay**

| Window | Selection | Top 1 | Top 2 | Top 3 | Top 4 | Top 5 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 64 | Previous policy, after update | 92.59% | 95.44% | 96.87% | 97.15% | 98.29% |
| 64 | Revised policy, after update | 96.01% | 98.58% | 99.72% | 99.72% | 99.72% |
| 64 | Revised policy, before update | 88.89% | 90.60% | 92.59% | 93.16% | 94.30% |
| 512 or 1,024 | Either policy, after update | 100% | 100% | 100% | 100% | 100% |
| 512 or 1,024 | Either policy, before update | 89.46% | 91.74% | 93.73% | 94.02% | 94.87% |

## What was replayed

- The November 25, 2026 Main Profile backup at 18:46 supplied the vocabulary Yomitan deck. The native Small Instant model (SM113) used the default compact low-rank state. November 18–21 supplied development runs. November 22–25 supplied the held-out decisions, starting with 226,946 canonical collection answers. The final answer of each day has no following decision in the replay.
- Each day starts with the actual prior model history and a full score map. The replay refreshes after every answer and assumes the background update finishes before the following answer. Each checkpoint scores the entire ordinary review pool at the recorded answer timestamp. Each approximate policy receives only its requested scores and retains the other installed values. Ties use ascending card ID. The pre-update choice is included in the batch if already visible.
- The comparison with precomputation on for both queues ranks the currently eligible cards using the last completed full update. The comparison with a fresh rescore ranks them after applying the current answer. Both use the same card pool and intervening-review and elapsed-time rules.
- The candidate pool uses the backup's active deck membership and each card's last positive signed review interval. Selection uses the preset target of 85%, allows same-day reviews, and requires three intervening reviews and 90 elapsed seconds. This isolates review ranking before daily limits, sibling burial, learning steps, new-card mixing and add-on target overrides.
- In the simulated ascending-R sessions, the reference pick receives the recorded grade and duration for that time slot. Again takes the card out of the ordinary review pool for the rest of the day. The recorded-order replay applies the original answer to the original card. Agreement measures how closely a partial update reproduces the model's ordering.

## Reproduce

Run from the repository with a copied backup. Select `recorded` to replay the actual card choices, or add `--skip-last-days 4` for the development dates. The command extracts the archive in a temporary directory and prints aggregate JSON.

```sh
RELEASE=1 just rwkv-candidate-refresh-backtest /private/tmp/history.colpkg 'Japan::1. Vocabulary::1.2 Normal Scheduling::Yomitan' --days 4 --trajectory ascending
```

Backup SHA-256 was `d3bee80eb3d97289cd34548bdb1cc6860469fe6a7391b875e456ecd66a7176a5`.
