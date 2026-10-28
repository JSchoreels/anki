// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use anki_proto::scheduler::bury_or_suspend_cards_request::Mode as BuryOrSuspendMode;
use anki_proto::scheduler::card_answer::Rating;
use anki_proto::scheduler::CardAnswer;

use super::*;
use crate::card::FsrsMemoryState;
use crate::config::BoolKey;
use crate::scheduler::states::SchedulingStates;
use crate::services::SchedulerService;
use crate::tests::open_fs_test_collection;
use crate::tests::CardAdder;

fn answer_easy(
    col: &mut Collection,
    card_id: CardId,
    states: SchedulingStates,
    s90: Option<f32>,
) -> Result<()> {
    let mut next = states.easy;
    if let Some(s90) = s90 {
        // Desktop's apply_review_s90_overrides creates this memory state and
        // set_answer_rwkv_metadata sends the same S90 on the answer.
        let CardState::Normal(NormalState::Review(review)) = &mut next else {
            panic!("expected Easy to graduate to Review");
        };
        review.memory_state = Some(FsrsMemoryState {
            stability: s90,
            stability_internal: s90,
            stability_fast: None,
            difficulty: 5.0,
        });
    }
    let _ = SchedulerService::answer_card(
        col,
        CardAnswer {
            card_id: card_id.0,
            current_state: Some(states.current.into()),
            new_state: Some(next.into()),
            rating: Rating::Easy as i32,
            answered_at_millis: TimestampMillis::now().0,
            rwkv_s90: s90,
            ..Default::default()
        },
    )?;
    Ok(())
}

#[test]
fn curve_answer_matches_desktop_when_fsrs_is_disabled() -> Result<()> {
    let (mut col, _dir) = open_fs_test_collection("rwkv-curve-fsrs-off");
    col.set_config_bool(BoolKey::Fsrs, false, false)?;
    col.update_default_deck_config(|config| config.rwkv_review_enabled = true);
    let card_id = CardAdder::new().add(&mut col)[0].id;
    let sm2_states = col.get_scheduling_states(card_id)?;
    let CardState::Normal(NormalState::Review(sm2_easy)) = sm2_states.easy else {
        panic!("expected Easy to graduate to Review");
    };

    let model = OfflineModel {
        path: Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../qt/aqt/rwkv_inference/sm56/weights.f32.bin"),
        precision: StatePrecision::Int8,
    };
    let mut runtime = RwkvOfflineRuntime::load(model)?;
    col.rwkv_offline_sync_history(&mut runtime)?;
    let row = col
        .rwkv_review_input_rows_for_cards(RwkvReviewInputRowsForCardsRequest {
            card_ids: vec![card_id.0],
            include_new_cards: true,
            ..Default::default()
        })?
        .rows
        .pop()
        .unwrap();
    let (_, s90s) = runtime.inference.curve_from_warm_up(query_input(&row))?;
    let s90 = s90s[3].expect("Curve must predict Easy's S90");
    col.state.rwkv_offline = Some(Box::new(runtime));

    let kuma_states = col.get_scheduling_states(card_id)?;
    let CardState::Normal(NormalState::Review(kuma_easy)) = kuma_states.easy else {
        panic!("expected Easy to graduate to Review");
    };
    assert_eq!(kuma_easy.scheduled_days, sm2_easy.scheduled_days);
    answer_easy(&mut col, card_id, kuma_states, None)?;
    let kuma_card = col.storage.get_card(card_id)?.unwrap();
    let kuma_revlog = col
        .storage
        .get_revlog_entries_for_card(card_id)?
        .pop()
        .unwrap();

    col.undo()?;
    let desktop_states = col.get_scheduling_states(card_id)?;
    answer_easy(&mut col, card_id, desktop_states, Some(s90))?;
    let desktop_card = col.storage.get_card(card_id)?.unwrap();
    let desktop_revlog = col
        .storage
        .get_revlog_entries_for_card(card_id)?
        .pop()
        .unwrap();

    assert!(desktop_card.memory_state.is_some());
    assert_eq!(kuma_card.memory_state, desktop_card.memory_state);
    assert_eq!(kuma_revlog.ease_factor, desktop_revlog.ease_factor);
    Ok(())
}

#[test]
fn curve_answer_states_expose_s90_when_fsrs_is_enabled() -> Result<()> {
    let (mut col, _dir) = open_fs_test_collection("rwkv-curve-fsrs-on");
    col.set_config_bool(BoolKey::Fsrs, true, false)?;
    col.update_default_deck_config(|config| config.rwkv_review_enabled = true);
    let card_id = CardAdder::new().add(&mut col)[0].id;
    let CardState::Normal(NormalState::Review(fsrs_easy)) =
        col.get_scheduling_states(card_id)?.easy
    else {
        panic!("expected Easy to graduate to Review");
    };
    let fsrs_memory = fsrs_easy.memory_state.unwrap();

    let model = OfflineModel {
        path: Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../qt/aqt/rwkv_inference/sm56/weights.f32.bin"),
        precision: StatePrecision::Int8,
    };
    let mut runtime = RwkvOfflineRuntime::load(model)?;
    col.rwkv_offline_sync_history(&mut runtime)?;
    let row = col
        .rwkv_review_input_rows_for_cards(RwkvReviewInputRowsForCardsRequest {
            card_ids: vec![card_id.0],
            include_new_cards: true,
            ..Default::default()
        })?
        .rows
        .pop()
        .unwrap();
    let (_, s90s) = runtime.inference.curve_from_warm_up(query_input(&row))?;
    let s90 = s90s[3].unwrap();
    col.state.rwkv_offline = Some(Box::new(runtime));

    let CardState::Normal(NormalState::Review(curve_easy)) =
        col.get_scheduling_states(card_id)?.easy
    else {
        panic!("expected Easy to graduate to Review");
    };
    let curve_memory = curve_easy.memory_state.unwrap();
    assert_ne!(s90, fsrs_memory.stability);
    assert_eq!(curve_memory.stability, s90);
    assert_eq!(
        curve_memory.stability_internal,
        fsrs_memory.stability_internal
    );
    assert_eq!(curve_memory.difficulty, fsrs_memory.difficulty);
    Ok(())
}

#[test]
fn unscoreable_card_does_not_reuse_earlier_curve_s90() -> Result<()> {
    let (mut col, _dir) = open_fs_test_collection("rwkv-stale-curve-s90");
    col.set_config_bool(BoolKey::Fsrs, true, false)?;
    col.update_default_deck_config(|config| config.rwkv_review_enabled = true);
    let card_id = CardAdder::new().add(&mut col)[0].id;
    let other_id = CardAdder::new().add(&mut col)[0].id;
    let model = OfflineModel {
        path: Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../qt/aqt/rwkv_inference/sm56/weights.f32.bin"),
        precision: StatePrecision::Int8,
    };
    let mut runtime = RwkvOfflineRuntime::load(model)?;
    col.rwkv_offline_sync_history(&mut runtime)?;
    col.state.rwkv_offline = Some(Box::new(runtime));
    let _ = col.get_scheduling_states(card_id)?;
    assert!(col.rwkv_offline_curve_s90(card_id, 3).is_some());
    let _ = col.get_scheduling_states(other_id)?;
    let other_s90 = col.rwkv_offline_curve_s90(other_id, 3);
    assert!(other_s90.is_some());

    col.bury_or_suspend_cards(&[card_id], BuryOrSuspendMode::Suspend)?;
    // Suspended new cards are not eligible for a new Curve prediction.
    let _ = col.get_scheduling_states(card_id)?;
    assert_eq!(col.rwkv_offline_curve_s90(card_id, 3), None);
    assert_eq!(col.rwkv_offline_curve_s90(other_id, 3), other_s90);
    Ok(())
}
