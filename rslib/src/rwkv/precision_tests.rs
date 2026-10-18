// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use super::*;

fn runtime(precision: StatePrecision) -> RwkvInference {
    let weights = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin");
    RwkvInference::load_with_precision(weights, 0.9, 36_500, precision).unwrap()
}

fn reviews(count: usize) -> Vec<ReviewInput> {
    (0..count)
        .map(|index| ReviewInput {
            card_id: 100 + (index % 7) as i64,
            note_id: Some(10 + (index % 7 / 2) as i64),
            deck_id: Some(1 + (index % 2) as i64),
            preset_id: Some(1),
            is_query: false,
            ease: Some((index % 4 + 1) as u8),
            duration_millis: Some(1234 + index as i64 * 7),
            card_type: Some(if index < 7 { 0 } else { 2 }),
            day_offset: Some(1000 + (index / 7) as i64),
            current_elapsed_days: Some((index / 7) as i64),
            current_elapsed_seconds: Some((index / 7) as i64 * 86_400),
            target_retentions: [Some(0.9); 4],
            enforce_grade_order: true,
        })
        .collect()
}

fn snapshot_bytes(inference: &RwkvInference) -> Vec<u8> {
    let mut out = Vec::new();
    for states in [
        &inference.warm_up_states.card as &dyn StateMap,
        &inference.warm_up_states.note,
        &inference.warm_up_states.deck,
        &inference.warm_up_states.preset,
    ] {
        write_snapshot_state_map(&mut out, states).unwrap();
    }
    write_snapshot_optional_bytes(
        &mut out,
        inference
            .warm_up_states
            .global
            .as_ref()
            .map(serialize_module_state)
            .as_deref(),
    )
    .unwrap();
    write_snapshot_optional_bytes(&mut out, Some(&inference.cache_state())).unwrap();
    out
}

#[test]
fn rwkv_state_precision_bulk_matches_answers_across_chunk_boundaries() {
    let reviews = reviews(51);
    for precision in [
        StatePrecision::F32,
        StatePrecision::Int8,
        StatePrecision::Int4,
    ] {
        let mut sequential = runtime(precision);
        sequential
            .warm_up_reviews_sequential(reviews.clone(), false)
            .unwrap();
        for chunk_size in [1, 9, 51] {
            let mut bulk = runtime(precision);
            bulk::warm_up_reviews_bulk_chunked(&mut bulk, reviews.clone(), false, chunk_size)
                .unwrap();
            assert_eq!(
                snapshot_bytes(&sequential),
                snapshot_bytes(&bulk),
                "{precision:?}, chunk {chunk_size}"
            );
        }
        if precision != StatePrecision::F32 {
            assert!(sequential
                .warm_up_states
                .card
                .values()
                .chain(sequential.warm_up_states.note.values())
                .all(|state| state.layers.is_empty() && state.packed.is_some()));
            assert!(sequential
                .warm_up_states
                .deck
                .values()
                .chain(sequential.warm_up_states.preset.values())
                .chain(&sequential.warm_up_states.global)
                .all(|state| state.packed.is_none()));
        }
    }
}

#[test]
fn rwkv_state_precision_queries_and_undo_read_packed_history() {
    for precision in [StatePrecision::Int8, StatePrecision::Int4] {
        let mut inference = runtime(precision);
        inference.warm_up_reviews(reviews(49), false).unwrap();
        let mut query = reviews(1).remove(0);
        query.is_query = true;
        query.ease = None;
        query.duration_millis = None;
        query.day_offset = Some(1010);
        query.current_elapsed_days = Some(4);
        query.current_elapsed_seconds = Some(4 * 86_400);
        let features = inference.features.features_for(&query);
        let packed = inference.warm_up_states.state_owned(&query);
        let decoded = SrsStateOwned {
            card: packed.card.clone().map(ModuleState::into_decoded),
            note: packed.note.clone().map(ModuleState::into_decoded),
            deck: packed.deck.clone(),
            preset: packed.preset.clone(),
            global: packed.global.clone(),
        };
        // Query-only scalar, GEMM batch, and full review heads all read the same
        // truncated history. Empty packed layers must never mean fresh state.
        let items = [ReviewPredictionQueryRef {
            features: &features,
            state: packed.as_ref(),
        }; 130];
        let expected = [ReviewPredictionQueryRef {
            features: &features,
            state: decoded.as_ref(),
        }; 130];
        assert_eq!(
            inference
                .model
                .review_retrievability_query_refs_scalar(&items),
            inference
                .model
                .review_retrievability_query_refs_scalar(&expected)
        );
        assert_eq!(
            inference
                .model
                .review_retrievability_query_refs_batched(&items),
            inference
                .model
                .review_retrievability_query_refs_batched(&expected)
        );
        let heads = inference.model.review(&features, packed.as_ref());
        let expected_heads = inference.model.review(&features, decoded.as_ref());
        assert_eq!(heads.retrievability, expected_heads.retrievability);
        let mut fresh = runtime(precision);
        let fresh_features = fresh.features.features_for(&query);
        let fresh_state = fresh.warm_up_states.state_owned(&query);
        let fresh_heads = fresh.model.review(&fresh_features, fresh_state.as_ref());
        assert!((heads.retrievability - fresh_heads.retrievability).abs() > 1e-4);

        let before = snapshot_bytes(&inference);
        let state = inference.warm_up_state(&query);
        let context = inference.state_for_card(query.card_id);
        let mut answer = query.clone();
        answer.is_query = false;
        answer.ease = Some(3);
        answer.duration_millis = Some(2345);
        inference.warm_up_reviews(vec![answer], false).unwrap();
        assert_ne!(snapshot_bytes(&inference), before);
        inference.restore_state(&context);
        inference
            .restore_warm_up_state(
                query.card_id,
                query.note_id,
                query.deck_id,
                query.preset_id,
                state,
            )
            .unwrap();
        assert_eq!(snapshot_bytes(&inference), before);
    }
}

#[test]
fn rwkv_state_precision_session_checkpoints_and_spills_preserve_continuation() {
    let reviews = reviews(49);
    for precision in [
        StatePrecision::F32,
        StatePrecision::Int8,
        StatePrecision::Int4,
    ] {
        let mut uninterrupted = runtime(precision);
        // Use the same ID feature assignment policy before/after each day.
        for session in reviews.chunks(7) {
            uninterrupted
                .warm_up_reviews(session.to_vec(), false)
                .unwrap();
        }
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.sqlite3");
        let mut restarted = runtime(precision);
        let mut parent = None;
        for (day, session) in reviews.chunks(7).enumerate() {
            restarted.warm_up_reviews(session.to_vec(), false).unwrap();
            let segment = restarted
                .write_warm_up_state_checkpoint(
                    path.clone(),
                    "test",
                    parent,
                    day as i64,
                    ((day + 1) * 7) as i64,
                    "history",
                    "replay",
                    &[],
                    &[],
                    &[],
                    parent.is_none(),
                    true,
                )
                .unwrap();
            restarted.finish_warm_up_state_checkpoints().unwrap();
            let expected = snapshot_bytes(&restarted);
            restarted = runtime(precision);
            restarted
                .restore_warm_up_state_checkpoint(path.clone(), "test", segment)
                .unwrap();
            assert_eq!(snapshot_bytes(&restarted), expected);
            parent = Some(segment);
        }
        assert_eq!(snapshot_bytes(&restarted), snapshot_bytes(&uninterrupted));
        let before = snapshot_bytes(&restarted);
        restarted
            .stash_warm_up_state_to_file(&dir.path().join("spill.bin"))
            .unwrap();
        restarted
            .warm_up_reviews(reviews[..7].to_vec(), false)
            .unwrap();
        restarted.restore_stashed_warm_up_state().unwrap();
        assert_eq!(snapshot_bytes(&restarted), before);
        assert!(!dir.path().join("spill.bin").exists());
    }
}

#[test]
fn rwkv_state_precision_int4_keeps_note_states_int8() {
    let inference = runtime(StatePrecision::Int4);
    assert_eq!(inference.model.modules[0].precision, StatePrecision::Int4);
    assert_eq!(inference.model.modules[2].precision, StatePrecision::Int8);
}
