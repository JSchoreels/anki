// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Long-card regression for the stored card / note state. The app stores the
//! quantized state after every answer, and the next answer reads only that
//! reconstruction, so rounding error compounds on cards with thousands of
//! answers. With the plain int4 layout (`cards_int4_notes_int8`) the f32 row
//! scales of such cards grew until float32 overflowed and every later answer
//! failed; the compact layouts clamp their float16 scales and stay finite.
//!
//! Synthetic single-card histories only (no collection or dataset rows).
//! The quick test runs by default; the full one (20,000 answers, all six
//! release models, both compact policies, the per-answer and the bulk path,
//! and the float32 reference for the drift) is ignored and run in release:
//!
//! ```text
//! cargo test --release -p anki --lib rwkv_long_card_full -- --ignored --nocapture
//! ```
//!
//! Optional environment: `RWKV_LONG_CARD_ANSWERS` (default 20,000),
//! `RWKV_LONG_CARD_THREADS` (default 6), `RWKV_LONG_CARD_PLAIN=1` to add the
//! plain int4 policy (report only: it is expected to fail on 1-day
//! patterns), and `RWKV_LONG_CARD_OUTPUT` (a JSON lines file for the rows).

use std::io::Write;
use std::panic::catch_unwind;
use std::panic::AssertUnwindSafe;
use std::path::PathBuf;
use std::sync::Mutex;

use crate::rwkv::ModelKind;
use crate::rwkv::RankSchedule;
use crate::rwkv::ReviewInput;
use crate::rwkv::ReviewState;
use crate::rwkv::RwkvRuntime;
use crate::rwkv::StatePrecision;

const F16_MAX: f32 = 65_504.0;

/// The release family (`rwkv-family-2026-11-13`): each size's two-head and
/// Instant-Only model, plus the 2026-11 release candidate's new Small and
/// Tiny models (the show-time two-head cool-down for Small (SM118), the
/// longer Small Instant-Only model (SM113), the six-layer Tiny (SM130)).
const MODELS: [(&str, ModelKind, &str); 10] = [
    (
        "Big: the show-time big model (SM91s2)",
        ModelKind::Legacy,
        "RWKV_trained_on_5000_10000.bin",
    ),
    (
        "Big: the show-time instant-only big model (SM99)",
        ModelKind::Sm99,
        "sm99/weights.f32.bin",
    ),
    (
        "Small: the show-time retrain (SM92)",
        ModelKind::Sm92,
        "sm92/weights.f32.bin",
    ),
    (
        "Small: the Small Instant-Only model (SM98)",
        ModelKind::Sm98,
        "sm98/weights.f32.bin",
    ),
    (
        "Tiny: the Tiny two-head model (SM106)",
        ModelKind::Sm106,
        "sm106/weights.f32.bin",
    ),
    (
        "Tiny: the Tiny Instant-Only model (SM101)",
        ModelKind::Sm101,
        "sm101/weights.f32.bin",
    ),
    (
        "Small (RC): the show-time two-head cool-down for Small (SM118)",
        ModelKind::Sm118,
        "sm118/weights.f32.bin",
    ),
    (
        "Small (RC): the longer Small Instant-Only model (SM113)",
        ModelKind::Sm113,
        "sm113/weights.f32.bin",
    ),
    (
        "Tiny (RC): the six-layer Tiny two-head model (SM130)",
        ModelKind::Sm130Th,
        "sm130th/weights.f32.bin",
    ),
    (
        "Tiny (RC): the six-layer Tiny Instant-Only model (SM130)",
        ModelKind::Sm130Io,
        "sm130io/weights.f32.bin",
    ),
];

/// The release models this build can load alone: the small Instant-Only
/// models need the small-model feature (`rwkv-sm40`, the default).
fn models() -> Vec<(&'static str, ModelKind, &'static str)> {
    MODELS
        .into_iter()
        .filter(|(_, kind, _)| {
            !(matches!(
                kind,
                ModelKind::Sm98 | ModelKind::Sm101 | ModelKind::Sm113 | ModelKind::Sm130Io
            ) && !super::SMALL_MODEL)
        })
        .collect()
}

struct Lcg(u64);

impl Lcg {
    fn next(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        self.0 >> 33
    }

    fn unit(&mut self) -> f64 {
        self.next() as f64 / (1u64 << 31) as f64
    }
}

/// (ease, seconds since the previous answer) per answer.
fn pattern(name: &str, answers: usize) -> Vec<(u8, i64)> {
    let mut rng = Lcg(12_345);
    let mut previous_again = false;
    (0..answers)
        .map(|index| {
            let (ease, gap) = match name {
                "again1d" => (1, 86_400),
                "good1d" => (3, 86_400),
                "mixed" => {
                    // 15% Again, 10% Hard, 65% Good, 10% Easy, plus a streak of
                    // 40 Agains every 250 answers; 10 minutes after an Again.
                    let ease = if (200..240).contains(&(index % 250)) {
                        1
                    } else {
                        match rng.unit() {
                            u if u < 0.15 => 1,
                            u if u < 0.25 => 2,
                            u if u < 0.90 => 3,
                            _ => 4,
                        }
                    };
                    let gap = if previous_again {
                        600
                    } else {
                        match rng.unit() {
                            u if u < 0.5 => 86_400,
                            u if u < 0.8 => 86_400 * (2 + (rng.next() % 2) as i64),
                            u if u < 0.95 => 3_600,
                            _ => 86_400 * (7 + (rng.next() % 4) as i64),
                        }
                    };
                    (ease, gap)
                }
                other => panic!("unknown pattern {other}"),
            };
            previous_again = ease == 1;
            (ease, gap)
        })
        .collect()
}

fn inputs(name: &str, answers: usize) -> Vec<ReviewInput> {
    let mut rng = Lcg(777);
    let mut time: i64 = 1_000 * 86_400 + 9 * 3_600;
    let mut previous: Option<(i64, u8)> = None;
    pattern(name, answers)
        .into_iter()
        .enumerate()
        .map(|(index, (ease, gap))| {
            if index > 0 {
                time += gap;
            }
            let card_type = match previous {
                None => 0,
                Some((_, 1)) => 3,
                Some(_) => 2,
            };
            let (days, seconds) = match previous {
                Some((at, _)) => (Some(time / 86_400 - at / 86_400), Some(time - at)),
                None => (None, None),
            };
            previous = Some((time, ease));
            ReviewInput {
                card_id: 1_700_000_000_001,
                note_id: Some(1_700_000_000_002),
                deck_id: Some(1),
                preset_id: Some(1),
                is_query: false,
                ease: Some(ease),
                duration_millis: Some(3_000 + (rng.next() % 12_000) as i64),
                card_type: Some(card_type),
                day_offset: Some(time / 86_400),
                current_elapsed_days: days,
                current_elapsed_seconds: seconds,
                target_retentions: [Some(0.9); 4],
                enforce_grade_order: true,
            }
        })
        .collect()
}

fn u32_at(bytes: &[u8], at: &mut usize) -> usize {
    let value = u32::from_le_bytes(bytes[*at..*at + 4].try_into().unwrap()) as usize;
    *at += 4;
    value
}

fn f16_at(bytes: &[u8], at: usize) -> f32 {
    half::f16::from_bits(u16::from_le_bytes([bytes[at], bytes[at + 1]])).to_f32()
}

/// The largest row scale of a stored compact / plain state (rows with a
/// non-zero code only), and whether any scale, shift or value is non-finite.
/// `None` for the f32 layout.
fn state_stats(bytes: &[u8]) -> Option<(f32, bool)> {
    if !bytes.starts_with(b"ARWKVMODSTATE2") {
        return None;
    }
    let mut at = 14;
    let code = bytes[at];
    at += 1;
    // 0x28 / 0x26: low-rank int8 / int6 factors (compact shifts, one
    // float16 scale per factor vector; int6 codes bit-packed, four in three
    // bytes).
    let lowrank = code == 0x28 || code == 0x26;
    let compact = code & 0x10 != 0 || lowrank;
    let int8 = code & 0x0f == 8;
    let scale_bytes = if compact { 2 } else { 4 };
    let mut max_scale = 0.0_f32;
    let mut non_finite = false;
    let shift = |bytes: &[u8], at: &mut usize, non_finite: &mut bool| {
        let count = u32_at(bytes, at);
        if count == 0 {
            return;
        }
        if compact {
            *non_finite |= !f16_at(bytes, *at).is_finite();
            *at += 2 + count;
        } else {
            *non_finite |= (0..count).any(|index| !f16_at(bytes, *at + 2 * index).is_finite());
            *at += 2 * count;
        }
    };
    for _ in 0..u32_at(bytes, &mut at) {
        shift(bytes, &mut at, &mut non_finite);
        let values = u32_at(bytes, &mut at);
        if values > 0 && lowrank {
            let head_size = ((values / 4) as f64).sqrt() as usize;
            let code_bytes = if code == 0x26 {
                head_size / 4 * 3
            } else {
                head_size
            };
            let rank = bytes[at + 1] as usize;
            at += 2;
            for _ in 0..4 * 2 * rank {
                let scale = f16_at(bytes, at);
                non_finite |= !scale.is_finite();
                if bytes[at + 2..at + 2 + code_bytes]
                    .iter()
                    .any(|&code| code != 0)
                {
                    max_scale = max_scale.max(scale);
                }
                at += 2 + code_bytes;
            }
        } else if values > 0 {
            let head_size = ((values / 4) as f64).sqrt() as usize;
            let code_bytes = if int8 { head_size } else { head_size / 2 };
            for _ in 0..values / head_size {
                let scale = if compact {
                    f16_at(bytes, at)
                } else {
                    f32::from_le_bytes(bytes[at..at + 4].try_into().unwrap())
                };
                non_finite |= !scale.is_finite();
                let codes = &bytes[at + scale_bytes..at + scale_bytes + code_bytes];
                if codes.iter().any(|&code| code != 0) {
                    max_scale = max_scale.max(scale);
                }
                at += scale_bytes + code_bytes;
            }
        }
        shift(bytes, &mut at, &mut non_finite);
    }
    assert_eq!(at, bytes.len(), "stored state layout");
    Some((max_scale, non_finite))
}

fn weights(file: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../qt/aqt/rwkv_inference")
        .join(file)
}

fn load(kind: ModelKind, file: &str, precision: StatePrecision) -> RwkvRuntime {
    RwkvRuntime::load_with_precision(weights(file), 0.9, 36_500, precision, kind).unwrap()
}

#[derive(Default)]
struct Run {
    answers: usize,
    /// The first error or panic, with its answer number.
    failure: Option<String>,
    /// The first answer with a non-finite output or stored value.
    non_finite_at: Option<usize>,
    /// The first answer whose stored card state has a row at the float16 cap.
    saturated_at: Option<usize>,
    peak_card_scale: f32,
    peak_note_scale: f32,
    retrievability: Vec<f32>,
    /// The last stored card and note state.
    last_card: Option<Vec<u8>>,
    last_note: Option<Vec<u8>>,
}

fn panic_message(panic: Box<dyn std::any::Any + Send>) -> String {
    panic
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| panic.downcast_ref::<&str>().map(|text| text.to_string()))
        .unwrap_or_default()
}

impl Run {
    fn observe(&mut self, answer: usize, card: Option<&[u8]>, note: Option<&[u8]>, finite: bool) {
        self.answers = answer;
        self.last_card = card.map(<[u8]>::to_vec);
        self.last_note = note.map(<[u8]>::to_vec);
        let card = card.and_then(state_stats);
        let note = note.and_then(state_stats);
        let stored_non_finite =
            card.is_some_and(|(_, bad)| bad) || note.is_some_and(|(_, bad)| bad);
        if (!finite || stored_non_finite) && self.non_finite_at.is_none() {
            self.non_finite_at = Some(answer);
        }
        if let Some((scale, _)) = card {
            self.peak_card_scale = self.peak_card_scale.max(scale);
            if scale >= F16_MAX && self.saturated_at.is_none() {
                self.saturated_at = Some(answer);
            }
        }
        if let Some((scale, _)) = note {
            self.peak_note_scale = self.peak_note_scale.max(scale);
        }
    }
}

/// The app's per-answer path: every stored module state is fed back.
fn per_answer(
    kind: ModelKind,
    file: &str,
    precision: StatePrecision,
    reviews: &[ReviewInput],
) -> Run {
    let mut runtime = load(kind, file, precision);
    let mut run = Run::default();
    let mut states: [Option<Vec<u8>>; 5] = Default::default();
    for (index, input) in reviews.iter().enumerate() {
        let result = catch_unwind(AssertUnwindSafe(|| {
            runtime.review(
                input.clone(),
                ReviewState {
                    card: states[0].as_deref(),
                    deck: states[1].as_deref(),
                    note: states[2].as_deref(),
                    preset: states[3].as_deref(),
                    global: states[4].as_deref(),
                },
            )
        }));
        match result {
            Ok(Ok(output)) => {
                let finite = output.retrievability.is_finite()
                    && output.button_probabilities.iter().all(|p| p.is_finite());
                run.retrievability.push(output.retrievability);
                run.observe(
                    index + 1,
                    Some(&output.card_state),
                    Some(&output.note_state),
                    finite,
                );
                states = [
                    Some(output.card_state),
                    Some(output.deck_state),
                    Some(output.note_state),
                    Some(output.preset_state),
                    Some(output.global_state),
                ];
            }
            Ok(Err(err)) => {
                run.failure = Some(format!("error at answer {}: {err}", index + 1));
                break;
            }
            Err(panic) => {
                run.failure = Some(format!(
                    "panic at answer {}: {}",
                    index + 1,
                    panic_message(panic)
                ));
                break;
            }
        }
    }
    run
}

/// The bulk warm-up path, in calls of `chunk` answers, as a cache rebuild or
/// catch-up replays them.
fn bulk(
    kind: ModelKind,
    file: &str,
    precision: StatePrecision,
    reviews: &[ReviewInput],
    chunk: usize,
) -> Run {
    let mut runtime = load(kind, file, precision);
    let mut run = Run::default();
    for (index, answers) in reviews.chunks(chunk).enumerate() {
        let result = catch_unwind(AssertUnwindSafe(|| {
            runtime.warm_up_reviews(answers.to_vec(), true)
        }));
        let done = index * chunk + answers.len();
        match result {
            Ok(Ok(predictions)) => {
                let finite = predictions.iter().all(|(_, r)| r.is_finite());
                run.retrievability
                    .extend(predictions.into_iter().map(|(_, r)| r));
                let state = runtime.warm_up_state(&answers[0]).unwrap();
                run.observe(done, state.card.as_deref(), state.note.as_deref(), finite);
            }
            Ok(Err(err)) => {
                run.failure = Some(format!("error in the call ending at answer {done}: {err}"));
                break;
            }
            Err(panic) => {
                run.failure = Some(format!(
                    "panic in the call ending at answer {done}: {}",
                    panic_message(panic)
                ));
                break;
            }
        }
    }
    run
}

fn mean_abs_difference(
    values: &[f32],
    reference: &[f32],
    range: std::ops::Range<usize>,
) -> Option<f64> {
    let end = range.end.min(values.len()).min(reference.len());
    let pairs: Vec<f64> = (range.start..end)
        .filter(|&index| values[index].is_finite())
        .map(|index| (values[index] - reference[index]).abs() as f64)
        .collect();
    (!pairs.is_empty()).then(|| pairs.iter().sum::<f64>() / pairs.len() as f64)
}

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn policy_name(precision: StatePrecision) -> &'static str {
    match precision {
        StatePrecision::CompactCardsInt8 => "compact_cards_int8",
        StatePrecision::CompactCardsInt4 => "compact_cards_int4",
        StatePrecision::CompactLowRank(schedule) if schedule == RankSchedule::DEFAULT => {
            "compact_lowrank_int8"
        }
        StatePrecision::CompactLowRank(_) => "compact_lowrank_int8:other",
        StatePrecision::CompactLowRankInt6(schedule) if schedule == RankSchedule::DEFAULT => {
            "compact_lowrank_int6"
        }
        StatePrecision::CompactLowRankInt6(_) => "compact_lowrank_int6:other",
        StatePrecision::CompactLowRankBySize(schedule) if schedule == RankSchedule::DEFAULT => {
            "compact_lowrank"
        }
        StatePrecision::CompactLowRankBySize(_) => "compact_lowrank:other",
        StatePrecision::Int4CardsInt8Notes => "cards_int4_notes_int8",
        StatePrecision::F32 => "f32",
        _ => "other",
    }
}

fn format_answer(value: Option<usize>) -> String {
    value.map_or_else(|| "-".to_string(), |answer| answer.to_string())
}

fn format_drift(value: Option<f64>) -> String {
    value.map_or_else(|| "-".to_string(), |drift| format!("{drift:.4}"))
}

#[test]
fn rwkv_long_card_quick_compact_states_stay_finite() {
    // A few hundred answers in debug: both compact policies, every size, the
    // per-answer and the bulk path; the 1-day Good pattern grows fastest.
    for (name, kind, file) in models() {
        let answers = if matches!(kind, ModelKind::Legacy | ModelKind::Sm99) {
            60
        } else {
            300
        };
        let reviews = inputs("good1d", answers);
        for precision in [
            StatePrecision::CompactCardsInt8,
            StatePrecision::CompactCardsInt4,
            StatePrecision::CompactLowRank(RankSchedule::DEFAULT),
            StatePrecision::CompactLowRankInt6(RankSchedule::DEFAULT),
        ] {
            for run in [
                per_answer(kind, file, precision, &reviews),
                bulk(kind, file, precision, &reviews, answers / 3),
            ] {
                assert_eq!(run.failure, None, "{name} {precision:?}");
                assert_eq!(run.non_finite_at, None, "{name} {precision:?}");
                assert_eq!(run.answers, answers, "{name} {precision:?}");
                assert!(run.peak_card_scale <= F16_MAX);
            }
        }
    }
}

/// The live path (each answer reads the state the previous answer stored, as
/// the app does) and a cache rebuild from the history (the bulk warm-up, in
/// one call and in calls of 7) store the same bytes for the card and the
/// note, for the low-rank policies on every size: the profile default
/// (`compact_lowrank`: int6 factors for Big and Small, int8 for Tiny) and
/// both explicit factor widths. 40 answers take the card past rank 4 (16
/// answers).
#[test]
fn rwkv_long_card_live_answers_store_what_a_rebuild_stores() {
    let answers = 40;
    let reviews = inputs("mixed", answers);
    for (name, kind, file) in models() {
        for precision in [
            StatePrecision::CompactLowRankBySize(RankSchedule::DEFAULT),
            StatePrecision::CompactLowRankInt6(RankSchedule::DEFAULT),
            StatePrecision::CompactLowRank(RankSchedule::DEFAULT),
        ] {
            let live = per_answer(kind, file, precision, &reviews);
            assert_eq!(live.failure, None, "{name} {precision:?}");
            let card = live.last_card.as_deref().unwrap();
            let tiny = matches!(
                kind,
                ModelKind::Sm101
                    | ModelKind::Sm106
                    | ModelKind::Sm115
                    | ModelKind::Sm130Th
                    | ModelKind::Sm130Io
            );
            let code = match precision {
                StatePrecision::CompactLowRank(_) => 0x28,
                StatePrecision::CompactLowRankInt6(_) => 0x26,
                _ if tiny => 0x28,
                _ => 0x26,
            };
            assert_eq!(card[14], code, "{name} {precision:?}");
            for chunk in [answers, 7] {
                let rebuild = bulk(kind, file, precision, &reviews, chunk);
                assert_eq!(rebuild.failure, None, "{name} {precision:?}");
                assert!(
                    rebuild.last_card == live.last_card && rebuild.last_note == live.last_note,
                    "{name} {precision:?}: the rebuild (calls of {chunk}) stores other bytes"
                );
            }
        }
    }
}

#[test]
#[ignore]
fn rwkv_long_card_full() {
    let answers = env_usize("RWKV_LONG_CARD_ANSWERS", 20_000);
    let threads = env_usize("RWKV_LONG_CARD_THREADS", 6).max(1);
    let mut policies = vec![
        StatePrecision::CompactCardsInt8,
        StatePrecision::CompactCardsInt4,
        StatePrecision::CompactLowRank(RankSchedule::DEFAULT),
        StatePrecision::CompactLowRankInt6(RankSchedule::DEFAULT),
    ];
    if std::env::var("RWKV_LONG_CARD_PLAIN").is_ok_and(|value| value == "1") {
        policies.push(StatePrecision::Int4CardsInt8Notes);
    }
    let models = models();
    let jobs: Vec<(usize, &str)> = (0..models.len())
        .flat_map(|model| ["mixed", "again1d", "good1d"].map(|pattern| (model, pattern)))
        .collect();
    let next = Mutex::new(0usize);
    let rows = Mutex::new(Vec::<(usize, String, serde_json::Value)>::new());
    std::thread::scope(|scope| {
        for _ in 0..threads {
            scope.spawn(|| loop {
                let job = {
                    let mut next = next.lock().unwrap();
                    *next += 1;
                    *next - 1
                };
                let Some(&(model, pattern_name)) = jobs.get(job) else {
                    break;
                };
                let (name, kind, file) = models[model];
                let reviews = inputs(pattern_name, answers);
                let started = std::time::Instant::now();
                let reference = per_answer(kind, file, StatePrecision::F32, &reviews);
                assert_eq!(reference.failure, None, "{name} f32 {pattern_name}");
                for &precision in &policies {
                    for (path, run) in [
                        ("per-answer", per_answer(kind, file, precision, &reviews)),
                        ("bulk (calls of 250)", bulk(kind, file, precision, &reviews, 250)),
                    ] {
                        // The bulk path records pre-answer queries, the per-answer
                        // path post-answer outputs: only the per-answer drift is
                        // compared with the (per-answer) float32 reference.
                        let drift = |range| {
                            (path == "per-answer")
                                .then(|| {
                                    mean_abs_difference(
                                        &run.retrievability,
                                        &reference.retrievability,
                                        range,
                                    )
                                })
                                .flatten()
                        };
                        let windows = [drift(0..500), drift(500..5_000), drift(5_000..answers)];
                        let line = format!(
                            "| {name} | {} | {pattern_name} | {path} | {} | {} | {} | {} | {:.3e} | {:.3e} | {} / {} / {} |",
                            policy_name(precision),
                            run.answers,
                            run.failure.clone().unwrap_or_else(|| "-".into()),
                            format_answer(run.non_finite_at),
                            format_answer(run.saturated_at),
                            run.peak_card_scale,
                            run.peak_note_scale,
                            format_drift(windows[0]),
                            format_drift(windows[1]),
                            format_drift(windows[2]),
                        );
                        let json = serde_json::json!({
                            "model": name,
                            "policy": policy_name(precision),
                            "pattern": pattern_name,
                            "path": path,
                            "answers": run.answers,
                            "failure": run.failure,
                            "non_finite_at": run.non_finite_at,
                            "saturated_at": run.saturated_at,
                            "peak_card_scale": run.peak_card_scale,
                            "peak_note_scale": run.peak_note_scale,
                            "mean_abs_dr_vs_f32": windows,
                        });
                        if precision != StatePrecision::Int4CardsInt8Notes {
                            assert_eq!(run.failure, None, "{line}");
                            assert_eq!(run.non_finite_at, None, "{line}");
                            assert_eq!(run.answers, answers, "{line}");
                        }
                        rows.lock().unwrap().push((job, line, json));
                    }
                }
                println!(
                    "done {name} {pattern_name} in {:.0} s",
                    started.elapsed().as_secs_f64()
                );
            });
        }
    });
    let mut rows = rows.into_inner().unwrap();
    rows.sort_by_key(|(job, ..)| *job);
    println!(
        "| model | policy | pattern | path | answers | failure | first non-finite | card saturated from | peak card scale | peak note scale | mean abs dr vs f32 (1-500 / 501-5,000 / 5,001-end) |"
    );
    println!("|---|---|---|---|---:|---|---:|---:|---:|---:|---|");
    for (_, line, _) in &rows {
        println!("{line}");
    }
    if let Ok(path) = std::env::var("RWKV_LONG_CARD_OUTPUT") {
        let mut file = std::fs::File::create(path).unwrap();
        for (_, _, json) in &rows {
            writeln!(file, "{json}").unwrap();
        }
    }
}
