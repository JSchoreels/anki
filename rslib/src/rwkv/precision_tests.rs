// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use super::state_scope_bench::load_for_scopes;
use super::*;

const PRECISIONS: [(StatePrecision, [bool; 3]); 5] = [
    (StatePrecision::F32, [false; 3]),
    (StatePrecision::Int8, [false; 3]),
    (StatePrecision::Int4, [false; 3]),
    (StatePrecision::Int4CardsInt8Notes, [false; 3]),
    (StatePrecision::Int8, [true; 3]),
];

fn runtime(precision: StatePrecision, shared: [bool; 3]) -> RwkvInference {
    let weights = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(MODEL_PATH);
    load_for_scopes(weights, precision, shared)
}

pub(super) fn reviews(count: usize) -> Vec<ReviewInput> {
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

pub(super) fn snapshot_bytes(inference: &RwkvInference) -> Vec<u8> {
    let mut out = Vec::new();
    for states in [
        &inference.warm_up_states.card,
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
fn rwkv_state_precision_mixed_packs_cards_as_int4_and_notes_as_int8() {
    let precision = StatePrecision::parse("cards_int4_notes_int8").unwrap();
    let mut mixed = runtime(precision, [false; 3]);
    let mut int8 = runtime(StatePrecision::Int8, [false; 3]);
    mixed.warm_up_reviews(reviews(14), false).unwrap();
    int8.warm_up_reviews(reviews(14), false).unwrap();
    for (&id, state) in &mixed.warm_up_states.card {
        let bytes = state.packed.as_ref().unwrap();
        assert_eq!(bytes[precision::MAGIC.len()], 4);
        assert!(bytes.len() < int8.warm_up_states.card[&id].packed.as_ref().unwrap().len());
    }
    for (&id, state) in &mixed.warm_up_states.note {
        let bytes = state.packed.as_ref().unwrap();
        assert_eq!(bytes[precision::MAGIC.len()], 8);
        assert_eq!(
            bytes.len(),
            int8.warm_up_states.note[&id].packed.as_ref().unwrap().len()
        );
    }
    assert!(mixed
        .warm_up_states
        .deck
        .values()
        .chain(mixed.warm_up_states.preset.values())
        .chain(&mixed.warm_up_states.global)
        .all(|state| state.packed.is_none()));
}

#[test]
fn rwkv_state_precision_bulk_matches_answers_across_chunk_boundaries() {
    let reviews = reviews(51);
    for (precision, shared) in PRECISIONS {
        let mut sequential = runtime(precision, shared);
        sequential
            .warm_up_reviews_sequential(reviews.clone(), false)
            .unwrap();
        for chunk_size in [1, 9, 51] {
            let mut bulk = runtime(precision, shared);
            bulk::warm_up_reviews_bulk_chunked(&mut bulk, reviews.clone(), false, chunk_size)
                .unwrap();
            assert_eq!(
                snapshot_bytes(&sequential),
                snapshot_bytes(&bulk),
                "{precision:?}, shared {shared:?}, chunk {chunk_size}"
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
                .all(|state| state.packed.is_some() == shared[0]));
        }
    }
}

#[test]
fn rwkv_state_precision_queries_and_undo_read_packed_history() {
    for (precision, shared) in PRECISIONS
        .into_iter()
        .filter(|(p, _)| *p != StatePrecision::F32)
    {
        let mut inference = runtime(precision, shared);
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
            deck: packed.deck.clone().map(ModuleState::into_decoded),
            preset: packed.preset.clone().map(ModuleState::into_decoded),
            global: packed.global.clone().map(ModuleState::into_decoded),
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
        let mut fresh = runtime(precision, shared);
        let fresh_features = fresh.features.features_for(&query);
        let fresh_heads = fresh
            .model
            .review(&fresh_features, fresh.warm_up_states.state_ref(&query));
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
    for (precision, shared) in PRECISIONS {
        let mut uninterrupted = runtime(precision, shared);
        // Use the same ID feature assignment policy before/after each day.
        for session in reviews.chunks(7) {
            uninterrupted
                .warm_up_reviews(session.to_vec(), false)
                .unwrap();
        }
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.sqlite3");
        let mut restarted = runtime(precision, shared);
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
            restarted = runtime(precision, shared);
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
