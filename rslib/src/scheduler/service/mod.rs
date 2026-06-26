// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod answering;
mod states;

use anki_proto::cards;
use anki_proto::generic;
use anki_proto::scheduler;
use anki_proto::scheduler::ComputeFsrsParamsResponse;
use anki_proto::scheduler::ComputeMemoryStateResponse;
use anki_proto::scheduler::ComputeOptimalRetentionResponse;
use anki_proto::scheduler::FsrsBenchmarkResponse;
use anki_proto::scheduler::FsrsCurrentRetrievabilityRequest;
use anki_proto::scheduler::FsrsCurrentRetrievabilityResponse;
use anki_proto::scheduler::FsrsDesiredRetentionForIntervalsBatchRequest;
use anki_proto::scheduler::FsrsDesiredRetentionForIntervalsBatchResponse;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityBatchRequest;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityBatchResponse;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityByConfigBatchRequest;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityByConfigBatchResponse;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityResponse;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityVariableBatchRequest;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityVariableBatchResponse;
use anki_proto::scheduler::FsrsNextIntervalRequest;
use anki_proto::scheduler::FsrsNextIntervalResponse;
use anki_proto::scheduler::FsrsPresetForCardResponse;
use anki_proto::scheduler::FuzzDeltaRequest;
use anki_proto::scheduler::FuzzDeltaResponse;
use anki_proto::scheduler::GetOptimalRetentionParametersResponse;
use anki_proto::scheduler::SimulateFsrsReviewRequest;
use anki_proto::scheduler::SimulateFsrsReviewResponse;
use anki_proto::scheduler::SimulateFsrsWorkloadResponse;
use fsrs::benchmark;
use fsrs::compute_parameters;
use fsrs::ComputeParametersInput;
use fsrs::ComputeParametersVersion;
use fsrs::FSRSItem;
use fsrs::FSRSReview;
use fsrs::FSRS;

use crate::backend::Backend;
use crate::config::BoolKey;
use crate::deckconfig::FsrsVersion;
use crate::prelude::*;
use crate::scheduler::answering::PreviewDelays;
use crate::scheduler::fsrs::batch::ComputeParamsBatchInput;
use crate::scheduler::fsrs::memory_state::fsrs_memory_state_for_params;
use crate::scheduler::fsrs::params::ComputeParamsRequest;
use crate::scheduler::fsrs::params::DynamicDesiredRetentionSimulatorOptions;
use crate::scheduler::fsrs::params::PrepareComputeParamsInput;
use crate::scheduler::fsrs::preset::FsrsPreset;
use crate::scheduler::fsrs::preset::FsrsPresetId;
use crate::scheduler::new::NewCardDueOrder;
use crate::scheduler::states::CardState;
use crate::scheduler::states::LearnState;
use crate::scheduler::states::SchedulingStates;
use crate::search::SortMode;
use crate::stats::studied_today;

impl crate::services::SchedulerService for Collection {
    /// This behaves like _updateCutoff() in older code - it also unburies at
    /// the start of a new day.
    fn sched_timing_today(&mut self) -> Result<scheduler::SchedTimingTodayResponse> {
        let timing = self.timing_today()?;
        self.unbury_if_day_rolled_over(timing)?;
        Ok(timing.into())
    }

    /// Fetch data from DB and return rendered string.
    fn studied_today(&mut self) -> Result<generic::String> {
        self.studied_today().map(Into::into)
    }

    /// Message rendering only, for old graphs.
    fn studied_today_message(
        &mut self,
        input: scheduler::StudiedTodayMessageRequest,
    ) -> Result<generic::String> {
        Ok(studied_today(input.cards, input.seconds as f32, &self.tr).into())
    }

    fn update_stats(&mut self, input: scheduler::UpdateStatsRequest) -> Result<()> {
        self.transact_no_undo(|col| {
            let today = col.current_due_day(0)?;
            let usn = col.usn()?;
            col.update_deck_stats(today, usn, input)
        })
    }

    fn extend_limits(&mut self, input: scheduler::ExtendLimitsRequest) -> Result<()> {
        self.transact_no_undo(|col| {
            let today = col.current_due_day(0)?;
            let usn = col.usn()?;
            col.extend_limits(
                today,
                usn,
                input.deck_id.into(),
                input.new_delta,
                input.review_delta,
            )
        })
    }

    fn counts_for_deck_today(
        &mut self,
        input: anki_proto::decks::DeckId,
    ) -> Result<scheduler::CountsForDeckTodayResponse> {
        self.counts_for_deck_today(input.did.into())
    }

    fn congrats_info(&mut self) -> Result<scheduler::CongratsInfoResponse> {
        self.congrats_info()
    }

    fn restore_buried_and_suspended_cards(
        &mut self,
        input: anki_proto::cards::CardIds,
    ) -> Result<anki_proto::collection::OpChanges> {
        let cids: Vec<_> = input.cids.into_iter().map(CardId).collect();
        self.unbury_or_unsuspend_cards(&cids).map(Into::into)
    }

    fn unbury_deck(
        &mut self,
        input: scheduler::UnburyDeckRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        self.unbury_deck(input.deck_id.into(), input.mode())
            .map(Into::into)
    }

    fn bury_or_suspend_cards(
        &mut self,
        input: scheduler::BuryOrSuspendCardsRequest,
    ) -> Result<anki_proto::collection::OpChangesWithCount> {
        let mode = input.mode();
        let cids = if input.card_ids.is_empty() {
            self.storage
                .card_ids_of_notes(&input.note_ids.into_newtype(NoteId))?
        } else {
            input.card_ids.into_newtype(CardId)
        };
        self.bury_or_suspend_cards(&cids, mode).map(Into::into)
    }

    fn empty_filtered_deck(
        &mut self,
        input: anki_proto::decks::DeckId,
    ) -> Result<anki_proto::collection::OpChanges> {
        self.empty_filtered_deck(input.did.into()).map(Into::into)
    }

    fn rebuild_filtered_deck(
        &mut self,
        input: anki_proto::decks::DeckId,
    ) -> Result<anki_proto::collection::OpChangesWithCount> {
        self.rebuild_filtered_deck(input.did.into()).map(Into::into)
    }

    fn schedule_cards_as_new(
        &mut self,
        input: scheduler::ScheduleCardsAsNewRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        let cids = input.card_ids.into_newtype(CardId);
        self.reschedule_cards_as_new(
            &cids,
            input.log,
            input.restore_position,
            input.reset_counts,
            input
                .context
                .and_then(|s| scheduler::schedule_cards_as_new_request::Context::try_from(s).ok()),
        )
        .map(Into::into)
    }

    fn schedule_cards_as_new_defaults(
        &mut self,
        input: scheduler::ScheduleCardsAsNewDefaultsRequest,
    ) -> Result<scheduler::ScheduleCardsAsNewDefaultsResponse> {
        Ok(Collection::reschedule_cards_as_new_defaults(
            self,
            input.context(),
        ))
    }

    fn set_due_date(
        &mut self,
        input: scheduler::SetDueDateRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        let config = input.config_key.map(|v| v.key().into());
        let days = input.days;
        let cids = input.card_ids.into_newtype(CardId);
        self.set_due_date(&cids, &days, config).map(Into::into)
    }

    fn grade_now(
        &mut self,
        input: scheduler::GradeNowRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        self.grade_now(input).map(Into::into)
    }

    fn sort_cards(
        &mut self,
        input: scheduler::SortCardsRequest,
    ) -> Result<anki_proto::collection::OpChangesWithCount> {
        let cids = input.card_ids.into_newtype(CardId);
        let (start, step, random, shift) = (
            input.starting_from,
            input.step_size,
            input.randomize,
            input.shift_existing,
        );
        let order = if random {
            NewCardDueOrder::Random
        } else {
            NewCardDueOrder::Preserve
        };

        self.sort_cards(&cids, start, step, order, shift)
            .map(Into::into)
    }

    fn reposition_defaults(&mut self) -> Result<scheduler::RepositionDefaultsResponse> {
        Ok(Collection::reposition_defaults(self))
    }

    fn sort_deck(
        &mut self,
        input: scheduler::SortDeckRequest,
    ) -> Result<anki_proto::collection::OpChangesWithCount> {
        self.sort_deck_legacy(input.deck_id.into(), input.randomize)
            .map(Into::into)
    }

    fn get_scheduling_states(
        &mut self,
        input: anki_proto::cards::CardId,
    ) -> Result<scheduler::SchedulingStates> {
        let cid: CardId = input.into();
        self.get_scheduling_states(cid).map(Into::into)
    }

    fn get_scheduling_states_with_opts(
        &mut self,
        input: scheduler::GetSchedulingStatesRequest,
    ) -> Result<scheduler::SchedulingStates> {
        self.get_scheduling_states_with_desired_retention_override(
            CardId(input.card_id),
            input.desired_retention_override,
        )
        .map(Into::into)
    }

    fn describe_next_states(
        &mut self,
        input: scheduler::SchedulingStates,
    ) -> Result<generic::StringList> {
        let states: SchedulingStates = input.into();
        self.describe_next_states(&states).map(Into::into)
    }

    fn state_is_leech(&mut self, input: scheduler::SchedulingState) -> Result<generic::Bool> {
        let state: CardState = input.into();
        Ok(state.leeched().into())
    }

    fn answer_card(
        &mut self,
        input: scheduler::CardAnswer,
    ) -> Result<anki_proto::collection::OpChanges> {
        self.answer_card(&mut input.into()).map(Into::into)
    }

    fn upgrade_scheduler(&mut self) -> Result<()> {
        self.transact_no_undo(|col| col.upgrade_to_v2_scheduler())
    }

    fn get_queued_cards(
        &mut self,
        input: scheduler::GetQueuedCardsRequest,
    ) -> Result<scheduler::QueuedCards> {
        self.get_queued_cards(
            input.fetch_limit as usize,
            input.intraday_learning_only,
            input.skip_scheduling_states,
        )
        .map(Into::into)
    }

    fn custom_study(
        &mut self,
        input: scheduler::CustomStudyRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        self.custom_study(input).map(Into::into)
    }

    fn custom_study_defaults(
        &mut self,
        input: scheduler::CustomStudyDefaultsRequest,
    ) -> Result<scheduler::CustomStudyDefaultsResponse> {
        self.custom_study_defaults(input.deck_id.into())
    }

    fn compute_fsrs_params(
        &mut self,
        input: scheduler::ComputeFsrsParamsRequest,
    ) -> Result<scheduler::ComputeFsrsParamsResponse> {
        self.compute_params(ComputeParamsRequest {
            search: &input.search,
            ignore_revlogs_before_ms: input.ignore_revlogs_before_ms.into(),
            current_preset: 1,
            total_presets: 1,
            current_params: &input.current_params,
            num_of_relearning_steps: input.num_of_relearning_steps as usize,
            health_check: input.health_check,
            include_same_day_reviews: input.include_same_day_reviews,
            model_version_override: input.fsrs_version.map(health_check_model_version),
            dynamic_desired_retention_enabled: input.dynamic_desired_retention_enabled,
            dynamic_desired_retention_review_limit: input.dynamic_desired_retention_review_limit,
            dynamic_desired_retention_max_cost_perday_minutes: input
                .dynamic_desired_retention_max_cost_perday_minutes,
        })
    }

    fn compute_fsrs_params_batch(
        &mut self,
        input: scheduler::ComputeFsrsParamsBatchRequest,
    ) -> Result<scheduler::ComputeFsrsParamsBatchResponse> {
        let mut response_meta = Vec::with_capacity(input.items.len());
        let mut jobs = Vec::with_capacity(input.items.len());

        for (index, item) in input.items.into_iter().enumerate() {
            let prepared = self.prepare_compute_params(PrepareComputeParamsInput {
                search: &item.search,
                ignore_revlogs_before: item.ignore_revlogs_before_ms.into(),
                current_params: &item.current_params,
                num_of_relearning_steps: item.num_of_relearning_steps as usize,
                include_same_day_reviews: item.include_same_day_reviews,
                model_version_override: item.fsrs_version.map(health_check_model_version),
                dynamic_desired_retention_enabled: item.dynamic_desired_retention_enabled,
                historical_retention: 0.9,
                desired_retention: 0.9,
                dynamic_desired_retention_simulator_options:
                    DynamicDesiredRetentionSimulatorOptions {
                        review_limit: item.dynamic_desired_retention_review_limit,
                        max_cost_perday_minutes: item
                            .dynamic_desired_retention_max_cost_perday_minutes,
                    },
            })?;
            response_meta.push((item.id.clone(), item.name.clone()));
            jobs.push(ComputeParamsBatchInput {
                index,
                name: item.name,
                prepared,
            });
        }

        let items = self
            .compute_params_batch(jobs)?
            .into_iter()
            .map(|output| {
                let (id, name) = &response_meta[output.index];
                let params = output.result?;
                Ok(scheduler::compute_fsrs_params_batch_response::Item {
                    id: id.clone(),
                    name: name.clone(),
                    params: params.params,
                    fsrs_items: params.fsrs_items,
                    fsrs_dynamic_desired_retention_params: params
                        .fsrs_dynamic_desired_retention_params,
                    fsrs_dynamic_desired_retention_weights: params
                        .fsrs_dynamic_desired_retention_weights,
                    fsrs_dynamic_desired_retention_avg_drs: params
                        .fsrs_dynamic_desired_retention_avg_drs,
                    fsrs_dynamic_desired_retention_fsrs_eq_weights: params
                        .fsrs_dynamic_desired_retention_fsrs_eq_weights,
                    fsrs_dynamic_desired_retention_fsrs_eq_drs: params
                        .fsrs_dynamic_desired_retention_fsrs_eq_drs,
                    fsrs_dynamic_desired_retention_fixed_target_weights: params
                        .fsrs_dynamic_desired_retention_fixed_target_weights,
                    fsrs_dynamic_desired_retention_fixed_target_drs: params
                        .fsrs_dynamic_desired_retention_fixed_target_drs,
                    fsrs_dynamic_desired_retention_min: params.fsrs_dynamic_desired_retention_min,
                    fsrs_dynamic_desired_retention_max: params.fsrs_dynamic_desired_retention_max,
                })
            })
            .collect::<Result<Vec<_>>>()?;

        Ok(scheduler::ComputeFsrsParamsBatchResponse { items })
    }

    fn simulate_fsrs_review(
        &mut self,
        input: SimulateFsrsReviewRequest,
    ) -> Result<SimulateFsrsReviewResponse> {
        self.simulate_review(input)
    }

    fn simulate_fsrs_workload(
        &mut self,
        input: SimulateFsrsReviewRequest,
    ) -> Result<SimulateFsrsWorkloadResponse> {
        self.simulate_workload(input)
    }

    fn compute_optimal_retention(
        &mut self,
        input: SimulateFsrsReviewRequest,
    ) -> Result<ComputeOptimalRetentionResponse> {
        Ok(ComputeOptimalRetentionResponse {
            optimal_retention: self.compute_optimal_retention(input)?,
        })
    }

    fn get_fsrs_new_card_intervals(
        &mut self,
        input: scheduler::GetFsrsNewCardIntervalsRequest,
    ) -> Result<generic::StringList> {
        let requested_short_term_with_steps = input.fsrs_short_term_with_steps_enabled;
        let requested_learning_queues_disabled = input.fsrs_learning_queues_disabled;
        let config = crate::deckconfig::DeckConfig {
            inner: input.config.unwrap_or_default(),
            ..Default::default()
        };
        let fsrs = FSRS::new(config.fsrs_params())?;
        let params = config.fsrs_params();
        let fsrs_allow_short_term = if params.len() >= 19 {
            params[17] > 0.0 && params[18] > 0.0
        } else {
            false
        };
        let fsrs_short_term_with_steps_enabled = selected_short_term_with_steps_for_preview(
            requested_short_term_with_steps,
            self.get_config_bool(BoolKey::FsrsShortTermWithStepsEnabled),
        );
        let fsrs_learning_queues_disabled = requested_learning_queues_disabled
            .unwrap_or_else(|| self.get_config_bool(BoolKey::FsrsLearningQueuesDisabled));
        let review_fuzz_config = self.review_fuzz_config();
        let make_ctx = |memory_state: Option<fsrs::MemoryState>,
                        days_elapsed: f32|
         -> Result<crate::scheduler::states::StateContext<'_>> {
            let fsrs_next_states = fsrs.next_states_with_elapsed_days(
                memory_state,
                config.inner.desired_retention,
                days_elapsed,
            )?;
            let fsrs_again_s90 = if config.inner.leech_only_if_young {
                Some(fsrs_memory_state_for_params(params, fsrs_next_states.again.memory)?.stability)
            } else {
                None
            };
            Ok(crate::scheduler::states::StateContext {
                fuzz_factor: None,
                fsrs_next_states: Some(fsrs_next_states),
                fsrs_short_term_with_steps_enabled,
                fsrs_learning_queues_disabled,
                fsrs_allow_short_term,
                steps: crate::scheduler::states::steps::LearningSteps::new(
                    &config.inner.learn_steps,
                ),
                graduating_interval_good: config.inner.graduating_interval_good,
                graduating_interval_easy: config.inner.graduating_interval_easy,
                initial_ease_factor: config.inner.initial_ease,
                hard_multiplier: config.inner.hard_multiplier,
                easy_multiplier: config.inner.easy_multiplier,
                interval_multiplier: config.inner.interval_multiplier,
                review_fuzz_config,
                maximum_review_interval: config.inner.maximum_review_interval,
                fsrs_minimum_interval_secs: config.inner.fsrs_minimum_interval_secs,
                leech_threshold: config.inner.leech_threshold,
                leech_only_if_young: config.inner.leech_only_if_young,
                fsrs_again_s90,
                load_balancer_ctx: None,
                relearn_steps: crate::scheduler::states::steps::LearningSteps::new(
                    &config.inner.relearn_steps,
                ),
                lapse_multiplier: config.inner.lapse_multiplier,
                minimum_lapse_interval: config.inner.minimum_lapse_interval,
                in_filtered_deck: false,
                preview_delays: PreviewDelays::default(),
            })
        };
        let next_followup_states =
            |state: &crate::scheduler::states::CardState| -> Result<SchedulingStates> {
                let (memory_state, days_elapsed) = match state {
                    crate::scheduler::states::CardState::Normal(
                        crate::scheduler::states::NormalState::Learning(state),
                    ) => (
                        state.memory_state.map(Into::into),
                        if state.scheduled_secs == 0 {
                            0.0
                        } else {
                            state.scheduled_secs as f32 / 86_400.0
                        },
                    ),
                    crate::scheduler::states::CardState::Normal(
                        crate::scheduler::states::NormalState::Review(state),
                    ) => (
                        state.memory_state.map(Into::into),
                        state.scheduled_days as f32,
                    ),
                    crate::scheduler::states::CardState::Normal(
                        crate::scheduler::states::NormalState::Relearning(state),
                    ) => (
                        state.learning.memory_state.map(Into::into),
                        if state.learning.scheduled_secs == 0 {
                            state.review.scheduled_days as f32
                        } else {
                            state.learning.scheduled_secs as f32 / 86_400.0
                        },
                    ),
                    crate::scheduler::states::CardState::Normal(
                        crate::scheduler::states::NormalState::New(_),
                    )
                    | crate::scheduler::states::CardState::Filtered(_) => (None, 0.0),
                };
                let ctx = make_ctx(memory_state, days_elapsed)?;
                Ok(state.next_states(&ctx))
            };
        let ctx = make_ctx(None, 0.0)?;
        let states = LearnState {
            remaining_steps: u32::from(!config.inner.learn_steps.is_empty()),
            scheduled_secs: 0,
            elapsed_secs: 0,
            memory_state: None,
        }
        .next_states(&ctx);
        let base = self.describe_next_states(&states)?;
        let again_followup_labels =
            self.describe_next_states(&next_followup_states(&states.again)?)?;
        let good_followup_labels =
            self.describe_next_states(&next_followup_states(&states.good)?)?;
        Ok(generic::StringList {
            vals: vec![
                base[0].clone(),
                base[1].clone(),
                base[2].clone(),
                base[3].clone(),
                again_followup_labels[2].clone(),
                again_followup_labels[0].clone(),
                good_followup_labels[0].clone(),
                good_followup_labels[2].clone(),
            ],
        })
    }

    fn evaluate_params(
        &mut self,
        input: scheduler::EvaluateParamsRequest,
    ) -> Result<scheduler::EvaluateParamsResponse> {
        let model_version = health_check_model_version(input.fsrs_version);
        let ret = self.evaluate_params(
            &input.search,
            input.search_for_training.as_deref(),
            input.ignore_revlogs_before_ms.into(),
            input.num_of_relearning_steps as usize,
            model_version,
            input.include_same_day_reviews,
            input.include_same_day_reviews_for_training,
        )?;
        Ok(scheduler::EvaluateParamsResponse {
            log_loss: ret.log_loss,
            rmse_bins: ret.rmse_bins,
        })
    }

    fn evaluate_params_legacy(
        &mut self,
        input: scheduler::EvaluateParamsLegacyRequest,
    ) -> Result<scheduler::EvaluateParamsResponse> {
        let ret = self.evaluate_params_legacy(
            &input.params,
            &input.search,
            input.ignore_revlogs_before_ms.into(),
            input.include_same_day_reviews,
        )?;
        Ok(scheduler::EvaluateParamsResponse {
            log_loss: ret.log_loss,
            rmse_bins: ret.rmse_bins,
        })
    }

    fn get_optimal_retention_parameters(
        &mut self,
        input: scheduler::GetOptimalRetentionParametersRequest,
    ) -> Result<scheduler::GetOptimalRetentionParametersResponse> {
        let revlogs = self
            .search_cards_into_table(&input.search, SortMode::NoOrder)?
            .col
            .storage
            .get_revlog_entries_for_searched_cards_in_card_order()?;
        let simulator_config = self.get_optimal_retention_parameters(revlogs)?;
        Ok(GetOptimalRetentionParametersResponse {
            deck_size: simulator_config.deck_size as u32,
            learn_span: simulator_config.learn_span as u32,
            max_cost_perday: simulator_config.max_cost_perday,
            max_ivl: simulator_config.max_ivl,
            first_rating_prob: simulator_config.first_rating_prob.to_vec(),
            review_rating_prob: simulator_config.review_rating_prob.to_vec(),
            loss_aversion: 1.0,
            learn_limit: simulator_config.learn_limit as u32,
            review_limit: simulator_config.review_limit as u32,
            learning_step_transitions: simulator_config
                .learning_step_transitions
                .iter()
                .flatten()
                .cloned()
                .collect(),
            relearning_step_transitions: simulator_config
                .relearning_step_transitions
                .iter()
                .flatten()
                .cloned()
                .collect(),
            state_rating_costs: simulator_config
                .state_rating_costs
                .iter()
                .flatten()
                .cloned()
                .collect(),
            learning_step_count: simulator_config.learning_step_count as u32,
            relearning_step_count: simulator_config.relearning_step_count as u32,
        })
    }

    fn compute_memory_state(&mut self, input: cards::CardId) -> Result<ComputeMemoryStateResponse> {
        self.compute_memory_state(input.into())
    }

    fn get_fsrs_preset_for_card(
        &mut self,
        input: cards::CardId,
    ) -> Result<FsrsPresetForCardResponse> {
        let card_id = input.into();
        let card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
        let preset = self.fsrs_preset_for_card(&card)?;
        Ok(fsrs_preset_to_proto(preset))
    }

    fn fuzz_delta(&mut self, input: FuzzDeltaRequest) -> Result<FuzzDeltaResponse> {
        Ok(FuzzDeltaResponse {
            delta_days: self.get_fuzz_delta(input.card_id.into(), input.interval)?,
        })
    }

    fn fsrs_current_retrievability(
        &mut self,
        input: FsrsCurrentRetrievabilityRequest,
    ) -> Result<FsrsCurrentRetrievabilityResponse> {
        Ok(FsrsCurrentRetrievabilityResponse {
            retrievability: self.fsrs_current_retrievability_for_card(
                input.card_id.into(),
                input.stability,
                input.elapsed_days,
            )?,
        })
    }

    fn fsrs_next_interval(
        &mut self,
        input: FsrsNextIntervalRequest,
    ) -> Result<FsrsNextIntervalResponse> {
        Ok(FsrsNextIntervalResponse {
            interval: self.fsrs_next_interval_for_card(
                input.card_id.into(),
                input.stability,
                input.desired_retention,
            )?,
        })
    }

    fn fsrs_interval_at_retrievability(
        &mut self,
        input: scheduler::FsrsIntervalAtRetrievabilityRequest,
    ) -> Result<FsrsIntervalAtRetrievabilityResponse> {
        Ok(FsrsIntervalAtRetrievabilityResponse {
            interval: self.fsrs_interval_at_retrievability_for_card(
                input.card_id.into(),
                input.stability,
                input.target_retrievability,
            )?,
        })
    }

    fn fsrs_interval_at_retrievability_batch(
        &mut self,
        input: FsrsIntervalAtRetrievabilityBatchRequest,
    ) -> Result<FsrsIntervalAtRetrievabilityBatchResponse> {
        let cards: Vec<(CardId, f32)> = input
            .items
            .iter()
            .map(|item| (item.card_id.into(), item.stability))
            .collect();
        let intervals =
            self.fsrs_interval_at_retrievability_for_cards(&cards, input.target_retrievability)?;
        let items = input
            .items
            .into_iter()
            .zip(intervals)
            .map(|(item, interval)| {
                scheduler::fsrs_interval_at_retrievability_batch_response::Item {
                    card_id: item.card_id,
                    interval,
                }
            })
            .collect();
        Ok(FsrsIntervalAtRetrievabilityBatchResponse { items })
    }

    fn fsrs_interval_at_retrievability_variable_batch(
        &mut self,
        input: FsrsIntervalAtRetrievabilityVariableBatchRequest,
    ) -> Result<FsrsIntervalAtRetrievabilityVariableBatchResponse> {
        let cards: Vec<(CardId, f32, f32)> = input
            .items
            .iter()
            .map(|item| {
                (
                    item.card_id.into(),
                    item.stability,
                    item.target_retrievability,
                )
            })
            .collect();
        let intervals = self.fsrs_interval_at_retrievability_for_card_targets(&cards)?;
        let items = input
            .items
            .into_iter()
            .zip(intervals)
            .map(|(item, interval)| {
                scheduler::fsrs_interval_at_retrievability_variable_batch_response::Item {
                    request_index: item.request_index,
                    interval,
                }
            })
            .collect();
        Ok(FsrsIntervalAtRetrievabilityVariableBatchResponse { items })
    }

    fn fsrs_desired_retention_for_intervals_batch(
        &mut self,
        input: FsrsDesiredRetentionForIntervalsBatchRequest,
    ) -> Result<FsrsDesiredRetentionForIntervalsBatchResponse> {
        let cards: Vec<(CardId, f32)> = input
            .items
            .iter()
            .map(|item| (item.card_id.into(), item.desired_retention))
            .collect();
        let targets = self.fsrs_desired_retention_for_intervals(&cards)?;
        let items = input
            .items
            .into_iter()
            .zip(targets)
            .map(|(item, target)| {
                scheduler::fsrs_desired_retention_for_intervals_batch_response::Item {
                    request_index: item.request_index,
                    interval_target_desired_retention: target.interval_target_desired_retention,
                    dynamic_desired_retentions: target
                        .dynamic_desired_retentions
                        .map(|retentions| retentions.to_vec())
                        .unwrap_or_default(),
                    dynamic_desired_retention_enabled: target.dynamic_desired_retention_enabled,
                }
            })
            .collect();
        Ok(FsrsDesiredRetentionForIntervalsBatchResponse { items })
    }

    fn fsrs_interval_at_retrievability_by_config_batch(
        &mut self,
        input: FsrsIntervalAtRetrievabilityByConfigBatchRequest,
    ) -> Result<FsrsIntervalAtRetrievabilityByConfigBatchResponse> {
        let configs: Vec<(DeckConfigId, f32)> = input
            .items
            .iter()
            .map(|item| (DeckConfigId(item.config_id), item.stability))
            .collect();
        let intervals = self
            .fsrs_interval_at_retrievability_for_configs(&configs, input.target_retrievability)?;
        let items = input
            .items
            .into_iter()
            .zip(intervals)
            .map(|(item, interval)| {
                scheduler::fsrs_interval_at_retrievability_by_config_batch_response::Item {
                    request_index: item.request_index,
                    interval,
                }
            })
            .collect();
        Ok(FsrsIntervalAtRetrievabilityByConfigBatchResponse { items })
    }
}

fn selected_short_term_with_steps_for_preview(requested: Option<bool>, stored: bool) -> bool {
    requested.unwrap_or(stored)
}

fn fsrs_preset_to_proto(preset: FsrsPreset) -> FsrsPresetForCardResponse {
    let (
        fsrs_dynamic_desired_retention_enabled,
        fsrs_dynamic_desired_retention_params,
        fsrs_dynamic_desired_retention_weights,
        fsrs_dynamic_desired_retention_avg_drs,
        fsrs_dynamic_desired_retention_min,
        fsrs_dynamic_desired_retention_max,
        fsrs_dynamic_desired_retention_fsrs_eq_weights,
        fsrs_dynamic_desired_retention_fsrs_eq_drs,
        fsrs_dynamic_desired_retention_fixed_target_weights,
        fsrs_dynamic_desired_retention_fixed_target_drs,
        fsrs_dynamic_desired_retention_clamp,
    ) = if let Some(dynamic_dr) = preset.dynamic_desired_retention {
        let (weights, avg_drs): (Vec<_>, Vec<_>) = dynamic_dr.calibration().iter().copied().unzip();
        let (fsrs_eq_weights, fsrs_eq_drs): (Vec<_>, Vec<_>) = dynamic_dr
            .fsrs_equivalent_calibration()
            .iter()
            .copied()
            .unzip();
        let (fixed_target_weights, fixed_target_drs): (Vec<_>, Vec<_>) = dynamic_dr
            .fixed_target_calibration()
            .iter()
            .copied()
            .unzip();
        (
            true,
            dynamic_dr.policy_params().to_vec(),
            weights,
            avg_drs,
            dynamic_dr.retention_min(),
            dynamic_dr.retention_max(),
            fsrs_eq_weights,
            fsrs_eq_drs,
            fixed_target_weights,
            fixed_target_drs,
            dynamic_dr.clamp_target(),
        )
    } else {
        (
            false,
            vec![],
            vec![],
            vec![],
            0.0,
            0.0,
            vec![],
            vec![],
            vec![],
            vec![],
            false,
        )
    };

    FsrsPresetForCardResponse {
        id: match preset.id {
            FsrsPresetId::DeckConfig(id) => id.0.to_string(),
            FsrsPresetId::Addon(id) => id,
        },
        name: preset.name,
        fsrs_version: preset.fsrs_version as i32,
        params: preset.params,
        desired_retention: preset.desired_retention,
        historical_retention: preset.historical_retention,
        ignore_revlogs_before_date: preset.ignore_revlogs_before_date,
        fsrs_dynamic_desired_retention_enabled,
        fsrs_dynamic_desired_retention_params,
        fsrs_dynamic_desired_retention_weights,
        fsrs_dynamic_desired_retention_avg_drs,
        fsrs_dynamic_desired_retention_min,
        fsrs_dynamic_desired_retention_max,
        fsrs_dynamic_desired_retention_fsrs_eq_weights,
        fsrs_dynamic_desired_retention_fsrs_eq_drs,
        fsrs_dynamic_desired_retention_fixed_target_weights,
        fsrs_dynamic_desired_retention_fixed_target_drs,
        fsrs_dynamic_desired_retention_clamp,
    }
}

fn health_check_model_version(fsrs_version: i32) -> ComputeParametersVersion {
    match FsrsVersion::try_from(fsrs_version).unwrap_or(FsrsVersion::Seven) {
        FsrsVersion::Seven => ComputeParametersVersion::Fsrs7,
        FsrsVersion::Six | FsrsVersion::Five | FsrsVersion::Four => ComputeParametersVersion::Fsrs6,
    }
}

impl crate::services::BackendSchedulerService for Backend {
    fn compute_fsrs_params_from_items(
        &self,
        req: scheduler::ComputeFsrsParamsFromItemsRequest,
    ) -> Result<scheduler::ComputeFsrsParamsResponse> {
        let fsrs_items = req.items.len() as u32;
        let params = compute_parameters(ComputeParametersInput {
            train_set: req.items.into_iter().map(fsrs_item_proto_to_fsrs).collect(),
            card_ids: None,
            progress: None,
            enable_short_term: true,
            enable_sched_penalties: true,
            model_version: ComputeParametersVersion::default(),
            num_relearning_steps: None,
        })?;
        Ok(ComputeFsrsParamsResponse {
            params,
            fsrs_items,
            health_check_passed: None,
            fsrs_dynamic_desired_retention_params: Vec::new(),
            fsrs_dynamic_desired_retention_weights: Vec::new(),
            fsrs_dynamic_desired_retention_avg_drs: Vec::new(),
            fsrs_dynamic_desired_retention_fsrs_eq_weights: Vec::new(),
            fsrs_dynamic_desired_retention_fsrs_eq_drs: Vec::new(),
            fsrs_dynamic_desired_retention_fixed_target_weights: Vec::new(),
            fsrs_dynamic_desired_retention_fixed_target_drs: Vec::new(),
            fsrs_dynamic_desired_retention_min: 0.0,
            fsrs_dynamic_desired_retention_max: 0.0,
        })
    }

    fn fsrs_benchmark(
        &self,
        req: scheduler::FsrsBenchmarkRequest,
    ) -> Result<scheduler::FsrsBenchmarkResponse> {
        let train_set = req
            .train_set
            .into_iter()
            .map(fsrs_item_proto_to_fsrs)
            .collect();
        let params = benchmark(ComputeParametersInput {
            train_set,
            card_ids: None,
            progress: None,
            enable_short_term: true,
            enable_sched_penalties: true,
            model_version: ComputeParametersVersion::default(),
            num_relearning_steps: None,
        });
        Ok(FsrsBenchmarkResponse { params })
    }

    fn export_dataset(&self, req: scheduler::ExportDatasetRequest) -> Result<()> {
        self.with_col(|col| {
            col.export_dataset(
                req.min_entries.try_into().unwrap(),
                req.target_path.as_ref(),
            )
        })
    }
}

fn fsrs_item_proto_to_fsrs(item: anki_proto::scheduler::FsrsItem) -> FSRSItem {
    FSRSItem {
        reviews: item
            .reviews
            .into_iter()
            .map(fsrs_review_proto_to_fsrs)
            .collect(),
    }
}

fn fsrs_review_proto_to_fsrs(review: anki_proto::scheduler::FsrsReview) -> FSRSReview {
    FSRSReview {
        delta_t: review.delta_t as f32,
        rating: review.rating,
    }
}

#[cfg(test)]
mod tests {
    use fsrs::ComputeParametersVersion;

    use super::fsrs_preset_to_proto;
    use super::health_check_model_version;
    use super::selected_short_term_with_steps_for_preview;
    use super::FsrsVersion;
    use crate::scheduler::fsrs::dynamic_desired_retention::DynamicDesiredRetention;
    use crate::scheduler::fsrs::dynamic_desired_retention::DynamicDesiredRetentionFields;
    use crate::scheduler::fsrs::preset::FsrsPreset;
    use crate::scheduler::fsrs::preset::FsrsPresetId;

    #[test]
    fn new_card_interval_preview_prefers_explicit_toggle_when_provided() {
        assert!(selected_short_term_with_steps_for_preview(
            Some(true),
            false
        ));
        assert!(!selected_short_term_with_steps_for_preview(
            Some(false),
            true
        ));
    }

    #[test]
    fn new_card_interval_preview_falls_back_to_stored_toggle() {
        assert!(selected_short_term_with_steps_for_preview(None, true));
        assert!(!selected_short_term_with_steps_for_preview(None, false));
    }

    #[test]
    fn health_check_uses_selected_fsrs6_or_fsrs7_family() {
        assert_eq!(
            health_check_model_version(FsrsVersion::Seven as i32),
            ComputeParametersVersion::Fsrs7
        );
        assert_eq!(
            health_check_model_version(FsrsVersion::Six as i32),
            ComputeParametersVersion::Fsrs6
        );
    }

    #[test]
    fn health_check_maps_fsrs4_and_fsrs5_to_fsrs6_family() {
        assert_eq!(
            health_check_model_version(FsrsVersion::Five as i32),
            ComputeParametersVersion::Fsrs6
        );
        assert_eq!(
            health_check_model_version(FsrsVersion::Four as i32),
            ComputeParametersVersion::Fsrs6
        );
    }

    #[test]
    fn fsrs_preset_response_exposes_dynamic_dr_fields() -> crate::prelude::Result<()> {
        let dynamic_dr = DynamicDesiredRetention::from_fields(DynamicDesiredRetentionFields {
            policy_params: vec![1.0; 15],
            calibration_weights: vec![0.0, 15.0],
            calibration_avg_drs: vec![0.9, 0.8],
            fsrs_equivalent_weights: vec![0.0, 15.0],
            fsrs_equivalent_drs: vec![0.91, 0.82],
            fixed_target_weights: vec![16.0, 4.0],
            fixed_target_drs: vec![0.8, 0.9],
            retention_min: 0.7,
            retention_max: 0.95,
            clamp_target: true,
            max_interval_days: None,
        })?;
        let response = fsrs_preset_to_proto(FsrsPreset {
            id: FsrsPresetId::Addon("addon:test".into()),
            name: "Test".into(),
            fsrs_version: FsrsVersion::Seven,
            params: vec![0.0; 21],
            desired_retention: 0.86,
            dynamic_desired_retention: Some(dynamic_dr),
            historical_retention: 0.9,
            ignore_revlogs_before_date: "2024-01-01".into(),
        });

        assert_eq!(response.id, "addon:test");
        assert_eq!(response.fsrs_version, FsrsVersion::Seven as i32);
        assert!(response.fsrs_dynamic_desired_retention_enabled);
        assert_eq!(
            response.fsrs_dynamic_desired_retention_params,
            vec![1.0; 15]
        );
        assert_eq!(
            response.fsrs_dynamic_desired_retention_weights,
            vec![0.0, 15.0]
        );
        assert_eq!(
            response.fsrs_dynamic_desired_retention_avg_drs,
            vec![0.9, 0.8]
        );
        assert_eq!(
            response.fsrs_dynamic_desired_retention_fsrs_eq_weights,
            vec![0.0, 15.0]
        );
        assert_eq!(
            response.fsrs_dynamic_desired_retention_fsrs_eq_drs,
            vec![0.91, 0.82]
        );
        assert_eq!(
            response.fsrs_dynamic_desired_retention_fixed_target_weights,
            vec![16.0, 4.0]
        );
        assert_eq!(
            response.fsrs_dynamic_desired_retention_fixed_target_drs,
            vec![0.8, 0.9]
        );
        assert_eq!(response.fsrs_dynamic_desired_retention_min, 0.7);
        assert_eq!(response.fsrs_dynamic_desired_retention_max, 0.95);
        assert!(response.fsrs_dynamic_desired_retention_clamp);
        Ok(())
    }
}
