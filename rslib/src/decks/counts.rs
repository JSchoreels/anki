// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
use std::collections::HashMap;
use std::collections::HashSet;

use crate::deckconfig::DeckConfig;
use crate::deckconfig::DeckConfigId;
use crate::decks::limits::LimitTreeMap;
use crate::prelude::*;
use crate::scheduler::rwkv::rwkv_review_candidate_metadata;
use crate::scheduler::rwkv::rwkv_review_score_eligibility;
use crate::scheduler::rwkv::rwkv_review_score_eligibility_ignoring_retention;
use crate::scheduler::rwkv::RwkvReviewScoreEligibility;
use crate::scheduler::timing::SchedTimingToday;

#[derive(Debug)]
pub(crate) struct DueCounts {
    pub new: u32,
    pub review: u32,
    /// interday+intraday
    pub learning: u32,

    pub intraday_learning: u32,
    pub interday_learning: u32,
    pub total_cards: u32,
}

impl Deck {
    /// Return the studied counts if studied today.
    /// May be negative if user has extended limits.
    pub(crate) fn new_rev_counts(&self, today: u32) -> (i32, i32) {
        if self.common.last_day_studied == today {
            (self.common.new_studied, self.common.review_studied)
        } else {
            (0, 0)
        }
    }
}

impl Collection {
    /// Get due counts for decks at the given timestamp.
    pub(crate) fn due_counts(
        &mut self,
        days_elapsed: u32,
        learn_cutoff: u32,
    ) -> Result<HashMap<DeckId, DueCounts>> {
        self.storage.due_counts(days_elapsed, learn_cutoff)
    }

    pub(crate) fn apply_rwkv_review_queue_counts(
        &mut self,
        counts: &mut HashMap<DeckId, DueCounts>,
        decks: &HashMap<DeckId, Deck>,
        configs: &HashMap<DeckConfigId, DeckConfig>,
        timing: SchedTimingToday,
    ) -> Result<()> {
        let deck_count_scores = self.take_rwkv_deck_count_scores_for_day(timing.days_elapsed);
        if !deck_count_scores.is_empty() {
            let result = deck_count_scores
                .iter()
                .try_for_each(|(&score_deck_id, scores)| {
                    self.apply_rwkv_score_scope_counts(
                        counts,
                        decks,
                        configs,
                        timing,
                        score_deck_id,
                        scores,
                    )
                });
            self.restore_rwkv_deck_count_scores(timing.days_elapsed, deck_count_scores);
            result?;
            return Ok(());
        }

        if let Some((score_deck_id, scores)) =
            self.rwkv_review_queue_scores_for_day(timing.days_elapsed)
        {
            self.apply_rwkv_score_scope_counts(
                counts,
                decks,
                configs,
                timing,
                score_deck_id,
                &scores,
            )?;
        }

        Ok(())
    }

    fn apply_rwkv_score_scope_counts(
        &mut self,
        counts: &mut HashMap<DeckId, DueCounts>,
        decks: &HashMap<DeckId, Deck>,
        configs: &HashMap<DeckConfigId, DeckConfig>,
        timing: SchedTimingToday,
        score_deck_id: DeckId,
        scores: &HashMap<CardId, crate::collection::RwkvReviewQueueScoreEntry>,
    ) -> Result<()> {
        let (allow_same_day_review, min_intervening_reviews, min_elapsed_secs) = match decks
            .get(&score_deck_id)
            .and_then(|deck| deck.config_id())
            .and_then(|config_id| configs.get(&config_id))
        {
            Some(config) if config.inner.rwkv_review_instant_order_enabled => (
                config.inner.rwkv_review_allow_same_day_review,
                config.inner.rwkv_review_min_intervening_reviews,
                config.inner.rwkv_review_min_elapsed_secs,
            ),
            _ => return Ok(()),
        };

        let root_deck = decks.get(&score_deck_id).or_not_found(score_deck_id)?;
        let mut scope_decks = self.storage.child_decks(root_deck)?;
        scope_decks.insert(0, root_deck.clone());
        let scope_deck_ids: HashSet<_> = scope_decks.iter().map(|deck| deck.id).collect();

        let scored_ids: Vec<_> = scores.keys().copied().collect();
        let metadata = rwkv_review_candidate_metadata(self, &scored_ids, timing)?;
        let mut pull_candidates = Vec::new();
        for (card_id, score) in scores {
            let Some(metadata) = metadata.get(card_id) else {
                continue;
            };
            if !scope_deck_ids.contains(&metadata.current_deck_id) {
                continue;
            }
            if decks
                .get(&metadata.current_deck_id)
                .is_some_and(Deck::is_filtered)
            {
                continue;
            }

            let eligibility = rwkv_review_score_eligibility(
                score.retrievability,
                metadata,
                allow_same_day_review,
                min_intervening_reviews,
                min_elapsed_secs,
                score.intervening_reviews,
                score.target_retention,
            );
            let rwkv_due = matches!(eligibility, RwkvReviewScoreEligibility::Eligible);
            if rwkv_due != metadata.fsrs_due_today {
                let Some(counts) = counts.get_mut(&metadata.current_deck_id) else {
                    continue;
                };
                if rwkv_due {
                    counts.review = counts.review.saturating_add(1);
                } else {
                    counts.review = counts.review.saturating_sub(1);
                }
            }

            if matches!(eligibility, RwkvReviewScoreEligibility::Blocked)
                && matches!(
                    rwkv_review_score_eligibility_ignoring_retention(
                        score.retrievability,
                        metadata,
                        allow_same_day_review,
                        min_intervening_reviews,
                        min_elapsed_secs,
                        score.intervening_reviews,
                    ),
                    RwkvReviewScoreEligibility::Eligible
                )
            {
                pull_candidates.push((*card_id, score.retrievability, metadata.current_deck_id));
            }
        }

        let mut minimums = LimitTreeMap::build(&scope_decks, configs, timing.days_elapsed, false);
        for deck in &scope_decks {
            if let Some(count) = counts.get(&deck.id) {
                minimums.reserve_rwkv_reviews(
                    deck.id,
                    count.review.saturating_add(count.interday_learning),
                )?;
            }
        }

        pull_candidates.sort_unstable_by(|(card_a, score_a, _), (card_b, score_b, _)| {
            score_a.total_cmp(score_b).then_with(|| card_a.cmp(card_b))
        });
        for (_, _, deck_id) in pull_candidates {
            if !minimums.rwkv_review_minimum_remaining(deck_id)? {
                continue;
            }
            if let Some(counts) = counts.get_mut(&deck_id) {
                counts.review = counts.review.saturating_add(1);
                minimums.reserve_rwkv_reviews(deck_id, 1)?;
            }
        }

        Ok(())
    }

    pub(crate) fn counts_for_deck_today(
        &mut self,
        did: DeckId,
    ) -> Result<anki_proto::scheduler::CountsForDeckTodayResponse> {
        let today = self.current_due_day(0)?;
        let mut deck = self.storage.get_deck(did)?.or_not_found(did)?;
        deck.reset_stats_if_day_changed(today);
        Ok(anki_proto::scheduler::CountsForDeckTodayResponse {
            new: deck.common.new_studied,
            review: deck.common.review_studied,
        })
    }
}
