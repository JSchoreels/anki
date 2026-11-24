// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Manual benchmark of the runtimes behind the Big / Small / Tiny model
//! sizes: answer replay (reviews/s), queue scoring, full predictions and the
//! stored state per card, on a synthetic history of 4,000 cards, 2,000
//! notes and 32,000 answers (sizes: `ANKI_RWKV_SIZE_BENCH_CARDS`,
//! `ANKI_RWKV_SIZE_BENCH_ROUNDS`) at the compact int8 card and note state
//! (`compact_cards_int8`) unless `ANKI_RWKV_SIZE_BENCH_PRECISION` says
//! otherwise.
//!
//! ```sh
//! ANKI_RWKV_SIZE_BENCH_RUNTIMES=small,small-pair,small-instant \
//!   cargo test --release rwkv_model_size_benchmark -- --ignored --nocapture
//! ```
//!
//! `ANKI_RWKV_SIZE_BENCH_RUNTIMES` picks runtimes (default: all that load),
//! `ANKI_RWKV_SIZE_BENCH_SAMPLES` the timed samples per runtime (default 5,
//! after one untimed warm-up sample), `ANKI_RWKV_SIZE_BENCH_PRECISION` the
//! state precision (default `compact_cards_int8`). A runtime whose model key the engine
//! does not know is skipped, so the same file runs on older sources.

use std::path::PathBuf;
use std::time::Instant;

use super::*;

const FULL_PREDICTIONS: usize = 500;

/// Cards (default 4,000; `ANKI_RWKV_SIZE_BENCH_CARDS`) and answer rounds
/// (default 8; `ANKI_RWKV_SIZE_BENCH_ROUNDS`, 24 takes every card past the
/// low-rank state's rank-4 tier at 16 answers).
fn shape() -> (i64, i64) {
    let read = |name: &str, default: i64| {
        std::env::var(name)
            .ok()
            .and_then(|value| value.parse::<i64>().ok())
            .unwrap_or(default)
            .max(1)
    };
    (
        read("ANKI_RWKV_SIZE_BENCH_CARDS", 4_000).max(FULL_PREDICTIONS as i64),
        read("ANKI_RWKV_SIZE_BENCH_ROUNDS", 8),
    )
}

fn weights(folder: &str) -> PathBuf {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../qt/aqt/rwkv_inference");
    if folder == "legacy" {
        root.join("RWKV_trained_on_5000_10000.bin")
    } else {
        root.join(folder).join("weights.f32.bin")
    }
}

/// `(name, main model, instant model beside it)`: each size's runtimes of
/// the RWKV family release (the family wiring (SM114)), plus the default
/// small model (SM63) alone, which no size runs any more. Tiny is the
/// six-layer Tiny (SM130) since the 2026-11 release candidate; the earlier
/// Tiny runtimes (the Tiny two-head model (SM106), the longer cool-down for
/// Tiny (SM115)) stay for comparison.
const RUNTIMES: [(&str, &str, Option<&str>); 13] = [
    ("big", "legacy", None),
    ("big-pair", "legacy", Some("sm99")),
    ("big-instant", "sm99", None),
    ("small", "sm118", None),
    ("small-pair", "sm118", Some("sm113")),
    ("small-instant", "sm113", None),
    ("tiny", "sm130th", None),
    ("tiny-pair", "sm130th", Some("sm130io")),
    ("tiny-instant", "sm130io", None),
    ("tiny-sm106", "sm106", None),
    ("tiny-sm106-pair", "sm106", Some("sm115")),
    ("tiny-sm115-instant", "sm115", None),
    ("sm63", "sm72e25", None),
];

fn load(main: &str, instant: Option<&str>) -> Option<RwkvRuntime> {
    let precision = StatePrecision::parse(
        &std::env::var("ANKI_RWKV_SIZE_BENCH_PRECISION").unwrap_or("compact_cards_int8".into()),
    )
    .unwrap();
    let main_kind = ModelKind::parse(main).ok()?;
    Some(match instant {
        None => RwkvRuntime::load_with_precision(weights(main), 0.9, 36_500, precision, main_kind)
            .unwrap(),
        Some(instant) => RwkvRuntime::load_paired(
            weights(main),
            weights(instant),
            0.9,
            36_500,
            precision,
            main_kind,
            ModelKind::parse(instant).ok()?,
        )
        .unwrap(),
    })
}

/// Every card answered once per round, rounds three days apart, in time
/// order; two cards per note, three decks, one preset.
fn answers() -> Vec<ReviewInput> {
    let (cards, rounds) = shape();
    let mut state = 0x9e37_79b9_7f4a_7c15_u64;
    let mut next = move || {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        state >> 33
    };
    let mut out = Vec::with_capacity((cards * rounds) as usize);
    for round in 0..rounds {
        for card in 0..cards {
            let elapsed_days = if round == 0 { 0 } else { 3 };
            out.push(ReviewInput {
                card_id: 10_000 + card,
                note_id: Some(5_000 + card / 2),
                deck_id: Some(1 + card % 3),
                preset_id: Some(1),
                is_query: false,
                ease: Some((1 + next() % 4) as u8),
                duration_millis: Some(1_000 + (next() % 9_000) as i64),
                card_type: Some(if round == 0 { 0 } else { 2 }),
                day_offset: Some(1_000 + 3 * round),
                current_elapsed_days: Some(elapsed_days),
                current_elapsed_seconds: Some(elapsed_days * 86_400 + (next() % 600) as i64),
                target_retentions: [Some(0.9); 4],
                enforce_grade_order: true,
            });
        }
    }
    out
}

fn queries() -> Vec<ReviewInput> {
    let (cards, rounds) = shape();
    (0..cards)
        .map(|card| ReviewInput {
            card_id: 10_000 + card,
            note_id: Some(5_000 + card / 2),
            deck_id: Some(1 + card % 3),
            preset_id: Some(1),
            is_query: true,
            ease: None,
            duration_millis: None,
            card_type: Some(2),
            day_offset: Some(1_000 + 3 * rounds),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(3 * 86_400),
            target_retentions: [Some(0.9); 4],
            enforce_grade_order: true,
        })
        .collect()
}

fn median(values: &mut [f64]) -> f64 {
    values.sort_by(f64::total_cmp);
    let middle = values.len() / 2;
    if values.len().is_multiple_of(2) {
        (values[middle - 1] + values[middle]) / 2.0
    } else {
        values[middle]
    }
}

#[test]
#[ignore]
fn rwkv_model_size_benchmark() {
    let selected = std::env::var("ANKI_RWKV_SIZE_BENCH_RUNTIMES").ok();
    let samples = std::env::var("ANKI_RWKV_SIZE_BENCH_SAMPLES")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(5)
        .max(1);
    let answers = answers();
    let queries = queries();
    if let Ok(threads) = std::env::var("RAYON_NUM_THREADS") {
        println!("rayon_num_threads={threads}");
    }
    let (cards, rounds) = shape();
    println!(
        "answers={} cards={cards} rounds={rounds} samples={samples}",
        answers.len()
    );
    for (name, main, instant) in RUNTIMES {
        if selected
            .as_deref()
            .is_some_and(|selected| !selected.split(',').any(|item| item == name))
        {
            continue;
        }
        let mut warm_up = Vec::new();
        let mut scoring = Vec::new();
        let mut full = Vec::new();
        let mut state_bytes = (0, 0);
        let mut checksum = 0.0_f64;
        for sample in 0..=samples {
            let Some(mut runtime) = load(main, instant) else {
                println!("{name}: skipped (model unknown to this engine)");
                break;
            };
            let started = Instant::now();
            runtime.warm_up_reviews(answers.clone(), false).unwrap();
            let warm_up_ms = started.elapsed().as_secs_f64() * 1000.0;
            let started = Instant::now();
            let recall = runtime
                .predict_retrievability_many_from_warm_up(queries.clone())
                .unwrap();
            let scoring_ms = started.elapsed().as_secs_f64() * 1000.0;
            let requests: Vec<_> = queries[..FULL_PREDICTIONS]
                .iter()
                .map(|input| ReviewPredictionRequest {
                    input: input.clone(),
                    state: runtime.warm_up_state(input).unwrap(),
                })
                .collect();
            let started = Instant::now();
            let outputs = runtime.predict_many(requests).unwrap();
            let full_ms = started.elapsed().as_secs_f64() * 1000.0;
            checksum = recall.iter().map(|&r| f64::from(r)).sum::<f64>()
                + outputs
                    .iter()
                    .map(|output| f64::from(output.retrievability))
                    .sum::<f64>();
            let snapshot = runtime.warm_up_snapshot().unwrap();
            let bytes = |states: &[(i64, Vec<u8>)]| states.iter().map(|(_, s)| s.len()).sum();
            state_bytes = (bytes(&snapshot.card_states), bytes(&snapshot.note_states));
            if sample > 0 {
                warm_up.push(warm_up_ms);
                scoring.push(scoring_ms);
                full.push(full_ms);
            }
        }
        if warm_up.is_empty() {
            continue;
        }
        let warm_up_ms = median(&mut warm_up);
        println!(
            "{name}: warm_up_ms={warm_up_ms:.1} reviews_per_s={:.0} min_warm_up_ms={:.1} \
             scoring_ms={:.1} full_{FULL_PREDICTIONS}_ms={:.1} card_state_bytes={} \
             note_state_bytes={} per_card_and_note={:.0} checksum={checksum:.6}",
            answers.len() as f64 / (warm_up_ms / 1000.0),
            warm_up[0],
            median(&mut scoring),
            median(&mut full),
            state_bytes.0,
            state_bytes.1,
            (state_bytes.0 as f64 + 2.0 * state_bytes.1 as f64) / cards as f64,
        );
    }
}
