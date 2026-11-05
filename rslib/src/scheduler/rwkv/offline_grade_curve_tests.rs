// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use anki_proto::scheduler::bury_or_suspend_cards_request::Mode as BuryOrSuspendMode;
use anki_proto::scheduler::card_answer::Rating;
use anki_proto::scheduler::CardAnswer;
use tempfile::TempDir;

use super::*;
use crate::config::BoolKey;
use crate::scheduler::addon_desired_retention::CONFIG_KEY;
use crate::scheduler::states::SchedulingStates;
use crate::services::SchedulerService;
use crate::tests::open_fs_test_collection;
use crate::tests::NoteAdder;

fn grade_curve_collection(native_dynamic_dr: bool) -> Result<(TempDir, Collection, CardId)> {
    grade_curve_collection_with_model(
        native_dynamic_dr,
        "../qt/aqt/rwkv_inference/sm72/weights.f32.bin",
        true,
    )
}

fn grade_curve_collection_with_model(
    native_dynamic_dr: bool,
    model_path: &str,
    leak_free_inputs: bool,
) -> Result<(TempDir, Collection, CardId)> {
    let (mut col, dir) = open_fs_test_collection("rwkv-grade-curve");
    col.set_config_bool(BoolKey::Fsrs, true, false)?;
    col.update_default_deck_config(|config| {
        config.rwkv_review_enabled = true;
        config.rwkv_review_enforce_grade_order = false;
        if native_dynamic_dr {
            config.fsrs_dynamic_desired_retention_enabled = true;
            config.fsrs_dynamic_desired_retention_params = vec![0.0; 15];
            config.fsrs_dynamic_desired_retention_weights = vec![0.0, 15.0];
            config.fsrs_dynamic_desired_retention_avg_drs = vec![0.8, 0.9];
            config.fsrs_dynamic_desired_retention_min = 0.75;
            config.fsrs_dynamic_desired_retention_max = 0.95;
        }
    });
    NoteAdder::basic(&mut col)
        .fields(&["10", "answer"])
        .add(&mut col);
    let card_id = col.get_first_card().id;
    col.set_due_date(&[card_id], "0", None)?;
    let model = SelectedOfflineModel {
        model: OfflineModel {
            path: Path::new(env!("CARGO_MANIFEST_DIR")).join(model_path),
            precision: StatePrecision::Int8,
        },
        leak_free_inputs,
    };
    let mut runtime = RwkvOfflineRuntime::load(model)?;
    col.rwkv_offline_sync_history(&mut runtime)?;
    col.state.rwkv_offline = Some(Box::new(runtime));
    Ok((dir, col, card_id))
}

fn answer(
    col: &mut Collection,
    card_id: CardId,
    states: SchedulingStates,
    rating: Rating,
    next: CardState,
) -> Result<()> {
    let _ = SchedulerService::answer_card(
        col,
        CardAnswer {
            card_id: card_id.0,
            current_state: Some(states.current.into()),
            new_state: Some(next.into()),
            rating: rating as i32,
            answered_at_millis: TimestampMillis::now().0,
            ..Default::default()
        },
    )?;
    Ok(())
}

#[test]
fn curve_uses_synced_grade_target_for_the_answer_interval() -> Result<()> {
    for (path, leak_free) in [
        ("../qt/aqt/rwkv_inference/sm56/weights.f32.bin", false),
        ("../qt/aqt/rwkv_inference/sm72/weights.f32.bin", true),
        (
            "../qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin",
            false,
        ),
    ] {
        let (_dir, mut col, card_id) = grade_curve_collection_with_model(false, path, leak_free)?;
        let current = col.get_scheduling_states(card_id)?.current;
        let timing = col.timing_today()?;
        let intervals = col
            .rwkv_offline_curve(card_id, &current, [0.9, 0.9, 0.6, 0.9], false, &timing)
            .expect("Curve must score this card");
        let expected = col.scheduling_states_with_intervals(card_id, intervals)?;

        col.set_config(
            CONFIG_KEY,
            &serde_json::json!({
                "version": 1,
                "grade_rules": [{"grade": "good", "mode": "absolute", "value": 0.6}]
            }),
        )?;
        let actual = col.get_scheduling_states(card_id)?;
        assert_eq!(actual.good, expected.good, "model: {path}");
    }
    Ok(())
}

#[test]
fn curve_records_the_grade_target_with_native_dynamic_dr() -> Result<()> {
    let (_dir, mut col, card_id) = grade_curve_collection(true)?;
    col.set_config(
        CONFIG_KEY,
        &serde_json::json!({
            "version": 1,
            "rules": [{"query": "deck:Default", "desired_retention": 0.85}],
            "grade_rules": [{"grade": "good", "mode": "absolute", "value": 0.85}]
        }),
    )?;
    let states = col.get_scheduling_states(card_id)?;
    let mapped = states.dynamic_desired_retentions.unwrap()[2];
    assert_ne!((mapped * 100.0).round() / 100.0, 0.85);
    let good = states.good;
    answer(&mut col, card_id, states, Rating::Good, good)?;
    assert_eq!(col.get_first_card().desired_retention, Some(0.85));
    Ok(())
}

#[test]
fn curve_does_not_record_grade_target_for_a_modified_answer() -> Result<()> {
    let (_dir, mut col, card_id) = grade_curve_collection(true)?;
    col.set_config(
        CONFIG_KEY,
        &serde_json::json!({
            "version": 1,
            "rules": [{"query": "deck:Default", "desired_retention": 0.85}],
            "grade_rules": [{"grade": "good", "mode": "absolute", "value": 0.85}]
        }),
    )?;
    let states = col.get_scheduling_states(card_id)?;
    let mapped = states.dynamic_desired_retentions.unwrap()[2];
    let mut modified = states.good;
    let CardState::Normal(NormalState::Review(ref mut review)) = modified else {
        panic!("expected a review state");
    };
    review.scheduled_days = review.scheduled_days.saturating_sub(1).max(1);
    assert_ne!(modified, states.good);
    answer(&mut col, card_id, states, Rating::Good, modified)?;
    assert_eq!(
        col.get_first_card().desired_retention,
        Some((mapped * 100.0).round() / 100.0)
    );
    Ok(())
}

#[test]
fn curve_relative_grade_uses_the_preset_as_its_base() -> Result<()> {
    let (_dir, mut col, card_id) = grade_curve_collection(false)?;
    col.set_config(
        CONFIG_KEY,
        &serde_json::json!({
            "version": 1,
            "grade_rules": [{"grade": "good", "mode": "relative", "value": -0.2}]
        }),
    )?;
    let states = col.get_scheduling_states(card_id)?;
    let good = states.good;
    answer(&mut col, card_id, states, Rating::Good, good)?;
    assert_eq!(col.get_first_card().desired_retention, Some(0.72));
    Ok(())
}

#[test]
fn curve_grade_target_still_obeys_field_interval_bounds() -> Result<()> {
    let (_dir, mut col, card_id) = grade_curve_collection(true)?;
    col.set_config(
        CONFIG_KEY,
        &serde_json::json!({
            "version": 1,
            "field_rules": [{"note_type": "Basic", "field": "Front", "value_type": "number",
                "thresholds": [{"value": "10", "desired_retention": 0.85,
                    "minimum_interval": 15, "maximum_interval": 15}]}],
            "grade_rules": [{"grade": "good", "mode": "absolute", "value": 0.85}]
        }),
    )?;
    let states = col.get_scheduling_states(card_id)?;
    let CardState::Normal(NormalState::Review(review)) = states.good else {
        panic!("expected a review state");
    };
    assert_eq!(review.scheduled_days, 15);
    let good = states.good;
    answer(&mut col, card_id, states, Rating::Good, good)?;
    let card = col.get_first_card();
    assert_eq!(card.interval, 15);
    assert_eq!(card.desired_retention, Some(0.85));
    Ok(())
}

#[test]
fn curve_unconfigured_grade_keeps_native_dynamic_retention() -> Result<()> {
    let (_dir, mut col, card_id) = grade_curve_collection(true)?;
    col.set_config(
        CONFIG_KEY,
        &serde_json::json!({
            "version": 1,
            "rules": [{"query": "deck:Default", "desired_retention": 0.85}],
            "grade_rules": [{"grade": "good", "mode": "absolute", "value": 0.85}]
        }),
    )?;
    let states = col.get_scheduling_states(card_id)?;
    let mapped = states.dynamic_desired_retentions.unwrap()[3];
    let easy = states.easy;
    answer(&mut col, card_id, states, Rating::Easy, easy)?;
    assert_eq!(
        col.get_first_card().desired_retention,
        Some((mapped * 100.0).round() / 100.0)
    );
    Ok(())
}

#[test]
fn unscoreable_curve_card_falls_back_to_fsrs_grade_rules() -> Result<()> {
    let (_dir, mut col, card_id) = grade_curve_collection(false)?;
    col.set_config(
        CONFIG_KEY,
        &serde_json::json!({
            "version": 1,
            "grade_rules": [{"grade": "good", "mode": "absolute", "value": 0.85}]
        }),
    )?;
    col.get_scheduling_states(card_id)?;
    assert!(col.rwkv_offline_curve_s90(card_id, 2).is_some());
    let _ = col.bury_or_suspend_cards(&[card_id], BuryOrSuspendMode::Suspend)?;

    let states = col.get_scheduling_states(card_id)?;
    let CardState::Normal(NormalState::Review(good)) = states.good else {
        panic!("expected a review state");
    };
    let stability = good.memory_state.unwrap().stability_internal;
    let fsrs_interval = col.fsrs_interval_at_retrievability_for_card(card_id, stability, 0.85)?;
    assert_eq!(good.scheduled_days, ((fsrs_interval + 0.5) as u32).max(1));
    assert!(col.rwkv_offline_curve_s90(card_id, 2).is_none());
    Ok(())
}
