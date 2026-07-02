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
use crate::notes::NoteId;
use crate::ops::Op;
use crate::prelude::*;
use crate::scheduler::fsrs::preset::FsrsPresetId;
use crate::scheduler::timing::SchedTimingToday;
use crate::search::SortMode;
use crate::storage::ids_to_string;

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
                preset_tag_state_enabled: config.inner.rwkv_review_preset_tag_state_enabled,
                japanese_feature_state_enabled: config
                    .inner
                    .rwkv_review_japanese_feature_state_enabled,
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
        let note_tags_by_id = if eligible
            .iter()
            .any(|partial| partial.preset_tag_state_enabled)
        {
            let note_ids: Vec<_> = eligible
                .iter()
                .filter(|partial| partial.preset_tag_state_enabled)
                .map(|partial| partial.card.note_id)
                .collect();
            self.storage
                .get_note_tags_by_id_list(&note_ids)?
                .into_iter()
                .map(|tags| (tags.id, tags.tags))
                .collect::<HashMap<NoteId, String>>()
        } else {
            HashMap::new()
        };
        let japanese_features_by_id = if eligible
            .iter()
            .any(|partial| partial.japanese_feature_state_enabled)
        {
            let mut note_ids: Vec<_> = eligible
                .iter()
                .filter(|partial| partial.japanese_feature_state_enabled)
                .map(|partial| partial.card.note_id)
                .collect();
            note_ids.sort_unstable();
            note_ids.dedup();
            self.rwkv_japanese_feature_buckets_by_note_id(&note_ids)?
        } else {
            HashMap::new()
        };
        let rows = eligible
            .into_iter()
            .filter_map(|partial| {
                let preset = presets_by_card.get(&partial.card.id)?;
                let base_preset_id = rwkv_fsrs_preset_id_to_string(preset.id.clone());
                let note_tags = partial
                    .preset_tag_state_enabled
                    .then(|| {
                        note_tags_by_id
                            .get(&partial.card.note_id)
                            .map(String::as_str)
                    })
                    .flatten();
                let preset_id = rwkv_preset_id_with_tags(&base_preset_id, note_tags);
                let japanese_features = partial
                    .japanese_feature_state_enabled
                    .then(|| {
                        japanese_features_by_id
                            .get(&partial.card.note_id)
                            .map(String::as_str)
                    })
                    .flatten();
                Some(scheduler::rwkv_review_input_rows_for_cards_response::Row {
                    card_id: partial.card.id.0,
                    note_id: partial.card.note_id.0,
                    deck_id: partial.current_deck_id.0,
                    preset_id: rwkv_preset_id_with_japanese_features(&preset_id, japanese_features),
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

    fn rwkv_japanese_feature_buckets_by_note_id(
        &self,
        note_ids: &[NoteId],
    ) -> Result<HashMap<NoteId, String>> {
        if note_ids.is_empty() {
            return Ok(HashMap::new());
        }

        let mut sql = String::from(
            "select n.id, n.flds, front.ord, reading.ord, front_kana.ord, frequency.ord \
             from notes n \
             left join fields front on front.ntid = n.mid and front.name = 'Front' \
             left join fields reading on reading.ntid = n.mid and reading.name = 'Reading' \
             left join fields front_kana on front_kana.ntid = n.mid and front_kana.name = 'Front_Kana' \
             left join fields frequency on frequency.ntid = n.mid and frequency.name = 'Frequency' \
             where n.id in ",
        );
        ids_to_string(&mut sql, note_ids);

        let mut by_note_id = HashMap::new();
        let mut stmt = self.storage.db.prepare(&sql)?;
        for row in stmt.query_and_then([], |row| -> Result<_> {
            let note_id: NoteId = row.get(0)?;
            let fields_raw: String = row.get(1)?;
            let front_ord: Option<i64> = row.get(2)?;
            let reading_ord: Option<i64> = row.get(3)?;
            let front_kana_ord: Option<i64> = row.get(4)?;
            let frequency_ord: Option<i64> = row.get(5)?;
            let fields = fields_raw.split('\x1f').collect::<Vec<_>>();
            Ok(rwkv_japanese_feature_bucket(
                rwkv_field_at_ord(&fields, front_ord),
                rwkv_field_at_ord(&fields, reading_ord),
                rwkv_field_at_ord(&fields, front_kana_ord),
                rwkv_field_at_ord(&fields, frequency_ord),
            )
            .map(|bucket| (note_id, bucket)))
        })? {
            if let Some((note_id, bucket)) = row? {
                by_note_id.insert(note_id, bucket);
            }
        }

        Ok(by_note_id)
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
    preset_tag_state_enabled: bool,
    japanese_feature_state_enabled: bool,
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

fn rwkv_preset_id_with_tags(base_preset_id: &str, note_tags: Option<&str>) -> String {
    let tags = rwkv_clean_preset_tags(note_tags.unwrap_or_default());
    if tags.is_empty() {
        return base_preset_id.to_string();
    }

    format!("rwkv-preset-tags:{base_preset_id}:{}", tags.join("\x1f"))
}

fn rwkv_preset_id_with_japanese_features(
    base_preset_id: &str,
    feature_bucket: Option<&str>,
) -> String {
    match feature_bucket.filter(|bucket| !bucket.is_empty()) {
        Some(bucket) => format!("rwkv-japanese-features:{base_preset_id}:{bucket}"),
        None => base_preset_id.to_string(),
    }
}

fn rwkv_japanese_feature_bucket(
    front: &str,
    reading: &str,
    front_kana: &str,
    frequency: &str,
) -> Option<String> {
    let front_len = rwkv_non_whitespace_char_count(front);
    if front_len == 0 {
        return None;
    }

    let kanji_count = front.chars().filter(|ch| rwkv_is_kanji(*ch)).count();
    let kana_count = front.chars().filter(|ch| rwkv_is_kana(*ch)).count();
    let kanji_ratio_bucket = ((kanji_count * 10) + (front_len / 2)) / front_len;
    let shape = match (kanji_count > 0, kana_count > 0) {
        (true, true) => "mixed",
        (true, false) => "kanji",
        (false, true) => "kana",
        (false, false) => "other",
    };

    Some(format!(
        "wl:{}|rl:{}|kc:{}|kana:{}|kr:{}|shape:{}|fkl:{}|freq:{}",
        rwkv_japanese_length_bucket(front_len),
        rwkv_japanese_length_bucket(rwkv_non_whitespace_char_count(reading)),
        rwkv_japanese_count_bucket(kanji_count),
        rwkv_japanese_count_bucket(kana_count),
        kanji_ratio_bucket.min(10),
        shape,
        rwkv_japanese_length_bucket(rwkv_non_whitespace_char_count(front_kana)),
        rwkv_japanese_frequency_bucket(frequency),
    ))
}

fn rwkv_field_at_ord<'a>(fields: &'a [&'a str], ord: Option<i64>) -> &'a str {
    ord.and_then(|ord| usize::try_from(ord).ok())
        .and_then(|ord| fields.get(ord).copied())
        .unwrap_or_default()
}

fn rwkv_non_whitespace_char_count(value: &str) -> usize {
    value.chars().filter(|ch| !ch.is_whitespace()).count()
}

fn rwkv_japanese_length_bucket(count: usize) -> String {
    match count {
        0..=6 => count.to_string(),
        7..=10 => "7-10".to_string(),
        _ => "11+".to_string(),
    }
}

fn rwkv_japanese_count_bucket(count: usize) -> String {
    match count {
        0..=7 => count.to_string(),
        _ => "8+".to_string(),
    }
}

fn rwkv_japanese_frequency_bucket(frequency: &str) -> String {
    let Some(rank) = rwkv_first_unsigned_number(frequency) else {
        return if frequency.trim().is_empty() {
            "0"
        } else {
            "text"
        }
        .to_string();
    };

    match rank {
        0 => "0",
        1..=100 => "1-100",
        101..=500 => "101-500",
        501..=1_000 => "501-1000",
        1_001..=2_000 => "1001-2000",
        2_001..=5_000 => "2001-5000",
        5_001..=10_000 => "5001-10000",
        10_001..=20_000 => "10001-20000",
        _ => "20001+",
    }
    .to_string()
}

fn rwkv_first_unsigned_number(value: &str) -> Option<u32> {
    let mut digits = String::new();
    let mut in_number = false;
    for ch in value.chars() {
        if ch.is_ascii_digit() {
            digits.push(ch);
            in_number = true;
        } else if in_number {
            break;
        }
    }
    digits.parse().ok()
}

fn rwkv_is_kanji(ch: char) -> bool {
    matches!(
        ch as u32,
        0x3400..=0x4DBF
            | 0x4E00..=0x9FFF
            | 0xF900..=0xFAFF
            | 0x20000..=0x2A6DF
            | 0x2A700..=0x2B73F
            | 0x2B740..=0x2B81F
            | 0x2B820..=0x2CEAF
            | 0x2CEB0..=0x2EBEF
            | 0x30000..=0x3134F
            | 0x31350..=0x323AF
    )
}

fn rwkv_is_kana(ch: char) -> bool {
    matches!(
        ch as u32,
        0x3040..=0x30FF | 0x31F0..=0x31FF | 0xFF66..=0xFF9F
    )
}

fn rwkv_clean_preset_tags(note_tags: &str) -> Vec<String> {
    let mut tags = note_tags
        .split_whitespace()
        .filter(|tag| !rwkv_preset_tag_is_outcome(tag))
        .map(str::to_string)
        .collect::<Vec<_>>();
    tags.sort();
    tags.dedup();
    tags
}

fn rwkv_preset_tag_is_outcome(tag: &str) -> bool {
    let tag = tag.to_ascii_lowercase();
    if tag == "leech" || tag.starts_with("am-") || tag.starts_with("ankimorphs::") {
        return true;
    }

    tag.split([':', '-', '_', '/', '\\']).any(|part| {
        matches!(
            part,
            "leech" | "fail" | "failed" | "failure" | "lapse" | "lapsed" | "wrong" | "missed"
        )
    })
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
    fn review_input_rows_can_fold_clean_tags_into_preset_id() -> Result<()> {
        let mut col = Collection::new();
        col.update_default_deck_config(|config| {
            config.rwkv_review_enabled = true;
            config.rwkv_review_preset_tag_state_enabled = true;
        });
        let mut note = col.basic_notetype().new_note();
        note.fields_mut()[0] = "front".into();
        note.tags = vec![
            "Yomitan".into(),
            "leech".into(),
            "am-ready".into(),
            "moeway-debut-idol-fail".into(),
            "Claude-Translated".into(),
        ];
        col.add_note(&mut note, DeckId(1))?;
        let mut card = col.storage.all_cards_of_note(note.id)?.remove(0);
        let timing = col.timing_today()?;
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.last_review_time = Some(timing.next_day_at.adding_secs(-4 * 86_400));
        col.storage.update_card(&card)?;

        let response =
            col.rwkv_review_input_rows_for_cards(RwkvReviewInputRowsForCardsRequest {
                card_ids: vec![card.id.0],
                include_suspended_review: false,
                include_disabled_decks: false,
            })?;

        assert_eq!(response.rows.len(), 1);
        assert_eq!(
            response.rows[0].preset_id,
            rwkv_preset_id_with_tags(
                "1",
                Some("Yomitan leech am-ready moeway-debut-idol-fail Claude-Translated")
            )
        );

        Ok(())
    }

    #[test]
    fn review_input_rows_can_fold_japanese_features_into_preset_id() -> Result<()> {
        let mut col = Collection::new();
        col.update_default_deck_config(|config| {
            config.rwkv_review_enabled = true;
            config.rwkv_review_japanese_feature_state_enabled = true;
        });
        let mut note = col.basic_notetype().new_note();
        note.fields_mut()[0] = "食べる".into();
        col.add_note(&mut note, DeckId(1))?;
        let mut card = col.storage.all_cards_of_note(note.id)?.remove(0);
        let timing = col.timing_today()?;
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.last_review_time = Some(timing.next_day_at.adding_secs(-4 * 86_400));
        col.storage.update_card(&card)?;

        let response =
            col.rwkv_review_input_rows_for_cards(RwkvReviewInputRowsForCardsRequest {
                card_ids: vec![card.id.0],
                include_suspended_review: false,
                include_disabled_decks: false,
            })?;

        let expected_bucket = rwkv_japanese_feature_bucket("食べる", "", "", "").unwrap();
        assert_eq!(response.rows.len(), 1);
        assert_eq!(
            response.rows[0].preset_id,
            rwkv_preset_id_with_japanese_features("1", Some(&expected_bucket))
        );

        Ok(())
    }

    #[test]
    fn clean_preset_tags_exclude_outcome_tags() {
        assert_eq!(
            rwkv_clean_preset_tags(
                "Yomitan leech am-ready moeway-debut-idol-fail Claude-Translated wrong::answer"
            ),
            vec!["Claude-Translated".to_string(), "Yomitan".to_string()]
        );
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
