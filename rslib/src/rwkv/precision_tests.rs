// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use super::state_scope_bench::load_for_scopes;
use super::*;

const PRECISIONS: [(StatePrecision, [bool; 3]); 8] = [
    (StatePrecision::F32, [false; 3]),
    (StatePrecision::Int8, [false; 3]),
    (StatePrecision::Int4CardsInt8Notes, [false; 3]),
    (StatePrecision::Int8, [true; 3]),
    (StatePrecision::CompactCardsInt8, [false; 3]),
    (StatePrecision::CompactCardsInt4, [false; 3]),
    (
        StatePrecision::CompactLowRank(RankSchedule::DEFAULT),
        [false; 3],
    ),
    (
        StatePrecision::CompactLowRank(RankSchedule::uniform(2)),
        [false; 3],
    ),
];

/// The tiny model (the smaller instant-only model, SM101): one card layer.
const TINY_MODEL_PATH: &str = "../qt/aqt/rwkv_inference/sm101/weights.f32.bin";

/// The Tiny two-head model (SM106): the tiny plan with a trained curve head.
const TINY_TWO_HEAD_MODEL_PATH: &str = "../qt/aqt/rwkv_inference/sm106/weights.f32.bin";

/// The six-layer Tiny two-head model (SM130): the six-layer plan (card 1,
/// deck 2, note 1, preset 1, user 1 layers; two schedule rounds).
const SIX_LAYER_MODEL_PATH: &str = "../qt/aqt/rwkv_inference/sm130th/weights.f32.bin";

/// The engine's test model and, in the small engine, the tiny model and the
/// six-layer Tiny (SM130): every state precision must run every layer plan.
fn models() -> Vec<PathBuf> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut models = vec![root.join(MODEL_PATH)];
    if SMALL_MODEL {
        models.push(root.join(TINY_MODEL_PATH));
        models.push(root.join(SIX_LAYER_MODEL_PATH));
    }
    models
}

fn runtime(weights: &Path, precision: StatePrecision, shared: [bool; 3]) -> RwkvInference {
    load_for_scopes(weights.to_path_buf(), precision, shared)
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
fn rwkv_state_precision_rejects_removed_full_int4_without_panicking() {
    // Full int4 was removed; callers fall back to the default policy.
    assert!(StatePrecision::parse("int4").is_err());
    assert_eq!(
        StatePrecision::parse("cards_int4_notes_int8").unwrap(),
        StatePrecision::Int4CardsInt8Notes
    );
}

#[test]
fn rwkv_state_precision_mixed_packs_cards_as_int4_and_notes_as_int8() {
    for weights in models() {
        check_mixed_packs_cards_as_int4_and_notes_as_int8(&weights);
    }
}

fn check_mixed_packs_cards_as_int4_and_notes_as_int8(weights: &Path) {
    let mut int8 = runtime(weights, StatePrecision::Int8, [false; 3]);
    int8.warm_up_reviews(reviews(14), false).unwrap();
    // (policy, card precision byte, note precision byte, card smaller than int8,
    // note smaller than int8)
    for (name, card_code, note_code, card_smaller, note_smaller) in [
        ("cards_int4_notes_int8", 4, 8, true, false),
        ("compact_cards_int8", 0x18, 0x18, true, true),
        ("compact_cards_int4", 0x14, 0x18, true, true),
        ("compact_lowrank_int8", 0x28, 0x28, true, true),
        ("compact_lowrank_int8:2", 0x28, 0x28, true, true),
    ] {
        let precision = StatePrecision::parse(name).unwrap();
        let mut mixed = runtime(weights, precision, [false; 3]);
        mixed.warm_up_reviews(reviews(14), false).unwrap();
        for (&id, state) in &mixed.warm_up_states.card {
            let bytes = state.packed.as_ref().unwrap();
            assert_eq!(bytes[precision::MAGIC.len()], card_code, "{name}");
            let int8_len = int8.warm_up_states.card[&id].packed.as_ref().unwrap().len();
            assert_eq!(bytes.len() < int8_len, card_smaller, "{name}");
        }
        for (&id, state) in &mixed.warm_up_states.note {
            let bytes = state.packed.as_ref().unwrap();
            assert_eq!(bytes[precision::MAGIC.len()], note_code, "{name}");
            let int8_len = int8.warm_up_states.note[&id].packed.as_ref().unwrap().len();
            assert_eq!(bytes.len() < int8_len, note_smaller, "{name}");
        }
        assert!(mixed
            .warm_up_states
            .deck
            .values()
            .chain(mixed.warm_up_states.preset.values())
            .chain(&mixed.warm_up_states.global)
            .all(|state| state.packed.is_none()));
    }
}

#[test]
fn rwkv_state_precision_bulk_matches_answers_across_chunk_boundaries() {
    let reviews = reviews(51);
    for (weights, (precision, shared)) in models()
        .into_iter()
        .flat_map(|weights| PRECISIONS.map(|precision| (weights.clone(), precision)))
    {
        let mut sequential = runtime(&weights, precision, shared);
        sequential
            .warm_up_reviews_sequential(reviews.clone(), false)
            .unwrap();
        for chunk_size in [1, 9, 51] {
            let mut bulk = runtime(&weights, precision, shared);
            bulk::warm_up_reviews_bulk_chunked(&mut bulk, reviews.clone(), false, chunk_size)
                .unwrap();
            assert_eq!(
                snapshot_bytes(&sequential),
                snapshot_bytes(&bulk),
                "{weights:?} {precision:?}, shared {shared:?}, chunk {chunk_size}"
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
    for (weights, (precision, shared)) in models().into_iter().flat_map(|weights| {
        PRECISIONS
            .into_iter()
            .filter(|(p, _)| *p != StatePrecision::F32)
            .map(move |precision| (weights.clone(), precision))
    }) {
        let mut inference = runtime(&weights, precision, shared);
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
            card: packed
                .card
                .clone()
                .map(|state| state.try_into_decoded().unwrap()),
            note: packed
                .note
                .clone()
                .map(|state| state.try_into_decoded().unwrap()),
            deck: packed
                .deck
                .clone()
                .map(|state| state.try_into_decoded().unwrap()),
            preset: packed
                .preset
                .clone()
                .map(|state| state.try_into_decoded().unwrap()),
            global: packed
                .global
                .clone()
                .map(|state| state.try_into_decoded().unwrap()),
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
                .review_retrievability_query_refs_scalar(&items)
                .unwrap(),
            inference
                .model
                .review_retrievability_query_refs_scalar(&expected)
                .unwrap()
        );
        assert_eq!(
            inference
                .model
                .review_retrievability_query_refs_batched(&items)
                .unwrap(),
            inference
                .model
                .review_retrievability_query_refs_batched(&expected)
                .unwrap()
        );
        let heads = inference.model.review(&features, packed.as_ref());
        let expected_heads = inference.model.review(&features, decoded.as_ref());
        assert_eq!(heads.retrievability, expected_heads.retrievability);
        let mut fresh = runtime(&weights, precision, shared);
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
fn rwkv_state_precision_queue_scoring_reports_invalid_resident_state() {
    for weights in models() {
        check_queue_scoring_reports_invalid_resident_state(&weights);
    }
}

fn check_queue_scoring_reports_invalid_resident_state(weights: &Path) {
    let mut inference = runtime(weights, StatePrecision::Int4CardsInt8Notes, [false; 3]);
    let mut answer = reviews(1).remove(0);
    // Malformed historical data can produce a non-finite in-memory state.
    answer.duration_millis = Some(-10);
    inference
        .warm_up_reviews(vec![answer.clone()], false)
        .unwrap();

    let mut query = answer;
    query.is_query = true;
    query.ease = None;
    query.duration_millis = None;
    let error = inference
        .predict_retrievability_many_from_warm_up(vec![query.clone()])
        .unwrap_err();

    assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    assert_eq!(
        error.to_string(),
        "RWKV card state: invalid RWKV compact state: non-finite shift"
    );

    // macOS uses the batched path, while other platforms use the scalar path.
    let features = inference.features.features_for(&query);
    let items = [ReviewPredictionQueryRef {
        features: &features,
        state: inference.warm_up_states.state_ref(&query),
    }];
    for result in [
        inference
            .model
            .review_retrievability_query_refs_scalar(&items),
        inference
            .model
            .review_retrievability_query_refs_batched(&items),
    ] {
        let error = result.unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert_eq!(
            error.to_string(),
            "RWKV card state: invalid RWKV compact state: non-finite shift"
        );
    }
}

#[test]
fn rwkv_state_precision_session_checkpoints_and_spills_preserve_continuation() {
    let reviews = reviews(49);
    for (weights, (precision, shared)) in models()
        .into_iter()
        .flat_map(|weights| PRECISIONS.map(|precision| (weights.clone(), precision)))
    {
        let mut uninterrupted = runtime(&weights, precision, shared);
        // Use the same ID feature assignment policy before/after each day.
        for session in reviews.chunks(7) {
            uninterrupted
                .warm_up_reviews(session.to_vec(), false)
                .unwrap();
        }
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.sqlite3");
        let mut restarted = runtime(&weights, precision, shared);
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
            restarted = runtime(&weights, precision, shared);
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

/// Bytes of one stored card or note layer: shift vectors at f16 and the WKV
/// matrix as one f32 scale plus the codes per row (int8 and int4); in the
/// compact layout int8 shift codes with one f16 scale per vector and one f16
/// scale per row; or the f32 layout without compaction.
fn stored_layer_bytes(precision: StatePrecision, channel_mixer: bool) -> usize {
    let rows = HEADS * HEAD_SIZE;
    let channel = usize::from(channel_mixer);
    match precision {
        StatePrecision::F32 => {
            (4 + 4 * D_MODEL) + (4 + 4 * rows * HEAD_SIZE) + 4 + 4 * D_MODEL * channel
        }
        StatePrecision::Int8 | StatePrecision::Int4 => {
            let codes = if precision == StatePrecision::Int8 {
                HEAD_SIZE
            } else {
                HEAD_SIZE / 2
            };
            (4 + 2 * D_MODEL) + (4 + rows * (4 + codes)) + 4 + 2 * D_MODEL * channel
        }
        StatePrecision::Int8Compact | StatePrecision::Int4Compact => {
            let codes = if precision == StatePrecision::Int8Compact {
                HEAD_SIZE
            } else {
                HEAD_SIZE / 2
            };
            (4 + 2 + D_MODEL) + (4 + rows * (2 + codes)) + 4 + (2 + D_MODEL) * channel
        }
        _ => unreachable!(),
    }
}

/// Stored card and note state per precision: the tiny models (the Tiny
/// Instant-Only model (SM101) and the Tiny two-head model (SM106)) keep one
/// card layer (the one with a channel mixer), the small model two; notes are
/// the same. Per card + note, int8: small 4,554 B, tiny 3,134 B; int4 cards +
/// int8 notes: 3,530 B and 2,622 B; f32: 13,640 B and 9,276 B. Compact
/// (float16 scales, int8 shifts), card / note: int8 cards small 2,545 /
/// 1,315 B, tiny 1,315 / 1,315 B; int4 cards small 1,521 / 1,315 B, tiny
/// 803 / 1,315 B (the training repository's `w8+s8hx8` and `w8+s4c8nhx8`).
/// The six-layer Tiny (SM130) stores exactly the tiny sizes: it keeps the
/// tiny card and note layers and cuts only the deck, preset and user
/// streams, whose states are per deck, preset and collection; at the
/// default compact int8 format 1,315 B per card and per note.
#[test]
fn rwkv_state_precision_stored_sizes_of_the_small_and_tiny_models() {
    if !SMALL_MODEL {
        return;
    }
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut per_card_and_note = Vec::new();
    for (weights, card_layers) in [
        (root.join(MODEL_PATH), 2),
        (root.join(TINY_MODEL_PATH), 1),
        (root.join(TINY_TWO_HEAD_MODEL_PATH), 1),
        (root.join(SIX_LAYER_MODEL_PATH), 1),
    ] {
        for (precision, card, note) in [
            (
                StatePrecision::F32,
                StatePrecision::F32,
                StatePrecision::F32,
            ),
            (
                StatePrecision::Int8,
                StatePrecision::Int8,
                StatePrecision::Int8,
            ),
            (
                StatePrecision::Int4CardsInt8Notes,
                StatePrecision::Int4,
                StatePrecision::Int8,
            ),
            (
                StatePrecision::CompactCardsInt8,
                StatePrecision::Int8Compact,
                StatePrecision::Int8Compact,
            ),
            (
                StatePrecision::CompactCardsInt4,
                StatePrecision::Int4Compact,
                StatePrecision::Int8Compact,
            ),
        ] {
            let mut inference = runtime(&weights, precision, [false; 3]);
            assert_eq!(inference.card_layers(), card_layers);
            inference.warm_up_reviews(reviews(14), false).unwrap();
            let header = |precision| {
                if precision == StatePrecision::F32 {
                    b"ARWKVMODSTATE1".len() + 4
                } else {
                    precision::MAGIC.len() + 1 + 4
                }
            };
            // Card layer 0 has a channel mixer, layer 1 (small model only) not.
            let card_bytes = header(card)
                + stored_layer_bytes(card, true)
                + (card_layers - 1) * stored_layer_bytes(card, false);
            let note_bytes = header(note) + stored_layer_bytes(note, true);
            for state in inference.warm_up_states.card.values() {
                assert_eq!(
                    serialize_module_state(state).len(),
                    card_bytes,
                    "{weights:?} {precision:?}"
                );
            }
            for state in inference.warm_up_states.note.values() {
                assert_eq!(
                    serialize_module_state(state).len(),
                    note_bytes,
                    "{weights:?} {precision:?}"
                );
            }
            per_card_and_note.push((card_bytes, note_bytes));
        }
    }
    let small = [
        (9_002, 4_638),
        (2_987, 1_567),
        (1_963, 1_567),
        (2_545, 1_315),
        (1_521, 1_315),
    ];
    let tiny = [
        (4_638, 4_638),
        (1_567, 1_567),
        (1_055, 1_567),
        (1_315, 1_315),
        (803, 1_315),
    ];
    assert_eq!(per_card_and_note, [small, tiny, tiny, tiny].concat());
    // The default (compact int8 cards and notes) of the six-layer Tiny.
    assert_eq!(per_card_and_note[18], (1_315, 1_315));
}

/// Big (the bundled big model): stored card / note bytes per policy. Compact
/// int8 cards: 13,891 / 9,267 B; compact int4 cards: 7,747 / 9,267 B; plain
/// int4 cards + int8 notes: 9,271 / 10,283 B; int8: 15,415 / 10,283 B.
#[test]
fn rwkv_state_precision_stored_sizes_of_the_big_model() {
    if SMALL_MODEL {
        return;
    }
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut sizes = Vec::new();
    for precision in [
        StatePrecision::Int8,
        StatePrecision::Int4CardsInt8Notes,
        StatePrecision::CompactCardsInt8,
        StatePrecision::CompactCardsInt4,
    ] {
        let mut inference = runtime(&root.join(MODEL_PATH), precision, [false; 3]);
        inference.warm_up_reviews(reviews(14), false).unwrap();
        let card: HashSet<usize> = inference
            .warm_up_states
            .card
            .values()
            .map(|state| serialize_module_state(state).len())
            .collect();
        let note: HashSet<usize> = inference
            .warm_up_states
            .note
            .values()
            .map(|state| serialize_module_state(state).len())
            .collect();
        assert_eq!((card.len(), note.len()), (1, 1), "{precision:?}");
        sizes.push((
            card.into_iter().next().unwrap(),
            note.into_iter().next().unwrap(),
        ));
    }
    assert_eq!(
        sizes,
        [
            (15_415, 10_283),
            (9_271, 10_283),
            (13_891, 9_267),
            (7_747, 9_267)
        ]
    );
}

/// Low-rank card and note state (the default schedule: rank 2 below 16
/// answers, rank 4 from 16): one card answered 1 to 20 times. Against the
/// compact int8 rows each time layer replaces its `HEADS * HEAD_SIZE` rows
/// by the block (answer count, rank, then per head 2 x rank factor vectors
/// of a float16 scale and `HEAD_SIZE` codes); the stored rank is min(n, r),
/// so the state grows at answers 2, 16 and 17. Checked on every model and
/// layer plan, cards and notes.
#[test]
fn rwkv_state_precision_lowrank_sizes_grow_with_the_answer_count() {
    let rows = HEADS * HEAD_SIZE * (2 + HEAD_SIZE);
    let block = |rank: usize| 2 + HEADS * 2 * rank * (2 + HEAD_SIZE);
    for weights in models() {
        let mut card_sizes = Vec::new();
        let mut note_sizes = Vec::new();
        for answers in 1..=20 {
            let reviews: Vec<ReviewInput> = reviews(7 * answers)
                .into_iter()
                .filter(|review| review.card_id == 100)
                .collect();
            assert_eq!(reviews.len(), answers);
            let sizes = |precision| {
                let mut inference = runtime(&weights, precision, [false; 3]);
                inference.warm_up_reviews(reviews.clone(), false).unwrap();
                let card = serialize_module_state(&inference.warm_up_states.card[&100]);
                let note = inference.warm_up_states.note.values().next().unwrap();
                (
                    card.len(),
                    serialize_module_state(note).len(),
                    inference.card_layers(),
                )
            };
            let (int8_card, int8_note, card_layers) = sizes(StatePrecision::CompactCardsInt8);
            let (card, note, _) = sizes(StatePrecision::CompactLowRank(RankSchedule::DEFAULT));
            let rank = RankSchedule::DEFAULT.rank(answers as u8);
            assert_eq!(
                rank,
                [1, 2, 4][usize::from(answers >= 2) + usize::from(answers >= 16)]
            );
            let saved = rows - block(rank);
            assert_eq!(
                int8_card - card,
                card_layers * saved,
                "{weights:?} {answers}"
            );
            assert!(
                int8_note > note && (int8_note - note) % saved == 0,
                "{weights:?} {answers}"
            );
            card_sizes.push(card);
            note_sizes.push(note);
        }
        assert!(card_sizes.windows(2).all(|pair| pair[0] <= pair[1]));
        println!(
            "{weights:?}: low-rank card bytes at 1, 2, 16, 20 answers: {} {} {} {}; \
             note bytes (one card's answers) at 1, 20: {} {}",
            card_sizes[0],
            card_sizes[1],
            card_sizes[15],
            card_sizes[19],
            note_sizes[0],
            note_sizes[19]
        );
    }
}
