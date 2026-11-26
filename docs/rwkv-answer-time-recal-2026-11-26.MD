# Rolling per-user recalibration of the timed answer-time probabilities (SM175) — 2026-11-26

The answer time is the whole time on the card, front and back: from the question shown to the grade press, capped at the preset's maximum answer seconds. "Within S s" always means recalled (or Good or Easy, or Easy) **and** answered within S seconds, reading the back included.

## What ships

Card Info's recall-by-answer-time chart now shows recalibrated values once the user has enough history. The answer-time head's raw P(recalled / Good or Easy / Easy and answered within S s) is mapped, per grade set and per threshold S, through sigmoid(a logit(p) + b), with (a, b) fitted on the user's own earlier reviews:

- **Rows:** the user's time-labelled reviews in the canonical RWKV history: a recorded time (duration above 0), a grade, and not the card's first review (a format-1 head also skips a card's same-day reviews, as it predicts none). The raw probability of each row is the head's prediction before that review, on the replayed state, exactly as Card Info queries it.
- **Blocks and window:** 7-day blocks counted from the user's first time-labelled review. Today's block uses (a, b) fitted on the latest 30,000 time-labelled reviews of strictly earlier days; reviews of today's block never enter the fit.
- **Minimum history:** fewer than 1,000 such reviews before today's block: no fit, the raw values are shown (about 96% of the 732-set rows had enough history).
- **Per event:** an event with fewer than 20 positives or 20 negatives among the fit rows keeps the identity map. S at or above the user's cap (60 s, or a lower maximum answer time that holds at least 0.5% and 20 of the recorded times) stays raw. Clamps: a in [0.05, 20], b in [-15, 15].
- **Fit:** Newton on the mean LogLoss with step halving, logit of p clipped to [1e-6, 1 - 1e-6], every event of the grid S in {2, 3, 4, 5, 6, 8, 10, 15, 20, 30} s fitted (the chart shows 2, 4, 6, 8, 10, 15, 20 and 30 s).
- **Repair:** non-decreasing in S, Easy within Good or Easy within recalled, and each timed event at most the raw P(set).
- **Unchanged:** the grade-only probabilities (recall, P(Good or Easy), P(Easy), the button probabilities), retrievability, intervals and every scheduling decision. The **RWKV : Time Under (if Good)** row (a conditional time probability, not one of the validated events) stays raw.

With a recalibration the chart's three lines come from the head's own grade probabilities (as in the validation); without one they stay the raw products of the active button probabilities and the head's time distributions.

## Evidence (training repository, 732 held-out users, show-time inputs)

The rolling recalibration on the 732 set (SM125), for the Big models (SM128) and for the six-layer Tiny (SM140), on the release-family models; the method was first tried on the user's own collection (SM123). "Reliable" = ECE below 0.01 and LogLoss below both baselines of fast Good/Easy prediction (SM120) with the 95% CI below 0 and at least 0.05%.

| model | 2-10 s timed cells reliable, raw / recalibrated (of 21) | Good or Easy within 4 s: ECE raw / recalibrated | LogLoss vs raw |
|---|---|---|---|
| the show-time big model (SM91s2) | 15 / 21 | 0.0116 / 0.0019 | -0.71% |
| the show-time instant-only big model (SM99) | 15 / 21 | 0.0108 / 0.0018 | -0.68% |
| the show-time two-head cool-down for Small (SM118) | 14 / 21 | 0.0128 / 0.0014 | -0.92% |
| the longer Small Instant-Only model (SM113) | 15 / 21 | 0.0105 / 0.0016 | -0.85% |
| the six-layer Tiny two-head model (SM130) | 13 / 21 | 0.0143 / 0.0016 | -1.24% |
| the six-layer Tiny Instant-Only model (SM130) | 14 / 21 | 0.0131 / 0.0022 | -1.09% |

No cell got worse; P(Good or Easy) and P(Easy) stay reliable raw.

## Where it is computed

- `rslib/src/rwkv/answer_time_recal.rs`: the events of a head prediction, the window, the fit, the repair and the encoding.
- `RwkvInference::fit_answer_time_recalibration` (`rslib/src/rwkv/engine.rs`): replays the canonical history on a private worker (the prefix through the bulk warm-up, the fit window one review at a time with the head's query), never touching the resident state. `RwkvRuntime::answer_time_recalibrator()` returns the model that serves the answer times (the instant model of a pair when it has a head).
- Bridge (`pylib/rsbridge/lib.rs`): `RwkvInference.answer_time_recalibrator()`, `RwkvAnswerTimeRecalibrator.fit_packed(reviews, today)`, `RwkvInference.set_answer_time_recalibration(values)`; `predict_answer_times_with_thresholds` adds a sixth element, the recalibrated (recalled, Good or Easy, Easy) per grid threshold, or `None`.
- Desktop (`qt/aqt/rwkv_scheduler.py`): opening Card Info starts the fit in a background thread when the backend has none for today's block (once per block and backend; a short history or a failure retries the next day). Until it is set, Card Info shows the raw values. The fit runs outside the runtime lock, so reviewing continues meanwhile.

Cost: one replay of the whole history per 7-day block (and after each start), plus the head on up to 30,000 reviews; the replay holds a second copy of the card and note states while it runs, as the workload forecast's calibration replay does. The fit itself is 33 two-parameter Newton fits.

## Parity and tests

- `rwkv_time_recal_matches_the_reference`: `qt/tools/rwkv_time_recal_vectors.py` runs the reference code unchanged (`recal_user` of the training repository's `scripts/sm125_recal.py`) on four SYNTHETIC users (no review data): the shipped scheme, a 20 s user cap, a smaller window that exercises the latest-rows cut and the 20-sample rule, and a too-short history. The Rust replay reproduces every recalibrated value within 4.5e-9 (1e-6 required) and the same recalibrated rows; the deployment fit (today's block only) gives each user's last row within 5e-11. Vectors: `rslib/src/rwkv/testdata/time-recal-vectors.json.gz`.
- `rwkv_time_recal_fits_only_on_earlier_reviews`, `rwkv_time_recal_keeps_raw_with_short_history`, `rwkv_time_recal_never_changes_grade_only_outputs`, `rwkv_time_recal_user_cap_follows_the_reference`, `rwkv_time_recal_events_follow_the_head`.
- `rwkv_runtime_answer_time_recalibration_replays_privately` (six-layer Tiny two-head model (SM130)): the fit leaves the runtime's grade and time outputs bitwise unchanged, ignores today's block, needs 1,000 reviews, and its raw row equals the Card Info query on a runtime warmed up to that review.
- Python: `test_recalibrated_answer_times_drive_the_recall_curve` and the bridge checks in `test_rsbridge_rwkv_answer_time_head_is_optional`.
