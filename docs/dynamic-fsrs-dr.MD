# Dynamic FSRS Desired Retention

Deck config stores Dynamic DR (SSP-MMC) data for FSRS-7 presets:

- `fsrs_dynamic_desired_retention_enabled`: enables ADR scheduling for an FSRS-7 preset.
- `fsrs_dynamic_desired_retention_params`: the 15 SSP-MMC policy coefficients.
- `fsrs_dynamic_desired_retention_weights`: calibration cost weights from policy evaluation.
- `fsrs_dynamic_desired_retention_avg_drs`: average ADR desired retentions for the calibration weights.
- `fsrs_dynamic_desired_retention_fsrs_eq_weights`: cost weights that have a fixed-FSRS equivalent DR.
- `fsrs_dynamic_desired_retention_fsrs_eq_drs`: fixed-FSRS desired retentions that reach the same simulated Memorized target as the matching `fsrs_eq_weights`.
- `fsrs_dynamic_desired_retention_fixed_target_weights`: cost weights selected from the efficient ADR frontier for fixed-FSRS DR targets.
- `fsrs_dynamic_desired_retention_fixed_target_drs`: fixed-FSRS DR targets covered by the matching `fixed_target_weights`.
- `fsrs_dynamic_desired_retention_min`: lower ADR retention bound used by the trained policy.
- `fsrs_dynamic_desired_retention_max`: upper ADR retention bound used by the trained policy.
- `fsrs_dynamic_desired_retention_clamp`: opt-in scheduling flag that clamps requested Dynamic DR targets outside the supported calibration range to the nearest supported target instead of using the fixed-FSRS DR fallback.

When FSRS-7 Dynamic DR is enabled, the scheduler interprets the preset desired retention as a fixed-FSRS target. With current optimized policies, it uses `fixed_target_*` calibration first: for a requested fixed DR, it selects the cheapest simulated ADR frontier point whose memorized average reaches at least the fixed-FSRS memorized target. The `fixed_target_drs` entries are the fixed-FSRS targets each stored weight covers, sorted by target, so targets between two entries use the next covered ADR point. Older stored policies without `fixed_target_*` fall back to `fsrs_eq_*` interpolation, then to target average ADR DR. After choosing the cost weight, scheduling computes per-answer desired retention from the policy using internal stability and difficulty, and stores the actual chosen answer's desired retention on the card. If the requested target is outside the supported calibration range, scheduling normally uses fixed FSRS DR. When `fsrs_dynamic_desired_retention_clamp` is enabled, the scheduler clamps the requested target to the nearest supported calibration target and uses Dynamic DR with that clamped target; invalid or missing Dynamic DR policy data still uses the fixed-FSRS path.

Browser Stability and Retrievability sorts read each matching card and its resolved FSRS preset so that FSRS-7 values use the exact model curve. Those sorts resolve presets in a batch, including add-on preset overlay rules, before computing per-card sort metrics.

Add-ons that need interval projections for many cards can call the batch desired-retention helper instead of `GetSchedulingStates` once per card. `fsrs_desired_retention_for_intervals_batch()` reads the requested cards, resolves each card's active FSRS preset in a batch, including add-on preset overlay rules, and returns the desired retention to use for interval projection. When Dynamic DR applies, the returned interval target is the Good-button ADR desired retention and the full per-answer ADR desired-retention array is included. When Dynamic DR is disabled, unavailable, or the requested target uses the fixed-FSRS fallback, the returned interval target is the requested fixed desired retention.

When FSRS-7 params are optimized with Dynamic DR enabled, Anki first optimizes the normal FSRS params, then trains the SSP-MMC policy for that preset with the default ADR retention bounds `0.30-0.995`. The ADR simulator is shaped from the selected training slice: deck size is the number of reviewed cards, learn span is the number of days with learning reviews, and learn limit is the rounded cards-per-learning-day rate. The optimize request may override the ADR simulator review limit and daily cost budget in minutes; defaults are `9999` reviews and `720` minutes. It then computes simulation-backed calibration weights by bisection for evenly spaced average ADR desired-retention targets, stores the subset of those weights that can be mapped to fixed-FSRS equivalent DRs, and stores the efficient fixed-target frontier points from ADR training. It stores the policy coefficients, all calibration families, and the bounds in deck config. When Dynamic DR is disabled, optimization leaves any stored Dynamic DR data unchanged and uses the normal fixed desired-retention scheduler path.

The 3D visualizer uses the same target-DR calibration as scheduling. Its DR control starts from the preset desired retention; changing it only changes the preview until the user saves it back to the preset.

The FSRS simulator has an opt-in Dynamic DR (ADR) toggle. With the toggle off,
the simulator treats the desired-retention input or Help Me Decide x-axis as a
fixed FSRS desired retention. With the toggle on, each simulator request carries
the fallback preset's Dynamic DR policy and calibration data. The backend also
resolves add-on `fsrsPresetOverlay.rules` once for the real starting cards, and
uses any matching add-on Dynamic DR preset as that card's initial simulator
preset. It additionally reads `fsrsPresetOverlay.simulator_rules` for preset
changes that can happen during the simulated review life. The simulator still
uses the displayed desired retention as the x-axis target, maps that target
through the active preset's scheduling calibration and clamp/fixed-fallback
rules, and then applies the ADR policy after each simulated repetition using the
card's simulated stability and difficulty. Simulator rules are evaluated in Rust
against simulated card state, currently repetition count and the current
scheduled interval, so add-ons can route to another preset during a simulation
without Python callbacks or database searches in the hot loop. A simulator rule
may also include a `search` selector; Anki resolves that selector once against
the real starting cards, then combines the resolved card-id set with simulated
repetition and interval bounds. In Help Me Decide, each subtree preset curve uses
that request's fallback ADR data, any matching starting-card add-on ADR preset,
plus any matching simulator overlay route; presets without valid ADR data remain
fixed-DR curves.

Add-ons can open the same visualizer with `aqt.dynamic_desired_retention_plot.open_dynamic_desired_retention_plot()` when they need to render external ADR preset data.

When an add-on opens the shared visualizer from another dialog, avoid nesting `QDialog.exec()` calls. A parent dialog opened with `exec()` can have its modal loop unwound when a child plot dialog also uses `exec()` or `open()`, leaving the main Anki window unclickable after both windows close. Use the newer non-blocking pattern for the parent dialog instead: keep a Python reference to the parent dialog, call `show()`, and apply saved config from the parent dialog's `finished` callback. The visualizer should also be kept alive by a parent-owned reference and shown with `show()` as a normal `Qt.WindowType.Window` child so macOS keeps native titlebar controls. The standalone visualizer route renders inline; do not wrap it in a Bootstrap modal inside the Qt dialog, or focus and modality can be trapped by a modal inside a modal.

During the ADR part of optimization, progress is shown as `Compute ADR values (SSP-MMC)`.

When the feature is disabled, scheduling remains the existing fixed desired-retention path.
