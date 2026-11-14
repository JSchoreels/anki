// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Deterministic synthetic benchmark for the RWKV Instant workload forecast.
//!
//! Builds a synthetic collection (review, new, intraday learning, relearning,
//! interday learning and non-Instant cards in three decks), warms the SM57
//! resident state from a synthetic history, then runs each requested
//! scenario and writes its complete output (`{:#?}`, exact for floats) so
//! runs can be compared byte for byte.
//!
//! ```sh
//! RUST_LOG=anki::rwkv::instant_workload_forecast=debug \
//! INSTANT_BENCH_CARDS=2000 \
//! INSTANT_BENCH_SCENARIOS=strat3:base:2,indep5:base:2 \
//! INSTANT_BENCH_OUT=/tmp/out \
//! cargo run -p anki --release --features bench --example instant_forecast_bench
//! ```
//!
//! A scenario is `mode:variant:days`. Modes: `strat3` (three stratified
//! paths) and `indepN` (N independent paths). Variants: `base`, `cand`
//! (candidate refresh window 64), `ri4` (refresh every 4 answers),
//! `noprecomp`, `scorenew` (retrievability new-card gather and a low review
//! limit), and `fullN` (item 4: full refresh every N answers).

use std::path::PathBuf;
use std::time::Instant;

use anki::deckconfig::DeckConfig;
use anki::deckconfig::DeckConfigId;
use anki::deckconfig::ReviewCardOrder;
use anki::decks::Deck;
use anki::decks::DeckId;
use anki::decks::DeckKind;
use anki::decks::NativeDeckName;
use anki::rwkv::ModelKind;
use anki::rwkv::ReviewInput;
use anki::rwkv::RwkvInstantForecastCard;
use anki::rwkv::RwkvInstantForecastConfig;
use anki::rwkv::RwkvRuntime;
use anki::rwkv::RwkvWorkloadReviewModel;
use anki::rwkv::RwkvWorkloadSimulationInput;
use anki::rwkv::StatePrecision;
use anki_proto::deck_config::deck_config::config::NewCardGatherPriority;

const TODAY: i64 = 1000;
const NOW_MILLIS: u32 = 6 * 3_600_000;

fn hash(mut x: u64) -> u64 {
    x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
    x = (x ^ (x >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    x ^ (x >> 31)
}

fn env(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

fn deck(id: i64, name: &str, config: i64) -> Deck {
    let mut deck = Deck::new_normal();
    deck.id = DeckId(id);
    deck.name = NativeDeckName::from_human_name(name);
    if let DeckKind::Normal(normal) = &mut deck.kind {
        normal.config_id = config;
    }
    deck
}

fn preset(id: i64, instant: bool, reviews: u32, gather: NewCardGatherPriority) -> DeckConfig {
    let mut config = DeckConfig {
        id: DeckConfigId(id),
        ..Default::default()
    };
    config.inner.rwkv_review_instant_order_enabled = instant;
    config.inner.rwkv_review_minimum_reviews_per_day = if instant { 10 } else { 0 };
    config.inner.reviews_per_day = reviews;
    config.inner.new_per_day = 20;
    config.inner.learn_steps = vec![1.0, 10.0];
    config.inner.relearn_steps = vec![10.0];
    config.inner.new_card_gather_priority = gather as i32;
    config
}

struct Collection {
    history: Vec<ReviewInput>,
    cards: Vec<RwkvInstantForecastCard>,
}

fn review(
    card_id: i64,
    deck_id: i64,
    card_type: i64,
    day: i64,
    elapsed_days: i64,
    ease: u8,
) -> ReviewInput {
    ReviewInput {
        card_id,
        note_id: Some(card_id / 2),
        deck_id: Some(deck_id),
        preset_id: Some(if deck_id == 3 { 2 } else { 1 }),
        is_query: false,
        ease: Some(ease),
        duration_millis: Some(3000 + (hash(card_id as u64 ^ day as u64) % 9000) as i64),
        card_type: Some(card_type),
        day_offset: Some(day),
        current_elapsed_days: Some(elapsed_days),
        current_elapsed_seconds: Some(if elapsed_days < 0 {
            -1
        } else {
            elapsed_days * 86_400
        }),
        target_retentions: [Some(0.9); 4],
        enforce_grade_order: true,
    }
}

fn build_collection(count: usize) -> Collection {
    let mut history = Vec::new();
    let mut cards = Vec::with_capacity(count);
    for i in 0..count {
        let card_id = 1_000_000 + i as i64;
        let r = hash(i as u64);
        let deck_id = 1 + (i % 3) as i64;
        let instant = deck_id != 3;
        let kind = i % 20;
        // (card_type, queue, interval, elapsed_days, learning_due_millis, steps)
        let interval = 1 + (r % 60) as i64;
        let (card_type, queue, elapsed, learning_due, steps) = match kind {
            0 | 1 => (0, 0, -1, 0, 0),
            // Intraday learning, due over the morning.
            2 => (
                1,
                1,
                0,
                i64::from(NOW_MILLIS) + ((r >> 8) % 120) as i64 * 60_000,
                1,
            ),
            // Relearning after a lapse today.
            3 => (
                3,
                1,
                0,
                i64::from(NOW_MILLIS) + ((r >> 8) % 60) as i64 * 60_000,
                1,
            ),
            // Interday learning due today.
            4 => (1, 3, 1, 0, 1),
            _ => (2, 2, ((r >> 16) % (interval as u64 * 2)) as i64, 0, 0),
        };
        let mut reps = 0;
        let mut lapses = 0;
        if card_type != 0 {
            let n = 2 + ((r >> 24) % 4) as i64;
            let last = TODAY - elapsed.max(0);
            let mut days = vec![last];
            let mut gap = (interval / 2).max(1);
            for _ in 1..n {
                let previous = *days.last().unwrap() - gap;
                days.push(previous);
                gap = (gap / 2).max(1);
            }
            days.reverse();
            for (k, &day) in days.iter().enumerate() {
                let elapsed_days = if k == 0 { -1 } else { day - days[k - 1] };
                let typ = if k == 0 { 0 } else { 2 };
                let ease = if card_type == 3 && k == days.len() - 1 {
                    lapses += 1;
                    1
                } else if hash(r ^ k as u64) % 10 == 0 {
                    1
                } else {
                    3
                };
                history.push(review(card_id, deck_id, typ, day, elapsed_days, ease));
                reps += 1;
            }
        }
        let elapsed_seconds = match kind {
            2 | 3 => Some(600 + ((r >> 40) % 1800) as i64),
            _ if card_type == 0 => None,
            _ => Some(elapsed * 86_400 + ((r >> 40) % 3600) as i64),
        };
        let mut input = review(card_id, deck_id, card_type, TODAY, elapsed, 3);
        input.is_query = true;
        input.ease = None;
        input.duration_millis = None;
        input.current_elapsed_days = (card_type != 0).then_some(elapsed.max(0));
        input.current_elapsed_seconds = elapsed_seconds;
        cards.push(RwkvInstantForecastCard {
            input: RwkvWorkloadSimulationInput {
                review_input: input,
                interval_days: (card_type != 0).then_some(if card_type == 1 {
                    0
                } else {
                    interval
                }),
                ease_factor: (card_type != 0).then_some(2500 - (r % 5) as i64 * 100),
                reps: Some(reps),
                lapses: Some(lapses),
            },
            due_day: if queue == 2 { interval - elapsed } else { 0 },
            group: deck_id,
            desired_retention: 0.9,
            instant_enabled: instant,
            intervening_reviews: (card_type != 0).then_some(20 + (r % 200) as u32),
            last_kind_and_grade: if kind == 3 { Some((1, 1)) } else { None },
            queue,
            learning_due_millis: learning_due,
            remaining_steps: steps,
            new_position: i as u32,
            fsrs_memory: None,
            fsrs_parameters: vec![],
            fsrs_historical_retention: 0.9,
            fsrs_enabled: i % 2 == 0,
        });
    }
    history.sort_by_key(|review| (review.day_offset, review.card_id));
    Collection { history, cards }
}

type TimingPatterns = (Vec<Vec<(u32, u32)>>, Vec<[f32; 4]>);

fn timing_patterns() -> TimingPatterns {
    let mut patterns = Vec::new();
    let mut shares = Vec::new();
    for p in 0..12u64 {
        let mut pattern = Vec::new();
        let mut at = 7 * 3_600_000 + p as u32 * 600_000;
        for (session, count) in [(0, 40 + 30 * p), (1, 20 + 10 * p)] {
            if session == 1 {
                at += 3 * 3_600_000;
            }
            for k in 0..count {
                let h = hash(p * 10_000 + session * 1000 + k);
                pattern.push((at, 2000 + (h % 13_000) as u32));
                at += 4000 + ((h >> 20) % 16_000) as u32;
            }
        }
        patterns.push(pattern);
        let f = p as f32 * 0.01;
        shares.push([0.0, 0.1 + f, 0.75, 0.15 - f]);
    }
    (patterns, shares)
}

fn config(mode: &str, variant: &str, days: u32) -> RwkvInstantForecastConfig {
    let (timing_patterns, timing_pattern_success_shares) = timing_patterns();
    let scorenew = variant == "scorenew";
    let gather = if scorenew {
        NewCardGatherPriority::AscendingRetrievability
    } else {
        NewCardGatherPriority::Deck
    };
    let stratified = mode == "strat3";
    let paths = if stratified {
        3
    } else {
        mode.trim_start_matches("indep").parse().unwrap()
    };
    RwkvInstantForecastConfig {
        days,
        paths,
        scenario_sigma: None,
        stratified_answers: stratified,
        stratified_scale: 1.0,
        stratified_bias_sd: 0.03,
        stratified_review_variance: 0.2,
        report_days: false,
        seed: env("INSTANT_BENCH_SEED", "42").parse().unwrap(),
        decks: vec![
            deck(1, "Study", 1),
            deck(2, "Study::A", 1),
            deck(3, "Study::B", 2),
        ],
        deck_configs: vec![
            preset(1, true, if scorenew { 100 } else { 400 }, gather),
            preset(2, false, 50, gather),
        ],
        study_deck_id: 1,
        today: TODAY as u32,
        new_cards_ignore_review_limit: !scorenew,
        fsrs_learning_queues_disabled: false,
        fsrs_short_term_with_steps_enabled: true,
        learn_ahead_secs: 1200,
        timing_patterns,
        timing_pattern_success_shares,
        review_time_model: None,
        now_millis: NOW_MILLIS,
        same_day_allowed: false,
        min_intervening_reviews: 3,
        min_elapsed_secs: 300,
        refresh_interval: if variant == "ri4" { 4 } else { 1 },
        approximate_refresh_answers: variant
            .strip_prefix("full")
            .map_or(0, |every| every.parse().unwrap()),
        candidate_refresh_enabled: variant == "cand",
        candidate_refresh_limit: 64,
        precompute_next_card: variant != "noprecomp",
        review_order: ReviewCardOrder::RetrievabilityAscending,
        review_model: RwkvWorkloadReviewModel {
            grade_seconds: [12.0, 9.0, 6.0, 4.0],
            bucket_probabilities: Vec::new(),
        },
    }
}

fn main() {
    if let Ok(filter) = std::env::var("RUST_LOG") {
        tracing_subscriber::fmt()
            .with_env_filter(tracing_subscriber::EnvFilter::new(filter))
            .with_writer(std::io::stderr)
            .init();
    }
    let count: usize = env("INSTANT_BENCH_CARDS", "2000").parse().unwrap();
    let repeat: usize = env("INSTANT_BENCH_REPEAT", "1").parse().unwrap();
    let scenarios = env("INSTANT_BENCH_SCENARIOS", "strat3:base:2,indep5:base:2");
    let out = std::env::var_os("INSTANT_BENCH_OUT").map(PathBuf::from);
    let weights = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../qt/aqt/rwkv_inference/sm57/weights.f32.bin");
    let mut runtime = RwkvRuntime::load_with_precision(
        weights,
        0.9,
        36_500,
        StatePrecision::Int4CardsInt8Notes,
        ModelKind::Sm57,
    )
    .unwrap();
    let collection = build_collection(count);
    let started = Instant::now();
    let history_len = collection.history.len();
    runtime.warm_up_reviews(collection.history, false).unwrap();
    println!(
        "cards={count} history={history_len} warm_up_ms={:.0}",
        started.elapsed().as_secs_f64() * 1000.0
    );
    for scenario in scenarios.split(',') {
        let parts = scenario.split(':').collect::<Vec<_>>();
        let (mode, variant, days) = (parts[0], parts[1], parts[2].parse().unwrap());
        for run in 0..repeat {
            let started = Instant::now();
            let output = runtime
                .forecast_instant_workload_from_warm_up(
                    collection.cards.clone(),
                    config(mode, variant, days),
                    &mut |_, _| Ok(()),
                    &mut |_, _, _| Ok(()),
                )
                .unwrap();
            let wall = started.elapsed().as_secs_f64() * 1000.0;
            let done = output.days.iter().map(|day| day.done.mean).sum::<f32>();
            let due = output.days.iter().map(|day| day.due.mean).sum::<f32>();
            println!(
                "scenario={scenario} cards={count} run={run} wall_ms={wall:.0} done={done} due={due}"
            );
            if let Some(out) = &out {
                std::fs::create_dir_all(out).unwrap();
                let name = format!("{count}-{}.txt", scenario.replace(':', "-"));
                std::fs::write(out.join(name), format!("{output:#?}")).unwrap();
            }
        }
    }
}
