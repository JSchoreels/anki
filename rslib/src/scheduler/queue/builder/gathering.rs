// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::collections::HashSet;

use super::DueCard;
use super::NewCard;
use super::QueueBuilder;
use crate::card::Card;
use crate::deckconfig::NewCardGatherPriority;
use crate::deckconfig::ReviewCardOrder;
use crate::decks::limits::LimitKind;
use crate::prelude::*;
use crate::scheduler::queue::DueCardKind;
use crate::scheduler::rwkv::rwkv_review_candidate_metadata;
use crate::scheduler::rwkv::rwkv_review_score_eligible;
use crate::scheduler::timing::SchedTimingToday;
use crate::storage::card::NewCardSorting;

#[derive(Debug, Clone, Copy)]
struct DueCardForRetrievabilitySort {
    card: DueCard,
    counts_towards_review_limit: bool,
    interday_or_review: bool,
}

pub(super) const RWKV_REVIEW_GATHER_MIN_CHUNK_SIZE: usize = 256;

impl QueueBuilder {
    pub(super) fn gather_cards(&mut self, col: &mut Collection) -> Result<()> {
        if self.context.sort_options.uses_rwkv_review_order() {
            self.gather_intraday_learning_cards(col)?;
            self.gather_due_cards(col, DueCardKind::Learning)?;
            if self.context.uses_rwkv_review_order() {
                self.gather_review_cards_with_rwkv_scores(col)?;
            } else {
                self.gather_due_cards(col, DueCardKind::Review)?;
            }
            self.gather_new_cards(col)?;
            return Ok(());
        }

        if self.context.non_news_sorted_by_retrievability() {
            self.gather_due_non_new_cards_with_exact_retrievability(col)?;
            self.gather_new_cards(col)?;
            return Ok(());
        }

        self.gather_intraday_learning_cards(col)?;
        self.gather_due_cards(col, DueCardKind::Learning)?;
        self.gather_due_cards(col, DueCardKind::Review)?;
        self.gather_new_cards(col)?;

        Ok(())
    }

    fn gather_review_cards_with_rwkv_scores(&mut self, col: &mut Collection) -> Result<()> {
        if matches!(
            self.context.sort_options.review_order,
            ReviewCardOrder::RetrievabilityAscending | ReviewCardOrder::RetrievabilityDescending
        ) {
            self.gather_review_cards_by_rwkv_retrievability(col)
        } else {
            self.gather_review_cards_by_configured_order(col)
        }
    }

    fn gather_review_cards_by_rwkv_retrievability(&mut self, col: &mut Collection) -> Result<()> {
        if self.limits.root_limit_reached(LimitKind::Review) {
            return Ok(());
        }

        let scores = self.context.rwkv_review_queue_scores.as_ref().unwrap();
        let descending = matches!(
            self.context.sort_options.review_order,
            ReviewCardOrder::RetrievabilityDescending
        );
        let mut ranked_scores: Vec<_> = scores
            .iter()
            .filter_map(|(&card_id, &score)| {
                score.retrievability.is_finite().then_some((card_id, score))
            })
            .collect();
        ranked_scores.sort_by(|(card_id_a, score_a), (card_id_b, score_b)| {
            let ord = score_a.retrievability.total_cmp(&score_b.retrievability);
            let ord = if descending { ord.reverse() } else { ord };
            ord.then_with(|| card_id_a.cmp(card_id_b))
        });
        let scored_card_ids: HashSet<_> =
            ranked_scores.iter().map(|(card_id, _)| *card_id).collect();

        let mut offset = 0;
        let mut chunk_size = (self.limits.remaining_root_limit(LimitKind::Review) as usize)
            .max(RWKV_REVIEW_GATHER_MIN_CHUNK_SIZE)
            .min(ranked_scores.len());
        while offset < ranked_scores.len() && !self.limits.root_limit_reached(LimitKind::Review) {
            let end = (offset + chunk_size).min(ranked_scores.len());
            let score_chunk = &ranked_scores[offset..end];
            let chunk_card_ids: Vec<_> = score_chunk.iter().map(|(card_id, _)| *card_id).collect();
            let mut cards_by_id = HashMap::with_capacity(score_chunk.len());
            col.storage
                .for_each_review_card_in_active_decks_with_ids(&chunk_card_ids, |card| {
                    cards_by_id.insert(card.id, card);
                    Ok(true)
                })?;
            let active_card_ids: Vec<_> = cards_by_id.keys().copied().collect();
            let candidate_metadata =
                rwkv_review_candidate_metadata(col, &active_card_ids, self.context.timing)?;

            for (card_id, score) in score_chunk {
                if self.limits.root_limit_reached(LimitKind::Review) {
                    break;
                }
                let Some(card) = cards_by_id.get(card_id).copied() else {
                    continue;
                };
                let metadata = candidate_metadata.get(card_id).or_not_found(*card_id)?;
                if !rwkv_review_score_eligible(
                    score.retrievability,
                    metadata,
                    self.context.sort_options.rwkv_review_allow_same_day_review,
                    self.context
                        .sort_options
                        .rwkv_review_min_intervening_reviews,
                    self.context.sort_options.rwkv_review_min_elapsed_secs,
                    score.intervening_reviews,
                    score.target_retention,
                ) {
                    continue;
                }
                if !self
                    .limits
                    .limit_reached(card.current_deck_id, LimitKind::Review)?
                    && self.add_due_card(card)
                {
                    self.limits.decrement_deck_and_parent_limits(
                        card.current_deck_id,
                        LimitKind::Review,
                    )?;
                }
            }

            offset = end;
            chunk_size = chunk_size
                .saturating_mul(2)
                .min(ranked_scores.len() - offset);
        }

        self.gather_due_review_cards_without_rwkv_scores(col, &scored_card_ids)
    }

    fn gather_review_cards_by_configured_order(&mut self, col: &mut Collection) -> Result<()> {
        if self.limits.root_limit_reached(LimitKind::Review) {
            return Ok(());
        }

        let scores = self.context.rwkv_review_queue_scores.as_ref().unwrap();
        let scored_card_ids: Vec<_> = scores.keys().copied().collect();
        let metadata = rwkv_review_candidate_metadata(col, &scored_card_ids, self.context.timing)?;
        let eligible_scored_card_ids: HashSet<_> = scores
            .iter()
            .filter_map(|(&card_id, score)| {
                let metadata = metadata.get(&card_id)?;
                rwkv_review_score_eligible(
                    score.retrievability,
                    metadata,
                    self.context.sort_options.rwkv_review_allow_same_day_review,
                    self.context
                        .sort_options
                        .rwkv_review_min_intervening_reviews,
                    self.context.sort_options.rwkv_review_min_elapsed_secs,
                    score.intervening_reviews,
                    score.target_retention,
                )
                .then_some(card_id)
            })
            .collect();
        let scored_card_ids: HashSet<_> = scored_card_ids.into_iter().collect();

        col.storage.for_each_review_card_in_active_decks(
            self.context.timing,
            self.context.sort_options.review_order,
            self.context.fsrs,
            |card| {
                if self.limits.root_limit_reached(LimitKind::Review) {
                    return Ok(false);
                }
                let eligible = if scored_card_ids.contains(&card.id) {
                    eligible_scored_card_ids.contains(&card.id)
                } else {
                    card.due <= self.context.timing.days_elapsed as i32
                };
                if !eligible {
                    return Ok(true);
                }
                if !self
                    .limits
                    .limit_reached(card.current_deck_id, LimitKind::Review)?
                    && self.add_due_card(card)
                {
                    self.limits.decrement_deck_and_parent_limits(
                        card.current_deck_id,
                        LimitKind::Review,
                    )?;
                }
                Ok(true)
            },
        )
    }

    fn gather_due_review_cards_without_rwkv_scores(
        &mut self,
        col: &mut Collection,
        scored_card_ids: &HashSet<CardId>,
    ) -> Result<()> {
        if self.limits.root_limit_reached(LimitKind::Review) {
            return Ok(());
        }

        col.storage.for_each_due_card_in_active_decks(
            self.context.timing,
            // RWKV already handled the scored cards above. Keep the unscored
            // fallback cheap instead of invoking FSRS retrievability ordering.
            ReviewCardOrder::Day,
            DueCardKind::Review,
            self.context.fsrs,
            |card| {
                if scored_card_ids.contains(&card.id) {
                    return Ok(true);
                }
                if self.limits.root_limit_reached(LimitKind::Review) {
                    return Ok(false);
                }
                if !self
                    .limits
                    .limit_reached(card.current_deck_id, LimitKind::Review)?
                    && self.add_due_card(card)
                {
                    self.limits.decrement_deck_and_parent_limits(
                        card.current_deck_id,
                        LimitKind::Review,
                    )?;
                }
                Ok(true)
            },
        )
    }

    fn gather_due_non_new_cards_with_exact_retrievability(
        &mut self,
        col: &mut Collection,
    ) -> Result<()> {
        let mut due_cards = Vec::new();
        self.gather_intraday_learning_cards_for_retrievability_sort(col, &mut due_cards)?;
        self.gather_due_cards_for_retrievability_sort(col, DueCardKind::Learning, &mut due_cards)?;
        self.gather_due_cards_for_retrievability_sort(col, DueCardKind::Review, &mut due_cards)?;

        let mut with_key = Vec::with_capacity(due_cards.len());
        for candidate in due_cards {
            with_key.push((
                candidate,
                exact_retrievability_key(col, candidate.card.id, self.context.timing)?,
            ));
        }
        with_key.sort_by(|(candidate_a, key_a), (candidate_b, key_b)| {
            key_a
                .total_cmp(key_b)
                .then_with(|| candidate_a.card.id.cmp(&candidate_b.card.id))
        });

        for (candidate, _) in with_key {
            if candidate.counts_towards_review_limit
                && (self.limits.root_limit_reached(LimitKind::Review)
                    || self
                        .limits
                        .limit_reached(candidate.card.current_deck_id, LimitKind::Review)?)
            {
                continue;
            }

            if self
                .add_due_card_for_retrievability_sort(candidate.card, candidate.interday_or_review)
            {
                self.r_sorted_non_new.push(candidate.card);

                if candidate.counts_towards_review_limit {
                    self.limits.decrement_deck_and_parent_limits(
                        candidate.card.current_deck_id,
                        LimitKind::Review,
                    )?;
                }
            }
        }

        Ok(())
    }

    fn gather_intraday_learning_cards(&mut self, col: &mut Collection) -> Result<()> {
        col.storage.for_each_intraday_card_in_active_decks(
            self.context.timing.next_day_at,
            |card| {
                self.get_and_update_bury_mode_for_note(card.into());
                self.learning.push(card);
            },
        )?;

        Ok(())
    }

    fn gather_intraday_learning_cards_for_retrievability_sort(
        &mut self,
        col: &mut Collection,
        due_cards: &mut Vec<DueCardForRetrievabilitySort>,
    ) -> Result<()> {
        col.storage.for_each_intraday_card_in_active_decks(
            self.context.timing.next_day_at,
            |card| {
                if card.due <= self.context.timing.now.0 as i32 {
                    due_cards.push(DueCardForRetrievabilitySort {
                        card,
                        counts_towards_review_limit: false,
                        interday_or_review: false,
                    });
                } else {
                    self.learning.push(card);
                }
            },
        )?;

        Ok(())
    }

    fn gather_due_cards_for_retrievability_sort(
        &mut self,
        col: &mut Collection,
        kind: DueCardKind,
        due_cards: &mut Vec<DueCardForRetrievabilitySort>,
    ) -> Result<()> {
        col.storage.for_each_due_card_in_active_decks(
            self.context.timing,
            ReviewCardOrder::Day,
            kind,
            self.context.fsrs,
            |card| {
                due_cards.push(DueCardForRetrievabilitySort {
                    card,
                    counts_towards_review_limit: true,
                    interday_or_review: true,
                });
                Ok(true)
            },
        )
    }

    fn gather_due_cards(&mut self, col: &mut Collection, kind: DueCardKind) -> Result<()> {
        if self.limits.root_limit_reached(LimitKind::Review) {
            return Ok(());
        }
        if self.context.fsrs
            && !self.context.sort_options.rwkv_review_enabled
            && matches!(
                self.context.sort_options.review_order,
                ReviewCardOrder::RetrievabilityAscending
                    | ReviewCardOrder::RetrievabilityDescending
            )
        {
            return self.gather_due_cards_with_exact_retrievability(col, kind);
        }
        col.storage.for_each_due_card_in_active_decks(
            self.context.timing,
            self.context.sort_options.gather_review_order(),
            kind,
            self.context.fsrs,
            |card| {
                if self.limits.root_limit_reached(LimitKind::Review) {
                    return Ok(false);
                }
                if !self
                    .limits
                    .limit_reached(card.current_deck_id, LimitKind::Review)?
                    && self.add_due_card(card)
                {
                    self.limits.decrement_deck_and_parent_limits(
                        card.current_deck_id,
                        LimitKind::Review,
                    )?;
                }
                Ok(true)
            },
        )
    }

    fn gather_due_cards_with_exact_retrievability(
        &mut self,
        col: &mut Collection,
        kind: DueCardKind,
    ) -> Result<()> {
        let mut due_cards = Vec::new();
        col.storage.for_each_due_card_in_active_decks(
            self.context.timing,
            ReviewCardOrder::Day,
            kind,
            self.context.fsrs,
            |card| {
                due_cards.push(card);
                Ok(true)
            },
        )?;

        let descending = matches!(
            self.context.sort_options.review_order,
            ReviewCardOrder::RetrievabilityDescending
        );
        let mut with_key = Vec::with_capacity(due_cards.len());
        for card in due_cards {
            with_key.push((
                card,
                exact_retrievability_key(col, card.id, self.context.timing)?,
            ));
        }
        with_key.sort_by(|(card_a, key_a), (card_b, key_b)| {
            let ord = key_a.total_cmp(key_b);
            let ord = if descending { ord.reverse() } else { ord };
            ord.then_with(|| card_a.id.cmp(&card_b.id))
        });

        for (card, _) in with_key {
            if self.limits.root_limit_reached(LimitKind::Review) {
                break;
            }
            if !self
                .limits
                .limit_reached(card.current_deck_id, LimitKind::Review)?
                && self.add_due_card(card)
            {
                self.limits
                    .decrement_deck_and_parent_limits(card.current_deck_id, LimitKind::Review)?;
            }
        }
        Ok(())
    }

    fn gather_new_cards(&mut self, col: &mut Collection) -> Result<()> {
        let salt = Self::knuth_salt(self.context.timing.days_elapsed);
        match self.context.sort_options.new_gather_priority {
            NewCardGatherPriority::Deck => {
                self.gather_new_cards_by_deck(col, NewCardSorting::LowestPosition)
            }
            NewCardGatherPriority::DeckThenRandomNotes => {
                self.gather_new_cards_by_deck(col, NewCardSorting::RandomNotes(salt))
            }
            NewCardGatherPriority::LowestPosition => {
                self.gather_new_cards_sorted(col, NewCardSorting::LowestPosition)
            }
            NewCardGatherPriority::HighestPosition => {
                self.gather_new_cards_sorted(col, NewCardSorting::HighestPosition)
            }
            NewCardGatherPriority::AscendingRetrievability => {
                self.gather_new_cards_by_retrievability(col, false)
            }
            NewCardGatherPriority::DescendingRetrievability => {
                self.gather_new_cards_by_retrievability(col, true)
            }
            NewCardGatherPriority::RandomNotes => {
                self.gather_new_cards_sorted(col, NewCardSorting::RandomNotes(salt))
            }
            NewCardGatherPriority::RandomCards => {
                self.gather_new_cards_sorted(col, NewCardSorting::RandomCards(salt))
            }
        }
    }

    fn gather_new_cards_by_deck(
        &mut self,
        col: &mut Collection,
        sort: NewCardSorting,
    ) -> Result<()> {
        for deck_id in col.storage.get_active_deck_ids_sorted()? {
            if self.limits.root_limit_reached(LimitKind::New) {
                break;
            }
            if self.limits.limit_reached(deck_id, LimitKind::New)? {
                continue;
            }
            col.storage
                .for_each_new_card_in_deck(deck_id, sort, |card| {
                    let limit_reached = self.limits.limit_reached(deck_id, LimitKind::New)?;
                    if !limit_reached && self.add_new_card(card) {
                        self.limits
                            .decrement_deck_and_parent_limits(deck_id, LimitKind::New)?;
                    }
                    Ok(!limit_reached)
                })?;
        }

        Ok(())
    }

    fn gather_new_cards_sorted(
        &mut self,
        col: &mut Collection,
        order: NewCardSorting,
    ) -> Result<()> {
        col.storage
            .for_each_new_card_in_active_decks(order, |card| {
                if self.limits.root_limit_reached(LimitKind::New) {
                    return Ok(false);
                }
                if !self
                    .limits
                    .limit_reached(card.current_deck_id, LimitKind::New)?
                    && self.add_new_card(card)
                {
                    self.limits
                        .decrement_deck_and_parent_limits(card.current_deck_id, LimitKind::New)?;
                }
                Ok(true)
            })
    }

    fn gather_new_cards_by_retrievability(
        &mut self,
        col: &mut Collection,
        descending: bool,
    ) -> Result<()> {
        let mut cards = Vec::new();
        col.storage
            .for_each_new_card_in_active_decks(NewCardSorting::LowestPosition, |card| {
                cards.push(card);
                Ok(true)
            })?;

        if let Some(scores) = self.context.rwkv_review_queue_scores.as_ref() {
            cards.sort_by(|card_a, card_b| {
                let score_a = scores
                    .get(&card_a.id)
                    .map(|score| score.retrievability)
                    .filter(|score| score.is_finite());
                let score_b = scores
                    .get(&card_b.id)
                    .map(|score| score.retrievability)
                    .filter(|score| score.is_finite());
                match (score_a, score_b) {
                    (Some(score_a), Some(score_b)) => {
                        let ord = score_a.total_cmp(&score_b);
                        if descending {
                            ord.reverse()
                        } else {
                            ord
                        }
                    }
                    (Some(_), None) => std::cmp::Ordering::Less,
                    (None, Some(_)) => std::cmp::Ordering::Greater,
                    (None, None) => std::cmp::Ordering::Equal,
                }
            });
        }

        for card in cards {
            if self.limits.root_limit_reached(LimitKind::New) {
                break;
            }
            if !self
                .limits
                .limit_reached(card.current_deck_id, LimitKind::New)?
                && self.add_new_card(card)
            {
                self.limits
                    .decrement_deck_and_parent_limits(card.current_deck_id, LimitKind::New)?;
            }
        }

        Ok(())
    }

    /// True if limit should be decremented.
    fn add_due_card(&mut self, card: DueCard) -> bool {
        let added = self.add_due_card_for_retrievability_sort(card, true);
        if added {
            match card.kind {
                DueCardKind::Review => self.review.push(card),
                DueCardKind::Learning => self.day_learning.push(card),
            }
        }

        added
    }

    fn add_due_card_for_retrievability_sort(
        &mut self,
        card: DueCard,
        interday_or_review: bool,
    ) -> bool {
        let bury_this_card = self
            .get_and_update_bury_mode_for_note(card.into())
            .map(|mode| match card.kind {
                DueCardKind::Review => mode.bury_reviews,
                DueCardKind::Learning if interday_or_review => mode.bury_interday_learning,
                DueCardKind::Learning => false,
            })
            .unwrap_or_default();
        !bury_this_card
    }

    // True if limit should be decremented.
    fn add_new_card(&mut self, card: NewCard) -> bool {
        let bury_this_card = self
            .get_and_update_bury_mode_for_note(card.into())
            .map(|mode| mode.bury_new)
            .unwrap_or_default();
        // no previous siblings seen?
        if bury_this_card {
            false
        } else {
            self.new.push(card);
            true
        }
    }

    // Generates a salt for use with fnvhash. Useful to increase randomness
    // when the base salt is a small integer.
    fn knuth_salt(base_salt: u32) -> u32 {
        base_salt.wrapping_mul(2654435761)
    }
}

fn elapsed_seconds_since_last_review(card: &Card, timing: SchedTimingToday) -> u32 {
    if let Some(last_review_time) = card.last_review_time {
        timing.now.elapsed_secs_since(last_review_time) as u32
    } else {
        let due = card.original_or_current_due() as i64;
        if due > 365_000 {
            let last_review_time = due.saturating_sub(card.interval as i64);
            timing.now.0.saturating_sub(last_review_time) as u32
        } else {
            let review_day = due.saturating_sub(card.interval as i64);
            timing.days_elapsed.saturating_sub(review_day as u32) * 86_400
        }
    }
}

fn exact_retrievability_key(
    col: &mut Collection,
    card_id: CardId,
    timing: SchedTimingToday,
) -> Result<f32> {
    let card = col.storage.get_card(card_id)?.or_not_found(card_id)?;
    if let Some(state) = card.memory_state {
        let elapsed_days = elapsed_seconds_since_last_review(&card, timing) as f32 / 86_400.0;
        col.fsrs_current_retrievability_for_card(card.id, state.stability_internal, elapsed_days)
    } else {
        // keep SM2-style fallback ordering when FSRS state is missing
        let due = card.original_or_current_due() as i64;
        let review_day = due.saturating_sub(card.interval as i64);
        let days_elapsed = if due > 365_000 {
            (timing.next_day_at.0 as u32).saturating_sub(due as u32) / 86_400
        } else {
            timing.days_elapsed.saturating_sub(review_day as u32)
        };
        Ok(-((days_elapsed as f32) + 0.001) / (card.interval as f32).max(1.0))
    }
}
