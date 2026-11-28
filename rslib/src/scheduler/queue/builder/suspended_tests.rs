// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use super::*;
use crate::card::CardType;
use crate::scheduler::queue::QueueEntryKind;
use crate::scheduler::states::CardState;
use crate::scheduler::states::NormalState;

#[test]
fn suspended_card_uses_one_new_slot_and_undo_restores_its_schedule() -> Result<()> {
    let mut col = Collection::new();
    let mut card = CardAdder::new().add(&mut col).pop().unwrap();
    card.queue = CardQueue::Suspended;
    card.ctype = CardType::Review;
    card.interval = 17;
    card.due = col.timing_today()?.days_elapsed as i32 + 12;
    card.reps = 8;
    col.storage.update_card(&card)?;
    assert!(col.get_next_card()?.is_none());
    assert_eq!(col.current_deck_tree()?.unwrap().new_count, 0);
    assert!(!col.congrats_info()?.new_remaining);

    let mut preset = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
    preset.inner.include_suspended_cards_in_learnable_pool = true;
    preset.inner.new_per_day = 1;
    col.transact(Op::UpdateDeckConfig, |col| {
        col.add_or_update_deck_config(&mut preset)
    })?;
    assert_eq!(col.current_deck_tree()?.unwrap().new_count, 1);
    assert!(col.congrats_info()?.new_remaining);
    let offered = col.get_next_card()?.unwrap();
    assert!(matches!(offered.kind, QueueEntryKind::New));
    assert!(
        matches!(offered.states.unwrap().current, CardState::Normal(NormalState::Review(s)) if s.scheduled_days == 17)
    );
    assert_eq!(offered.card.queue, CardQueue::Suspended);
    assert_eq!(
        col.storage.get_card(card.id)?.unwrap().queue,
        CardQueue::Suspended
    );
    assert_eq!(
        col.rebuild_queued_cards_preserving_current_card(card.id)?
            .new_count,
        1
    );

    col.answer_good();
    let answered = col.storage.get_card(card.id)?.unwrap();
    assert_eq!(answered.queue, CardQueue::Review);
    assert_eq!(answered.reps, 9);
    let day = col.timing_today()?.days_elapsed;
    assert_eq!(col.get_current_deck()?.new_rev_counts(day), (1, 0));
    assert_eq!(
        col.storage.get_revlog_entries_for_card(card.id)?[0].review_kind,
        crate::revlog::RevlogReviewKind::Review
    );
    col.undo()?;
    assert_eq!(col.get_current_deck()?.new_rev_counts(day), (0, 0));
    let restored = col.storage.get_card(card.id)?.unwrap();
    assert_eq!(
        (
            restored.queue,
            restored.interval,
            restored.due,
            restored.reps
        ),
        (CardQueue::Suspended, 17, card.due, 8)
    );
    assert_eq!(col.get_next_card()?.unwrap().card.id, card.id);
    Ok(())
}

#[test]
fn suspended_order_selects_daily_slots_and_uses_original_positions() -> Result<()> {
    let mut col = Collection::new();
    let deck = DeckId(1);
    // The studied cards' due values would put the unpositioned card first.
    let mut add = |nid, queue, ctype, due, original_position| -> Result<CardId> {
        let mut card = Card::new(NoteId(nid), 0, deck, due);
        card.queue = queue;
        card.ctype = ctype;
        card.original_position = original_position;
        col.add_card(&mut card)?;
        Ok(card.id)
    };
    let new1 = add(1, CardQueue::New, CardType::New, 5, None)?;
    let new2 = add(2, CardQueue::New, CardType::New, 6, None)?;
    let missing = add(3, CardQueue::Suspended, CardType::Review, 0, None)?;
    let positioned = add(4, CardQueue::Suspended, CardType::Review, 99, Some(2))?;
    let suspended_new = add(5, CardQueue::Suspended, CardType::New, 3, None)?;
    add(6, CardQueue::UserBuried, CardType::New, 1, None)?;
    let day = col.timing_today()?.days_elapsed;
    let mixed = if day % 2 == 0 {
        vec![new1, positioned, new2]
    } else {
        vec![positioned, new1, suspended_new]
    };
    let mut preset = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
    preset.inner.include_suspended_cards_in_learnable_pool = true;
    preset.inner.new_per_day = 3;
    preset.inner.new_card_sort_order = NewCardSortOrder::NoSort as i32;
    for (order, expected) in [
        (
            SuspendedCardOrder::SuspendedBeforeNew,
            vec![positioned, suspended_new, missing],
        ),
        (
            SuspendedCardOrder::NewBeforeSuspended,
            vec![new1, new2, positioned],
        ),
        (SuspendedCardOrder::Mixed, mixed),
    ] {
        preset.inner.suspended_card_order = order as i32;
        col.transact(Op::UpdateDeckConfig, |col| {
            col.add_or_update_deck_config(&mut preset)
        })?;
        let queued = col.get_queued_cards(10, false, true)?;
        assert_eq!(queued.new_count, 3);
        assert_eq!(
            queued.cards.iter().map(|c| c.card.id).collect::<Vec<_>>(),
            expected,
            "{order:?}"
        );
    }
    Ok(())
}

#[test]
fn suspended_siblings_stay_suspended_and_respect_burying_after_rebuild() -> Result<()> {
    let mut col = Collection::new();
    let mut preset = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
    preset.inner.include_suspended_cards_in_learnable_pool = true;
    preset.inner.bury_new = true;
    col.add_or_update_deck_config(&mut preset)?;
    let siblings = CardAdder::new().siblings(2).add(&mut col);
    for mut card in siblings {
        card.queue = CardQueue::Suspended;
        col.storage.update_card(&card)?;
    }
    let first = col.answer_easy().card_id;
    col.clear_study_queues();
    assert!(col.get_next_card()?.is_none());
    assert_eq!(col.current_deck_tree()?.unwrap().new_count, 0);
    assert!(!col.congrats_info()?.new_remaining);
    col.undo()?;
    assert_eq!(col.get_next_card()?.unwrap().card.id, first);
    assert_eq!(
        col.storage.get_card(first)?.unwrap().queue,
        CardQueue::Suspended
    );
    Ok(())
}

#[test]
fn subdeck_presets_control_inclusion_and_display_sort_preserves_mixed_slots() -> Result<()> {
    let mut col = Collection::new();
    let parent = col.get_or_create_normal_deck("Parent")?;
    let mut child = col.get_or_create_normal_deck("Parent::Included")?;
    let other = col.get_or_create_normal_deck("Parent::Excluded")?;
    let first = col.get_or_create_normal_deck("Parent::alpha")?;
    let mut included = DeckConfig {
        name: "Include suspended".into(),
        ..Default::default()
    };
    included.inner.include_suspended_cards_in_learnable_pool = true;
    col.add_or_update_deck_config(&mut included)?;
    child.normal_mut()?.config_id = included.id.0;
    col.add_or_update_deck(&mut child)?;
    for (deck, queue, ord) in [
        (other.id, CardQueue::Suspended, 0),
        (child.id, CardQueue::Suspended, 2),
        (child.id, CardQueue::Suspended, 1),
        (other.id, CardQueue::New, 0),
        (first.id, CardQueue::New, 0),
    ] {
        let mut card = Card::new(NoteId(30 + i64::from(ord)), ord, deck, 1);
        card.queue = queue;
        col.add_card(&mut card)?;
    }
    col.set_current_deck(parent.id)?;
    assert_eq!(col.current_deck_tree()?.unwrap().new_count, 4);
    let queued = col.get_queued_cards(4, false, true)?;
    let suspended: Vec<_> = queued
        .cards
        .iter()
        .map(|c| c.card.queue == CardQueue::Suspended)
        .collect();
    assert_eq!(suspended.len(), 4);
    assert!(suspended.windows(2).all(|w| w[0] != w[1]));
    let suspended_templates: Vec<_> = queued
        .cards
        .iter()
        .filter(|c| c.card.queue == CardQueue::Suspended)
        .map(|c| (c.card.deck_id, c.card.template_idx))
        .collect();
    assert_eq!(suspended_templates, [(child.id, 1), (child.id, 2)]);
    let new_decks: Vec<_> = queued
        .cards
        .iter()
        .filter(|c| c.card.queue == CardQueue::New)
        .map(|c| c.card.deck_id)
        .collect();
    assert_eq!(new_decks, [first.id, other.id]);
    Ok(())
}

#[test]
fn field_gather_ranks_candidates_before_the_limit_and_reacts_to_edits() -> Result<()> {
    use NewCardGatherPriority::NoteFieldAscending;
    use NewCardGatherPriority::NoteFieldDescending;
    use SuspendedCardOrder::Mixed;
    use SuspendedCardOrder::NewBeforeSuspended;
    use SuspendedCardOrder::SuspendedBeforeNew;

    let mut col = Collection::new();
    let mut add = |front, back, suspended| -> Result<Note> {
        let note = NoteAdder::basic(&mut col)
            .fields(&[front, back])
            .add(&mut col);
        if suspended {
            let mut card = col.storage.all_cards_of_note(note.id)?.pop().unwrap();
            card.queue = CardQueue::Suspended;
            col.storage.update_card(&card)?;
        }
        Ok(note)
    };
    let mut ten = add("1", "10", false)?;
    let negative = add("7", "-1.5", false)?.id;
    let two = add("3", "<b>2</b>", true)?.id;
    let hundred = add("2", "1e2", true)?.id;
    let blank = add("4", "  ", false)?.id;
    let beta = add("5", "Beta", false)?.id;
    let alpha = add("6", "apple", false)?.id;
    let missing = NoteAdder::cloze(&mut col)
        .fields(&["{{c1::missing}}", ""])
        .add(&mut col)
        .id;
    let mut preset = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
    preset.inner.include_suspended_cards_in_learnable_pool = true;
    preset.inner.new_card_sort_order = NewCardSortOrder::RandomCard as i32;
    preset.inner.new_card_gather_field = "Back".into();
    let queued_notes = |col: &mut Collection| -> Result<Vec<NoteId>> {
        Ok(col
            .get_queued_cards(20, false, true)?
            .cards
            .into_iter()
            .map(|card| card.card.note_id)
            .collect())
    };
    for (priority, order, limit, expected) in [
        (NoteFieldAscending, Mixed, 3, vec![negative, two, ten.id]),
        (NoteFieldDescending, Mixed, 3, vec![hundred, ten.id, two]),
        (
            NoteFieldAscending,
            SuspendedBeforeNew,
            3,
            vec![two, hundred, negative],
        ),
        (
            NoteFieldDescending,
            NewBeforeSuspended,
            3,
            vec![ten.id, negative, beta],
        ),
        (
            NoteFieldAscending,
            Mixed,
            8,
            vec![negative, two, ten.id, hundred, alpha, beta, blank, missing],
        ),
        (
            NoteFieldDescending,
            Mixed,
            8,
            vec![hundred, ten.id, two, negative, beta, alpha, blank, missing],
        ),
    ] {
        preset.inner.new_card_gather_priority = priority as i32;
        preset.inner.suspended_card_order = order as i32;
        preset.inner.new_per_day = limit;
        col.transact(Op::UpdateDeckConfig, |col| {
            col.add_or_update_deck_config(&mut preset)
        })?;
        assert_eq!(queued_notes(&mut col)?, expected, "{priority:?} {order:?}");
    }
    ten.fields_mut()[1] = "999".into();
    let changed = col.update_note(&mut ten)?.changes;
    assert!(changed.requires_study_queue_rebuild());
    assert_eq!(queued_notes(&mut col)?[0], ten.id);
    col.undo()?;
    assert_eq!(queued_notes(&mut col)?[0], hundred);

    // Each note type's configured sort field is the default. Field gathering
    // also works when ordinary new cards are the only eligible candidates.
    preset.inner.include_suspended_cards_in_learnable_pool = false;
    preset.inner.new_card_gather_priority = NoteFieldAscending as i32;
    preset.inner.new_card_gather_field.clear();
    preset.inner.new_per_day = 2;
    col.transact(Op::UpdateDeckConfig, |col| {
        col.add_or_update_deck_config(&mut preset)
    })?;
    assert_eq!(queued_notes(&mut col)?, [ten.id, blank]);
    Ok(())
}
