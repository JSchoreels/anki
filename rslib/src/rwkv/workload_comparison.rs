// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Compare two scheduling policies against the same simulated learner.
//!
//! The learner owns its own state. Each scheduler sees only its own answers,
//! and paired runs use the same per-card/repetition random quantiles even if
//! the schedulers review that card on different days.

use std::sync::Arc;

use fsrs::MemoryState;
use fsrs::FSRS;

use super::*;
use crate::scheduler::fsrs::simulator::normalized_fsrs_parameters;

struct PreparedCard {
    input: RwkvComparisonCard,
    model: Arc<FSRS>,
    memory: Option<MemoryState>,
}

struct ComparisonCard {
    card: RwkvSimulationCard,
    model: Arc<FSRS>,
    scheduler_memory: Option<MemoryState>,
    learner_memory: Option<MemoryState>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Scheduler {
    Fsrs,
    Rwkv,
}

impl RwkvInference {
    pub fn compare_workloads_from_warm_up(
        &self,
        inputs: Vec<RwkvComparisonCard>,
        config: RwkvComparisonConfig,
        progress: &mut dyn FnMut(u32, u32) -> io::Result<()>,
    ) -> io::Result<RwkvComparisonOutput> {
        if config.min_dr > config.max_dr || config.max_dr > 99 || config.min_dr < 1 {
            return Err(invalid("invalid comparison retention range"));
        }
        if config.repetitions == 0 || config.repetitions > 100 {
            return Err(invalid("comparison repetitions must be between 1 and 100"));
        }
        if !config.review_limit.is_finite()
            || config.review_limit < 0.0
            || !config.new_limit.is_finite()
            || config.new_limit < 0.0
            || !config.output_scale.is_finite()
            || config.output_scale < 1.0
        {
            return Err(invalid("invalid comparison daily limit"));
        }

        let (prepared, estimated_fsrs_states) = prepare_cards(inputs)?;
        let targets = target_drs(config.min_dr, config.max_dr, config.target_dr_step.max(1));
        let total = 1 + targets.len() as u32 * config.repetitions * 2;
        progress(0, total)?;
        let baseline = self.comparison_reviewless(&prepared, &config)?;
        progress(1, total)?;
        let mut completed = 1;
        let mut fsrs_points = Vec::with_capacity(targets.len());
        let mut rwkv_points = Vec::with_capacity(targets.len());
        for dr in targets {
            let mut sums = [(0.0f64, 0.0f64, 0.0f64, 0u64); 2];
            for seed in 0..config.repetitions {
                for (index, scheduler) in [Scheduler::Fsrs, Scheduler::Rwkv].into_iter().enumerate()
                {
                    let point =
                        self.compare_target(&prepared, &config, dr, seed, scheduler, &mut || {
                            progress(completed, total)
                        })?;
                    sums[index].0 += f64::from(point.memorized);
                    sums[index].1 += f64::from(point.weighted_memorized);
                    sums[index].2 += f64::from(point.cost);
                    sums[index].3 += u64::from(point.review_count);
                    completed += 1;
                    progress(completed, total)?;
                }
            }
            let average = |(memorized, weighted, cost, count): (f64, f64, f64, u64)| {
                let n = f64::from(config.repetitions);
                RwkvWorkloadSimulationPoint {
                    memorized: (memorized / n * f64::from(config.output_scale)) as f32,
                    weighted_memorized: (weighted / n * f64::from(config.output_scale)) as f32,
                    cost: (cost / n * f64::from(config.output_scale)) as f32,
                    review_count: ((count as f64 / n * f64::from(config.output_scale))
                        .round()
                        .min(f64::from(u32::MAX))) as u32,
                }
            };
            fsrs_points.push((dr, average(sums[0])));
            rwkv_points.push((dr, average(sums[1])));
        }
        let output = |points| RwkvWorkloadSimulationOutput {
            reviewless_end_memorized: baseline.0 * config.output_scale,
            reviewless_end_weighted_memorized: baseline.1 * config.output_scale,
            points,
        };
        Ok(RwkvComparisonOutput {
            fsrs: output(fsrs_points),
            rwkv: output(rwkv_points),
            estimated_fsrs_states,
        })
    }

    fn comparison_reviewless(
        &self,
        prepared: &[PreparedCard],
        config: &RwkvComparisonConfig,
    ) -> io::Result<(f32, f32)> {
        let cards = initial_cards(prepared);
        let mut worker = self.workload_worker(self.features.clone(), HashMap::new());
        let states = SimulationStates::new(&self.warm_up_states);
        comparison_memorized(
            &cards,
            &mut worker,
            &states,
            config,
            config.days_to_simulate as i64,
        )
    }

    fn compare_target(
        &self,
        prepared: &[PreparedCard],
        config: &RwkvComparisonConfig,
        dr: u32,
        seed: u32,
        scheduler: Scheduler,
        check_progress: &mut dyn FnMut() -> io::Result<()>,
    ) -> io::Result<RwkvWorkloadSimulationPoint> {
        let retention = dr as f32 / 100.0;
        let mut cards = initial_cards(prepared);
        let mut scheduler_worker = self.workload_worker(self.features.clone(), HashMap::new());
        let mut scheduler_states = SimulationStates::new(&self.warm_up_states);
        let mut learner_worker = self.workload_worker(self.features.clone(), HashMap::new());
        let mut learner_states = SimulationStates::new(&self.warm_up_states);
        let mut cost = 0.0;
        let mut review_count = 0u32;

        for day in 0..i64::from(config.days_to_simulate) {
            if day % 7 == 0 {
                check_progress()?;
            }
            let mut due = cards
                .iter()
                .enumerate()
                .filter_map(|(index, card)| {
                    (!card.card.is_new && !card.card.suspended && card.card.due_day <= day)
                        .then_some(index)
                })
                .collect::<Vec<_>>();
            let by_index = if matches!(
                config.review_order,
                ReviewCardOrder::RetrievabilityAscending
                    | ReviewCardOrder::RetrievabilityDescending
                    | ReviewCardOrder::RelativeOverdueness
            ) {
                let predictions = scheduler_predictions(
                    &cards,
                    &due,
                    &mut scheduler_worker,
                    &scheduler_states,
                    retention,
                    day,
                    scheduler,
                );
                due.iter()
                    .copied()
                    .zip(predictions)
                    .collect::<HashMap<_, _>>()
            } else {
                HashMap::new()
            };
            due.sort_by_key(|index| {
                simulation_review_priority(
                    &cards[*index].card,
                    by_index.get(index),
                    config.review_order,
                    retention,
                )
            });
            let review_limit = day_budget(config.review_limit, day);
            due.truncate(review_limit);

            let mut new = cards
                .iter()
                .enumerate()
                .filter_map(|(index, card)| {
                    (card.card.is_new && !card.card.suspended && card.card.due_day <= day)
                        .then_some(index)
                })
                .collect::<Vec<_>>();
            new.sort_by_key(|index| cards[*index].card.review_input.review_input.card_id);
            new.truncate(day_budget(config.new_limit, day));
            if !config.new_cards_ignore_review_limit {
                new.truncate(review_limit.saturating_sub(due.len()));
            }
            due.extend(new);

            for index in due {
                let card = &mut cards[index];
                let elapsed = (day - card.card.last_review_day).max(0) as u32;
                let query = simulation_query_input_for_card(&card.card, retention, day);
                let learner_r = if card.card.is_new {
                    // The first grade is shared by both policies. Its simple
                    // baseline is intentionally distinct from model forecasts.
                    0.85
                } else {
                    learner_prediction(card, &mut learner_worker, &learner_states, config, &query)?
                        .0
                };
                let actual_r = performance_probability(learner_r, day, config);
                let ease = paired_grade(
                    &config.review_model,
                    actual_r,
                    card.card.review_input.review_input.card_id,
                    card.card.reps,
                    seed,
                );
                let seconds = config.review_model.grade_seconds[(ease - 1) as usize];
                cost += seconds;
                review_count = review_count.saturating_add(1);

                let interval = match scheduler {
                    Scheduler::Fsrs => {
                        let next = fsrs_answer(
                            &card.model,
                            card.scheduler_memory,
                            retention,
                            elapsed,
                            ease,
                        )?;
                        card.scheduler_memory = Some(next.memory);
                        clamped_interval_days(next.interval, config.max_interval.max(1))
                    }
                    Scheduler::Rwkv => scheduler_worker.selected_answer_intervals_for_inputs(
                        std::slice::from_ref(&query),
                        &scheduler_states,
                        &[ease],
                    )[0]
                    .unwrap_or(1)
                    .clamp(1, config.max_interval.max(1)),
                };

                let answer = simulation_answer_input(
                    &card.card,
                    retention,
                    day,
                    ease,
                    (seconds * 1000.0).round().max(1.0) as i64,
                );
                if scheduler == Scheduler::Rwkv {
                    scheduler_worker.apply_simulation_answer(&answer, &mut scheduler_states)?;
                }
                match config.learner {
                    RwkvComparisonLearner::Fsrs => {
                        card.learner_memory = Some(
                            fsrs_answer(
                                &card.model,
                                card.learner_memory,
                                retention,
                                elapsed,
                                ease,
                            )?
                            .memory,
                        );
                    }
                    RwkvComparisonLearner::Rwkv => {
                        learner_worker.apply_simulation_answer(&answer, &mut learner_states)?;
                    }
                }
                card.card.last_review_day = day;
                card.card.due_day = day + i64::from(interval);
                card.card.interval_days = i64::from(interval);
                card.card.reps += 1;
                if card.card.is_new {
                    card.card.is_new = false;
                    card.card.review_input.review_input.card_type = Some(CardType::Review as i64);
                }
                if ease == 1 {
                    card.card.lapses += 1;
                    if config
                        .suspend_after_lapses
                        .is_some_and(|n| card.card.lapses >= i64::from(n))
                    {
                        card.card.suspended = true;
                    }
                }
            }
        }
        let (memorized, weighted_memorized) = comparison_memorized(
            &cards,
            &mut learner_worker,
            &learner_states,
            config,
            i64::from(config.days_to_simulate),
        )?;
        Ok(RwkvWorkloadSimulationPoint {
            memorized,
            weighted_memorized,
            cost,
            review_count,
        })
    }
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

fn prepare_cards(inputs: Vec<RwkvComparisonCard>) -> io::Result<(Vec<PreparedCard>, u32)> {
    let mut models = HashMap::<Vec<u32>, Arc<FSRS>>::new();
    let mut estimated = 0;
    let mut cards = Vec::with_capacity(inputs.len());
    for input in inputs {
        let params = normalized_fsrs_parameters(&input.fsrs_parameters)
            .map_err(|err| invalid(format!("invalid FSRS comparison parameters: {err}")))?;
        let key = params
            .iter()
            .map(|value| value.to_bits())
            .collect::<Vec<_>>();
        let model = if let Some(model) = models.get(&key) {
            Arc::clone(model)
        } else {
            let model = Arc::new(FSRS::new(&params).map_err(|err| invalid(err.to_string()))?);
            models.insert(key, Arc::clone(&model));
            model
        };
        let is_new = input.input.review_input.card_type == Some(CardType::New as i64);
        let memory = if is_new {
            None
        } else if let Some(memory) = input
            .fsrs_memory
            .map(|(stability, difficulty, stability_fast)| MemoryState {
                stability,
                difficulty,
                stability_fast,
            })
            .filter(valid_memory)
        {
            Some(memory)
        } else {
            estimated += 1;
            let historical_retention = if (0.0..1.0).contains(&input.historical_retention) {
                input.historical_retention
            } else {
                0.9
            };
            Some(
                model
                    .memory_state_from_sm2(
                        input.input.ease_factor.unwrap_or(2500) as f32 / 1000.0,
                        input.input.interval_days.unwrap_or(1).max(1) as f32,
                        historical_retention,
                    )
                    .map_err(|err| invalid(format!("unable to estimate FSRS state: {err}")))?,
            )
        };
        cards.push(PreparedCard {
            input,
            model,
            memory,
        });
    }
    Ok((cards, estimated))
}

fn valid_memory(memory: &MemoryState) -> bool {
    memory.stability.is_finite()
        && memory.stability > 0.0
        && memory.stability_fast.is_finite()
        && memory.stability_fast > 0.0
        && memory.difficulty.is_finite()
        && (1.0..=10.0).contains(&memory.difficulty)
}

fn initial_cards(prepared: &[PreparedCard]) -> Vec<ComparisonCard> {
    prepared
        .iter()
        .map(|item| {
            let input = &item.input.input;
            let is_new = input.review_input.card_type == Some(CardType::New as i64);
            ComparisonCard {
                card: RwkvSimulationCard {
                    review_input: input.clone(),
                    due_day: if is_new { 0 } else { item.input.due_day },
                    last_review_day: -input_elapsed_days(&input.review_input),
                    reps: input.reps.unwrap_or(0),
                    lapses: input.lapses.unwrap_or(0),
                    interval_days: input.interval_days.unwrap_or(1).max(1),
                    ease_factor: input.ease_factor.unwrap_or(0),
                    is_new,
                    suspended: false,
                },
                model: Arc::clone(&item.model),
                scheduler_memory: item.memory,
                learner_memory: item.memory,
            }
        })
        .collect()
}

fn scheduler_predictions(
    cards: &[ComparisonCard],
    indexes: &[usize],
    worker: &mut RwkvInference,
    states: &SimulationStates<'_>,
    retention: f32,
    day: i64,
    scheduler: Scheduler,
) -> Vec<RwkvWorkloadQueryPrediction> {
    if scheduler == Scheduler::Rwkv {
        let queries = indexes
            .iter()
            .map(|index| simulation_query_input_for_card(&cards[*index].card, retention, day))
            .collect::<Vec<_>>();
        let work_items = queries
            .iter()
            .map(|query| ReviewPredictionBorrowedWorkItem {
                features: worker.features.features_for(query),
                state: states.state_ref(query),
            })
            .collect::<Vec<_>>();
        worker
            .model
            .review_many_borrowed(&work_items, |_, heads| RwkvWorkloadQueryPrediction {
                retrievability: heads.retrievability,
                current_interval: None,
                current_s90: None,
            })
    } else {
        indexes
            .iter()
            .map(|index| {
                let card = &cards[*index];
                let elapsed = (day - card.card.last_review_day).max(0) as f32;
                RwkvWorkloadQueryPrediction {
                    retrievability: card.scheduler_memory.map_or(0.85, |memory| {
                        card.model.current_retrievability(memory, elapsed)
                    }),
                    current_interval: None,
                    current_s90: None,
                }
            })
            .collect()
    }
}

fn learner_prediction(
    card: &ComparisonCard,
    worker: &mut RwkvInference,
    states: &SimulationStates<'_>,
    config: &RwkvComparisonConfig,
    query: &ReviewInput,
) -> io::Result<(f32, Option<f32>)> {
    let result = match config.learner {
        RwkvComparisonLearner::Fsrs => {
            let memory = card
                .learner_memory
                .ok_or_else(|| invalid("missing FSRS learner state"))?;
            (
                card.model
                    .current_retrievability(memory, input_elapsed_days(query) as f32),
                Some(card.model.s90(memory)),
            )
        }
        RwkvComparisonLearner::Rwkv => {
            // The per-answer draw needs recall only; the final S90 weight is
            // calculated separately when the memorized total is computed.
            let heads = worker.review_heads_for_state(query, states.state_ref(query))?;
            (heads.retrievability, None)
        }
    };
    if valid_probability(result.0) {
        Ok(result)
    } else {
        Err(invalid("learner produced an invalid recall probability"))
    }
}

fn comparison_memorized(
    cards: &[ComparisonCard],
    worker: &mut RwkvInference,
    states: &SimulationStates<'_>,
    config: &RwkvComparisonConfig,
    day: i64,
) -> io::Result<(f32, f32)> {
    let mut memorized = 0.0;
    let mut weighted = 0.0;
    let active = cards
        .iter()
        .filter(|card| !card.card.is_new)
        .collect::<Vec<_>>();
    let predictions = if config.learner == RwkvComparisonLearner::Rwkv {
        let queries = active
            .iter()
            .map(|card| simulation_query_input_for_card(&card.card, S90_TARGET_RETENTION, day))
            .collect::<Vec<_>>();
        worker
            .workload_query_predictions_for_inputs(&queries, states)
            .into_iter()
            .map(|prediction| (prediction.retrievability, prediction.current_s90))
            .collect::<Vec<_>>()
    } else {
        active
            .iter()
            .map(|card| {
                let query = simulation_query_input_for_card(&card.card, S90_TARGET_RETENTION, day);
                learner_prediction(card, worker, states, config, &query)
            })
            .collect::<io::Result<Vec<_>>>()?
    };
    for (r, s90) in predictions {
        if !valid_probability(r) {
            return Err(invalid("learner produced an invalid recall probability"));
        }
        let r = performance_probability(r, day, config);
        memorized += r;
        weighted += r * s90_weight(s90);
    }
    Ok((memorized, weighted))
}

fn fsrs_answer(
    model: &FSRS,
    memory: Option<MemoryState>,
    retention: f32,
    elapsed: u32,
    ease: u8,
) -> io::Result<fsrs::ItemState> {
    let next = model
        .next_states(memory, retention, elapsed)
        .map_err(|err| invalid(err.to_string()))?;
    Ok(match ease {
        1 => next.again,
        2 => next.hard,
        3 => next.good,
        4 => next.easy,
        _ => return Err(invalid("invalid simulated grade")),
    })
}

fn performance_probability(r: f32, day: i64, config: &RwkvComparisonConfig) -> f32 {
    let days = i64::from(config.days_to_simulate);
    let start = days / 3;
    let end = (days * 2) / 3;
    let delta = match config.scenario {
        RwkvPerformanceScenario::Steady => 0.0,
        RwkvPerformanceScenario::TemporaryDrop if day >= start && day < end => -0.15,
        RwkvPerformanceScenario::PersistentDrop if day >= start => -0.15,
        RwkvPerformanceScenario::TemporaryImprovement if day >= start && day < end => 0.10,
        _ => 0.0,
    };
    (r + delta).clamp(0.0, 1.0)
}

fn paired_grade(model: &RwkvWorkloadReviewModel, r: f32, card_id: i64, reps: i64, seed: u32) -> u8 {
    let quantile = simulation_unit_hash(
        card_id ^ (i64::from(seed).wrapping_mul(0x5851_F42D_4C95_7F2D)),
        0,
        reps,
    );
    let probabilities = model.probabilities_for(r);
    let success_total = probabilities[1] + probabilities[2] + probabilities[3];
    let shares = if success_total > 0.0 {
        [
            probabilities[1] / success_total,
            probabilities[2] / success_total,
            probabilities[3] / success_total,
        ]
    } else {
        [0.2, 0.7, 0.1]
    };
    let again = 1.0 - r;
    let thresholds = [
        again,
        again + r * shares[0],
        again + r * (shares[0] + shares[1]),
    ];
    thresholds
        .iter()
        .position(|threshold| quantile < f64::from(*threshold))
        .map_or(4, |index| index as u8 + 1)
}

/// Distribute scaled fractional daily limits over time instead of rounding
/// every day up, which can badly exaggerate a small sample's workload.
fn day_budget(limit: f32, day: i64) -> usize {
    let limit = f64::from(limit);
    (((day + 1) as f64 * limit).floor() - (day as f64 * limit).floor()).max(0.0) as usize
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fractional_daily_limit_is_distributed_without_inflation() {
        assert_eq!(
            (0..8).map(|day| day_budget(0.25, day)).collect::<Vec<_>>(),
            vec![0, 0, 0, 1, 0, 0, 0, 1]
        );
        assert_eq!(day_budget(3.0, 4), 3);
    }

    #[test]
    fn performance_scenarios_change_only_the_intended_days() {
        let mut config = test_config();
        config.scenario = RwkvPerformanceScenario::TemporaryDrop;
        assert_eq!(performance_probability(0.8, 2, &config), 0.8);
        assert!((performance_probability(0.8, 3, &config) - 0.65).abs() < 1e-6);
        assert_eq!(performance_probability(0.8, 6, &config), 0.8);
        config.scenario = RwkvPerformanceScenario::PersistentDrop;
        assert!((performance_probability(0.8, 9, &config) - 0.65).abs() < 1e-6);
        config.scenario = RwkvPerformanceScenario::TemporaryImprovement;
        assert!((performance_probability(0.8, 4, &config) - 0.9).abs() < 1e-6);
    }

    #[test]
    fn paired_randomness_is_stable_and_responds_to_performance() {
        let model = test_config().review_model;
        assert_eq!(
            paired_grade(&model, 0.9, 123, 4, 2),
            paired_grade(&model, 0.9, 123, 4, 2)
        );
        let low = (0..1000)
            .filter(|id| paired_grade(&model, 0.4, *id, 1, 0) == 1)
            .count();
        let high = (0..1000)
            .filter(|id| paired_grade(&model, 0.9, *id, 1, 0) == 1)
            .count();
        assert!(low > high);
    }

    #[test]
    fn both_schedulers_use_the_same_learner_and_keep_resident_state() {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(if SMALL_MODEL {
            "../qt/aqt/rwkv_inference/sm40v2/weights.f32.bin"
        } else {
            "../qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin"
        });
        if !path.exists() {
            return;
        }
        let mut inference = RwkvInference::load(path, 0.9, 36500).unwrap();
        let mut inputs = Vec::new();
        for card_id in [101, 102] {
            let answer = ReviewInput {
                card_id,
                note_id: Some(card_id),
                deck_id: Some(1),
                preset_id: Some(1),
                is_query: false,
                ease: Some(3),
                duration_millis: Some(8000),
                card_type: Some(CardType::Review as i64),
                day_offset: Some(100),
                current_elapsed_days: Some(10),
                current_elapsed_seconds: Some(864000),
                target_retentions: [None; 4],
                enforce_grade_order: true,
            };
            inference
                .warm_up_reviews(vec![answer.clone()], false)
                .unwrap();
            let mut query = answer;
            query.is_query = true;
            query.ease = None;
            query.duration_millis = None;
            inputs.push(RwkvComparisonCard {
                input: RwkvWorkloadSimulationInput {
                    review_input: query,
                    interval_days: Some(10),
                    ease_factor: Some(2500),
                    reps: Some(2),
                    lapses: Some(0),
                },
                due_day: 0,
                fsrs_memory: Some((10.0, 5.0, 10.0)),
                fsrs_parameters: vec![],
                historical_retention: 0.9,
            });
        }
        let cache = inference.cache_state();
        let mut missing_memory = inputs.clone();
        missing_memory[0].fsrs_memory = None;
        let (estimated, estimated_count) = prepare_cards(missing_memory).unwrap();
        assert_eq!(estimated_count, 1);
        assert!(valid_memory(&estimated[0].memory.unwrap()));
        let (prepared, _) = prepare_cards(inputs.clone()).unwrap();
        let cards = initial_cards(&prepared);
        let states = SimulationStates::new(&inference.warm_up_states);
        let mut worker = inference.workload_worker(inference.features.clone(), HashMap::new());
        let queries = cards
            .iter()
            .map(|card| simulation_query_input_for_card(&card.card, 0.9, 0))
            .collect::<Vec<_>>();
        let reference = worker.workload_query_predictions_for_inputs(&queries, &states);
        let optimized = scheduler_predictions(
            &cards,
            &[0, 1],
            &mut worker,
            &states,
            0.9,
            0,
            Scheduler::Rwkv,
        );
        for (query, (expected, actual)) in queries.iter().zip(reference.iter().zip(&optimized)) {
            assert!((expected.retrievability - actual.retrievability).abs() < 1e-6);
            let scalar = worker
                .review_heads_for_state(query, states.state_ref(query))
                .unwrap();
            assert!((expected.retrievability - scalar.retrievability).abs() < 1e-6);
        }
        for learner in [RwkvComparisonLearner::Fsrs, RwkvComparisonLearner::Rwkv] {
            let mut steady = test_config();
            steady.min_dr = 90;
            steady.max_dr = 90;
            steady.review_limit = 0.0;
            steady.learner = learner;
            let normal = inference
                .compare_workloads_from_warm_up(inputs.clone(), steady, &mut |_, _| Ok(()))
                .unwrap();
            let mut drop = test_config();
            drop.min_dr = 90;
            drop.max_dr = 90;
            drop.review_limit = 0.0;
            drop.learner = learner;
            drop.scenario = RwkvPerformanceScenario::PersistentDrop;
            let changed = inference
                .compare_workloads_from_warm_up(inputs.clone(), drop, &mut |_, _| Ok(()))
                .unwrap();
            assert_eq!(normal.fsrs.points[0].1.review_count, 0);
            assert_eq!(normal.rwkv.points[0].1.review_count, 0);
            assert!(
                (normal.fsrs.points[0].1.memorized - normal.rwkv.points[0].1.memorized).abs()
                    < 1e-6
            );
            assert!(
                (normal.fsrs.points[0].1.memorized - normal.fsrs.reviewless_end_memorized).abs()
                    < 1e-6
            );
            assert!(
                (normal.fsrs.points[0].1.memorized - changed.fsrs.points[0].1.memorized - 0.3)
                    .abs()
                    < 1e-5
            );
            assert_eq!(
                changed.fsrs.points[0].1.memorized,
                changed.rwkv.points[0].1.memorized
            );

            let mut future_inputs = inputs.clone();
            for item in &mut future_inputs {
                item.due_day = 10;
            }
            let mut future = test_config();
            future.min_dr = 90;
            future.max_dr = 90;
            future.learner = learner;
            let future = inference
                .compare_workloads_from_warm_up(future_inputs, future, &mut |_, _| Ok(()))
                .unwrap();
            assert_eq!(future.fsrs.points[0].1.review_count, 0);
            assert_eq!(future.rwkv.points[0].1.review_count, 0);

            let mut new_card = inputs[0].clone();
            new_card.input.review_input.card_type = Some(CardType::New as i64);
            new_card.fsrs_memory = None;
            let mut new_config = test_config();
            new_config.min_dr = 90;
            new_config.max_dr = 90;
            new_config.days_to_simulate = 1;
            new_config.new_limit = 1.0;
            new_config.review_limit = 1.0;
            new_config.learner = learner;
            let introduced = inference
                .compare_workloads_from_warm_up(vec![new_card], new_config, &mut |_, _| Ok(()))
                .unwrap();
            assert_eq!(introduced.fsrs.points[0].1.review_count, 1);
            assert_eq!(introduced.rwkv.points[0].1.review_count, 1);
            assert_eq!(
                introduced.fsrs.points[0].1.cost,
                introduced.rwkv.points[0].1.cost
            );
            assert_eq!(
                introduced.fsrs.points[0].1.memorized,
                introduced.rwkv.points[0].1.memorized
            );

            let active_config = || {
                let mut config = test_config();
                config.min_dr = 90;
                config.max_dr = 90;
                config.learner = learner;
                config.repetitions = 2;
                config
            };
            let first = inference
                .compare_workloads_from_warm_up(inputs.clone(), active_config(), &mut |_, _| Ok(()))
                .unwrap();
            let second = inference
                .compare_workloads_from_warm_up(inputs.clone(), active_config(), &mut |_, _| Ok(()))
                .unwrap();
            assert!(first.fsrs.points[0].1.review_count > 0);
            assert!(first.rwkv.points[0].1.review_count > 0);
            assert_eq!(
                first.fsrs.points[0].1.review_count,
                second.fsrs.points[0].1.review_count
            );
            assert_eq!(
                first.rwkv.points[0].1.memorized.to_bits(),
                second.rwkv.points[0].1.memorized.to_bits()
            );
            assert_eq!(
                first.fsrs.reviewless_end_memorized,
                first.rwkv.reviewless_end_memorized
            );
        }
        let mut config = test_config();
        config.min_dr = 90;
        config.max_dr = 90;
        let mut callbacks = 0;
        let result = inference.compare_workloads_from_warm_up(inputs, config, &mut |_, _| {
            callbacks += 1;
            if callbacks == 4 {
                Err(io::Error::new(io::ErrorKind::Interrupted, "cancelled"))
            } else {
                Ok(())
            }
        });
        assert_eq!(result.err().unwrap().kind(), io::ErrorKind::Interrupted);
        assert_eq!(cache, inference.cache_state());
    }

    fn test_config() -> RwkvComparisonConfig {
        RwkvComparisonConfig {
            min_dr: 80,
            max_dr: 90,
            target_dr_step: 5,
            days_to_simulate: 9,
            review_limit: 10.0,
            new_limit: 0.0,
            new_cards_ignore_review_limit: false,
            max_interval: 36500,
            review_order: ReviewCardOrder::Day,
            suspend_after_lapses: None,
            learner: RwkvComparisonLearner::Fsrs,
            scenario: RwkvPerformanceScenario::Steady,
            repetitions: 1,
            output_scale: 1.0,
            review_model: RwkvWorkloadReviewModel {
                grade_seconds: [12.0, 9.0, 7.0, 5.0],
                bucket_probabilities: vec![],
            },
        }
    }
}
