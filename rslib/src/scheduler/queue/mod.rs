// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod builder;
mod entry;
mod learning;
mod main;
pub(crate) mod undo;

use std::collections::HashMap;
use std::collections::VecDeque;

use anki_proto::scheduler::SchedulingContext;
pub(crate) use builder::DueCard;
pub(crate) use builder::DueCardKind;
pub(crate) use builder::NewCard;
pub(crate) use entry::QueueEntry;
pub(crate) use entry::QueueEntryKind;
pub(crate) use learning::LearningQueueEntry;
pub(crate) use main::MainQueueEntry;
pub(crate) use main::MainQueueEntryKind;

use self::undo::QueueUpdate;
use super::states::SchedulingStates;
use super::timing::SchedTimingToday;
use crate::prelude::*;
use crate::scheduler::rwkv::RwkvReviewScoreEligibility;
use crate::scheduler::states::load_balancer::LoadBalancer;
use crate::timestamp::TimestampSecs;

#[derive(Debug)]
pub(crate) struct CardQueues {
    counts: Counts,
    main: VecDeque<MainQueueEntry>,
    intraday_learning: VecDeque<LearningQueueEntry>,
    current_day: u32,
    learn_ahead_secs: i64,
    build_time: TimestampMillis,
    /// Updated each time a card is answered, and by get_queued_cards() when the
    /// counts are zero. Ensures we don't show a newly-due learning card after a
    /// user returns from editing a review card.
    current_learning_cutoff: TimestampSecs,
    shown_top_card: Option<CardId>,
    non_news_sorted_by_retrievability: bool,
    deferred_rwkv_reviews: HashMap<CardId, DeferredRwkvReview>,
    pub(crate) load_balancer: Option<LoadBalancer>,
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct DeferredRwkvReview {
    pub(crate) required_intervening_reviews: Option<u32>,
    pub(crate) intervening_reviews: Option<u32>,
    pub(crate) eligible_at: Option<TimestampSecs>,
}

impl DeferredRwkvReview {
    pub(crate) fn from_eligibility(
        eligibility: RwkvReviewScoreEligibility,
        now: TimestampSecs,
    ) -> Option<Self> {
        match eligibility {
            RwkvReviewScoreEligibility::Deferred {
                required_intervening_reviews,
                intervening_reviews,
                remaining_elapsed_secs,
            } => Some(Self {
                required_intervening_reviews,
                intervening_reviews,
                eligible_at: remaining_elapsed_secs
                    .map(|remaining| now.adding_secs(remaining.into())),
            }),
            RwkvReviewScoreEligibility::Eligible | RwkvReviewScoreEligibility::Blocked => None,
        }
    }

    fn is_ready(self, now: TimestampSecs) -> bool {
        self.required_intervening_reviews.map_or(true, |required| {
            self.intervening_reviews
                .is_some_and(|reviews| reviews >= required)
        }) && self
            .eligible_at
            .map_or(true, |eligible_at| now >= eligible_at)
    }
}

#[derive(Debug, Copy, Clone)]
pub struct Counts {
    pub new: usize,
    pub learning: usize,
    pub review: usize,
}

impl Counts {
    fn all_zero(self) -> bool {
        self.new == 0 && self.learning == 0 && self.review == 0
    }
}

#[derive(Debug, Clone)]
pub struct QueuedCard {
    pub card: Card,
    pub kind: QueueEntryKind,
    pub states: Option<SchedulingStates>,
    pub context: SchedulingContext,
}

#[derive(Debug)]
pub struct QueuedCards {
    pub cards: Vec<QueuedCard>,
    pub new_count: usize,
    pub learning_count: usize,
    pub review_count: usize,
}

/// When we encounter a card with new or review burying enabled, all future
/// siblings need to be buried, regardless of their own settings.
#[derive(Default, Debug, Clone, Copy)]
pub(crate) struct BuryMode {
    pub(crate) bury_new: bool,
    pub(crate) bury_reviews: bool,
    pub(crate) bury_interday_learning: bool,
}

impl Collection {
    pub fn get_next_card(&mut self) -> Result<Option<QueuedCard>> {
        self.get_queued_cards(1, false, false)
            .map(|queued| queued.cards.first().cloned())
    }

    pub fn get_queued_cards(
        &mut self,
        fetch_limit: usize,
        intraday_learning_only: bool,
        skip_scheduling_states: bool,
    ) -> Result<QueuedCards> {
        let queues = self.get_queues()?;
        let counts = queues.counts();
        let entries: Vec<_> = if intraday_learning_only {
            queues
                .intraday_now_iter()
                .chain(queues.intraday_ahead_iter())
                .map(Into::into)
                .collect()
        } else {
            queues.iter().take(fetch_limit).collect()
        };
        if fetch_limit == 1 && !intraday_learning_only {
            queues.mark_top_card_shown(entries.first().map(QueueEntry::card_id));
        }
        let cards: Vec<_> = entries
            .into_iter()
            .map(|entry| {
                let card = self
                    .storage
                    .get_card(entry.card_id())?
                    .or_not_found(entry.card_id())?;
                require!(
                    card.mtime == entry.mtime(),
                    "bug: card modified without updating queue: id:{} card:{} entry:{}",
                    card.id,
                    card.mtime,
                    entry.mtime()
                );

                let next_states = if skip_scheduling_states {
                    None
                } else {
                    // fixme: pass in card instead of id
                    Some(self.get_scheduling_states(card.id)?)
                };

                Ok(QueuedCard {
                    context: new_scheduling_context(self, &card)?,
                    card,
                    states: next_states,
                    kind: entry.kind(),
                })
            })
            .collect::<Result<_>>()?;
        Ok(QueuedCards {
            cards,
            new_count: counts.new,
            learning_count: counts.learning,
            review_count: counts.review,
        })
    }
}

fn new_scheduling_context(col: &mut Collection, card: &Card) -> Result<SchedulingContext> {
    Ok(SchedulingContext {
        deck_name: col
            .get_deck(card.original_or_current_deck_id())?
            .or_not_found(card.deck_id)?
            .human_name(),
        seed: card.review_seed(),
        decay: card.decay,
        desired_retention: card.desired_retention,
    })
}

impl CardQueues {
    pub(crate) fn main_contains(&self, card_id: CardId) -> bool {
        self.main.iter().any(|entry| entry.id == card_id)
    }

    pub(crate) fn update_deferred_rwkv_intervening_reviews(
        &mut self,
        intervening_reviews_by_card_id: &HashMap<CardId, u32>,
    ) -> bool {
        let now = TimestampSecs::now();
        let mut rebuild_required = false;
        for (card_id, intervening_reviews) in intervening_reviews_by_card_id {
            if let Some(deferred) = self.deferred_rwkv_reviews.get_mut(card_id) {
                deferred.intervening_reviews = Some(*intervening_reviews);
                rebuild_required |= deferred.is_ready(now);
            }
        }
        rebuild_required
    }

    pub(crate) fn defer_rwkv_review(&mut self, card_id: CardId, deferred: DeferredRwkvReview) {
        self.deferred_rwkv_reviews.insert(card_id, deferred);
    }

    pub(crate) fn remove_deferred_rwkv_review(&mut self, card_id: CardId) {
        self.deferred_rwkv_reviews.remove(&card_id);
    }

    fn deferred_rwkv_review_is_ready(&self) -> bool {
        let now = TimestampSecs::now();
        self.deferred_rwkv_reviews
            .values()
            .any(|deferred| deferred.is_ready(now))
    }

    fn mark_top_card_shown(&mut self, card_id: Option<CardId>) {
        self.shown_top_card = card_id;
    }

    fn preserve_current_card(&mut self, card_id: CardId) -> Result<()> {
        if let Some(position) = self.main.iter().position(|entry| entry.id == card_id) {
            let entry = self.main.remove(position).unwrap();
            self.main.push_front(entry);
        } else {
            require!(
                self.intraday_learning
                    .iter()
                    .any(|entry| entry.id == card_id),
                "current card missing from rebuilt queue"
            );
        }
        self.mark_top_card_shown(Some(card_id));
        Ok(())
    }

    /// An iterator over the card queues, in the order the cards will
    /// be presented.
    fn iter(&self) -> impl Iterator<Item = QueueEntry> + '_ {
        let intraday_now = (!self.non_news_sorted_by_retrievability)
            .then_some(())
            .into_iter()
            .flat_map(|_| self.intraday_now_iter().map(Into::into));
        let intraday_ahead = (!self.non_news_sorted_by_retrievability)
            .then_some(())
            .into_iter()
            .flat_map(|_| self.intraday_ahead_iter().map(Into::into));
        intraday_now
            .chain(self.main.iter().map(Into::into))
            .chain(intraday_ahead)
    }

    /// Remove the provided card from the top of the queues and
    /// adjust the counts. If it was not at the top, return an error.
    fn pop_entry(&mut self, id: CardId) -> Result<QueueEntry> {
        if let Some(pos) = self.intraday_learning.iter().position(|e| e.id == id) {
            let entry = self.intraday_learning.remove(pos).unwrap();
            // FIXME:
            // under normal circumstances this should not go below 0, but currently
            // the Python unit tests answer learning cards before they're due
            self.counts.learning = self.counts.learning.saturating_sub(1);
            self.shown_top_card = None;
            Ok(entry.into())
        } else if self.main.front().filter(|e| e.id == id).is_some() {
            self.shown_top_card = None;
            Ok(self.pop_main().unwrap().into())
        } else {
            invalid_input!("not at top of queue")
        }
    }

    fn push_undo_entry(&mut self, entry: QueueEntry) {
        self.shown_top_card = None;
        match entry {
            QueueEntry::IntradayLearning(entry) => self.push_intraday_learning(entry),
            QueueEntry::Main(entry) => self.push_main(entry),
        }
    }

    /// Return the current due counts. If there are no due cards, the learning
    /// cutoff is updated to the current time first, and any newly-due learning
    /// cards are added to the counts.
    pub(crate) fn counts(&mut self) -> Counts {
        if self.counts.all_zero() && !self.non_news_sorted_by_retrievability {
            // we discard the returned undo information in this case
            self.update_learning_cutoff_and_count();
        }
        self.counts
    }

    fn is_stale(&self, current_day: u32) -> bool {
        self.current_day != current_day
    }

    fn due_intraday_needs_retrievability_resort(&self) -> bool {
        self.non_news_sorted_by_retrievability
            && self
                .intraday_learning
                .front()
                .map(|entry| entry.due <= TimestampSecs::now())
                .unwrap_or(false)
    }
}

impl Collection {
    pub(crate) fn rebuild_queued_cards_preserving_current_card(
        &mut self,
        current_card_id: CardId,
    ) -> Result<QueuedCards> {
        let deck = self.get_current_deck()?;
        self.clear_queues_if_day_changed()?;
        let card = self
            .storage
            .get_card(current_card_id)?
            .or_not_found(current_card_id)?;
        let mut queues = self.build_queues_with_current_card(deck.id, Some(&card))?;
        let counts = queues.counts();
        self.state.card_queues = Some(queues);
        Ok(QueuedCards {
            cards: vec![],
            new_count: counts.new,
            learning_count: counts.learning,
            review_count: counts.review,
        })
    }

    /// This is automatically done when transact() is called for everything
    /// except card answers, so unless you are modifying state outside of a
    /// transaction, you probably don't need this.
    pub(crate) fn clear_study_queues(&mut self) {
        self.state.card_queues = None;
    }

    pub(crate) fn maybe_clear_study_queues_after_op(&mut self, op: &OpChanges) {
        if op.op != Op::AnswerCard && op.requires_study_queue_rebuild() {
            self.state.card_queues = None;
        }
    }

    pub(crate) fn update_queues_after_answering_card(
        &mut self,
        card: &Card,
        timing: SchedTimingToday,
        is_finished_preview: bool,
    ) -> Result<()> {
        if let Some(queues) = &mut self.state.card_queues {
            let entry = queues.pop_entry(card.id)?;
            let requeued_learning = if is_finished_preview {
                None
            } else {
                queues.maybe_requeue_learning_card(card, timing)
            };
            let cutoff_snapshot = queues.update_learning_cutoff_and_count();
            let queue_build_time = queues.build_time;
            self.save_queue_update_undo(Box::new(QueueUpdate {
                entry,
                learning_requeue: requeued_learning,
                queue_build_time,
                cutoff_snapshot,
            }));
        } else {
            // we currently allow the queues to be empty for unit tests
        }

        Ok(())
    }

    /// Get the card queues, building if necessary.
    pub(crate) fn get_queues(&mut self) -> Result<&mut CardQueues> {
        let deck = self.get_current_deck()?;
        self.clear_queues_if_day_changed()?;
        if self.state.card_queues.is_none()
            || self
                .state
                .card_queues
                .as_ref()
                .map(|queues| {
                    queues.due_intraday_needs_retrievability_resort()
                        || queues.deferred_rwkv_review_is_ready()
                })
                .unwrap_or(false)
        {
            self.state.card_queues = Some(self.build_queues(deck.id)?);
        }

        Ok(self.state.card_queues.as_mut().unwrap())
    }

    // Returns queues if they are valid and have not been rebuilt. If build time has
    // changed, they are cleared.
    pub(crate) fn get_or_invalidate_queues(
        &mut self,
        build_time: TimestampMillis,
    ) -> Result<Option<&mut CardQueues>> {
        self.clear_queues_if_day_changed()?;
        let same_build = self
            .state
            .card_queues
            .as_ref()
            .map(|q| q.build_time == build_time)
            .unwrap_or_default();
        if same_build {
            Ok(self.state.card_queues.as_mut())
        } else {
            self.clear_study_queues();
            Ok(None)
        }
    }

    fn clear_queues_if_day_changed(&mut self) -> Result<()> {
        let timing = self.timing_today()?;
        let day_rolled_over = self
            .state
            .card_queues
            .as_ref()
            .map(|q| q.is_stale(timing.days_elapsed))
            .unwrap_or(false);
        if day_rolled_over {
            self.discard_undo_and_study_queues();
            self.unbury_on_day_rollover(timing.days_elapsed)?;
        }
        Ok(())
    }
}

// test helpers
#[cfg(test)]
impl Collection {
    pub(crate) fn counts(&mut self) -> [usize; 3] {
        self.get_queued_cards(1, false, false)
            .map(|q| [q.new_count, q.learning_count, q.review_count])
            .unwrap_or([0; 3])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deferred_rwkv_review_requires_every_guard_to_be_ready() {
        let now = TimestampSecs(100);
        let mut deferred = DeferredRwkvReview {
            required_intervening_reviews: Some(2),
            intervening_reviews: Some(1),
            eligible_at: Some(TimestampSecs(99)),
        };
        assert!(!deferred.is_ready(now));

        deferred.intervening_reviews = Some(2);
        deferred.eligible_at = Some(TimestampSecs(101));
        assert!(!deferred.is_ready(now));

        deferred.eligible_at = Some(now);
        assert!(deferred.is_ready(now));
    }
}
