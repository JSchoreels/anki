// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod burying;
mod gathering;
pub(crate) mod intersperser;
pub(crate) mod sized_chain;
mod sorting;

use std::collections::HashMap;
use std::collections::VecDeque;

use intersperser::Intersperser;
use sized_chain::SizedChain;

use super::BuryMode;
use super::CardQueues;
use super::Counts;
use super::LearningQueueEntry;
use super::MainQueueEntry;
use super::MainQueueEntryKind;
use crate::collection::RwkvReviewQueueScoreEntry;
use crate::deckconfig::NewCardGatherPriority;
use crate::deckconfig::NewCardSortOrder;
use crate::deckconfig::ReviewCardOrder;
use crate::deckconfig::ReviewMix;
use crate::decks::limits::LimitTreeMap;
use crate::prelude::*;
use crate::scheduler::states::load_balancer::LoadBalancer;
use crate::scheduler::timing::SchedTimingToday;

/// Temporary holder for review cards that will be built into a queue.
#[derive(Debug, Clone, Copy)]
pub(crate) struct DueCard {
    pub id: CardId,
    pub note_id: NoteId,
    pub mtime: TimestampSecs,
    pub due: i32,
    pub current_deck_id: DeckId,
    pub original_deck_id: DeckId,
    pub kind: DueCardKind,
    pub reps: u32,
}

#[derive(Debug, Clone, Copy)]
pub(crate) enum DueCardKind {
    Review,
    Learning,
}

/// Temporary holder for new cards that will be built into a queue.
#[derive(Debug, Default, Clone, Copy)]
pub(crate) struct NewCard {
    pub id: CardId,
    pub note_id: NoteId,
    pub mtime: TimestampSecs,
    pub current_deck_id: DeckId,
    pub original_deck_id: DeckId,
    pub template_index: u32,
    pub hash: u64,
}

impl From<DueCard> for MainQueueEntry {
    fn from(c: DueCard) -> Self {
        MainQueueEntry {
            id: c.id,
            mtime: c.mtime,
            kind: match c.kind {
                DueCardKind::Review => MainQueueEntryKind::Review,
                DueCardKind::Learning => MainQueueEntryKind::InterdayLearning,
            },
        }
    }
}

impl From<NewCard> for MainQueueEntry {
    fn from(c: NewCard) -> Self {
        MainQueueEntry {
            id: c.id,
            mtime: c.mtime,
            kind: MainQueueEntryKind::New,
        }
    }
}

impl From<DueCard> for LearningQueueEntry {
    fn from(c: DueCard) -> Self {
        LearningQueueEntry {
            due: TimestampSecs(c.due as i64),
            id: c.id,
            mtime: c.mtime,
            reps: c.reps,
        }
    }
}

#[derive(Default, Clone, Debug)]
pub(super) struct QueueSortOptions {
    pub(super) new_order: NewCardSortOrder,
    pub(super) new_gather_priority: NewCardGatherPriority,
    pub(super) review_order: ReviewCardOrder,
    pub(super) day_learn_mix: ReviewMix,
    pub(super) new_review_mix: ReviewMix,
    pub(super) rwkv_review_enabled: bool,
    pub(super) rwkv_review_instant_order_enabled: bool,
    pub(super) rwkv_review_allow_same_day_review: bool,
    pub(super) rwkv_review_min_intervening_reviews: u32,
    pub(super) rwkv_review_min_elapsed_secs: u32,
}

#[derive(Debug)]
pub(super) struct QueueBuilder {
    pub(super) new: Vec<NewCard>,
    pub(super) review: Vec<DueCard>,
    pub(super) learning: Vec<DueCard>,
    pub(super) day_learning: Vec<DueCard>,
    pub(super) r_sorted_non_new: Vec<DueCard>,
    limits: LimitTreeMap,
    load_balancer: Option<LoadBalancer>,
    context: Context,
}

/// Data container and helper for building queues.
#[derive(Debug, Clone)]
struct Context {
    timing: SchedTimingToday,
    config_map: HashMap<DeckConfigId, DeckConfig>,
    root_deck: Deck,
    sort_options: QueueSortOptions,
    seen_note_ids: HashMap<NoteId, BuryMode>,
    deck_map: HashMap<DeckId, Deck>,
    fsrs: bool,
    rwkv_review_queue_scores: Option<HashMap<CardId, RwkvReviewQueueScoreEntry>>,
}

impl QueueBuilder {
    pub(super) fn new(col: &mut Collection, deck_id: DeckId) -> Result<Self> {
        let timing = col.timing_for_timestamp(TimestampSecs::now())?;
        let new_cards_ignore_review_limit = col.get_config_bool(BoolKey::NewCardsIgnoreReviewLimit);
        let apply_all_parent_limits = col.get_config_bool(BoolKey::ApplyAllParentLimits);
        let config_map = col.storage.get_deck_config_map()?;
        let root_deck = col.storage.get_deck(deck_id)?.or_not_found(deck_id)?;
        let mut decks = col.storage.child_decks(&root_deck)?;
        decks.insert(0, root_deck.clone());
        if apply_all_parent_limits {
            for parent in col.storage.parent_decks(&root_deck)? {
                decks.insert(0, parent);
            }
        }
        let limits = LimitTreeMap::build(
            &decks,
            &config_map,
            timing.days_elapsed,
            new_cards_ignore_review_limit,
        );
        let sort_options = sort_options(&root_deck, &config_map);
        let rwkv_review_queue_scores = if sort_options.uses_rwkv_retrievability_scores() {
            col.rwkv_review_queue_scores(root_deck.id, timing.days_elapsed)
                .or_else(|| {
                    col.rwkv_review_queue_scores_for_day(timing.days_elapsed)
                        .map(|(_, scores)| scores)
                })
        } else {
            None
        };
        let deck_map = col.storage.get_decks_map()?;

        let load_balancer = col
            .get_config_bool(BoolKey::LoadBalancerEnabled)
            .then(|| {
                let did_to_dcid = deck_map
                    .values()
                    .filter_map(|deck| Some((deck.id, deck.config_id()?)))
                    .collect::<HashMap<_, _>>();
                LoadBalancer::new(
                    timing.days_elapsed,
                    did_to_dcid,
                    col.review_fuzz_config(),
                    col.timing_today()?.next_day_at,
                    &col.storage,
                )
            })
            .transpose()?;

        Ok(QueueBuilder {
            new: Vec::new(),
            review: Vec::new(),
            learning: Vec::new(),
            day_learning: Vec::new(),
            r_sorted_non_new: Vec::new(),
            limits,
            load_balancer,
            context: Context {
                timing,
                config_map,
                root_deck,
                sort_options,
                seen_note_ids: HashMap::new(),
                deck_map,
                fsrs: col.get_config_bool(BoolKey::Fsrs),
                rwkv_review_queue_scores,
            },
        })
    }

    pub(super) fn build(mut self, learn_ahead_secs: i64) -> CardQueues {
        self.sort_new();

        // intraday learning and total learn count
        let intraday_learning = sort_learning(self.learning);
        let now = TimestampSecs::now();
        let cutoff = now.adding_secs(learn_ahead_secs);
        let shared_r_sort = self.context.non_news_sorted_by_retrievability();
        let r_sorted_learning_count = self
            .r_sorted_non_new
            .iter()
            .filter(|card| matches!(card.kind, DueCardKind::Learning))
            .count();
        let r_sorted_review_count = self
            .r_sorted_non_new
            .iter()
            .filter(|card| matches!(card.kind, DueCardKind::Review))
            .count();
        let learn_count = if shared_r_sort {
            r_sorted_learning_count
        } else {
            intraday_learning.iter().filter(|e| e.due <= cutoff).count() + self.day_learning.len()
        };

        let review_count = if shared_r_sort {
            r_sorted_review_count
        } else {
            self.review.len()
        };
        let new_count = self.new.len();

        // merge due non-new and new cards into main
        let with_interday_learn = if shared_r_sort {
            Box::new(self.r_sorted_non_new.into_iter().map(Into::into))
                as Box<dyn ExactSizeIterator<Item = MainQueueEntry>>
        } else {
            merge_day_learning(
                self.review,
                self.day_learning,
                self.context.sort_options.day_learn_mix,
            )
        };
        let main_iter = merge_new(
            with_interday_learn,
            self.new,
            self.context.sort_options.new_review_mix,
        );

        CardQueues {
            counts: Counts {
                new: new_count,
                review: review_count,
                learning: learn_count,
            },
            main: main_iter.collect(),
            intraday_learning,
            learn_ahead_secs,
            current_day: self.context.timing.days_elapsed,
            build_time: TimestampMillis::now(),
            load_balancer: self.load_balancer,
            current_learning_cutoff: now,
            shown_top_card: None,
            non_news_sorted_by_retrievability: shared_r_sort,
        }
    }
}

impl Context {
    fn non_news_sorted_by_retrievability(&self) -> bool {
        self.fsrs
            && !self.sort_options.uses_rwkv_review_order()
            && !self.sort_options.rwkv_review_enabled
            && matches!(
                self.sort_options.review_order,
                ReviewCardOrder::RetrievabilityAscending
            )
    }

    fn uses_rwkv_review_order(&self) -> bool {
        self.rwkv_review_queue_scores.is_some() && self.sort_options.uses_rwkv_review_order()
    }
}

impl QueueSortOptions {
    fn uses_rwkv_review_order(&self) -> bool {
        self.rwkv_review_enabled
            && self.rwkv_review_instant_order_enabled
            && matches!(
                self.review_order,
                ReviewCardOrder::RetrievabilityAscending
                    | ReviewCardOrder::RetrievabilityDescending
            )
    }

    fn uses_rwkv_retrievability_scores(&self) -> bool {
        self.uses_rwkv_review_order()
            || matches!(
                self.new_gather_priority,
                NewCardGatherPriority::AscendingRetrievability
                    | NewCardGatherPriority::DescendingRetrievability
            )
    }

    fn gather_review_order(&self) -> ReviewCardOrder {
        if self.rwkv_review_enabled
            && matches!(
                self.review_order,
                ReviewCardOrder::RetrievabilityAscending
                    | ReviewCardOrder::RetrievabilityDescending
            )
        {
            ReviewCardOrder::Day
        } else {
            self.review_order
        }
    }
}

fn sort_options(deck: &Deck, config_map: &HashMap<DeckConfigId, DeckConfig>) -> QueueSortOptions {
    deck.config_id()
        .and_then(|config_id| config_map.get(&config_id))
        .map(|config| QueueSortOptions {
            new_order: config.inner.new_card_sort_order(),
            new_gather_priority: config.inner.new_card_gather_priority(),
            review_order: config.inner.review_order(),
            day_learn_mix: config.inner.interday_learning_mix(),
            new_review_mix: config.inner.new_mix(),
            rwkv_review_enabled: config.inner.rwkv_review_enabled,
            rwkv_review_instant_order_enabled: config.inner.rwkv_review_instant_order_enabled,
            rwkv_review_allow_same_day_review: config.inner.rwkv_review_allow_same_day_review,
            rwkv_review_min_intervening_reviews: config.inner.rwkv_review_min_intervening_reviews,
            rwkv_review_min_elapsed_secs: config.inner.rwkv_review_min_elapsed_secs,
        })
        .unwrap_or_else(|| {
            // filtered decks do not space siblings
            QueueSortOptions {
                new_order: NewCardSortOrder::NoSort,
                ..Default::default()
            }
        })
}

fn merge_day_learning(
    reviews: Vec<DueCard>,
    day_learning: Vec<DueCard>,
    mode: ReviewMix,
) -> Box<dyn ExactSizeIterator<Item = MainQueueEntry>> {
    let day_learning_iter = day_learning.into_iter().map(Into::into);
    let reviews_iter = reviews.into_iter().map(Into::into);

    match mode {
        ReviewMix::AfterReviews => Box::new(SizedChain::new(reviews_iter, day_learning_iter)),
        ReviewMix::BeforeReviews => Box::new(SizedChain::new(day_learning_iter, reviews_iter)),
        ReviewMix::MixWithReviews => Box::new(Intersperser::new(reviews_iter, day_learning_iter)),
    }
}

fn merge_new(
    review_iter: impl ExactSizeIterator<Item = MainQueueEntry> + 'static,
    new: Vec<NewCard>,
    mode: ReviewMix,
) -> Box<dyn ExactSizeIterator<Item = MainQueueEntry>> {
    let new_iter = new.into_iter().map(Into::into);

    match mode {
        ReviewMix::BeforeReviews => Box::new(SizedChain::new(new_iter, review_iter)),
        ReviewMix::AfterReviews => Box::new(SizedChain::new(review_iter, new_iter)),
        ReviewMix::MixWithReviews => Box::new(Intersperser::new(review_iter, new_iter)),
    }
}

fn sort_learning(learning: Vec<DueCard>) -> VecDeque<LearningQueueEntry> {
    let mut entries: Vec<LearningQueueEntry> =
        learning.into_iter().map(LearningQueueEntry::from).collect();
    entries.sort_by(|a, b| a.cmp_by_reps_then_due(b));
    entries.into_iter().collect()
}

impl Collection {
    pub(crate) fn build_queues(&mut self, deck_id: DeckId) -> Result<CardQueues> {
        let mut queues = QueueBuilder::new(self, deck_id)?;
        self.storage
            .update_active_decks(&queues.context.root_deck)?;

        queues.gather_cards(self)?;

        let queues = queues.build(self.learn_ahead_secs() as i64);

        Ok(queues)
    }
}

#[cfg(test)]
mod test {
    use std::collections::HashMap;

    use anki_proto::deck_config::deck_config::config::NewCardGatherPriority;
    use anki_proto::deck_config::deck_config::config::NewCardSortOrder;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::search::SortMode;

    impl Collection {
        fn set_deck_gather_order(&mut self, deck: &mut Deck, order: NewCardGatherPriority) {
            let mut conf = DeckConfig::default();
            conf.inner.new_card_gather_priority = order as i32;
            conf.inner.new_card_sort_order = NewCardSortOrder::NoSort as i32;
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn set_deck_new_limit(&mut self, deck: &mut Deck, new_limit: u32) {
            let mut conf = DeckConfig::default();
            conf.inner.new_per_day = new_limit;
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn set_deck_review_limit(&mut self, deck: DeckId, limit: u32) {
            let dcid = self.get_deck(deck).unwrap().unwrap().config_id().unwrap();
            let mut conf = self.get_deck_config(dcid, false).unwrap().unwrap();
            conf.inner.reviews_per_day = limit;
            self.add_or_update_deck_config(&mut conf).unwrap();
        }

        fn queue_as_deck_and_template(&mut self, deck_id: DeckId) -> Vec<(DeckId, u16)> {
            self.build_queues(deck_id)
                .unwrap()
                .iter()
                .map(|entry| {
                    let card = self.storage.get_card(entry.card_id()).unwrap().unwrap();
                    (card.deck_id, card.template_idx)
                })
                .collect()
        }

        fn set_deck_review_order(&mut self, deck: &mut Deck, order: ReviewCardOrder) {
            let mut conf = DeckConfig::default();
            conf.inner.review_order = order as i32;
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn set_deck_rwkv_review_order(&mut self, deck: &mut Deck, order: ReviewCardOrder) {
            let mut conf = DeckConfig::default();
            conf.inner.review_order = order as i32;
            conf.inner.rwkv_review_enabled = true;
            conf.inner.rwkv_review_instant_order_enabled = true;
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn set_deck_rwkv_review_order_with_desired_retention(
            &mut self,
            deck: &mut Deck,
            order: ReviewCardOrder,
            desired_retention: f32,
        ) {
            self.set_deck_rwkv_review_order_with_options(deck, order, desired_retention, false);
        }

        fn set_deck_rwkv_review_order_with_options(
            &mut self,
            deck: &mut Deck,
            order: ReviewCardOrder,
            desired_retention: f32,
            allow_same_day_review: bool,
        ) {
            let mut conf = DeckConfig::default();
            conf.inner.review_order = order as i32;
            conf.inner.rwkv_review_enabled = true;
            conf.inner.rwkv_review_instant_order_enabled = true;
            conf.inner.desired_retention = desired_retention;
            conf.inner.rwkv_review_allow_same_day_review = allow_same_day_review;
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn set_deck_rwkv_review_order_with_repeat_guards(
            &mut self,
            deck: &mut Deck,
            order: ReviewCardOrder,
            desired_retention: f32,
            min_intervening_reviews: u32,
            min_elapsed_secs: u32,
        ) {
            let mut conf = DeckConfig::default();
            conf.inner.review_order = order as i32;
            conf.inner.rwkv_review_enabled = true;
            conf.inner.rwkv_review_instant_order_enabled = true;
            conf.inner.desired_retention = desired_retention;
            conf.inner.rwkv_review_allow_same_day_review = true;
            conf.inner.rwkv_review_min_intervening_reviews = min_intervening_reviews;
            conf.inner.rwkv_review_min_elapsed_secs = min_elapsed_secs;
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn queue_as_due_and_ivl(&mut self, deck_id: DeckId) -> Vec<(i32, u32)> {
            self.build_queues(deck_id)
                .unwrap()
                .iter()
                .map(|entry| {
                    let card = self.storage.get_card(entry.card_id()).unwrap().unwrap();
                    (card.due, card.interval)
                })
                .collect()
        }

        fn queue_as_ids(&mut self, deck_id: DeckId) -> Vec<CardId> {
            self.build_queues(deck_id)
                .unwrap()
                .iter()
                .map(|entry| entry.card_id())
                .collect()
        }

        fn queued_card_ids(&mut self, fetch_limit: usize) -> Result<Vec<CardId>> {
            Ok(self
                .get_queued_cards(fetch_limit, false, true)?
                .cards
                .into_iter()
                .map(|queued| queued.card.id)
                .collect())
        }
    }

    #[test]
    fn queued_cards_can_skip_scheduling_states() {
        let mut col = Collection::new();
        CardAdder::new().add(&mut col);

        let queued = col.get_queued_cards(1, false, true).unwrap();
        assert_eq!(queued.cards.len(), 1);
        assert!(queued.cards[0].states.is_none());

        let queued = col.get_queued_cards(1, false, false).unwrap();
        assert_eq!(queued.cards.len(), 1);
        assert!(queued.cards[0].states.is_some());
    }

    #[test]
    fn should_build_empty_queue_if_limit_is_reached() {
        let mut col = Collection::new();
        CardAdder::new().due_dates(["0"]).add(&mut col);
        col.set_deck_review_limit(DeckId(1), 0);
        assert_eq!(col.queue_as_deck_and_template(DeckId(1)), vec![]);
    }

    #[test]
    fn new_queue_building() -> Result<()> {
        let mut col = Collection::new();

        // parent
        // ┣━━child━━grandchild
        // ┗━━child_2
        let mut parent = DeckAdder::new("parent").add(&mut col);
        let mut child = DeckAdder::new("parent::child").add(&mut col);
        let child_2 = DeckAdder::new("parent::child_2").add(&mut col);
        let grandchild = DeckAdder::new("parent::child::grandchild").add(&mut col);

        // add 2 new cards to each deck
        for deck in [&parent, &child, &child_2, &grandchild] {
            CardAdder::new().siblings(2).deck(deck.id).add(&mut col);
        }

        // set child's new limit to 3, which should affect grandchild
        col.set_deck_new_limit(&mut child, 3);

        // depth-first tree order
        col.set_deck_gather_order(&mut parent, NewCardGatherPriority::Deck);
        let cards = vec![
            (parent.id, 0),
            (parent.id, 1),
            (child.id, 0),
            (child.id, 1),
            (grandchild.id, 0),
            (child_2.id, 0),
            (child_2.id, 1),
        ];
        assert_eq!(col.queue_as_deck_and_template(parent.id), cards);

        // insertion order
        col.set_deck_gather_order(&mut parent, NewCardGatherPriority::LowestPosition);
        let cards = vec![
            (parent.id, 0),
            (parent.id, 1),
            (child.id, 0),
            (child.id, 1),
            (child_2.id, 0),
            (child_2.id, 1),
            (grandchild.id, 0),
        ];
        assert_eq!(col.queue_as_deck_and_template(parent.id), cards);

        // inverted insertion order, but sibling order is preserved
        col.set_deck_gather_order(&mut parent, NewCardGatherPriority::HighestPosition);
        let cards = vec![
            (grandchild.id, 0),
            (grandchild.id, 1),
            (child_2.id, 0),
            (child_2.id, 1),
            (child.id, 0),
            (parent.id, 0),
            (parent.id, 1),
        ];
        assert_eq!(col.queue_as_deck_and_template(parent.id), cards);

        Ok(())
    }

    #[test]
    fn review_queue_building() -> Result<()> {
        let mut col = Collection::new();

        let mut deck = col.get_or_create_normal_deck("Default").unwrap();
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut cards = vec![];

        // relative overdueness
        let expected_queue = vec![
            (-150, 1),
            (-100, 1),
            (-50, 1),
            (-150, 5),
            (-100, 5),
            (-50, 5),
            (-150, 20),
            (-150, 20),
            (-100, 20),
            (-50, 20),
            (-150, 100),
            (-100, 100),
            (-50, 100),
            (0, 1),
            (0, 5),
            (0, 20),
            (0, 100),
        ];
        for t in expected_queue.iter() {
            let mut note = nt.new_note();
            note.set_field(0, "foo")?;
            note.id.0 = 0;
            col.add_note(&mut note, deck.id)?;
            let mut card = col.storage.get_card_by_ordinal(note.id, 0)?.unwrap();
            card.interval = t.1;
            card.due = t.0;
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            cards.push(card);
        }
        col.update_cards_maybe_undoable(cards, false)?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RelativeOverdueness);
        assert_eq!(col.queue_as_due_and_ivl(deck.id), expected_queue);

        Ok(())
    }

    #[test]
    fn fsrs_retrievability_order_ignores_stale_decay() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, deck.id)?;
        col.add_note(&mut note2, deck.id)?;

        let mut ids = col.search_cards("", SortMode::NoOrder)?;
        ids.sort();
        let timing = col.timing_today()?;
        let mut card1 = col.storage.get_card(ids[0])?.unwrap();
        let mut card2 = col.storage.get_card(ids[1])?.unwrap();
        for card in [&mut card1, &mut card2] {
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.due = 0;
            card.interval = 20;
            card.memory_state = Some(FsrsMemoryState {
                stability: 30.0,
                stability_internal: 30.0,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.desired_retention = Some(0.8);
            card.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));
        }
        card1.decay = Some(0.1);
        card2.decay = Some(2.0);
        col.storage.update_card(&card1)?;
        col.storage.update_card(&card2)?;

        // exact FSRS ordering should tie on identical state/elapsed/DR and fall
        // back to card id, not stale per-card decay.
        assert_eq!(col.queue_as_ids(deck.id), vec![card1.id, card2.id]);
        Ok(())
    }

    fn add_memory_state_card(
        col: &mut Collection,
        deck_id: DeckId,
        queue: CardQueue,
        ctype: CardType,
        due: i32,
        elapsed_secs: i64,
        stability: f32,
    ) -> Result<CardId> {
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "foo")?;
        col.add_note(&mut note, deck_id)?;
        let mut card = col.storage.get_card_by_ordinal(note.id, 0)?.unwrap();
        card.ctype = ctype;
        card.queue = queue;
        card.due = due;
        card.interval = 1;
        card.memory_state = Some(FsrsMemoryState {
            stability,
            stability_internal: stability,
            stability_fast: None,
            difficulty: 5.0,
        });
        card.desired_retention = Some(0.9);
        card.last_review_time = Some(TimestampSecs::now().adding_secs(-elapsed_secs));
        col.storage.update_card(&card)?;
        Ok(card.id)
    }

    #[test]
    fn fsrs_retrievability_order_interleaves_due_non_new_queues() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let day_learning = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::DayLearn,
            CardType::Relearn,
            timing.days_elapsed as i32,
            4 * 86_400,
            30.0,
        )?;
        let intraday_learning = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Learn,
            CardType::Relearn,
            (timing.now.0 - 1) as i32,
            6 * 86_400,
            30.0,
        )?;

        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![intraday_learning, day_learning, review]
        );
        assert_eq!(col.counts(), [0, 2, 1]);
        Ok(())
    }

    #[test]
    fn fsrs_retrievability_order_applies_limits_after_sorting_filtered_child_cards() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let mut parent = DeckAdder::new("Parent").add(&mut col);
        let study = DeckAdder::new("Parent::Study").add(&mut col);
        let filtered = DeckAdder::new("Parent::Filtered")
            .filtered(true)
            .add(&mut col);
        col.set_deck_review_order(&mut parent, ReviewCardOrder::RetrievabilityAscending);
        col.set_deck_review_limit(parent.id, 1);

        let timing = col.timing_today()?;
        let lower_retrievability_card = add_memory_state_card(
            &mut col,
            study.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            20 * 86_400,
            30.0,
        )?;
        let filtered_card = add_memory_state_card(
            &mut col,
            study.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            86_400,
            30.0,
        )?;

        let mut card = col.storage.get_card(filtered_card)?.unwrap();
        card.original_deck_id = card.deck_id;
        card.deck_id = filtered.id;
        card.original_due = card.due;
        card.due = -100_000;
        col.storage.update_card(&card)?;

        assert_eq!(col.queue_as_ids(parent.id), vec![lower_retrievability_card]);
        Ok(())
    }

    #[test]
    fn fsrs_retrievability_order_uses_actual_r_across_child_decks() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let mut parent = DeckAdder::new("Parent").add(&mut col);
        let low_r_deck = DeckAdder::new("Parent::LowR").add(&mut col);
        let high_r_deck = DeckAdder::new("Parent::HighR").add(&mut col);
        col.set_deck_review_order(&mut parent, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let low_r_card = add_memory_state_card(
            &mut col,
            low_r_deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            20 * 86_400,
            30.0,
        )?;
        let high_r_card = add_memory_state_card(
            &mut col,
            high_r_deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            86_400,
            30.0,
        )?;

        let mut low_r = col.storage.get_card(low_r_card)?.unwrap();
        low_r.desired_retention = Some(0.1);
        col.storage.update_card(&low_r)?;
        let mut high_r = col.storage.get_card(high_r_card)?.unwrap();
        high_r.desired_retention = Some(0.9999);
        col.storage.update_card(&high_r)?;

        let low_retrievability =
            col.fsrs_current_retrievability_for_card(low_r_card, 30.0, 20.0)?;
        let high_retrievability =
            col.fsrs_current_retrievability_for_card(high_r_card, 30.0, 1.0)?;
        assert!(low_retrievability < high_retrievability);
        assert_eq!(col.queue_as_ids(parent.id), vec![low_r_card, high_r_card]);
        Ok(())
    }

    #[test]
    fn fsrs_retrievability_order_excludes_future_intraday_learning() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Learn,
            CardType::Relearn,
            (timing.now.0 + 60) as i32,
            6 * 86_400,
            30.0,
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![review]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    #[test]
    fn fsrs_retrievability_order_preserves_new_card_mix() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);
        let deck_config_id = deck.config_id().unwrap();
        let mut config = col.get_deck_config(deck_config_id, false)?.unwrap();
        config.inner.new_mix = ReviewMix::BeforeReviews as i32;
        col.add_or_update_deck_config(&mut config)?;

        let new_card = CardAdder::new().add(&mut col)[0].id;
        let timing = col.timing_today()?;
        let review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![new_card, review]);
        Ok(())
    }

    #[test]
    fn rwkv_descending_retrievability_gathers_new_cards() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_gather_order(&mut deck, NewCardGatherPriority::DescendingRetrievability);

        let first = CardAdder::new().add(&mut col)[0].id;
        let second = CardAdder::new().add(&mut col)[0].id;
        let unscored = CardAdder::new().add(&mut col)[0].id;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(first, 0.10), (second, 0.80)]))?;

        assert_eq!(col.queue_as_ids(deck.id), vec![second, first, unscored]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_gather_uses_current_day_scores_when_deck_id_differs() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        let prepared_deck = DeckAdder::new("Prepared").add(&mut col);
        col.set_deck_gather_order(&mut deck, NewCardGatherPriority::DescendingRetrievability);

        let first = CardAdder::new().add(&mut col)[0].id;
        let second = CardAdder::new().add(&mut col)[0].id;
        col.set_rwkv_review_queue_scores(
            prepared_deck.id,
            HashMap::from([(first, 0.10), (second, 0.80)]),
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![second, first]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_gather_is_not_overridden_by_new_card_sort_order() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        let mut conf = DeckConfig::default();
        conf.inner.new_card_gather_priority =
            NewCardGatherPriority::DescendingRetrievability as i32;
        conf.inner.new_card_sort_order = NewCardSortOrder::Template as i32;
        col.add_or_update_deck_config(&mut conf)?;
        deck.normal_mut().unwrap().config_id = conf.id.0;
        col.add_or_update_deck(&mut deck)?;

        let siblings = CardAdder::new().siblings(2).add(&mut col);
        let lower_template = siblings[0].id;
        let higher_template = siblings[1].id;
        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(lower_template, 0.61), (higher_template, 0.83)]),
        )?;

        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![higher_template, lower_template]
        );
        Ok(())
    }

    #[test]
    fn rwkv_descending_retrievability_gathers_new_cards_across_child_decks() -> Result<()> {
        let mut col = Collection::new();
        let mut parent = DeckAdder::new("Parent").add(&mut col);
        let child1 = DeckAdder::new("Parent::Child 1").add(&mut col);
        let child2 = DeckAdder::new("Parent::Child 2").add(&mut col);
        let child3 = DeckAdder::new("Parent::Child 3").add(&mut col);
        col.set_deck_gather_order(&mut parent, NewCardGatherPriority::DescendingRetrievability);

        let card32 = CardAdder::new().deck(child1.id).add(&mut col)[0].id;
        let card33 = CardAdder::new().deck(child2.id).add(&mut col)[0].id;
        let card35 = CardAdder::new().deck(child3.id).add(&mut col)[0].id;
        col.set_rwkv_review_queue_scores(
            parent.id,
            HashMap::from([(card32, 0.32), (card33, 0.33), (card35, 0.35)]),
        )?;

        assert_eq!(col.queue_as_ids(parent.id), vec![card35, card33, card32]);
        Ok(())
    }

    #[test]
    fn rwkv_ascending_retrievability_gathers_new_cards() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_gather_order(&mut deck, NewCardGatherPriority::AscendingRetrievability);

        let first = CardAdder::new().add(&mut col)[0].id;
        let second = CardAdder::new().add(&mut col)[0].id;
        let unscored = CardAdder::new().add(&mut col)[0].id;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(first, 0.10), (second, 0.80)]))?;

        assert_eq!(col.queue_as_ids(deck.id), vec![first, second, unscored]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_can_include_future_review_cards() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        let other_deck = col.get_or_create_normal_deck("Other")?;
        col.set_deck_rwkv_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let due_review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let future_review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        let inactive_deck_review = add_memory_state_card(
            &mut col,
            other_deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([
                (due_review, 0.90),
                (future_review, 0.10),
                (inactive_deck_review, 0.01),
            ]),
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![future_review, due_review]);
        assert_eq!(col.counts(), [0, 0, 2]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_keeps_due_reviews_without_scores() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let due_review_without_score = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let future_review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(future_review, 0.10)]))?;

        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![future_review, due_review_without_score]
        );
        assert_eq!(col.counts(), [0, 0, 2]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_excludes_scores_above_desired_retention() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );

        let timing = col.timing_today()?;
        let due_high_rwkv_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let future_low_rwkv_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        for card_id in [due_high_rwkv_r, future_low_rwkv_r] {
            let mut card = col.storage.get_card(card_id)?.unwrap();
            card.desired_retention = None;
            col.storage.update_card(&card)?;
        }
        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(due_high_rwkv_r, 0.80), (future_low_rwkv_r, 0.20)]),
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![future_low_rwkv_r]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_uses_card_desired_retention_override() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.desired_retention = Some(0.85);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(card_id, 0.80)]))?;

        assert_eq!(col.queue_as_ids(deck.id), vec![card_id]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_excludes_scores_above_card_desired_retention() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.90,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.desired_retention = Some(0.90);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([(
                card_id,
                RwkvReviewQueueScoreEntry {
                    retrievability: 0.60,
                    intervening_reviews: None,
                    target_retention: Some(0.50),
                },
            )]),
        )?;

        assert!(col.queue_as_ids(deck.id).is_empty());
        assert_eq!(col.counts(), [0, 0, 0]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_excludes_same_day_reviews_by_default() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(card_id, 0.20)]))?;

        assert!(col.queue_as_ids(deck.id).is_empty());
        assert_eq!(col.counts(), [0, 0, 0]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_can_allow_same_day_reviews() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_options(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
            true,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(card_id, 0.20)]))?;

        assert_eq!(col.queue_as_ids(deck.id), vec![card_id]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_requires_min_intervening_reviews() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_repeat_guards(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
            2,
            0,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([(
                card_id,
                RwkvReviewQueueScoreEntry {
                    retrievability: 0.20,
                    intervening_reviews: Some(1),
                    target_retention: None,
                },
            )]),
        )?;

        assert!(col.queue_as_ids(deck.id).is_empty());
        assert_eq!(col.counts(), [0, 0, 0]);

        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([(
                card_id,
                RwkvReviewQueueScoreEntry {
                    retrievability: 0.20,
                    intervening_reviews: Some(2),
                    target_retention: None,
                },
            )]),
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![card_id]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    #[test]
    fn rwkv_intervening_review_update_patches_existing_queue_score() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_repeat_guards(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
            2,
            0,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([(
                card_id,
                RwkvReviewQueueScoreEntry {
                    retrievability: 0.20,
                    intervening_reviews: Some(1),
                    target_retention: None,
                },
            )]),
        )?;

        assert!(col.queue_as_ids(deck.id).is_empty());

        col.update_rwkv_review_queue_intervening_reviews(deck.id, HashMap::from([(card_id, 2)]))?;

        assert_eq!(col.queue_as_ids(deck.id), vec![card_id]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_requires_min_elapsed_secs() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_repeat_guards(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
            0,
            300,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now.adding_secs(-299));
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(card_id, 0.20)]))?;

        assert!(col.queue_as_ids(deck.id).is_empty());
        assert_eq!(col.counts(), [0, 0, 0]);

        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now.adding_secs(-300));
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(card_id, 0.20)]))?;

        assert_eq!(col.queue_as_ids(deck.id), vec![card_id]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    #[test]
    fn rwkv_unscored_due_reviews_do_not_use_fsrs_retrievability_order() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let older_due_high_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 - 10,
            10 * 86_400,
            1000.0,
        )?;
        let later_due_low_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            86_400,
            0.1,
        )?;
        let future_scored = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(future_scored, 0.10)]))?;

        let older_retrievability =
            col.fsrs_current_retrievability_for_card(older_due_high_r, 1000.0, 10.0)?;
        let later_retrievability =
            col.fsrs_current_retrievability_for_card(later_due_low_r, 0.1, 1.0)?;
        assert!(older_retrievability > later_retrievability);
        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![future_scored, older_due_high_r, later_due_low_r]
        );
        Ok(())
    }

    #[test]
    fn rwkv_review_order_with_empty_scores_does_not_use_fsrs_retrievability_order() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let older_due_high_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 - 10,
            10 * 86_400,
            1000.0,
        )?;
        let later_due_low_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            86_400,
            0.1,
        )?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::new())?;

        let older_retrievability =
            col.fsrs_current_retrievability_for_card(older_due_high_r, 1000.0, 10.0)?;
        let later_retrievability =
            col.fsrs_current_retrievability_for_card(later_due_low_r, 0.1, 1.0)?;
        assert!(older_retrievability > later_retrievability);
        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![older_due_high_r, later_due_low_r]
        );
        Ok(())
    }

    #[test]
    fn rwkv_review_without_instant_order_does_not_use_fsrs_retrievability_order() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        let mut conf = DeckConfig::default();
        conf.inner.review_order = ReviewCardOrder::RetrievabilityAscending as i32;
        conf.inner.rwkv_review_enabled = true;
        conf.inner.rwkv_review_instant_order_enabled = false;
        col.add_or_update_deck_config(&mut conf)?;
        deck.normal_mut().unwrap().config_id = conf.id.0;
        col.add_or_update_deck(&mut deck)?;

        let timing = col.timing_today()?;
        let older_due_high_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 - 10,
            10 * 86_400,
            1000.0,
        )?;
        let later_due_low_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            86_400,
            0.1,
        )?;

        let older_retrievability =
            col.fsrs_current_retrievability_for_card(older_due_high_r, 1000.0, 10.0)?;
        let later_retrievability =
            col.fsrs_current_retrievability_for_card(later_due_low_r, 0.1, 1.0)?;
        assert!(older_retrievability > later_retrievability);
        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![older_due_high_r, later_due_low_r]
        );
        Ok(())
    }

    #[test]
    fn rwkv_score_update_rebuilds_review_queue_with_new_scores() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_current_deck(deck.id)?;
        col.set_deck_rwkv_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let first = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let second = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let future = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(first, 0.10), (second, 0.20)]))?;

        assert_eq!(col.queued_card_ids(10)?, vec![first, second]);

        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(first, 0.90), (second, 0.05), (future, 0.01)]),
        )?;

        assert_eq!(col.queued_card_ids(10)?, vec![future, second, first]);
        assert_eq!(col.counts(), [0, 0, 3]);
        Ok(())
    }

    #[test]
    fn rwkv_score_update_rebuilds_displayed_review_queue_head() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_current_deck(deck.id)?;
        col.set_deck_rwkv_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let first = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let second = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(first, 0.10), (second, 0.20)]))?;

        assert_eq!(col.queued_card_ids(1)?, vec![first]);

        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(first, 0.90), (second, 0.05)]))?;

        assert_eq!(col.queued_card_ids(10)?, vec![second, first]);
        Ok(())
    }

    impl Collection {
        fn card_queue_len(&mut self) -> usize {
            self.get_queued_cards(5, false, false).unwrap().cards.len()
        }
    }

    #[test]
    fn new_card_potentially_burying_review_card() {
        let mut col = Collection::new();
        // add one new and one review card
        CardAdder::new().siblings(2).due_dates(["0"]).add(&mut col);
        // Potentially problematic config: New cards are shown first and would bury
        // review siblings. This poses a problem because we gather review cards first.
        col.update_default_deck_config(|config| {
            config.new_mix = ReviewMix::BeforeReviews as i32;
            config.bury_new = false;
            config.bury_reviews = true;
        });

        let old_queue_len = col.card_queue_len();
        col.answer_easy();
        col.clear_study_queues();

        // The number of cards in the queue must decrease by exactly 1, either because
        // no burying was performed, or the first built queue anticipated it and didn't
        // include the buried card.
        assert_eq!(col.card_queue_len(), old_queue_len - 1);
    }

    #[test]
    fn new_cards_may_ignore_review_limit() {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::NewCardsIgnoreReviewLimit, true, false)
            .unwrap();
        col.update_default_deck_config(|config| {
            config.reviews_per_day = 0;
        });
        CardAdder::new().add(&mut col);

        // review limit doesn't apply to new card
        assert_eq!(col.card_queue_len(), 1);
    }

    #[test]
    fn reviews_dont_affect_new_limit_before_review_limit_is_reached() {
        let mut col = Collection::new();
        col.update_default_deck_config(|config| {
            config.new_per_day = 1;
        });
        CardAdder::new().siblings(2).due_dates(["0"]).add(&mut col);
        assert_eq!(col.card_queue_len(), 2);
    }

    #[test]
    fn may_apply_parent_limits() {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::ApplyAllParentLimits, true, false)
            .unwrap();
        col.update_default_deck_config(|config| {
            config.new_per_day = 0;
        });
        let child = DeckAdder::new("Default::child")
            .with_config(|_| ())
            .add(&mut col);
        CardAdder::new().deck(child.id).add(&mut col);
        col.set_current_deck(child.id).unwrap();
        assert_eq!(col.card_queue_len(), 0);
    }
}
