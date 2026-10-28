// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::io::Read;

use super::*;

#[derive(serde::Deserialize)]
struct VectorRow {
    features: Vec<f32>,
    ids: [i64; 5],
    skip: bool,
    elapsed: f32,
    fp32: Vec<f32>,
    int8: Vec<f32>,
}
#[derive(serde::Deserialize)]
struct Vectors {
    name: String,
    sweep: Vec<f32>,
    rows: Vec<VectorRow>,
}

#[test]
fn rwkv_sm40_matches_published_vectors() {
    let data = if let Ok(path) = std::env::var("ANKI_RWKV_SM40_VECTORS") {
        fs::read(path).unwrap()
    } else {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(&include_bytes!("testdata/sm40-vectors.json.gz")[..])
            .read_to_end(&mut data)
            .unwrap();
        data
    };
    check_published_vectors("sm40v2", &data);
}

#[test]
fn rwkv_sm36_matches_published_vectors() {
    let data = if let Ok(path) = std::env::var("ANKI_RWKV_SM36_VECTORS") {
        fs::read(path).unwrap()
    } else {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(&include_bytes!("testdata/sm36-vectors.json.gz")[..])
            .read_to_end(&mut data)
            .unwrap();
        data
    };
    check_published_vectors("sm36", &data);
}

#[test]
fn rwkv_sm55_matches_published_vectors() {
    let data = if let Ok(path) = std::env::var("ANKI_RWKV_SM55_VECTORS") {
        fs::read(path).unwrap()
    } else {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(&include_bytes!("testdata/sm55-vectors.json.gz")[..])
            .read_to_end(&mut data)
            .unwrap();
        data
    };
    check_published_vectors("sm55", &data);
}

#[test]
fn rwkv_sm56_matches_published_vectors() {
    let data = if let Ok(path) = std::env::var("ANKI_RWKV_SM56_VECTORS") {
        fs::read(path).unwrap()
    } else {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(&include_bytes!("testdata/sm56-vectors.json.gz")[..])
            .read_to_end(&mut data)
            .unwrap();
        data
    };
    check_published_vectors("sm56", &data);
}

fn sm72_vectors() -> Vec<u8> {
    if let Ok(path) = std::env::var("ANKI_RWKV_SM72_VECTORS") {
        fs::read(path).unwrap()
    } else {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(&include_bytes!("testdata/sm72-vectors.json.gz")[..])
            .read_to_end(&mut data)
            .unwrap();
        data
    }
}

#[test]
fn rwkv_sm72_matches_published_vectors() {
    check_published_vectors("sm72", &sm72_vectors());
}

/// The leak-free small model (SM72) was trained with the missing note, deck
/// and preset flags always zero. Setting them must not change any of its
/// outputs, while the same weights without the rule do read them.
#[test]
fn rwkv_sm72_ignores_missing_id_flags_in_every_inference_path() {
    let vectors: Vec<Vectors> = serde_json::from_slice(&sm72_vectors()).unwrap();
    let user = &vectors[0];
    let mut model = SrsModel::load(
        &PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../qt/aqt/rwkv_inference/sm72/weights.f32.bin"),
    )
    .unwrap();
    for leak_free in [false, true] {
        model.leak_free_inputs = leak_free;
        let mut states = ReviewStateMaps::default();
        let mut changed = false;
        for row in user.rows.iter().take(200) {
            assert!(row.features[13..16].iter().all(|&flag| flag == 0.0));
            let mut flagged = row.features.clone();
            flagged[13..16].fill(1.0);
            let input = vector_input(row);
            let state = states.state_ref(&input);
            let heads = model.review(&row.features, state);
            let flagged_heads = model.review(&flagged, state);
            let queries = [
                ReviewPredictionQueryRef {
                    features: &row.features,
                    state,
                },
                ReviewPredictionQueryRef {
                    features: &flagged,
                    state,
                },
            ];
            let scalar = model.review_retrievability_query_refs_scalar(&queries);
            let batched = model.review_retrievability_query_refs_batched(&queries);
            let same = heads.retrievability == flagged_heads.retrievability
                && heads.button_probabilities == flagged_heads.button_probabilities
                && predict_curve(&heads.curve, row.elapsed)
                    == predict_curve(&flagged_heads.curve, row.elapsed)
                && scalar[0] == scalar[1]
                && batched[0] == batched[1];
            if leak_free {
                assert!(same, "{} flags changed an output", user.name);
            }
            changed |= !same;
            if !row.skip {
                states.store(&input, heads.next_state);
            }
        }
        assert_eq!(changed, !leak_free);
    }
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

fn check_published_vectors(package: &str, data: &[u8]) {
    let vectors: Vec<Vectors> = serde_json::from_slice(data).unwrap();
    for format in ["f32", "int8"] {
        let mut model = SrsModel::load(&PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(format!(
            "../qt/aqt/rwkv_inference/{package}/weights.{format}.bin"
        )))
        .unwrap();
        // Runtime input rule of the leak-free model; its vectors keep the
        // flags at zero, so this only mirrors the runtime configuration.
        model.leak_free_inputs = package == "sm72";
        let mut maximum = 0.0_f32;
        for user in &vectors {
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
                let expected = if format == "f32" {
                    &row.fp32
                } else {
                    &row.int8
                };
                for (head, (&a, &b)) in actual.iter().zip(expected).enumerate() {
                    maximum = maximum.max((a - b).abs());
                    assert!(
                        (a - b).abs() <= 1e-5,
                        "{} {format} row {index} head {head}: {a} != {b}",
                        user.name
                    );
                }
                if !row.skip {
                    states.store(&input, heads.next_state);
                }
            }
        }
        println!(
            "{package} {format} published vectors: maximum absolute probability error {maximum}"
        );
    }
}

#[test]
fn rwkv_sm40_spec_matches_compiled_architecture() {
    for source in [
        include_str!("../../../qt/aqt/rwkv_inference/sm40v2/architecture.json"),
        include_str!("../../../qt/aqt/rwkv_inference/sm36/architecture.json"),
        include_str!("../../../qt/aqt/rwkv_inference/sm55/architecture.json"),
        include_str!("../../../qt/aqt/rwkv_inference/sm56/architecture.json"),
        include_str!("../../../qt/aqt/rwkv_inference/sm72/architecture.json"),
    ] {
        check_compiled_architecture(source);
    }
}

fn check_compiled_architecture(source: &str) {
    let spec: serde_json::Value = serde_json::from_str(source).unwrap();
    assert_eq!(spec["d_model"], D_MODEL);
    assert_eq!(spec["head_fc_dim"], HEAD_DIM);
    assert_eq!(spec["num_curves"], NUM_CURVES);
    for id in 0..5 {
        assert_eq!(spec["streams"][id]["n_layers"], MODULE_LAYERS[id]);
        assert_eq!(spec["streams"][id]["head_size"], HEAD_SIZE);
        for layer in 0..MODULE_LAYERS[id] {
            let stripped = spec["streams"][id]["channel_mixer_stripped_layers"]
                .as_array()
                .unwrap()
                .contains(&serde_json::json!(layer));
            assert_eq!(channel_mixer_present(id, layer), !stripped);
        }
    }
    let steps: Vec<(usize, usize)> = serde_json::from_value(spec["layer_steps"].clone()).unwrap();
    assert_eq!(steps, LAYER_STEPS);
}
