# Dynamic FSRS Desired Retention

Deck config stores Dynamic DR (SSP-MMC) data for FSRS-7 presets:

- `fsrs_dynamic_desired_retention_enabled`: enables ADR scheduling for an FSRS-7 preset.
- `fsrs_dynamic_desired_retention_params`: the 15 SSP-MMC policy coefficients.
- `fsrs_dynamic_desired_retention_weights`: calibration cost weights from policy evaluation.
- `fsrs_dynamic_desired_retention_avg_drs`: average ADR desired retentions for the calibration weights.
- `fsrs_dynamic_desired_retention_min`: lower ADR retention bound used by the trained policy.
- `fsrs_dynamic_desired_retention_max`: upper ADR retention bound used by the trained policy.

When FSRS-7 Dynamic DR is enabled, the scheduler interprets the preset desired retention as the target average ADR DR. It interpolates the matching cost weight from the calibration arrays, computes per-answer desired retention from the policy using internal stability and difficulty, and stores the actual chosen answer's desired retention on the card.

When FSRS-7 params are optimized with Dynamic DR enabled, Anki first optimizes the normal FSRS params, then computes long-term historical retrievability targets with those params. It uses the 5th and 95th percentiles as policy retention bounds, widened to at least 5 percentage points and clamped to the supported ADR range. Anki trains the SSP-MMC policy for that preset with those bounds, then stores the policy coefficients, the cost-weight/average-DR calibration arrays, and the bounds in deck config. When Dynamic DR is disabled, optimization leaves any stored Dynamic DR data unchanged and uses the normal fixed desired-retention scheduler path.

When the feature is disabled, scheduling remains the existing fixed desired-retention path.
