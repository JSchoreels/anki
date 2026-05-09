// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use anki_proto::deck_config::deck_config::config::ReviewCardOrder;
use anki_proto::scheduler::SimulateFsrsReviewRequest;
use anki_proto::scheduler::SimulateFsrsReviewResponse;
use anki_proto::scheduler::SimulateFsrsWorkloadResponse;
use fsrs::simulate;
use fsrs::SimulatorConfig;
use fsrs::DEFAULT_PARAMETERS;
use fsrs::FSRS;
use itertools::Itertools;

use crate::card::CardQueue;
use crate::card::CardType;
use crate::card::FsrsMemoryState;
use crate::prelude::*;
use crate::scheduler::fsrs::memory_state::memory_state_from_sm2_with_params;
use crate::scheduler::fsrs::params::reviews_for_fsrs;
use crate::scheduler::states::fuzz::ReviewFuzzConfig;
use crate::scheduler::states::load_balancer::parse_easy_days_percentages;
use crate::search::SortMode;

fn create_review_priority_fn(
    review_order: ReviewCardOrder,
    _deck_size: usize,
) -> Option<fsrs::ReviewPriorityFn> {
    match review_order {
        ReviewCardOrder::Day | ReviewCardOrder::DayThenDeck | ReviewCardOrder::DeckThenDay => {
            Some(fsrs::ReviewPriorityFn::new(|card| {
                stable_card_hash(card.id)
            }))
        }
        ReviewCardOrder::IntervalsAscending => Some(fsrs::ReviewPriorityFn::new(|card| {
            scaled_i32(card.interval, 100.0)
        })),
        ReviewCardOrder::IntervalsDescending => Some(fsrs::ReviewPriorityFn::new(|card| {
            -scaled_i32(card.interval, 100.0)
        })),
        ReviewCardOrder::EaseAscending => Some(fsrs::ReviewPriorityFn::new(|card| {
            -scaled_i32(card.difficulty, 100.0)
        })),
        ReviewCardOrder::EaseDescending => Some(fsrs::ReviewPriorityFn::new(|card| {
            scaled_i32(card.difficulty, 100.0)
        })),
        ReviewCardOrder::RetrievabilityAscending => Some(fsrs::ReviewPriorityFn::new(|card| {
            scaled_i32(card.retrievability(), 1000.0)
        })),
        ReviewCardOrder::RetrievabilityDescending => Some(fsrs::ReviewPriorityFn::new(|card| {
            -scaled_i32(card.retrievability(), 1000.0)
        })),
        ReviewCardOrder::RelativeOverdueness => Some(fsrs::ReviewPriorityFn::new(|card| {
            scaled_i32(
                card.retrievability() / card.desired_retention.max(0.0001),
                1000.0,
            )
        })),
        ReviewCardOrder::Random => Some(fsrs::ReviewPriorityFn::new(|card| {
            stable_card_hash(card.id)
        })),
        ReviewCardOrder::Added => Some(fsrs::ReviewPriorityFn::new(|card| {
            card.id.clamp(i32::MIN as i64, i32::MAX as i64) as i32
        })),
        ReviewCardOrder::ReverseAdded => Some(fsrs::ReviewPriorityFn::new(|card| {
            -(card.id.clamp(i32::MIN as i64, i32::MAX as i64) as i32)
        })),
    }
}

fn scaled_i32(value: f32, scale: f32) -> i32 {
    if value.is_finite() {
        (value * scale)
            .round()
            .clamp(i32::MIN as f32, i32::MAX as f32) as i32
    } else {
        0
    }
}

fn stable_card_hash(id: i64) -> i32 {
    let mut value = id as u64;
    value ^= value >> 33;
    value = value.wrapping_mul(0xff51afd7ed558ccd);
    value ^= value >> 33;
    value = value.wrapping_mul(0xc4ceb9fe1a85ec53);
    value ^= value >> 33;
    (value >> 32) as i32
}

pub(crate) fn is_included_card(c: &Card) -> bool {
    c.queue != CardQueue::Suspended
        && c.queue != CardQueue::PreviewRepeat
        && c.ctype != CardType::New
}

const R_BUCKET_COUNT: usize = 20;
const MAX_TAKEN_MILLIS: u32 = 1_200_000;
const S_BUCKET_COUNT_FOR_UI: usize = 1;
const MIN_REPS_FOR_REGRESSION: f32 = 2.0;
const MAX_REPS_FOR_REGRESSION: f32 = 30.0;

fn help_me_decide_timing_line(
    total_elapsed_ms: u128,
    review_time_model_elapsed_ms: u128,
    workload_sweep_elapsed_ms: u128,
) -> String {
    format!(
        "[help-me-decide timing] total={}ms review_time_model={}ms workload_sweep={}ms",
        total_elapsed_ms, review_time_model_elapsed_ms, workload_sweep_elapsed_ms
    )
}

fn include_repetitions_in_regression(repetitions: f32) -> bool {
    repetitions >= MIN_REPS_FOR_REGRESSION && repetitions <= MAX_REPS_FOR_REGRESSION
}

fn consume_review_repetition(prior_review_repetitions: &mut u32, is_review: bool) -> Option<f32> {
    if !is_review {
        return None;
    }
    let repetitions = *prior_review_repetitions as f32;
    *prior_review_repetitions += 1;
    Some(repetitions)
}

#[derive(Clone)]
struct HelpMeDecideReviewTimeModel {
    // Per-rating linear models:
    // seconds = a + b * (1 - retrievability) + c * stability + d * repetitions + e * difficulty
    coeffs: [[f32; 5]; 4],
    // per-group fallback if regression is not applicable / prediction is invalid
    group_fallback: [f32; 4],
    // per-group representative stability used for flattened output
    group_mean_stability: [f32; 4],
    // per-group representative repetitions used for cost prediction
    group_mean_repetitions: [f32; 4],
    // per-group representative difficulty used for flattened output
    group_mean_difficulty: [f32; 4],
    // sample count per R bucket (all grades combined)
    sample_counts: [u32; R_BUCKET_COUNT],
    // Markov transition probabilities P(next_grade | current_grade), flattened row-wise for UI.
    transition_probs: [[f32; 4]; 4],
    transition_counts: [[u32; 4]; 4],
    // P(Hard/Good/Easy | R bucket) + bucket sample size.
    success_grade_probs_by_r_bucket: [[f32; 3]; R_BUCKET_COUNT],
    success_grade_counts_by_r_bucket: [u32; R_BUCKET_COUNT],
    // predicted next-grade weights from transition matrix steady-state.
    grade_weights: [f32; 4],
}

impl HelpMeDecideReviewTimeModel {
    const AGAIN_GROUP: usize = 0;
    const HARD_GROUP: usize = 1;
    const GOOD_GROUP: usize = 2;
    const EASY_GROUP: usize = 3;

    fn group_index_from_grade(grade: usize) -> Option<usize> {
        if (1..=4).contains(&grade) {
            Some(grade - 1)
        } else {
            None
        }
    }

    fn r_bucket(retrievability: f32) -> usize {
        let clamped = retrievability.clamp(0.0, 1.0);
        let base_index = ((clamped * 100.0).min(99.9999) / 5.0).floor() as usize;
        // Bucket 0 represents [95%,100%], bucket 1 [90%,95%), etc.
        R_BUCKET_COUNT.saturating_sub(1 + base_index)
    }

    fn from_samples(
        samples: &[(f32, f32, f32, f32, usize, f32)],
        transition_counts: [[u32; 4]; 4],
        enforce_monotonic_success_grade_probs: bool,
        default_review_costs: [f32; 4],
    ) -> Self {
        let mut sample_counts = [0u32; R_BUCKET_COUNT];
        let mut success_grade_counts_by_r_bucket = [[0u32; 3]; R_BUCKET_COUNT];
        let mut group_sum = [0.0f32; 4];
        let mut group_count = [0u32; 4];
        let mut sum_y = [0.0f32; 4];
        let mut group_sum_stability = [0.0f32; 4];
        let mut group_sum_repetitions = [0.0f32; 4];
        let mut group_sum_difficulty = [0.0f32; 4];
        let mut sum_x1 = [0.0f32; 4];
        let mut sum_x2 = [0.0f32; 4];
        let mut sum_x3 = [0.0f32; 4];
        let mut sum_x4 = [0.0f32; 4];
        let mut sum_x1x1 = [0.0f32; 4];
        let mut sum_x2x2 = [0.0f32; 4];
        let mut sum_x3x3 = [0.0f32; 4];
        let mut sum_x4x4 = [0.0f32; 4];
        let mut sum_x1x2 = [0.0f32; 4];
        let mut sum_x1x3 = [0.0f32; 4];
        let mut sum_x1x4 = [0.0f32; 4];
        let mut sum_x2x3 = [0.0f32; 4];
        let mut sum_x2x4 = [0.0f32; 4];
        let mut sum_x3x4 = [0.0f32; 4];
        let mut sum_x1y = [0.0f32; 4];
        let mut sum_x2y = [0.0f32; 4];
        let mut sum_x3y = [0.0f32; 4];
        let mut sum_x4y = [0.0f32; 4];

        for (retrievability, stability, repetitions, difficulty, grade, seconds) in samples {
            let Some(group_idx) = Self::group_index_from_grade(*grade) else {
                continue;
            };
            let rb = Self::r_bucket(*retrievability);
            sample_counts[rb] += 1;
            if (2..=4).contains(grade) {
                success_grade_counts_by_r_bucket[rb][*grade - 2] += 1;
            }
            group_sum[group_idx] += *seconds;
            group_count[group_idx] += 1;
            group_sum_stability[group_idx] += *stability;
            group_sum_repetitions[group_idx] += *repetitions;
            group_sum_difficulty[group_idx] += *difficulty;
            let x1 = 1.0 - retrievability.clamp(0.0, 1.0);
            let x2 = stability.max(0.0);
            let x3 = repetitions.max(0.0);
            let x4 = difficulty.max(0.0);
            let y = *seconds;
            sum_x1[group_idx] += x1;
            sum_x2[group_idx] += x2;
            sum_x3[group_idx] += x3;
            sum_x4[group_idx] += x4;
            sum_y[group_idx] += y;
            sum_x1x1[group_idx] += x1 * x1;
            sum_x2x2[group_idx] += x2 * x2;
            sum_x3x3[group_idx] += x3 * x3;
            sum_x4x4[group_idx] += x4 * x4;
            sum_x1x2[group_idx] += x1 * x2;
            sum_x1x3[group_idx] += x1 * x3;
            sum_x1x4[group_idx] += x1 * x4;
            sum_x2x3[group_idx] += x2 * x3;
            sum_x2x4[group_idx] += x2 * x4;
            sum_x3x4[group_idx] += x3 * x4;
            sum_x1y[group_idx] += x1 * y;
            sum_x2y[group_idx] += x2 * y;
            sum_x3y[group_idx] += x3 * y;
            sum_x4y[group_idx] += x4 * y;
        }

        let mut coeffs = [[0.0f32; 5]; 4];
        let mut group_fallback = [0.0f32; 4];
        let mut group_mean_stability = [0.0f32; 4];
        let mut group_mean_repetitions = [0.0f32; 4];
        let mut group_mean_difficulty = [0.0f32; 4];
        for g in 0..4 {
            let fallback = if group_count[g] > 0 {
                group_sum[g] / group_count[g] as f32
            } else {
                default_review_costs[g]
            };
            group_fallback[g] = fallback;
            group_mean_stability[g] = if group_count[g] > 0 {
                group_sum_stability[g] / group_count[g] as f32
            } else {
                0.0
            };
            group_mean_repetitions[g] = if group_count[g] > 0 {
                group_sum_repetitions[g] / group_count[g] as f32
            } else {
                0.0
            };
            group_mean_difficulty[g] = if group_count[g] > 0 {
                group_sum_difficulty[g] / group_count[g] as f32
            } else {
                0.0
            };
            if group_count[g] >= 5 {
                let n = group_count[g] as f32;
                let matrix = [
                    [n, sum_x1[g], sum_x2[g], sum_x3[g], sum_x4[g]],
                    [
                        sum_x1[g],
                        sum_x1x1[g],
                        sum_x1x2[g],
                        sum_x1x3[g],
                        sum_x1x4[g],
                    ],
                    [
                        sum_x2[g],
                        sum_x1x2[g],
                        sum_x2x2[g],
                        sum_x2x3[g],
                        sum_x2x4[g],
                    ],
                    [
                        sum_x3[g],
                        sum_x1x3[g],
                        sum_x2x3[g],
                        sum_x3x3[g],
                        sum_x3x4[g],
                    ],
                    [
                        sum_x4[g],
                        sum_x1x4[g],
                        sum_x2x4[g],
                        sum_x3x4[g],
                        sum_x4x4[g],
                    ],
                ];
                let vector = [sum_y[g], sum_x1y[g], sum_x2y[g], sum_x3y[g], sum_x4y[g]];
                if let Some(solution) = Self::solve_5x5(matrix, vector) {
                    coeffs[g] = solution;
                    continue;
                }
            }
            coeffs[g] = [fallback, 0.0, 0.0, 0.0, 0.0];
        }

        let transition_probs = Self::transition_probabilities_from_counts(transition_counts);
        let grade_weights = Self::stationary_distribution(transition_probs)
            .unwrap_or_else(|| Self::grade_weights_from_counts(group_count));
        let (success_grade_probs_by_r_bucket, success_grade_counts_by_r_bucket) =
            Self::success_grade_probs_by_r_bucket(
                success_grade_counts_by_r_bucket,
                enforce_monotonic_success_grade_probs,
            );
        Self {
            coeffs,
            group_fallback,
            group_mean_stability,
            group_mean_repetitions,
            group_mean_difficulty,
            sample_counts,
            transition_probs,
            transition_counts,
            success_grade_probs_by_r_bucket,
            success_grade_counts_by_r_bucket,
            grade_weights,
        }
    }

    fn grade_weights_from_counts(group_count: [u32; 4]) -> [f32; 4] {
        let total = group_count.iter().sum::<u32>();
        if total == 0 {
            return [0.25; 4];
        }
        group_count.map(|count| count as f32 / total as f32)
    }

    fn transition_probabilities_from_counts(counts: [[u32; 4]; 4]) -> [[f32; 4]; 4] {
        let mut probs = [[0.0; 4]; 4];
        let mut global_next = [0u32; 4];
        for row in counts {
            for (idx, count) in row.into_iter().enumerate() {
                global_next[idx] += count;
            }
        }
        let global_total = global_next.iter().sum::<u32>();
        let global_fallback = if global_total == 0 {
            [0.25; 4]
        } else {
            global_next.map(|count| count as f32 / global_total as f32)
        };

        for row_idx in 0..4 {
            let row_total = counts[row_idx].iter().sum::<u32>();
            if row_total == 0 {
                probs[row_idx] = global_fallback;
            } else {
                probs[row_idx] = counts[row_idx].map(|count| count as f32 / row_total as f32);
            }
        }
        probs
    }

    fn stationary_distribution(transition_probs: [[f32; 4]; 4]) -> Option<[f32; 4]> {
        let mut dist = [0.25f32; 4];
        for _ in 0..128 {
            let mut next = [0.0f32; 4];
            for from in 0..4 {
                for to in 0..4 {
                    next[to] += dist[from] * transition_probs[from][to];
                }
            }
            let sum = next.iter().sum::<f32>();
            if !sum.is_finite() || sum <= f32::EPSILON {
                return None;
            }
            for v in &mut next {
                *v /= sum;
            }
            let delta = (0..4).map(|i| (next[i] - dist[i]).abs()).sum::<f32>();
            dist = next;
            if delta <= 1e-6 {
                break;
            }
        }
        if dist.iter().all(|v| v.is_finite() && *v >= 0.0) {
            Some(dist)
        } else {
            None
        }
    }

    fn success_grade_probs_by_r_bucket(
        counts: [[u32; 3]; R_BUCKET_COUNT],
        enforce_monotonic: bool,
    ) -> ([[f32; 3]; R_BUCKET_COUNT], [u32; R_BUCKET_COUNT]) {
        let mut raw_probs = [[0.0f32; 3]; R_BUCKET_COUNT];
        let mut totals = [0u32; R_BUCKET_COUNT];
        for (rb, row) in counts.into_iter().enumerate() {
            let row_total = row.iter().sum::<u32>();
            totals[rb] = row_total;
            // Laplace smoothing to avoid zero probabilities in geometric blend.
            let denom = row_total as f32 + 3.0;
            raw_probs[rb] = row.map(|count| (count as f32 + 1.0) / denom);
        }

        // Reliability shrinkage for sparse low-R buckets:
        // blend bucket-local estimate with a distance-weighted neighborhood prior.
        const SHRINK_K: f32 = 100.0;
        let mut probs = [[0.0f32; 3]; R_BUCKET_COUNT];
        for rb in 0..R_BUCKET_COUNT {
            let mut prior = [0.0f32; 3];
            let mut prior_weight = 0.0f32;
            for nb in 0..R_BUCKET_COUNT {
                let distance = (rb as i32 - nb as i32).unsigned_abs() as f32;
                let kernel = 1.0 / (1.0 + distance);
                let weight = kernel * (totals[nb] as f32 + 1.0);
                for g in 0..3 {
                    prior[g] += raw_probs[nb][g] * weight;
                }
                prior_weight += weight;
            }
            if prior_weight > f32::EPSILON {
                for g in 0..3 {
                    prior[g] /= prior_weight;
                }
            } else {
                prior = [1.0 / 3.0; 3];
            }

            let local_weight = totals[rb] as f32 / (totals[rb] as f32 + SHRINK_K);
            let mut blended = [0.0f32; 3];
            for g in 0..3 {
                blended[g] = local_weight * raw_probs[rb][g] + (1.0 - local_weight) * prior[g];
            }
            probs[rb] = Self::normalize_triplet(blended);
        }

        if enforce_monotonic {
            let mut hard = [0.0f32; R_BUCKET_COUNT];
            let mut easy = [0.0f32; R_BUCKET_COUNT];
            let mut weights = [0.0f32; R_BUCKET_COUNT];
            for rb in 0..R_BUCKET_COUNT {
                hard[rb] = probs[rb][0];
                easy[rb] = probs[rb][2];
                weights[rb] = totals[rb] as f32 + 1.0;
            }
            let hard = Self::isotonic_increasing(hard, weights);
            let neg_easy = easy.map(|v| -v);
            let neg_easy = Self::isotonic_increasing(neg_easy, weights);
            let easy = neg_easy.map(|v| -v);
            for rb in 0..R_BUCKET_COUNT {
                let mut h = hard[rb].clamp(0.0, 1.0);
                let mut e = easy[rb].clamp(0.0, 1.0);
                if h + e >= 0.999 {
                    let scale = 0.999 / (h + e);
                    h *= scale;
                    e *= scale;
                }
                let g = 1.0 - h - e;
                probs[rb] = Self::normalize_triplet([h, g.max(0.0), e]);
            }
        }
        (probs, totals)
    }

    fn isotonic_increasing(
        values: [f32; R_BUCKET_COUNT],
        weights: [f32; R_BUCKET_COUNT],
    ) -> [f32; R_BUCKET_COUNT] {
        let mut starts: Vec<usize> = Vec::with_capacity(R_BUCKET_COUNT);
        let mut ends: Vec<usize> = Vec::with_capacity(R_BUCKET_COUNT);
        let mut sums: Vec<f32> = Vec::with_capacity(R_BUCKET_COUNT);
        let mut ws: Vec<f32> = Vec::with_capacity(R_BUCKET_COUNT);

        for i in 0..R_BUCKET_COUNT {
            starts.push(i);
            ends.push(i);
            sums.push(values[i] * weights[i]);
            ws.push(weights[i]);
            while sums.len() >= 2 {
                let n = sums.len();
                let avg_prev = sums[n - 2] / ws[n - 2];
                let avg_curr = sums[n - 1] / ws[n - 1];
                if avg_prev <= avg_curr {
                    break;
                }
                let merged_sum = sums[n - 2] + sums[n - 1];
                let merged_w = ws[n - 2] + ws[n - 1];
                sums[n - 2] = merged_sum;
                ws[n - 2] = merged_w;
                ends[n - 2] = ends[n - 1];
                sums.pop();
                ws.pop();
                starts.pop();
                ends.pop();
            }
        }

        let mut out = [0.0f32; R_BUCKET_COUNT];
        for block in 0..sums.len() {
            let avg = sums[block] / ws[block];
            for idx in starts[block]..=ends[block] {
                out[idx] = avg;
            }
        }
        out
    }

    fn normalize_triplet(values: [f32; 3]) -> [f32; 3] {
        let sum = values.iter().sum::<f32>();
        if !sum.is_finite() || sum <= f32::EPSILON {
            [1.0 / 3.0; 3]
        } else {
            [values[0] / sum, values[1] / sum, values[2] / sum]
        }
    }

    fn blend_weight_for_r_bucket(&self, r_bucket: usize) -> f32 {
        let r_count = self.success_grade_counts_by_r_bucket[r_bucket] as f32;
        let t_count = self
            .transition_counts
            .iter()
            .flatten()
            .copied()
            .sum::<u32>() as f32;
        let r_conf = r_count / (r_count + 20.0);
        let t_conf = t_count / (t_count + 50.0);
        let denom = r_conf + t_conf;
        if denom <= f32::EPSILON {
            0.5
        } else {
            (t_conf / denom).clamp(0.0, 1.0)
        }
    }

    fn solve_5x5(matrix: [[f32; 5]; 5], vector: [f32; 5]) -> Option<[f32; 5]> {
        let mut m = [[0.0f32; 6]; 5];
        for row in 0..5 {
            for col in 0..5 {
                m[row][col] = matrix[row][col];
            }
            m[row][5] = vector[row];
        }

        for pivot in 0..5 {
            let mut best = pivot;
            for row in (pivot + 1)..5 {
                if m[row][pivot].abs() > m[best][pivot].abs() {
                    best = row;
                }
            }
            if m[best][pivot].abs() <= 1e-8 {
                return None;
            }
            if best != pivot {
                m.swap(best, pivot);
            }
            let pivot_value = m[pivot][pivot];
            for col in pivot..6 {
                m[pivot][col] /= pivot_value;
            }
            for row in 0..5 {
                if row == pivot {
                    continue;
                }
                let factor = m[row][pivot];
                if factor.abs() <= f32::EPSILON {
                    continue;
                }
                for col in pivot..6 {
                    m[row][col] -= factor * m[pivot][col];
                }
            }
        }

        let solution = [m[0][5], m[1][5], m[2][5], m[3][5], m[4][5]];
        if solution.iter().all(|v| v.is_finite()) {
            Some(solution)
        } else {
            None
        }
    }

    fn predict_seconds_for_group(
        &self,
        group_idx: usize,
        retrievability: f32,
        stability: f32,
        difficulty: f32,
    ) -> f32 {
        let [a, b, c, d, e] = self.coeffs[group_idx];
        let x1 = 1.0 - retrievability.clamp(0.0, 1.0);
        let x2 = stability.max(0.0);
        let x3 = self.group_mean_repetitions[group_idx];
        let x4 = difficulty.max(0.0);
        let predicted = a + b * x1 + c * x2 + d * x3 + e * x4;
        if predicted.is_finite() && predicted > 0.0 {
            predicted
        } else {
            self.group_fallback[group_idx]
        }
    }

    fn review_cost_for_rating(
        &self,
        retrievability: f32,
        stability: f32,
        difficulty: f32,
        rating: usize,
    ) -> f32 {
        let Some(group_idx) = Self::group_index_from_grade(rating) else {
            return 0.0;
        };
        self.predict_seconds_for_group(group_idx, retrievability, stability, difficulty)
    }

    #[cfg(test)]
    fn cost_for(
        &self,
        retrievability: f32,
        stability: f32,
        repetitions: f32,
        difficulty: f32,
        grade: usize,
    ) -> f32 {
        let Some(group_idx) = Self::group_index_from_grade(grade) else {
            return 0.0;
        };
        let [a, b, c, d, e] = self.coeffs[group_idx];
        let x1 = 1.0 - retrievability.clamp(0.0, 1.0);
        let x2 = stability.max(0.0);
        let x3 = repetitions.max(0.0);
        let x4 = difficulty.max(0.0);
        let predicted = a + b * x1 + c * x2 + d * x3 + e * x4;
        if predicted.is_finite() && predicted > 0.0 {
            predicted
        } else {
            self.group_fallback[group_idx]
        }
    }

    fn coeffs_for_group(&self, group_idx: usize) -> [f32; 5] {
        self.coeffs[group_idx]
    }

    fn grade_flattened(&self) -> [Vec<f32>; 4] {
        let mut again = Vec::with_capacity(R_BUCKET_COUNT * S_BUCKET_COUNT_FOR_UI);
        let mut hard = Vec::with_capacity(R_BUCKET_COUNT * S_BUCKET_COUNT_FOR_UI);
        let mut good = Vec::with_capacity(R_BUCKET_COUNT * S_BUCKET_COUNT_FOR_UI);
        let mut easy = Vec::with_capacity(R_BUCKET_COUNT * S_BUCKET_COUNT_FOR_UI);
        for rb in 0..R_BUCKET_COUNT {
            let retrievability = (1.0 - ((rb as f32 + 0.5) * 0.05)).clamp(0.0, 1.0);
            again.push(self.predict_seconds_for_group(
                Self::AGAIN_GROUP,
                retrievability,
                self.group_mean_stability[Self::AGAIN_GROUP],
                self.group_mean_difficulty[Self::AGAIN_GROUP],
            ));
            hard.push(self.predict_seconds_for_group(
                Self::HARD_GROUP,
                retrievability,
                self.group_mean_stability[Self::HARD_GROUP],
                self.group_mean_difficulty[Self::HARD_GROUP],
            ));
            good.push(self.predict_seconds_for_group(
                Self::GOOD_GROUP,
                retrievability,
                self.group_mean_stability[Self::GOOD_GROUP],
                self.group_mean_difficulty[Self::GOOD_GROUP],
            ));
            easy.push(self.predict_seconds_for_group(
                Self::EASY_GROUP,
                retrievability,
                self.group_mean_stability[Self::EASY_GROUP],
                self.group_mean_difficulty[Self::EASY_GROUP],
            ));
        }
        [again, hard, good, easy]
    }

    fn sample_counts_flattened(&self) -> Vec<u32> {
        let mut counts = Vec::with_capacity(R_BUCKET_COUNT * S_BUCKET_COUNT_FOR_UI);
        for rb in 0..R_BUCKET_COUNT {
            counts.push(self.sample_counts[rb]);
        }
        counts
    }

    fn grade_weights(&self) -> [f32; 4] {
        self.grade_weights
    }

    fn transition_probs_flattened(&self) -> Vec<f32> {
        self.transition_probs
            .iter()
            .flat_map(|row| row.iter().copied())
            .collect_vec()
    }

    fn transition_counts_flattened(&self) -> Vec<u32> {
        self.transition_counts
            .iter()
            .flat_map(|row| row.iter().copied())
            .collect_vec()
    }

    fn success_review_rating_prob(&self, fallback: [f32; 3]) -> [f32; 3] {
        let [again, hard, good, easy] = self.grade_weights;
        let success = hard + good + easy;
        if success <= 1e-6 || !again.is_finite() {
            return fallback;
        }
        let mut out = [hard / success, good / success, easy / success];
        let sum = out.iter().sum::<f32>();
        if !sum.is_finite() || sum <= f32::EPSILON {
            fallback
        } else {
            out.iter_mut().for_each(|v| *v /= sum);
            out
        }
    }

    fn success_review_rating_prob_for_retrievability(
        &self,
        retrievability: f32,
        fallback: [f32; 3],
        blend_alpha_override: Option<f32>,
    ) -> [f32; 3] {
        let rb = Self::r_bucket(retrievability);
        let pr = self.success_grade_probs_by_r_bucket[rb];
        let pt = self.success_review_rating_prob(fallback);
        let alpha = blend_alpha_override
            .map(|v| v.clamp(0.0, 1.0))
            .unwrap_or_else(|| self.blend_weight_for_r_bucket(rb));
        let mut scores = [0.0f32; 3];
        for i in 0..3 {
            scores[i] = pr[i].powf(1.0 - alpha) * pt[i].powf(alpha);
        }
        let sum = scores.iter().sum::<f32>();
        if !sum.is_finite() || sum <= f32::EPSILON {
            fallback
        } else {
            scores.map(|v| v / sum)
        }
    }

    fn success_grade_probs_flattened(&self) -> Vec<f32> {
        self.success_grade_probs_by_r_bucket
            .iter()
            .flat_map(|row| row.iter().copied())
            .collect_vec()
    }

    fn success_grade_counts_flattened(&self) -> Vec<u32> {
        self.success_grade_counts_by_r_bucket.to_vec()
    }
}

impl Collection {
    fn build_help_me_decide_review_time_model(
        &mut self,
        req: &SimulateFsrsReviewRequest,
        default_review_costs: [f32; 4],
    ) -> Result<HelpMeDecideReviewTimeModel> {
        let fsrs = FSRS::new(&req.params)?;
        let next_day_at = self.timing_today()?.next_day_at;
        let guard = self.search_cards_into_table(&req.search, SortMode::NoOrder)?;
        let revlogs = guard
            .col
            .storage
            .get_revlog_entries_for_searched_cards_in_card_order()?;
        drop(guard);

        let mut samples = Vec::new();
        let mut transition_counts = [[0u32; 4]; 4];

        for (_cid, group) in &revlogs.into_iter().chunk_by(|r| r.cid) {
            let entries = group.collect_vec();
            let Some(output) =
                reviews_for_fsrs(entries, next_day_at, false, TimestampMillis(0), false)
            else {
                continue;
            };
            if !output.revlogs_complete {
                continue;
            }
            let Some((_, item)) = output.fsrs_items.last() else {
                continue;
            };
            let item = item.clone();
            let states = fsrs.historical_memory_states(item.clone(), None)?;
            if output.filtered_revlogs.len() != item.reviews.len()
                || states.len() != item.reviews.len()
            {
                continue;
            }

            let mut prior_review_repetitions = output
                .filtered_revlogs
                .first()
                .map(|entry| (entry.review_kind == crate::revlog::RevlogReviewKind::Review) as u32)
                .unwrap_or(0);
            let mut previous_review_grade: Option<usize> =
                output.filtered_revlogs.first().and_then(|entry| {
                    let grade = entry.button_chosen as usize;
                    if entry.review_kind == crate::revlog::RevlogReviewKind::Review
                        && (1..=4).contains(&grade)
                    {
                        Some(grade)
                    } else {
                        None
                    }
                });
            for idx in 1..output.filtered_revlogs.len() {
                let entry = &output.filtered_revlogs[idx];
                let Some(repetitions) = consume_review_repetition(
                    &mut prior_review_repetitions,
                    entry.review_kind == crate::revlog::RevlogReviewKind::Review,
                ) else {
                    continue;
                };
                if entry.taken_millis == 0 || entry.taken_millis >= MAX_TAKEN_MILLIS {
                    continue;
                }
                let grade = entry.button_chosen as usize;
                if !(1..=4).contains(&grade) {
                    continue;
                }
                if let Some(prev_grade) = previous_review_grade {
                    transition_counts[prev_grade - 1][grade - 1] += 1;
                }
                previous_review_grade = Some(grade);
                let previous_state = states[idx - 1];
                let retrievability =
                    fsrs.current_retrievability(previous_state, item.reviews[idx].delta_t as f32);
                let stability = previous_state.stability;
                let difficulty = previous_state.difficulty;
                if !include_repetitions_in_regression(repetitions) {
                    continue;
                }
                let seconds = entry.taken_millis as f32 / 1000.0;
                samples.push((
                    retrievability,
                    stability,
                    repetitions,
                    difficulty,
                    grade,
                    seconds,
                ));
            }
        }

        Ok(HelpMeDecideReviewTimeModel::from_samples(
            &samples,
            transition_counts,
            req.help_me_decide_enforce_monotonic_success_grade_probs
                .unwrap_or(false),
            default_review_costs,
        ))
    }

    pub fn simulate_request_to_config(
        &mut self,
        req: &SimulateFsrsReviewRequest,
    ) -> Result<(SimulatorConfig, Vec<fsrs::Card>)> {
        let guard = self.search_cards_into_table(&req.search, SortMode::NoOrder)?;
        let revlogs = guard
            .col
            .storage
            .get_revlog_entries_for_searched_cards_in_card_order()?;
        let mut cards = guard.col.storage.all_searched_cards()?;
        drop(guard);
        // calculate any missing memory state
        for c in &mut cards {
            if is_included_card(c) && c.memory_state.is_none() {
                let fsrs_data = self.compute_memory_state(c.id)?;
                c.memory_state = fsrs_data.state.map(Into::into);
                c.desired_retention = Some(fsrs_data.desired_retention);
                c.decay = Some(fsrs_data.decay);
                self.storage.update_card(c)?;
            }
        }
        let days_elapsed = self.timing_today().unwrap().days_elapsed as i32;
        let new_cards = cards
            .iter()
            .filter(|c| c.ctype == CardType::New && c.queue != CardQueue::Suspended)
            .count()
            + req.deck_size as usize;
        let fsrs = FSRS::new(&req.params)?;
        let filled_params = normalized_fsrs_parameters(&req.params)?;
        let shared_parameters = Arc::new(filled_params);
        let mut converted_cards = cards
            .into_iter()
            .filter(is_included_card)
            .filter_map(|mut c| {
                let memory_state = match c.memory_state {
                    Some(state) => state,
                    // cards that lack memory states after compute_memory_state have no FSRS items,
                    // implying a truncated or ignored revlog
                    None => memory_state_from_sm2_with_params(
                        &fsrs,
                        &req.params,
                        c.ease_factor(),
                        c.interval as f32,
                        req.historical_retention,
                    )
                    .ok()?
                    .into(),
                };
                // Simulator DR should reflect the request, regardless of any
                // stale per-card desired retention persisted on cards.
                apply_simulation_desired_retention(&mut c, req.desired_retention);
                Card::convert_with_options(
                    c,
                    days_elapsed,
                    memory_state,
                    req.desired_retention,
                    &shared_parameters,
                )
            })
            .collect_vec();
        let introduced_today_count = self
            .search_cards(&format!("{} introduced:1", &req.search), SortMode::NoOrder)?
            .len()
            .min(req.new_limit as usize);
        if req.new_limit > 0 {
            let new_cards = (0..new_cards).map(|i| fsrs::Card {
                id: -(i as i64),
                difficulty: f32::NEG_INFINITY,
                stability: 1e-8,              // Not filtered by fsrs-rs
                last_date: f32::NEG_INFINITY, // Treated as a new card in simulation
                due: ((introduced_today_count + i) / req.new_limit as usize) as f32,
                interval: f32::NEG_INFINITY,
                lapses: 0,
                desired_retention: req.desired_retention,
                parameters: shared_parameters.clone(),
            });
            converted_cards.extend(new_cards);
        }
        let deck_size = converted_cards.len();
        let p = self.get_optimal_retention_parameters(revlogs)?;

        let easy_days_percentages = parse_easy_days_percentages(&req.easy_days_percentages)?;
        let mut review_fuzz_config = ReviewFuzzConfig::default();
        if let Some(value) = req.review_fuzz_base {
            review_fuzz_config.base = value;
        }
        if let Some(value) = req.review_fuzz_factor_short {
            review_fuzz_config.factor_short = value;
        }
        if let Some(value) = req.review_fuzz_factor_mid {
            review_fuzz_config.factor_mid = value;
        }
        if let Some(value) = req.review_fuzz_factor_long {
            review_fuzz_config.factor_long = value;
        }
        let next_day_at = self.timing_today()?.next_day_at;

        let post_scheduling_fn = if self.get_config_bool(BoolKey::LoadBalancerEnabled) {
            let _ = (next_day_at, easy_days_percentages, review_fuzz_config);
            None
        } else {
            None
        };

        let review_priority_fn = req
            .review_order
            .try_into()
            .ok()
            .and_then(|order| create_review_priority_fn(order, deck_size));

        let config = SimulatorConfig {
            deck_size,
            learn_span: req.days_to_simulate as usize,
            max_cost_perday: f32::MAX,
            max_ivl: req.max_interval as f32,
            first_rating_prob: p.first_rating_prob,
            review_rating_prob: p.review_rating_prob,
            learn_limit: req.new_limit as usize,
            review_limit: req.review_limit as usize,
            new_cards_ignore_review_limit: req.new_cards_ignore_review_limit,
            suspend_after_lapses: req.suspend_after_lapse_count,
            post_scheduling_fn,
            review_priority_fn,
            review_rating_cost_fn: None,
            learning_step_transitions: p.learning_step_transitions,
            relearning_step_transitions: p.relearning_step_transitions,
            state_rating_costs: p.state_rating_costs,
            learning_step_count: req.learning_step_count as usize,
            relearning_step_count: req.relearning_step_count as usize,
        };

        Ok((config, converted_cards))
    }

    pub fn simulate_review(
        &mut self,
        req: SimulateFsrsReviewRequest,
    ) -> Result<SimulateFsrsReviewResponse> {
        let (config, cards) = self.simulate_request_to_config(&req)?;
        let result = simulate(
            &config,
            &req.params,
            req.desired_retention,
            None,
            Some(cards),
        )?;
        Ok(SimulateFsrsReviewResponse {
            accumulated_knowledge_acquisition: result.memorized_cnt_per_day,
            daily_review_count: result
                .review_cnt_per_day
                .iter()
                .map(|x| *x as u32)
                .collect_vec(),
            daily_new_count: result
                .learn_cnt_per_day
                .iter()
                .map(|x| *x as u32)
                .collect_vec(),
            daily_time_cost: result.cost_per_day,
        })
    }

    pub fn simulate_workload(
        &mut self,
        req: SimulateFsrsReviewRequest,
    ) -> Result<SimulateFsrsWorkloadResponse> {
        let total_start = Instant::now();
        let (mut config, cards) = self.simulate_request_to_config(&req)?;
        let default_review_costs = config.state_rating_costs[1];
        let review_time_model_start = Instant::now();
        let model = self.build_help_me_decide_review_time_model(&req, default_review_costs)?;
        let review_time_model_elapsed_ms = review_time_model_start.elapsed().as_millis();
        let [review_time_again_seconds, review_time_hard_seconds, review_time_good_seconds, review_time_easy_seconds] =
            model.grade_flattened();
        let review_time_sample_counts = model.sample_counts_flattened();
        let model = Arc::new(model);
        let review_cost_model = model.clone();
        config.review_rating_cost_fn = Some(fsrs::ReviewRatingCostFn::new(
            move |card, rating, retrievability| {
                review_cost_model.review_cost_for_rating(
                    retrievability,
                    card.stability,
                    card.difficulty,
                    rating,
                )
            },
        ));
        let workload_sweep_start = Instant::now();
        let mut dr_workload = HashMap::with_capacity(99);
        let base_review_rating_prob = config.review_rating_prob;
        let blend_alpha_override = req.help_me_decide_transition_blend_alpha;
        for dr in 1u32..=99u32 {
            let desired_retention = dr as f32 / 100.;
            config.review_rating_prob = model.success_review_rating_prob_for_retrievability(
                desired_retention,
                base_review_rating_prob,
                blend_alpha_override,
            );
            let result = simulate_workload_for_desired_retention(
                &config,
                &req.params,
                &cards,
                desired_retention,
            )?;
            dr_workload.insert(
                dr,
                (
                    *result.memorized_cnt_per_day.last().unwrap_or(&0.),
                    result.cost_per_day.iter().sum::<f32>(),
                    result.review_cnt_per_day.iter().sum::<usize>() as u32
                        + result.learn_cnt_per_day.iter().sum::<usize>() as u32,
                ),
            );
        }
        let workload_sweep_elapsed_ms = workload_sweep_start.elapsed().as_millis();
        let reviewless_end_memorized = cards
            .iter()
            .fold(0., |p, c| p + c.retention_on(req.days_to_simulate as f32));
        let total_elapsed_ms = total_start.elapsed().as_millis();
        eprintln!(
            "{}",
            help_me_decide_timing_line(
                total_elapsed_ms,
                review_time_model_elapsed_ms,
                workload_sweep_elapsed_ms,
            )
        );
        Ok(SimulateFsrsWorkloadResponse {
            reviewless_end_memorized,
            memorized: dr_workload.iter().map(|(k, v)| (*k, v.0)).collect(),
            cost: dr_workload.iter().map(|(k, v)| (*k, v.1)).collect(),
            review_count: dr_workload.iter().map(|(k, v)| (*k, v.2)).collect(),
            review_time_r_bucket_count: R_BUCKET_COUNT as u32,
            review_time_s_bucket_count: S_BUCKET_COUNT_FOR_UI as u32,
            review_time_again_seconds,
            review_time_hard_seconds,
            review_time_good_seconds,
            review_time_easy_seconds,
            review_time_sample_counts,
            review_time_again_coeffs: model
                .coeffs_for_group(HelpMeDecideReviewTimeModel::AGAIN_GROUP)
                .to_vec(),
            review_time_hard_coeffs: model
                .coeffs_for_group(HelpMeDecideReviewTimeModel::HARD_GROUP)
                .to_vec(),
            review_time_good_coeffs: model
                .coeffs_for_group(HelpMeDecideReviewTimeModel::GOOD_GROUP)
                .to_vec(),
            review_time_easy_coeffs: model
                .coeffs_for_group(HelpMeDecideReviewTimeModel::EASY_GROUP)
                .to_vec(),
            review_time_grade_weights: model.grade_weights().to_vec(),
            review_time_transition_probs: model.transition_probs_flattened(),
            review_time_transition_counts: model.transition_counts_flattened(),
            review_time_success_grade_probs: model.success_grade_probs_flattened(),
            review_time_success_grade_counts: model.success_grade_counts_flattened(),
        })
    }
}

fn apply_simulation_desired_retention(card: &mut Card, desired_retention: f32) {
    card.desired_retention = Some(desired_retention);
}

fn apply_simulation_desired_retention_to_cards(cards: &mut [fsrs::Card], desired_retention: f32) {
    for card in cards {
        card.desired_retention = desired_retention;
    }
}

fn simulate_workload_for_desired_retention(
    config: &SimulatorConfig,
    params: &[f32],
    cards: &[fsrs::Card],
    desired_retention: f32,
) -> Result<fsrs::SimulationResult> {
    let mut cards_for_dr = cards.to_vec();
    apply_simulation_desired_retention_to_cards(&mut cards_for_dr, desired_retention);
    Ok(simulate(
        config,
        params,
        desired_retention,
        None,
        Some(cards_for_dr),
    )?)
}

impl Card {
    pub(crate) fn convert(
        card: Card,
        days_elapsed: i32,
        memory_state: FsrsMemoryState,
    ) -> Option<fsrs::Card> {
        let parameters = Arc::new(DEFAULT_PARAMETERS.to_vec());
        Self::convert_with_options(card, days_elapsed, memory_state, 0.9, &parameters)
    }

    pub(crate) fn convert_with_options(
        card: Card,
        days_elapsed: i32,
        memory_state: FsrsMemoryState,
        default_desired_retention: f32,
        parameters: &Arc<Vec<f32>>,
    ) -> Option<fsrs::Card> {
        match card.queue {
            CardQueue::DayLearn | CardQueue::Review => {
                let due = card.original_or_current_due();
                let relative_due = due - days_elapsed;
                let last_date = (relative_due - card.interval as i32).min(0) as f32;
                Some(fsrs::Card {
                    id: card.id.0,
                    difficulty: memory_state.difficulty,
                    stability: memory_state.stability,
                    last_date,
                    due: relative_due as f32,
                    interval: card.interval as f32,
                    lapses: card.lapses,
                    desired_retention: card.desired_retention.unwrap_or(default_desired_retention),
                    parameters: parameters.clone(),
                })
            }
            CardQueue::New => None,
            CardQueue::Learn | CardQueue::SchedBuried | CardQueue::UserBuried => Some(fsrs::Card {
                id: card.id.0,
                difficulty: memory_state.difficulty,
                stability: memory_state.stability,
                last_date: 0.0,
                due: 0.0,
                interval: card.interval as f32,
                lapses: card.lapses,
                desired_retention: card.desired_retention.unwrap_or(default_desired_retention),
                parameters: parameters.clone(),
            }),
            CardQueue::PreviewRepeat => None,
            CardQueue::Suspended => None,
        }
    }
}

fn normalized_fsrs_parameters(params: &[f32]) -> Result<Vec<f32>> {
    let converted = match params.len() {
        0 => DEFAULT_PARAMETERS.to_vec(),
        17 => {
            let mut parameters = params.to_vec();
            parameters[4] = parameters[5].mul_add(2.0, parameters[4]);
            parameters[5] = parameters[5].mul_add(3.0, 1.0).ln() / 3.0;
            parameters[6] += 0.5;
            parameters.extend_from_slice(&[0.0, 0.0, 0.0, fsrs::FSRS5_DEFAULT_DECAY]);
            parameters
        }
        19 => {
            let mut parameters = params.to_vec();
            parameters.extend_from_slice(&[0.0, fsrs::FSRS5_DEFAULT_DECAY]);
            parameters
        }
        21 => params.to_vec(),
        35 => params.to_vec(),
        _ => invalid_input!("invalid FSRS parameter count"),
    };
    if converted.iter().any(|w| !w.is_finite()) {
        invalid_input!("invalid FSRS parameter values")
    } else {
        Ok(converted)
    }
}

#[cfg(test)]
mod tests {
    use fsrs::Card as SimCard;
    use fsrs::SimulatorConfig;
    use fsrs::DEFAULT_PARAMETERS;

    use super::apply_simulation_desired_retention;
    use super::apply_simulation_desired_retention_to_cards;
    use super::create_review_priority_fn;
    use super::simulate_workload_for_desired_retention;
    use super::HelpMeDecideReviewTimeModel;
    use crate::card::Card;
    use crate::deckconfig::ReviewCardOrder;

    fn no_transitions() -> [[u32; 4]; 4] {
        [[0u32; 4]; 4]
    }

    fn synthetic_fail_model() -> HelpMeDecideReviewTimeModel {
        HelpMeDecideReviewTimeModel::from_samples(
            &[
                (0.9, 5.0, 1.0, 5.0, 1, 27.3),
                (0.8, 7.0, 2.0, 6.0, 1, 40.6),
                (0.7, 9.0, 3.0, 7.0, 1, 53.9),
                (0.6, 6.0, 4.0, 6.5, 1, 47.2),
                (0.5, 8.0, 1.0, 5.5, 1, 40.5),
                (0.85, 10.0, 2.0, 7.5, 1, 52.45),
            ],
            no_transitions(),
            false,
            [20.0, 18.0, 12.0, 9.0],
        )
    }

    #[test]
    fn review_time_model_uses_five_percent_r_buckets() {
        assert_eq!(HelpMeDecideReviewTimeModel::r_bucket(1.0), 0);
        assert_eq!(HelpMeDecideReviewTimeModel::r_bucket(0.95), 0);
        assert_eq!(HelpMeDecideReviewTimeModel::r_bucket(0.949), 1);
        assert_eq!(HelpMeDecideReviewTimeModel::r_bucket(0.90), 1);
    }

    #[test]
    fn review_time_model_uses_linear_regression_on_retrievability() {
        let model = synthetic_fail_model();
        let high_r = model.cost_for(0.9, 7.0, 2.0, 6.0, 1);
        let low_r = model.cost_for(0.7, 7.0, 2.0, 6.0, 1);
        assert!(low_r > high_r);
    }

    #[test]
    fn help_me_decide_timing_line_includes_expected_fields() {
        assert_eq!(
            super::help_me_decide_timing_line(123, 45, 67),
            "[help-me-decide timing] total=123ms review_time_model=45ms workload_sweep=67ms"
        );
    }

    #[test]
    fn review_priority_fn_reflects_review_order() {
        let short_low_r = SimCard {
            id: 1,
            difficulty: 3.0,
            stability: 5.0,
            last_date: -5.0,
            due: -1.0,
            interval: 5.0,
            lapses: 0,
            desired_retention: 0.9,
            parameters: std::sync::Arc::new(DEFAULT_PARAMETERS.to_vec()),
        };
        let long_high_r = SimCard {
            id: 2,
            difficulty: 7.0,
            stability: 50.0,
            last_date: -5.0,
            due: -5.0,
            interval: 50.0,
            lapses: 0,
            desired_retention: 0.9,
            parameters: std::sync::Arc::new(DEFAULT_PARAMETERS.to_vec()),
        };

        let interval_ascending =
            create_review_priority_fn(ReviewCardOrder::IntervalsAscending, 2).unwrap();
        assert!(interval_ascending(&short_low_r) < interval_ascending(&long_high_r));

        let interval_descending =
            create_review_priority_fn(ReviewCardOrder::IntervalsDescending, 2).unwrap();
        assert!(interval_descending(&short_low_r) > interval_descending(&long_high_r));

        let retrievability_ascending =
            create_review_priority_fn(ReviewCardOrder::RetrievabilityAscending, 2).unwrap();
        assert!(retrievability_ascending(&short_low_r) < retrievability_ascending(&long_high_r));

        let retrievability_descending =
            create_review_priority_fn(ReviewCardOrder::RetrievabilityDescending, 2).unwrap();
        assert!(retrievability_descending(&short_low_r) > retrievability_descending(&long_high_r));
    }

    #[test]
    fn review_priority_changes_limited_workload_simulation() {
        let short = SimCard {
            id: 1,
            difficulty: 5.0,
            stability: 10.0,
            last_date: -10.0,
            due: -1.0,
            interval: 10.0,
            lapses: 0,
            desired_retention: 0.9,
            parameters: std::sync::Arc::new(DEFAULT_PARAMETERS.to_vec()),
        };
        let long = SimCard {
            id: 2,
            difficulty: 5.0,
            stability: 50.0,
            last_date: -50.0,
            due: -5.0,
            interval: 50.0,
            lapses: 0,
            desired_retention: 0.9,
            parameters: std::sync::Arc::new(DEFAULT_PARAMETERS.to_vec()),
        };
        let cards = vec![short, long];
        let mut ascending_config = SimulatorConfig {
            deck_size: cards.len(),
            learn_span: 1,
            learn_limit: 0,
            review_limit: 1,
            review_rating_prob: [0.0, 1.0, 0.0],
            ..Default::default()
        };
        ascending_config.review_priority_fn =
            create_review_priority_fn(ReviewCardOrder::IntervalsAscending, cards.len());
        let mut descending_config = SimulatorConfig {
            deck_size: cards.len(),
            learn_span: 1,
            learn_limit: 0,
            review_limit: 1,
            review_rating_prob: [0.0, 1.0, 0.0],
            ..Default::default()
        };
        descending_config.review_priority_fn =
            create_review_priority_fn(ReviewCardOrder::IntervalsDescending, cards.len());

        let ascending = simulate_workload_for_desired_retention(
            &ascending_config,
            &DEFAULT_PARAMETERS,
            &cards,
            0.9,
        )
        .unwrap();
        let descending = simulate_workload_for_desired_retention(
            &descending_config,
            &DEFAULT_PARAMETERS,
            &cards,
            0.9,
        )
        .unwrap();

        assert_eq!(ascending.cards[0].last_date, 0.0);
        assert_eq!(ascending.cards[1].last_date, -50.0);
        assert_eq!(descending.cards[0].last_date, -10.0);
        assert_eq!(descending.cards[1].last_date, 0.0);
    }

    #[test]
    fn grade_matrix_uses_per_grade_regression() {
        let mut samples = Vec::new();
        samples.push((0.9, 5.0, 2.0, 5.0, 1, 30.0));
        samples.push((0.9, 5.0, 2.0, 5.0, 2, 20.0));
        samples.push((0.9, 5.0, 2.0, 5.0, 3, 10.0));
        samples.push((0.9, 5.0, 2.0, 5.0, 4, 5.0));
        samples.push((0.8, 5.0, 2.0, 5.0, 1, 33.0));
        samples.push((0.8, 5.0, 2.0, 5.0, 2, 23.0));
        samples.push((0.8, 5.0, 2.0, 5.0, 3, 13.0));
        samples.push((0.8, 5.0, 2.0, 5.0, 4, 8.0));
        samples.push((0.7, 5.0, 2.0, 5.0, 1, 36.0));
        samples.push((0.7, 5.0, 2.0, 5.0, 2, 26.0));
        samples.push((0.7, 5.0, 2.0, 5.0, 3, 16.0));
        samples.push((0.7, 5.0, 2.0, 5.0, 4, 11.0));
        let model = HelpMeDecideReviewTimeModel::from_samples(
            &samples,
            no_transitions(),
            false,
            [1.0, 1.0, 1.0, 1.0],
        );
        let [again, hard, good, easy] = model.grade_flattened();
        let rb = HelpMeDecideReviewTimeModel::r_bucket(0.9);
        let idx = rb;
        assert!(again[idx] > hard[idx]);
        assert!(hard[idx] > good[idx]);
        assert!(good[idx] > easy[idx]);
    }

    #[test]
    fn sample_count_matrix_tracks_observed_samples() {
        let model = HelpMeDecideReviewTimeModel::from_samples(
            &[(0.9, 5.0, 2.0, 5.0, 1, 30.0), (0.9, 5.0, 2.0, 5.0, 3, 10.0)],
            no_transitions(),
            false,
            [1.0, 1.0, 1.0, 1.0],
        );
        let rb = HelpMeDecideReviewTimeModel::r_bucket(0.9);
        let counts = model.sample_counts_flattened();
        assert_eq!(counts[rb], 2);
    }

    #[test]
    fn review_time_model_falls_back_to_constant_with_single_sample() {
        let model = HelpMeDecideReviewTimeModel::from_samples(
            &[(0.7, 12.0, 2.0, 5.0, 2, 9.0)],
            no_transitions(),
            false,
            [1.0, 1.0, 1.0, 1.0],
        );
        assert!((model.cost_for(0.2, 0.0, 2.0, 5.0, 2) - 9.0).abs() < 0.0001);
        assert!((model.cost_for(0.9, 0.0, 2.0, 5.0, 2) - 9.0).abs() < 0.0001);
    }

    #[test]
    fn review_time_model_uses_stability_factor() {
        let model = synthetic_fail_model();
        let low_s = model.cost_for(0.8, 5.0, 2.0, 6.0, 1);
        let high_s = model.cost_for(0.8, 9.0, 2.0, 6.0, 1);
        assert!(high_s > low_s);
    }

    #[test]
    fn review_time_model_uses_repetition_factor() {
        let model = synthetic_fail_model();
        let low_reps = model.cost_for(0.8, 7.0, 1.0, 6.0, 1);
        let high_reps = model.cost_for(0.8, 7.0, 4.0, 6.0, 1);
        assert!(high_reps > low_reps);
    }

    #[test]
    fn review_time_model_uses_difficulty_factor() {
        let model = synthetic_fail_model();
        let low_d = model.cost_for(0.8, 7.0, 2.0, 5.0, 1);
        let high_d = model.cost_for(0.8, 7.0, 2.0, 8.0, 1);
        assert!(high_d > low_d);
    }

    #[test]
    fn grade_weights_default_to_uniform_without_transitions() {
        let model = HelpMeDecideReviewTimeModel::from_samples(
            &[
                (0.8, 5.0, 2.0, 5.0, 1, 20.0),
                (0.8, 5.0, 2.0, 5.0, 2, 20.0),
                (0.8, 5.0, 2.0, 5.0, 2, 20.0),
                (0.8, 5.0, 2.0, 5.0, 3, 20.0),
            ],
            no_transitions(),
            false,
            [1.0, 1.0, 1.0, 1.0],
        );
        let weights = model.grade_weights();
        assert!((weights[0] - 0.25).abs() < 1e-6);
        assert!((weights[1] - 0.25).abs() < 1e-6);
        assert!((weights[2] - 0.25).abs() < 1e-6);
        assert!((weights[3] - 0.25).abs() < 1e-6);
    }

    #[test]
    fn transition_probabilities_and_weights_follow_observed_chain() {
        let transitions = [
            [0, 0, 0, 0],
            [0, 0, 2, 0], // Hard -> Good
            [0, 0, 0, 3], // Good -> Easy
            [4, 0, 0, 0], /* Easy -> Again
                           * Again row has no outgoing transitions in this synthetic example. */
        ];
        let model = HelpMeDecideReviewTimeModel::from_samples(
            &[(0.8, 5.0, 2.0, 5.0, 2, 20.0)],
            transitions,
            false,
            [1.0, 1.0, 1.0, 1.0],
        );
        let probs = model.transition_probs_flattened();
        // Hard->Good = 1.0
        assert!((probs[1 * 4 + 2] - 1.0).abs() < 1e-6);
        // Good->Easy = 1.0
        assert!((probs[2 * 4 + 3] - 1.0).abs() < 1e-6);
        // Easy->Again = 1.0
        assert!((probs[3 * 4] - 1.0).abs() < 1e-6);
        let success_probs = model.success_review_rating_prob([0.2, 0.6, 0.2]);
        let sum = success_probs.iter().sum::<f32>();
        assert!((sum - 1.0).abs() < 1e-6);
    }

    #[test]
    fn success_grade_probability_depends_on_r_bucket() {
        let mut samples = Vec::new();
        for _ in 0..20 {
            samples.push((0.95, 5.0, 3.0, 5.0, 4, 8.0));
            samples.push((0.95, 5.0, 3.0, 5.0, 3, 9.0));
            samples.push((0.60, 5.0, 3.0, 5.0, 2, 12.0));
            samples.push((0.60, 5.0, 3.0, 5.0, 3, 10.0));
        }
        let model = HelpMeDecideReviewTimeModel::from_samples(
            &samples,
            no_transitions(),
            false,
            [1.0, 1.0, 1.0, 1.0],
        );
        let p_hi = model.success_review_rating_prob_for_retrievability(0.95, [0.2, 0.6, 0.2], None);
        let p_lo = model.success_review_rating_prob_for_retrievability(0.60, [0.2, 0.6, 0.2], None);
        // Easy at high R should exceed easy at low R.
        assert!(p_hi[2] > p_lo[2]);
        // Hard at low R should exceed hard at high R.
        assert!(p_lo[0] > p_hi[0]);
    }

    #[test]
    fn success_grade_probs_shrink_sparse_buckets_to_neighbor_trend() {
        let mut samples = Vec::new();
        // Neighboring buckets around low-R with stable ~20/80 hard/good split.
        for _ in 0..200 {
            samples.push((0.35, 5.0, 3.0, 5.0, 2, 12.0));
            samples.push((0.35, 5.0, 3.0, 5.0, 3, 10.0));
            samples.push((0.35, 5.0, 3.0, 5.0, 3, 10.0));
            samples.push((0.35, 5.0, 3.0, 5.0, 3, 10.0));
            samples.push((0.45, 5.0, 3.0, 5.0, 2, 12.0));
            samples.push((0.45, 5.0, 3.0, 5.0, 3, 10.0));
            samples.push((0.45, 5.0, 3.0, 5.0, 3, 10.0));
            samples.push((0.45, 5.0, 3.0, 5.0, 3, 10.0));
        }
        let model = HelpMeDecideReviewTimeModel::from_samples(
            &samples,
            no_transitions(),
            false,
            [1.0, 1.0, 1.0, 1.0],
        );
        let rb_sparse = HelpMeDecideReviewTimeModel::r_bucket(0.25);
        let probs = model.success_grade_probs_flattened();
        let p_hard_sparse = probs[rb_sparse * 3];
        let p_good_sparse = probs[rb_sparse * 3 + 1];
        // Should follow neighboring trend instead of defaulting to 1/3,1/3,1/3.
        assert!(p_hard_sparse < 0.30);
        assert!(p_good_sparse > 0.60);
    }

    #[test]
    fn success_grade_blend_alpha_override_works() {
        let transitions = [
            [0, 0, 0, 0],
            [0, 0, 10, 0], // Hard -> Good
            [0, 0, 0, 10], // Good -> Easy
            [10, 0, 0, 0], // Easy -> Again
        ];
        let mut samples = Vec::new();
        for _ in 0..20 {
            // High-R mostly Easy in bucket model
            samples.push((0.95, 5.0, 3.0, 5.0, 4, 8.0));
            samples.push((0.95, 5.0, 3.0, 5.0, 4, 8.0));
            samples.push((0.95, 5.0, 3.0, 5.0, 2, 12.0));
        }
        let model = HelpMeDecideReviewTimeModel::from_samples(
            &samples,
            transitions,
            false,
            [1.0, 1.0, 1.0, 1.0],
        );
        let p_r_only =
            model.success_review_rating_prob_for_retrievability(0.95, [0.2, 0.6, 0.2], Some(0.0));
        let p_t_only =
            model.success_review_rating_prob_for_retrievability(0.95, [0.2, 0.6, 0.2], Some(1.0));
        // R-only should prefer Easy, transition-only prior should lean away from Hard
        // in this setup.
        assert!(p_r_only[2] > p_r_only[0]);
        assert!(p_t_only[1] >= p_t_only[0]);
    }

    #[test]
    fn monotonic_toggle_enforces_hard_and_easy_trends() {
        let mut samples = Vec::new();
        // Intentionally noisy pattern across neighboring buckets.
        for _ in 0..120 {
            samples.push((0.62, 5.0, 3.0, 5.0, 2, 12.0)); // hard
            samples.push((0.62, 5.0, 3.0, 5.0, 3, 10.0)); // good
            samples.push((0.58, 5.0, 3.0, 5.0, 3, 10.0)); // good-heavy lower R
            samples.push((0.58, 5.0, 3.0, 5.0, 4, 8.0)); // easy noise
        }
        let model = HelpMeDecideReviewTimeModel::from_samples(
            &samples,
            no_transitions(),
            true,
            [1.0, 1.0, 1.0, 1.0],
        );
        let probs = model.success_grade_probs_flattened();
        for rb in 1..super::R_BUCKET_COUNT {
            let hard_prev = probs[(rb - 1) * 3];
            let hard_cur = probs[rb * 3];
            let easy_prev = probs[(rb - 1) * 3 + 2];
            let easy_cur = probs[rb * 3 + 2];
            // As R decreases with rb index, hard should not go down and easy should not go
            // up.
            assert!(hard_cur + 1e-6 >= hard_prev);
            assert!(easy_cur <= easy_prev + 1e-6);
        }
    }

    #[test]
    fn repetition_filter_for_regression_is_2_to_30_inclusive() {
        assert!(!super::include_repetitions_in_regression(1.0));
        assert!(super::include_repetitions_in_regression(2.0));
        assert!(super::include_repetitions_in_regression(30.0));
        assert!(!super::include_repetitions_in_regression(31.0));
    }

    #[test]
    fn review_repetition_counter_uses_review_events_only() {
        let mut prior_review_repetitions = 0;
        assert_eq!(
            super::consume_review_repetition(&mut prior_review_repetitions, false),
            None
        );
        assert_eq!(
            super::consume_review_repetition(&mut prior_review_repetitions, true),
            Some(0.0)
        );
        assert_eq!(
            super::consume_review_repetition(&mut prior_review_repetitions, false),
            None
        );
        assert_eq!(
            super::consume_review_repetition(&mut prior_review_repetitions, true),
            Some(1.0)
        );
        assert_eq!(
            super::consume_review_repetition(&mut prior_review_repetitions, true),
            Some(2.0)
        );
    }

    #[test]
    fn simulation_overrides_card_desired_retention_with_request_value() {
        let mut card = Card {
            desired_retention: Some(0.75),
            ..Default::default()
        };
        apply_simulation_desired_retention(&mut card, 0.9);
        assert_eq!(card.desired_retention, Some(0.9));
    }

    #[test]
    fn simulation_overrides_existing_sim_cards_for_each_workload_dr() {
        let config = SimulatorConfig {
            deck_size: 1,
            learn_span: 365,
            learn_limit: 0,
            review_limit: 9999,
            review_rating_prob: [0.0, 1.0, 0.0],
            ..Default::default()
        };
        let card = SimCard {
            id: 1,
            difficulty: 5.0,
            stability: 100.0,
            last_date: -10.0,
            due: 0.0,
            interval: 10.0,
            lapses: 0,
            desired_retention: 0.95,
            parameters: std::sync::Arc::new(DEFAULT_PARAMETERS.to_vec()),
        };
        let cards = vec![card];

        let low_dr =
            simulate_workload_for_desired_retention(&config, &DEFAULT_PARAMETERS, &cards, 0.6)
                .unwrap();
        let high_dr =
            simulate_workload_for_desired_retention(&config, &DEFAULT_PARAMETERS, &cards, 0.9)
                .unwrap();

        let low_reviews = low_dr.review_cnt_per_day.iter().sum::<usize>();
        let high_reviews = high_dr.review_cnt_per_day.iter().sum::<usize>();
        let low_interval = low_dr.cards[0].interval;
        let high_interval = high_dr.cards[0].interval;
        assert!(
            low_reviews <= high_reviews,
            "lower DR should not increase reviews when existing cards are overridden per DR"
        );
        assert!(
            low_interval > high_interval,
            "lower DR should produce longer intervals when existing cards are overridden per DR"
        );
    }

    #[test]
    fn apply_simulation_desired_retention_overrides_all_sim_cards() {
        let mut cards = vec![
            SimCard {
                desired_retention: 0.8,
                ..Default::default()
            },
            SimCard {
                desired_retention: 0.95,
                ..Default::default()
            },
        ];
        apply_simulation_desired_retention_to_cards(&mut cards, 0.7);
        assert!(cards
            .iter()
            .all(|c| (c.desired_retention - 0.7).abs() < 1e-6));
    }
}
