# RWKV sibling-only note memory experiment — 2026-10-10

Dropping note recurrent memory is a poor tradeoff for the current trained model. Keeping note memory only when a note has existing siblings saves **5.25 MB** of state payload but increases log loss by **0.69%** and RMSE(bins) by **3.08%**. Keeping it only when multiple cards have been reviewed saves **127.66 MB**, but increases log loss by **61.90%** and RMSE(bins) by **768.96%**. The experiment leaves the installed scheduler behavior unchanged.

## Accuracy

All **223,832 retained reviews** update the recurrent state. Scores cover **176,722 reviews** separated by at least one scheduler day, from **984 review days**, **13,965 cards**, and **13,190 notes**. The baseline is the current int8 card/note representation, with f16 shift vectors and f32 computation/shared scopes.

| Retain note memory for | Retained note states | Log loss | Relative loss increase | RMSE(bins) | Relative RMSE increase |
| --- | ---: | ---: | ---: | ---: | ---: |
| Every reviewed note | 13,190 | 0.45573628 | 0% | 0.01855671 | 0% |
| Notes with at least two existing cards | 12,679 | 0.45888669 | +0.6913% | 0.01912840 | +3.0807% |
| Notes with at least two reviewed cards | 775 | 0.73781972 | +61.8962% | 0.16125000 | +768.9577% |

Percent increases are `100 × (variant / current int8 baseline − 1)`. Lower is better. RMSE(bins) measures calibration, using exactly the clipping, outcomes and fixed review-history bins documented in [the precision benchmark](rwkv-state-precision-2026-10-10.MD). It is not RMSE of prediction differences or per-review binary outcomes. These are measured changes on this collection, without a confidence interval or a claim about every collection.

For the existing-siblings rule, the **11,081 scored reviews** on the 511 excluded notes have **19.66% higher log loss** and **60.79% higher RMSE(bins)** than the baseline on those same reviews. The small overall increase averages this damage over a much larger collection. The largest individual prediction change is **72.32 percentage points**; the 99th percentile across all scored reviews is **9.97 percentage points**.

For the reviewed-siblings rule, **170,290 scored reviews** lose their note stream. Even the **6,432 scored reviews** whose note memory is retained have **13.43% higher log loss**. Changes to note outputs propagate into the shared preset/global recurrent streams, so the effects extend beyond excluded notes.

## Storage and memory

| Retain note memory for | Note payload | Card + note payload reduction | Full native checkpoint | Checkpoint reduction | Fresh-load peak native RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| Every reviewed note | 135.63 MB | 0% | 371.33 MB | 0% | 503.09 MB |
| At least two existing cards | 130.38 MB | 1.50% | 365.95 MB | 1.45% | 497.91 MB |
| At least two reviewed cards | 7.97 MB | 36.38% | 243.40 MB | 34.45% | 341.28 MB |

MB is decimal. The exact int8 note-state payload is **10,283 bytes per note**, and the card payload is unchanged at **215,270,475 bytes**. The existing-siblings rule removes **5,254,613 bytes** of note state and **5,373,952 bytes** from the native checkpoint. The reviewed-siblings rule removes **127,663,445 bytes** of note state and **127,926,272 bytes** from the checkpoint.

Fresh-load peak RSS is the median of three separate native-process loads in rotated order. It includes the model, runtime features, state, loader scratch and native test runner, excluding Python and the Anki GUI. The existing-siblings rule reduces this measurement by about **5.18 MB (1.03%)**; the aggressive rule by **161.81 MB (32.16%)**. Allocation overhead explains why this differs from payload savings. The existing-siblings difference is small relative to process-memory variability.

These sizes use one full checkpoint per policy, including runtime feature state and minimal history-map placeholders. They differ from the prior precision benchmark's recovery-root-plus-final-delta store. Compare policies within this table, rather than directly comparing these absolute figures with the older report.

## Definition and method

- Used the already copied/extracted 2026-10-10 backup and its prepared canonical history. No live profile database or installed runtime was changed.
- Existing siblings are counted across the entire copied collection, including new, suspended, buried and other-deck cards. Of reviewed notes, 12,679 have at least two existing cards and 511 have exactly one.
- Reviewed siblings require at least two distinct cards in the retained review history. Only 775 notes qualify; 12,415 have only one reviewed card, often with another unreviewed sibling. This policy is materially different from checking whether siblings exist.
- Both eligibility sets are fixed from the final copied snapshot/history for the entire replay. The experiment does not simulate adding a sibling later or changing note types. A production policy would need a defined reconstruction/warm-start strategy for those cases.
- Every review retains the original card/note/deck/preset IDs, ID embeddings, missing-ID flags and input features. Excluded notes still run the trained note module, starting with fresh recurrent state for each review; their resulting note state is discarded. The module is not bypassed, and note IDs are not replaced with null.
- The experiment changes only the note recurrent stream plan through a per-model test-only eligibility set. Card memory and shared deck/preset/global recurrence stay enabled, with the existing learned weights. Inputs are replayed through the optimized native Rust path in calls of 16,384 reviews.
- The model has distinct card and note recurrent weights; single-card notes cannot simply alias their card state. The note module also consumes deck-module output. Its memory is a learned part of the recall predictor, beyond the presence of siblings.
- Saving uses the normal native checkpoint writer, finalization and fsync. All three full checkpoints restore byte-identical retained states and runtime features. A smaller interleaved scalar control verifies omission after every answer, bulk chunk continuity and save/reload continuation.
- Final `just check` passed in the isolated worktree. The initial check had two installer-fixture failures from a missing local macOS template submodule; initialization resolved them.
- The baseline's **223,832 predictions exactly match** the previously measured optimized desktop int8 replay. That verifies model/input/feature alignment against the existing production benchmark.
- Native load uses a warm OS file cache. Later load repeats overlapped the final repository check; their timings are retained for transparency but should not be used to claim a load-speed improvement. The one prediction-enabled replay per policy is also insufficient to establish a state-only rebuild speedup or extrapolate to the roughly ten-second normal rebuild.

Keep note state for all notes with the current model. Quantizing its representation preserves far more of the learned information. A sibling-only design would need further model work, such as training with missing note memory and re-evaluating accuracy, rather than simply deleting those streams.

[Aggregate measurements and verification](rwkv-sibling-note-memory-2026-10-10.json) contain no raw review history or note IDs. The local experiment patch and analysis scripts are saved under `.local/benchmarks/rwkv-f16-2026-10-10/`; the native hook ran only in the isolated benchmark worktree based on `d79d1ad56`.
