// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::collections::HashSet;

use anki_proto::scheduler;
use anki_proto::scheduler::RwkvReviewInputRowsForCardsRequest;
use anki_proto::scheduler::RwkvReviewInputRowsForCardsResponse;
use anki_proto::scheduler::RwkvReviewInputRowsForDeckReviewQueueRequest;
use anki_proto::scheduler::RwkvReviewInputRowsForSearchRequest;

use crate::card::Card;
use crate::card::CardQueue;
use crate::card::CardType;
use crate::card::FsrsMemoryState;
use crate::deckconfig::DeckConfig;
use crate::deckconfig::DeckConfigId;
use crate::decks::Deck;
use crate::decks::DeckId;
use crate::ops::Op;
use crate::prelude::*;
use crate::scheduler::fsrs::preset::FsrsPresetId;
use crate::scheduler::timing::SchedTimingToday;
use crate::search::SortMode;

pub(crate) struct RwkvReviewRescheduleItem {
    pub(crate) card_id: CardId,
    pub(crate) interval_days: u32,
    pub(crate) elapsed_days: u32,
    pub(crate) s90: f32,
}

impl Collection {
    pub(crate) fn apply_rwkv_review_reschedule(
        &mut self,
        items: Vec<RwkvReviewRescheduleItem>,
    ) -> Result<OpOutput<usize>> {
        let timing = self.timing_today()?;
        let usn = self.usn()?;

        self.transact(Op::Custom("RWKV reschedule".into()), |col| {
            let mut updated = 0;
            for item in items {
                require!(item.interval_days >= 1, "invalid RWKV interval");
                require!(item.s90.is_finite() && item.s90 > 0.0, "invalid RWKV S90");

                let Some(mut card) = col.storage.get_card(item.card_id)? else {
                    continue;
                };
                if !(card.ctype == CardType::Review && card.queue == CardQueue::Review) {
                    continue;
                }

                let original = card.clone();
                card.interval = item.interval_days;
                card.memory_state = Some(rwkv_rescheduled_memory_state(&card, item.s90));

                let due = if card.original_due != 0 {
                    &mut card.original_due
                } else {
                    &mut card.due
                };
                *due = rwkv_rescheduled_due_day(
                    timing.days_elapsed,
                    item.elapsed_days,
                    item.interval_days,
                );

                col.update_card_inner(&mut card, original, usn)?;
                updated += 1;
            }

            Ok(updated)
        })
    }

    pub(crate) fn rwkv_review_input_rows_for_cards(
        &mut self,
        input: RwkvReviewInputRowsForCardsRequest,
    ) -> Result<RwkvReviewInputRowsForCardsResponse> {
        let card_ids: Vec<CardId> = input.card_ids.into_iter().map(Into::into).collect();
        if card_ids.is_empty() {
            return Ok(RwkvReviewInputRowsForCardsResponse::default());
        }

        let timing = self.timing_today()?;
        let decks_by_id = self.storage.get_decks_map()?;
        let configs_by_id = self.storage.get_deck_config_map()?;
        let enabled_deck_ids = (!input.include_disabled_decks)
            .then(|| rwkv_enabled_deck_ids(&decks_by_id, &configs_by_id));
        let cards = self.storage.rwkv_review_input_candidate_cards_for_ids(
            &card_ids,
            input.include_suspended_review,
            enabled_deck_ids.as_ref(),
        )?;
        let mut response = self.rwkv_review_input_rows_from_cards(
            cards,
            timing,
            &decks_by_id,
            &configs_by_id,
            input.include_suspended_review,
            input.include_disabled_decks,
        )?;
        response.searched_cards = card_ids.len() as u32;
        Ok(response)
    }

    pub(crate) fn rwkv_review_input_rows_for_search(
        &mut self,
        input: RwkvReviewInputRowsForSearchRequest,
    ) -> Result<RwkvReviewInputRowsForCardsResponse> {
        let timing = self.timing_today()?;
        let decks_by_id = self.storage.get_decks_map()?;
        let configs_by_id = self.storage.get_deck_config_map()?;
        let enabled_deck_ids = (!input.include_disabled_decks)
            .then(|| rwkv_enabled_deck_ids(&decks_by_id, &configs_by_id));
        let guard = self.search_cards_into_table(&input.search, SortMode::NoOrder)?;
        let searched_cards = guard.cards as u32;
        let cards = guard
            .col
            .storage
            .rwkv_review_input_candidate_cards_in_search(
                input.include_suspended_review,
                enabled_deck_ids.as_ref(),
            )?;
        let mut response = guard.col.rwkv_review_input_rows_from_cards(
            cards,
            timing,
            &decks_by_id,
            &configs_by_id,
            input.include_suspended_review,
            input.include_disabled_decks,
        )?;
        response.searched_cards = searched_cards;
        Ok(response)
    }

    pub(crate) fn rwkv_review_input_rows_for_deck_review_queue(
        &mut self,
        input: RwkvReviewInputRowsForDeckReviewQueueRequest,
    ) -> Result<RwkvReviewInputRowsForCardsResponse> {
        let deck_id = DeckId(input.deck_id);
        let Some(deck) = self.get_deck(deck_id)? else {
            return Ok(RwkvReviewInputRowsForCardsResponse::default());
        };
        let deck_ids = self.storage.deck_id_with_children(deck.as_ref())?;
        let timing = self.timing_today()?;
        let decks_by_id = self.storage.get_decks_map()?;
        let configs_by_id = self.storage.get_deck_config_map()?;
        let enabled_deck_ids = (!input.include_disabled_decks)
            .then(|| rwkv_enabled_deck_ids(&decks_by_id, &configs_by_id));
        let (searched_cards, cards) = self
            .storage
            .rwkv_review_input_candidate_cards_for_deck_review_queue(
                &deck_ids,
                enabled_deck_ids.as_ref(),
            )?;
        let mut response = self.rwkv_review_input_rows_from_cards(
            cards,
            timing,
            &decks_by_id,
            &configs_by_id,
            false,
            input.include_disabled_decks,
        )?;
        response.searched_cards = searched_cards;
        Ok(response)
    }

    fn rwkv_review_input_rows_from_cards(
        &mut self,
        mut cards: Vec<Card>,
        timing: SchedTimingToday,
        decks_by_id: &HashMap<DeckId, Deck>,
        configs_by_id: &HashMap<DeckConfigId, DeckConfig>,
        include_suspended_review: bool,
        include_disabled_decks: bool,
    ) -> Result<RwkvReviewInputRowsForCardsResponse> {
        self.populate_rwkv_last_review_times(&mut cards)?;

        let mut deck_config_decks = HashSet::new();
        let mut cards_with_supported_state = 0;
        let mut disabled_config_cards = 0;
        let mut eligible = Vec::new();
        let loaded_cards = cards.len() as u32;

        for card in cards {
            let Some(state) =
                self.rwkv_review_input_state(&card, timing, include_suspended_review)?
            else {
                continue;
            };
            cards_with_supported_state += 1;

            let current_deck_id = card.original_deck_id.or(card.deck_id);
            deck_config_decks.insert(current_deck_id);
            let Some(deck) = decks_by_id.get(&current_deck_id) else {
                continue;
            };
            let Some(config_id) = deck.config_id() else {
                continue;
            };
            let Some(config) = configs_by_id.get(&config_id) else {
                continue;
            };
            if !config.inner.rwkv_review_enabled && !include_disabled_decks {
                disabled_config_cards += 1;
                continue;
            }

            eligible.push(RwkvReviewInputRowPartial {
                target_retention: deck.effective_desired_retention(config),
                batch_size: config.inner.rwkv_review_batch_size,
                card,
                current_deck_id,
                state,
            });
        }

        let preset_cards: Vec<_> = eligible
            .iter()
            .map(|partial| partial.card.clone())
            .collect();
        let presets_by_card = self.fsrs_presets_for_cards(&preset_cards)?;
        let rows = eligible
            .into_iter()
            .filter_map(|partial| {
                let preset = presets_by_card.get(&partial.card.id)?;
                Some(scheduler::rwkv_review_input_rows_for_cards_response::Row {
                    card_id: partial.card.id.0,
                    note_id: partial.card.note_id.0,
                    deck_id: partial.current_deck_id.0,
                    preset_id: rwkv_fsrs_preset_id_to_string(preset.id.clone()),
                    card_type: partial.card.ctype as i32,
                    card_queue: partial.card.queue as i32,
                    card_due: partial.card.due,
                    interval_days: partial.card.interval,
                    ease_factor: partial.card.ease_factor.into(),
                    reps: partial.card.reps,
                    lapses: partial.card.lapses,
                    day_offset: timing.days_elapsed,
                    current_state_kind: partial.state.state_kind,
                    current_normal_state_kind: partial.state.normal_state_kind,
                    current_elapsed_days: partial.state.elapsed_days,
                    current_elapsed_seconds: partial.state.elapsed_seconds,
                    target_retention: valid_rwkv_target_retention(partial.target_retention),
                    batch_size: partial.batch_size,
                })
            })
            .collect();

        Ok(RwkvReviewInputRowsForCardsResponse {
            rows,
            loaded_cards,
            cards_with_supported_state,
            disabled_config_cards,
            deck_configs: deck_config_decks.len() as u32,
            searched_cards: 0,
        })
    }

    fn rwkv_review_input_state(
        &self,
        card: &Card,
        timing: SchedTimingToday,
        include_suspended_review: bool,
    ) -> Result<Option<RwkvReviewInputState>> {
        match (card.ctype, card.queue) {
            (CardType::Review, CardQueue::Review | CardQueue::Suspended) => {
                if card.queue == CardQueue::Suspended && !include_suspended_review {
                    return Ok(None);
                }

                let elapsed_days = self.rwkv_last_review_time(card)?.map(|last_review_time| {
                    timing.next_day_at.elapsed_days_since(last_review_time) as u32
                });
                Ok(Some(RwkvReviewInputState {
                    state_kind: elapsed_days
                        .map(|_| "normal".to_string())
                        .unwrap_or_default(),
                    normal_state_kind: elapsed_days
                        .map(|_| "review".to_string())
                        .unwrap_or_default(),
                    elapsed_days,
                    elapsed_seconds: None,
                }))
            }
            (CardType::Learn, CardQueue::Learn | CardQueue::DayLearn) => {
                let elapsed_seconds = self.rwkv_last_review_time(card)?.map(|last_review_time| {
                    TimestampSecs::now()
                        .elapsed_secs_since(last_review_time)
                        .max(0) as u32
                });
                Ok(Some(RwkvReviewInputState {
                    state_kind: elapsed_seconds
                        .map(|_| "normal".to_string())
                        .unwrap_or_default(),
                    normal_state_kind: elapsed_seconds
                        .map(|_| "learning".to_string())
                        .unwrap_or_default(),
                    elapsed_days: None,
                    elapsed_seconds,
                }))
            }
            (CardType::Relearn, CardQueue::Learn | CardQueue::DayLearn) => {
                let Some(last_review_time) = self.rwkv_last_review_time(card)? else {
                    return Ok(Some(RwkvReviewInputState {
                        state_kind: String::new(),
                        normal_state_kind: String::new(),
                        elapsed_days: None,
                        elapsed_seconds: None,
                    }));
                };
                Ok(Some(RwkvReviewInputState {
                    state_kind: "normal".to_string(),
                    normal_state_kind: "relearning".to_string(),
                    elapsed_days: Some(
                        timing.next_day_at.elapsed_days_since(last_review_time) as u32
                    ),
                    elapsed_seconds: Some(
                        TimestampSecs::now()
                            .elapsed_secs_since(last_review_time)
                            .max(0) as u32,
                    ),
                }))
            }
            _ => Ok(None),
        }
    }

    fn rwkv_last_review_time(&self, card: &Card) -> Result<Option<TimestampSecs>> {
        Ok(card.last_review_time)
    }

    fn populate_rwkv_last_review_times(&self, cards: &mut [Card]) -> Result<()> {
        let missing_card_ids: Vec<_> = cards
            .iter()
            .filter(|card| card.last_review_time.is_none())
            .map(|card| card.id)
            .collect();
        if missing_card_ids.is_empty() {
            return Ok(());
        }

        let review_times = self.storage.times_of_last_review(&missing_card_ids)?;
        for card in cards {
            if card.last_review_time.is_none() {
                card.last_review_time = review_times.get(&card.id).copied();
            }
        }

        Ok(())
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct RwkvReviewCandidateMetadata {
    pub(crate) target_retention: f32,
    pub(crate) reviewed_today: bool,
    pub(crate) current_deck_id: DeckId,
    pub(crate) fsrs_due_today: bool,
}

pub(crate) fn rwkv_review_candidate_metadata(
    col: &mut Collection,
    card_ids: &[CardId],
    timing: SchedTimingToday,
) -> Result<HashMap<CardId, RwkvReviewCandidateMetadata>> {
    let cards = col.all_cards_for_ids(card_ids, false)?;
    let mut metadata = HashMap::with_capacity(cards.len());
    let mut partial_by_card = HashMap::new();
    let mut without_card_target = Vec::new();

    for card in cards {
        if card.queue != CardQueue::Review {
            continue;
        }

        let partial = RwkvReviewCandidatePartial {
            reviewed_today: card_reviewed_today(&card, timing),
            current_deck_id: card.deck_id,
            fsrs_due_today: card.due <= timing.days_elapsed as i32,
        };
        if let Some(desired_retention) = card_desired_retention(&card) {
            metadata.insert(card.id, partial.with_target_retention(desired_retention));
        } else {
            partial_by_card.insert(card.id, partial);
            without_card_target.push(card);
        }
    }

    for (card_id, preset) in col.fsrs_presets_for_cards(&without_card_target)? {
        if let Some(partial) = partial_by_card.remove(&card_id) {
            metadata.insert(
                card_id,
                partial.with_target_retention(preset.desired_retention),
            );
        }
    }

    Ok(metadata)
}

pub(crate) fn rwkv_review_score_eligible(
    score: f32,
    metadata: &RwkvReviewCandidateMetadata,
    allow_same_day_review: bool,
) -> bool {
    score.is_finite()
        && score <= metadata.target_retention
        && (allow_same_day_review || !metadata.reviewed_today)
}

#[derive(Debug, Clone, Copy)]
struct RwkvReviewCandidatePartial {
    reviewed_today: bool,
    current_deck_id: DeckId,
    fsrs_due_today: bool,
}

impl RwkvReviewCandidatePartial {
    fn with_target_retention(self, target_retention: f32) -> RwkvReviewCandidateMetadata {
        RwkvReviewCandidateMetadata {
            target_retention,
            reviewed_today: self.reviewed_today,
            current_deck_id: self.current_deck_id,
            fsrs_due_today: self.fsrs_due_today,
        }
    }
}

#[derive(Debug)]
struct RwkvReviewInputRowPartial {
    card: Card,
    current_deck_id: DeckId,
    state: RwkvReviewInputState,
    target_retention: f32,
    batch_size: u32,
}

#[derive(Debug)]
struct RwkvReviewInputState {
    state_kind: String,
    normal_state_kind: String,
    elapsed_days: Option<u32>,
    elapsed_seconds: Option<u32>,
}

fn rwkv_fsrs_preset_id_to_string(id: FsrsPresetId) -> String {
    match id {
        FsrsPresetId::DeckConfig(id) => id.0.to_string(),
        FsrsPresetId::Addon(id) => id,
    }
}

fn valid_rwkv_target_retention(target_retention: f32) -> f32 {
    if target_retention.is_finite() && (0.0..=1.0).contains(&target_retention) {
        target_retention
    } else {
        0.9
    }
}

fn rwkv_enabled_deck_ids(
    decks_by_id: &HashMap<DeckId, Deck>,
    configs_by_id: &HashMap<DeckConfigId, DeckConfig>,
) -> HashSet<DeckId> {
    decks_by_id
        .iter()
        .filter_map(|(deck_id, deck)| {
            let config_id = deck.config_id()?;
            configs_by_id
                .get(&config_id)
                .is_some_and(|config| config.inner.rwkv_review_enabled)
                .then_some(*deck_id)
        })
        .collect()
}

fn card_desired_retention(card: &Card) -> Option<f32> {
    card.desired_retention
        .filter(|dr| dr.is_finite() && (0.0..1.0).contains(dr))
}

fn card_reviewed_today(card: &Card, timing: SchedTimingToday) -> bool {
    card.last_review_time.is_some_and(|last_review_time| {
        let today_start = timing.next_day_at.0.saturating_sub(86_400);
        last_review_time.0 >= today_start && last_review_time.0 < timing.next_day_at.0
    })
}

fn rwkv_rescheduled_memory_state(card: &Card, s90: f32) -> FsrsMemoryState {
    let existing = card.memory_state;
    FsrsMemoryState {
        stability: s90,
        stability_internal: existing
            .map(|state| state.stability_internal)
            .filter(|stability| stability.is_finite() && *stability > 0.0)
            .unwrap_or(s90),
        difficulty: existing
            .map(|state| state.difficulty)
            .filter(|difficulty| difficulty.is_finite() && *difficulty > 0.0)
            .unwrap_or(5.0),
    }
}

fn rwkv_rescheduled_due_day(today: u32, elapsed_days: u32, interval_days: u32) -> i32 {
    ((today as i64) - (elapsed_days as i64) + (interval_days as i64)).clamp(0, i32::MAX as i64)
        as i32
}

#[cfg(test)]
mod test {
    use anki_proto::scheduler::RwkvReviewInputRowsForCardsRequest;
    use anki_proto::scheduler::RwkvReviewInputRowsForDeckReviewQueueRequest;
    use anki_proto::scheduler::RwkvReviewInputRowsForSearchRequest;

    use super::*;
    use crate::notes::NoteId;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;

    #[test]
    fn apply_review_reschedule_does_not_write_revlog() -> Result<()> {
        let mut col = Collection::new();
        let timing = col.timing_today()?;
        let mut card = Card::new(NoteId(10), 0, DeckId(1), timing.days_elapsed as i32 + 8);
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 4;
        col.add_card(&mut card)?;

        let revlogs_before = col.storage.get_revlog_entries_for_card(card.id)?.len();
        let result = col.apply_rwkv_review_reschedule(vec![RwkvReviewRescheduleItem {
            card_id: card.id,
            interval_days: 12,
            elapsed_days: 4,
            s90: 9.5,
        }])?;

        let updated = col.storage.get_card(card.id)?.unwrap();
        assert_eq!(result.output, 1);
        assert_eq!(updated.interval, 12);
        assert_eq!(updated.memory_state.unwrap().stability, 9.5);
        assert_eq!(
            col.storage.get_revlog_entries_for_card(card.id)?.len(),
            revlogs_before
        );

        Ok(())
    }

    #[test]
    fn review_input_rows_return_eligible_review_cards() -> Result<()> {
        let mut col = Collection::new();
        col.update_default_deck_config(|config| {
            config.rwkv_review_enabled = true;
            config.rwkv_review_batch_size = 1024;
            config.desired_retention = 0.86;
        });
        let timing = col.timing_today()?;
        let last_review_time = timing.next_day_at.adding_secs(-39 * 86_400);
        let mut card = Card::new(NoteId(10), 0, DeckId(1), timing.days_elapsed as i32 + 8);
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 4;
        card.ease_factor = 2500;
        card.reps = 5;
        card.lapses = 1;
        card.last_review_time = Some(last_review_time);
        col.add_card(&mut card)?;

        let response =
            col.rwkv_review_input_rows_for_cards(RwkvReviewInputRowsForCardsRequest {
                card_ids: vec![card.id.0],
                include_suspended_review: false,
                include_disabled_decks: false,
            })?;

        assert_eq!(response.loaded_cards, 1);
        assert_eq!(response.cards_with_supported_state, 1);
        assert_eq!(response.disabled_config_cards, 0);
        assert_eq!(response.deck_configs, 1);
        assert_eq!(response.rows.len(), 1);
        let row = &response.rows[0];
        assert_eq!(row.card_id, card.id.0);
        assert_eq!(row.note_id, card.note_id.0);
        assert_eq!(row.deck_id, 1);
        assert_eq!(row.preset_id, "1");
        assert_eq!(row.card_type, CardType::Review as i32);
        assert_eq!(row.card_queue, CardQueue::Review as i32);
        assert_eq!(row.card_due, card.due);
        assert_eq!(row.interval_days, 4);
        assert_eq!(row.ease_factor, 2500);
        assert_eq!(row.reps, 5);
        assert_eq!(row.lapses, 1);
        assert_eq!(row.day_offset, timing.days_elapsed);
        assert_eq!(row.current_state_kind, "normal");
        assert_eq!(row.current_normal_state_kind, "review");
        assert_eq!(row.current_elapsed_days, Some(39));
        assert_eq!(row.current_elapsed_seconds, None);
        assert_eq!(row.target_retention, 0.86);
        assert_eq!(row.batch_size, 1024);

        Ok(())
    }

    #[test]
    fn review_input_rows_use_revlog_last_review_time_when_card_data_missing() -> Result<()> {
        let mut col = Collection::new();
        col.update_default_deck_config(|config| {
            config.rwkv_review_enabled = true;
            config.rwkv_review_batch_size = 1024;
        });
        let timing = col.timing_today()?;
        let last_review_time = timing.next_day_at.adding_secs(-39 * 86_400);
        let ignored_filtered_time = timing.next_day_at.adding_secs(-3 * 86_400);
        let mut card = Card::new(NoteId(10), 0, DeckId(1), timing.days_elapsed as i32 + 8);
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 4;
        card.ease_factor = 2500;
        card.reps = 5;
        card.lapses = 1;
        col.add_card(&mut card)?;
        col.storage.add_revlog_entry(
            &RevlogEntry {
                id: RevlogId(last_review_time.0 * 1000),
                cid: card.id,
                usn: Usn(0),
                button_chosen: 3,
                interval: 4,
                last_interval: 3,
                ease_factor: 2500,
                review_kind: RevlogReviewKind::Review,
                ..Default::default()
            },
            false,
        )?;
        col.storage.add_revlog_entry(
            &RevlogEntry {
                id: RevlogId(ignored_filtered_time.0 * 1000),
                cid: card.id,
                usn: Usn(0),
                button_chosen: 3,
                review_kind: RevlogReviewKind::Filtered,
                ..Default::default()
            },
            false,
        )?;

        let response =
            col.rwkv_review_input_rows_for_cards(RwkvReviewInputRowsForCardsRequest {
                card_ids: vec![card.id.0],
                include_suspended_review: false,
                include_disabled_decks: false,
            })?;

        assert_eq!(response.loaded_cards, 1);
        assert_eq!(response.cards_with_supported_state, 1);
        assert_eq!(response.rows.len(), 1);
        assert_eq!(response.rows[0].current_elapsed_days, Some(39));

        Ok(())
    }

    #[test]
    fn review_input_rows_filter_disabled_decks_before_loading() -> Result<()> {
        let mut col = Collection::new();
        let timing = col.timing_today()?;
        let mut card = Card::new(NoteId(10), 0, DeckId(1), timing.days_elapsed as i32);
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 4;
        card.last_review_time = Some(timing.next_day_at.adding_secs(-4 * 86_400));
        col.add_card(&mut card)?;

        let filtered =
            col.rwkv_review_input_rows_for_cards(RwkvReviewInputRowsForCardsRequest {
                card_ids: vec![card.id.0],
                include_suspended_review: false,
                include_disabled_decks: false,
            })?;
        assert_eq!(filtered.loaded_cards, 0);
        assert!(filtered.rows.is_empty());

        let included =
            col.rwkv_review_input_rows_for_cards(RwkvReviewInputRowsForCardsRequest {
                card_ids: vec![card.id.0],
                include_suspended_review: false,
                include_disabled_decks: true,
            })?;
        assert_eq!(included.loaded_cards, 1);
        assert_eq!(included.rows.len(), 1);

        Ok(())
    }

    #[test]
    fn review_input_rows_for_search_uses_search_table() -> Result<()> {
        let mut col = Collection::new();
        col.update_default_deck_config(|config| {
            config.rwkv_review_enabled = true;
            config.rwkv_review_batch_size = 1024;
        });
        let timing = col.timing_today()?;
        let mut review_card = Card::new(NoteId(10), 0, DeckId(1), timing.days_elapsed as i32);
        review_card.ctype = CardType::Review;
        review_card.queue = CardQueue::Review;
        review_card.interval = 4;
        review_card.last_review_time = Some(timing.next_day_at.adding_secs(-4 * 86_400));
        col.add_card(&mut review_card)?;
        let mut new_card = Card::new(NoteId(20), 0, DeckId(1), timing.days_elapsed as i32);
        col.add_card(&mut new_card)?;

        let response =
            col.rwkv_review_input_rows_for_search(RwkvReviewInputRowsForSearchRequest {
                search: format!("cid:{},{}", review_card.id.0, new_card.id.0),
                include_suspended_review: false,
                include_disabled_decks: false,
            })?;

        assert_eq!(response.searched_cards, 2);
        assert_eq!(response.loaded_cards, 1);
        assert_eq!(response.cards_with_supported_state, 1);
        assert_eq!(response.rows.len(), 1);
        assert_eq!(response.rows[0].card_id, review_card.id.0);

        Ok(())
    }

    #[test]
    fn review_input_rows_for_deck_review_queue_uses_child_decks() -> Result<()> {
        let mut col = Collection::new();
        col.update_default_deck_config(|config| {
            config.rwkv_review_enabled = true;
            config.rwkv_review_batch_size = 1024;
        });
        let parent = col.get_or_create_normal_deck("Parent")?;
        let child = col.get_or_create_normal_deck("Parent::Child")?;
        let timing = col.timing_today()?;
        let last_review_time = timing.next_day_at.adding_secs(-4 * 86_400);
        let mut review_card = Card::new(NoteId(10), 0, child.id, timing.days_elapsed as i32 + 8);
        review_card.ctype = CardType::Review;
        review_card.queue = CardQueue::Review;
        review_card.interval = 4;
        review_card.last_review_time = Some(last_review_time);
        col.add_card(&mut review_card)?;
        let mut new_card = Card::new(NoteId(20), 0, child.id, timing.days_elapsed as i32);
        col.add_card(&mut new_card)?;

        let response = col.rwkv_review_input_rows_for_deck_review_queue(
            RwkvReviewInputRowsForDeckReviewQueueRequest {
                deck_id: parent.id.0,
                include_disabled_decks: false,
            },
        )?;

        assert_eq!(response.searched_cards, 1);
        assert_eq!(response.loaded_cards, 1);
        assert_eq!(response.cards_with_supported_state, 1);
        assert_eq!(response.rows.len(), 1);
        assert_eq!(response.rows[0].card_id, review_card.id.0);
        assert_eq!(response.rows[0].deck_id, child.id.0);

        Ok(())
    }
}
