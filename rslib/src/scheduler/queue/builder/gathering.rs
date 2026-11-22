// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::collections::HashSet;
use std::hash::Hasher;
use std::sync::Arc;
use std::time::Instant;

use fnv::FnvHasher;
use fsrs::FSRS;

use super::DueCard;
use super::DueCardWithState;
use super::NewCard;
use super::QueueBuilder;
use crate::card::CardQueue;
use crate::collection::RwkvReviewQueueScoreEntry;
use crate::deckconfig::NewCardGatherPriority;
use crate::deckconfig::ReviewCardOrder;
use crate::deckconfig::SuspendedCardOrder;
use crate::decks::limits::LimitKind;
use crate::prelude::*;
use crate::scheduler::fsrs::preset::FsrsPresetCard;
use crate::scheduler::queue::DeferredRwkvReview;
use crate::scheduler::queue::DueCardKind;
use crate::scheduler::rwkv::rwkv_review_candidate_metadata;
use crate::scheduler::rwkv::rwkv_review_relative_overdueness;
use crate::scheduler::rwkv::rwkv_review_score_eligibility;
use crate::scheduler::rwkv::rwkv_review_score_eligibility_ignoring_retention;
use crate::scheduler::rwkv::RwkvReviewCandidateMetadata;
use crate::scheduler::rwkv::RwkvReviewScoreEligibility;
use crate::scheduler::timing::SchedTimingToday;
use crate::storage::card::learnable::learnable_suspended_filter;
use crate::storage::card::NewCardSorting;

#[derive(Debug, Clone, Copy)]
struct DueCardForRetrievabilitySort {
    card: DueCard,
    counts_towards_review_limit: bool,
    interday_or_review: bool,
}

pub(super) const RWKV_REVIEW_GATHER_MIN_CHUNK_SIZE: usize = 256;

/// The part of an RWKV-Instant review gather that can run after new cards:
/// the daily-minimum pass and the final review sort.
enum RwkvReviewGatherTail {
    Priority {
        ranked_scores: Vec<(CardId, RwkvReviewQueueScoreEntry)>,
        priorities: HashMap<CardId, f32>,
    },
    ConfiguredOrder {
        scores: Arc<HashMap<CardId, RwkvReviewQueueScoreEntry>>,
        cards: Vec<(DueCard, bool)>,
        metadata: HashMap<CardId, RwkvReviewCandidateMetadata>,
        eligibility_by_card: HashMap<CardId, RwkvReviewScoreEligibility>,
    },
}

impl QueueBuilder {
    pub(super) fn gather_cards(&mut self, col: &mut Collection) -> Result<()> {
        if self.context.sort_options.uses_rwkv_review_order() {
            self.gather_intraday_learning_cards(col)?;
            self.gather_due_cards(col, DueCardKind::Learning)?;
            let tail = if self.context.uses_rwkv_review_order() {
                self.gather_review_cards_with_rwkv_scores(col)?
            } else {
                self.gather_due_cards(col, DueCardKind::Review)?;
                None
            };
            // With the option on, new cards take their share of the daily
            // limits before reviews pulled only to reach the minimum.
            let new_first = self.context.sort_options.new_cards_ignore_minimum_reviews();
            if new_first {
                self.gather_new_cards(col)?;
            }
            self.finish_rwkv_review_gather(col, tail)?;
            if !new_first {
                self.gather_new_cards(col)?;
            }
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

    fn gather_review_cards_with_rwkv_scores(
        &mut self,
        col: &mut Collection,
    ) -> Result<Option<RwkvReviewGatherTail>> {
        if matches!(
            self.context.sort_options.review_order,
            ReviewCardOrder::RetrievabilityAscending
                | ReviewCardOrder::RetrievabilityDescending
                | ReviewCardOrder::RelativeOverdueness
        ) {
            self.gather_review_cards_by_rwkv_priority(col)
        } else {
            self.gather_review_cards_by_configured_order(col)
        }
    }

    fn gather_review_cards_by_rwkv_priority(
        &mut self,
        col: &mut Collection,
    ) -> Result<Option<RwkvReviewGatherTail>> {
        if self.review_gather_finished() {
            return Ok(None);
        }

        let scores = self.context.rwkv_review_queue_scores.clone().unwrap();
        let review_order = self.context.sort_options.review_order;
        let relative_overdueness = matches!(review_order, ReviewCardOrder::RelativeOverdueness);
        let candidate_metadata = if relative_overdueness {
            let card_ids = scores.keys().copied().collect::<Vec<_>>();
            rwkv_review_candidate_metadata(col, &card_ids, self.context.timing)?
        } else {
            HashMap::new()
        };
        let priorities = scores
            .iter()
            .filter_map(|(&card_id, &score)| {
                if !score.retrievability.is_finite() {
                    return None;
                }
                let priority = match review_order {
                    ReviewCardOrder::RetrievabilityDescending => -score.retrievability,
                    ReviewCardOrder::RelativeOverdueness => rwkv_review_relative_overdueness(
                        score.retrievability,
                        candidate_metadata.get(&card_id)?,
                        score.target_retention,
                    ),
                    _ => score.retrievability,
                };
                priority.is_finite().then_some((card_id, priority))
            })
            .collect::<HashMap<_, _>>();
        let mut ranked_scores: Vec<_> = scores
            .iter()
            .filter_map(|(&card_id, &score)| {
                priorities
                    .contains_key(&card_id)
                    .then_some((card_id, score))
            })
            .collect();
        let compare_scores =
            |(card_id_a, _): &(CardId, RwkvReviewQueueScoreEntry),
             (card_id_b, _): &(CardId, RwkvReviewQueueScoreEntry)| {
                priorities[card_id_a]
                    .total_cmp(&priorities[card_id_b])
                    .then_with(|| card_id_a.cmp(card_id_b))
            };
        let mut remaining_scores = ranked_scores.as_mut_slice();
        let mut chunk_size = (self.limits.remaining_root_limit(LimitKind::Review) as usize)
            .max(RWKV_REVIEW_GATHER_MIN_CHUNK_SIZE)
            .min(remaining_scores.len());
        while !remaining_scores.is_empty() && !self.review_gather_finished() {
            if chunk_size < remaining_scores.len() {
                remaining_scores.select_nth_unstable_by(chunk_size, compare_scores);
            }
            let (score_chunk, rest) = remaining_scores.split_at_mut(chunk_size);
            score_chunk.sort_unstable_by(compare_scores);
            let chunk_card_ids: Vec<_> = score_chunk.iter().map(|(card_id, _)| *card_id).collect();
            let mut cards_by_id = HashMap::with_capacity(score_chunk.len());
            col.storage
                .for_each_review_card_in_active_decks_with_ids(&chunk_card_ids, |card| {
                    cards_by_id.insert(card.id, card);
                    Ok(true)
                })?;
            let active_card_ids: Vec<_> = cards_by_id.keys().copied().collect();
            let fetched_metadata;
            let chunk_metadata = if relative_overdueness {
                &candidate_metadata
            } else {
                fetched_metadata =
                    rwkv_review_candidate_metadata(col, &active_card_ids, self.context.timing)?;
                &fetched_metadata
            };

            for &(card_id, score) in score_chunk.iter() {
                if self.review_gather_finished() {
                    break;
                }
                let Some(card) = cards_by_id.get(&card_id).copied() else {
                    continue;
                };
                let metadata = chunk_metadata.get(&card_id).or_not_found(card_id)?;
                let eligibility = rwkv_review_score_eligibility(
                    score.retrievability,
                    metadata,
                    self.context
                        .sort_options
                        .rwkv_review_min_intervening_reviews,
                    self.context.sort_options.rwkv_review_min_elapsed_secs,
                    score.intervening_reviews,
                    score.target_retention,
                );
                match eligibility {
                    RwkvReviewScoreEligibility::Eligible => {}
                    RwkvReviewScoreEligibility::Deferred { .. } => {
                        self.deferred_rwkv_reviews.insert(
                            card_id,
                            DeferredRwkvReview::from_eligibility(
                                eligibility,
                                self.context.timing.now,
                            )
                            .unwrap(),
                        );
                        continue;
                    }
                    RwkvReviewScoreEligibility::Blocked => continue,
                }
                self.add_rwkv_review_card(card)?;
            }

            remaining_scores = rest;
            chunk_size = chunk_size.saturating_mul(2).min(remaining_scores.len());
        }

        let scored_card_ids = ranked_scores.iter().map(|(card_id, _)| *card_id).collect();
        self.gather_due_review_cards_without_rwkv_scores(col, &scored_card_ids)?;
        Ok(Some(RwkvReviewGatherTail::Priority {
            ranked_scores,
            priorities,
        }))
    }

    fn gather_review_cards_by_configured_order(
        &mut self,
        col: &mut Collection,
    ) -> Result<Option<RwkvReviewGatherTail>> {
        if self.review_gather_finished() {
            return Ok(None);
        }

        let scores = self.context.rwkv_review_queue_scores.clone().unwrap();
        let scored_card_ids: Vec<_> = scores.keys().copied().collect();
        let metadata = rwkv_review_candidate_metadata(col, &scored_card_ids, self.context.timing)?;
        let mut eligibility_by_card = HashMap::new();
        let mut deferred_reviews = Vec::new();
        for (&card_id, score) in scores.iter() {
            let Some(metadata) = metadata.get(&card_id) else {
                continue;
            };
            let eligibility = rwkv_review_score_eligibility(
                score.retrievability,
                metadata,
                self.context
                    .sort_options
                    .rwkv_review_min_intervening_reviews,
                self.context.sort_options.rwkv_review_min_elapsed_secs,
                score.intervening_reviews,
                score.target_retention,
            );
            match eligibility {
                RwkvReviewScoreEligibility::Eligible | RwkvReviewScoreEligibility::Blocked => {}
                RwkvReviewScoreEligibility::Deferred { .. } => {
                    deferred_reviews.push((
                        card_id,
                        DeferredRwkvReview::from_eligibility(eligibility, self.context.timing.now)
                            .unwrap(),
                    ));
                }
            }
            eligibility_by_card.insert(card_id, eligibility);
        }
        self.deferred_rwkv_reviews.extend(deferred_reviews);
        let scored_card_ids: HashSet<_> = scored_card_ids.into_iter().collect();
        let mut cards = Vec::new();

        col.storage.for_each_review_card_in_active_decks(
            self.context.timing,
            self.context.sort_options.review_order,
            self.context.fsrs,
            |card, is_due| {
                cards.push((card, is_due));
                Ok(true)
            },
        )?;

        for (card, is_due) in cards.iter().copied() {
            if self.review_gather_finished() {
                break;
            }
            let eligible = if scored_card_ids.contains(&card.id) {
                matches!(
                    eligibility_by_card.get(&card.id),
                    Some(RwkvReviewScoreEligibility::Eligible)
                )
            } else {
                is_due
            };
            if eligible {
                if metadata.contains_key(&card.id) {
                    self.add_rwkv_review_card(card)?;
                } else {
                    self.add_due_card_respecting_review_limit(card)?;
                }
            }
        }

        Ok(Some(RwkvReviewGatherTail::ConfiguredOrder {
            scores,
            cards,
            metadata,
            eligibility_by_card,
        }))
    }

    /// Runs the daily-minimum pass and the final review sort of an RWKV-Instant
    /// review gather.
    fn finish_rwkv_review_gather(
        &mut self,
        col: &mut Collection,
        tail: Option<RwkvReviewGatherTail>,
    ) -> Result<()> {
        match tail {
            None => {}
            Some(RwkvReviewGatherTail::Priority {
                ranked_scores,
                priorities,
            }) => {
                if !self.review_limit_stops_gathering()
                    && self.limits.any_rwkv_review_minimum_remaining()
                {
                    self.gather_rwkv_review_minimum_cards(col, &ranked_scores)?;
                }
                self.sort_review_cards_by_rwkv_priority(&priorities);
            }
            Some(RwkvReviewGatherTail::ConfiguredOrder {
                scores,
                cards,
                metadata,
                eligibility_by_card,
            }) => {
                if self.limits.any_rwkv_review_minimum_remaining() {
                    self.gather_rwkv_review_minimum_cards_in_configured_order(
                        &scores,
                        &cards,
                        &metadata,
                        &eligibility_by_card,
                    )?;
                }
                let configured_position: HashMap<_, _> = cards
                    .iter()
                    .enumerate()
                    .map(|(position, (card, _))| (card.id, position))
                    .collect();
                self.review.sort_by_key(|card| {
                    configured_position
                        .get(&card.id)
                        .copied()
                        .unwrap_or(usize::MAX)
                });
            }
        }
        Ok(())
    }

    fn gather_rwkv_review_minimum_cards_in_configured_order(
        &mut self,
        scores: &HashMap<CardId, RwkvReviewQueueScoreEntry>,
        cards: &[(DueCard, bool)],
        metadata: &HashMap<CardId, RwkvReviewCandidateMetadata>,
        eligibility_by_card: &HashMap<CardId, RwkvReviewScoreEligibility>,
    ) -> Result<()> {
        let mut pull_candidates = cards.iter().map(|(card, _)| *card).collect::<Vec<_>>();
        pull_candidates.sort_by(|card_a, card_b| {
            let score_a = scores
                .get(&card_a.id)
                .map(|score| score.retrievability)
                .filter(|score| score.is_finite());
            let score_b = scores
                .get(&card_b.id)
                .map(|score| score.retrievability)
                .filter(|score| score.is_finite());
            match (score_a, score_b) {
                (Some(score_a), Some(score_b)) => score_a
                    .total_cmp(&score_b)
                    .then_with(|| card_a.id.cmp(&card_b.id)),
                (Some(_), None) => std::cmp::Ordering::Less,
                (None, Some(_)) => std::cmp::Ordering::Greater,
                (None, None) => std::cmp::Ordering::Equal,
            }
        });
        let mut attempted = HashSet::new();
        for depth in self.limits.rwkv_review_minimum_depths() {
            for card in pull_candidates.iter().copied() {
                if !self.limits.any_rwkv_review_minimum_remaining()
                    || self.review_limit_stops_gathering()
                {
                    break;
                }
                if attempted.contains(&card.id)
                    || !self
                        .limits
                        .rwkv_review_minimum_remaining_at_depth(card.current_deck_id, depth)?
                    || !matches!(
                        eligibility_by_card.get(&card.id),
                        Some(RwkvReviewScoreEligibility::Blocked)
                    )
                    || self.card_ignores_review_limit(card)
                {
                    continue;
                }
                let score = scores.get(&card.id).or_not_found(card.id)?;
                let metadata = metadata.get(&card.id).or_not_found(card.id)?;
                let eligibility = rwkv_review_score_eligibility_ignoring_retention(
                    score.retrievability,
                    metadata,
                    self.context
                        .sort_options
                        .rwkv_review_min_intervening_reviews,
                    self.context.sort_options.rwkv_review_min_elapsed_secs,
                    score.intervening_reviews,
                );
                match eligibility {
                    RwkvReviewScoreEligibility::Eligible => {}
                    RwkvReviewScoreEligibility::Deferred { .. } => {
                        self.deferred_rwkv_reviews.insert(
                            card.id,
                            DeferredRwkvReview::from_eligibility(
                                eligibility,
                                self.context.timing.now,
                            )
                            .unwrap(),
                        );
                        continue;
                    }
                    RwkvReviewScoreEligibility::Blocked => continue,
                }
                self.add_minimum_review(card)?;
                attempted.insert(card.id);
            }
        }
        Ok(())
    }

    fn gather_rwkv_review_minimum_cards(
        &mut self,
        col: &mut Collection,
        ranked_scores: &[(CardId, RwkvReviewQueueScoreEntry)],
    ) -> Result<()> {
        let mut pull_scores = ranked_scores.to_vec();
        pull_scores.sort_unstable_by(|(card_id_a, score_a), (card_id_b, score_b)| {
            score_a
                .retrievability
                .total_cmp(&score_b.retrievability)
                .then_with(|| card_id_a.cmp(card_id_b))
        });
        let mut attempted = HashSet::new();
        for depth in self.limits.rwkv_review_minimum_depths() {
            for score_chunk in pull_scores.chunks(RWKV_REVIEW_GATHER_MIN_CHUNK_SIZE) {
                if !self.limits.has_rwkv_review_minimum_at_depth(depth)
                    || self.review_limit_stops_gathering()
                {
                    break;
                }
                let chunk_card_ids: Vec<_> =
                    score_chunk.iter().map(|(card_id, _)| *card_id).collect();
                let mut cards_by_id = HashMap::with_capacity(score_chunk.len());
                col.storage.for_each_review_card_in_active_decks_with_ids(
                    &chunk_card_ids,
                    |card| {
                        cards_by_id.insert(card.id, card);
                        Ok(true)
                    },
                )?;
                let active_card_ids: Vec<_> = cards_by_id.keys().copied().collect();
                let metadata =
                    rwkv_review_candidate_metadata(col, &active_card_ids, self.context.timing)?;

                for &(card_id, score) in score_chunk {
                    if self.review_limit_stops_gathering() {
                        break;
                    }
                    let Some(card) = cards_by_id.get(&card_id).copied() else {
                        continue;
                    };
                    if attempted.contains(&card_id)
                        || !self
                            .limits
                            .rwkv_review_minimum_remaining_at_depth(card.current_deck_id, depth)?
                        || self.card_ignores_review_limit(card)
                    {
                        continue;
                    }
                    let metadata = metadata.get(&card_id).or_not_found(card_id)?;
                    if !matches!(
                        rwkv_review_score_eligibility(
                            score.retrievability,
                            metadata,
                            self.context
                                .sort_options
                                .rwkv_review_min_intervening_reviews,
                            self.context.sort_options.rwkv_review_min_elapsed_secs,
                            score.intervening_reviews,
                            score.target_retention,
                        ),
                        RwkvReviewScoreEligibility::Blocked
                    ) {
                        continue;
                    }
                    let eligibility = rwkv_review_score_eligibility_ignoring_retention(
                        score.retrievability,
                        metadata,
                        self.context
                            .sort_options
                            .rwkv_review_min_intervening_reviews,
                        self.context.sort_options.rwkv_review_min_elapsed_secs,
                        score.intervening_reviews,
                    );
                    match eligibility {
                        RwkvReviewScoreEligibility::Eligible => {}
                        RwkvReviewScoreEligibility::Deferred { .. } => {
                            self.deferred_rwkv_reviews.insert(
                                card_id,
                                DeferredRwkvReview::from_eligibility(
                                    eligibility,
                                    self.context.timing.now,
                                )
                                .unwrap(),
                            );
                            continue;
                        }
                        RwkvReviewScoreEligibility::Blocked => continue,
                    }
                    self.add_minimum_review(card)?;
                    attempted.insert(card_id);
                }
            }
        }
        Ok(())
    }

    fn review_limit_stops_gathering(&self) -> bool {
        !self.context.same_day_reviews_may_ignore_limit
            && self.limits.root_limit_reached(LimitKind::Review)
    }

    // The preview also needs one eligible review that the limit kept out.
    fn review_gather_finished(&self) -> bool {
        self.review_limit_stops_gathering()
            && self
                .preview
                .as_ref()
                .is_none_or(|trace| trace.review_limit_card.is_some())
    }

    fn add_rwkv_review_card(&mut self, card: DueCard) -> Result<bool> {
        self.add_due_card_respecting_review_limit(card)
    }

    fn add_minimum_review(&mut self, card: DueCard) -> Result<()> {
        if self.add_rwkv_review_card(card)? {
            self.minimum_review_ids.insert(card.id);
        }
        Ok(())
    }

    fn card_ignores_review_limit(&self, card: DueCard) -> bool {
        if !self.context.reviewed_today_card_ids.contains(&card.id) {
            return false;
        }
        let source_deck_id = card.original_deck_id.or(card.current_deck_id);
        self.context
            .deck_map
            .get(&source_deck_id)
            .and_then(Deck::config_id)
            .and_then(|config_id| self.context.config_map.get(&config_id))
            .is_some_and(|config| config.inner.same_day_reviews_ignore_review_limit)
    }

    /// True if the card was added to the queue.
    fn add_due_card_respecting_review_limit(&mut self, card: DueCard) -> Result<bool> {
        let ignore_limit = self.card_ignores_review_limit(card);
        if !ignore_limit
            && self
                .limits
                .limit_reached(card.current_deck_id, LimitKind::Review)?
        {
            if let Some(trace) = &mut self.preview {
                trace.review_limit_card.get_or_insert(card.id);
            }
            return Ok(false);
        }
        let added = self.add_due_card(card);
        if added && !ignore_limit {
            self.limits
                .reserve_review(card.current_deck_id, card.original_deck_id)?;
        }
        Ok(added)
    }

    fn sort_review_cards_by_rwkv_priority(&mut self, priorities: &HashMap<CardId, f32>) {
        self.review.sort_by(|card_a, card_b| {
            match (priorities.get(&card_a.id), priorities.get(&card_b.id)) {
                (Some(priority_a), Some(priority_b)) => priority_a
                    .total_cmp(priority_b)
                    .then_with(|| card_a.id.cmp(&card_b.id)),
                (Some(_), None) => std::cmp::Ordering::Less,
                (None, Some(_)) => std::cmp::Ordering::Greater,
                (None, None) => std::cmp::Ordering::Equal,
            }
        });
    }

    fn gather_due_review_cards_without_rwkv_scores(
        &mut self,
        col: &mut Collection,
        scored_card_ids: &HashSet<CardId>,
    ) -> Result<()> {
        if self.review_gather_finished() {
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
                if self.review_gather_finished() {
                    return Ok(false);
                }
                self.add_due_card_respecting_review_limit(card)?;
                Ok(true)
            },
        )
    }

    fn gather_due_non_new_cards_with_exact_retrievability(
        &mut self,
        col: &mut Collection,
    ) -> Result<()> {
        let started = Instant::now();
        self.gather_future_learning_cards_for_retrievability_sort(col)?;
        let mut cards = col
            .storage
            .due_cards_with_state_in_active_decks(self.context.timing)?;
        let mut due_cards = Vec::with_capacity(cards.len());
        cards.retain(|candidate| {
            let interday_or_review =
                matches!(candidate.queue, CardQueue::Review | CardQueue::DayLearn);
            let card = candidate.card;
            if !interday_or_review && self.card_is_pinned(card.id) {
                return false;
            }
            due_cards.push(DueCardForRetrievabilitySort {
                card,
                counts_towards_review_limit: interday_or_review
                    && !self.card_ignores_review_limit(card),
                interday_or_review,
            });
            true
        });
        let gathered = Instant::now();
        let candidate_count = due_cards.len();

        let keys = exact_review_order_keys(
            col,
            &cards,
            self.context.timing,
            self.context.sort_options.review_order,
            self.preview.as_ref().map(|_| &self.context),
        )?;
        let scored = Instant::now();
        let mut with_key = Vec::with_capacity(due_cards.len());
        for (candidate, key) in due_cards.into_iter().zip(keys) {
            with_key.push((candidate, key, fnvhash_due_card(&candidate.card)));
        }
        let descending = matches!(
            self.context.sort_options.review_order,
            ReviewCardOrder::RetrievabilityDescending
        );
        with_key.sort_unstable_by(
            |(candidate_a, key_a, hash_a), (candidate_b, key_b, hash_b)| {
                let ord = key_a.total_cmp(key_b);
                let ord = if descending { ord.reverse() } else { ord };
                ord.then_with(|| hash_a.cmp(hash_b))
                    .then_with(|| candidate_a.card.id.cmp(&candidate_b.card.id))
            },
        );
        let sorted = Instant::now();

        for (candidate, _, _) in with_key {
            if candidate.counts_towards_review_limit
                && (self.limits.root_limit_reached(LimitKind::Review)
                    || self
                        .limits
                        .limit_reached(candidate.card.current_deck_id, LimitKind::Review)?)
            {
                if let Some(trace) = &mut self.preview {
                    trace.review_limit_card.get_or_insert(candidate.card.id);
                }
                continue;
            }

            if self
                .add_due_card_for_retrievability_sort(candidate.card, candidate.interday_or_review)
            {
                self.push_retrievability_sorted_card(candidate.card, candidate.interday_or_review);

                if candidate.counts_towards_review_limit {
                    self.limits.reserve_review(
                        candidate.card.current_deck_id,
                        candidate.card.original_deck_id,
                    )?;
                }
            }
        }

        tracing::debug!(
            candidates = candidate_count,
            gathered_ms = (gathered - started).as_secs_f64() * 1_000.0,
            scored_ms = (scored - gathered).as_secs_f64() * 1_000.0,
            sorted_ms = (sorted - scored).as_secs_f64() * 1_000.0,
            enqueued_ms = sorted.elapsed().as_secs_f64() * 1_000.0,
            "FSRS queue gather profile"
        );

        Ok(())
    }

    fn gather_intraday_learning_cards(&mut self, col: &mut Collection) -> Result<()> {
        col.storage.for_each_intraday_card_in_active_decks(
            self.context.timing.next_day_at,
            |card| {
                if self.card_is_pinned(card.id) {
                    return;
                }
                self.get_and_update_bury_mode_for_note(card.into());
                self.learning.push(card);
            },
        )?;

        Ok(())
    }

    fn gather_future_learning_cards_for_retrievability_sort(
        &mut self,
        col: &mut Collection,
    ) -> Result<()> {
        col.storage.for_each_intraday_card_in_active_decks(
            self.context.timing.next_day_at,
            |card| {
                if !self.card_is_pinned(card.id) && card.due > self.context.timing.now.0 as i32 {
                    self.learning.push(card);
                }
            },
        )?;

        Ok(())
    }

    fn gather_due_cards(&mut self, col: &mut Collection, kind: DueCardKind) -> Result<()> {
        if self.review_gather_finished() {
            return Ok(());
        }
        col.storage.for_each_due_card_in_active_decks(
            self.context.timing,
            self.context.sort_options.gather_review_order(),
            kind,
            self.context.fsrs,
            |card| {
                if self.review_limit_stops_gathering() {
                    if let Some(trace) = &mut self.preview {
                        trace.review_limit_card.get_or_insert(card.id);
                    }
                    return Ok(false);
                }
                self.add_due_card_respecting_review_limit(card)?;
                Ok(true)
            },
        )
    }

    fn gather_new_cards(&mut self, col: &mut Collection) -> Result<()> {
        if self.new_gather_finished() {
            return Ok(());
        }
        let priority = self.context.sort_options.new_gather_priority;
        let by_deck = matches!(
            priority,
            NewCardGatherPriority::Deck | NewCardGatherPriority::DeckThenRandomNotes
        );
        let salt = Self::knuth_salt(self.context.timing.days_elapsed);
        let active_decks = col.storage.get_active_deck_ids_sorted()?;
        let suspended_filter = learnable_suspended_filter(
            active_decks.iter().copied(),
            &self.context.deck_map,
            &self.context.config_map,
            self.context.timing,
        );
        let sort = match priority {
            NewCardGatherPriority::NoteFieldAscending
            | NewCardGatherPriority::NoteFieldDescending => {
                let field = self.context.sort_options.new_gather_field.clone();
                return col.storage.for_each_new_card_by_field(
                    &field,
                    priority == NewCardGatherPriority::NoteFieldDescending,
                    self.context.sort_options.suspended_card_order,
                    suspended_filter.as_deref(),
                    |card| {
                        self.gather_new_card_if_eligible(card)?;
                        Ok(!self.new_gather_finished())
                    },
                );
            }
            NewCardGatherPriority::Deck
            | NewCardGatherPriority::LowestPosition
            | NewCardGatherPriority::AscendingRetrievability
            | NewCardGatherPriority::DescendingRetrievability => NewCardSorting::LowestPosition,
            NewCardGatherPriority::HighestPosition => NewCardSorting::HighestPosition,
            NewCardGatherPriority::DeckThenRandomNotes | NewCardGatherPriority::RandomNotes => {
                NewCardSorting::RandomNotes(salt)
            }
            NewCardGatherPriority::RandomCards => NewCardSorting::RandomCards(salt),
        };
        // The usual deck-first gather can stop at the limit without sorting
        // the entire deck tree when there is only one group.
        if by_deck && suspended_filter.is_none() {
            return self.gather_new_cards_by_deck(col, &active_decks, sort);
        }
        let scores = self.context.rwkv_review_queue_scores.clone();
        col.storage.with_new_card_candidates(
            sort,
            by_deck,
            suspended_filter.as_deref(),
            |new, suspended| {
                if let (
                    Some(scores),
                    NewCardGatherPriority::AscendingRetrievability
                    | NewCardGatherPriority::DescendingRetrievability,
                ) = (scores, priority)
                {
                    let descending = priority == NewCardGatherPriority::DescendingRetrievability;
                    let new = new_cards_by_retrievability(new, &scores, descending)?;
                    let suspended = new_cards_by_retrievability(suspended, &scores, descending)?;
                    self.gather_new_card_groups(
                        &mut new.into_iter().map(Ok),
                        &mut suspended.into_iter().map(Ok),
                    )
                } else {
                    self.gather_new_card_groups(new, suspended)
                }
            },
        )
    }

    fn gather_new_cards_by_deck(
        &mut self,
        col: &mut Collection,
        deck_ids: &[DeckId],
        sort: NewCardSorting,
    ) -> Result<()> {
        for &deck_id in deck_ids {
            if self.new_gather_finished() {
                break;
            }
            if self.limits.limit_reached(deck_id, LimitKind::New)? && !self.needs_preview_boundary()
            {
                continue;
            }
            col.storage
                .for_each_new_card_in_deck(deck_id, sort, |card| {
                    self.gather_new_card_if_eligible(card)?;
                    Ok(!self.limits.limit_reached(deck_id, LimitKind::New)?
                        || self.needs_preview_boundary())
                })?;
        }

        Ok(())
    }

    fn gather_new_card_groups(
        &mut self,
        new: &mut dyn Iterator<Item = Result<NewCard>>,
        suspended: &mut dyn Iterator<Item = Result<NewCard>>,
    ) -> Result<()> {
        let day = self.context.timing.days_elapsed;
        let studied = self.context.root_deck.new_rev_counts(day).0;
        let mut suspended_next = self
            .new
            .first()
            .map_or((i64::from(day) + i64::from(studied)) % 2 != 0, |pinned| {
                !pinned.suspended
            });
        while !self.new_gather_finished() {
            let prefer_suspended = match self.context.sort_options.suspended_card_order {
                SuspendedCardOrder::Mixed => suspended_next,
                SuspendedCardOrder::SuspendedBeforeNew => true,
                SuspendedCardOrder::NewBeforeSuspended => false,
            };
            let next = if prefer_suspended {
                suspended.next().or_else(|| new.next())
            } else {
                new.next().or_else(|| suspended.next())
            };
            let Some(card) = next.transpose()? else { break };
            if self.gather_new_card_if_eligible(card)? {
                suspended_next = !card.suspended;
            }
        }

        Ok(())
    }

    fn gather_new_card_if_eligible(&mut self, card: NewCard) -> Result<bool> {
        let deck = card.current_deck_id;
        if let Some(limit) = self.limits.exhausted_new_limit(deck)? {
            if let Some(trace) = &mut self.preview {
                trace.new_limit_card.get_or_insert((card.id, limit));
            }
            return Ok(false);
        }
        if !self.add_new_card(card) {
            if let Some(trace) = &mut self.preview {
                trace.sibling.get_or_insert(card.id);
            }
            return Ok(false);
        }
        if self.context.sort_options.new_cards_ignore_minimum_reviews() {
            // Minimum pulls are gathered after new cards, so they must only use
            // the review capacity that new cards leave.
            self.limits.reserve_new_card(deck)?;
        } else {
            self.limits
                .decrement_deck_and_parent_limits(deck, LimitKind::New)?;
        }
        Ok(true)
    }

    fn needs_preview_boundary(&self) -> bool {
        self.preview
            .as_ref()
            .is_some_and(|trace| trace.new_limit_card.is_none())
    }

    fn new_gather_finished(&self) -> bool {
        self.limits.root_limit_reached(LimitKind::New) && !self.needs_preview_boundary()
    }

    /// True if limit should be decremented.
    pub(super) fn add_due_card(&mut self, card: DueCard) -> bool {
        if self.card_is_pinned(card.id) {
            return false;
        }
        let added = self.add_due_card_for_retrievability_sort(card, true);
        if added {
            match card.kind {
                DueCardKind::Review => self.review.push(card),
                DueCardKind::Learning => self.day_learning.push(card),
            }
        }

        added
    }

    pub(super) fn add_due_card_for_retrievability_sort(
        &mut self,
        card: DueCard,
        interday_or_review: bool,
    ) -> bool {
        if self.card_is_pinned(card.id) {
            return false;
        }
        let bury_this_card = self
            .get_and_update_bury_mode_for_note(card.into())
            .map(|mode| match card.kind {
                DueCardKind::Review => mode.bury_reviews,
                DueCardKind::Learning if interday_or_review => mode.bury_interday_learning,
                DueCardKind::Learning => false,
            })
            .unwrap_or_default();
        if bury_this_card {
            if let Some(trace) = &mut self.preview {
                trace.sibling.get_or_insert(card.id);
            }
        }
        !bury_this_card
    }

    // True if limit should be decremented.
    pub(super) fn add_new_card(&mut self, card: NewCard) -> bool {
        if self.card_is_pinned(card.id) {
            return false;
        }
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

fn new_cards_by_retrievability(
    rows: &mut dyn Iterator<Item = Result<NewCard>>,
    scores: &HashMap<CardId, RwkvReviewQueueScoreEntry>,
    descending: bool,
) -> Result<Vec<NewCard>> {
    let mut cards = rows.collect::<Result<Vec<_>>>()?;
    // Unscored cards follow scored cards in their original gather order.
    let score = |id| {
        scores
            .get(&id)
            .map(|score| score.retrievability)
            .filter(|score| score.is_finite())
            .map(|score| if descending { -score } else { score })
            .unwrap_or(f32::INFINITY)
    };
    cards.sort_by(|a, b| score(a.id).total_cmp(&score(b.id)));
    Ok(cards)
}

fn exact_review_order_keys(
    col: &mut Collection,
    cards: &[DueCardWithState],
    timing: SchedTimingToday,
    order: ReviewCardOrder,
    draft: Option<&super::Context>,
) -> Result<Vec<f32>> {
    let started = Instant::now();
    // Cards without memory state use the legacy fallback and need no preset.
    let fsrs_cards = cards
        .iter()
        .filter(|card| card.memory_state.is_some())
        .map(|candidate| FsrsPresetCard {
            id: candidate.card.id,
            home_deck_id: candidate
                .card
                .original_deck_id
                .or(candidate.card.current_deck_id),
        })
        .collect::<Vec<_>>();
    let selected = Instant::now();
    let presets = match draft {
        Some(draft) => {
            col.fsrs_metric_presets_for_draft(&fsrs_cards, &draft.deck_map, &draft.config_map)?
        }
        None => col.fsrs_metric_presets_for_card_inputs(&fsrs_cards)?,
    };
    let resolved = Instant::now();
    let mut models = HashMap::new();
    let keys = cards
        .iter()
        .map(|card| {
            if let Some(state) = card.memory_state {
                let preset = presets.get(&card.card.id).or_not_found(card.card.id)?;
                let fsrs = match models.entry(preset.id.clone()) {
                    std::collections::hash_map::Entry::Occupied(entry) => entry.into_mut(),
                    std::collections::hash_map::Entry::Vacant(entry) => {
                        entry.insert(FSRS::new(&preset.params)?)
                    }
                };
                let elapsed_days = timing.fsrs_elapsed_seconds(
                    card.last_review_time,
                    card.original_or_current_due(),
                    card.interval,
                ) as f32
                    / 86_400.0;
                let key = if matches!(order, ReviewCardOrder::RelativeOverdueness) {
                    let target = card
                        .desired_retention
                        .unwrap_or(preset.desired_retention)
                        .clamp(0.0001, 0.9999);
                    -elapsed_days
                        / fsrs
                            .interval_at_retrievability(state.into(), target)
                            .max(0.0001)
                } else {
                    fsrs.current_retrievability(state.into(), elapsed_days)
                };
                require!(key.is_finite(), "invalid FSRS parameter values");
                return Ok(key);
            }
            // keep SM2-style fallback ordering when FSRS state is missing
            let due = card.original_or_current_due() as i64;
            let review_day = due.saturating_sub(card.interval as i64);
            let days_elapsed = if due > 365_000 {
                (timing.next_day_at.0 as u32).saturating_sub(due as u32) / 86_400
            } else {
                timing.days_elapsed.saturating_sub(review_day as u32)
            };
            Ok(-((days_elapsed as f32) + 0.001) / (card.interval as f32).max(1.0))
        })
        .collect::<Result<Vec<_>>>()?;
    tracing::debug!(
        cards = cards.len(),
        selected_ms = (selected - started).as_secs_f64() * 1_000.0,
        resolved_ms = (resolved - selected).as_secs_f64() * 1_000.0,
        computed_ms = resolved.elapsed().as_secs_f64() * 1_000.0,
        "FSRS queue score profile"
    );
    Ok(keys)
}

fn fnvhash_due_card(card: &DueCard) -> i64 {
    let mut hasher = FnvHasher::default();
    hasher.write_i64(card.id.0);
    hasher.write_i64(card.mtime.0);
    hasher.finish() as i64
}

#[cfg(test)]
pub(super) fn benchmark_retrievability_sort(col: &mut Collection) -> Result<()> {
    let timing = col.timing_today()?;
    let cards = col.storage.due_cards_with_state_in_active_decks(timing)?;
    let keys = exact_review_order_keys(
        col,
        &cards,
        timing,
        ReviewCardOrder::RetrievabilityAscending,
        None,
    )?;
    let entries = cards
        .iter()
        .zip(keys)
        .map(|(card, key)| {
            let card = card.card;
            (
                DueCardForRetrievabilitySort {
                    card,
                    counts_towards_review_limit: true,
                    interday_or_review: true,
                },
                key,
                fnvhash_due_card(&card),
            )
        })
        .collect::<Vec<_>>();
    let compare = |a: &(DueCardForRetrievabilitySort, f32, i64),
                   b: &(DueCardForRetrievabilitySort, f32, i64)| {
        a.1.total_cmp(&b.1)
            .then_with(|| a.2.cmp(&b.2))
            .then_with(|| a.0.card.id.cmp(&b.0.card.id))
    };
    let mut stable_ms = Vec::new();
    let mut unstable_ms = Vec::new();
    for sample in 0..10 {
        let mut stable_ids = Vec::new();
        let mut unstable_ids = Vec::new();
        for unstable in if sample % 2 == 0 {
            [false, true]
        } else {
            [true, false]
        } {
            let mut entries = entries.clone();
            let start = Instant::now();
            if unstable {
                entries.sort_unstable_by(compare);
                unstable_ms.push(start.elapsed().as_secs_f64() * 1_000.0);
                unstable_ids = entries.iter().map(|entry| entry.0.card.id).collect();
            } else {
                entries.sort_by(compare);
                stable_ms.push(start.elapsed().as_secs_f64() * 1_000.0);
                stable_ids = entries.iter().map(|entry| entry.0.card.id).collect();
            }
        }
        assert_eq!(stable_ids, unstable_ids);
    }
    stable_ms.sort_by(f64::total_cmp);
    unstable_ms.sort_by(f64::total_cmp);
    println!(
        "sort,{},stable_ms,{:.3},unstable_ms,{:.3}",
        cards.len(),
        stable_ms[5],
        unstable_ms[5]
    );
    Ok(())
}
