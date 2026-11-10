// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The bundled big model ("Legacy - Bigger Model"): the big model (1
//! leak-free epoch), checked on the big engine against vectors from the
//! training code. Compiled into whichever engine module runs the big
//! architecture.

use std::io::Read;

use super::*;
use crate::rwkv::ModelKind;

#[derive(serde::Deserialize)]
struct VectorRow {
    features: Vec<f32>,
    ids: [i64; 5],
    skip: bool,
    elapsed: f32,
    fp32: Vec<f32>,
}

#[derive(serde::Deserialize)]
struct Vectors {
    name: String,
    sweep: Vec<f32>,
    rows: Vec<VectorRow>,
}

/// PyTorch outputs of the big model (1 leak-free epoch) on two users' full
/// histories (normal inputs, deleted cards: neighbour), first 500 rows per
/// user; `ANKI_RWKV_LEGACY_VECTORS` points the test at the full vectors.
fn legacy_vectors() -> Vec<Vectors> {
    let data = if let Ok(path) = std::env::var("ANKI_RWKV_LEGACY_VECTORS") {
        fs::read(path).unwrap()
    } else {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(&include_bytes!("testdata/legacy-vectors.json.gz")[..])
            .read_to_end(&mut data)
            .unwrap();
        data
    };
    serde_json::from_slice(&data).unwrap()
}

fn legacy_model() -> SrsModel {
    let mut model = SrsModel::load(
        &PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin"),
    )
    .unwrap();
    model.leak_free_inputs = ModelKind::Legacy.leak_free_inputs();
    model
}

fn vector_input(row: &VectorRow) -> ReviewInput {
    ReviewInput {
        card_id: row.ids[0],
        deck_id: Some(row.ids[1]),
        note_id: Some(row.ids[2]),
        preset_id: Some(row.ids[3]),
        is_query: row.skip,
        ease: None,
        duration_millis: None,
        card_type: None,
        day_offset: None,
        current_elapsed_days: None,
        current_elapsed_seconds: None,
        target_retentions: [None; 4],
        enforce_grade_order: true,
    }
}

/// Instant recall, the four button probabilities, curve recall at the row's
/// elapsed time and at the sweep times match the training code. The curve is
/// the trainer's raw forgetting curve: the runtime ignores the big model's
/// "ahead" logit residual (`predict_curve`).
#[test]
fn rwkv_legacy_matches_published_vectors() {
    let model = legacy_model();
    let mut maximum = 0.0_f32;
    let mut rows = 0;
    for user in legacy_vectors() {
        let mut states = ReviewStateMaps::default();
        for (index, row) in user.rows.iter().enumerate() {
            let input = vector_input(row);
            let heads = model.review(&row.features, states.state_ref(&input));
            let mut actual = vec![heads.retrievability.clamp(1e-5, 1.0 - 1e-5)];
            actual.extend(heads.button_probabilities);
            actual.push(predict_curve(&heads.curve, row.elapsed));
            actual.extend(
                user.sweep
                    .iter()
                    .map(|&elapsed| predict_curve(&heads.curve, elapsed)),
            );
            assert_eq!(actual.len(), row.fp32.len());
            for (head, (&a, &b)) in actual.iter().zip(&row.fp32).enumerate() {
                maximum = maximum.max((a - b).abs());
                assert!(
                    (a - b).abs() <= 1e-5,
                    "{} row {index} head {head}: {a} != {b}",
                    user.name
                );
            }
            if !row.skip {
                states.store(&input, heads.next_state);
            }
            rows += 1;
        }
    }
    println!("legacy published vectors: {rows} rows, maximum absolute probability error {maximum}");
}

/// The big model (1 leak-free epoch) was trained with the missing note, deck
/// and preset flags always zero: the runtime feeds it that rule, so setting
/// them changes no output.
#[test]
fn rwkv_legacy_ignores_missing_id_flags() {
    assert!(ModelKind::Legacy.leak_free_inputs());
    let model = legacy_model();
    let user = legacy_vectors().remove(0);
    let mut states = ReviewStateMaps::default();
    for row in user.rows.iter().take(200) {
        assert!(row.features[13..16].iter().all(|&flag| flag == 0.0));
        let mut flagged = row.features.clone();
        flagged[13..16].fill(1.0);
        let input = vector_input(row);
        let state = states.state_ref(&input);
        let heads = model.review(&row.features, state);
        let flagged_heads = model.review(&flagged, state);
        assert_eq!(heads.retrievability, flagged_heads.retrievability);
        assert_eq!(
            heads.button_probabilities,
            flagged_heads.button_probabilities
        );
        assert_eq!(
            predict_curve(&heads.curve, row.elapsed),
            predict_curve(&flagged_heads.curve, row.elapsed)
        );
        if !row.skip {
            states.store(&input, heads.next_state);
        }
    }
}
