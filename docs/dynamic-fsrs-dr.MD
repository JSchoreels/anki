# Dynamic FSRS Desired Retention

Deck config stores Dynamic DR (SSP-MMC) data for FSRS-7 presets:

- `fsrs_dynamic_desired_retention_enabled`: enables ADR scheduling for an FSRS-7 preset.
- `fsrs_dynamic_desired_retention_params`: the 15 SSP-MMC policy coefficients.
- `fsrs_dynamic_desired_retention_weights`: calibration cost weights from policy evaluation.
- `fsrs_dynamic_desired_retention_avg_drs`: average ADR desired retentions for the calibration weights.
- `fsrs_dynamic_desired_retention_fsrs_eq_weights`: cost weights that have a fixed-FSRS equivalent DR.
- `fsrs_dynamic_desired_retention_fsrs_eq_drs`: fixed-FSRS desired retentions that reach the same simulated Memorized target as the matching `fsrs_eq_weights`.
- `fsrs_dynamic_desired_retention_min`: lower ADR retention bound used by the trained policy.
- `fsrs_dynamic_desired_retention_max`: upper ADR retention bound used by the trained policy.

When FSRS-7 Dynamic DR is enabled, the scheduler interprets the preset desired retention as a fixed-FSRS equivalent DR when `fsrs_eq_*` calibration is available, and falls back to target average ADR DR for older stored policies. It interpolates the matching cost weight from the calibration arrays, computes per-answer desired retention from the policy using internal stability and difficulty, and stores the actual chosen answer's desired retention on the card.

Browser Stability and Retrievability sorts read each matching card and its resolved FSRS preset so that FSRS-7 values use the exact model curve. Those sorts resolve presets in a batch, including add-on preset overlay rules, before computing per-card sort metrics.

When FSRS-7 params are optimized with Dynamic DR enabled, Anki first optimizes the normal FSRS params, then trains the SSP-MMC policy for that preset with the default ADR retention bounds `0.30-0.995`. The ADR simulator is shaped from the selected training slice: deck size is the number of reviewed cards, learn span is the number of days with learning reviews, and learn limit is the rounded cards-per-learning-day rate. The optimize request may override the ADR simulator review limit and daily cost budget in minutes; defaults are `9999` reviews and `720` minutes. It then computes simulation-backed calibration weights by bisection for evenly spaced average ADR desired-retention targets, and also stores the subset of those weights that can be mapped to fixed-FSRS equivalent DRs. It stores the policy coefficients, both calibration families, and the bounds in deck config. When Dynamic DR is disabled, optimization leaves any stored Dynamic DR data unchanged and uses the normal fixed desired-retention scheduler path.

The 3D visualizer uses the same target-DR calibration as scheduling. Its DR control starts from the preset desired retention; changing it only changes the preview until the user saves it back to the preset.

Add-ons can open the same visualizer with `aqt.dynamic_desired_retention_plot.open_dynamic_desired_retention_plot()` when they need to render external ADR preset data.

When an add-on opens the shared visualizer from another dialog, avoid nesting `QDialog.exec()` calls. A parent dialog opened with `exec()` can have its modal loop unwound when a child plot dialog also uses `exec()` or `open()`, leaving the main Anki window unclickable after both windows close. Use the newer non-blocking pattern for the parent dialog instead: keep a Python reference to the parent dialog, call `show()`, and apply saved config from the parent dialog's `finished` callback. The visualizer should also be kept alive by a parent-owned reference and shown with `show()` as a normal `Qt.WindowType.Window` child so macOS keeps native titlebar controls. The standalone visualizer route renders inline; do not wrap it in a Bootstrap modal inside the Qt dialog, or focus and modality can be trapped by a modal inside a modal.

During the ADR part of optimization, progress is shown as `Compute ADR values (SSP-MMC)`.

When the feature is disabled, scheduling remains the existing fixed desired-retention path.
