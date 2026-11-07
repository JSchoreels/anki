// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use anki_proto::deck_config::deck_config::config::ReviewCardOrder;

#[derive(Debug, Clone)]
pub struct ReviewInput {
    pub card_id: i64,
    pub note_id: Option<i64>,
    pub deck_id: Option<i64>,
    pub preset_id: Option<i64>,
    pub is_query: bool,
    pub ease: Option<u8>,
    pub duration_millis: Option<i64>,
    /// Legacy field name: answered inputs contain the training dataset state.
    pub card_type: Option<i64>,
    pub day_offset: Option<i64>,
    pub current_elapsed_days: Option<i64>,
    pub current_elapsed_seconds: Option<i64>,
    pub target_retentions: [Option<f32>; 4],
    pub enforce_grade_order: bool,
}

pub struct ReviewState<'a> {
    pub card: Option<&'a [u8]>,
    pub deck: Option<&'a [u8]>,
    pub note: Option<&'a [u8]>,
    pub preset: Option<&'a [u8]>,
    pub global: Option<&'a [u8]>,
}

pub struct ReviewOutput {
    pub retrievability: f32,
    pub curve_retrievability: Option<f32>,
    pub button_probabilities: [f32; 4],
    pub current_interval: Option<u32>,
    pub current_s90: Option<f32>,
    pub intervals: [Option<f32>; 4],
    pub s90s: [Option<f32>; 4],
    pub card_state: Vec<u8>,
    pub deck_state: Vec<u8>,
    pub note_state: Vec<u8>,
    pub preset_state: Vec<u8>,
    pub global_state: Vec<u8>,
}

pub struct ReviewPredictionRequest {
    pub input: ReviewInput,
    pub state: ReviewStateOwned,
}

pub struct ReviewStateOwned {
    pub card: Option<Vec<u8>>,
    pub deck: Option<Vec<u8>>,
    pub note: Option<Vec<u8>>,
    pub preset: Option<Vec<u8>>,
    pub global: Option<Vec<u8>>,
}

pub struct ReviewPredictionOutput {
    pub retrievability: f32,
    pub curve_retrievability: Option<f32>,
    pub button_probabilities: [f32; 4],
    pub current_interval: Option<u32>,
    pub current_s90: Option<f32>,
    pub intervals: [Option<f32>; 4],
    pub s90s: [Option<f32>; 4],
}

pub struct RwkvWarmUpSnapshot {
    pub card_states: Vec<(i64, Vec<u8>)>,
    pub note_states: Vec<(i64, Vec<u8>)>,
    pub deck_states: Vec<(i64, Vec<u8>)>,
    pub preset_states: Vec<(i64, Vec<u8>)>,
    pub global_state: Option<Vec<u8>>,
}

pub struct RwkvWorkloadSimulationSnapshot {
    pub card_states: Vec<(i64, Vec<u8>)>,
    pub note_states: Vec<(i64, Vec<u8>)>,
    pub deck_states: Vec<(i64, Vec<u8>)>,
    pub preset_states: Vec<(i64, Vec<u8>)>,
    pub global_state: Option<Vec<u8>>,
    pub runtime_state: Option<Vec<u8>>,
}

#[derive(Clone)]
pub struct RwkvWorkloadSimulationInput {
    pub review_input: ReviewInput,
    pub interval_days: Option<i64>,
    pub ease_factor: Option<i64>,
    pub reps: Option<i64>,
    pub lapses: Option<i64>,
}

pub struct RwkvWorkloadSimulationPoint {
    pub memorized: f32,
    pub weighted_memorized: f32,
    pub cost: f32,
    pub review_count: u32,
}

pub struct RwkvWorkloadSimulationOutput {
    pub reviewless_end_memorized: f32,
    pub reviewless_end_weighted_memorized: f32,
    pub points: Vec<(u32, RwkvWorkloadSimulationPoint)>,
}

pub struct RwkvWorkloadReviewModel {
    pub grade_seconds: [f32; 4],
    pub bucket_probabilities: Vec<(u32, [f32; 4])>,
}

pub struct RwkvWorkloadSimulationConfig {
    pub min_dr: u32,
    pub max_dr: u32,
    pub target_dr_step: u32,
    pub days_to_simulate: u32,
    pub review_limit: u32,
    pub new_limit: u32,
    pub new_cards_ignore_review_limit: bool,
    pub max_interval: u32,
    pub review_order: ReviewCardOrder,
    pub suspend_after_lapses: Option<u32>,
    pub state_update_interval: u32,
    pub review_model: RwkvWorkloadReviewModel,
}

#[derive(Clone)]
pub struct RwkvComparisonCard {
    pub input: RwkvWorkloadSimulationInput,
    pub due_day: i64,
    pub fsrs_memory: Option<(f32, f32, f32)>,
    pub fsrs_parameters: Vec<f32>,
    pub historical_retention: f32,
}

/// One recorded background answer, placed in a repeating 365-day pattern.
#[derive(Clone)]
pub struct RwkvComparisonBackgroundReview {
    pub input: ReviewInput,
    pub pattern_day: u32,
    pub second_of_day: u32,
    pub last_review_day: i64,
    pub last_review_second: i64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RwkvComparisonLearner {
    Fsrs,
    Rwkv,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RwkvPerformanceScenario {
    Steady,
    TemporaryDrop,
    PersistentDrop,
    TemporaryImprovement,
}

pub struct RwkvComparisonConfig {
    pub min_dr: u32,
    pub max_dr: u32,
    pub target_dr_step: u32,
    pub days_to_simulate: u32,
    pub review_limit: f32,
    pub new_limit: f32,
    pub new_cards_ignore_review_limit: bool,
    pub max_interval: u32,
    pub review_order: ReviewCardOrder,
    pub suspend_after_lapses: Option<u32>,
    pub learner: RwkvComparisonLearner,
    pub scenario: RwkvPerformanceScenario,
    pub repetitions: u32,
    pub output_scale: f32,
    pub review_model: RwkvWorkloadReviewModel,
    pub background: Vec<RwkvComparisonBackgroundReview>,
}

pub struct RwkvComparisonOutput {
    pub fsrs_scored_by_fsrs: RwkvWorkloadSimulationOutput,
    pub rwkv_scored_by_fsrs: RwkvWorkloadSimulationOutput,
    pub fsrs_scored_by_rwkv: RwkvWorkloadSimulationOutput,
    pub rwkv_scored_by_rwkv: RwkvWorkloadSimulationOutput,
    pub estimated_fsrs_states: u32,
}

/// Which scheduler sets the next interval of a forecast card. Grades are
/// always drawn from the RWKV curve model's recall probability.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RwkvForecastScheduler {
    /// RWKV-Curve (and RWKV-Instant) presets: curve-model intervals at the
    /// card's desired retention.
    Rwkv,
    /// FSRS presets: FSRS memory state and parameters at the card's desired
    /// retention.
    Fsrs,
    /// SM-2 presets: ease-factor intervals (no fuzz, no interval modifiers).
    Sm2,
}

#[derive(Clone)]
pub struct RwkvForecastCard {
    pub input: RwkvWorkloadSimulationInput,
    /// Days from today until the card is due; <= 0 means due today. `None`
    /// derives the due day from the curve, as the DR workload simulation does.
    pub due_day: Option<i64>,
    /// Limit group (usually the deck id); each group has its own limits.
    pub group: i64,
    pub desired_retention: f32,
    pub scheduler: RwkvForecastScheduler,
    pub fsrs_memory: Option<(f32, f32, f32)>,
    pub fsrs_parameters: Vec<f32>,
    pub historical_retention: f32,
}

#[derive(Clone, Debug)]
pub struct RwkvForecastGroup {
    pub group: i64,
    pub review_limit: u32,
    pub new_limit: u32,
    /// Already done today: day 0 only gets the rest of each limit.
    pub reviews_done_today: u32,
    pub new_done_today: u32,
}

pub struct RwkvWorkloadForecastConfig {
    pub days: u32,
    pub paths: u32,
    pub seed: u32,
    pub groups: Vec<RwkvForecastGroup>,
    /// Limits of groups missing from `groups`.
    pub default_review_limit: u32,
    pub default_new_limit: u32,
    pub new_cards_ignore_review_limit: bool,
    pub max_interval: u32,
    pub review_order: ReviewCardOrder,
    pub suspend_after_lapses: Option<u32>,
    pub state_update_interval: u32,
    pub review_model: RwkvWorkloadReviewModel,
}

/// Distribution of one daily quantity over the simulated paths.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct RwkvForecastBand {
    pub mean: f32,
    pub p1: f32,
    pub p5: f32,
    pub p50: f32,
    pub p95: f32,
    pub p99: f32,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct RwkvForecastDay {
    pub day: u32,
    /// Review cards due that day (before limits, including backlog).
    pub due: RwkvForecastBand,
    /// Review cards answered that day (after limits).
    pub done: RwkvForecastBand,
    /// New cards introduced that day.
    pub new: RwkvForecastBand,
    /// Review answers graded Again that day.
    pub lapses: RwkvForecastBand,
    /// Estimated answer time of all of that day's answers, in seconds.
    pub seconds: RwkvForecastBand,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct RwkvForecastGroupDays {
    pub group: i64,
    pub due_mean: Vec<f32>,
    pub done_mean: Vec<f32>,
    pub new_mean: Vec<f32>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct RwkvWorkloadForecastOutput {
    pub paths: u32,
    pub days: Vec<RwkvForecastDay>,
    pub groups: Vec<RwkvForecastGroupDays>,
    pub estimated_fsrs_states: u32,
}
