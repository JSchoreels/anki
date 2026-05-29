# Dynamic FSRS Desired Retention

Deck config stores Dynamic DR (SSP-MMC) data for FSRS-7 presets:

- `fsrs_dynamic_desired_retention_enabled`: enables ADR scheduling for an FSRS-7 preset.
- `fsrs_dynamic_desired_retention_params`: the 15 SSP-MMC policy coefficients.
- `fsrs_dynamic_desired_retention_weights`: calibration cost weights from policy evaluation.
- `fsrs_dynamic_desired_retention_avg_drs`: average ADR desired retentions for the calibration weights.

When FSRS-7 Dynamic DR is enabled, the scheduler interprets the preset desired retention as the target average ADR DR. It interpolates the matching cost weight from the calibration arrays, computes per-answer desired retention from the policy using internal stability and difficulty, and stores the actual chosen answer's desired retention on the card.

When the feature is disabled, scheduling remains the existing fixed desired-retention path.
