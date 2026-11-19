// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::BTreeSet;
use std::collections::HashMap;
use std::collections::HashSet;

use anki_proto::deck_config::preview_study_session_request::Source;
use anki_proto::deck_config::study_session_preview::Card as PreviewCard;
use anki_proto::deck_config::study_session_preview::Kind;
use anki_proto::deck_config::study_session_preview::Reason;
use anki_proto::deck_config::PreviewStudySessionRequest;
use anki_proto::deck_config::StudySessionPreview;
use anki_proto::deck_config::UpdateDeckConfigsMode;

use super::deck_is_within_scope;
use super::QueueBuilder;
use crate::card::CardQueue;
use crate::card::CardType;
use crate::cloze::strip_clozes;
use crate::collection::CollectionBuilder;
use crate::deckconfig::update_deck_limits;
use crate::deckconfig::UpdateDeckConfigsRequest;
use crate::notetype::NoteField;
use crate::prelude::*;
use crate::scheduler::queue::QueueEntryKind;
use crate::scheduler::timing::SchedTimingToday;
use crate::text::strip_html;

#[derive(Debug, Default)]
pub(super) struct PreviewTrace {
    pub(super) limit_card: Option<CardId>,
    pub(super) review_limit_card: Option<CardId>,
    pub(super) sibling: Option<CardId>,
    pub(super) minimum_reviews: HashSet<CardId>,
}

pub(super) fn apply_draft(
    draft: &UpdateDeckConfigsRequest,
    configs: &mut HashMap<DeckConfigId, DeckConfig>,
    root: &Deck,
    decks: &mut HashMap<DeckId, Deck>,
    today: u32,
) -> Result<()> {
    let selected = draft
        .configs
        .last()
        .or_invalid("missing selected preset")?
        .id;
    configs.extend(
        draft
            .configs
            .iter()
            .map(|config| (config.id, config.clone())),
    );
    for id in &draft.removed_config_ids {
        configs.remove(id);
    }
    for deck in decks.values_mut() {
        let Some(id) = deck.config_id() else { continue };
        let in_scope = deck_is_within_scope(deck, root);
        if deck.id == root.id
            || (in_scope && draft.mode == UpdateDeckConfigsMode::ApplyToChildren)
            || draft.removed_config_ids.contains(&id)
        {
            let normal = deck.normal_mut()?;
            normal.config_id = selected.0;
            update_deck_limits(normal, &draft.limits, today);
        }
        if deck.id == root.id
            || (in_scope && draft.limits.rwkv_apply_respect_subdeck_minimums_to_children)
        {
            if let Some(respect) = draft.limits.rwkv_respect_subdeck_minimums {
                deck.set_rwkv_respect_subdeck_minimums(respect)?;
            }
        }
    }
    Ok(())
}

impl Collection {
    pub(crate) fn study_session_preview(
        &mut self,
        request: PreviewStudySessionRequest,
    ) -> Result<StudySessionPreview> {
        let source = request.source();
        let mut draft: UpdateDeckConfigsRequest =
            request.options.or_invalid("missing deck options")?.into();
        if source != Source::Deck {
            let field = &draft
                .configs
                .last()
                .or_invalid("missing selected preset")?
                .inner
                .new_card_gather_field;
            let mut example = example_collection(source, field)?;
            draft.target_deck_id = DeckId(1);
            return example.preview_current_queue(&draft);
        }
        // The queue builder uses a temporary active_decks table. Restore its
        // previous contents even when previewing another deck or gathering fails.
        self.storage.begin_rust_trx()?;
        let result = self.preview_current_queue(&draft);
        self.storage.rollback_rust_trx()?;
        self.storage.commit_rust_trx()?;
        result
    }

    fn preview_current_queue(
        &mut self,
        draft: &UpdateDeckConfigsRequest,
    ) -> Result<StudySessionPreview> {
        let mut builder = QueueBuilder::with_options(self, draft.target_deck_id, Some(draft))?;
        self.storage
            .update_active_decks(&builder.context.root_deck)?;
        builder.gather_cards(self)?;
        let trace = builder.preview.take().unwrap_or_default();
        let timing = builder.context.timing;
        let field = builder.context.sort_options.new_gather_field.clone();
        let scores = builder.context.rwkv_review_queue_scores.clone();
        let ranks: HashMap<_, _> = builder
            .new
            .iter()
            .enumerate()
            .map(|(i, card)| (card.id, i as u32 + 1))
            .collect();
        let queues = builder.build(self.learn_ahead_secs() as i64);
        let entries: Vec<_> = queues.iter().collect();
        // Return a bounded sample, retaining actual positions for gaps.
        let mut indices: BTreeSet<_> = (0..entries.len().min(12)).collect();
        indices.extend(entries.len().checked_sub(1));
        for kind in [
            QueueEntryKind::New,
            QueueEntryKind::Review,
            QueueEntryKind::Learning,
        ] {
            indices.extend(entries.iter().position(|entry| entry.kind() == kind));
            indices.extend(entries.iter().rposition(|entry| entry.kind() == kind));
        }
        let mut cards = Vec::with_capacity(indices.len() + 1);
        for i in indices {
            let id = entries[i].card_id();
            let reason = if trace.minimum_reviews.contains(&id) {
                Reason::Minimum
            } else {
                Reason::Scheduled
            };
            let mut card = self.describe_preview_card(id, timing, &field, reason)?;
            card.position = i as u32 + 1;
            card.gather_position = ranks.get(&id).copied().unwrap_or_default();
            card.recall = scores
                .as_ref()
                .and_then(|scores| scores.get(&id))
                .map(|score| score.retrievability);
            cards.push(card);
        }
        let held = trace
            .limit_card
            .or(trace.review_limit_card)
            .map(|id| (id, Reason::DailyLimit))
            .or_else(|| trace.sibling.map(|id| (id, Reason::Sibling)));
        let held = match held {
            Some(card) => Some(card),
            None => {
                let included = entries.iter().map(|entry| entry.card_id()).collect();
                self.storage
                    .first_card_outside_preview(&included, timing.days_elapsed)?
                    .map(|(id, queue, due)| {
                        let reason = match queue {
                            CardQueue::Suspended => Reason::Suspended,
                            CardQueue::Learn if i64::from(due) > timing.now.0 => Reason::Later,
                            CardQueue::SchedBuried | CardQueue::UserBuried => Reason::Buried,
                            CardQueue::Review | CardQueue::DayLearn
                                if due > timing.days_elapsed as i32 =>
                            {
                                Reason::Later
                            }
                            _ => Reason::NotSelected,
                        };
                        (id, reason)
                    })
            }
        };
        if let Some((id, reason)) = held {
            cards.push(self.describe_preview_card(id, timing, &field, reason)?);
        }
        Ok(StudySessionPreview {
            total: entries.len() as u32,
            cards,
            has_rwkv_scores: scores.is_some(),
        })
    }

    fn describe_preview_card(
        &mut self,
        id: CardId,
        timing: SchedTimingToday,
        field: &str,
        reason: Reason,
    ) -> Result<PreviewCard> {
        let card = self.storage.get_card(id)?.or_not_found(id)?;
        let note = self
            .storage
            .get_note(card.note_id)?
            .or_not_found(card.note_id)?;
        let nt = self
            .get_notetype(note.notetype_id)?
            .or_not_found(note.notetype_id)?;
        let plain = |s: &str| {
            strip_html(s)
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .chars()
                .take(80)
                .collect::<String>()
        };
        let sort_idx = nt.config.sort_field_idx as usize;
        let label_text = |s: &String| plain(&strip_clozes(s));
        let label = note
            .fields()
            .get(sort_idx)
            .map(label_text)
            .filter(|s| !s.is_empty())
            .or_else(|| note.fields().iter().map(label_text).find(|s| !s.is_empty()))
            .unwrap_or_else(|| format!("#{}", id.0));
        let field_idx = if field.is_empty() {
            Some(sort_idx)
        } else {
            nt.get_field_ord(field)
        };
        let field_value = field_idx
            .and_then(|i| note.fields().get(i))
            .map(|s| plain(s))
            .unwrap_or_default();
        let (kind, due) = match card.queue {
            CardQueue::New => (Kind::New, card.due),
            CardQueue::Suspended => (
                Kind::Suspended,
                if card.ctype == CardType::New {
                    card.due
                } else {
                    card.original_position.map_or(0, |p| p as i32)
                },
            ),
            CardQueue::Learn | CardQueue::PreviewRepeat => (
                Kind::SameDay,
                (i64::from(card.due) - timing.now.0).div_euclid(60) as i32,
            ),
            CardQueue::DayLearn => (Kind::CarriedOver, card.due - timing.days_elapsed as i32),
            _ => (Kind::Review, card.due - timing.days_elapsed as i32),
        };
        Ok(PreviewCard {
            id: id.0,
            label,
            kind: kind as i32,
            reason: reason as i32,
            field_value,
            due,
            interval: card.interval,
            template_number: card.template_idx as u32 + 1,
            ..Default::default()
        })
    }
}

/// A private in-memory collection exercises exactly the same gather, sort and
/// limit code as the user's deck. Numbered cards make the daily cutoff visible.
fn example_collection(source: Source, field: &str) -> Result<Collection> {
    let mut col = CollectionBuilder::default().build()?;
    let mut nt = (*col
        .get_all_notetypes()?
        .into_iter()
        .find(|nt| !nt.is_cloze() && nt.templates.len() == 1 && nt.fields.len() == 2)
        .or_invalid("missing basic note type")?)
    .clone();
    if !field.is_empty() && nt.get_field_ord(field).is_none() {
        nt.fields.push(NoteField::new(field));
        col.update_notetype(&mut nt, false)?;
    }
    let timing = col.timing_today()?;
    let mut scores = HashMap::new();
    let new_count = if source == Source::Numbered { 120 } else { 3 };
    for index in 0..new_count + 6 {
        let label = if index < new_count {
            format!("N{:03}", index + 1)
        } else {
            ["R01", "R02", "S01", "L01", "L02", "R03"][index - new_count].to_string()
        };
        let mut note = nt.new_note();
        note.set_field(0, label)?;
        if let Some(i) = nt.get_field_ord(field).filter(|i| *i > 0) {
            note.set_field(i, ((index * 17) % (new_count + 6) + 1).to_string())?;
        }
        // Stable example IDs also keep the daily randomized orders repeatable.
        let id = CardId(1_700_000_000_000 + index as i64);
        note.id = NoteId(id.0);
        note.prepare_for_update(&nt, true)?;
        col.storage.add_note_if_unique(&note)?;
        let mut card = Card::new(note.id, 0, DeckId(1), index as i32 + 1);
        card.id = id;
        if index < new_count {
            col.storage.add_card_if_unique(&card)?;
            continue;
        }
        let today = timing.days_elapsed as i32;
        (card.queue, card.ctype, card.due) = match index - new_count {
            0 => (CardQueue::Review, CardType::Review, today - 2),
            1 => (CardQueue::Review, CardType::Review, today),
            2 => (CardQueue::Suspended, CardType::New, card.due),
            3 => (CardQueue::Learn, CardType::Learn, timing.now.0 as i32 - 60),
            4 => (CardQueue::DayLearn, CardType::Learn, today),
            _ => (CardQueue::Review, CardType::Review, today + 3),
        };
        if card.ctype == CardType::Review {
            card.interval = 7;
            card.reps = 3;
            let retrievability = if card.due > today {
                0.97
            } else if card.due < today {
                0.68
            } else {
                0.82
            };
            scores.insert(
                id,
                crate::collection::RwkvReviewQueueScoreEntry {
                    retrievability,
                    intervening_reviews: Some(99),
                    target_retention: None,
                },
            );
        }
        col.storage.add_card_if_unique(&card)?;
    }
    col.set_rwkv_review_queue_score_entries(DeckId(1), scores)?;
    Ok(col)
}

#[cfg(test)]
mod tests {
    use anki_proto::deck_config::UpdateDeckConfigsRequest as Options;

    use super::*;
    use crate::deckconfig::SuspendedCardOrder;

    #[test]
    fn an_empty_queue_explains_a_buried_card_or_a_future_learning_step() -> Result<()> {
        let mut col = Collection::new();
        let mut card = CardAdder::new().add(&mut col).remove(0);
        let preset = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
        let request = PreviewStudySessionRequest {
            options: Some(Options {
                target_deck_id: 1,
                configs: vec![preset.into()],
                ..Default::default()
            }),
            source: Source::Deck as i32,
        };
        card.ctype = CardType::Learn;
        card.due = col.timing_today()?.days_elapsed as i32 + 2;
        for (queue, reason) in [
            (CardQueue::UserBuried, Reason::Buried),
            (CardQueue::DayLearn, Reason::Later),
        ] {
            card.queue = queue;
            col.storage.update_card(&card)?;
            let preview = col.study_session_preview(request.clone())?;
            assert_eq!(preview.total, 0);
            assert_eq!(preview.cards.len(), 1);
            assert_eq!(
                (preview.cards[0].id, preview.cards[0].reason()),
                (card.id.0, reason)
            );
        }
        Ok(())
    }

    #[test]
    fn preview_uses_draft_limits_without_changing_the_active_deck_or_unsuspending_cards(
    ) -> Result<()> {
        let mut col = Collection::new();
        let active = CardAdder::new().add(&mut col)[0].id;
        let other = DeckAdder::new("Preview").add(&mut col);
        let mut suspended = CardAdder::new().deck(other.id).add(&mut col).remove(0);
        suspended.queue = CardQueue::Suspended;
        col.storage.update_card(&suspended)?;
        let next = CardAdder::new().deck(other.id).add(&mut col)[0].id;
        assert_eq!(col.get_next_card()?.unwrap().card.id, active);
        let active_decks = col.storage.get_active_deck_ids_sorted()?;
        let preset = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
        let mut edited = preset.clone();
        edited.inner.new_per_day = 1;
        edited.inner.include_suspended_cards_in_learnable_pool = true;
        edited.inner.suspended_card_order = SuspendedCardOrder::SuspendedBeforeNew as i32;
        let preview = col.study_session_preview(PreviewStudySessionRequest {
            options: Some(Options {
                target_deck_id: other.id.0,
                configs: vec![edited.into()],
                ..Default::default()
            }),
            source: Source::Deck as i32,
        })?;
        assert_eq!(preview.total, 1);
        assert_eq!(
            preview
                .cards
                .iter()
                .map(|c| (c.id, c.position, c.reason()))
                .collect::<Vec<_>>(),
            [
                (suspended.id.0, 1, Reason::Scheduled),
                (next.0, 0, Reason::DailyLimit)
            ]
        );
        assert_eq!(col.storage.get_card(suspended.id)?.unwrap(), suspended);
        assert_eq!(col.storage.get_active_deck_ids_sorted()?, active_decks);
        assert_eq!(
            col.get_deck_config(DeckConfigId(1), false)?.unwrap().inner,
            preset.inner
        );
        assert_eq!(col.get_next_card()?.unwrap().card.id, active);
        Ok(())
    }
}
