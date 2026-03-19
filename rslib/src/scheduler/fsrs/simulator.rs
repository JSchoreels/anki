// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
use std::collections::HashMap;
use std::sync::Arc;

use anki_proto::deck_config::deck_config::config::ReviewCardOrder;
use anki_proto::deck_config::deck_config::config::ReviewCardOrder::*;
use anki_proto::scheduler::SimulateFsrsReviewRequest;
use anki_proto::scheduler::SimulateFsrsReviewResponse;
use anki_proto::scheduler::SimulateFsrsWorkloadResponse;
use fsrs::simulate;
use fsrs::PostSchedulingFn;
use fsrs::ReviewCostFn;
use fsrs::ReviewPriorityFn;
use fsrs::SimulatorConfig;
use fsrs::FSRS;
use itertools::Itertools;
use rand::rngs::StdRng;
use rand::Rng;
use rayon::iter::IntoParallelIterator;
use rayon::iter::ParallelIterator;

use crate::card::CardQueue;
use crate::card::CardType;
use crate::card::FsrsMemoryState;
use crate::prelude::*;
use crate::scheduler::fsrs::params::reviews_for_fsrs;
use crate::scheduler::states::fuzz::constrained_fuzz_bounds;
use crate::scheduler::states::fuzz::ReviewFuzzConfig;
use crate::scheduler::states::load_balancer::calculate_easy_days_modifiers;
use crate::scheduler::states::load_balancer::interval_to_weekday;
use crate::scheduler::states::load_balancer::parse_easy_days_percentages;
use crate::scheduler::states::load_balancer::select_weighted_interval;
use crate::scheduler::states::load_balancer::EasyDay;
use crate::scheduler::states::load_balancer::LoadBalancerInterval;
use crate::search::SortMode;

pub(crate) fn apply_load_balance_and_easy_days(
    interval: f32,
    max_interval: f32,
    day_elapsed: usize,
    due_cnt_per_day: &[usize],
    rng: &mut StdRng,
    next_day_at: TimestampSecs,
    easy_days_percentages: &[EasyDay; 7],
    review_fuzz_config: ReviewFuzzConfig,
) -> f32 {
    let (lower, upper) =
        constrained_fuzz_bounds(interval, 1, max_interval as u32, review_fuzz_config);
    let mut review_counts = vec![0; upper as usize - lower as usize + 1];

    // Fill review_counts with due counts for each interval
    let start = day_elapsed + lower as usize;
    let end = (day_elapsed + upper as usize + 1).min(due_cnt_per_day.len());
    if start < due_cnt_per_day.len() {
        let copy_len = (end - start).min(review_counts.len());
        review_counts[..copy_len].copy_from_slice(&due_cnt_per_day[start..start + copy_len]);
    }

    let possible_intervals: Vec<u32> = (lower..=upper).collect();
    let weekdays = possible_intervals
        .iter()
        .map(|interval| {
            interval_to_weekday(
                *interval,
                next_day_at.adding_secs(day_elapsed as i64 * 86400),
            )
        })
        .collect::<Vec<_>>();
    let easy_days_modifier =
        calculate_easy_days_modifiers(easy_days_percentages, &weekdays, &review_counts);

    let intervals =
        possible_intervals
            .iter()
            .enumerate()
            .map(|(interval_index, &target_interval)| LoadBalancerInterval {
                target_interval,
                review_count: review_counts[interval_index],
                sibling_modifier: 1.0,
                easy_days_modifier: easy_days_modifier[interval_index],
            });
    let fuzz_seed = rng.random();
    select_weighted_interval(intervals, Some(fuzz_seed)).unwrap() as f32
}

fn create_review_priority_fn(
    review_order: ReviewCardOrder,
    deck_size: usize,
) -> Option<ReviewPriorityFn> {
    // Helper macro to wrap closure in ReviewPriorityFn
    macro_rules! wrap {
        ($f:expr) => {
            Some(ReviewPriorityFn(std::sync::Arc::new($f)))
        };
    }

    match review_order {
        // Ease-based ordering
        EaseAscending => wrap!(|c, _w| -(c.difficulty * 100.0) as i32),
        EaseDescending => wrap!(|c, _w| (c.difficulty * 100.0) as i32),

        // Interval-based ordering
        IntervalsAscending => wrap!(|c, _w| c.interval as i32),
        IntervalsDescending => wrap!(|c, _w| (c.interval as i32).saturating_neg()),
        // Retrievability-based ordering
        RetrievabilityAscending => {
            wrap!(move |c, w| (c.retrievability(w) * 1000.0) as i32)
        }
        RetrievabilityDescending => {
            wrap!(move |c, w| -(c.retrievability(w) * 1000.0) as i32)
        }

        // Due date ordering
        Day | DayThenDeck | DeckThenDay => {
            wrap!(|c, _w| c.scheduled_due() as i32)
        }

        // Random ordering
        Random => {
            wrap!(move |_c, _w| rand::rng().random_range(0..deck_size) as i32)
        }

        // Not implemented yet
        Added | ReverseAdded => None,
    }
}

pub(crate) fn is_included_card(c: &Card) -> bool {
    c.queue != CardQueue::Suspended
        && c.queue != CardQueue::PreviewRepeat
        && c.ctype != CardType::New
}

const R_BUCKET_COUNT: usize = 20;
const MAX_TAKEN_MILLIS: u32 = 1_200_000;
const S_BUCKET_COUNT_FOR_UI: usize = 1;

#[derive(Clone)]
struct HelpMeDecideReviewTimeModel {
    // grade (Again..Easy) x R bucket
    costs: [[f32; R_BUCKET_COUNT]; 4],
    // sample count per R bucket (all grades combined)
    sample_counts: [u32; R_BUCKET_COUNT],
}

impl HelpMeDecideReviewTimeModel {
    fn r_bucket(retrievability: f32) -> usize {
        let clamped = retrievability.clamp(0.0, 1.0);
        let base_index = ((clamped * 100.0).min(99.9999) / 5.0).floor() as usize;
        // Bucket 0 represents [95%,100%], bucket 1 [90%,95%), etc.
        R_BUCKET_COUNT.saturating_sub(1 + base_index)
    }

    fn from_samples(samples: &[(f32, usize, f32)], default_review_costs: [f32; 4]) -> Self {
        let mut sums = [[0.0; R_BUCKET_COUNT]; 4];
        let mut counts = [[0u32; R_BUCKET_COUNT]; 4];
        let mut sample_counts = [0u32; R_BUCKET_COUNT];
        let mut grade_sum = [0.0; 4];
        let mut grade_count = [0u32; 4];

        for (retrievability, grade, seconds) in samples {
            if !(1..=4).contains(grade) {
                continue;
            }
            let g = *grade - 1;
            let rb = Self::r_bucket(*retrievability);
            sums[g][rb] += *seconds;
            counts[g][rb] += 1;
            sample_counts[rb] += 1;
            grade_sum[g] += *seconds;
            grade_count[g] += 1;
        }

        let mut resolved = [[0.0; R_BUCKET_COUNT]; 4];
        for g in 0..4 {
            let grade_fallback = if grade_count[g] > 0 {
                grade_sum[g] / grade_count[g] as f32
            } else {
                default_review_costs[g]
            };
            for rb in 0..R_BUCKET_COUNT {
                resolved[g][rb] = Self::resolve_cell(g, rb, &sums, &counts, grade_fallback);
            }
        }

        Self {
            costs: resolved,
            sample_counts,
        }
    }

    fn resolve_cell(
        grade: usize,
        target_r: usize,
        sums: &[[f32; R_BUCKET_COUNT]; 4],
        counts: &[[u32; R_BUCKET_COUNT]; 4],
        grade_fallback: f32,
    ) -> f32 {
        let exact_count = counts[grade][target_r];
        if exact_count > 0 {
            return sums[grade][target_r] / exact_count as f32;
        }

        let max_distance = R_BUCKET_COUNT - 1;
        for distance in 1..=max_distance {
            let r_min = target_r.saturating_sub(distance);
            let r_max = (target_r + distance).min(R_BUCKET_COUNT - 1);
            let mut nearby_sum = 0.0;
            let mut nearby_count = 0u32;
            if target_r >= r_min {
                let left = target_r.saturating_sub(distance);
                if left >= r_min {
                    let count = counts[grade][left];
                    if count > 0 {
                        nearby_sum += sums[grade][left];
                        nearby_count += count;
                    }
                }
            }
            let right = target_r + distance;
            if right <= r_max {
                let count = counts[grade][right];
                if count > 0 {
                    nearby_sum += sums[grade][right];
                    nearby_count += count;
                }
            }

            if nearby_count > 0 {
                return nearby_sum / nearby_count as f32;
            }
        }

        grade_fallback
    }

    fn cost_for(&self, retrievability: f32, _stability: f32, grade: usize) -> f32 {
        let grade_idx = grade.saturating_sub(1).min(3);
        let rb = Self::r_bucket(retrievability);
        self.costs[grade_idx][rb]
    }

    fn fail_pass_flattened(&self, review_rating_prob: [f32; 3]) -> (Vec<f32>, Vec<f32>) {
        let mut fail = Vec::with_capacity(R_BUCKET_COUNT * S_BUCKET_COUNT_FOR_UI);
        let mut pass = Vec::with_capacity(R_BUCKET_COUNT * S_BUCKET_COUNT_FOR_UI);
        for rb in 0..R_BUCKET_COUNT {
            fail.push(self.costs[0][rb]);
            let weighted_success = self.costs[1][rb] * review_rating_prob[0]
                + self.costs[2][rb] * review_rating_prob[1]
                + self.costs[3][rb] * review_rating_prob[2];
            pass.push(weighted_success);
        }
        (fail, pass)
    }

    fn sample_counts_flattened(&self) -> Vec<u32> {
        let mut counts = Vec::with_capacity(R_BUCKET_COUNT * S_BUCKET_COUNT_FOR_UI);
        for rb in 0..R_BUCKET_COUNT {
            counts.push(self.sample_counts[rb]);
        }
        counts
    }
}

impl Collection {
    fn build_help_me_decide_review_time_model(
        &mut self,
        req: &SimulateFsrsReviewRequest,
        default_review_costs: [f32; 4],
    ) -> Result<HelpMeDecideReviewTimeModel> {
        let fsrs = FSRS::new(Some(&req.params))?;
        let decay = req
            .params
            .get(20)
            .copied()
            .unwrap_or(fsrs::FSRS6_DEFAULT_DECAY);
        let next_day_at = self.timing_today()?.next_day_at;
        let guard = self.search_cards_into_table(&req.search, SortMode::NoOrder)?;
        let revlogs = guard
            .col
            .storage
            .get_revlog_entries_for_searched_cards_in_card_order()?;
        drop(guard);

        let mut samples = Vec::new();

        for (_cid, group) in &revlogs.into_iter().chunk_by(|r| r.cid) {
            let entries = group.collect_vec();
            let Some(output) = reviews_for_fsrs(entries, next_day_at, false, TimestampMillis(0))
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

            for idx in 1..output.filtered_revlogs.len() {
                let entry = &output.filtered_revlogs[idx];
                if entry.review_kind != crate::revlog::RevlogReviewKind::Review {
                    continue;
                }
                if entry.taken_millis == 0 || entry.taken_millis >= MAX_TAKEN_MILLIS {
                    continue;
                }
                let grade = entry.button_chosen as usize;
                if !(1..=4).contains(&grade) {
                    continue;
                }
                let previous_state = states[idx - 1];
                let retrievability =
                    fsrs.current_retrievability(previous_state, item.reviews[idx].delta_t, decay);
                let seconds = entry.taken_millis as f32 / 1000.0;
                samples.push((retrievability, grade, seconds));
            }
        }

        Ok(HelpMeDecideReviewTimeModel::from_samples(
            &samples,
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
        let fsrs = FSRS::new(Some(&req.params))?;
        let mut converted_cards = cards
            .into_iter()
            .filter(is_included_card)
            .filter_map(|c| {
                let memory_state = match c.memory_state {
                    Some(state) => state,
                    // cards that lack memory states after compute_memory_state have no FSRS items,
                    // implying a truncated or ignored revlog
                    None => fsrs
                        .memory_state_from_sm2(
                            c.ease_factor(),
                            c.interval as f32,
                            req.historical_retention,
                        )
                        .ok()?
                        .into(),
                };
                Card::convert(c, days_elapsed, memory_state)
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

        let post_scheduling_fn: Option<PostSchedulingFn> =
            if self.get_config_bool(BoolKey::LoadBalancerEnabled) {
                Some(PostSchedulingFn(Arc::new(
                    move |card, max_interval, today, due_cnt_per_day, rng| {
                        apply_load_balance_and_easy_days(
                            card.interval,
                            max_interval,
                            today,
                            due_cnt_per_day,
                            rng,
                            next_day_at,
                            &easy_days_percentages,
                            review_fuzz_config,
                        )
                    },
                )))
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
            learning_step_transitions: p.learning_step_transitions,
            relearning_step_transitions: p.relearning_step_transitions,
            state_rating_costs: p.state_rating_costs,
            learning_step_count: req.learning_step_count as usize,
            relearning_step_count: req.relearning_step_count as usize,
            review_cost_fn: None,
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
        let (mut config, cards) = self.simulate_request_to_config(&req)?;
        let default_review_costs = config.state_rating_costs[1];
        let model = self.build_help_me_decide_review_time_model(&req, default_review_costs)?;
        let (review_time_fail_seconds, review_time_pass_seconds) =
            model.fail_pass_flattened(config.review_rating_prob);
        let review_time_sample_counts = model.sample_counts_flattened();
        let model = Arc::new(model);
        config.review_cost_fn = Some(ReviewCostFn(Arc::new(move |r, s, grade| {
            model.cost_for(r, s, grade)
        })));
        let dr_workload = (1u32..=99u32)
            .into_par_iter()
            .map(|dr| {
                let result = simulate(
                    &config,
                    &req.params,
                    dr as f32 / 100.,
                    None,
                    Some(cards.clone()),
                )?;
                Ok((
                    dr,
                    (
                        *result.memorized_cnt_per_day.last().unwrap_or(&0.),
                        result.cost_per_day.iter().sum::<f32>(),
                        result.review_cnt_per_day.iter().sum::<usize>() as u32
                            + result.learn_cnt_per_day.iter().sum::<usize>() as u32,
                    ),
                ))
            })
            .collect::<Result<HashMap<_, _>>>()?;
        let start_memorized = cards.iter().fold(0., |p, c| {
            p + c.retention_on(&req.params, req.days_to_simulate as f32)
        });
        Ok(SimulateFsrsWorkloadResponse {
            start_memorized,
            memorized: dr_workload.iter().map(|(k, v)| (*k, v.0)).collect(),
            cost: dr_workload.iter().map(|(k, v)| (*k, v.1)).collect(),
            review_count: dr_workload.iter().map(|(k, v)| (*k, v.2)).collect(),
            review_time_r_bucket_count: R_BUCKET_COUNT as u32,
            review_time_s_bucket_count: S_BUCKET_COUNT_FOR_UI as u32,
            review_time_fail_seconds,
            review_time_pass_seconds,
            review_time_sample_counts,
        })
    }
}

impl Card {
    pub(crate) fn convert(
        card: Card,
        days_elapsed: i32,
        memory_state: FsrsMemoryState,
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
            }),
            CardQueue::PreviewRepeat => None,
            CardQueue::Suspended => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::HelpMeDecideReviewTimeModel;

    #[test]
    fn review_time_model_uses_five_percent_r_buckets() {
        assert_eq!(HelpMeDecideReviewTimeModel::r_bucket(1.0), 0);
        assert_eq!(HelpMeDecideReviewTimeModel::r_bucket(0.95), 0);
        assert_eq!(HelpMeDecideReviewTimeModel::r_bucket(0.949), 1);
        assert_eq!(HelpMeDecideReviewTimeModel::r_bucket(0.90), 1);
    }

    #[test]
    fn review_time_model_falls_back_to_neighbor_bucket_average() {
        let mut samples = Vec::new();
        let target_r = 0.84;
        samples.push((0.89, 3, 8.0));
        samples.push((0.79, 3, 12.0));
        let model = HelpMeDecideReviewTimeModel::from_samples(&samples, [20.0, 18.0, 12.0, 9.0]);
        let predicted = model.cost_for(target_r, 10.0, 3);
        assert!((predicted - 10.0).abs() < 0.0001);
    }

    #[test]
    fn fail_pass_matrix_uses_weighted_success_cost() {
        let mut samples = Vec::new();
        samples.push((0.9, 1, 30.0));
        samples.push((0.9, 2, 20.0));
        samples.push((0.9, 3, 10.0));
        samples.push((0.9, 4, 5.0));
        let model = HelpMeDecideReviewTimeModel::from_samples(&samples, [1.0, 1.0, 1.0, 1.0]);
        let (fail, pass) = model.fail_pass_flattened([0.2, 0.5, 0.3]);
        let rb = HelpMeDecideReviewTimeModel::r_bucket(0.9);
        let idx = rb;
        assert!((fail[idx] - 30.0).abs() < 0.0001);
        assert!((pass[idx] - (20.0 * 0.2 + 10.0 * 0.5 + 5.0 * 0.3)).abs() < 0.0001);
    }

    #[test]
    fn sample_count_matrix_tracks_observed_samples() {
        let model = HelpMeDecideReviewTimeModel::from_samples(
            &[(0.9, 1, 30.0), (0.9, 3, 10.0)],
            [1.0, 1.0, 1.0, 1.0],
        );
        let rb = HelpMeDecideReviewTimeModel::r_bucket(0.9);
        let counts = model.sample_counts_flattened();
        assert_eq!(counts[rb], 2);
    }
}
