// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The small model against its package's golden vectors (sm40 testvectors:
//! every row's outputs from JSchoreels' NumPy reference, within 3e-6 of
//! PyTorch), as INTEGRATION.md section 10 asks: within 1e-5.
//!
//! ANKI_RWKV_SMALL_MODEL: weights.f32.bin, or weights.int8.bin dequantized;
//! ANKI_RWKV_SMALL_WEIGHTS: `int8` to compare with the int8 outputs;
//! ANKI_RWKV_SMALL_VECTORS: vectors.bin (kuma3:
//! C:\dojo\work\sm40\make_vectors.py).

use std::collections::HashMap;

use super::*;

fn env_path(name: &str) -> PathBuf {
    PathBuf::from(std::env::var(name).unwrap_or_else(|_| panic!("{name} is not set")))
}

#[test]
#[ignore = "needs the sm40 package files"]
fn small_model_matches_reference_vectors() {
    if !SMALL {
        return;
    }
    let model = SrsModel::load(&env_path("ANKI_RWKV_SMALL_MODEL")).unwrap();
    let int8 = std::env::var("ANKI_RWKV_SMALL_WEIGHTS").as_deref() == Ok("int8");
    let data = fs::read(env_path("ANKI_RWKV_SMALL_VECTORS")).unwrap();
    let mut cursor = Cursor::new(&data);
    cursor.expect_magic(b"SM40VEC1").unwrap();
    // largest difference: curve at the label time, curve sweep, instant,
    // ratings, and the two query paths against the full pass
    let mut worst = [0.0_f32; 5];
    let mut total_rows = 0;
    for _ in 0..cursor.u32().unwrap() {
        let rows = cursor.u32().unwrap();
        let sweep: Vec<f32> = (0..6).map(|_| cursor.f32().unwrap()).collect();
        let mut states: [HashMap<i64, ModuleState>; 5] = Default::default();
        for _ in 0..rows {
            let features: Vec<f32> = (0..CARD_FEATURES).map(|_| cursor.f32().unwrap()).collect();
            let ids: [i64; 5] = std::array::from_fn(|_| cursor.i64().unwrap());
            let skip = cursor.u8().unwrap() == 1;
            let label_seconds = cursor.f32().unwrap();
            let expected: [Vec<f32>; 2] =
                std::array::from_fn(|_| (0..12).map(|_| cursor.f32().unwrap()).collect());
            let expected = &expected[int8 as usize];

            let state = SrsStateRef {
                card: states[0].get(&ids[0]),
                deck: states[1].get(&ids[1]),
                note: states[2].get(&ids[2]),
                preset: states[3].get(&ids[3]),
                global: states[4].get(&ids[4]),
            };
            let heads = model.review_features(&features, state);
            let mut got = vec![predict_curve(&heads.curve, label_seconds)];
            got.extend(
                sweep
                    .iter()
                    .map(|seconds| predict_curve(&heads.curve, *seconds)),
            );
            got.push(heads.retrievability.clamp(1e-5, 1.0 - 1e-5));
            got.extend(heads.button_probabilities);
            for (index, (got, expected)) in got.iter().zip(expected).enumerate() {
                let kind = match index {
                    0 => 0,
                    1..=6 => 1,
                    7 => 2,
                    _ => 3,
                };
                worst[kind] = worst[kind].max((got - expected).abs());
            }

            let query = [ReviewPredictionQueryRef {
                features: &features,
                state,
            }];
            for retrievability in [
                model.review_retrievability_query_refs_scalar(&query)[0],
                model.review_retrievability_query_refs_batched(&query)[0],
            ] {
                worst[4] = worst[4].max((retrievability - heads.retrievability).abs());
            }

            if !skip {
                let SrsState {
                    card,
                    deck,
                    note,
                    preset,
                    global,
                } = heads.next_state;
                for (module, state) in [card, deck, note, preset, global].into_iter().enumerate() {
                    states[module].insert(ids[module], state);
                }
            }
        }
        total_rows += rows;
    }
    cursor.expect_end().unwrap();
    eprintln!(
        "{total_rows} rows ({}): max |d| curve {:.1e}, sweep {:.1e}, instant {:.1e}, ratings {:.1e}, query paths {:.1e}",
        if int8 { "int8" } else { "fp32" },
        worst[0],
        worst[1],
        worst[2],
        worst[3],
        worst[4]
    );
    assert!(worst.iter().all(|value| *value < 1e-5), "{worst:?}");
}
