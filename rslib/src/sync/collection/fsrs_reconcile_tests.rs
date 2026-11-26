// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Sync-level tests for reconciling FSRS state after a download. Each test
//! syncs two collections through a local server, so the order of the merge,
//! reconcile and upload phases is exercised as in a real sync.

use std::collections::HashMap;

use anki_proto::scheduler::bury_or_suspend_cards_request::Mode as BuryOrSuspendMode;
use fsrs::DEFAULT_PARAMETERS;
use fsrs::FSRS6_DEFAULT_PARAMETERS;

use crate::card::Card;
use crate::card::CardQueue;
use crate::card::CardType;
use crate::card::FsrsMemoryState;
use crate::deckconfig::DeckConfig;
use crate::deckconfig::FsrsVersion;
use crate::decks::DeckKind;
use crate::decks::NativeDeckName;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::revlog::RevlogId;
use crate::revlog::RevlogReviewKind;
use crate::scheduler::fsrs::memory_state::get_decay_from_params;
use crate::scheduler::fsrs::memory_state::UpdateMemoryStateEntry;
use crate::scheduler::fsrs::memory_state::UpdateMemoryStateRequest;
use crate::scheduler::fsrs::params::ignore_revlogs_before_ms_from_config;
use crate::search::Node;
use crate::search::SearchNode;
use crate::search::SortMode;
use crate::storage::comma_separated_ids;
use crate::sync::collection::chunks::CHUNK_SIZE;
use crate::sync::collection::normal::SyncActionRequired;
use crate::sync::collection::tests::with_active_server;
use crate::sync::collection::tests::SyncTestContext;

const SECOND: std::time::Duration = std::time::Duration::from_millis(1100);

fn default_params(version: FsrsVersion) -> Vec<f32> {
    if version == FsrsVersion::Six {
        FSRS6_DEFAULT_PARAMETERS.into()
    } else {
        DEFAULT_PARAMETERS.into()
    }
}

fn set_params(config: &mut DeckConfig, version: FsrsVersion, params: Vec<f32>) {
    config.inner.fsrs_version = version as i32;
    if version == FsrsVersion::Six {
        config.inner.fsrs_params_6 = params;
    } else {
        config.inner.fsrs_params_7 = params;
    }
}

/// Turns on FSRS with the default preset on `version`.
fn enable_fsrs(col: &mut Collection, version: FsrsVersion) -> Result<()> {
    col.set_config_bool(BoolKey::Fsrs, true, false)?;
    let mut config = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
    set_params(&mut config, version, default_params(version));
    col.add_or_update_deck_config(&mut config)?;
    Ok(())
}

fn add_note(col: &mut Collection, front: &str, deck_id: DeckId) -> Result<CardId> {
    let nt = col.get_notetype_by_name("Basic")?.unwrap();
    let mut note = nt.new_note();
    note.set_field(0, front)?;
    col.add_note(&mut note, deck_id)?;
    Ok(col.search_cards(note.id, SortMode::NoOrder)?[0])
}

/// Recomputes memory states without rescheduling, as saving deck options
/// does.
fn recompute_memory_states(
    col: &mut Collection,
    config_id: DeckConfigId,
    card_ids: &[CardId],
    deck_desired_retention: HashMap<DeckId, f32>,
) -> Result<()> {
    let config = col.get_deck_config(config_id, false)?.unwrap();
    let request = UpdateMemoryStateRequest {
        params: config.fsrs_params().to_vec(),
        preset_desired_retention: config.inner.desired_retention,
        historical_retention: config.inner.historical_retention,
        max_interval: config.inner.maximum_review_interval,
        review_fuzz_config: col.review_fuzz_config(),
        reschedule: false,
        deck_desired_retention,
    };
    let ignore_before = ignore_revlogs_before_ms_from_config(&config)?;
    col.transact(Op::UpdateDeckConfig, |col| {
        col.update_memory_state(vec![UpdateMemoryStateEntry {
            req: Some(request),
            search: Node::Search(SearchNode::CardIds(comma_separated_ids(card_ids))),
            ignore_before,
            preset_name: config.name.clone(),
            current_preset: 1,
            total_presets: 1,
        }])
    })?;
    Ok(())
}

/// Stale FSRS-only edit: shifts the stability and replaces the stored
/// desired retention and decay.
fn make_fsrs_state_stale(
    col: &mut Collection,
    card_id: CardId,
    stability_delta: f32,
    desired_retention: f32,
    decay: f32,
) -> Result<Card> {
    col.get_and_update_card(card_id, |card| {
        let state = card.memory_state.unwrap();
        card.memory_state = Some(FsrsMemoryState {
            stability: state.stability + stability_delta,
            ..state
        });
        card.desired_retention = Some(desired_retention);
        card.decay = Some(decay);
        Ok(())
    })
}

async fn sync_fsrs_collections(ctx: &SyncTestContext, mut col1: Collection) -> Result<()> {
    let out = ctx.normal_sync(&mut col1).await;
    assert!(matches!(
        out.required,
        SyncActionRequired::FullSyncRequired { .. }
    ));
    ctx.full_upload(col1).await;

    let mut col2 = ctx.col2();
    let out = ctx.normal_sync(&mut col2).await;
    assert_eq!(
        out.required,
        SyncActionRequired::FullSyncRequired {
            upload_ok: false,
            download_ok: true,
        }
    );
    ctx.full_download(col2).await;

    Ok(())
}

/// Syncs a collection holding one card answered Easy to a second device, and
/// returns both collections plus the card id. `None` keeps FSRS off.
async fn two_devices_with_easy_card(
    ctx: &SyncTestContext,
    version: Option<FsrsVersion>,
) -> Result<(Collection, Collection, CardId)> {
    two_devices_with_easy_card_and_config(ctx, version, |_| {}).await
}

async fn two_devices_with_easy_card_and_config(
    ctx: &SyncTestContext,
    version: Option<FsrsVersion>,
    update_config: impl FnOnce(&mut DeckConfig),
) -> Result<(Collection, Collection, CardId)> {
    let mut col1 = ctx.col1();
    if let Some(version) = version {
        enable_fsrs(&mut col1, version)?;
    }
    let mut config = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
    update_config(&mut config);
    col1.add_or_update_deck_config(&mut config)?;
    add_note(&mut col1, "easy-card", DeckId(1))?;
    let card_id = col1.answer_easy().card_id;
    sync_fsrs_collections(ctx, col1).await?;
    Ok((ctx.col1(), ctx.col2(), card_id))
}

/// Makes the review card due today so it can be answered again.
fn make_card_due(col: &mut Collection, card_id: CardId) -> Result<()> {
    col.storage
        .db
        .execute("update cards set due = 0 where id = ?", [card_id])?;
    col.clear_study_queues();
    Ok(())
}

fn card(col: &Collection, card_id: CardId) -> Result<Card> {
    Ok(col.storage.get_card(card_id)?.unwrap())
}

fn pending_cards(col: &Collection) -> Result<Vec<CardId>> {
    col.storage.objects_pending_sync("cards", Usn(-1))
}

async fn sync_ok(ctx: &SyncTestContext, col: &mut Collection) {
    let out = ctx.normal_sync(col).await;
    assert_eq!(out.required, SyncActionRequired::NoChanges);
}

/// Compares the card row fields that sync and FSRS reconciliation touch.
fn assert_card_rows_converged(a: &Card, b: &Card) {
    assert_eq!(a.deck_id, b.deck_id);
    assert_eq!(a.ctype, b.ctype);
    assert_eq!(a.queue, b.queue);
    assert_eq!(a.due, b.due);
    assert_eq!(a.interval, b.interval);
    assert_eq!(a.original_due, b.original_due);
    assert_eq!(a.memory_state, b.memory_state);
    assert_eq!(a.desired_retention, b.desired_retention);
    assert_eq!(a.decay, b.decay);
    assert_eq!(a.last_review_time, b.last_review_time);
}

/// The stored memory state must match one recomputed from the card's revlog
/// with its resolved preset. `compute_memory_state` runs one card at a time
/// while reconciliation runs a batch, and storage rounds the stored values,
/// so allow for that.
fn assert_memory_state_matches_merged_log(col: &mut Collection, card_id: CardId) -> Result<()> {
    let stored = card(col, card_id)?.memory_state.unwrap();
    let expected: FsrsMemoryState = col.compute_memory_state(card_id)?.state.unwrap().into();
    assert!(
        (stored.stability - expected.stability).abs() < 1e-3,
        "stability {} vs {}",
        stored.stability,
        expected.stability
    );
    assert!(
        (stored.difficulty - expected.difficulty).abs() < 1e-3,
        "difficulty {} vs {}",
        stored.difficulty,
        expected.difficulty
    );
    Ok(())
}

/// The reconciled state is the merged log replayed. FSRS-6 counts whole
/// days, so the replay also equals the state the answering device stored.
/// FSRS-7 replays fractional days from revlog milliseconds, while the answer
/// path counts from the card's `last_review_time` in seconds, so the two can
/// differ in the last stored digits; the next sync then converges both
/// devices on the replay.
fn assert_reconciled_memory_state(
    col: &mut Collection,
    card_id: CardId,
    answered: &Card,
    version: FsrsVersion,
) -> Result<()> {
    assert_memory_state_matches_merged_log(col, card_id)?;
    if version == FsrsVersion::Six {
        assert_eq!(card(col, card_id)?.memory_state, answered.memory_state);
    }
    Ok(())
}

// One device reviews, the other recomputes stale state
///////////////////////////////////////////////////////

async fn stale_card_state_is_reconciled_during_sync(version: FsrsVersion) -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) = two_devices_with_easy_card(&ctx, Some(version)).await?;

        std::thread::sleep(SECOND);
        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        std::thread::sleep(std::time::Duration::from_millis(1));
        recompute_memory_states(&mut col2, DeckConfigId(1), &[card_id], HashMap::new())?;

        let stale_card = card(&col2, card_id)?;
        let reviewed_card = card(&col1, card_id)?;
        assert!(
            stale_card.memory_state != reviewed_card.memory_state
                || stale_card.last_review_time != reviewed_card.last_review_time
                || stale_card.interval != reviewed_card.interval
                || stale_card.due != reviewed_card.due
        );

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let reconciled_card = card(&col2, card_id)?;
        assert_reconciled_memory_state(&mut col2, card_id, &reviewed_card, version)?;
        assert_eq!(
            reconciled_card.last_review_time,
            reviewed_card.last_review_time
        );
        assert_eq!(reconciled_card.interval, reviewed_card.interval);
        assert_eq!(reconciled_card.due, reviewed_card.due);

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &reconciled_card);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs6_stale_card_state_is_reconciled_during_sync() -> Result<()> {
    stale_card_state_is_reconciled_during_sync(FsrsVersion::Six).await
}

#[tokio::test]
async fn fsrs7_stale_card_state_is_reconciled_during_sync() -> Result<()> {
    stale_card_state_is_reconciled_during_sync(FsrsVersion::Seven).await
}

// FSRS metadata differs, schedule does not
///////////////////////////////////////////

async fn metadata_conflict_is_reconciled_without_rescheduling(version: FsrsVersion) -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) = two_devices_with_easy_card(&ctx, Some(version)).await?;

        // Recompute FSRS metadata on col1 with a different desired retention,
        // but without rescheduling.
        let mut deck = (*col1.get_deck(DeckId(1))?.unwrap()).clone();
        deck.normal_mut().unwrap().desired_retention = Some(0.83);
        col1.add_or_update_deck(&mut deck)?;
        recompute_memory_states(
            &mut col1,
            DeckConfigId(1),
            &[card_id],
            HashMap::from([(DeckId(1), 0.83)]),
        )?;

        // The other device holds stale FSRS data while its schedule still
        // matches the review history.
        let stale_card = make_fsrs_state_stale(&mut col2, card_id, 1.0, 0.97, 0.12)?;
        let updated_card = card(&col1, card_id)?;
        assert_eq!(stale_card.interval, updated_card.interval);
        assert_eq!(stale_card.due, updated_card.due);
        assert_ne!(stale_card.memory_state, updated_card.memory_state);
        assert_ne!(stale_card.desired_retention, updated_card.desired_retention);

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let reconciled_card = card(&col2, card_id)?;
        let synced_card = card(&col1, card_id)?;
        assert_eq!(reconciled_card.memory_state, synced_card.memory_state);
        assert_eq!(reconciled_card.desired_retention, Some(0.83));
        assert_eq!(reconciled_card.decay, synced_card.decay);
        assert_eq!(
            reconciled_card.last_review_time,
            synced_card.last_review_time
        );
        // Only FSRS metadata diverged, so the schedule is kept.
        assert_eq!(reconciled_card.interval, stale_card.interval);
        assert_eq!(reconciled_card.due, stale_card.due);

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &reconciled_card);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs6_metadata_conflict_is_reconciled_without_rescheduling() -> Result<()> {
    metadata_conflict_is_reconciled_without_rescheduling(FsrsVersion::Six).await
}

#[tokio::test]
async fn fsrs7_metadata_conflict_is_reconciled_without_rescheduling() -> Result<()> {
    metadata_conflict_is_reconciled_without_rescheduling(FsrsVersion::Seven).await
}

#[tokio::test]
async fn fsrs_itemless_card_state_is_cleared_during_sync() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        enable_fsrs(&mut col1, FsrsVersion::Seven)?;
        let card_id = add_note(&mut col1, "fsrs-itemless", DeckId(1))?;
        sync_fsrs_collections(&ctx, col1).await?;
        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // A manual-only revlog puts the card in the merged history without
        // producing an FSRS item.
        col1.get_and_update_card(card_id, |card| {
            card.due += 7;
            Ok(())
        })?;
        col1.storage.add_revlog_entry(
            &RevlogEntry {
                id: RevlogId::new(),
                cid: card_id,
                usn: col1.usn()?,
                button_chosen: 0,
                interval: 0,
                last_interval: 0,
                ease_factor: 2500,
                taken_millis: 0,
                review_kind: RevlogReviewKind::Manual,
            },
            true,
        )?;
        col2.get_and_update_card(card_id, |card| {
            card.memory_state = Some(FsrsMemoryState {
                stability: 7.0,
                stability_internal: 7.0,
                difficulty: 4.0,
                stability_fast: None,
            });
            card.desired_retention = Some(0.72);
            card.decay = Some(0.34);
            card.last_review_time = Some(TimestampSecs(123));
            Ok(())
        })?;
        let stale_card = card(&col2, card_id)?;

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let reconciled_card = card(&col2, card_id)?;
        let config = col2.get_deck_config(DeckConfigId(1), false)?.unwrap();
        assert_eq!(reconciled_card.memory_state, None);
        assert_eq!(reconciled_card.last_review_time, None);
        assert_eq!(
            reconciled_card.desired_retention,
            Some(config.inner.desired_retention)
        );
        assert!(
            (reconciled_card.decay.unwrap() - get_decay_from_params(config.fsrs_params())).abs()
                < 0.001
        );
        // Itemless reconciliation does not reschedule.
        assert_eq!(reconciled_card.due, stale_card.due);
        assert_eq!(reconciled_card.interval, stale_card.interval);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_conflicts_are_reconciled_per_preset() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        let mut config1 = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
        config1.inner.desired_retention = 0.81;
        set_params(
            &mut config1,
            FsrsVersion::Six,
            default_params(FsrsVersion::Six),
        );
        col1.add_or_update_deck_config(&mut config1)?;
        let mut config2 = DeckConfig {
            name: "fsrs second config".into(),
            ..Default::default()
        };
        config2.inner.desired_retention = 0.93;
        let mut params2 = default_params(FsrsVersion::Six);
        params2[20] = 0.21;
        set_params(&mut config2, FsrsVersion::Six, params2);
        col1.add_or_update_deck_config(&mut config2)?;
        let mut deck2 = col1.get_or_create_normal_deck("fsrs second deck")?;
        if let DeckKind::Normal(deck) = &mut deck2.kind {
            deck.config_id = config2.id.0;
        }
        col1.add_or_update_deck(&mut deck2)?;

        add_note(&mut col1, "fsrs-preset-1", DeckId(1))?;
        col1.set_current_deck(DeckId(1))?;
        let card1 = col1.answer_easy().card_id;
        add_note(&mut col1, "fsrs-preset-2", deck2.id)?;
        col1.set_current_deck(deck2.id)?;
        let card2 = col1.answer_easy().card_id;
        sync_fsrs_collections(&ctx, col1).await?;
        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        recompute_memory_states(&mut col1, DeckConfigId(1), &[card1], HashMap::new())?;
        recompute_memory_states(&mut col1, config2.id, &[card2], HashMap::new())?;
        sync_ok(&ctx, &mut col1).await;

        // Newer stale edits on col2 keep the local rows, which must then be
        // repaired with each card's own preset.
        make_fsrs_state_stale(&mut col2, card1, 1.0, 0.99, 0.11)?;
        make_fsrs_state_stale(&mut col2, card2, 2.0, 0.77, 0.31)?;
        sync_ok(&ctx, &mut col2).await;

        let config1 = col2.get_deck_config(DeckConfigId(1), false)?.unwrap();
        let config2 = col2.get_deck_config(config2.id, false)?.unwrap();
        let reconciled_card1 = card(&col2, card1)?;
        let reconciled_card2 = card(&col2, card2)?;
        assert_eq!(
            reconciled_card1.memory_state,
            card(&col1, card1)?.memory_state
        );
        assert_eq!(
            reconciled_card2.memory_state,
            card(&col1, card2)?.memory_state
        );
        assert_eq!(reconciled_card1.desired_retention, Some(0.81));
        assert_eq!(reconciled_card2.desired_retention, Some(0.93));
        assert!(
            (reconciled_card1.decay.unwrap() - get_decay_from_params(config1.fsrs_params())).abs()
                < 0.001
        );
        assert!(
            (reconciled_card2.decay.unwrap() - get_decay_from_params(config2.fsrs_params())).abs()
                < 0.001
        );
        assert_ne!(reconciled_card1.decay, reconciled_card2.decay);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_reconciliation_uses_original_deck_for_filtered_cards() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        enable_fsrs(&mut col1, FsrsVersion::Seven)?;
        let mut home_deck = col1.get_or_create_normal_deck("fsrs home deck")?;
        home_deck.normal_mut().unwrap().desired_retention = Some(0.84);
        col1.add_or_update_deck(&mut home_deck)?;
        add_note(&mut col1, "fsrs-filtered-home", home_deck.id)?;
        col1.set_current_deck(home_deck.id)?;
        let card_id = col1.answer_easy().card_id;
        sync_fsrs_collections(&ctx, col1).await?;
        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        recompute_memory_states(
            &mut col1,
            DeckConfigId(1),
            &[card_id],
            HashMap::from([(home_deck.id, 0.84)]),
        )?;
        sync_ok(&ctx, &mut col1).await;

        let mut filtered_deck = Deck::new_filtered();
        filtered_deck.name = NativeDeckName::from_native_str("fsrs filtered");
        {
            let filtered = filtered_deck.filtered_mut()?;
            filtered.reschedule = false;
            filtered.search_terms[0].search = format!("cid:{}", card_id.0);
        }
        col2.add_or_update_deck(&mut filtered_deck)?;
        assert_eq!(col2.rebuild_filtered_deck(filtered_deck.id)?.output, 1);
        let stale_card = make_fsrs_state_stale(&mut col2, card_id, 1.5, 0.99, 0.12)?;
        assert_eq!(stale_card.deck_id, filtered_deck.id);
        assert_eq!(stale_card.original_deck_id, home_deck.id);

        sync_ok(&ctx, &mut col2).await;

        let reconciled_card = card(&col2, card_id)?;
        let synced_card = card(&col1, card_id)?;
        assert_eq!(reconciled_card.deck_id, filtered_deck.id);
        assert_eq!(reconciled_card.original_deck_id, home_deck.id);
        assert_eq!(reconciled_card.memory_state, synced_card.memory_state);
        assert_eq!(reconciled_card.desired_retention, Some(0.84));
        assert_eq!(
            reconciled_card.last_review_time,
            synced_card.last_review_time
        );

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_reconciliation_respects_deck_overrides_within_one_preset() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        enable_fsrs(&mut col1, FsrsVersion::Seven)?;
        let mut shared_config = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
        shared_config.inner.desired_retention = 0.89;
        col1.add_or_update_deck_config(&mut shared_config)?;
        let mut deck1 = col1.get_or_create_normal_deck("fsrs override deck 1")?;
        deck1.normal_mut().unwrap().desired_retention = Some(0.82);
        col1.add_or_update_deck(&mut deck1)?;
        let mut deck2 = col1.get_or_create_normal_deck("fsrs override deck 2")?;
        deck2.normal_mut().unwrap().desired_retention = Some(0.95);
        col1.add_or_update_deck(&mut deck2)?;

        add_note(&mut col1, "fsrs-override-1", deck1.id)?;
        col1.set_current_deck(deck1.id)?;
        let card1 = col1.answer_easy().card_id;
        add_note(&mut col1, "fsrs-override-2", deck2.id)?;
        col1.set_current_deck(deck2.id)?;
        let card2 = col1.answer_easy().card_id;
        sync_fsrs_collections(&ctx, col1).await?;
        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        recompute_memory_states(
            &mut col1,
            DeckConfigId(1),
            &[card1, card2],
            HashMap::from([(deck1.id, 0.82), (deck2.id, 0.95)]),
        )?;
        sync_ok(&ctx, &mut col1).await;

        make_fsrs_state_stale(&mut col2, card1, 1.0, 0.99, 0.11)?;
        make_fsrs_state_stale(&mut col2, card2, 1.5, 0.77, 0.31)?;
        sync_ok(&ctx, &mut col2).await;

        let config = col2.get_deck_config(DeckConfigId(1), false)?.unwrap();
        let reconciled_card1 = card(&col2, card1)?;
        let reconciled_card2 = card(&col2, card2)?;
        assert_eq!(
            reconciled_card1.memory_state,
            card(&col1, card1)?.memory_state
        );
        assert_eq!(
            reconciled_card2.memory_state,
            card(&col1, card2)?.memory_state
        );
        assert_eq!(reconciled_card1.desired_retention, Some(0.82));
        assert_eq!(reconciled_card2.desired_retention, Some(0.95));
        for reconciled in [reconciled_card1, reconciled_card2] {
            assert!(
                (reconciled.decay.unwrap() - get_decay_from_params(config.fsrs_params())).abs()
                    < 0.001
            );
        }

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_mixed_schedule_and_metadata_conflicts_reconcile_selectively() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        enable_fsrs(&mut col1, FsrsVersion::Six)?;
        let mut metadata_deck = col1.get_or_create_normal_deck("fsrs metadata deck")?;
        metadata_deck.normal_mut().unwrap().desired_retention = Some(0.83);
        col1.add_or_update_deck(&mut metadata_deck)?;
        add_note(&mut col1, "fsrs-mixed-schedule", DeckId(1))?;
        col1.set_current_deck(DeckId(1))?;
        let card1 = col1.answer_easy().card_id;
        add_note(&mut col1, "fsrs-mixed-metadata", metadata_deck.id)?;
        col1.set_current_deck(metadata_deck.id)?;
        let card2 = col1.answer_easy().card_id;
        sync_fsrs_collections(&ctx, col1).await?;
        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // Card 1 gets a new review on col1; card 2 only a metadata refresh.
        std::thread::sleep(SECOND);
        make_card_due(&mut col1, card1)?;
        col1.set_current_deck(DeckId(1))?;
        col1.answer_good();
        recompute_memory_states(
            &mut col1,
            DeckConfigId(1),
            &[card2],
            HashMap::from([(metadata_deck.id, 0.83)]),
        )?;

        let stale_card1 = col2.get_and_update_card(card1, |card| {
            let state = card.memory_state.unwrap();
            card.memory_state = Some(FsrsMemoryState {
                stability: state.stability + 1.0,
                ..state
            });
            // Far enough from the answered schedule that fuzz cannot make
            // them coincide.
            card.interval += 10;
            card.due += 10;
            card.desired_retention = Some(0.99);
            card.decay = Some(0.11);
            Ok(())
        })?;
        let stale_card2 = make_fsrs_state_stale(&mut col2, card2, 1.5, 0.97, 0.12)?;
        let reviewed_card = card(&col1, card1)?;
        let refreshed_card = card(&col1, card2)?;
        assert!(
            stale_card1.interval != reviewed_card.interval || stale_card1.due != reviewed_card.due
        );
        assert_eq!(stale_card2.interval, refreshed_card.interval);
        assert_eq!(stale_card2.due, refreshed_card.due);

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let reconciled_card1 = card(&col2, card1)?;
        let reconciled_card2 = card(&col2, card2)?;
        let synced_card1 = card(&col1, card1)?;
        let synced_card2 = card(&col1, card2)?;
        assert_eq!(reconciled_card1.memory_state, synced_card1.memory_state);
        assert_eq!(
            reconciled_card1.last_review_time,
            synced_card1.last_review_time
        );
        assert_eq!(reconciled_card1.interval, synced_card1.interval);
        assert_eq!(reconciled_card1.due, synced_card1.due);
        assert_eq!(reconciled_card2.memory_state, synced_card2.memory_state);
        assert_eq!(
            reconciled_card2.desired_retention,
            synced_card2.desired_retention
        );
        assert_eq!(
            reconciled_card2.last_review_time,
            synced_card2.last_review_time
        );
        // Card 2 got no new review, so its schedule is kept although it was
        // processed in the same preset batch as card 1.
        assert_eq!(reconciled_card2.interval, stale_card2.interval);
        assert_eq!(reconciled_card2.due, stale_card2.due);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_filtered_card_schedule_conflict_uses_original_deck() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        enable_fsrs(&mut col1, FsrsVersion::Six)?;
        let mut home_deck = col1.get_or_create_normal_deck("fsrs filtered schedule home")?;
        home_deck.normal_mut().unwrap().desired_retention = Some(0.84);
        col1.add_or_update_deck(&mut home_deck)?;
        add_note(&mut col1, "fsrs-filtered-schedule", home_deck.id)?;
        col1.set_current_deck(home_deck.id)?;
        let card_id = col1.answer_easy().card_id;
        sync_fsrs_collections(&ctx, col1).await?;
        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        std::thread::sleep(SECOND);
        make_card_due(&mut col1, card_id)?;
        col1.set_current_deck(home_deck.id)?;
        col1.answer_good();

        let mut filtered_deck = Deck::new_filtered();
        filtered_deck.name = NativeDeckName::from_native_str("fsrs filtered schedule");
        {
            let filtered = filtered_deck.filtered_mut()?;
            filtered.reschedule = false;
            filtered.search_terms[0].search = format!("cid:{}", card_id.0);
        }
        col2.add_or_update_deck(&mut filtered_deck)?;
        assert_eq!(col2.rebuild_filtered_deck(filtered_deck.id)?.output, 1);
        let stale_card = col2.get_and_update_card(card_id, |card| {
            let state = card.memory_state.unwrap();
            card.memory_state = Some(FsrsMemoryState {
                stability: state.stability + 1.5,
                ..state
            });
            card.interval += 10;
            card.original_due += 10;
            card.desired_retention = Some(0.99);
            card.decay = Some(0.12);
            Ok(())
        })?;
        assert_eq!(stale_card.deck_id, filtered_deck.id);
        assert_eq!(stale_card.original_deck_id, home_deck.id);
        let reviewed_card = card(&col1, card_id)?;
        assert!(
            stale_card.interval != reviewed_card.interval
                || stale_card.original_due != reviewed_card.due
        );

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let reconciled_card = card(&col2, card_id)?;
        let synced_card = card(&col1, card_id)?;
        assert_eq!(reconciled_card.deck_id, filtered_deck.id);
        assert_eq!(reconciled_card.original_deck_id, home_deck.id);
        assert_eq!(reconciled_card.memory_state, synced_card.memory_state);
        assert_eq!(reconciled_card.desired_retention, Some(0.84));
        assert_eq!(reconciled_card.interval, synced_card.interval);
        assert_eq!(reconciled_card.original_due, synced_card.due);
        // The filtered deck position stays; only the home-deck schedule moves.
        assert_eq!(reconciled_card.due, stale_card.due);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_state_is_recomputed_from_reviews_on_both_devices() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) =
            two_devices_with_easy_card(&ctx, Some(FsrsVersion::Seven)).await?;

        std::thread::sleep(SECOND);
        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        std::thread::sleep(SECOND);
        make_card_due(&mut col2, card_id)?;
        col2.answer_easy();

        let local_card1 = card(&col1, card_id)?;
        let local_card2 = card(&col2, card_id)?;
        assert!(
            local_card1.memory_state != local_card2.memory_state
                || local_card1.last_review_time != local_card2.last_review_time
                || local_card1.interval != local_card2.interval
                || local_card1.due != local_card2.due
        );

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let merged_card = card(&col2, card_id)?;
        let pre_converged_card = card(&col1, card_id)?;
        assert_eq!(col2.storage.get_revlog_entries_for_card(card_id)?.len(), 3);
        assert_eq!(col1.storage.get_revlog_entries_for_card(card_id)?.len(), 2);
        assert!(
            merged_card.memory_state != pre_converged_card.memory_state
                || merged_card.last_review_time != pre_converged_card.last_review_time
                || merged_card.interval != pre_converged_card.interval
                || merged_card.due != pre_converged_card.due
        );
        assert_memory_state_matches_merged_log(&mut col2, card_id)?;

        sync_ok(&ctx, &mut col1).await;
        assert_eq!(col1.storage.get_revlog_entries_for_card(card_id)?.len(), 3);
        assert_card_rows_converged(&card(&col1, card_id)?, &card(&col2, card_id)?);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_reconciles_when_server_copy_is_newer() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) =
            two_devices_with_easy_card(&ctx, Some(FsrsVersion::Six)).await?;

        make_fsrs_state_stale(&mut col2, card_id, 1.0, 0.97, 0.12)?;
        // col1 reviews the card later, so its copy is the newer one.
        std::thread::sleep(SECOND);
        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        let col1_card = card(&col1, card_id)?;
        let col2_stale_card = card(&col2, card_id)?;
        assert!(col1_card.mtime > col2_stale_card.mtime);
        assert_ne!(col1_card.memory_state, col2_stale_card.memory_state);

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let col2_card = card(&col2, card_id)?;
        assert_card_rows_converged(&col2_card, &col1_card);

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &col2_card);

        Ok(())
    })
    .await
}

/// Counterpart of the chunk-level test that keeps the winning row when there
/// is no history: with merged history, the remote winner is rebuilt and the
/// result uploaded.
#[tokio::test]
async fn fsrs6_remote_winner_is_reconciled_from_merged_history_and_uploaded() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) =
            two_devices_with_easy_card(&ctx, Some(FsrsVersion::Six)).await?;

        // col2 reviews first; col1 reviews later without seeing that review,
        // so col1's newer row wins but misses part of the history.
        std::thread::sleep(SECOND);
        make_card_due(&mut col2, card_id)?;
        col2.answer_hard();
        std::thread::sleep(SECOND);
        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        let remote_winner = card(&col1, card_id)?;
        assert!(remote_winner.mtime > card(&col2, card_id)?.mtime);

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let reconciled = card(&col2, card_id)?;
        assert_ne!(reconciled.memory_state, remote_winner.memory_state);
        assert_memory_state_matches_merged_log(&mut col2, card_id)?;
        // The newest review owns the schedule.
        assert_eq!(reconciled.interval, remote_winner.interval);
        assert_eq!(reconciled.due, remote_winner.due);
        assert!(pending_cards(&col2)?.is_empty());

        // The reconciled row reached the server.
        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &reconciled);

        Ok(())
    })
    .await
}

// Deck moves
//////////////

async fn deck_move_is_not_rescheduled(version: FsrsVersion) -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        enable_fsrs(&mut col1, version)?;
        let mut config = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
        config.inner.desired_retention = 0.9;
        col1.add_or_update_deck_config(&mut config)?;
        let deck_a = col1.get_or_create_normal_deck("fsrs deck a")?;
        let mut deck_b = col1.get_or_create_normal_deck("fsrs deck b")?;
        deck_b.normal_mut().unwrap().desired_retention = Some(0.70);
        col1.add_or_update_deck(&mut deck_b)?;
        add_note(&mut col1, "fsrs-deck-move", deck_a.id)?;
        col1.set_current_deck(deck_a.id)?;
        let card_id = col1.answer_easy().card_id;
        sync_fsrs_collections(&ctx, col1).await?;
        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // The server holds a newer row only because of a flag change.
        col1.set_card_flag(&[card_id], 1)?;
        // The deck move on col2 is the newest local change.
        std::thread::sleep(SECOND);
        col2.set_deck(&[card_id], deck_b.id)?;
        let moved_card = card(&col2, card_id)?;
        assert_eq!(moved_card.deck_id, deck_b.id);
        assert!(moved_card.mtime > card(&col1, card_id)?.mtime);

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let synced_card = card(&col2, card_id)?;
        assert_eq!(synced_card.deck_id, deck_b.id);
        assert_eq!(synced_card.interval, moved_card.interval);
        assert_eq!(synced_card.due, moved_card.due);
        assert_eq!(synced_card.desired_retention, Some(0.70));

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &synced_card);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs6_deck_move_is_not_rescheduled() -> Result<()> {
    deck_move_is_not_rescheduled(FsrsVersion::Six).await
}

#[tokio::test]
async fn fsrs7_deck_move_is_not_rescheduled() -> Result<()> {
    deck_move_is_not_rescheduled(FsrsVersion::Seven).await
}

async fn deck_move_with_new_review_takes_reviewed_schedule(version: FsrsVersion) -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) = two_devices_with_easy_card(&ctx, Some(version)).await?;
        let mut deck_b = col2.get_or_create_normal_deck("fsrs review deck b")?;
        deck_b.normal_mut().unwrap().desired_retention = Some(0.70);
        col2.add_or_update_deck(&mut deck_b)?;

        make_card_due(&mut col1, card_id)?;
        // On FSRS-7 an immediate Good keeps the Easy interval, so use Hard to
        // make the reviewed schedule differ.
        if version == FsrsVersion::Six {
            col1.answer_good();
        } else {
            col1.answer_hard();
        }
        // The deck move on col2 is newer than the review on col1.
        std::thread::sleep(SECOND);
        col2.set_deck(&[card_id], deck_b.id)?;
        let reviewed_card = card(&col1, card_id)?;
        let moved_card = card(&col2, card_id)?;
        assert!(moved_card.mtime > reviewed_card.mtime);
        assert_ne!(moved_card.due, reviewed_card.due);

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let synced_card = card(&col2, card_id)?;
        assert_eq!(synced_card.deck_id, deck_b.id);
        assert_eq!(synced_card.desired_retention, Some(0.70));
        assert_eq!(synced_card.interval, reviewed_card.interval);
        assert_eq!(synced_card.due, reviewed_card.due);
        assert_eq!(synced_card.last_review_time, reviewed_card.last_review_time);
        assert_memory_state_matches_merged_log(&mut col2, card_id)?;

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &synced_card);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs6_deck_move_with_new_review_takes_reviewed_schedule() -> Result<()> {
    deck_move_with_new_review_takes_reviewed_schedule(FsrsVersion::Six).await
}

#[tokio::test]
async fn fsrs7_deck_move_with_new_review_takes_reviewed_schedule() -> Result<()> {
    deck_move_with_new_review_takes_reviewed_schedule(FsrsVersion::Seven).await
}

async fn stale_winner_without_the_local_review_takes_its_schedule(
    version: FsrsVersion,
) -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) = two_devices_with_easy_card(&ctx, Some(version)).await?;

        // last_review_time has one-second resolution.
        std::thread::sleep(SECOND);
        make_card_due(&mut col1, card_id)?;
        // On FSRS-7 an immediate Good keeps the Easy interval, so use Hard to
        // make the reviewed schedule differ.
        if version == FsrsVersion::Six {
            col1.answer_good();
        } else {
            col1.answer_hard();
        }
        let reviewed_card = card(&col1, card_id)?;
        // col2 has not seen col1's review and flags the card. Its row wins the
        // merge: put its mtime ahead of col1's so the order is deterministic.
        col2.set_card_flag(&[card_id], 2)?;
        let winning_mtime = TimestampSecs::now().0 + 100;
        col2.storage.db.execute(
            "update cards set mod = ? where id = ?",
            [winning_mtime, card_id.0],
        )?;
        let stale_card = card(&col2, card_id)?;
        assert!(stale_card.mtime > reviewed_card.mtime);
        assert_ne!(stale_card.due, reviewed_card.due);
        assert!(stale_card.last_review_time < reviewed_card.last_review_time);

        sync_ok(&ctx, &mut col2).await;
        sync_ok(&ctx, &mut col1).await;

        let synced_card = card(&col1, card_id)?;
        assert_eq!(synced_card.flags, 2);
        assert_eq!(synced_card.interval, reviewed_card.interval);
        assert_eq!(synced_card.due, reviewed_card.due);
        assert_eq!(synced_card.last_review_time, reviewed_card.last_review_time);
        assert_memory_state_matches_merged_log(&mut col1, card_id)?;

        sync_ok(&ctx, &mut col2).await;
        let other_card = card(&col2, card_id)?;
        assert_eq!(other_card.flags, 2);
        assert_card_rows_converged(&other_card, &synced_card);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs6_stale_winner_without_the_local_review_takes_its_schedule() -> Result<()> {
    stale_winner_without_the_local_review_takes_its_schedule(FsrsVersion::Six).await
}

#[tokio::test]
async fn fsrs7_stale_winner_without_the_local_review_takes_its_schedule() -> Result<()> {
    stale_winner_without_the_local_review_takes_its_schedule(FsrsVersion::Seven).await
}

// Local schedule owners
/////////////////////////

#[tokio::test]
async fn fsrs_set_due_date_after_arriving_review_keeps_local_schedule() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) =
            two_devices_with_easy_card(&ctx, Some(FsrsVersion::Seven)).await?;

        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        // col2 sets the due date after col1's review, so it owns the schedule.
        std::thread::sleep(SECOND);
        col2.set_due_date(&[card_id], "30!", None)?;
        let rescheduled_card = card(&col2, card_id)?;
        assert_eq!(rescheduled_card.interval, 30);

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let synced_card = card(&col2, card_id)?;
        assert_eq!(col2.storage.get_revlog_entries_for_card(card_id)?.len(), 3);
        assert_eq!(synced_card.interval, rescheduled_card.interval);
        assert_eq!(synced_card.due, rescheduled_card.due);
        assert_memory_state_matches_merged_log(&mut col2, card_id)?;

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &synced_card);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_relearning_card_keeps_local_schedule() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) =
            two_devices_with_easy_card(&ctx, Some(FsrsVersion::Seven)).await?;

        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        std::thread::sleep(SECOND);
        make_card_due(&mut col2, card_id)?;
        col2.answer_again();
        let relearning_card = card(&col2, card_id)?;
        assert_eq!(relearning_card.ctype, CardType::Relearn);

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let synced_card = card(&col2, card_id)?;
        assert_eq!(synced_card.ctype, relearning_card.ctype);
        assert_eq!(synced_card.queue, relearning_card.queue);
        assert_eq!(synced_card.due, relearning_card.due);
        assert_eq!(synced_card.interval, relearning_card.interval);
        assert_eq!(synced_card.original_due, relearning_card.original_due);
        let revlog = col2.storage.get_revlog_entries_for_card(card_id)?;
        assert_eq!(revlog.len(), 3);
        let again_review = revlog.iter().max_by_key(|entry| entry.id).unwrap();
        assert_eq!(again_review.button_chosen, 1);
        assert_eq!(
            synced_card.last_review_time,
            Some(again_review.id.as_secs())
        );
        assert_memory_state_matches_merged_log(&mut col2, card_id)?;

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &synced_card);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_suspended_card_keeps_local_schedule() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) =
            two_devices_with_easy_card(&ctx, Some(FsrsVersion::Seven)).await?;

        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        std::thread::sleep(SECOND);
        col2.bury_or_suspend_cards(&[card_id], BuryOrSuspendMode::Suspend)?;
        let suspended_card = card(&col2, card_id)?;
        assert_eq!(suspended_card.queue, CardQueue::Suspended);

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let synced_card = card(&col2, card_id)?;
        assert_eq!(synced_card.queue, CardQueue::Suspended);
        assert_eq!(synced_card.due, suspended_card.due);
        assert_eq!(synced_card.interval, suspended_card.interval);
        assert_eq!(col2.storage.get_revlog_entries_for_card(card_id)?.len(), 2);
        assert_memory_state_matches_merged_log(&mut col2, card_id)?;

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &synced_card);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn sm2_sync_conflict_is_unchanged() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) = two_devices_with_easy_card(&ctx, None).await?;

        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        std::thread::sleep(SECOND);
        make_card_due(&mut col2, card_id)?;
        col2.answer_easy();
        let local_card = card(&col2, card_id)?;
        assert_ne!(local_card.interval, card(&col1, card_id)?.interval);

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let synced_card = card(&col2, card_id)?;
        assert_eq!(synced_card.due, local_card.due);
        assert_eq!(synced_card.interval, local_card.interval);
        assert_eq!(synced_card.ease_factor, local_card.ease_factor);
        assert_eq!(synced_card.queue, local_card.queue);
        assert_eq!(synced_card.ctype, local_card.ctype);
        assert_eq!(synced_card.memory_state, None);
        assert_eq!(synced_card.desired_retention, None);
        assert_eq!(synced_card.decay, None);

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &synced_card);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_reconcile_without_load_balancer_converges() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        enable_fsrs(&mut col1, FsrsVersion::Six)?;
        col1.set_config_bool(BoolKey::LoadBalancerEnabled, false, false)?;
        add_note(&mut col1, "fsrs-no-load-balancer", DeckId(1))?;
        let card_id = col1.answer_easy().card_id;
        sync_fsrs_collections(&ctx, col1).await?;
        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();
        assert!(!col1.get_config_bool(BoolKey::LoadBalancerEnabled));
        assert!(!col2.get_config_bool(BoolKey::LoadBalancerEnabled));

        std::thread::sleep(SECOND);
        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        std::thread::sleep(std::time::Duration::from_millis(1));
        recompute_memory_states(&mut col2, DeckConfigId(1), &[card_id], HashMap::new())?;

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let reconciled_card = card(&col2, card_id)?;
        let reviewed_card = card(&col1, card_id)?;
        assert_eq!(reconciled_card.memory_state, reviewed_card.memory_state);
        assert_eq!(
            reconciled_card.last_review_time,
            reviewed_card.last_review_time
        );
        assert_eq!(reconciled_card.interval, reviewed_card.interval);
        assert_eq!(reconciled_card.due, reviewed_card.due);

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &reconciled_card);

        Ok(())
    })
    .await
}

async fn forgotten_card_is_not_restored_by_sync(version: FsrsVersion) -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) = two_devices_with_easy_card(&ctx, Some(version)).await?;

        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        std::thread::sleep(SECOND);
        // Same flags as the "Forget" action: log the change, keep positions
        // and counts as they are.
        col2.reschedule_cards_as_new(&[card_id], true, false, false, None)?;
        let forgotten_card = card(&col2, card_id)?;
        assert_eq!(forgotten_card.ctype, CardType::New);
        assert_eq!(forgotten_card.queue, CardQueue::New);
        assert_eq!(forgotten_card.memory_state, None);
        let revlog = col2.storage.get_revlog_entries_for_card(card_id)?;
        assert_eq!(revlog.len(), 2);
        assert_eq!(
            revlog
                .iter()
                .max_by_key(|entry| entry.id)
                .unwrap()
                .review_kind,
            RevlogReviewKind::Manual
        );

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let synced_card = card(&col2, card_id)?;
        assert_eq!(synced_card.ctype, CardType::New);
        assert_eq!(synced_card.queue, CardQueue::New);
        assert_eq!(synced_card.memory_state, forgotten_card.memory_state);
        assert_eq!(
            synced_card.last_review_time,
            forgotten_card.last_review_time
        );

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &synced_card);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs6_forgotten_card_is_not_restored_by_sync() -> Result<()> {
    forgotten_card_is_not_restored_by_sync(FsrsVersion::Six).await
}

#[tokio::test]
async fn fsrs7_forgotten_card_is_not_restored_by_sync() -> Result<()> {
    forgotten_card_is_not_restored_by_sync(FsrsVersion::Seven).await
}

async fn reconcile_uses_params_changed_on_other_device(version: FsrsVersion) -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        enable_fsrs(&mut col1, version)?;
        add_note(&mut col1, "fsrs-params-a", DeckId(1))?;
        let card_a = col1.answer_easy().card_id;
        add_note(&mut col1, "fsrs-params-b", DeckId(1))?;
        let card_b = col1.answer_easy().card_id;
        assert_ne!(card_a, card_b);
        sync_fsrs_collections(&ctx, col1).await?;
        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // col1 changes the preset params and recomputes both cards with them.
        let mut new_params = default_params(version);
        for param in &mut new_params[0..4] {
            *param *= 2.0;
        }
        let mut config = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
        set_params(&mut config, version, new_params.clone());
        col1.add_or_update_deck_config(&mut config)?;
        recompute_memory_states(
            &mut col1,
            DeckConfigId(1),
            &[card_a, card_b],
            HashMap::new(),
        )?;
        sync_ok(&ctx, &mut col1).await;

        // col2 still has the old params and reviews card A afterwards.
        std::thread::sleep(SECOND);
        make_card_due(&mut col2, card_a)?;
        col2.answer_good();
        let reviewed_card_a = card(&col2, card_a)?;
        assert_eq!(
            col2.get_deck_config(DeckConfigId(1), false)?
                .unwrap()
                .fsrs_params()
                .to_vec(),
            default_params(version)
        );

        sync_ok(&ctx, &mut col2).await;

        let synced_config = col2.get_deck_config(DeckConfigId(1), false)?.unwrap();
        assert_eq!(synced_config.fsrs_params().to_vec(), new_params);
        assert_eq!(
            card(&col2, card_b)?.memory_state,
            card(&col1, card_b)?.memory_state
        );
        let synced_card_a = card(&col2, card_a)?;
        assert_ne!(synced_card_a.memory_state, reviewed_card_a.memory_state);
        assert_memory_state_matches_merged_log(&mut col2, card_a)?;

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_a)?, &synced_card_a);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs6_reconcile_uses_params_changed_on_other_device() -> Result<()> {
    reconcile_uses_params_changed_on_other_device(FsrsVersion::Six).await
}

#[tokio::test]
async fn fsrs7_reconcile_uses_params_changed_on_other_device() -> Result<()> {
    reconcile_uses_params_changed_on_other_device(FsrsVersion::Seven).await
}

// Fork-specific behavior
//////////////////////////

#[tokio::test]
async fn rwkv_card_is_left_to_the_sync_winner() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) =
            two_devices_with_easy_card(&ctx, Some(FsrsVersion::Seven)).await?;
        let mut config = col2.get_deck_config(DeckConfigId(1), false)?.unwrap();
        config.inner.rwkv_review_enabled = true;
        col2.add_or_update_deck_config(&mut config)?;

        // A stale local edit loses to a later review on col1, which would be
        // reconciled for an FSRS preset.
        make_fsrs_state_stale(&mut col2, card_id, 1.0, 0.97, 0.12)?;
        std::thread::sleep(SECOND);
        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        let remote_winner = card(&col1, card_id)?;

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let synced_card = card(&col2, card_id)?;
        assert_eq!(synced_card.mtime, remote_winner.mtime);
        assert_card_rows_converged(&synced_card, &remote_winner);
        assert!(pending_cards(&col2)?.is_empty());
        // Nothing reconciled was uploaded.
        sync_ok(&ctx, &mut col1).await;
        assert_eq!(card(&col1, card_id)?.mtime, remote_winner.mtime);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn dynamic_desired_retention_card_keeps_its_desired_retention() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) =
            two_devices_with_easy_card_and_config(&ctx, Some(FsrsVersion::Seven), |config| {
                let config = &mut config.inner;
                config.fsrs_dynamic_desired_retention_enabled = true;
                config.fsrs_dynamic_desired_retention_params = vec![0.0; 15];
                config.fsrs_dynamic_desired_retention_weights = vec![0.0, 15.0];
                config.fsrs_dynamic_desired_retention_avg_drs = vec![0.8, 0.9];
                config.fsrs_dynamic_desired_retention_min = 0.75;
                config.fsrs_dynamic_desired_retention_max = 0.95;
            })
            .await?;
        let preset_desired_retention = col2
            .get_deck_config(DeckConfigId(1), false)?
            .unwrap()
            .inner
            .desired_retention;

        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        let reviewed_card = card(&col1, card_id)?;
        // A later stale local edit wins on mtime.
        std::thread::sleep(SECOND);
        let stale_card = make_fsrs_state_stale(&mut col2, card_id, 1.0, 0.93, 0.12)?;
        assert_ne!(stale_card.desired_retention, Some(preset_desired_retention));

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        let reconciled_card = card(&col2, card_id)?;
        assert_eq!(reconciled_card.desired_retention, Some(0.93));
        assert_memory_state_matches_merged_log(&mut col2, card_id)?;
        assert_eq!(reconciled_card.interval, reviewed_card.interval);
        assert_eq!(reconciled_card.due, reviewed_card.due);
        assert_eq!(
            reconciled_card.last_review_time,
            reviewed_card.last_review_time
        );

        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &reconciled_card);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs7_review_arriving_after_its_card_chunk_is_reconciled_once() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let mut col1 = ctx.col1();
        enable_fsrs(&mut col1, FsrsVersion::Seven)?;
        add_note(&mut col1, "fsrs-chunked", DeckId(1))?;
        let card_id = col1.answer_easy().card_id;
        let filler_card = add_note(&mut col1, "fsrs-chunk-filler", DeckId(1))?;
        sync_fsrs_collections(&ctx, col1).await?;
        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        let review_id = col1.storage.get_revlog_entries_for_card(card_id)?[1].id;
        // Newer entries are sent first, so these push the review out of the
        // chunk holding its card.
        for offset in 1..=(CHUNK_SIZE as i64 + 50) {
            col1.storage.add_revlog_entry(
                &RevlogEntry {
                    id: RevlogId(review_id.0 + offset),
                    cid: filler_card,
                    usn: Usn(-1),
                    review_kind: RevlogReviewKind::Manual,
                    ..Default::default()
                },
                false,
            )?;
        }
        let reviewed_card = card(&col1, card_id)?;
        std::thread::sleep(SECOND);
        make_fsrs_state_stale(&mut col2, card_id, 1.0, 0.97, 0.12)?;

        sync_ok(&ctx, &mut col1).await;
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let review_position = out
            .remote_review_ids
            .iter()
            .position(|id| *id == review_id)
            .unwrap();
        assert!(review_position >= CHUNK_SIZE);

        let reconciled_card = card(&col2, card_id)?;
        assert_memory_state_matches_merged_log(&mut col2, card_id)?;
        assert_eq!(reconciled_card.interval, reviewed_card.interval);
        assert_eq!(reconciled_card.due, reviewed_card.due);
        assert!(pending_cards(&col2)?.is_empty());

        // The reconciled row converges, and later syncs upload nothing.
        sync_ok(&ctx, &mut col1).await;
        assert_card_rows_converged(&card(&col1, card_id)?, &reconciled_card);
        assert!(pending_cards(&col1)?.is_empty());
        sync_ok(&ctx, &mut col2).await;
        assert_eq!(card(&col2, card_id)?, reconciled_card);
        assert!(pending_cards(&col2)?.is_empty());

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_reconcile_error_leaves_cards_untouched_and_sync_completes() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2, card_id) =
            two_devices_with_easy_card(&ctx, Some(FsrsVersion::Seven)).await?;

        make_card_due(&mut col1, card_id)?;
        col1.answer_good();
        std::thread::sleep(SECOND);
        let stale_card = make_fsrs_state_stale(&mut col2, card_id, 1.0, 0.97, 0.12)?;
        let mut config = col2.get_deck_config(DeckConfigId(1), false)?.unwrap();
        config.inner.ignore_revlogs_before_date = "not a date".into();
        col2.add_or_update_deck_config(&mut config)?;

        sync_ok(&ctx, &mut col1).await;
        sync_ok(&ctx, &mut col2).await;

        // The local winner is kept exactly as it was.
        let synced_card = card(&col2, card_id)?;
        assert_eq!(
            Card {
                usn: stale_card.usn,
                ..synced_card.clone()
            },
            stale_card
        );
        assert_eq!(col2.storage.get_revlog_entries_for_card(card_id)?.len(), 2);
        assert!(pending_cards(&col2)?.is_empty());

        Ok(())
    })
    .await
}
