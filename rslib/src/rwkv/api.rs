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
}

pub struct RwkvComparisonOutput {
    pub fsrs: RwkvWorkloadSimulationOutput,
    pub rwkv: RwkvWorkloadSimulationOutput,
    pub estimated_fsrs_states: u32,
}
