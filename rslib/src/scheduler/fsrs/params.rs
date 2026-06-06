// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
use std::collections::HashMap;
use std::collections::HashSet;
use std::iter;
use std::path::Path;
use std::sync::atomic::AtomicU8;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use anki_io::write_file;
use anki_proto::scheduler::ComputeFsrsParamsResponse;
use anki_proto::stats::revlog_entry;
use anki_proto::stats::Dataset;
use anki_proto::stats::DeckEntry;
use chrono::NaiveDate;
use chrono::NaiveTime;
use fsrs::compute_parameters;
use fsrs::evaluate_with_time_series_splits;
use fsrs::extract_simulator_config;
use fsrs::CombinedProgressState;
use fsrs::ComputeParametersInput;
use fsrs::ComputeParametersVersion;
use fsrs::CostAdrEvaluationPoint;
use fsrs::CostAdrMetrics;
use fsrs::CostAdrPolicy;
use fsrs::CostAdrTrainingConfig;
use fsrs::FSRSItem;
use fsrs::FSRSReview;
use fsrs::ModelEvaluation;
use fsrs::SimulatorConfig;
use fsrs::FSRS;
use itertools::Itertools;
use prost::Message;

use crate::decks::immediate_parent_name;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::revlog::RevlogReviewKind;
use crate::scheduler::fsrs::dynamic_desired_retention::DEFAULT_RETENTION_MAX;
use crate::scheduler::fsrs::dynamic_desired_retention::DEFAULT_RETENTION_MIN;
use crate::search::Node;
use crate::search::SearchNode;
use crate::search::SortMode;

pub(crate) type Params = Vec<f32>;

fn model_version_for_params(params: &[f32]) -> ComputeParametersVersion {
    if params.len() == 35 {
        ComputeParametersVersion::Fsrs7
    } else {
        ComputeParametersVersion::Fsrs6
    }
}

fn include_same_day_training_entries(
    model_version: ComputeParametersVersion,
    include_same_day_override: Option<bool>,
) -> bool {
    include_same_day_override.unwrap_or(matches!(model_version, ComputeParametersVersion::Fsrs7))
}

pub(crate) fn include_same_day_for_params(params: &[f32]) -> bool {
    include_same_day_training_entries(model_version_for_params(params), None)
}

pub(crate) fn ignore_revlogs_before_date_to_ms(
    ignore_revlogs_before_date: &String,
) -> Result<TimestampMillis> {
    Ok(match ignore_revlogs_before_date {
        s if s.is_empty() => 0,
        s => NaiveDate::parse_from_str(s.as_str(), "%Y-%m-%d")
            .or_else(|err| invalid_input!(err, "Error parsing date: {s}"))?
            .and_time(NaiveTime::from_hms_milli_opt(0, 0, 0, 0).unwrap())
            .and_utc()
            .timestamp_millis(),
    }
    .into())
}

pub(crate) fn ignore_revlogs_before_ms_from_config(config: &DeckConfig) -> Result<TimestampMillis> {
    ignore_revlogs_before_date_to_ms(&config.inner.ignore_revlogs_before_date)
}

pub struct ComputeParamsRequest<'t> {
    pub search: &'t str,
    pub ignore_revlogs_before_ms: TimestampMillis,
    pub current_preset: u32,
    pub total_presets: u32,
    pub current_params: &'t Params,
    pub num_of_relearning_steps: usize,
    pub health_check: bool,
    pub include_same_day_reviews: Option<bool>,
    pub model_version_override: Option<ComputeParametersVersion>,
    pub dynamic_desired_retention_enabled: bool,
    pub dynamic_desired_retention_review_limit: Option<u32>,
    pub dynamic_desired_retention_max_cost_perday_minutes: Option<f32>,
}

pub(crate) struct PreparedComputeParams {
    pub current_params: Params,
    pub num_of_relearning_steps: usize,
    pub model_version: ComputeParametersVersion,
    pub include_same_day_reviews: bool,
    pub dynamic_desired_retention_enabled: bool,
    pub simulator_config: SimulatorConfig,
    pub items: Vec<FSRSItem>,
    pub item_card_ids: Vec<i64>,
    pub target_counts: TrainingTargetCounts,
}

pub(crate) struct PrepareComputeParamsInput<'a> {
    pub search: &'a str,
    pub ignore_revlogs_before: TimestampMillis,
    pub current_params: &'a [f32],
    pub num_of_relearning_steps: usize,
    pub include_same_day_reviews: Option<bool>,
    pub model_version_override: Option<ComputeParametersVersion>,
    pub dynamic_desired_retention_enabled: bool,
    pub dynamic_desired_retention_simulator_options: DynamicDesiredRetentionSimulatorOptions,
}

struct DynamicDesiredRetentionCalibration {
    params: Vec<f32>,
    weights: Vec<f32>,
    avg_drs: Vec<f32>,
    fsrs_eq_weights: Vec<f32>,
    fsrs_eq_drs: Vec<f32>,
    retention_min: f32,
    retention_max: f32,
}

#[derive(Debug, Clone, Copy, PartialEq)]
struct DynamicDesiredRetentionBounds {
    retention_min: f32,
    retention_max: f32,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct DynamicDesiredRetentionSimulatorOptions {
    pub review_limit: Option<u32>,
    pub max_cost_perday_minutes: Option<f32>,
}

#[derive(Default, Clone, Copy, Debug, PartialEq, Eq)]
pub enum ComputeParamsProgressPhase {
    #[default]
    OptimizingFsrsParams = 0,
    TrainingDynamicDesiredRetention = 1,
}

impl ComputeParamsProgressPhase {
    pub(crate) fn from_shared(progress_phase: &SharedComputeParamsProgressPhase) -> Self {
        match progress_phase.load(Ordering::Acquire) {
            1 => Self::TrainingDynamicDesiredRetention,
            _ => Self::OptimizingFsrsParams,
        }
    }
}

pub(crate) type SharedComputeParamsProgressPhase = Arc<AtomicU8>;

pub(crate) fn new_compute_params_progress_phase() -> SharedComputeParamsProgressPhase {
    Arc::new(AtomicU8::new(
        ComputeParamsProgressPhase::OptimizingFsrsParams as u8,
    ))
}

fn set_compute_params_progress_phase(
    progress_phase: Option<&SharedComputeParamsProgressPhase>,
    phase: ComputeParamsProgressPhase,
) {
    if let Some(progress_phase) = progress_phase {
        progress_phase.store(phase as u8, Ordering::Release);
    }
}

/// r: retention
fn log_loss_adjustment(r: f32) -> f32 {
    0.623 * (4. * r * (1. - r)).powf(0.738)
}

/// r: retention
///
/// c: review count
fn rmse_adjustment(r: f32, c: u32) -> f32 {
    0.0135 / (r.powf(0.504) - 1.14) + 0.176 / ((c as f32 / 1000.).powf(0.825) + 2.22) + 0.101
}

#[derive(Clone)]
struct TrainingItemsForFsrs {
    items: Vec<FSRSItem>,
    card_ids: Option<Vec<i64>>,
}

impl TrainingItemsForFsrs {
    fn with_card_ids(items: Vec<FSRSItem>, card_ids: Vec<i64>) -> Self {
        debug_assert_eq!(items.len(), card_ids.len());
        Self {
            items,
            card_ids: Some(card_ids),
        }
    }

    fn without_card_ids(items: Vec<FSRSItem>) -> Self {
        Self {
            items,
            card_ids: None,
        }
    }

    fn filter_non_same_day_evaluation_targets(self) -> Self {
        match self.card_ids {
            Some(card_ids) => {
                let (items, card_ids) = self
                    .items
                    .into_iter()
                    .zip_eq(card_ids)
                    .filter(|(item, _)| has_long_term_target(item))
                    .unzip();
                Self {
                    items,
                    card_ids: Some(card_ids),
                }
            }
            None => Self::without_card_ids(
                self.items
                    .into_iter()
                    .filter(has_long_term_target)
                    .collect(),
            ),
        }
    }

    fn slice(&self, start: usize, end: usize) -> Self {
        Self {
            items: self.items[start..end].to_vec(),
            card_ids: self.card_ids.as_ref().map(|ids| ids[start..end].to_vec()),
        }
    }

    fn target_counts(&self) -> TrainingTargetCounts {
        training_target_counts_from_items(&self.items)
    }
}

fn has_long_term_target(item: &FSRSItem) -> bool {
    item.reviews
        .last()
        .is_some_and(|review| review.delta_t >= 1.0)
}

#[cfg(test)]
fn filter_non_same_day_evaluation_targets(items: Vec<FSRSItem>) -> Vec<FSRSItem> {
    TrainingItemsForFsrs::without_card_ids(items)
        .filter_non_same_day_evaluation_targets()
        .items
}

fn training_search<'a>(search: &'a str, search_for_training: Option<&'a str>) -> &'a str {
    match search_for_training.map(str::trim) {
        Some(non_empty) if !non_empty.is_empty() => non_empty,
        _ => search,
    }
}

fn uses_external_evaluation(training_search: &str, evaluation_search: &str) -> bool {
    training_search != evaluation_search
}

fn resolved_model_version(
    current_params: &[f32],
    model_version_override: Option<ComputeParametersVersion>,
) -> ComputeParametersVersion {
    model_version_override.unwrap_or_else(|| model_version_for_params(current_params))
}

fn training_target_counts_from_items(items: &[FSRSItem]) -> TrainingTargetCounts {
    let total_targets = items.len();
    let long_term_targets = items
        .iter()
        .filter(|item| has_long_term_target(item))
        .count();
    let short_term_targets = total_targets.saturating_sub(long_term_targets);
    TrainingTargetCounts {
        total_targets,
        long_term_targets,
        short_term_targets,
    }
}

fn health_check_passed_for_evaluated_targets(eval: ModelEvaluation, items: &[FSRSItem]) -> bool {
    let fsrs_items = items.len() as u32;
    if fsrs_items == 0 {
        return false;
    }
    let r = items.iter().fold(0, |passed, item| {
        passed
            + (item
                .reviews
                .last()
                .map(|reviews| reviews.rating)
                .unwrap_or(0)
                > 1) as u32
    }) as f32
        / fsrs_items as f32;
    let adjusted_log_loss = eval.log_loss / log_loss_adjustment(r);
    let adjusted_rmse = eval.rmse_bins / rmse_adjustment(r, fsrs_items);
    adjusted_log_loss <= 1.11 || adjusted_rmse <= 1.53
}

fn time_series_split_items(
    sorted_items: TrainingItemsForFsrs,
    n_splits: usize,
) -> Vec<(TrainingItemsForFsrs, TrainingItemsForFsrs)> {
    if sorted_items.items.is_empty() || n_splits == 0 {
        return vec![];
    }
    let total_items = sorted_items.items.len();
    let segment_size = total_items / (n_splits + 1);
    if segment_size == 0 {
        return vec![];
    }
    (0..n_splits)
        .map(|i| {
            let test_start = (i + 1) * segment_size;
            let test_end = if i == n_splits - 1 {
                total_items
            } else {
                (i + 2) * segment_size
            };
            (
                sorted_items.slice(0, test_start),
                sorted_items.slice(test_start, test_end),
            )
        })
        .collect()
}

fn evaluate_with_time_series_splits_for_targets<F>(
    ComputeParametersInput {
        train_set,
        card_ids,
        enable_short_term,
        enable_sched_penalties,
        model_version,
        num_relearning_steps,
        ..
    }: ComputeParametersInput,
    include_target: impl Fn(&FSRSItem) -> bool,
    mut progress: F,
) -> Result<ModelEvaluation>
where
    F: FnMut(fsrs::ItemProgress) -> bool,
{
    if train_set.is_empty() {
        return Err(fsrs::FSRSError::NotEnoughData.into());
    }
    let splits = time_series_split_items(
        TrainingItemsForFsrs {
            items: train_set,
            card_ids,
        },
        5,
    );
    if splits.is_empty() {
        return Err(fsrs::FSRSError::NotEnoughData.into());
    }
    let mut progress_info = fsrs::ItemProgress {
        current: 0,
        total: splits.len(),
    };
    let mut total_eval_items = 0usize;
    let mut weighted_log_loss = 0.0f64;
    let mut weighted_rmse_bins = 0.0f64;

    for (train_items, test_items) in splits {
        let parameters = compute_parameters(ComputeParametersInput {
            train_set: train_items.items,
            card_ids: train_items.card_ids,
            progress: None,
            enable_short_term,
            enable_sched_penalties,
            model_version,
            num_relearning_steps,
        })?;
        let eval_items = test_items
            .items
            .into_iter()
            .filter(|item| include_target(item))
            .collect_vec();
        if !eval_items.is_empty() {
            let fold_eval = FSRS::new(&parameters)?.evaluate(eval_items.clone(), |_| true)?;
            let fold_size = eval_items.len() as f64;
            weighted_log_loss += fold_eval.log_loss as f64 * fold_size;
            weighted_rmse_bins += fold_eval.rmse_bins as f64 * fold_size;
            total_eval_items += eval_items.len();
        }

        progress_info.current += 1;
        if !progress(progress_info) {
            return Err(fsrs::FSRSError::Interrupted.into());
        }
    }

    if total_eval_items == 0 {
        return Err(fsrs::FSRSError::NotEnoughData.into());
    }

    Ok(ModelEvaluation {
        log_loss: (weighted_log_loss / total_eval_items as f64) as f32,
        rmse_bins: (weighted_rmse_bins / total_eval_items as f64) as f32,
    })
}

fn evaluate_from_training_to_external_targets(
    ComputeParametersInput {
        train_set,
        card_ids,
        enable_short_term,
        enable_sched_penalties,
        model_version,
        num_relearning_steps,
        ..
    }: ComputeParametersInput,
    evaluation_set: Vec<FSRSItem>,
) -> Result<ModelEvaluation> {
    if train_set.is_empty() || evaluation_set.is_empty() {
        return Err(fsrs::FSRSError::NotEnoughData.into());
    }
    let parameters = compute_parameters(ComputeParametersInput {
        train_set,
        card_ids,
        progress: None,
        enable_short_term,
        enable_sched_penalties,
        model_version,
        num_relearning_steps,
    })?;
    Ok(FSRS::new(&parameters)?.evaluate(evaluation_set, |_| true)?)
}

pub(crate) fn compute_params_from_prepared(
    PreparedComputeParams {
        current_params,
        num_of_relearning_steps,
        model_version,
        include_same_day_reviews,
        dynamic_desired_retention_enabled,
        simulator_config,
        items,
        item_card_ids,
        target_counts: _,
    }: PreparedComputeParams,
    progress: Option<Arc<Mutex<CombinedProgressState>>>,
    progress_phase: Option<SharedComputeParamsProgressPhase>,
    health_check: bool,
) -> Result<ComputeFsrsParamsResponse> {
    let fsrs_items = items.len() as u32;
    if fsrs_items == 0 {
        return Ok(ComputeFsrsParamsResponse {
            params: current_params,
            fsrs_items,
            health_check_passed: None,
            fsrs_dynamic_desired_retention_params: Vec::new(),
            fsrs_dynamic_desired_retention_weights: Vec::new(),
            fsrs_dynamic_desired_retention_avg_drs: Vec::new(),
            fsrs_dynamic_desired_retention_fsrs_eq_weights: Vec::new(),
            fsrs_dynamic_desired_retention_fsrs_eq_drs: Vec::new(),
            fsrs_dynamic_desired_retention_min: 0.0,
            fsrs_dynamic_desired_retention_max: 0.0,
        });
    }

    set_compute_params_progress_phase(
        progress_phase.as_ref(),
        ComputeParamsProgressPhase::OptimizingFsrsParams,
    );
    let input = ComputeParametersInput {
        train_set: items.clone(),
        card_ids: Some(item_card_ids.clone()),
        progress: progress.clone(),
        enable_short_term: true,
        enable_sched_penalties: true,
        model_version,
        num_relearning_steps: Some(num_of_relearning_steps),
    };
    let params = coerce_computed_params_to_selected_version(
        model_version,
        &current_params,
        compute_parameters(input)?,
    );
    let dynamic_desired_retention = train_dynamic_desired_retention(
        dynamic_desired_retention_enabled,
        model_version,
        &simulator_config,
        &items,
        &params,
        progress,
        progress_phase.as_ref(),
    )?;

    let health_check_items = if include_same_day_reviews {
        TrainingItemsForFsrs::with_card_ids(items.clone(), item_card_ids.clone())
    } else {
        TrainingItemsForFsrs::with_card_ids(items.clone(), item_card_ids.clone())
            .filter_non_same_day_evaluation_targets()
    };
    let health_check_passed = if health_check && health_check_items.items.len() > 300 {
        evaluate_with_time_series_splits(
            ComputeParametersInput {
                train_set: health_check_items.items.clone(),
                card_ids: health_check_items.card_ids.clone(),
                progress: None,
                enable_short_term: true,
                enable_sched_penalties: true,
                model_version,
                num_relearning_steps: Some(num_of_relearning_steps),
            },
            |_| true,
        )
        .ok()
        .map(|eval| health_check_passed_for_evaluated_targets(eval, &health_check_items.items))
    } else {
        None
    };

    Ok(ComputeFsrsParamsResponse {
        params,
        fsrs_items,
        health_check_passed,
        fsrs_dynamic_desired_retention_params: dynamic_desired_retention
            .as_ref()
            .map(|calibration| calibration.params.clone())
            .unwrap_or_default(),
        fsrs_dynamic_desired_retention_weights: dynamic_desired_retention
            .as_ref()
            .map(|calibration| calibration.weights.clone())
            .unwrap_or_default(),
        fsrs_dynamic_desired_retention_avg_drs: dynamic_desired_retention
            .as_ref()
            .map(|calibration| calibration.avg_drs.clone())
            .unwrap_or_default(),
        fsrs_dynamic_desired_retention_fsrs_eq_weights: dynamic_desired_retention
            .as_ref()
            .map(|calibration| calibration.fsrs_eq_weights.clone())
            .unwrap_or_default(),
        fsrs_dynamic_desired_retention_fsrs_eq_drs: dynamic_desired_retention
            .as_ref()
            .map(|calibration| calibration.fsrs_eq_drs.clone())
            .unwrap_or_default(),
        fsrs_dynamic_desired_retention_min: dynamic_desired_retention
            .as_ref()
            .map(|calibration| calibration.retention_min)
            .unwrap_or_default(),
        fsrs_dynamic_desired_retention_max: dynamic_desired_retention
            .as_ref()
            .map(|calibration| calibration.retention_max)
            .unwrap_or_default(),
    })
}

fn train_dynamic_desired_retention(
    enabled: bool,
    model_version: ComputeParametersVersion,
    simulator_config: &SimulatorConfig,
    _items: &[FSRSItem],
    params: &[f32],
    progress: Option<Arc<Mutex<CombinedProgressState>>>,
    progress_phase: Option<&SharedComputeParamsProgressPhase>,
) -> Result<Option<DynamicDesiredRetentionCalibration>> {
    if !enabled || model_version != ComputeParametersVersion::Fsrs7 {
        return Ok(None);
    }

    let bounds = default_dynamic_desired_retention_bounds();
    set_compute_params_progress_phase(
        progress_phase,
        ComputeParamsProgressPhase::TrainingDynamicDesiredRetention,
    );
    let training_config = CostAdrTrainingConfig {
        retention_min: bounds.retention_min,
        retention_max: bounds.retention_max,
        progress,
        ..Default::default()
    };
    let result = CostAdrPolicy::train_single_user(simulator_config, params, &training_config)?;
    let calibration_points = result.policy.calibrate_average_desired_retention_range(
        simulator_config,
        params,
        DYNAMIC_DR_CALIBRATION_POINT_COUNT,
        training_config.simulation_seed,
    )?;
    let fsrs_equivalent_points = fsrs_equivalent_desired_retention_points(
        &training_config.baseline_desired_retentions,
        &result.baseline_metrics,
        &calibration_points,
    );
    dynamic_desired_retention_calibration_from_parts(
        result.policy.coefficients,
        calibration_points
            .into_iter()
            .map(|point| (point.goal_cost_weight, point.average_desired_retention)),
        fsrs_equivalent_points,
        bounds,
    )
    .map(Some)
}

const DYNAMIC_DR_CALIBRATION_POINT_COUNT: usize = 16;
const DYNAMIC_DR_DEFAULT_REVIEW_LIMIT: usize = 9999;
const DYNAMIC_DR_DEFAULT_MAX_COST_PERDAY_MINUTES: f32 = 720.0;

fn default_dynamic_desired_retention_bounds() -> DynamicDesiredRetentionBounds {
    DynamicDesiredRetentionBounds {
        retention_min: DEFAULT_RETENTION_MIN,
        retention_max: DEFAULT_RETENTION_MAX,
    }
}

fn shape_simulator_config_for_dynamic_desired_retention(
    config: &mut SimulatorConfig,
    revlogs: &[RevlogEntry],
    day_cutoff: i64,
    options: DynamicDesiredRetentionSimulatorOptions,
) -> Result<()> {
    let reviewed_cards = revlogs
        .iter()
        .map(|entry| entry.cid)
        .collect::<HashSet<_>>();
    if reviewed_cards.is_empty() {
        return Ok(());
    }
    let review_limit = options
        .review_limit
        .map(|value| value as usize)
        .unwrap_or(DYNAMIC_DR_DEFAULT_REVIEW_LIMIT);
    let max_cost_perday_minutes = options
        .max_cost_perday_minutes
        .unwrap_or(DYNAMIC_DR_DEFAULT_MAX_COST_PERDAY_MINUTES);
    require!(review_limit > 0, "Dynamic DR review limit must be positive");
    require!(
        max_cost_perday_minutes.is_finite() && max_cost_perday_minutes > 0.0,
        "Dynamic DR daily time budget must be positive minutes"
    );

    let active_new_card_days = revlogs
        .iter()
        .filter(|entry| entry.review_kind == RevlogReviewKind::Learning)
        .map(|entry| real_day(entry.id.0, day_cutoff))
        .collect::<HashSet<_>>()
        .len()
        .max(1);
    let learn_limit =
        ((reviewed_cards.len() as f32 / active_new_card_days as f32).round() as usize).max(1);

    config.deck_size = reviewed_cards.len();
    config.learn_span = active_new_card_days;
    config.learn_limit = learn_limit;
    config.review_limit = review_limit;
    config.max_cost_perday = max_cost_perday_minutes * 60.0;
    Ok(())
}

fn real_day(timestamp_millis: i64, day_cutoff: i64) -> i64 {
    (timestamp_millis / 1000 - day_cutoff) / 86400
}

fn dynamic_desired_retention_calibration_from_parts(
    params: Vec<f32>,
    points: impl IntoIterator<Item = (f32, Option<f32>)>,
    fsrs_equivalent_points: impl IntoIterator<Item = (f32, f32)>,
    bounds: DynamicDesiredRetentionBounds,
) -> Result<DynamicDesiredRetentionCalibration> {
    let calibration = points
        .into_iter()
        .filter_map(|(weight, average_desired_retention)| {
            average_desired_retention.map(|avg_dr| (weight, avg_dr))
        })
        .collect::<Vec<_>>();
    require!(
        calibration.len() >= 2,
        "Dynamic DR calibration did not produce enough points"
    );

    let (weights, avg_drs) = calibration.into_iter().unzip();
    let (fsrs_eq_weights, fsrs_eq_drs) = fsrs_equivalent_points.into_iter().unzip();
    Ok(DynamicDesiredRetentionCalibration {
        params,
        weights,
        avg_drs,
        fsrs_eq_weights,
        fsrs_eq_drs,
        retention_min: bounds.retention_min,
        retention_max: bounds.retention_max,
    })
}

fn fsrs_equivalent_desired_retention_points(
    baseline_desired_retentions: &[f32],
    baseline_metrics: &[CostAdrMetrics],
    points: &[CostAdrEvaluationPoint],
) -> Vec<(f32, f32)> {
    let mut baseline_points = baseline_desired_retentions
        .iter()
        .copied()
        .zip(baseline_metrics.iter().copied())
        .filter(|(desired_retention, metrics)| {
            desired_retention.is_finite()
                && metrics.memorized_average.is_finite()
                && metrics.time_average.is_finite()
        })
        .map(|(desired_retention, metrics)| (metrics.memorized_average, desired_retention))
        .collect::<Vec<_>>();
    baseline_points.sort_by(|left, right| left.0.total_cmp(&right.0));

    points
        .iter()
        .filter_map(|point| {
            interpolated_desired_retention_for_memory_target(
                &baseline_points,
                point.metrics.memorized_average,
            )
            .map(|desired_retention| (point.goal_cost_weight, desired_retention))
        })
        .collect()
}

fn interpolated_desired_retention_for_memory_target(
    baseline_points: &[(f32, f32)],
    target_memorized_average: f32,
) -> Option<f32> {
    if !(target_memorized_average.is_finite() && baseline_points.len() >= 2) {
        return None;
    }

    baseline_points.windows(2).find_map(|pair| {
        let (left_memory, left_retention) = pair[0];
        let (right_memory, right_retention) = pair[1];
        if (left_memory - target_memorized_average) * (right_memory - target_memorized_average)
            > 0.0
        {
            return None;
        }
        if (left_memory - right_memory).abs() < f32::EPSILON {
            return Some(left_retention);
        }
        let t = ((target_memorized_average - left_memory) / (right_memory - left_memory))
            .clamp(0.0, 1.0);
        Some(left_retention + (right_retention - left_retention) * t)
    })
}

impl Collection {
    /// Note this does not return an error if there are less than 400 items -
    /// the caller should instead check the fsrs_items count in the return
    /// value.
    pub fn compute_params(
        &mut self,
        request: ComputeParamsRequest,
    ) -> Result<ComputeFsrsParamsResponse> {
        let ComputeParamsRequest {
            search,
            ignore_revlogs_before_ms: ignore_revlogs_before,
            current_preset,
            total_presets,
            current_params,
            num_of_relearning_steps,
            health_check,
            include_same_day_reviews,
            model_version_override,
            dynamic_desired_retention_enabled,
            dynamic_desired_retention_review_limit,
            dynamic_desired_retention_max_cost_perday_minutes,
        } = request;

        self.clear_progress();
        let prepared = self.prepare_compute_params(PrepareComputeParamsInput {
            search,
            ignore_revlogs_before,
            current_params,
            num_of_relearning_steps,
            include_same_day_reviews,
            model_version_override,
            dynamic_desired_retention_enabled,
            dynamic_desired_retention_simulator_options: DynamicDesiredRetentionSimulatorOptions {
                review_limit: dynamic_desired_retention_review_limit,
                max_cost_perday_minutes: dynamic_desired_retention_max_cost_perday_minutes,
            },
        })?;

        if prepared.items.is_empty() {
            return Ok(ComputeFsrsParamsResponse {
                params: current_params.to_vec(),
                fsrs_items: 0,
                health_check_passed: None,
                fsrs_dynamic_desired_retention_params: Vec::new(),
                fsrs_dynamic_desired_retention_weights: Vec::new(),
                fsrs_dynamic_desired_retention_avg_drs: Vec::new(),
                fsrs_dynamic_desired_retention_fsrs_eq_weights: Vec::new(),
                fsrs_dynamic_desired_retention_fsrs_eq_drs: Vec::new(),
                fsrs_dynamic_desired_retention_min: 0.0,
                fsrs_dynamic_desired_retention_max: 0.0,
            });
        }
        // adapt the progress handler to our built-in progress handling

        let create_progress_thread = || -> Result<_> {
            let mut anki_progress = self.new_progress_handler::<ComputeParamsProgress>();
            anki_progress.update(false, |p| {
                p.current_preset = current_preset;
                p.total_presets = total_presets;
            })?;
            let progress = CombinedProgressState::new_shared();
            let progress_phase = new_compute_params_progress_phase();
            let progress2 = progress.clone();
            let progress_phase2 = progress_phase.clone();
            let progress_thread = thread::spawn(move || {
                let mut finished = false;
                while !finished {
                    thread::sleep(Duration::from_millis(100));
                    let mut guard = progress.lock().unwrap();
                    if let Err(_err) = anki_progress.update(false, |s| {
                        s.total_iterations = guard.total() as u32;
                        s.current_iteration = guard.current() as u32;
                        s.reviews = prepared.target_counts.total_targets as u32;
                        s.long_term_reviews = prepared.target_counts.long_term_targets as u32;
                        s.short_term_reviews = prepared.target_counts.short_term_targets as u32;
                        s.phase = ComputeParamsProgressPhase::from_shared(&progress_phase);
                        finished = guard.finished();
                    }) {
                        guard.want_abort = true;
                        return;
                    }
                }
            });
            Ok((progress2, progress_phase2, progress_thread))
        };

        let (progress, progress_phase, progress_thread) = create_progress_thread()?;
        let output = compute_params_from_prepared(
            prepared,
            Some(progress.clone()),
            Some(progress_phase),
            health_check,
        );
        progress_thread.join().ok();
        output
    }

    pub(crate) fn prepare_compute_params(
        &mut self,
        input: PrepareComputeParamsInput<'_>,
    ) -> Result<PreparedComputeParams> {
        let PrepareComputeParamsInput {
            search,
            ignore_revlogs_before,
            current_params,
            num_of_relearning_steps,
            include_same_day_reviews,
            model_version_override,
            dynamic_desired_retention_enabled,
            dynamic_desired_retention_simulator_options,
        } = input;
        let timing = self.timing_today()?;
        let revlogs = self.revlog_for_srs(search)?;
        let mut simulator_config = extract_simulator_config(
            revlogs.iter().cloned().map(Into::into).collect(),
            timing.next_day_at.into(),
            true,
        );
        if dynamic_desired_retention_enabled {
            shape_simulator_config_for_dynamic_desired_retention(
                &mut simulator_config,
                &revlogs,
                timing.next_day_at.into(),
                dynamic_desired_retention_simulator_options,
            )?;
        }
        let model_version = resolved_model_version(current_params, model_version_override);
        let include_same_day_reviews =
            include_same_day_training_entries(model_version, include_same_day_reviews);
        let training_items = fsrs_items_for_training(
            revlogs,
            timing.next_day_at,
            ignore_revlogs_before,
            include_same_day_reviews,
        );
        let target_counts = training_items.target_counts();
        let TrainingItemsForFsrs { items, card_ids } = training_items;
        Ok(PreparedComputeParams {
            current_params: current_params.to_vec(),
            num_of_relearning_steps,
            model_version,
            include_same_day_reviews,
            dynamic_desired_retention_enabled,
            simulator_config,
            items,
            item_card_ids: card_ids.unwrap_or_default(),
            target_counts,
        })
    }

    pub(crate) fn revlog_for_srs(
        &mut self,
        search: impl TryIntoSearch,
    ) -> Result<Vec<RevlogEntry>> {
        let search = search.try_into_search()?;
        // a whole-collection search can match revlog entries of deleted cards, too
        if let Node::Group(nodes) = &search {
            if let &[Node::Search(SearchNode::WholeCollection)] = &nodes[..] {
                return self.storage.get_all_revlog_entries_in_card_order();
            }
        }
        self.search_cards_into_table(search, SortMode::NoOrder)?
            .col
            .storage
            .get_revlog_entries_for_searched_cards_in_card_order()
    }

    /// Used for exporting revlogs for algorithm research.
    pub fn export_dataset(&mut self, min_entries: usize, target_path: &Path) -> Result<()> {
        let revlog_entries = self.storage.get_revlog_entries_for_export_dataset()?;
        if revlog_entries.len() < min_entries {
            return Err(AnkiError::FsrsInsufficientData);
        }
        let revlogs = revlog_entries
            .into_iter()
            .map(revlog_entry_to_proto)
            .collect_vec();
        let cards = self.storage.get_all_card_entries()?;

        let decks_map = self.storage.get_decks_map()?;
        let deck_name_to_id: HashMap<String, DeckId> = decks_map
            .into_iter()
            .map(|(id, deck)| (deck.name.to_string(), id))
            .collect();

        let decks = self
            .storage
            .get_all_decks()?
            .into_iter()
            .filter_map(|deck| {
                if let Some(preset_id) = deck.config_id().map(|id| id.0) {
                    let parent_id = immediate_parent_name(&deck.name.to_string())
                        .and_then(|parent_name| deck_name_to_id.get(parent_name))
                        .map(|id| id.0)
                        .unwrap_or(0);
                    Some(DeckEntry {
                        id: deck.id.0,
                        parent_id,
                        preset_id,
                    })
                } else {
                    None
                }
            })
            .collect_vec();
        let next_day_at = self.timing_today()?.next_day_at.0;
        let dataset = Dataset {
            revlogs,
            cards,
            decks,
            next_day_at,
        };
        let data = dataset.encode_to_vec();
        write_file(target_path, data)?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn evaluate_params(
        &mut self,
        search: &str,
        search_for_training: Option<&str>,
        ignore_revlogs_before: TimestampMillis,
        num_of_relearning_steps: usize,
        model_version: ComputeParametersVersion,
        include_same_day_reviews: Option<bool>,
        include_same_day_reviews_for_training: Option<bool>,
    ) -> Result<ModelEvaluation> {
        let timing = self.timing_today()?;
        let training_search = training_search(search, search_for_training);
        let training_revlogs = self.revlog_for_srs(training_search)?;
        let evaluation_revlogs = if training_search == search {
            training_revlogs.clone()
        } else {
            self.revlog_for_srs(search)?
        };
        let include_same_day_reviews_for_training =
            include_same_day_training_entries(model_version, include_same_day_reviews_for_training);
        let include_same_day_reviews =
            include_same_day_training_entries(model_version, include_same_day_reviews);
        let training_items = fsrs_items_for_training(
            training_revlogs,
            timing.next_day_at,
            ignore_revlogs_before,
            include_same_day_reviews_for_training,
        );
        let evaluation_base_items = fsrs_items_for_training(
            evaluation_revlogs,
            timing.next_day_at,
            ignore_revlogs_before,
            include_same_day_reviews,
        );
        let evaluation_items = if include_same_day_reviews {
            evaluation_base_items
        } else {
            evaluation_base_items.filter_non_same_day_evaluation_targets()
        };
        let target_counts = evaluation_items.target_counts();
        let mut anki_progress = self.new_progress_handler::<ComputeParamsProgress>();
        anki_progress.state.reviews = target_counts.total_targets as u32;
        anki_progress.state.long_term_reviews = target_counts.long_term_targets as u32;
        anki_progress.state.short_term_reviews = target_counts.short_term_targets as u32;
        // Ensure UI receives review counts even in paths that don't emit per-fold
        // progress.
        let _ = anki_progress.update(false, |_| {});

        let eval = if uses_external_evaluation(training_search, search) {
            evaluate_from_training_to_external_targets(
                ComputeParametersInput {
                    train_set: training_items.items,
                    card_ids: training_items.card_ids,
                    progress: None,
                    enable_short_term: true,
                    enable_sched_penalties: true,
                    model_version,
                    num_relearning_steps: Some(num_of_relearning_steps),
                },
                evaluation_items.items,
            )?
        } else if include_same_day_reviews == include_same_day_reviews_for_training {
            evaluate_with_time_series_splits(
                ComputeParametersInput {
                    train_set: evaluation_items.items,
                    card_ids: evaluation_items.card_ids,
                    progress: None,
                    enable_short_term: true,
                    enable_sched_penalties: true,
                    model_version,
                    num_relearning_steps: Some(num_of_relearning_steps),
                },
                |ip| {
                    anki_progress
                        .update(false, |p| {
                            p.total_iterations = ip.total as u32;
                            p.current_iteration = ip.current as u32;
                        })
                        .is_ok()
                },
            )?
        } else {
            evaluate_with_time_series_splits_for_targets(
                ComputeParametersInput {
                    train_set: training_items.items,
                    card_ids: training_items.card_ids,
                    progress: None,
                    enable_short_term: true,
                    enable_sched_penalties: true,
                    model_version,
                    num_relearning_steps: Some(num_of_relearning_steps),
                },
                |item| {
                    include_same_day_reviews
                        || item
                            .reviews
                            .last()
                            .is_some_and(|review| review.delta_t >= 1.0)
                },
                |ip| {
                    anki_progress
                        .update(false, |p| {
                            p.total_iterations = ip.total as u32;
                            p.current_iteration = ip.current as u32;
                        })
                        .is_ok()
                },
            )?
        };
        Ok(eval)
    }

    pub fn evaluate_params_legacy(
        &mut self,
        params: &Params,
        search: &str,
        ignore_revlogs_before: TimestampMillis,
        include_same_day_reviews: Option<bool>,
    ) -> Result<ModelEvaluation> {
        let timing = self.timing_today()?;
        let mut anki_progress = self.new_progress_handler::<ComputeParamsProgress>();
        let guard = self.search_cards_into_table(search, SortMode::NoOrder)?;
        let revlogs: Vec<RevlogEntry> = guard
            .col
            .storage
            .get_revlog_entries_for_searched_cards_in_card_order()?;
        let model_version = model_version_for_params(params);
        let include_same_day_reviews =
            include_same_day_training_entries(model_version, include_same_day_reviews);
        let items = fsrs_items_for_training(
            revlogs,
            timing.next_day_at,
            ignore_revlogs_before,
            include_same_day_reviews,
        );
        let items = if include_same_day_reviews {
            items
        } else {
            items.filter_non_same_day_evaluation_targets()
        };
        let target_counts = items.target_counts();
        anki_progress.state.reviews = target_counts.total_targets as u32;
        anki_progress.state.long_term_reviews = target_counts.long_term_targets as u32;
        anki_progress.state.short_term_reviews = target_counts.short_term_targets as u32;
        let fsrs = FSRS::new(params)?;
        Ok(fsrs.evaluate(items.items, |ip| {
            anki_progress
                .update(false, |p| {
                    p.total_iterations = ip.total as u32;
                    p.current_iteration = ip.current as u32;
                })
                .is_ok()
        })?)
    }
}

fn coerce_computed_params_to_selected_version(
    model_version: ComputeParametersVersion,
    current_params: &[f32],
    computed_params: Vec<f32>,
) -> Vec<f32> {
    if current_params.is_empty() || current_params.len() == computed_params.len() {
        return computed_params;
    }

    let expected_len = match model_version {
        ComputeParametersVersion::Fsrs7 => 35,
        ComputeParametersVersion::Fsrs6 => 21,
    };

    if computed_params.len() == expected_len {
        return computed_params;
    }

    if current_params.len() == expected_len {
        current_params.to_vec()
    } else {
        computed_params
    }
}

#[derive(Default, Clone, Copy, Debug)]
pub struct ComputeParamsProgress {
    pub current_iteration: u32,
    pub total_iterations: u32,
    /// Total training targets used by optimizer (long-term + same-day)
    pub reviews: u32,
    /// Targets where delta_t >= 1 day
    pub long_term_reviews: u32,
    /// Targets where delta_t < 1 day
    pub short_term_reviews: u32,
    /// Only used in 'compute all params' case
    pub current_preset: u32,
    /// Only used in 'compute all params' case
    pub total_presets: u32,
    pub phase: ComputeParamsProgressPhase,
}

#[derive(Default, Clone, Debug)]
pub struct ComputeAllParamsProgress {
    pub current_iteration: u32,
    pub total_iterations: u32,
    pub presets: Vec<ComputeAllParamsPresetProgress>,
}

#[derive(Default, Clone, Debug)]
pub struct ComputeAllParamsPresetProgress {
    pub name: String,
    pub current_iteration: u32,
    pub total_iterations: u32,
    pub reviews: u32,
    pub long_term_reviews: u32,
    pub short_term_reviews: u32,
    pub finished: bool,
    pub skipped: bool,
    pub phase: ComputeParamsProgressPhase,
}

#[derive(Default, Clone, Copy, Debug)]
pub(crate) struct TrainingTargetCounts {
    pub total_targets: usize,
    pub long_term_targets: usize,
    pub short_term_targets: usize,
}

/// Convert a series of revlog entries sorted by card id into FSRS items.
fn fsrs_items_for_training(
    revlogs: Vec<RevlogEntry>,
    next_day_at: TimestampSecs,
    review_revlogs_before: TimestampMillis,
    include_same_day: bool,
) -> TrainingItemsForFsrs {
    let mut revlogs = revlogs
        .into_iter()
        .chunk_by(|r| r.cid)
        .into_iter()
        .filter_map(|(cid, entries)| {
            reviews_for_fsrs(
                entries.collect(),
                next_day_at,
                true,
                review_revlogs_before,
                include_same_day,
            )
            .map(|reviews| {
                reviews
                    .fsrs_items
                    .into_iter()
                    .map(move |(revlog_id, item)| (revlog_id, cid, item))
            })
        })
        .flatten()
        .collect_vec();
    // Sort by RevlogId
    revlogs.sort_by_key(|(revlog_id, _, _)| revlog_id.0);
    let (card_ids, items) = revlogs
        .into_iter()
        .map(|(_, card_id, item)| (card_id.0, item))
        .unzip();
    TrainingItemsForFsrs::with_card_ids(items, card_ids)
}

pub(crate) struct ReviewsForFsrs {
    /// The revlog entries that remain after filtering (e.g. excluding
    /// review entries prior to a card being reset).
    pub filtered_revlogs: Vec<RevlogEntry>,
    /// FSRS items derived from the filtered revlogs.
    pub fsrs_items: Vec<(RevlogId, FSRSItem)>,
    /// True if there is enough history to derive memory state from history
    /// alone. If false, memory state will be derived from SM2.
    pub revlogs_complete: bool,
}

/// Filter out unwanted revlog entries, then create a series of FSRS items for
/// training/memory state calculation.
///
/// Filtering consists of removing revlog entries before the supplied timestamp,
/// and removing items such as reviews that happened prior to a card being reset
/// to new.
pub(crate) fn reviews_for_fsrs(
    mut entries: Vec<RevlogEntry>,
    next_day_at: TimestampSecs,
    training: bool,
    ignore_revlogs_before: TimestampMillis,
    include_same_day_training_entries: bool,
) -> Option<ReviewsForFsrs> {
    let mut first_of_last_learn_entries = None;
    let mut first_user_grade_idx = None;
    let mut revlogs_complete = false;
    // Working backwards from the latest review...
    for (index, entry) in entries.iter().enumerate().rev() {
        if entry.is_cramming() {
            continue;
        }
        // For incomplete review histories, initial memory state is based on the first
        // user-graded review after the cutoff date with interval >= 1d.
        let within_cutoff = entry.id.0 > ignore_revlogs_before.0;
        let user_graded = entry.has_rating();
        let interday = entry.interval >= 1 || entry.interval <= -86400;
        if user_graded && within_cutoff && interday {
            first_user_grade_idx = Some(index);
        }

        if user_graded && entry.review_kind == RevlogReviewKind::Learning {
            first_of_last_learn_entries = Some(index);
            revlogs_complete = true;
        } else if entry.is_reset() {
            // Ignore entries prior to a `Reset` if a learning step has come after,
            // but consider revlogs complete.
            if first_of_last_learn_entries.is_some() {
                revlogs_complete = true;
                break;
            // Ignore entries prior to a `Reset` if the user has graded a card
            // after the reset.
            } else if first_user_grade_idx.is_some() {
                revlogs_complete = false;
                break;
            // User has not graded the card since it was reset, so all history
            // filtered out.
            } else {
                return None;
            }
        // Previous versions of Anki didn't add a revlog entry when the card was
        // reset.
        } else if first_of_last_learn_entries.is_some() {
            break;
        }
    }
    if training {
        // While training, ignore the entire card if the first learning step of the last
        // group of learning steps is before the ignore_revlogs_before date
        if let Some(idx) = first_of_last_learn_entries {
            if entries[idx].id.0 < ignore_revlogs_before.0 {
                return None;
            }
        }
    } else {
        // While reviewing, if the first learning step is before the ignore date,
        // we ignore it, and will fall back on SM2 info and the last user grade below.
        if let Some(idx) = first_of_last_learn_entries {
            if entries[idx].id.0 < ignore_revlogs_before.0 && idx < entries.len() - 1 {
                revlogs_complete = false;
                first_of_last_learn_entries = None;
            }
        }
    }
    if let Some(idx) = first_of_last_learn_entries {
        // start from the learning step
        if idx > 0 {
            entries.drain(..idx);
        }
    } else if training {
        // when training, we ignore cards that don't have any learning steps
        return None;
    } else if let Some(idx) = first_user_grade_idx {
        // if there are no learning entries, but the user has reviewed the card,
        // we ignore all entries before the first grade
        if idx > 0 {
            entries.drain(..idx);
        }
    } else {
        // if no valid user grades were found, ignore the card.
        return None;
    }

    // Filter out unwanted entries
    entries.retain(|entry| entry.has_rating_and_affects_scheduling());

    // Compute delta_t for each entry
    let delta_ts = iter::once(0.0f32)
        .chain(entries.iter().tuple_windows().map(|(previous, current)| {
            let elapsed_days =
                previous.days_elapsed(next_day_at) - current.days_elapsed(next_day_at);
            if include_same_day_training_entries {
                // FSRS-7 accepts fractional elapsed days; use revlog timestamps directly.
                let elapsed_millis = current.id.0.saturating_sub(previous.id.0).max(1) as f32;
                elapsed_millis / 86_400_000.0
            } else {
                elapsed_days as f32
            }
        }))
        .collect_vec();

    let items = if training {
        // Convert the remaining entries into separate FSRSItems, where each item
        // contains all reviews done until then.
        let mut items = Vec::with_capacity(entries.len());
        let mut current_reviews = Vec::with_capacity(entries.len());
        for (idx, (entry, &delta_t)) in entries.iter().zip(delta_ts.iter()).enumerate() {
            current_reviews.push(FSRSReview {
                rating: entry.button_chosen as u32,
                delta_t,
            });
            let keep_for_training = delta_t > 0.0 || include_same_day_training_entries;
            if idx >= 1 && keep_for_training {
                items.push((
                    entry.id,
                    FSRSItem {
                        reviews: current_reviews.clone(),
                    },
                ));
            }
        }
        items
    } else {
        // When not training, we only need the final FSRS item, which represents
        // the complete history of the card. This avoids expensive clones in a loop.
        let reviews = entries
            .iter()
            .zip(delta_ts.iter())
            .map(|(entry, &delta_t)| FSRSReview {
                rating: entry.button_chosen as u32,
                delta_t,
            })
            .collect();
        let last_entry = entries.last().unwrap();

        vec![(last_entry.id, FSRSItem { reviews })]
    };

    if items.is_empty() {
        None
    } else {
        Some(ReviewsForFsrs {
            fsrs_items: items,
            revlogs_complete,
            filtered_revlogs: entries,
        })
    }
}

impl RevlogEntry {
    fn days_elapsed(&self, next_day_at: TimestampSecs) -> u32 {
        (next_day_at.elapsed_secs_since(self.id.as_secs()) / 86_400).max(0) as u32
    }
}

fn revlog_entry_to_proto(e: RevlogEntry) -> anki_proto::stats::RevlogEntry {
    anki_proto::stats::RevlogEntry {
        id: e.id.0,
        cid: e.cid.0,
        usn: 0,
        button_chosen: e.button_chosen as u32,
        interval: e.interval,
        last_interval: e.last_interval,
        ease_factor: e.ease_factor,
        taken_millis: e.taken_millis,
        review_kind: match e.review_kind {
            RevlogReviewKind::Learning => revlog_entry::ReviewKind::Learning,
            RevlogReviewKind::Review => revlog_entry::ReviewKind::Review,
            RevlogReviewKind::Relearning => revlog_entry::ReviewKind::Relearning,
            RevlogReviewKind::Filtered => revlog_entry::ReviewKind::Filtered,
            RevlogReviewKind::Manual => revlog_entry::ReviewKind::Manual,
            RevlogReviewKind::Rescheduled => revlog_entry::ReviewKind::Rescheduled,
        } as i32,
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    const NEXT_DAY_AT: TimestampSecs = TimestampSecs(86400 * 1000);

    fn days_ago_ms(days_ago: i64) -> TimestampMillis {
        ((NEXT_DAY_AT.0 - days_ago * 86400) * 1000).into()
    }

    pub(crate) fn revlog(review_kind: RevlogReviewKind, days_ago: i64) -> RevlogEntry {
        let button_chosen = match review_kind {
            RevlogReviewKind::Manual | RevlogReviewKind::Rescheduled => 0,
            _ => 3,
        };
        RevlogEntry {
            review_kind,
            id: days_ago_ms(days_ago).into(),
            button_chosen,
            interval: 1,
            ..Default::default()
        }
    }

    pub(crate) fn review(delta_t: u32) -> FSRSReview {
        FSRSReview {
            rating: 3,
            delta_t: delta_t as f32,
        }
    }

    fn review_f(delta_t: f32) -> FSRSReview {
        FSRSReview { rating: 3, delta_t }
    }

    pub(crate) fn convert_ignore_before(
        revlog: &[RevlogEntry],
        training: bool,
        ignore_before: TimestampMillis,
    ) -> Option<Vec<FSRSItem>> {
        reviews_for_fsrs(revlog.to_vec(), NEXT_DAY_AT, training, ignore_before, false)
            .map(|i| i.fsrs_items.into_iter().map(|(_, item)| item).collect_vec())
    }

    fn convert_with_model(
        revlog: &[RevlogEntry],
        training: bool,
        model_version: ComputeParametersVersion,
    ) -> Option<Vec<FSRSItem>> {
        reviews_for_fsrs(
            revlog.to_vec(),
            NEXT_DAY_AT,
            training,
            0.into(),
            include_same_day_training_entries(model_version, None),
        )
        .map(|i| i.fsrs_items.into_iter().map(|(_, item)| item).collect_vec())
    }

    fn convert_with_model_and_override(
        revlog: &[RevlogEntry],
        training: bool,
        model_version: ComputeParametersVersion,
        include_same_day_reviews: Option<bool>,
    ) -> Option<Vec<FSRSItem>> {
        reviews_for_fsrs(
            revlog.to_vec(),
            NEXT_DAY_AT,
            training,
            0.into(),
            include_same_day_training_entries(model_version, include_same_day_reviews),
        )
        .map(|i| i.fsrs_items.into_iter().map(|(_, item)| item).collect_vec())
    }

    pub(crate) fn convert(revlog: &[RevlogEntry], training: bool) -> Option<Vec<FSRSItem>> {
        convert_ignore_before(revlog, training, 0.into())
    }

    #[test]
    fn compute_params_from_prepared_returns_current_params_without_items() -> Result<()> {
        let current_params = fsrs::DEFAULT_PARAMETERS.to_vec();
        let output = compute_params_from_prepared(
            PreparedComputeParams {
                current_params: current_params.clone(),
                num_of_relearning_steps: 1,
                model_version: ComputeParametersVersion::Fsrs7,
                include_same_day_reviews: true,
                dynamic_desired_retention_enabled: false,
                simulator_config: Default::default(),
                items: vec![],
                item_card_ids: vec![],
                target_counts: TrainingTargetCounts::default(),
            },
            None,
            None,
            false,
        )?;

        assert_eq!(output.params, current_params);
        assert_eq!(output.fsrs_items, 0);
        assert_eq!(output.health_check_passed, None);
        Ok(())
    }

    #[test]
    fn dynamic_desired_retention_calibration_keeps_weights_and_avg_drs() -> Result<()> {
        let calibration = dynamic_desired_retention_calibration_from_parts(
            vec![1.0; 15],
            [(0.0, Some(0.9)), (16.0, None), (64.0, Some(0.8))],
            [(0.0, 0.91), (64.0, 0.81)],
            DynamicDesiredRetentionBounds {
                retention_min: 0.75,
                retention_max: 0.95,
            },
        )?;

        assert_eq!(calibration.params, vec![1.0; 15]);
        assert_eq!(calibration.weights, vec![0.0, 64.0]);
        assert_eq!(calibration.avg_drs, vec![0.9, 0.8]);
        assert_eq!(calibration.fsrs_eq_weights, vec![0.0, 64.0]);
        assert_eq!(calibration.fsrs_eq_drs, vec![0.91, 0.81]);
        assert_eq!(calibration.retention_min, 0.75);
        assert_eq!(calibration.retention_max, 0.95);
        Ok(())
    }

    #[test]
    fn dynamic_desired_retention_uses_default_bounds() -> Result<()> {
        let bounds = default_dynamic_desired_retention_bounds();

        assert_eq!(bounds.retention_min, DEFAULT_RETENTION_MIN);
        assert_eq!(bounds.retention_max, DEFAULT_RETENTION_MAX);
        Ok(())
    }

    #[test]
    fn dynamic_desired_retention_simulator_uses_selected_slice_shape() {
        let mut card_one_learning = revlog(RevlogReviewKind::Learning, 10);
        card_one_learning.cid = CardId(1);
        let mut card_one_review = revlog(RevlogReviewKind::Review, 9);
        card_one_review.cid = CardId(1);
        let mut card_two_learning = revlog(RevlogReviewKind::Learning, 4);
        card_two_learning.cid = CardId(2);
        let mut card_two_review = revlog(RevlogReviewKind::Review, 2);
        card_two_review.cid = CardId(2);
        let mut config = SimulatorConfig::default();

        shape_simulator_config_for_dynamic_desired_retention(
            &mut config,
            &[
                card_one_learning,
                card_one_review,
                card_two_learning,
                card_two_review,
            ],
            NEXT_DAY_AT.0,
            DynamicDesiredRetentionSimulatorOptions::default(),
        )
        .unwrap();

        assert_eq!(config.deck_size, 2);
        assert_eq!(config.learn_span, 2);
        assert_eq!(config.learn_limit, 1);
        assert_eq!(config.review_limit, DYNAMIC_DR_DEFAULT_REVIEW_LIMIT);
        assert_eq!(
            config.max_cost_perday,
            DYNAMIC_DR_DEFAULT_MAX_COST_PERDAY_MINUTES * 60.0
        );
    }

    #[test]
    fn dynamic_desired_retention_simulator_uses_limit_overrides() {
        let mut card_learning = revlog(RevlogReviewKind::Learning, 10);
        card_learning.cid = CardId(1);
        let mut config = SimulatorConfig::default();

        shape_simulator_config_for_dynamic_desired_retention(
            &mut config,
            &[card_learning],
            NEXT_DAY_AT.0,
            DynamicDesiredRetentionSimulatorOptions {
                review_limit: Some(123),
                max_cost_perday_minutes: Some(45.0),
            },
        )
        .unwrap();

        assert_eq!(config.review_limit, 123);
        assert_eq!(config.max_cost_perday, 45.0 * 60.0);
    }

    #[macro_export]
    macro_rules! fsrs_items {
        ($($reviews:expr),*) => {
            Some(vec![
                $(
                    FSRSItem {
                        reviews: $reviews.to_vec()
                    }
                ),*
            ])
        };
    }

    #[test]
    fn delta_t_is_correct() -> Result<()> {
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 1),
                    revlog(RevlogReviewKind::Review, 0)
                ],
                true,
            ),
            fsrs_items!([review(0), review(1)])
        );
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 15),
                    revlog(RevlogReviewKind::Learning, 13),
                    revlog(RevlogReviewKind::Review, 10),
                    revlog(RevlogReviewKind::Review, 5)
                ],
                true,
            ),
            fsrs_items!(
                [review(0), review(2)],
                [review(0), review(2), review(3)],
                [review(0), review(2), review(3), review(5)]
            )
        );
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 15),
                    revlog(RevlogReviewKind::Learning, 13),
                ],
                true,
            ),
            fsrs_items!([review(0), review(2),])
        );
        Ok(())
    }

    #[test]
    fn cram_is_filtered() {
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 10),
                    revlog(RevlogReviewKind::Review, 9),
                    revlog(RevlogReviewKind::Filtered, 7),
                    revlog(RevlogReviewKind::Review, 4),
                ],
                true,
            ),
            fsrs_items!([review(0), review(1)], [review(0), review(1), review(5)])
        );
    }

    #[test]
    fn set_due_date_is_filtered() {
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 10),
                    revlog(RevlogReviewKind::Review, 9),
                    RevlogEntry {
                        ease_factor: 100,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                    revlog(RevlogReviewKind::Review, 4),
                ],
                true,
            ),
            fsrs_items!([review(0), review(1)], [review(0), review(1), review(5)])
        );
    }

    #[test]
    fn card_reset_drops_all_previous_history() {
        // If Reset comes in between two Learn entries, only the ones after the Reset
        // are used.
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 10),
                    RevlogEntry {
                        ease_factor: 0,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                    revlog(RevlogReviewKind::Learning, 4),
                    revlog(RevlogReviewKind::Review, 0),
                ],
                true,
            ),
            fsrs_items!([review(0), review(4)])
        );
        // Return None if Reset is the last entry or is followed by only manual entries.
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 10),
                    revlog(RevlogReviewKind::Review, 9),
                    RevlogEntry {
                        ease_factor: 0,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                    RevlogEntry {
                        ease_factor: 100,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                ],
                false,
            ),
            None,
        );
        // If non-learning user-graded entries are found after Reset, return None during
        // training but return the remaining entries during memory state calculation.
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 10),
                    revlog(RevlogReviewKind::Review, 9),
                    RevlogEntry {
                        ease_factor: 0,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                    revlog(RevlogReviewKind::Review, 1),
                    revlog(RevlogReviewKind::Relearning, 0),
                ],
                true,
            ),
            None,
        );
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Review, 9),
                    RevlogEntry {
                        ease_factor: 0,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                    revlog(RevlogReviewKind::Review, 1),
                    revlog(RevlogReviewKind::Relearning, 0),
                ],
                false,
            ),
            fsrs_items!([review(0), review(1)])
        );
    }

    #[test]
    fn coerce_computed_params_prefers_computed_when_matches_selected_family() {
        let current = vec![1.0; 21];
        let computed = vec![2.0; 35];
        assert_eq!(
            coerce_computed_params_to_selected_version(
                ComputeParametersVersion::Fsrs7,
                &current,
                computed.clone()
            ),
            computed
        );
    }

    #[test]
    fn coerce_computed_params_keeps_computed_when_lengths_match() {
        let current = vec![1.0; 21];
        let computed = vec![2.0; 21];
        assert_eq!(
            coerce_computed_params_to_selected_version(
                ComputeParametersVersion::Fsrs6,
                &current,
                computed.clone()
            ),
            computed
        );
    }

    #[test]
    fn coerce_computed_params_falls_back_to_current_when_computed_invalid_for_selected_family() {
        let current = vec![1.0; 35];
        let computed = vec![2.0; 21];
        assert_eq!(
            coerce_computed_params_to_selected_version(
                ComputeParametersVersion::Fsrs7,
                &current,
                computed
            ),
            current
        );
    }

    #[test]
    fn single_learning_step_skipped_when_training() {
        assert_eq!(
            convert(&[revlog(RevlogReviewKind::Learning, 1),], true),
            None,
        );
        assert_eq!(
            convert(&[revlog(RevlogReviewKind::Learning, 1),], false),
            fsrs_items!([review(0)])
        );
    }

    #[test]
    fn fsrs7_training_includes_same_day_only_targets() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 1),
            revlog(RevlogReviewKind::Review, 1),
            revlog(RevlogReviewKind::Review, 1),
        ];
        assert_eq!(
            convert_with_model(revlogs, true, ComputeParametersVersion::Fsrs6),
            None
        );
        assert_eq!(
            convert_with_model(revlogs, true, ComputeParametersVersion::Fsrs7),
            Some(vec![
                FSRSItem {
                    reviews: vec![review(0), review_f(1.0 / 86_400_000.0)],
                },
                FSRSItem {
                    reviews: vec![
                        review(0),
                        review_f(1.0 / 86_400_000.0),
                        review_f(1.0 / 86_400_000.0),
                    ],
                },
            ])
        );
    }

    #[test]
    fn fsrs7_training_can_ignore_same_day_targets_with_override() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 1),
            revlog(RevlogReviewKind::Review, 1),
            revlog(RevlogReviewKind::Review, 1),
        ];
        assert_eq!(
            convert_with_model_and_override(
                revlogs,
                true,
                ComputeParametersVersion::Fsrs7,
                Some(false),
            ),
            None
        );
    }

    #[test]
    fn fsrs7_training_toggle_true_path_is_unchanged() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 1),
            revlog(RevlogReviewKind::Review, 1),
            revlog(RevlogReviewKind::Review, 1),
        ];
        assert_eq!(
            convert_with_model_and_override(
                revlogs,
                true,
                ComputeParametersVersion::Fsrs7,
                Some(true),
            ),
            convert_with_model(revlogs, true, ComputeParametersVersion::Fsrs7)
        );
    }

    #[test]
    fn filtered_empty_dataset_returns_not_enough_data() {
        let filtered = filter_non_same_day_evaluation_targets(vec![FSRSItem {
            reviews: vec![review(0), review_f(0.5)],
        }]);
        assert!(filtered.is_empty());
        let err = evaluate_with_time_series_splits(
            ComputeParametersInput {
                train_set: filtered,
                card_ids: None,
                progress: None,
                enable_short_term: true,
                enable_sched_penalties: true,
                model_version: ComputeParametersVersion::Fsrs7,
                num_relearning_steps: Some(1),
            },
            |_| true,
        )
        .unwrap_err();
        assert!(matches!(err, fsrs::FSRSError::NotEnoughData));
    }

    #[test]
    fn health_check_adjustment_uses_filtered_target_counts() {
        let mut items = vec![];
        for _ in 0..19 {
            items.push(FSRSItem {
                reviews: vec![review(0), review(2)],
            });
        }
        items.push(FSRSItem {
            reviews: vec![
                review(0),
                FSRSReview {
                    rating: 1,
                    delta_t: 2.0,
                },
            ],
        });
        for _ in 0..4 {
            items.push(FSRSItem {
                reviews: vec![
                    review(0),
                    FSRSReview {
                        rating: 1,
                        delta_t: 0.5,
                    },
                ],
            });
        }

        let filtered = filter_non_same_day_evaluation_targets(items.clone());
        assert_eq!(filtered.len(), 20);
        let eval = ModelEvaluation {
            log_loss: 0.3,
            rmse_bins: 1.0,
        };
        assert!(health_check_passed_for_evaluated_targets(eval, &items));
        assert!(!health_check_passed_for_evaluated_targets(eval, &filtered));
    }

    #[test]
    fn fsrs7_training_keeps_same_day_targets_after_long_term_review() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 3),
            revlog(RevlogReviewKind::Review, 2),
            revlog(RevlogReviewKind::Review, 2),
        ];
        assert_eq!(
            convert_with_model(revlogs, true, ComputeParametersVersion::Fsrs6),
            fsrs_items!([review(0), review(1)])
        );
        assert_eq!(
            convert_with_model(revlogs, true, ComputeParametersVersion::Fsrs7),
            Some(vec![
                FSRSItem {
                    reviews: vec![review(0), review(1)],
                },
                FSRSItem {
                    reviews: vec![review(0), review(1), review_f(1.0 / 86_400_000.0)],
                },
            ])
        );
    }

    #[test]
    fn fsrs_items_for_training_keeps_items_without_long_term_review_for_fsrs7() {
        let revlogs = vec![
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Learning, 1)
            },
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Review, 1)
            },
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Review, 1)
            },
        ];
        let items = fsrs_items_for_training(
            revlogs,
            NEXT_DAY_AT,
            TimestampMillis(0),
            include_same_day_training_entries(ComputeParametersVersion::Fsrs7, None),
        );
        assert_eq!(
            items.items,
            vec![
                FSRSItem {
                    reviews: vec![review(0), review_f(1.0 / 86_400_000.0)],
                },
                FSRSItem {
                    reviews: vec![
                        review(0),
                        review_f(1.0 / 86_400_000.0),
                        review_f(1.0 / 86_400_000.0),
                    ],
                },
            ]
        );
        assert_eq!(items.card_ids, Some(vec![1, 1]));
    }

    #[test]
    fn fsrs_items_for_training_reports_long_and_same_day_target_counts() {
        let revlogs = vec![
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Learning, 3)
            },
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Review, 2)
            },
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Review, 2)
            },
        ];
        let fsrs6_items = fsrs_items_for_training(
            revlogs.clone(),
            NEXT_DAY_AT,
            TimestampMillis(0),
            include_same_day_training_entries(ComputeParametersVersion::Fsrs6, None),
        );
        let fsrs6_counts = fsrs6_items.target_counts();
        assert_eq!(fsrs6_counts.total_targets, 1);
        assert_eq!(fsrs6_counts.long_term_targets, 1);
        assert_eq!(fsrs6_counts.short_term_targets, 0);

        let fsrs7_items = fsrs_items_for_training(
            revlogs,
            NEXT_DAY_AT,
            TimestampMillis(0),
            include_same_day_training_entries(ComputeParametersVersion::Fsrs7, None),
        );
        let fsrs7_counts = fsrs7_items.target_counts();
        assert_eq!(fsrs7_counts.total_targets, 2);
        assert_eq!(fsrs7_counts.long_term_targets, 1);
        assert_eq!(fsrs7_counts.short_term_targets, 1);
    }

    #[test]
    fn fsrs7_same_day_delta_uses_fractional_elapsed_time() {
        let base = days_ago_ms(1).0 + 3_600_000;
        let revlogs = vec![
            RevlogEntry {
                id: RevlogId(base),
                ..revlog(RevlogReviewKind::Learning, 1)
            },
            RevlogEntry {
                id: RevlogId(base + 3_600_000),
                ..revlog(RevlogReviewKind::Review, 1)
            },
        ];
        let converted =
            convert_with_model(&revlogs, true, ComputeParametersVersion::Fsrs7).unwrap();
        let delta = converted[0].reviews[1].delta_t;
        assert!(delta > 0.0);
        assert!((delta - (1.0 / 24.0)).abs() < 1e-6);
    }

    #[test]
    fn fsrs7_same_day_delta_is_positive_when_timestamps_equal() {
        let base = days_ago_ms(1).0;
        let revlogs = vec![
            RevlogEntry {
                id: RevlogId(base),
                ..revlog(RevlogReviewKind::Learning, 1)
            },
            RevlogEntry {
                id: RevlogId(base),
                ..revlog(RevlogReviewKind::Review, 1)
            },
        ];
        let converted =
            convert_with_model(&revlogs, true, ComputeParametersVersion::Fsrs7).unwrap();
        assert!(converted[0].reviews[1].delta_t > 0.0);
    }

    #[test]
    fn fsrs7_interday_delta_uses_fractional_elapsed_time() {
        let revlogs = vec![
            RevlogEntry {
                id: RevlogId(days_ago_ms(3).0),
                ..revlog(RevlogReviewKind::Learning, 3)
            },
            RevlogEntry {
                // 0.5 day after the D-1 boundary -> elapsed days differs from elapsed timestamp.
                id: RevlogId(days_ago_ms(1).0 + 43_200_000),
                ..revlog(RevlogReviewKind::Review, 1)
            },
        ];
        let converted6 =
            convert_with_model(&revlogs, true, ComputeParametersVersion::Fsrs6).unwrap();
        let converted7 =
            convert_with_model(&revlogs, true, ComputeParametersVersion::Fsrs7).unwrap();
        assert_eq!(converted6[0].reviews[1].delta_t, 3.0);
        assert!((converted7[0].reviews[1].delta_t - 2.5).abs() < 1e-6);
    }

    #[test]
    fn resolved_model_version_prefers_override() {
        assert_eq!(
            super::resolved_model_version(&[0.0; 21], Some(ComputeParametersVersion::Fsrs7)),
            ComputeParametersVersion::Fsrs7
        );
        assert_eq!(
            super::resolved_model_version(&[0.0; 35], Some(ComputeParametersVersion::Fsrs6)),
            ComputeParametersVersion::Fsrs6
        );
    }

    #[test]
    fn resolved_model_version_falls_back_to_param_length() {
        assert_eq!(
            super::resolved_model_version(&[0.0; 35], None),
            ComputeParametersVersion::Fsrs7
        );
        assert_eq!(
            super::resolved_model_version(&[0.0; 21], None),
            ComputeParametersVersion::Fsrs6
        );
    }

    #[test]
    fn ignores_cards_before_ignore_before_date_when_training() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 10),
            revlog(RevlogReviewKind::Learning, 8),
        ];
        // | = Ignore before
        // L = learning step
        // L L |
        assert_eq!(convert_ignore_before(revlogs, true, days_ago_ms(7)), None);
        // L | L
        assert_eq!(convert_ignore_before(revlogs, true, days_ago_ms(9)), None);
        // L (|L) (exact same millisecond)
        assert_eq!(
            convert_ignore_before(revlogs, true, days_ago_ms(10)),
            convert(revlogs, true)
        );
        // | L L
        assert_eq!(
            convert_ignore_before(revlogs, true, days_ago_ms(11)),
            convert(revlogs, true)
        );
    }

    #[test]
    fn partially_ignored_learning_steps_terminate_training() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 10),
            revlog(RevlogReviewKind::Learning, 8),
            revlog(RevlogReviewKind::Review, 6),
        ];
        // | = Ignore before
        // L = learning step
        // L | L R
        assert_eq!(convert_ignore_before(revlogs, true, days_ago_ms(9)), None);
    }

    #[test]
    fn skip_initial_relearning_steps() {
        let revlogs = &[
            revlog(RevlogReviewKind::Review, 10),
            RevlogEntry {
                button_chosen: 1, // Again
                interval: -600,
                ..revlog(RevlogReviewKind::Review, 8)
            },
            revlog(RevlogReviewKind::Relearning, 8),
            revlog(RevlogReviewKind::Review, 6),
        ];
        // | = Ignore before
        // A = Again
        // X = Relearning
        // R | A X R
        assert_eq!(
            convert_ignore_before(revlogs, false, days_ago_ms(9)),
            fsrs_items!([review(0), review(2)])
        );
    }

    #[test]
    fn ignore_before_date_between_learning_steps_when_reviewing() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 10),
            revlog(RevlogReviewKind::Learning, 8),
            revlog(RevlogReviewKind::Review, 2),
        ];
        // L | L R
        assert_ne!(
            convert_ignore_before(revlogs, false, days_ago_ms(9)),
            convert(revlogs, false)
        );
        assert_eq!(
            convert_ignore_before(revlogs, false, days_ago_ms(9))
                .unwrap()
                .last()
                .unwrap()
                .reviews
                .len(),
            2
        );
        // | L L R
        assert_eq!(
            convert_ignore_before(revlogs, false, days_ago_ms(11)),
            convert(revlogs, false)
        );
    }

    #[test]
    fn handle_ignore_before_when_no_learning_steps() {
        let revlogs = &[
            revlog(RevlogReviewKind::Review, 10),
            revlog(RevlogReviewKind::Review, 8),
            revlog(RevlogReviewKind::Review, 6),
        ];
        // R | R R
        assert_eq!(
            convert_ignore_before(revlogs, false, days_ago_ms(9))
                .unwrap()
                .last()
                .unwrap()
                .reviews
                .len(),
            2
        );
    }

    #[test]
    fn ignore_before_after_last_revlog_entry() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 10),
            revlog(RevlogReviewKind::Review, 6),
        ];
        // L R |
        assert_eq!(convert_ignore_before(revlogs, false, days_ago_ms(4)), None);
    }

    #[test]
    fn training_search_uses_override_when_non_empty() {
        assert_eq!(
            super::training_search("deck:train", Some("deck:eval")),
            "deck:eval"
        );
        assert_eq!(
            super::training_search("deck:train", Some("   deck:eval2  ")),
            "deck:eval2"
        );
    }

    #[test]
    fn training_search_falls_back_to_search_when_empty() {
        assert_eq!(super::training_search("deck:train", None), "deck:train");
        assert_eq!(
            super::training_search("deck:train", Some("   ")),
            "deck:train"
        );
    }

    #[test]
    fn external_evaluation_is_enabled_only_for_different_searches() {
        assert!(super::uses_external_evaluation(
            "preset:vocabulary rated:1",
            "preset:vocabulary"
        ));
        assert!(!super::uses_external_evaluation(
            "preset:vocabulary",
            "preset:vocabulary"
        ));
    }

    #[test]
    fn external_target_evaluation_rejects_empty_sets() {
        let err = super::evaluate_from_training_to_external_targets(
            ComputeParametersInput {
                train_set: vec![],
                card_ids: None,
                progress: None,
                enable_short_term: true,
                enable_sched_penalties: true,
                model_version: ComputeParametersVersion::Fsrs6,
                num_relearning_steps: Some(1),
            },
            vec![FSRSItem {
                reviews: vec![review(0), review(2)],
            }],
        )
        .unwrap_err();
        assert!(matches!(
            err,
            AnkiError::FsrsInsufficientData | AnkiError::FsrsInsufficientReviews { .. }
        ));
    }

    #[test]
    fn external_target_evaluation_rejects_empty_evaluation_set() {
        let err = super::evaluate_from_training_to_external_targets(
            ComputeParametersInput {
                train_set: vec![FSRSItem {
                    reviews: vec![review(0), review(2)],
                }],
                card_ids: None,
                progress: None,
                enable_short_term: true,
                enable_sched_penalties: true,
                model_version: ComputeParametersVersion::Fsrs6,
                num_relearning_steps: Some(1),
            },
            vec![],
        )
        .unwrap_err();
        assert!(matches!(
            err,
            AnkiError::FsrsInsufficientData | AnkiError::FsrsInsufficientReviews { .. }
        ));
    }
}
