// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;

use anki_proto::scheduler::ComputeMemoryStateResponse;
use fsrs::FSRSItem;
use fsrs::MemoryState;
use fsrs::DEFAULT_PARAMETERS;
use fsrs::FSRS;
use fsrs::FSRS5_DEFAULT_DECAY;
use fsrs::FSRS6_DEFAULT_DECAY;
use itertools::Either;
use itertools::Itertools;

use super::rescheduler::Rescheduler;
use crate::card::CardQueue;
use crate::card::CardType;
use crate::card::FsrsMemoryState;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::scheduler::answering::fsrs_elapsed_days;
use crate::scheduler::answering::get_fuzz_seed;
use crate::scheduler::fsrs::params::include_same_day_for_params;
use crate::scheduler::fsrs::params::reviews_for_fsrs;
use crate::scheduler::fsrs::params::Params;
use crate::scheduler::fsrs::params_fingerprint;
use crate::scheduler::fsrs::round_to_two_decimals;
use crate::scheduler::states::fuzz::with_review_fuzz;
use crate::scheduler::states::fuzz::ReviewFuzzConfig;
use crate::search::Negated;
use crate::search::SearchNode;
use crate::search::StateKind;

const S_MIN: f32 = 0.0001;
const S_MAX: f32 = 36_500.0;
const D_MIN: f32 = 1.0;
const D_MAX: f32 = 10.0;

#[derive(Debug, Clone, Default)]
pub struct ComputeMemoryProgress {
    pub current_cards: u32,
    pub total_cards: u32,
    pub preset_name: String,
    pub current_preset: u32,
    pub total_presets: u32,
    pub rescheduling: bool,
    pub saving: bool,
    pub presets: Vec<ComputeMemoryPresetProgress>,
}

#[derive(Debug, Clone, Default)]
pub struct ComputeMemoryPresetProgress {
    pub name: String,
    pub current_cards: u32,
    pub total_cards: u32,
    pub finished: bool,
    pub rescheduling: bool,
    pub saving: bool,
}

struct ComputedMemoryState {
    memory_state: Option<FsrsMemoryState>,
    desired_retention: f32,
    decay: f32,
}

#[derive(Debug, Clone, PartialEq)]
pub struct FsrsDesiredRetentionForInterval {
    pub interval_target_desired_retention: f32,
    pub dynamic_desired_retention_enabled: bool,
    pub dynamic_desired_retentions: Option<[f32; 4]>,
}

/// Helper function to determine the appropriate decay value based on FSRS
/// parameters
pub(crate) fn get_decay_from_params(params: &[f32]) -> f32 {
    if params.is_empty() {
        FSRS6_DEFAULT_DECAY // default decay for FSRS-6
    } else if params.len() < 21 {
        FSRS5_DEFAULT_DECAY // default decay for FSRS-4.5 and FSRS-5
    } else if params.len() >= 35 {
        // FSRS-7 uses a mixture curve; expose the first decay component for
        // compatibility with existing callers that expect a single decay value.
        params[27]
    } else {
        params[20]
    }
}

pub(crate) fn fsrs_current_retrievability_for_params(
    params: &[f32],
    stability: f32,
    elapsed_days: f32,
) -> Result<f32> {
    fsrs_current_retrievability_scalar_for_params(params, stability, elapsed_days)
}

pub(crate) fn fsrs_current_retrievability_scalar_for_params(
    params: &[f32],
    stability: f32,
    elapsed_days: f32,
) -> Result<f32> {
    if params.len() != 35 {
        let fsrs = FSRS::new(params)?;
        return Ok(fsrs.current_retrievability(
            MemoryState {
                stability,
                difficulty: 5.0,
            },
            elapsed_days.max(0.0),
        ));
    }
    let retrievability = fsrs7_current_retrievability_scalar(params, stability, elapsed_days);
    require!(retrievability.is_finite(), "invalid FSRS parameter values");
    Ok(retrievability)
}

fn fsrs7_current_retrievability_scalar(params: &[f32], stability: f32, elapsed_days: f32) -> f32 {
    let stability = stability.max(S_MIN);
    let t_over_s = elapsed_days.max(0.0) / stability;

    let decay1 = -params[27];
    let decay2 = -params[28];
    let base1 = params[29];
    let base2 = params[30];

    let factor1 = base1.powf(1.0 / decay1) - 1.0;
    let factor2 = base2.powf(1.0 / decay2) - 1.0;
    let r1 = (1.0 + factor1 * t_over_s).powf(decay1);
    let r2 = (1.0 + factor2 * t_over_s).powf(decay2);

    let weight1 = params[31] * stability.powf(-params[33]);
    let weight2 = params[32] * stability.powf(params[34]);

    (weight1 * r1 + weight2 * r2) / (weight1 + weight2)
}

pub(crate) fn fsrs_next_interval_for_params(
    params: &[f32],
    stability: f32,
    desired_retention: f32,
) -> Result<f32> {
    let fsrs = FSRS::new(params)?;
    Ok(fsrs.next_interval(Some(stability), desired_retention.clamp(0.0001, 0.9999), 0))
}

pub(crate) fn fsrs_interval_at_retrievability_for_params(
    params: &[f32],
    stability: f32,
    target_retrievability: f32,
) -> Result<f32> {
    let fsrs = FSRS::new(params)?;
    Ok(fsrs.interval_at_retrievability(
        MemoryState {
            stability,
            difficulty: 5.0,
        },
        target_retrievability.clamp(0.0001, 0.9999),
    ))
}

pub(crate) fn fsrs_memory_state_for_params(
    params: &[f32],
    memory_state: MemoryState,
) -> Result<FsrsMemoryState> {
    let fsrs = FSRS::new(params)?;
    Ok(fsrs_memory_state_for_fsrs(&fsrs, memory_state))
}

pub(crate) fn fsrs_memory_state_for_fsrs(
    fsrs: &FSRS,
    memory_state: MemoryState,
) -> FsrsMemoryState {
    let stability = fsrs.interval_at_retrievability(memory_state, 0.9);
    FsrsMemoryState {
        stability,
        stability_internal: memory_state.stability,
        difficulty: memory_state.difficulty,
    }
}

fn log_expm1(x: f64) -> f64 {
    if x > 50.0 {
        x
    } else {
        x.exp_m1().ln()
    }
}

/// Compute memory state from SM-2 fields with a stable path for FSRS-7 params.
///
/// FSRS-7 no longer uses a single legacy decay index at position 20, and some
/// valid FSRS-7 parameter sets can cause overflow in the legacy conversion path
/// used for older parameterizations. This helper keeps older behavior for
/// legacy params and applies a numerically-stable conversion when params have
/// 35 values.
pub(crate) fn memory_state_from_sm2_with_params(
    fsrs: &FSRS,
    params: &[f32],
    ease_factor: f32,
    interval: f32,
    sm2_retention: f32,
) -> Result<MemoryState> {
    let params = if params.is_empty() {
        &DEFAULT_PARAMETERS[..]
    } else {
        params
    };

    if params.len() != 35 {
        return Ok(fsrs.memory_state_from_sm2(ease_factor, interval, sm2_retention)?);
    }

    let interval = interval.max(S_MIN);
    let retention = sm2_retention.clamp(0.70, 0.9999);
    let decay = -get_decay_from_params(params).max(0.001);
    let stability = if (retention - 0.9).abs() < 1e-6 {
        interval
    } else {
        let inv_decay = 1.0f64 / decay as f64;
        let target = retention as f64;
        let x = inv_decay * 0.9f64.ln();
        let y = inv_decay * target.ln();
        let ratio = (log_expm1(x) - log_expm1(y)).clamp(-80.0, 80.0).exp();
        (interval as f64 * ratio) as f32
    }
    .clamp(S_MIN, S_MAX);

    // FSRS-7 does not use the same scalar decay regime as legacy models.
    // When only SM-2 fields are available, keep difficulty neutral.
    let difficulty = 5.0f32.clamp(D_MIN, D_MAX);

    Ok(MemoryState {
        stability,
        difficulty,
    })
}

#[derive(Debug)]
pub(crate) struct UpdateMemoryStateRequest {
    pub params: Params,
    pub preset_desired_retention: f32,
    pub historical_retention: f32,
    pub max_interval: u32,
    pub review_fuzz_config: ReviewFuzzConfig,
    pub reschedule: bool,
    pub deck_desired_retention: HashMap<DeckId, f32>,
}

pub(crate) struct UpdateMemoryStateEntry {
    pub req: Option<UpdateMemoryStateRequest>,
    pub search: SearchNode,
    pub ignore_before: TimestampMillis,
    pub preset_name: String,
    pub current_preset: u32,
    pub total_presets: u32,
}

trait ChunkIntoVecs<T> {
    fn chunk_into_vecs(&mut self, chunk_size: usize) -> impl Iterator<Item = Vec<T>>;
}

impl<T> ChunkIntoVecs<T> for Vec<T> {
    fn chunk_into_vecs(&mut self, chunk_size: usize) -> impl Iterator<Item = Vec<T>> {
        std::iter::from_fn(move || {
            (!self.is_empty()).then(|| self.drain(..chunk_size.min(self.len())).collect())
        })
    }
}

impl Collection {
    /// For each provided set of params, locate cards with the provided search,
    /// and update their memory state.
    /// Should be called inside a transaction.
    /// If Params are None, it means the user disabled FSRS, and the existing
    /// memory state should be removed.
    pub(crate) fn update_memory_state(
        &mut self,
        entries: Vec<UpdateMemoryStateEntry>,
    ) -> Result<()> {
        let timing = self.timing_today()?;
        let usn = self.usn()?;
        let mut preset_progress = entries
            .iter()
            .map(|entry| ComputeMemoryPresetProgress {
                name: entry.preset_name.clone(),
                rescheduling: entry.req.as_ref().is_some_and(|req| req.reschedule),
                ..Default::default()
            })
            .collect_vec();

        for (
            entry_index,
            UpdateMemoryStateEntry {
                req,
                search,
                ignore_before,
                preset_name,
                current_preset,
                total_presets,
            },
        ) in entries.into_iter().enumerate()
        {
            let rescheduling = req.as_ref().is_some_and(|req| req.reschedule);
            if let Some(progress) = preset_progress.get_mut(entry_index) {
                progress.name.clone_from(&preset_name);
                progress.rescheduling = rescheduling;
            }

            let search =
                SearchBuilder::all([search.into(), SearchNode::State(StateKind::New).negated()]);
            let revlog = self.revlog_for_srs(search)?;

            let Some(req) = &req else {
                let items = fsrs_items_for_memory_states(
                    &FSRS::new(&[])?,
                    &[],
                    revlog,
                    timing.next_day_at,
                    0.9,
                    ignore_before,
                )?;

                Self::prepare_memory_progress_entry(&mut preset_progress, entry_index, items.len());
                let on_updated_card = self.create_progress_closure(
                    preset_progress.clone(),
                    entry_index,
                    preset_name.clone(),
                    current_preset,
                    total_presets,
                    false,
                )?;

                // clear FSRS data if FSRS is disabled
                self.clear_fsrs_data_for_cards(
                    items.iter().map(|(card_id, _)| *card_id),
                    usn,
                    on_updated_card,
                )?;
                Self::finish_memory_progress_entry(&mut preset_progress, entry_index, items.len());
                continue;
            };
            let fsrs = FSRS::new(&req.params)?;
            let params = &req.params[..];
            let last_revlog_info = req.reschedule.then(|| get_last_revlog_info(&revlog));
            let items = fsrs_items_for_memory_states(
                &fsrs,
                params,
                revlog,
                timing.next_day_at,
                req.historical_retention,
                ignore_before,
            )?;

            Self::prepare_memory_progress_entry(&mut preset_progress, entry_index, items.len());
            let mut on_updated_card = self.create_progress_closure(
                preset_progress.clone(),
                entry_index,
                preset_name,
                current_preset,
                total_presets,
                req.reschedule,
            )?;
            let item_count = items.len();

            let (items, cards_without_items): (Vec<(CardId, FsrsItemForMemoryState)>, Vec<CardId>) =
                items.into_iter().partition_map(|(card_id, item)| {
                    if let Some(item) = item {
                        Either::Left((card_id, item))
                    } else {
                        Either::Right(card_id)
                    }
                });

            let decay = get_decay_from_params(&req.params);

            // Store decay and desired retention in the card so that add-ons, card info,
            // stats and browser search/sorts don't need to access the deck config.
            // Unlike memory states, scheduler doesn't use decay and dr stored in the card.
            let set_decay_and_desired_retention = move |card: &mut Card| {
                let deck_id = card.original_or_current_deck_id();

                let desired_retention = *req
                    .deck_desired_retention
                    .get(&deck_id)
                    .unwrap_or(&req.preset_desired_retention);

                card.desired_retention = Some(desired_retention);
                card.decay = Some(decay);
            };

            self.update_memory_state_for_itemless_cards(
                cards_without_items,
                set_decay_and_desired_retention,
                usn,
                &mut on_updated_card,
            )?;

            let mut rescheduler =
                if req.reschedule && self.get_config_bool(BoolKey::LoadBalancerEnabled) {
                    Some(Rescheduler::new(self)?)
                } else {
                    None
                };

            let reschedule =
                move |card: &mut Card, collection: &mut Self, fsrs: &FSRS| -> Result<()> {
                    // we are rescheduling
                    let Some(last_revlog_info) = &last_revlog_info else {
                        return Ok(());
                    };

                    // we have a last review time for the card
                    let Some(last_info) = last_revlog_info.get(&card.id) else {
                        return Ok(());
                    };
                    let Some(last_review) = &last_info.last_reviewed_at else {
                        return Ok(());
                    };
                    // the card isn't in (re)learning or suspended
                    if !(card.ctype == CardType::Review && card.queue != CardQueue::Suspended) {
                        return Ok(());
                    };

                    let deck = collection
                        .get_deck(card.original_or_current_deck_id())?
                        .or_not_found(card.original_or_current_deck_id())?;
                    let deckconfig_id = deck.config_id().unwrap();
                    // reschedule it
                    let days_elapsed = timing.next_day_at.elapsed_days_since(*last_review) as i32;
                    let original_interval = card.interval;
                    let min_interval = |interval: u32| {
                        let previous_interval = last_info.previous_interval.unwrap_or(0);
                        if interval > previous_interval {
                            // interval grew; don't allow fuzzed interval to
                            // be less than previous+1
                            previous_interval + 1
                        } else {
                            // interval shrunk; don't restrict negative fuzz
                            0
                        }
                        .max(1)
                    };
                    let interval = fsrs.next_interval(
                        Some(
                            card.memory_state
                                .expect("We set it before this function is called")
                                .stability,
                        ),
                        card.desired_retention
                            .expect("We set it before this function is called"),
                        0,
                    );
                    card.interval = rescheduler
                        .as_mut()
                        .and_then(|r| {
                            r.find_interval(
                                interval,
                                min_interval(interval as u32),
                                req.max_interval,
                                days_elapsed as u32,
                                deckconfig_id,
                                get_fuzz_seed(card, true),
                            )
                        })
                        .unwrap_or_else(|| {
                            with_review_fuzz(
                                card.get_fuzz_factor(true),
                                interval,
                                min_interval(interval as u32),
                                req.max_interval,
                                req.review_fuzz_config,
                            )
                        });
                    let due = if card.original_due != 0 {
                        &mut card.original_due
                    } else {
                        &mut card.due
                    };
                    let new_due =
                        (timing.days_elapsed as i32) - days_elapsed + card.interval as i32;
                    if let Some(rescheduler) = &mut rescheduler {
                        rescheduler.update_due_cnt_per_day(*due, new_due, deckconfig_id);
                    }
                    *due = new_due;
                    // Add a rescheduled revlog entry
                    collection.log_rescheduled_review(card, original_interval, usn)?;

                    Ok(())
                };

            self.update_memory_state_for_cards_with_items(
                items,
                &fsrs,
                set_decay_and_desired_retention,
                reschedule,
                usn,
                on_updated_card,
            )?;
            Self::finish_memory_progress_entry(&mut preset_progress, entry_index, item_count);
        }
        Ok(())
    }

    fn prepare_memory_progress_entry(
        preset_progress: &mut [ComputeMemoryPresetProgress],
        entry_index: usize,
        total_cards: usize,
    ) {
        if let Some(progress) = preset_progress.get_mut(entry_index) {
            progress.total_cards = total_cards as u32;
            progress.finished = total_cards == 0;
        }
    }

    fn finish_memory_progress_entry(
        preset_progress: &mut [ComputeMemoryPresetProgress],
        entry_index: usize,
        total_cards: usize,
    ) {
        if let Some(progress) = preset_progress.get_mut(entry_index) {
            progress.current_cards = total_cards as u32;
            progress.total_cards = total_cards as u32;
            progress.finished = true;
        }
    }

    fn set_active_memory_progress(progress: &mut ComputeMemoryProgress, active_index: usize) {
        if let Some(active) = progress.presets.get(active_index) {
            progress.current_cards = active.current_cards;
            progress.total_cards = active.total_cards;
            progress.preset_name.clone_from(&active.name);
            progress.rescheduling = active.rescheduling;
            progress.saving = active.saving;
        }
    }

    fn create_progress_closure(
        &self,
        presets: Vec<ComputeMemoryPresetProgress>,
        active_index: usize,
        preset_name: String,
        current_preset: u32,
        total_presets: u32,
        rescheduling: bool,
    ) -> Result<impl FnMut() -> Result<()>> {
        let mut progress = self.new_progress_handler::<ComputeMemoryProgress>();
        progress.update(false, |s| {
            s.presets = presets;
            s.preset_name = preset_name;
            s.current_preset = current_preset;
            s.total_presets = total_presets;
            s.rescheduling = rescheduling;
            s.saving = false;
            Self::set_active_memory_progress(s, active_index);
        })?;
        let on_updated_card = move || {
            progress.update(true, |p| {
                if let Some(active) = p.presets.get_mut(active_index) {
                    active.current_cards = active
                        .current_cards
                        .saturating_add(1)
                        .min(active.total_cards);
                    active.finished = active.current_cards >= active.total_cards;
                }
                Self::set_active_memory_progress(p, active_index);
            })
        };
        Ok(on_updated_card)
    }

    fn clear_fsrs_data_for_cards(
        &mut self,
        cards: impl Iterator<Item = CardId>,
        usn: Usn,
        mut on_updated_card: impl FnMut() -> Result<()>,
    ) -> Result<()> {
        for card_id in cards {
            let mut card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
            let original = card.clone();
            card.clear_fsrs_data();
            self.update_card_inner(&mut card, original, usn)?;
            on_updated_card()?
        }
        Ok(())
    }

    fn update_memory_state_for_itemless_cards(
        &mut self,
        cards: Vec<CardId>,
        mut set_decay_and_desired_retention: impl FnMut(&mut Card),
        usn: Usn,
        mut on_updated_card: impl FnMut() -> Result<()>,
    ) -> Result<()> {
        for card_id in cards {
            let mut card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
            let original = card.clone();
            set_decay_and_desired_retention(&mut card);
            card.memory_state = None;
            self.update_card_inner(&mut card, original, usn)?;
            on_updated_card()?;
        }
        Ok(())
    }

    fn update_memory_state_for_cards_with_items(
        &mut self,
        items: Vec<(CardId, FsrsItemForMemoryState)>,
        fsrs: &FSRS,
        mut set_decay_and_desired_retention: impl FnMut(&mut Card),
        mut maybe_reschedule_card: impl FnMut(&mut Card, &mut Self, &FSRS) -> Result<()>,
        usn: Usn,
        mut on_updated_card: impl FnMut() -> Result<()>,
    ) -> Result<()> {
        const FSRS_BATCH_SIZE: usize = 1000;

        let mut to_update = Vec::new();
        let mut fsrs_items = Vec::new();
        let mut starting_states = Vec::new();

        for (card_id, item) in items.into_iter() {
            to_update.push(card_id);
            fsrs_items.push(item.item);
            starting_states.push(item.starting_state);
        }

        // fsrs.memory_state_batch is O(nm) where n is the number of cards and m is the
        // max review count between all items. Therefore we want to pass batches
        // to fsrs.memory_state_batch where the review count is relatively even.
        let mut p = permutation::sort_unstable_by_key(&fsrs_items, |item| item.reviews.len());
        p.apply_slice_in_place(&mut to_update);
        p.apply_slice_in_place(&mut fsrs_items);
        p.apply_slice_in_place(&mut starting_states);

        for ((to_update, fsrs_items), starting_states) in to_update
            .chunk_into_vecs(FSRS_BATCH_SIZE)
            .zip_eq(fsrs_items.chunk_into_vecs(FSRS_BATCH_SIZE))
            .zip_eq(starting_states.chunk_into_vecs(FSRS_BATCH_SIZE))
        {
            let memory_states = fsrs.memory_state_batch(fsrs_items, starting_states)?;

            for (card_id, memory_state) in to_update.into_iter().zip_eq(memory_states) {
                let mut card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
                let original = card.clone();
                set_decay_and_desired_retention(&mut card);
                card.memory_state = Some(fsrs_memory_state_for_fsrs(fsrs, memory_state));
                maybe_reschedule_card(&mut card, self, fsrs)?;
                self.update_card_inner(&mut card, original, usn)?;
                on_updated_card()?;
            }
        }
        Ok(())
    }

    fn fsrs_params_for_card_id(&mut self, card_id: CardId) -> Result<Vec<f32>> {
        let card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
        Ok(self.fsrs_preset_for_card(&card)?.params)
    }

    fn fsrs_params_for_config_id(&mut self, config_id: DeckConfigId) -> Result<Vec<f32>> {
        let config = self
            .storage
            .get_deck_config(config_id)?
            .or_not_found(config_id)?;
        Ok(config.fsrs_params().to_vec())
    }

    pub fn fsrs_current_retrievability_for_card(
        &mut self,
        card_id: CardId,
        stability: f32,
        elapsed_days: f32,
    ) -> Result<f32> {
        let params = self.fsrs_params_for_card_id(card_id)?;
        fsrs_current_retrievability_for_params(&params, stability, elapsed_days)
    }

    pub fn fsrs_next_interval_for_card(
        &mut self,
        card_id: CardId,
        stability: f32,
        desired_retention: f32,
    ) -> Result<f32> {
        let params = self.fsrs_params_for_card_id(card_id)?;
        fsrs_next_interval_for_params(&params, stability, desired_retention)
    }

    pub fn fsrs_interval_at_retrievability_for_card(
        &mut self,
        card_id: CardId,
        stability: f32,
        target_retrievability: f32,
    ) -> Result<f32> {
        let params = self.fsrs_params_for_card_id(card_id)?;
        fsrs_interval_at_retrievability_for_params(&params, stability, target_retrievability)
    }

    pub fn fsrs_interval_at_retrievability_for_cards(
        &mut self,
        cards: &[(CardId, f32)],
        target_retrievability: f32,
    ) -> Result<Vec<f32>> {
        let cards: Vec<(CardId, f32, f32)> = cards
            .iter()
            .map(|(card_id, stability)| (*card_id, *stability, target_retrievability))
            .collect();
        self.fsrs_interval_at_retrievability_for_card_targets(&cards)
    }

    pub fn fsrs_interval_at_retrievability_for_card_targets(
        &mut self,
        cards: &[(CardId, f32, f32)],
    ) -> Result<Vec<f32>> {
        let card_ids: Vec<CardId> = cards.iter().map(|(card_id, _, _)| *card_id).collect();
        let card_rows = self.all_cards_for_ids(&card_ids, false)?;
        let presets_by_card = self.fsrs_presets_for_cards(&card_rows)?;
        let mut fsrs_by_preset_id = HashMap::new();
        let mut intervals = Vec::with_capacity(cards.len());

        for (card_id, stability, target_retrievability) in cards {
            let preset = presets_by_card.get(card_id).or_not_found(*card_id)?;
            if !fsrs_by_preset_id.contains_key(&preset.id) {
                fsrs_by_preset_id.insert(preset.id.clone(), FSRS::new(&preset.params)?);
            }
            let fsrs = fsrs_by_preset_id
                .get(&preset.id)
                .expect("FSRS instance inserted");
            intervals.push(fsrs.interval_at_retrievability(
                MemoryState {
                    stability: *stability,
                    difficulty: 5.0,
                },
                target_retrievability.clamp(0.0001, 0.9999),
            ));
        }

        Ok(intervals)
    }

    pub fn fsrs_desired_retention_for_intervals(
        &mut self,
        cards: &[(CardId, f32)],
    ) -> Result<Vec<FsrsDesiredRetentionForInterval>> {
        let card_ids: Vec<CardId> = cards
            .iter()
            .map(|(card_id, _desired_retention)| *card_id)
            .collect();
        let card_rows = self.all_cards_for_ids(&card_ids, false)?;
        let card_by_id: HashMap<CardId, _> = card_rows.iter().map(|card| (card.id, card)).collect();
        let presets_by_card = self.fsrs_presets_for_cards(&card_rows)?;
        let timing = self.timing_today()?;
        let now = TimestampSecs::now();
        let fsrs_enabled = self.get_config_bool(BoolKey::Fsrs);
        let mut fsrs_by_preset_id = HashMap::new();
        let mut targets = Vec::with_capacity(cards.len());

        for (card_id, desired_retention) in cards {
            let card = card_by_id.get(card_id).or_not_found(*card_id)?;
            let preset = presets_by_card.get(card_id).or_not_found(*card_id)?;
            let dynamic_desired_retention_enabled = preset.dynamic_desired_retention.is_some();
            let mut target = FsrsDesiredRetentionForInterval {
                interval_target_desired_retention: *desired_retention,
                dynamic_desired_retention_enabled,
                dynamic_desired_retentions: None,
            };

            if fsrs_enabled {
                if let Some(dynamic_dr) = preset.dynamic_desired_retention.as_ref() {
                    if let Some(scheduling_desired_retention) =
                        dynamic_dr.scheduling_target(*desired_retention)?
                    {
                        if !fsrs_by_preset_id.contains_key(&preset.id) {
                            fsrs_by_preset_id.insert(preset.id.clone(), FSRS::new(&preset.params)?);
                        }
                        let fsrs = fsrs_by_preset_id
                            .get(&preset.id)
                            .expect("FSRS instance inserted");
                        let last_review_time = if card.last_review_time.is_some() {
                            card.last_review_time
                        } else {
                            self.storage.time_of_last_review(card.id)?
                        };
                        let days_elapsed = last_review_time
                            .map(|last_review_time| {
                                fsrs_elapsed_days(card, last_review_time, timing.next_day_at, now)
                            })
                            .unwrap_or_default();
                        let dynamic_states = dynamic_dr.next_states(
                            fsrs,
                            card.memory_state.map(Into::into),
                            scheduling_desired_retention,
                            days_elapsed,
                        )?;
                        target.interval_target_desired_retention =
                            dynamic_states.desired_retentions[2];
                        target.dynamic_desired_retentions = Some(dynamic_states.desired_retentions);
                    }
                }
            }

            targets.push(target);
        }

        Ok(targets)
    }

    pub fn fsrs_interval_at_retrievability_for_configs(
        &mut self,
        configs: &[(DeckConfigId, f32)],
        target_retrievability: f32,
    ) -> Result<Vec<f32>> {
        let mut params_by_config: HashMap<DeckConfigId, Vec<f32>> = HashMap::new();
        let mut intervals = Vec::with_capacity(configs.len());
        for (config_id, stability) in configs {
            let params = if let Some(params) = params_by_config.get(config_id) {
                params
            } else {
                let params = self.fsrs_params_for_config_id(*config_id)?;
                params_by_config.insert(*config_id, params);
                params_by_config
                    .get(config_id)
                    .expect("config params inserted")
            };
            intervals.push(fsrs_interval_at_retrievability_for_params(
                params,
                *stability,
                target_retrievability,
            )?);
        }
        Ok(intervals)
    }

    pub fn compute_memory_state(&mut self, card_id: CardId) -> Result<ComputeMemoryStateResponse> {
        let card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
        let computed = self.compute_memory_state_for_card(&card, false)?;
        Ok(ComputeMemoryStateResponse {
            state: computed.memory_state.map(Into::into),
            desired_retention: computed.desired_retention,
            decay: computed.decay,
        })
    }

    pub(crate) fn recompute_fsrs_data_for_card(&mut self, card: &mut Card) -> Result<()> {
        if card.ctype == CardType::New {
            return Ok(());
        }

        let computed = self.compute_memory_state_for_card(card, true)?;
        card.memory_state = computed.memory_state;
        card.desired_retention = Some(computed.desired_retention);
        card.decay = Some(computed.decay);
        Ok(())
    }

    fn compute_memory_state_for_card(
        &mut self,
        card: &Card,
        infer_from_current_card_state: bool,
    ) -> Result<ComputedMemoryState> {
        let fsrs_preset = self.fsrs_preset_for_card(card)?;
        let desired_retention = fsrs_preset.desired_retention;
        let historical_retention = fsrs_preset.historical_retention;
        let params = &fsrs_preset.params;
        let decay = get_decay_from_params(params);
        let fsrs = FSRS::new(params)?;
        let mut revlog = self.storage.get_revlog_entries_for_card(card.id)?;
        let revlog_count = revlog.len();
        revlog.sort_unstable_by_key(|entry| entry.id);
        let item = fsrs_item_for_memory_state(
            &fsrs,
            params,
            revlog,
            self.timing_today()?.next_day_at,
            historical_retention,
            fsrs_preset.ignore_revlogs_before_ms()?,
        )?;
        let memory_state = if item.is_some() || infer_from_current_card_state {
            let mut card = card.clone();
            card.set_memory_state(&fsrs, params, item, historical_retention)?;
            card.memory_state
        } else {
            None
        };
        tracing::debug!(
            card_id = card.id.0,
            preset_id = ?fsrs_preset.id,
            preset_name = fsrs_preset.name.as_str(),
            fsrs_version = ?fsrs_preset.fsrs_version,
            params_len = params.len(),
            params_fingerprint = format_args!("{:016x}", params_fingerprint(params)),
            desired_retention = round_to_two_decimals(desired_retention),
            historical_retention = round_to_two_decimals(historical_retention),
            decay = round_to_two_decimals(decay),
            revlog_count,
            computed_s90 = memory_state.map(|state| round_to_two_decimals(state.stability)),
            computed_internal_stability = memory_state
                .map(|state| round_to_two_decimals(state.stability_internal)),
            computed_difficulty = memory_state.map(|state| round_to_two_decimals(state.difficulty)),
            "computed FSRS memory state"
        );

        Ok(ComputedMemoryState {
            memory_state,
            desired_retention,
            decay,
        })
    }
}

impl Card {
    pub(crate) fn set_memory_state(
        &mut self,
        fsrs: &FSRS,
        params: &[f32],
        item: Option<FsrsItemForMemoryState>,
        historical_retention: f32,
    ) -> Result<()> {
        let memory_state = if let Some(i) = item {
            Some(fsrs.memory_state(i.item, i.starting_state)?)
        } else if self.ctype == CardType::New || self.interval == 0 {
            None
        } else {
            // no valid revlog entries; infer state from current card state
            Some(memory_state_from_sm2_with_params(
                fsrs,
                params,
                self.ease_factor(),
                self.interval as f32,
                historical_retention,
            )?)
        };
        self.memory_state = memory_state.map(|state| fsrs_memory_state_for_fsrs(fsrs, state));
        Ok(())
    }
}

#[derive(Debug, Clone)]
pub(crate) struct FsrsItemForMemoryState {
    pub item: FSRSItem,
    /// When revlogs have been truncated, this stores the initial state at first
    /// review
    pub starting_state: Option<MemoryState>,
    pub filtered_revlogs: Vec<RevlogEntry>,
}

/// Like [fsrs_item_for_memory_state], but for updating multiple cards at once.
pub(crate) fn fsrs_items_for_memory_states(
    fsrs: &FSRS,
    params: &[f32],
    revlogs: Vec<RevlogEntry>,
    next_day_at: TimestampSecs,
    historical_retention: f32,
    ignore_revlogs_before: TimestampMillis,
) -> Result<Vec<(CardId, Option<FsrsItemForMemoryState>)>> {
    revlogs
        .into_iter()
        .chunk_by(|r| r.cid)
        .into_iter()
        .map(|(card_id, group)| {
            Ok((
                card_id,
                fsrs_item_for_memory_state(
                    fsrs,
                    params,
                    group.collect(),
                    next_day_at,
                    historical_retention,
                    ignore_revlogs_before,
                )?,
            ))
        })
        .collect()
}

pub(crate) struct LastRevlogInfo {
    /// Used to determine the actual elapsed time between the last time the user
    /// reviewed the card and now, so that we can determine an accurate period
    /// when the card has subsequently been rescheduled to a different day.
    pub(crate) last_reviewed_at: Option<TimestampSecs>,
    /// The interval before the latest review. Used to prevent fuzz from going
    /// backwards when rescheduling the card
    pub(crate) previous_interval: Option<u32>,
}

/// Return a map of cards to info about last review.
pub(crate) fn get_last_revlog_info(revlogs: &[RevlogEntry]) -> HashMap<CardId, LastRevlogInfo> {
    let mut out = HashMap::new();
    revlogs
        .iter()
        .chunk_by(|r| r.cid)
        .into_iter()
        .for_each(|(card_id, group)| {
            let mut last_reviewed_at = None;
            let mut previous_interval = None;
            for e in group.into_iter() {
                if e.has_rating_and_affects_scheduling() {
                    last_reviewed_at = Some(e.id.as_secs());
                    previous_interval = if e.last_interval >= 0 && e.button_chosen > 1 {
                        Some(e.last_interval as u32)
                    } else {
                        None
                    };
                } else if e.is_reset() {
                    last_reviewed_at = None;
                    previous_interval = None;
                }
            }
            out.insert(
                card_id,
                LastRevlogInfo {
                    last_reviewed_at,
                    previous_interval,
                },
            );
        });
    out
}

/// When calculating memory state, only the last FSRSItem is required. If the
/// revlog is non-empty and no learning steps have been detected (indicative of
/// a truncated revlog), we return the starting state inferred from the first
/// revlog entry, so that the first review is not treated as if started from
/// scratch.
pub(crate) fn fsrs_item_for_memory_state(
    fsrs: &FSRS,
    params: &[f32],
    entries: Vec<RevlogEntry>,
    next_day_at: TimestampSecs,
    historical_retention: f32,
    ignore_revlogs_before: TimestampMillis,
) -> Result<Option<FsrsItemForMemoryState>> {
    struct FirstReview {
        interval: f32,
        ease_factor: f32,
    }
    if let Some(mut output) = reviews_for_fsrs(
        entries,
        next_day_at,
        false,
        ignore_revlogs_before,
        include_same_day_for_params(params),
    ) {
        let mut item = output.fsrs_items.pop().unwrap().1;
        if output.revlogs_complete {
            Ok(Some(FsrsItemForMemoryState {
                item,
                starting_state: None,
                filtered_revlogs: output.filtered_revlogs,
            }))
        } else if let Some(first_user_grade) = output.filtered_revlogs.first() {
            // the revlog has been truncated, but not fully
            let first_review = FirstReview {
                interval: first_user_grade.interval.max(1) as f32,
                ease_factor: if first_user_grade.ease_factor == 0 {
                    2500
                } else {
                    first_user_grade.ease_factor
                } as f32
                    / 1000.0,
            };
            let mut starting_state = memory_state_from_sm2_with_params(
                fsrs,
                params,
                first_review.ease_factor,
                first_review.interval,
                historical_retention,
            )?;
            // if the ease factor is less than 1.1, the revlog entry is generated by FSRS
            if first_review.ease_factor <= 1.1 {
                starting_state.difficulty = (first_review.ease_factor - 0.1) * 9.0 + 1.0;
            }
            // remove the first review because it has been converted to the starting state
            item.reviews.remove(0);
            Ok(Some(FsrsItemForMemoryState {
                item,
                starting_state: Some(starting_state),
                filtered_revlogs: output.filtered_revlogs,
            }))
        } else {
            // only manual and rescheduled revlogs; treat like empty
            Ok(None)
        }
    } else {
        // no revlogs (new card or caused by ignore_revlogs_before or deleted revlogs)
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use anki_proto::deck_config::deck_configs_for_update::current_deck::Limits;
    use anki_proto::deck_config::UpdateDeckConfigsMode;
    use fsrs::MemoryState;
    use fsrs::DEFAULT_PARAMETERS;

    use super::*;
    use crate::deckconfig::FsrsVersion;
    use crate::deckconfig::UpdateDeckConfigsRequest;
    use crate::revlog::RevlogId;
    use crate::revlog::RevlogReviewKind;
    use crate::scheduler::fsrs::params::tests::convert;
    use crate::scheduler::fsrs::params::tests::revlog;
    use crate::scheduler::fsrs::preset::AddonFsrsPreset;
    use crate::scheduler::fsrs::preset::AddonFsrsVersion;
    use crate::scheduler::fsrs::preset::FsrsPresetOverlay;
    use crate::scheduler::fsrs::preset::FsrsPresetRule;
    use crate::scheduler::fsrs::preset::FSRS_PRESET_OVERLAY_CONFIG_KEY;
    use crate::search::SortMode;

    /// Floating point precision can vary between platforms, and each FSRS
    /// update tends to result in small changes to these numbers, so we
    /// round them.
    fn assert_int_eq(actual: Option<FsrsMemoryState>, expected: Option<FsrsMemoryState>) {
        let actual = actual.unwrap();
        let expected = expected.unwrap();
        assert_eq!(actual.stability.round(), expected.stability.round());
        assert_eq!(actual.difficulty.round(), expected.difficulty.round());
    }

    fn make_review_card(col: &mut Collection, note_id: NoteId, stability: f32) -> Result<CardId> {
        let mut card = col
            .storage
            .all_cards_of_note(note_id)?
            .into_iter()
            .next()
            .or_not_found(note_id)?;
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 10;
        card.due = col.timing_today()?.days_elapsed as i32;
        card.memory_state = Some(FsrsMemoryState {
            stability,
            stability_internal: stability,
            difficulty: 5.0,
        });
        card.last_review_time = Some(TimestampSecs::now().adding_secs(-5 * 86_400));
        col.storage.update_card(&card)?;
        Ok(card.id)
    }

    fn set_selected_fsrs_params_for_deck(
        col: &mut Collection,
        deck_id: DeckId,
        version: FsrsVersion,
        params: Vec<f32>,
    ) -> Result<DeckConfigId> {
        let output = col.get_deck_configs_for_update(deck_id)?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: deck_id,
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            card_state_customizer: String::new(),
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            apply_all_parent_limits: false,
            fsrs: true,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs_learning_queues_disabled: false,
            fsrs_reschedule: false,
            fsrs_health_check: true,
            review_fuzz_config: Default::default(),
        };
        match version {
            FsrsVersion::Six => {
                input.configs[0].inner.fsrs_version = FsrsVersion::Six as i32;
                input.configs[0].inner.fsrs_params_6 = params;
            }
            FsrsVersion::Seven => {
                input.configs[0].inner.fsrs_version = FsrsVersion::Seven as i32;
                input.configs[0].inner.fsrs_params_7 = params;
            }
            _ => unreachable!("unsupported FSRS version in test helper"),
        }
        col.update_deck_configs(input)?;
        let deck = col.get_deck(deck_id)?.or_not_found(deck_id)?;
        Ok(DeckConfigId(deck.normal()?.config_id))
    }

    fn assign_new_fsrs_config_to_deck(
        col: &mut Collection,
        deck_id: DeckId,
        version: FsrsVersion,
        params: Vec<f32>,
    ) -> Result<DeckConfigId> {
        let output = col.get_deck_configs_for_update(deck_id)?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: deck_id,
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            card_state_customizer: String::new(),
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            apply_all_parent_limits: false,
            fsrs: true,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs_learning_queues_disabled: false,
            fsrs_reschedule: false,
            fsrs_health_check: true,
            review_fuzz_config: Default::default(),
        };
        let mut new_config = input.configs[0].clone();
        new_config.id = DeckConfigId(0);
        match version {
            FsrsVersion::Six => {
                new_config.inner.fsrs_version = FsrsVersion::Six as i32;
                new_config.inner.fsrs_params_6 = params;
            }
            FsrsVersion::Seven => {
                new_config.inner.fsrs_version = FsrsVersion::Seven as i32;
                new_config.inner.fsrs_params_7 = params;
            }
            _ => unreachable!("unsupported FSRS version in test helper"),
        }
        input.configs.push(new_config);
        col.update_deck_configs(input)?;
        let deck = col.get_deck(deck_id)?.or_not_found(deck_id)?;
        Ok(DeckConfigId(deck.normal()?.config_id))
    }

    #[test]
    fn bypassed_learning_is_handled() -> Result<()> {
        // cards without any learning steps due to truncated history still have memory
        // state calculated
        let fsrs = FSRS::new(&[]).unwrap();
        let item = fsrs_item_for_memory_state(
            &fsrs,
            &[],
            vec![
                RevlogEntry {
                    ease_factor: 2500,
                    interval: 100,
                    ..revlog(RevlogReviewKind::Review, 99)
                },
                revlog(RevlogReviewKind::Review, 0),
            ],
            TimestampSecs::now(),
            0.9,
            0.into(),
        )?
        .unwrap();
        assert_int_eq(
            item.starting_state.map(Into::into),
            Some(FsrsMemoryState {
                stability: 100.0,
                stability_internal: 100.0,
                difficulty: 5.003576,
            }),
        );
        let mut card = Card {
            reps: 1,
            ..Default::default()
        };
        card.set_memory_state(&fsrs, &[], Some(item), 0.9)?;
        assert_int_eq(
            card.memory_state,
            Some(FsrsMemoryState {
                stability: 248.9251,
                stability_internal: 248.9251,
                difficulty: 4.9938006,
            }),
        );
        // cards with a single review-type entry also get memory states from revlog
        // rather than card states
        let item = fsrs_item_for_memory_state(
            &fsrs,
            &[],
            vec![RevlogEntry {
                ease_factor: 2500,
                interval: 100,
                ..revlog(RevlogReviewKind::Review, 100)
            }],
            TimestampSecs::now(),
            0.9,
            0.into(),
        )?
        .unwrap();
        assert!(item.item.reviews.is_empty());
        card.set_memory_state(&fsrs, &[], Some(item), 0.9)?;
        assert_int_eq(
            card.memory_state,
            Some(FsrsMemoryState {
                stability: 100.0,
                stability_internal: 100.0,
                difficulty: 5.003576,
            }),
        );
        Ok(())
    }

    #[test]
    fn zero_history_is_handled() -> Result<()> {
        // when the history is empty, no items are produced
        assert_eq!(convert(&[], false), None);
        // but memory state should still be inferred, by using the card's current state
        let mut card = Card {
            ctype: CardType::Review,
            interval: 100,
            ease_factor: 1300,
            reps: 1,
            ..Default::default()
        };
        card.set_memory_state(&FSRS::new(&[]).unwrap(), &[], None, 0.9)?;
        assert_int_eq(
            card.memory_state,
            Some(
                MemoryState {
                    stability: 99.999954,
                    difficulty: 5.0,
                }
                .into(),
            ),
        );
        Ok(())
    }

    fn reconstructed_same_day_delta(params: &[f32]) -> Result<f32> {
        let fsrs = FSRS::new(params)?;
        let next_day_at = TimestampSecs(86_400 * 1000);
        let base = (next_day_at.0 - 86_400 + 3_600) * 1000;
        let item = fsrs_item_for_memory_state(
            &fsrs,
            params,
            vec![
                RevlogEntry {
                    id: RevlogId(base),
                    ..revlog(RevlogReviewKind::Learning, 1)
                },
                RevlogEntry {
                    id: RevlogId(base + 3_600_000),
                    ..revlog(RevlogReviewKind::Review, 1)
                },
            ],
            next_day_at,
            0.9,
            0.into(),
        )?
        .unwrap();
        Ok(item.item.reviews[1].delta_t)
    }

    fn relearning_card_1779209293223_revlogs() -> Vec<RevlogEntry> {
        let ratings = [1, 1, 3, 3, 3, 3, 3, 3, 1, 3, 3, 3, 3, 3, 3, 3];
        let elapsed_millis = [
            454_026, 1_544, 4_430, 2_798, 1_981, 3_691, 10_876, 80_546, 1_799, 36_528, 2_281,
            7_919, 265_132, 2_144, 13_635,
        ];
        let mut id = 1_779_281_655_000;
        let mut revlogs = Vec::with_capacity(ratings.len());
        for (index, rating) in ratings.into_iter().enumerate() {
            if index > 0 {
                id += elapsed_millis[index - 1];
            }
            revlogs.push(RevlogEntry {
                id: RevlogId(id),
                button_chosen: rating,
                review_kind: RevlogReviewKind::Learning,
                ..Default::default()
            });
        }
        revlogs
    }

    #[test]
    fn fsrs7_memory_state_reconstruction_preserves_reported_relearning_elapsed_time() -> Result<()>
    {
        let fsrs = FSRS::new(&DEFAULT_PARAMETERS)?;
        let revlogs = relearning_card_1779209293223_revlogs();
        let next_day_at = TimestampSecs(1_779_292_800);
        let reconstructed = fsrs_item_for_memory_state(
            &fsrs,
            &DEFAULT_PARAMETERS,
            revlogs.clone(),
            next_day_at,
            0.9,
            0.into(),
        )?
        .unwrap();
        let legacy = reviews_for_fsrs(revlogs, next_day_at, false, 0.into(), false)
            .unwrap()
            .fsrs_items
            .pop()
            .unwrap()
            .1;

        let reconstructed_elapsed_secs = reconstructed
            .item
            .reviews
            .iter()
            .map(|review| review.delta_t)
            .sum::<f32>()
            * 86_400.0;
        let legacy_elapsed_secs = legacy
            .reviews
            .iter()
            .map(|review| review.delta_t)
            .sum::<f32>()
            * 86_400.0;

        assert!((reconstructed_elapsed_secs - 889.33).abs() < 0.01);
        assert_eq!(legacy_elapsed_secs, 0.0);

        let reconstructed_state = fsrs.memory_state(reconstructed.item, None)?;
        let legacy_state = fsrs.memory_state(legacy, None)?;
        assert!(reconstructed_state.stability > legacy_state.stability);
        Ok(())
    }

    #[test]
    fn fsrs7_memory_state_reconstruction_uses_card_1779209293223_real_timestamps() -> Result<()> {
        let params = vec![
            0.164_769_28,
            1.781_043_3,
            3.960_628_7,
            17.227_514,
            5.775_701_5,
            0.350_562_72,
            3.297_959,
            2.193_530_6,
            0.315_878_87,
            1.232_677_5,
            0.383919,
            0.006_497_408_7,
            0.687_791_05,
            0.0,
            0.541_600_17,
            1.3993783,
            0.961_712_66,
            0.372_928_4,
            3.647_044_7,
            0.443_311_54,
            0.001,
            0.37068215,
            2.665_900_5,
            0.563_425_4,
            1.305_368_8,
            2.5,
            0.910_621_2,
            0.134_659_71,
            0.534_766_55,
            0.632_104_46,
            0.978_705_9,
            0.194_363_62,
            0.696_230_1,
            0.121_837_68,
            0.37259683,
        ];
        let fsrs = FSRS::new(&params)?;
        let revlogs: Vec<_> = [
            (1_779_280_455_399, 1, -794, 631),
            (1_779_280_909_425, 1, -76, 969),
            (1_779_280_910_969, 3, -76, 964),
            (1_779_280_915_399, 3, -76, 958),
            (1_779_280_918_197, 3, -76, 953),
            (1_779_280_920_178, 3, -76, 948),
            (1_779_280_923_869, 3, -76, 942),
            (1_779_280_934_745, 3, -76, 937),
            (1_779_281_015_291, 1, -9, 1050),
            (1_779_281_017_090, 3, -9, 1044),
            (1_779_281_053_618, 3, -49, 1038),
            (1_779_281_055_899, 3, -49, 1032),
            (1_779_281_063_818, 3, -49, 1025),
            (1_779_281_328_950, 3, -248, 1019),
            (1_779_281_331_094, 3, -248, 1013),
            (1_779_281_344_729, 3, -248, 1008),
            (1_779_281_802_797, 3, -572, 1002),
            (1_779_281_809_803, 4, 1, 960),
        ]
        .into_iter()
        .map(|(id, button_chosen, interval, ease_factor)| RevlogEntry {
            id: RevlogId(id),
            button_chosen,
            interval,
            ease_factor,
            review_kind: RevlogReviewKind::Learning,
            ..Default::default()
        })
        .collect();
        let item = fsrs_item_for_memory_state(
            &fsrs,
            &params,
            revlogs.clone(),
            TimestampSecs(1_779_292_800),
            0.9,
            0.into(),
        )?
        .unwrap();
        let elapsed_secs = item
            .item
            .reviews
            .iter()
            .map(|review| review.delta_t)
            .sum::<f32>()
            * 86_400.0;
        let internal = fsrs.memory_state(item.item, item.starting_state)?;
        let s90 = fsrs.interval_at_retrievability(internal, 0.9);

        assert!((elapsed_secs - 1354.404).abs() < 0.01);
        assert!((internal.stability - 0.14337839).abs() < 1e-6);
        assert!((s90 - 0.029642101).abs() < 1e-6);
        assert!((internal.difficulty - 8.743349).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs7_memory_state_reconstruction_uses_fractional_same_day_delta() -> Result<()> {
        let delta = reconstructed_same_day_delta(&DEFAULT_PARAMETERS)?;
        assert!((delta - (1.0 / 24.0)).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs6_memory_state_reconstruction_keeps_integer_same_day_delta() -> Result<()> {
        let delta = reconstructed_same_day_delta(&DEFAULT_PARAMETERS[0..21])?;
        assert_eq!(delta, 0.0);
        Ok(())
    }

    #[test]
    fn minimum_stability_uses_fsrs_floor() {
        assert_eq!(S_MIN, 0.0001);
    }

    #[test]
    fn fsrs7_sm2_conversion_handles_small_legacy_decay_slot() -> Result<()> {
        let params = vec![
            0.1558, 3.0107, 6.2423, 22.3570, 5.6837, 0.5279, 2.2999, 1.9751, 0.2886, 1.2884,
            0.8518, 0.0149, 0.7189, 0.6297, 0.3777, 2.8929, 0.9740, 0.5923, 3.6757, 0.8299, 0.0010,
            0.6994, 2.6457, 0.5673, 1.3138, 2.5067, 0.9955, 0.0499, 0.4071, 0.5686, 0.8969, 0.2210,
            0.8008, 0.0147, 0.1591,
        ];
        let fsrs = FSRS::new(&params)?;
        let state = memory_state_from_sm2_with_params(&fsrs, &params, 2.5, 100.0, 0.9)?;
        assert!(state.stability.is_finite());
        assert!(state.difficulty.is_finite());
        Ok(())
    }

    #[test]
    fn fsrs_math_helpers_match_inference_fsrs7() -> Result<()> {
        let params = DEFAULT_PARAMETERS.to_vec();
        let stability = 14.2;
        let elapsed_days = 21.0;
        let desired_retention = 0.88;
        let target_retrievability = 0.9;

        let expected = FSRS::new(&params)?.current_retrievability(
            MemoryState {
                stability,
                difficulty: 5.0,
            },
            elapsed_days,
        );
        let actual = fsrs_current_retrievability_for_params(&params, stability, elapsed_days)?;
        assert!((actual - expected).abs() < 1e-6);

        let expected_interval =
            FSRS::new(&params)?.next_interval(Some(stability), desired_retention, 0);
        let actual_interval = fsrs_next_interval_for_params(&params, stability, desired_retention)?;
        assert!((actual_interval - expected_interval).abs() < 1e-6);

        let expected_interval_at_target = FSRS::new(&params)?.interval_at_retrievability(
            MemoryState {
                stability,
                difficulty: 5.0,
            },
            target_retrievability,
        );
        let actual_interval_at_target =
            fsrs_interval_at_retrievability_for_params(&params, stability, target_retrievability)?;
        assert!((actual_interval_at_target - expected_interval_at_target).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_math_helpers_match_inference_fsrs6() -> Result<()> {
        let params = DEFAULT_PARAMETERS[0..21].to_vec();
        let stability = 9.5;
        let elapsed_days = 12.0;
        let desired_retention = 0.9;
        let target_retrievability = 0.9;

        let expected = FSRS::new(&params)?.current_retrievability(
            MemoryState {
                stability,
                difficulty: 5.0,
            },
            elapsed_days,
        );
        let actual = fsrs_current_retrievability_for_params(&params, stability, elapsed_days)?;
        assert!((actual - expected).abs() < 1e-6);

        let expected_interval =
            FSRS::new(&params)?.next_interval(Some(stability), desired_retention, 0);
        let actual_interval = fsrs_next_interval_for_params(&params, stability, desired_retention)?;
        assert!((actual_interval - expected_interval).abs() < 1e-6);

        let expected_interval_at_target = FSRS::new(&params)?.interval_at_retrievability(
            MemoryState {
                stability,
                difficulty: 5.0,
            },
            target_retrievability,
        );
        let actual_interval_at_target =
            fsrs_interval_at_retrievability_for_params(&params, stability, target_retrievability)?;
        assert!((actual_interval_at_target - expected_interval_at_target).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_batch_matches_singular_calls() -> Result<()> {
        let mut col = Collection::new();
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, DeckId(1))?;

        let mut card_ids = col.search_cards("", SortMode::NoOrder)?;
        card_ids.sort();
        let target_retrievability = 0.9;
        let cards = vec![(card_ids[0], 12.0), (card_ids[1], 42.0)];

        let batch = col.fsrs_interval_at_retrievability_for_cards(&cards, target_retrievability)?;
        let singular_1 =
            col.fsrs_interval_at_retrievability_for_card(card_ids[0], 12.0, target_retrievability)?;
        let singular_2 =
            col.fsrs_interval_at_retrievability_for_card(card_ids[1], 42.0, target_retrievability)?;

        assert_eq!(batch.len(), 2);
        assert!((batch[0] - singular_1).abs() < 1e-6);
        assert!((batch[1] - singular_2).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_batch_uses_overlay_presets() -> Result<()> {
        let mut col = Collection::new();
        let deck_params = DEFAULT_PARAMETERS.to_vec();
        set_selected_fsrs_params_for_deck(
            &mut col,
            DeckId(1),
            FsrsVersion::Seven,
            deck_params.clone(),
        )?;
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut tagged_note = nt.new_note();
        let mut fallback_note = nt.new_note();
        col.add_note(&mut tagged_note, DeckId(1))?;
        col.add_note(&mut fallback_note, DeckId(1))?;
        col.add_tags_to_notes(&[tagged_note.id], "medical")?;

        let addon_params = vec![1.0; 21];
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:medical".into(),
                    name: "Medical".into(),
                    fsrs_version: AddonFsrsVersion::Six,
                    params: addon_params.clone(),
                    desired_retention: 0.81,
                    historical_retention: 0.71,
                    ignore_revlogs_before_date: String::new(),
                    ..Default::default()
                }],
                rules: vec![crate::scheduler::fsrs::preset::FsrsPresetRule {
                    search: "tag:medical".into(),
                    preset_id: "addon:test:medical".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        let cids = col.search_cards("", SortMode::NoOrder)?;
        let cards = col.all_cards_for_ids(&cids, false)?;
        let tagged_card = cards
            .iter()
            .find(|card| card.note_id == tagged_note.id)
            .or_not_found(tagged_note.id)?;
        let fallback_card = cards
            .iter()
            .find(|card| card.note_id == fallback_note.id)
            .or_not_found(fallback_note.id)?;
        let target_retrievability = 0.9;

        let intervals = col.fsrs_interval_at_retrievability_for_cards(
            &[(tagged_card.id, 12.0), (fallback_card.id, 24.0)],
            target_retrievability,
        )?;
        let expected_tagged =
            fsrs_interval_at_retrievability_for_params(&addon_params, 12.0, target_retrievability)?;
        let expected_fallback =
            fsrs_interval_at_retrievability_for_params(&deck_params, 24.0, target_retrievability)?;

        assert_eq!(intervals.len(), 2);
        assert!((intervals[0] - expected_tagged).abs() < 1e-6);
        assert!((intervals[1] - expected_fallback).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_variable_batch_uses_item_targets() -> Result<()> {
        let mut col = Collection::new();
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, DeckId(1))?;

        let mut card_ids = col.search_cards("", SortMode::NoOrder)?;
        card_ids.sort();
        let cards = vec![(card_ids[0], 12.0, 0.9), (card_ids[1], 12.0, 0.8)];

        let intervals = col.fsrs_interval_at_retrievability_for_card_targets(&cards)?;
        let singular_1 = col.fsrs_interval_at_retrievability_for_card(card_ids[0], 12.0, 0.9)?;
        let singular_2 = col.fsrs_interval_at_retrievability_for_card(card_ids[1], 12.0, 0.8)?;

        assert_eq!(intervals.len(), 2);
        assert!((intervals[0] - singular_1).abs() < 1e-6);
        assert!((intervals[1] - singular_2).abs() < 1e-6);
        require!(
            intervals[0] != intervals[1],
            "test requires distinct targets"
        );
        Ok(())
    }

    #[test]
    fn fsrs_desired_retention_for_intervals_matches_dynamic_scheduling_states() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, false)?;
        col.update_default_deck_config(|config| {
            config.fsrs_version = FsrsVersion::Seven as i32;
            config.fsrs_params_7 = DEFAULT_PARAMETERS.to_vec();
            config.desired_retention = 0.9;
            config.fsrs_dynamic_desired_retention_enabled = true;
            config.fsrs_dynamic_desired_retention_params = vec![0.0; 15];
            config.fsrs_dynamic_desired_retention_weights = vec![0.0, 15.0];
            config.fsrs_dynamic_desired_retention_avg_drs = vec![0.9, 0.8];
            config.fsrs_dynamic_desired_retention_min = 0.75;
            config.fsrs_dynamic_desired_retention_max = 0.95;
        });
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;
        let card_id = make_review_card(&mut col, note.id, 10.0)?;

        let batch = col.fsrs_desired_retention_for_intervals(&[(card_id, 0.9)])?;
        let states =
            col.get_scheduling_states_with_desired_retention_override(card_id, Some(0.9))?;
        let states_dynamic_retentions = states.dynamic_desired_retentions.unwrap();

        assert_eq!(batch.len(), 1);
        assert!(batch[0].dynamic_desired_retention_enabled);
        assert_eq!(
            batch[0].dynamic_desired_retentions.unwrap(),
            states_dynamic_retentions
        );
        assert!(
            (batch[0].interval_target_desired_retention - states_dynamic_retentions[2]).abs()
                < 1e-6
        );
        Ok(())
    }

    #[test]
    fn fsrs_desired_retention_for_intervals_uses_overlay_presets() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, false)?;
        col.update_default_deck_config(|config| {
            config.fsrs_version = FsrsVersion::Seven as i32;
            config.fsrs_params_7 = DEFAULT_PARAMETERS.to_vec();
            config.desired_retention = 0.65;
        });
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:medical".into(),
                    name: "Medical".into(),
                    fsrs_version: AddonFsrsVersion::Seven,
                    params: DEFAULT_PARAMETERS.to_vec(),
                    desired_retention: 0.9,
                    historical_retention: 0.9,
                    ignore_revlogs_before_date: String::new(),
                    fsrs_dynamic_desired_retention_enabled: true,
                    fsrs_dynamic_desired_retention_params: vec![0.0; 15],
                    fsrs_dynamic_desired_retention_weights: vec![0.0, 15.0],
                    fsrs_dynamic_desired_retention_avg_drs: vec![0.9, 0.8],
                    fsrs_dynamic_desired_retention_min: 0.75,
                    fsrs_dynamic_desired_retention_max: 0.95,
                    ..Default::default()
                }],
                rules: vec![FsrsPresetRule {
                    search: "tag:medical".into(),
                    preset_id: "addon:test:medical".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut tagged_note = nt.new_note();
        let mut fallback_note = nt.new_note();
        col.add_note(&mut tagged_note, DeckId(1))?;
        col.add_note(&mut fallback_note, DeckId(1))?;
        col.add_tags_to_notes(&[tagged_note.id], "medical")?;
        let tagged_card_id = make_review_card(&mut col, tagged_note.id, 10.0)?;
        let fallback_card_id = make_review_card(&mut col, fallback_note.id, 20.0)?;

        let batch = col.fsrs_desired_retention_for_intervals(&[
            (tagged_card_id, 0.9),
            (fallback_card_id, 0.9),
        ])?;
        let tagged_states =
            col.get_scheduling_states_with_desired_retention_override(tagged_card_id, Some(0.9))?;
        let tagged_dynamic_retentions = tagged_states.dynamic_desired_retentions.unwrap();

        assert_eq!(batch.len(), 2);
        assert!(batch[0].dynamic_desired_retention_enabled);
        assert_eq!(
            batch[0].dynamic_desired_retentions.unwrap(),
            tagged_dynamic_retentions
        );
        assert!(
            (batch[0].interval_target_desired_retention - tagged_dynamic_retentions[2]).abs()
                < 1e-6
        );
        assert!(!batch[1].dynamic_desired_retention_enabled);
        assert!(batch[1].dynamic_desired_retentions.is_none());
        assert!((batch[1].interval_target_desired_retention - 0.9).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_by_config_batch_uses_fsrs7_config() -> Result<()> {
        let mut col = Collection::new();
        let params_7 = DEFAULT_PARAMETERS.to_vec();
        let config_id = set_selected_fsrs_params_for_deck(
            &mut col,
            DeckId(1),
            FsrsVersion::Seven,
            params_7.clone(),
        )?;
        let target_retrievability = 0.9;
        let stability = 17.3;

        let actual = col.fsrs_interval_at_retrievability_for_configs(
            &[(config_id, stability)],
            target_retrievability,
        )?[0];
        let expected = fsrs_interval_at_retrievability_for_params(
            &params_7,
            stability,
            target_retrievability,
        )?;
        assert!((actual - expected).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_by_config_batch_uses_fsrs6_config() -> Result<()> {
        let mut col = Collection::new();
        let params_6 = DEFAULT_PARAMETERS[0..21].to_vec();
        let config_id = set_selected_fsrs_params_for_deck(
            &mut col,
            DeckId(1),
            FsrsVersion::Six,
            params_6.clone(),
        )?;
        let target_retrievability = 0.9;
        let stability = 11.2;

        let actual = col.fsrs_interval_at_retrievability_for_configs(
            &[(config_id, stability)],
            target_retrievability,
        )?[0];
        let expected = fsrs_interval_at_retrievability_for_params(
            &params_6,
            stability,
            target_retrievability,
        )?;
        assert!((actual - expected).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_by_config_batch_supports_mixed_configs() -> Result<()> {
        let mut col = Collection::new();
        let params_7 = DEFAULT_PARAMETERS.to_vec();
        let params_6 = DEFAULT_PARAMETERS[0..21].to_vec();
        let config_1 = set_selected_fsrs_params_for_deck(
            &mut col,
            DeckId(1),
            FsrsVersion::Seven,
            params_7.clone(),
        )?;
        let second_deck = col.get_or_create_normal_deck("second-config-batch")?;
        let config_2 = assign_new_fsrs_config_to_deck(
            &mut col,
            second_deck.id,
            FsrsVersion::Six,
            params_6.clone(),
        )?;
        require!(config_1 != config_2, "test requires different config ids");

        let target_retrievability = 0.9;
        let actual = col.fsrs_interval_at_retrievability_for_configs(
            &[(config_1, 20.0), (config_2, 20.0)],
            target_retrievability,
        )?;
        let expected_1 =
            fsrs_interval_at_retrievability_for_params(&params_7, 20.0, target_retrievability)?;
        let expected_2 =
            fsrs_interval_at_retrievability_for_params(&params_6, 20.0, target_retrievability)?;
        assert_eq!(actual.len(), 2);
        assert!((actual[0] - expected_1).abs() < 1e-6);
        assert!((actual[1] - expected_2).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_by_config_batch_preserves_order_with_duplicates(
    ) -> Result<()> {
        let mut col = Collection::new();
        let params_7 = DEFAULT_PARAMETERS.to_vec();
        let params_6 = DEFAULT_PARAMETERS[0..21].to_vec();
        let config_1 = set_selected_fsrs_params_for_deck(
            &mut col,
            DeckId(1),
            FsrsVersion::Seven,
            params_7.clone(),
        )?;
        let second_deck = col.get_or_create_normal_deck("second-config-duplicates")?;
        let config_2 = assign_new_fsrs_config_to_deck(
            &mut col,
            second_deck.id,
            FsrsVersion::Six,
            params_6.clone(),
        )?;
        let target_retrievability = 0.9;
        let request = vec![
            (config_2, 8.0),
            (config_1, 12.0),
            (config_2, 21.0),
            (config_1, 12.0),
        ];
        let actual =
            col.fsrs_interval_at_retrievability_for_configs(&request, target_retrievability)?;
        let expected = vec![
            fsrs_interval_at_retrievability_for_params(&params_6, 8.0, target_retrievability)?,
            fsrs_interval_at_retrievability_for_params(&params_7, 12.0, target_retrievability)?,
            fsrs_interval_at_retrievability_for_params(&params_6, 21.0, target_retrievability)?,
            fsrs_interval_at_retrievability_for_params(&params_7, 12.0, target_retrievability)?,
        ];
        assert_eq!(actual.len(), expected.len());
        for (a, e) in actual.into_iter().zip(expected) {
            assert!((a - e).abs() < 1e-6);
        }
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_by_config_batch_matches_card_helper() -> Result<()> {
        let mut col = Collection::new();
        let params_7 = DEFAULT_PARAMETERS.to_vec();
        let params_6 = DEFAULT_PARAMETERS[0..21].to_vec();
        let config_1 =
            set_selected_fsrs_params_for_deck(&mut col, DeckId(1), FsrsVersion::Seven, params_7)?;
        let second_deck = col.get_or_create_normal_deck("second-config-parity")?;
        let config_2 =
            assign_new_fsrs_config_to_deck(&mut col, second_deck.id, FsrsVersion::Six, params_6)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, second_deck.id)?;
        let mut card_ids = col.search_cards("", SortMode::NoOrder)?;
        card_ids.sort();
        let card_1 = card_ids[0];
        let card_2 = card_ids[1];

        let target_retrievability = 0.9;
        let stability_1 = 16.0;
        let stability_2 = 24.0;
        let by_config = col.fsrs_interval_at_retrievability_for_configs(
            &[(config_1, stability_1), (config_2, stability_2)],
            target_retrievability,
        )?;
        let by_card_1 = col.fsrs_interval_at_retrievability_for_card(
            card_1,
            stability_1,
            target_retrievability,
        )?;
        let by_card_2 = col.fsrs_interval_at_retrievability_for_card(
            card_2,
            stability_2,
            target_retrievability,
        )?;
        assert_eq!(by_config.len(), 2);
        assert!((by_config[0] - by_card_1).abs() < 1e-6);
        assert!((by_config[1] - by_card_2).abs() < 1e-6);
        Ok(())
    }

    mod update_memory_state {
        use super::*;

        #[test]
        fn no_req_clears_fsrs_data() -> Result<()> {
            let mut col = Collection::new();
            let nt = col.get_notetype_by_name("Basic")?.unwrap();
            let mut note1 = nt.new_note();
            col.add_note(&mut note1, DeckId(1))?;
            let mut card = col
                .storage
                .all_cards_of_note(note1.id)?
                .into_iter()
                .next()
                .unwrap();
            let card_id = card.id;
            // Make the card not new
            card.ctype = CardType::Review;
            card.interval = 1;
            // Set FSRS parameters
            card.memory_state = Some(FsrsMemoryState {
                stability: 1.0,
                stability_internal: 1.0,
                difficulty: 1.0,
            });
            card.desired_retention = Some(0.123);
            card.decay = Some(0.456);

            col.storage.update_card(&card)?;

            // Add a revlog entry so the card is found within update_memory_state
            let mut rev = revlog(RevlogReviewKind::Review, 1);
            rev.cid = card_id;
            col.storage.add_revlog_entry(&rev, false)?;

            let entry = UpdateMemoryStateEntry {
                req: None,
                search: SearchNode::WholeCollection,
                ignore_before: TimestampMillis(0),
                preset_name: "Default".to_string(),
                current_preset: 1,
                total_presets: 1,
            };
            col.transact(Op::UpdateDeckConfig, |col| {
                col.update_memory_state(vec![entry]).unwrap();
                Ok(())
            })
            .unwrap();

            let card = col.storage.get_card(card_id)?.unwrap();
            assert_eq!(card.memory_state, None);
            assert_eq!(card.desired_retention, None);
            assert_eq!(card.decay, None);

            Ok(())
        }
    }
}
