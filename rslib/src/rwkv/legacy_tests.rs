// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The bundled big model (the Big size, profile key `legacy`): the show-time
//! big model (SM91s2) since 2026-11-11 (before: the big model (1 leak-free
//! epoch)), checked on the big engine against vectors from the training code,
//! and the Big choice's RWKV-Instant model, the show-time instant-only big
//! model (SM99), checked the same way. Compiled into whichever engine module
//! runs the big architecture.

use std::io::Read;

use super::*;
use crate::rwkv::ModelKind;

#[derive(serde::Deserialize)]
struct VectorRow {
    features: Vec<f32>,
    ids: [i64; 5],
    skip: bool,
    elapsed: f32,
    /// The row's own interval inputs (elapsed seconds, elapsed days).
    interval: [f64; 2],
    fp32: Vec<f32>,
}

#[derive(serde::Deserialize)]
struct Vectors {
    name: String,
    sweep: Vec<f32>,
    rows: Vec<VectorRow>,
}

/// PyTorch outputs of the show-time big model (SM91s2) on two users' full
/// histories (show-time inputs, the inputs it was trained on and the ones
/// RWKV-Instant gives it; deleted cards: neighbour), first 500 rows per user;
/// `ANKI_RWKV_LEGACY_VECTORS` points the test at full vectors (show-time or
/// normal inputs).
fn legacy_vectors() -> Vec<Vectors> {
    read_vectors(
        "ANKI_RWKV_LEGACY_VECTORS",
        include_bytes!("testdata/legacy-vectors.json.gz"),
    )
}

/// PyTorch outputs of the show-time instant-only big model (SM99), made the
/// same way (show-time inputs, first 500 rows per user);
/// `ANKI_RWKV_SM99_VECTORS` points the test at full vectors.
fn sm99_vectors() -> Vec<Vectors> {
    read_vectors(
        "ANKI_RWKV_SM99_VECTORS",
        include_bytes!("testdata/sm99-vectors.json.gz"),
    )
}

fn read_vectors(variable: &str, bundled: &[u8]) -> Vec<Vectors> {
    let data = if let Ok(path) = std::env::var(variable) {
        fs::read(path).unwrap()
    } else {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(bundled)
            .read_to_end(&mut data)
            .unwrap();
        data
    };
    serde_json::from_slice(&data).unwrap()
}

fn big_model(file: &str, kind: ModelKind) -> SrsModel {
    let mut model = SrsModel::load(
        &PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../qt/aqt/rwkv_inference")
            .join(file),
    )
    .unwrap();
    model.leak_free_inputs = kind.leak_free_inputs();
    model
}

fn legacy_model() -> SrsModel {
    big_model("RWKV_trained_on_5000_10000.bin", ModelKind::Legacy)
}

fn sm99_model() -> SrsModel {
    big_model("sm99/weights.f32.bin", ModelKind::Sm99)
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
    let (rows, maximum, _) = check_published_vectors(&legacy_model(), legacy_vectors());
    println!("legacy published vectors: {rows} rows, maximum absolute probability error {maximum}");
}

/// The show-time instant-only big model (SM99) on the big engine: instant
/// recall and the four button probabilities (the outputs it serves) and the
/// untrained curve (never served, checked as engine parity only).
#[test]
fn rwkv_sm99_matches_published_vectors() {
    assert!(ModelKind::Sm99.instant_only());
    let (rows, maximum, instant) = check_published_vectors(&sm99_model(), sm99_vectors());
    println!(
        "sm99 published vectors: {rows} rows, maximum absolute probability error {maximum} (instant recall and buttons {instant})"
    );
}

/// `(rows, maximum error over every output, maximum over instant recall and
/// the four button probabilities)`.
fn check_published_vectors(model: &SrsModel, vectors: Vec<Vectors>) -> (usize, f32, f32) {
    let mut maximum = 0.0_f32;
    let mut instant = 0.0_f32;
    let mut rows = 0;
    for user in vectors {
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
                if head < 5 {
                    instant = instant.max((a - b).abs());
                }
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
    (rows, maximum, instant)
}

/// The show-time big model (SM91s2), like the big model (1 leak-free epoch) it
/// was trained from, was trained with the missing note, deck and preset flags
/// always zero: the runtime feeds it that rule, so setting
/// them changes no output.
#[test]
fn rwkv_legacy_ignores_missing_id_flags() {
    assert!(ModelKind::Legacy.leak_free_inputs());
    check_ignores_missing_id_flags(&legacy_model(), legacy_vectors().remove(0));
}

/// The show-time instant-only big model (SM99) has the same input rule.
#[test]
fn rwkv_sm99_ignores_missing_id_flags() {
    assert!(ModelKind::Sm99.leak_free_inputs());
    check_ignores_missing_id_flags(&sm99_model(), sm99_vectors().remove(0));
}

fn check_ignores_missing_id_flags(model: &SrsModel, user: Vectors) {
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

/// The show-time big model (SM91s2) was trained on show-time intervals: a
/// query row's interval runs from the card's previous answer to the moment the
/// card is shown, and its cumulative interval is the card's committed
/// answer-to-answer intervals plus that interval; a committed answer keeps
/// the answer-to-answer interval. The native feature builder fed each row's
/// own interval reproduces the published interval inputs (features 0..8) of
/// every query and answer row.
#[test]
fn rwkv_legacy_feature_builder_matches_show_time_intervals() {
    let mut maximum = 0.0_f32;
    let mut queries = 0;
    for user in legacy_vectors() {
        let mut features = FeatureState::default();
        for (index, row) in user.rows.iter().enumerate() {
            let [seconds, days] = row.interval;
            let mut input = vector_input(row);
            input.current_elapsed_seconds = Some(seconds as i64);
            input.current_elapsed_days = Some(days as i64);
            let actual = features.features_for(&input);
            for (feature, (&a, &b)) in actual[..8].iter().zip(&row.features[..8]).enumerate() {
                maximum = maximum.max((a - b).abs());
                assert!(
                    (a - b).abs() <= 1e-5,
                    "{} row {index} feature {feature}: {a} != {b}",
                    user.name
                );
            }
            if row.skip {
                queries += 1;
            } else {
                features.store_review(&input);
            }
        }
    }
    assert!(queries > 0);
    println!("legacy interval features: {queries} queries, maximum absolute error {maximum}");
}

#[derive(serde::Deserialize)]
struct CompactVectorRow {
    features: Vec<f32>,
    ids: [i64; 5],
    skip: bool,
    elapsed: f32,
    fp32: Vec<f32>,
    // `compact-state-big-vectors.json.gz`
    #[serde(default)]
    compact_int8: Vec<f32>,
    #[serde(default)]
    compact_int4: Vec<f32>,
    // `lowrank-state-big-vectors.json.gz`
    #[serde(default)]
    t24: Vec<f32>,
    #[serde(default)]
    t24_q6: Vec<f32>,
    /// (label_y, has_label, is_query) (`lowrank-state-big-vectors.json.gz`).
    #[serde(default)]
    label: Vec<f32>,
}

impl CompactVectorRow {
    fn outputs(&self, label: &str) -> &[f32] {
        match label {
            "fp32" => &self.fp32,
            "compact_int8" => &self.compact_int8,
            "compact_int4" => &self.compact_int4,
            "t24" => &self.t24,
            "t24_q6" => &self.t24_q6,
            other => panic!("unknown format {other}"),
        }
    }
}

#[derive(serde::Deserialize)]
struct CompactVectors {
    name: String,
    sweep: Vec<f32>,
    rows: Vec<CompactVectorRow>,
}

fn compact_input(row: &CompactVectorRow) -> ReviewInput {
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

fn compact_outputs(heads: &ReviewHeads, elapsed: f32, sweep: &[f32]) -> Vec<f32> {
    let mut out = vec![heads.retrievability.clamp(1e-5, 1.0 - 1e-5)];
    out.extend(heads.button_probabilities);
    out.push(predict_curve(&heads.curve, elapsed));
    out.extend(
        sweep
            .iter()
            .map(|&elapsed| predict_curve(&heads.curve, elapsed)),
    );
    out
}

/// The training repository scorer's outputs of the show-time big model
/// (SM91s2, float32 weights) with the card and note state stored rounded
/// after every answer, at float32, `compact_cards_int8` and
/// `compact_cards_int4`, on the two SYNTHETIC users of
/// `synthetic_testvector_users.py` (show-time inputs, first 500 rows per user;
/// made by `qt/tools/rwkv_compact_state_big_vectors.py`).
/// `ANKI_RWKV_COMPACT_BIG_VECTORS` points the test at the full vectors.
///
/// Float32 state must match within 1e-5. A rounded state is chaotic: a float
/// summation-order difference of one ulp flips an occasional code, and the
/// flip carries forward. So the compact gate is measured against that floor:
/// the same model run on inputs moved by one ulp. The mean difference from
/// the scorer must stay within three times the mean difference the one-ulp
/// change causes, and the rounding's effect (the mean distance from the float32
/// state) must agree with the scorer's within 5%: a missing or extra rounding
/// step changes that effect.
#[test]
fn rwkv_legacy_compact_state_matches_the_scorer() {
    let bytes = match std::env::var("ANKI_RWKV_COMPACT_BIG_VECTORS") {
        Ok(path) => fs::read(path).unwrap(),
        Err(_) => include_bytes!("testdata/compact-state-big-vectors.json.gz").to_vec(),
    };
    state_matches_the_scorer(
        &bytes,
        &[
            ("fp32", StatePrecision::F32),
            ("compact_int8", StatePrecision::CompactCardsInt8),
            ("compact_int4", StatePrecision::CompactCardsInt4),
        ],
    );
}

/// The low-rank state's twin of `rwkv_legacy_compact_state_matches_the_scorer`:
/// the scorer's outputs of the show-time big model (SM91s2) at float32,
/// `compact_lowrank_int8` (`w8+lr2.16.4q8x8`) and `compact_lowrank_int6`
/// (`w8+lr2.16.4q6x8`, Big's profile default since the 6-bit factors
/// (SM162)), on the same two SYNTHETIC users (first 500 rows per user, which
/// take cards and notes past 16 answers, so rank 4 is covered; made by
/// `qt/tools/rwkv_lowrank_state_big_vectors.py`).
/// `ANKI_RWKV_LOWRANK_BIG_VECTORS` points the test at other vectors.
///
/// Float32 and the rounding-effect gate as there. The one-ulp floor does not
/// apply: the runtime's factored solve runs in float64, the scorer's in
/// float32, and that difference (not an input ulp) moves a factor code now
/// and then; a coarse int6 grid absorbs a one-ulp input change almost
/// always, so its floor is near zero. The low-rank formats take the
/// low-rank states end-to-end rule (SM151) instead: per user, the runtime's
/// curve LogLoss (non-query labelled rows, curve at the row's elapsed time)
/// and instant LogLoss (labelled query rows) within 3e-4 of the scorer's,
/// and the mean signed difference within 1e-4.
#[test]
fn rwkv_legacy_lowrank_state_matches_the_scorer() {
    let bytes = match std::env::var("ANKI_RWKV_LOWRANK_BIG_VECTORS") {
        Ok(path) => fs::read(path).unwrap(),
        Err(_) => include_bytes!("testdata/lowrank-state-big-vectors.json.gz").to_vec(),
    };
    state_matches_the_scorer(
        &bytes,
        &[
            ("fp32", StatePrecision::F32),
            ("t24", StatePrecision::CompactLowRank(RankSchedule::DEFAULT)),
            (
                "t24_q6",
                StatePrecision::CompactLowRankInt6(RankSchedule::DEFAULT),
            ),
        ],
    );
}

fn state_matches_the_scorer(bytes: &[u8], formats: &[(&str, StatePrecision)]) {
    let data = {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(bytes)
            .read_to_end(&mut data)
            .unwrap();
        data
    };
    let vectors: Vec<CompactVectors> = serde_json::from_slice(&data).unwrap();
    for &(label, precision) in formats {
        let mut model = legacy_model();
        let (card, note) = precision.module_precisions();
        model.modules[0].precision = card;
        model.modules[2].precision = note;
        let (mut count, mut maximum, mut above) = (0usize, 0.0_f32, 0usize);
        let (mut scorer, mut ulp, mut effect, mut scorer_effect) = (0.0_f64, 0.0, 0.0, 0.0);
        let lowrank = matches!(
            precision,
            StatePrecision::CompactLowRank(_) | StatePrecision::CompactLowRankInt6(_)
        );
        // Per user and head (curve, instant): runtime minus scorer LogLoss.
        let mut log_loss_differences = [Vec::new(), Vec::new()];
        for user in &vectors {
            // (label, runtime, scorer) per head
            let mut labelled: [Vec<(f32, f32, f32)>; 2] = [Vec::new(), Vec::new()];
            let mut states = ReviewStateMaps::default();
            let mut moved_states = ReviewStateMaps::default();
            for (index, row) in user.rows.iter().enumerate() {
                let input = compact_input(row);
                let heads = model.review(&row.features, states.state_ref(&input));
                let moved_features: Vec<f32> = row
                    .features
                    .iter()
                    .map(|&value| {
                        if value == 0.0 {
                            value
                        } else {
                            f32::from_bits(value.to_bits() + 1)
                        }
                    })
                    .collect();
                let rounded = label != "fp32";
                let actual = compact_outputs(&heads, row.elapsed, &user.sweep);
                let moved_heads =
                    rounded.then(|| model.review(&moved_features, moved_states.state_ref(&input)));
                let moved = moved_heads.as_ref().map_or_else(
                    || actual.clone(),
                    |moved| compact_outputs(moved, row.elapsed, &user.sweep),
                );
                let expected = row.outputs(label);
                assert_eq!(actual.len(), expected.len());
                if lowrank && row.label[1] > 0.0 {
                    // [p_imm, 4 buttons, curve at the row's elapsed time, ...]
                    let (head, output) = if row.label[2] > 0.0 { (1, 0) } else { (0, 5) };
                    labelled[head].push((row.label[0], actual[output], expected[output]));
                }
                for (head, &a) in actual.iter().enumerate() {
                    let error = (a - expected[head]).abs();
                    if label == "fp32" {
                        assert!(
                            error <= 1e-5,
                            "{} row {index} head {head}: {a} != {}",
                            user.name,
                            expected[head]
                        );
                    }
                    count += 1;
                    maximum = maximum.max(error);
                    above += usize::from(error > 1e-4);
                    scorer += f64::from(error);
                    ulp += f64::from((a - moved[head]).abs());
                    effect += f64::from((a - row.fp32[head]).abs());
                    scorer_effect += f64::from((expected[head] - row.fp32[head]).abs());
                }
                if !row.skip {
                    states.store(&input, heads.next_state);
                    if let Some(moved) = moved_heads {
                        moved_states.store(&input, moved.next_state);
                    }
                }
            }
            for (head, rows) in labelled.iter().enumerate() {
                if rows.is_empty() {
                    continue;
                }
                let log_loss = |pick: fn(&(f32, f32, f32)) -> f32| {
                    rows.iter()
                        .map(|row| {
                            let (y, p) = (
                                f64::from(row.0),
                                f64::from(pick(row)).clamp(1e-7, 1.0 - 1e-7),
                            );
                            -(y * p.ln() + (1.0 - y) * (1.0 - p).ln())
                        })
                        .sum::<f64>()
                        / rows.len() as f64
                };
                log_loss_differences[head].push(log_loss(|row| row.1) - log_loss(|row| row.2));
            }
        }
        if lowrank {
            for (head, differences) in ["curve", "instant"].iter().zip(&log_loss_differences) {
                assert_eq!(differences.len(), vectors.len(), "{label} {head}");
                let worst = differences.iter().fold(0.0_f64, |m, d| m.max(d.abs()));
                let mean = differences.iter().sum::<f64>() / differences.len() as f64;
                let per_user: Vec<String> =
                    differences.iter().map(|d| format!("{d:+.2e}")).collect();
                println!(
                    "{label} {head} LogLoss, runtime minus scorer: per user {}, max abs {worst:.2e}, mean {mean:+.2e}",
                    per_user.join(" ")
                );
                assert!(
                    worst <= 3e-4 && mean.abs() <= 1e-4,
                    "{label} {head}: {differences:?}"
                );
            }
        }
        let n = count as f64;
        let (scorer, ulp, effect, scorer_effect) =
            (scorer / n, ulp / n, effect / n, scorer_effect / n);
        println!(
            "{label}: {count} outputs; vs the scorer: mean {scorer:.3e}, maximum {maximum:.3e}, {above} above 1e-4; \
             one-ulp input change: mean {ulp:.3e}; mean distance from float32 state: {effect:.3e} (scorer {scorer_effect:.3e})"
        );
        if label != "fp32" {
            if !lowrank {
                assert!(scorer <= 3.0 * ulp, "{label}: {scorer} > 3 x {ulp}");
            }
            assert!(
                (effect - scorer_effect).abs() <= 0.05 * scorer_effect,
                "{label}: rounding effect {effect} vs the scorer's {scorer_effect}"
            );
        }
    }
}

#[derive(serde::Deserialize)]
struct EndToEndRow {
    features: Vec<f32>,
    ids: [i64; 5],
    skip: bool,
    elapsed: f32,
}

#[derive(serde::Deserialize)]
struct EndToEndUser {
    name: String,
    rows: Vec<EndToEndRow>,
}

/// End-to-end check of the low-rank card and note state (SM151) on benchmark
/// users: `ANKI_RWKV_LOWRANK_E2E_VECTORS` is a file written by the training
/// repository's `artifacts/small-model/sm151/e2e_big_vectors.py` (features,
/// ids and the scorer's outputs per row; benchmark rows, never bundled),
/// `ANKI_RWKV_LOWRANK_E2E_MODEL` is `legacy` (the show-time big model
/// (SM91s2)) or `sm99` (the show-time instant-only big model (SM99)), and
/// `ANKI_RWKV_LOWRANK_E2E_OUT` receives the runtime's instant recall and curve
/// recall at the row's elapsed time per row and state format; the comparison
/// with the scorer is done by `e2e_compare.py` next to that script.
#[test]
#[ignore]
fn rwkv_big_lowrank_state_end_to_end() {
    let vectors: Vec<EndToEndUser> = serde_json::from_slice(
        &fs::read(std::env::var("ANKI_RWKV_LOWRANK_E2E_VECTORS").unwrap()).unwrap(),
    )
    .unwrap();
    let model_name = std::env::var("ANKI_RWKV_LOWRANK_E2E_MODEL").unwrap();
    let formats = [
        ("fp32", StatePrecision::F32),
        ("compact_int8", StatePrecision::CompactCardsInt8),
        ("t24", StatePrecision::CompactLowRank(RankSchedule::DEFAULT)),
        (
            "q2",
            StatePrecision::CompactLowRank(RankSchedule::uniform(2)),
        ),
        // Int6 factors (SM162) and the profile default, whose factor width
        // follows the model's size (int6 for Big and Small, int8 for Tiny).
        (
            "t24_q6",
            StatePrecision::CompactLowRankInt6(RankSchedule::DEFAULT),
        ),
        (
            "by_size",
            StatePrecision::CompactLowRankBySize(RankSchedule::DEFAULT),
        ),
    ];
    // Optional subset: `ANKI_RWKV_LOWRANK_E2E_FORMATS=compact_int8,t24,...`.
    let selected = std::env::var("ANKI_RWKV_LOWRANK_E2E_FORMATS").ok();
    let formats: Vec<(&str, StatePrecision)> = formats
        .into_iter()
        .filter(|(label, _)| {
            selected
                .as_deref()
                .is_none_or(|list| list.split(',').any(|name| name == *label))
        })
        .collect();
    let mut out = serde_json::Map::new();
    for user in &vectors {
        let mut by_format = serde_json::Map::new();
        for &(label, precision) in &formats {
            let mut model = match model_name.as_str() {
                "legacy" => legacy_model(),
                "sm99" => sm99_model(),
                other => panic!("unknown model {other}"),
            };
            let (card, note) = precision
                .for_card_layers(model.card_layers())
                .module_precisions();
            model.modules[0].precision = card;
            model.modules[2].precision = note;
            let mut states = ReviewStateMaps::default();
            let mut values = Vec::with_capacity(user.rows.len());
            for row in &user.rows {
                let input = ReviewInput {
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
                };
                let heads = model.review(&row.features, states.state_ref(&input));
                values.push(serde_json::json!([
                    heads.retrievability.clamp(1e-5, 1.0 - 1e-5),
                    predict_curve(&heads.curve, row.elapsed)
                ]));
                if !row.skip {
                    states.store(&input, heads.next_state);
                }
            }
            by_format.insert(label.to_string(), serde_json::Value::Array(values));
        }
        println!("{}: {} rows", user.name, user.rows.len());
        out.insert(user.name.clone(), serde_json::Value::Object(by_format));
    }
    fs::write(
        std::env::var("ANKI_RWKV_LOWRANK_E2E_OUT").unwrap(),
        serde_json::to_vec(&out).unwrap(),
    )
    .unwrap();
}

#[derive(serde::Deserialize)]
struct AnswerTimeVectors {
    rows: Vec<usize>,
    review_th: Vec<usize>,
    explicit: Vec<Vec<f32>>,
    /// Head logits of the PyTorch head (f32 weights) on the f32 trunk.
    fp32: Vec<Vec<f32>>,
    /// Head logits of the INT8 head on the f32 trunk (the fork has no INT8
    /// big weights).
    int8: Vec<Vec<f32>>,
    /// card id, grade, duration ms, elapsed days of each review
    revlog: [Vec<i64>; 4],
}

#[derive(serde::Deserialize)]
struct AnswerTimeUser {
    name: String,
    rows: Vec<VectorRow>,
    time: AnswerTimeVectors,
}

/// The answer-time heads of the big models (the time heads for the family
/// (SM117)): PyTorch frozen features (the residual after the input encoder
/// and after each of the 5 streams, then the instant head's hidden layer),
/// history features and head logits on two synthetic users (no dataset
/// data), show-time inputs, same-day reviews included, first 500 rows per
/// user.
fn answer_time_vectors(variable: &str, bundled: &[u8]) -> Vec<AnswerTimeUser> {
    let data = if let Ok(path) = std::env::var(variable) {
        fs::read(path).unwrap()
    } else {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(bundled)
            .read_to_end(&mut data)
            .unwrap();
        data
    };
    serde_json::from_slice(&data).unwrap()
}

fn legacy_answer_time_vectors() -> Vec<AnswerTimeUser> {
    answer_time_vectors(
        "ANKI_RWKV_LEGACY_TIME_VECTORS",
        include_bytes!("testdata/legacy-time-vectors.json.gz"),
    )
}

fn sm99_answer_time_vectors() -> Vec<AnswerTimeUser> {
    answer_time_vectors(
        "ANKI_RWKV_SM99_TIME_VECTORS",
        include_bytes!("testdata/sm99-time-vectors.json.gz"),
    )
}

fn grade_softmax(logits: &[f32]) -> Vec<f64> {
    let bins = crate::rwkv::ANSWER_TIME_BINS;
    (0..logits.len() / bins)
        .map(|grade| &logits[grade * bins..(grade + 1) * bins])
        .flat_map(|grade| {
            let max = grade.iter().copied().fold(f32::NEG_INFINITY, f32::max) as f64;
            let exp: Vec<f64> = grade.iter().map(|&v| (v as f64 - max).exp()).collect();
            let sum: f64 = exp.iter().sum();
            exp.into_iter().map(move |v| v / sum)
        })
        .collect()
}

#[test]
fn rwkv_legacy_answer_time_head_matches_published_vectors() {
    check_big_answer_time_head(
        &legacy_model(),
        "RWKV_trained_on_5000_10000.time_head",
        legacy_answer_time_vectors(),
    );
}

#[test]
fn rwkv_sm99_answer_time_head_matches_published_vectors() {
    check_big_answer_time_head(&sm99_model(), "sm99/time_head", sm99_answer_time_vectors());
}

/// The history features recomputed from each synthetic user's revlog equal
/// the Python reference's at every published query, same-day ones included.
#[test]
fn rwkv_big_answer_time_history_matches_published_features() {
    for users in [legacy_answer_time_vectors(), sm99_answer_time_vectors()] {
        for user in users {
            let [cards, grades, durations, elapsed] = &user.time.revlog;
            let wanted: HashMap<usize, usize> = user
                .time
                .review_th
                .iter()
                .enumerate()
                .map(|(index, &review_th)| (review_th, index))
                .collect();
            let mut history = crate::rwkv::AnswerTimeHistory::new();
            let mut checked = 0;
            for review in 0..cards.len() {
                if let Some(&index) = wanted.get(&(review + 1)) {
                    let actual = history.features(cards[review], elapsed[review]);
                    for (feature, (&a, &b)) in
                        actual.iter().zip(&user.time.explicit[index]).enumerate()
                    {
                        assert!(
                            (a - b).abs() <= 1e-6,
                            "{} review {} feature {feature}: {a} != {b}",
                            user.name,
                            review + 1
                        );
                    }
                    checked += 1;
                }
                history.push(cards[review], grades[review], durations[review]);
            }
            assert_eq!(checked, user.time.rows.len(), "{}", user.name);
        }
    }
}

/// The big engine's answer-time inputs (1,306 values: 6 x 128 residuals, the
/// 512-wide instant head's hidden layer, 26 history features) and the head
/// reproduce the PyTorch head for both head formats, and the head's trunk
/// pass gives the grade path's button probabilities bit for bit.
fn check_big_answer_time_head(model: &SrsModel, head_stem: &str, users: Vec<AnswerTimeUser>) {
    assert_eq!(AnswerTimeHead::INPUTS, 1306);
    let folder = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../qt/aqt/rwkv_inference");
    for format in ["f32", "int8"] {
        let head = AnswerTimeHead::load(&folder.join(format!("{head_stem}.{format}.bin"))).unwrap();
        assert!(head.same_day);
        let (mut maximum, mut logit_maximum, mut checked) = (0.0_f64, 0.0_f32, 0);
        for user in &users {
            let time_rows: HashMap<usize, usize> = user
                .time
                .rows
                .iter()
                .enumerate()
                .map(|(index, &row)| (row, index))
                .collect();
            let mut states = ReviewStateMaps::default();
            for (index, row) in user.rows.iter().enumerate() {
                let input = vector_input(row);
                let heads = model.review(&row.features, states.state_ref(&input));
                if let Some(&time) = time_rows.get(&index) {
                    let (frozen, buttons) =
                        model.answer_time_inputs(&row.features, states.state_ref(&input));
                    assert_eq!(frozen.len(), AnswerTimeHead::FROZEN);
                    assert_eq!(
                        buttons, heads.button_probabilities,
                        "{} row {index}",
                        user.name
                    );
                    let logits = head.logits(&frozen, &user.time.explicit[time]);
                    let expected = if format == "f32" {
                        &user.time.fp32[time]
                    } else {
                        &user.time.int8[time]
                    };
                    for (&a, &b) in logits.iter().zip(expected) {
                        logit_maximum = logit_maximum.max((a - b).abs());
                    }
                    for (bin, (a, b)) in grade_softmax(&logits)
                        .into_iter()
                        .zip(grade_softmax(expected))
                        .enumerate()
                    {
                        maximum = maximum.max((a - b).abs());
                        assert!(
                            (a - b).abs() <= 1e-5,
                            "{} {format} row {index} bin {bin}: {a} != {b}",
                            user.name
                        );
                    }
                    checked += 1;
                }
                if !row.skip {
                    states.store(&input, heads.next_state);
                }
            }
        }
        assert!(checked > 0);
        println!(
            "{head_stem} {format} answer-time head: {checked} queries, maximum absolute bin probability error {maximum}, logit error {logit_maximum}"
        );
    }
}
