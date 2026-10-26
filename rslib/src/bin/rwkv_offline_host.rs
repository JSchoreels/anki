// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Host check of the offline RWKV layer: prepares a collection the way
//! AnkiDroid does, builds deck counts, and prints each scored card.
//!
//! usage: rwkv_offline_host <collection.anki2> <model.bin> [out.csv]

use std::env;
use std::fs::File;
use std::io::BufWriter;
use std::io::Write;
use std::time::Instant;

use anki::collection::CollectionBuilder;
use anki::services::DecksService;
use anki::services::SchedulerService;
use anki_proto::decks::DeckTreeRequest;
use anki_proto::scheduler::RwkvOfflineForecastRequest;
use anki_proto::scheduler::RwkvOfflineInstantPassStepRequest;
use anki_proto::scheduler::RwkvPrepareOfflineRequest;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args().collect();
    if args.len() < 3 {
        return Err("usage: rwkv_offline_host <collection.anki2> <model.bin> [out.csv]".into());
    }
    let mut col = CollectionBuilder::new(&args[1]).build()?;

    let start = Instant::now();
    prepare_and_wait(&mut col, &args[2])?;
    println!("prepare: {} ms", start.elapsed().as_millis());
    if args.get(4).map(String::as_str) == Some("calibration") {
        return calibration_check(col, &args[1], &args[2]);
    }

    let start = Instant::now();
    prepare_and_wait(&mut col, &args[2])?;
    println!("prepare again: {} ms", start.elapsed().as_millis());

    let start = Instant::now();
    let pass = instant_pass(&mut col)?;
    println!(
        "instant pass: available={} scored={} in {} ms",
        pass.available,
        pass.scored,
        start.elapsed().as_millis()
    );

    // "Rebuild RWKV State": a full replay must give the same scores again
    let before_rebuild = {
        DecksService::deck_tree(&mut col, DeckTreeRequest { now: 0 }).ok();
        all_scores(&mut col)?
    };
    let rebuilt = SchedulerService::rwkv_offline_instant_pass_step(
        &mut col,
        RwkvOfflineInstantPassStepRequest {
            restart: true,
            ..Default::default()
        },
    )?;
    let after_rebuild = all_scores(&mut col)?;
    report(
        "before rebuild vs after rebuild (no answers)",
        &before_rebuild,
        &after_rebuild,
    );
    let status = replay_status(&mut col)?;
    println!(
        "rebuild: {} reviews replayed in {} ms, {} absorbed, {} scored; status: {} absorbed, {} replayed",
        rebuilt.reviews_replayed,
        rebuilt.step_micros / 1000,
        rebuilt.reviews_absorbed,
        rebuilt.scored,
        status.reviews_absorbed,
        status.reviews_replayed,
    );

    // A new start (collection closed and opened again, as when the app
    // restarts) loads the saved state file instead of replaying the history,
    // and must give the same scores.
    let state_file = std::path::Path::new(&args[1]).with_extension("rwkv-offline");
    let before_reopen = {
        instant_pass(&mut col)?;
        all_scores(&mut col)?
    };
    col.close(None)?;
    let mut col = CollectionBuilder::new(&args[1]).build()?;
    let start = Instant::now();
    prepare_and_wait(&mut col, &args[2])?;
    let prepare_ms = start.elapsed().as_millis();
    let after_reopen = {
        instant_pass(&mut col)?;
        all_scores(&mut col)?
    };
    println!(
        "reopen: state file {} KB; prepare {} ms",
        std::fs::metadata(&state_file).map_or(0, |m| m.len() / 1024),
        prepare_ms,
    );
    report(
        "before reopen vs after reopen",
        &before_reopen,
        &after_reopen,
    );

    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_secs() as i64;
    let start = Instant::now();
    let tree = DecksService::deck_tree(&mut col, DeckTreeRequest { now })?;
    println!("deck tree in {} ms", start.elapsed().as_millis());
    for deck in &tree.children {
        println!(
            "  {}: new={} learn={} review={}",
            deck.name, deck.new_count, deck.learn_count, deck.review_count
        );
        for child in &deck.children {
            println!(
                "    {}: new={} learn={} review={}",
                child.name, child.new_count, child.learn_count, child.review_count
            );
        }
    }

    // Forecast: "now" must equal the installed scores; later times only move
    // the clock, so recall can only change by time passing.
    let start = Instant::now();
    let forecast = SchedulerService::rwkv_offline_forecast(
        &mut col,
        RwkvOfflineForecastRequest {
            search: String::new(),
            offsets_secs: vec![0, 3600, 3 * 3600, -1],
        },
    )?;
    let ms = start.elapsed().as_millis();
    let installed: std::collections::HashMap<_, _> = all_scores(&mut col)?.into_iter().collect();
    let due = |i: usize| {
        forecast
            .cards
            .iter()
            .filter(|c| c.recall[i] < c.target_retention)
            .count()
    };
    let max_diff = forecast
        .cards
        .iter()
        .filter_map(|c| installed.get(&c.card_id).map(|r| (r - c.recall[0]).abs()))
        .fold(0f32, f32::max);
    println!(
        "forecast: available={} {} cards in {ms} ms; due now {} / 1 h {} / 3 h {} / tomorrow {}; max |now - installed| {max_diff:.6}",
        forecast.available,
        forecast.cards.len(),
        due(0),
        due(1),
        due(2),
        due(3),
    );
    // due now - waiting + minimum cards must equal the deck list's review counts
    let minimum = forecast.cards.iter().filter(|c| c.minimum_today).count();
    let waiting = forecast.cards.iter().filter(|c| c.waiting).count();
    // a top deck whose preset is not RWKV-Instant shows 0: count its children then
    let deck_list: u32 = tree
        .children
        .iter()
        .map(|d| {
            d.review_count
                .max(d.children.iter().map(|c| c.review_count).sum())
        })
        .sum();
    println!(
        "minimum: {minimum} cards today, {waiting} waiting; due - waiting + minimum = {} vs deck list {deck_list}; tomorrow with minimum {:?}",
        due(0) - waiting + minimum,
        forecast.rollover_with_minimum,
    );

    if let Some(path) = args.get(3) {
        let mut out = BufWriter::new(File::create(format!("{path}.forecast.csv"))?);
        writeln!(out, "card_id,deck_id,target,now,1h,3h,tomorrow,minimum")?;
        for c in &forecast.cards {
            let r: Vec<_> = c.recall.iter().map(|r| format!("{r:.6}")).collect();
            writeln!(
                out,
                "{},{},{},{},{}",
                c.card_id,
                c.deck_id,
                c.target_retention,
                r.join(","),
                c.minimum_today as u8
            )?;
        }
        let mut out = BufWriter::new(File::create(path)?);
        writeln!(out, "card_id,retrievability")?;
        let scores = all_scores(&mut col)?;
        for (card_id, retrievability) in &scores {
            writeln!(out, "{card_id},{retrievability:.6}")?;
        }
        println!("wrote {} scores to {path}", scores.len());
    }

    if args.get(4).map(String::as_str) == Some("lag") {
        let deck: i64 = args[5].parse()?;
        // optional answer count (30): 200+ reaches a state-file save
        let answers = args.get(6).map_or(Ok(30), |n| n.parse())?;
        return lag_check(col, deck, now, answers);
    }
    if args.get(4).map(String::as_str) == Some("probe") {
        let deck: i64 = args[5].parse()?;
        // rating for each answer: 1 Again, 3 Good (default)
        let rating: i32 = args.get(6).map_or(Ok(3), |n| n.parse())?;
        return probe_check(col, deck, now, rating);
    }
    if args.get(4).map(String::as_str) == Some("curve") {
        let deck: i64 = args[5].parse()?;
        let cards = args.get(6).map_or(Ok(10), |n| n.parse())?;
        return curve_check(col, deck, now, cards);
    }
    if args.get(4).map(String::as_str) == Some("curve-reschedule") {
        return curve_reschedule_check(col, args[5].parse()?);
    }
    if args.get(4).map(String::as_str) == Some("answer") {
        answer_check(col, &args[1], &args[2], now)?;
    }
    Ok(())
}

/// The stats page's RWKV calibration data: the first call starts a recording
/// replay in the background; it is saved with the state and loaded with it by
/// the next start; answers extend it; "Rebuild RWKV State" records the same
/// numbers again. Prints its log loss against the answers.
fn calibration_check(
    mut col: Col,
    col_path: &str,
    model_path: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let calibration = |col: &mut Col, review_ids: Vec<i64>| -> anki::error::Result<_> {
        let started = Instant::now();
        let out = SchedulerService::rwkv_offline_calibration(
            col,
            anki_proto::scheduler::RwkvOfflineCalibrationRequest { review_ids },
        )?;
        println!(
            "calibration: available={} computing={} started={} {} predictions in {} ms",
            out.available,
            out.computing,
            out.started,
            out.predictions.len(),
            started.elapsed().as_millis()
        );
        Ok(out)
    };
    let started = Instant::now();
    assert!(calibration(&mut col, vec![])?.started);
    while !calibration(&mut col, vec![])?.available {
        std::thread::sleep(std::time::Duration::from_secs(5));
    }
    println!("recorded in {} s", started.elapsed().as_secs());
    col.close(None)?;

    let answers: Vec<(i64, i64)> = {
        let db = rusqlite::Connection::open(col_path)?;
        let mut query = db.prepare("select id, ease from revlog order by id")?;
        let rows = query.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?;
        rows.collect::<Result<_, _>>()?
    };
    let ids: Vec<i64> = answers.iter().map(|(id, _)| *id).collect();
    let mut col = CollectionBuilder::new(col_path).build()?;
    prepare_and_wait(&mut col, model_path)?;
    let first = calibration(&mut col, ids.clone())?;
    assert!(first.available, "calibration file not loaded with the state");
    let ease: std::collections::HashMap<i64, i64> = answers.into_iter().collect();
    let loss = first
        .review_ids
        .iter()
        .zip(&first.predictions)
        .map(|(id, p)| {
            let p = (*p as f64).clamp(1e-6, 1.0 - 1e-6);
            if ease[id] > 1 {
                -p.ln()
            } else {
                -(1.0 - p).ln()
            }
        })
        .sum::<f64>();
    println!(
        "{} of {} reviews; log loss {:.4}",
        first.predictions.len(),
        ids.len(),
        loss / first.predictions.len() as f64
    );

    let millis = || {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64
    };
    let before = millis();
    let cards = col.search_cards("is:review -is:suspended", anki::search::SortMode::NoOrder)?;
    for (index, cid) in cards.iter().take(3).enumerate() {
        let states =
            SchedulerService::get_scheduling_states(&mut col, anki_proto::cards::CardId { cid: cid.0 })?;
        answer(&mut col, cid.0, &states, states.good.clone(), 2, before + index as i64)?;
    }
    // the answers' revlog ids are the time they were saved
    let mut asked = ids;
    asked.extend(before..=millis() + 1000);
    let after = calibration(&mut col, asked.clone())?;
    assert_eq!(after.predictions.len(), first.predictions.len() + 3);
    assert_eq!(after.predictions[..first.predictions.len()], first.predictions[..]);

    SchedulerService::rwkv_offline_instant_pass_step(
        &mut col,
        RwkvOfflineInstantPassStepRequest {
            restart: true,
            ..Default::default()
        },
    )?;
    let rebuilt = calibration(&mut col, asked)?;
    let rebuilt: std::collections::HashMap<_, _> =
        rebuilt.review_ids.into_iter().zip(rebuilt.predictions).collect();
    let worst = after
        .review_ids
        .iter()
        .zip(&after.predictions)
        .filter_map(|(id, p)| rebuilt.get(id).map(|q| (p - q).abs()))
        .fold(0.0, f32::max);
    println!("incremental vs rebuilt: worst difference {worst:.6}");
    assert!(worst < 1e-3);
    println!("*** calibration ok");
    Ok(())
}

fn all_scores(
    col: &mut anki::collection::Collection,
) -> Result<Vec<(i64, f32)>, Box<dyn std::error::Error>> {
    let mut scores = vec![];
    for card_id in col.search_cards("-is:new", anki::search::SortMode::NoOrder)? {
        let score = SchedulerService::get_rwkv_retrievability_score(
            col,
            anki_proto::cards::CardId { cid: card_id.0 },
        )?;
        if let Some(retrievability) = score.retrievability {
            scores.push((card_id.0, retrievability));
        }
    }
    scores.sort_by_key(|(card_id, _)| *card_id);
    Ok(scores)
}

/// Answer three scored cards through the service (as AnkiDroid does), then
/// check that the incrementally updated model state gives the same scores as
/// a fresh replay of the whole history.
fn answer_check(
    mut col: anki::collection::Collection,
    col_path: &str,
    model_path: &str,
    now: i64,
) -> Result<(), Box<dyn std::error::Error>> {
    let before = all_scores(&mut col)?;
    let mut answered: Vec<i64> = before.iter().take(3).map(|(card_id, _)| *card_id).collect();
    // The card answered Good is answered once more: a same-day repeat.
    answered.push(answered[2]);
    for (index, cid) in answered.iter().enumerate() {
        let states = SchedulerService::get_scheduling_states(
            &mut col,
            anki_proto::cards::CardId { cid: *cid },
        )?;
        // Again, Hard, Good
        let (new_state, rating) = match index {
            0 => (states.again.clone(), 0),
            1 => (states.hard.clone(), 1),
            _ => (states.good.clone(), 2),
        };
        answer(
            &mut col,
            *cid,
            &states,
            new_state,
            rating,
            now * 1000 + index as i64,
        )?;
    }
    let pass = instant_pass(&mut col)?;
    DecksService::deck_tree(&mut col, DeckTreeRequest { now })?;
    let incremental = all_scores(&mut col)?;
    std::thread::sleep(std::time::Duration::from_secs(20));
    instant_pass(&mut col)?;
    DecksService::deck_tree(&mut col, DeckTreeRequest { now })?;
    report(
        "incremental vs itself 20 s later",
        &incremental,
        &all_scores(&mut col)?,
    );
    println!(
        "answered {answered:?}; rescored {} cards in {} ms",
        pass.scored,
        pass.step_micros / 1000
    );
    for cid in &answered {
        let find = |scores: &[(i64, f32)]| scores.iter().find(|(id, _)| id == cid).map(|(_, r)| *r);
        println!("  {cid}: {:?} -> {:?}", find(&before), find(&incremental));
    }
    col.close(None)?;

    // A new start with the state file (saved before these answers: only the
    // new reviews are replayed), then one without it (the whole history).
    let state_file = std::path::Path::new(col_path).with_extension("rwkv-offline");
    for label in ["start with the state file", "fresh replay"] {
        if label == "fresh replay" {
            std::fs::remove_file(&state_file)?;
        }
        let mut col = CollectionBuilder::new(col_path).build()?;
        prepare_and_wait(&mut col, model_path)?;
        instant_pass(&mut col)?;
        DecksService::deck_tree(&mut col, DeckTreeRequest { now })?;
        let other = all_scores(&mut col)?;
        report(&format!("incremental vs {label}"), &incremental, &other);
        col.close(None)?;
    }

    // Undo of an answer: the history changed under the state, so it is
    // rebuilt on its own thread; the deck list must not wait for it.
    let mut col = CollectionBuilder::new(col_path).build()?;
    prepare_and_wait(&mut col, model_path)?;
    DecksService::deck_tree(&mut col, DeckTreeRequest { now })?;
    let before_answer = all_scores(&mut col)?;
    let cid = answered[0];
    let states =
        SchedulerService::get_scheduling_states(&mut col, anki_proto::cards::CardId { cid })?;
    answer(
        &mut col,
        cid,
        &states,
        states.good.clone(),
        2,
        now * 1000 + 100,
    )?;
    DecksService::deck_tree(&mut col, DeckTreeRequest { now })?;
    col.undo()?;
    let started = Instant::now();
    DecksService::deck_tree(&mut col, DeckTreeRequest { now })?;
    let undo_ms = started.elapsed().as_millis();
    let started = Instant::now();
    wait_ready(&mut col)?;
    println!(
        "undo: deck list after {undo_ms} ms, rebuilt in the background in {} ms",
        started.elapsed().as_millis()
    );
    assert!(
        undo_ms < 2_000,
        "deck list waited for the replay after undo"
    );
    DecksService::deck_tree(&mut col, DeckTreeRequest { now })?;
    report(
        "before the answer vs after its undo",
        &before_answer,
        &all_scores(&mut col)?,
    );
    col.close(None)?;
    Ok(())
}

type Col = anki::collection::Collection;
type Progress = anki_proto::scheduler::RwkvOfflineInstantPassProgress;

/// The prepare call returns while the runtime is still being built on its own
/// thread (as on the phone); this waits for it, so the checks see RWKV scores.
fn prepare_and_wait(col: &mut Col, model_path: &str) -> anki::error::Result<()> {
    SchedulerService::rwkv_prepare_offline(
        col,
        RwkvPrepareOfflineRequest {
            model_path: model_path.into(),
            state_precision: state_precision(),
        },
    )?;
    wait_ready(col)
}

/// Waits for a runtime being built on its own thread (after prepare or undo).
/// A model that fails to load disables RWKV without a status of its own: give
/// up after 10 minutes instead of waiting forever.
fn wait_ready(col: &mut Col) -> anki::error::Result<()> {
    let started = Instant::now();
    while !replay_status(col)?.available {
        assert!(
            started.elapsed().as_secs() < 600,
            "RWKV not ready after 10 minutes (did the model load?)"
        );
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    Ok(())
}

fn replay_status(col: &mut Col) -> anki::error::Result<Progress> {
    SchedulerService::rwkv_offline_instant_pass_step(
        col,
        RwkvOfflineInstantPassStepRequest {
            status_only: true,
            ..Default::default()
        },
    )
}

fn instant_pass(col: &mut Col) -> anki::error::Result<Progress> {
    SchedulerService::rwkv_offline_instant_pass_step(
        col,
        RwkvOfflineInstantPassStepRequest::default(),
    )
}

/// Answers `card_id` with `new_state` (`rating` 0 Again .. 3 Easy), 5 s taken,
/// as the app does.
fn answer(
    col: &mut Col,
    card_id: i64,
    states: &anki_proto::scheduler::SchedulingStates,
    new_state: Option<anki_proto::scheduler::SchedulingState>,
    rating: i32,
    answered_at_millis: i64,
) -> anki::error::Result<()> {
    SchedulerService::answer_card(
        col,
        anki_proto::scheduler::CardAnswer {
            card_id,
            current_state: states.current.clone(),
            new_state,
            rating,
            answered_at_millis,
            milliseconds_taken: 5_000,
            ..Default::default()
        },
    )?;
    Ok(())
}

/// How many cards differ between two score lists, and the worst ones.
fn report(label: &str, a: &[(i64, f32)], b: &[(i64, f32)]) {
    assert_eq!(a.len(), b.len());
    let mut diffs: Vec<(f32, i64, f32, f32)> = a
        .iter()
        .zip(b)
        .map(|((a_id, a), (b_id, b))| {
            assert_eq!(a_id, b_id);
            ((a - b).abs(), *a_id, *a, *b)
        })
        .collect();
    diffs.sort_by(|x, y| y.0.total_cmp(&x.0));
    let over = |t: f32| diffs.iter().filter(|d| d.0 > t).count();
    println!(
        "{label}: {} cards; max {:.6}; over 0.0001: {}, 0.001: {}, 0.01: {}, 0.05: {}",
        diffs.len(),
        diffs[0].0,
        over(0.0001),
        over(0.001),
        over(0.01),
        over(0.05)
    );
    for (diff, cid, a, b) in diffs.iter().take(12).filter(|d| d.0 > 0.001) {
        println!("  WORST {cid} {a:.4} {b:.4} diff {diff:.4}");
    }
}

/// Review 30 cards of `deck` (Good) the way the app does, timing the fetch of
/// the next card and the answer separately.
fn lag_check(
    mut col: anki::collection::Collection,
    deck: i64,
    now: i64,
    answers: i64,
) -> Result<(), Box<dyn std::error::Error>> {
    col.set_current_deck(anki::decks::DeckId(deck))?;
    {
        let config_id = col
            .get_deck(anki::decks::DeckId(deck))?
            .unwrap()
            .config_id()
            .unwrap();
        let config = col.get_deck_config(config_id, false)?.unwrap();
        println!(
            "LAG preset {}: instant {} refresh_interval {} candidate_refresh {} batch {}",
            config.name,
            config.inner.rwkv_review_instant_order_enabled,
            config.inner.rwkv_review_refresh_interval,
            config.inner.rwkv_review_candidate_refresh_enabled,
            config.inner.rwkv_review_batch_size
        );
    }
    let request = anki_proto::scheduler::GetQueuedCardsRequest {
        fetch_limit: 1,
        ..Default::default()
    };
    for index in 0..answers {
        let start = Instant::now();
        let queued = SchedulerService::get_queued_cards(&mut col, request.clone())?;
        let fetch_ms = start.elapsed().as_millis();
        let Some(card) = queued.cards.first() else {
            break;
        };
        let card_id = card.card.as_ref().unwrap().id;
        let states = card.states.clone().unwrap();
        let start = Instant::now();
        answer(
            &mut col,
            card_id,
            &states,
            states.good.clone(),
            2,
            (now + index) * 1000,
        )?;
        println!(
            "LAG card {index}: fetch {fetch_ms} ms, answer {} ms (queue {})",
            start.elapsed().as_millis(),
            card.queue
        );
    }
    let start = Instant::now();
    DecksService::deck_tree(&mut col, DeckTreeRequest { now: now + 30 })?;
    println!("LAG deck list after: {} ms", start.elapsed().as_millis());
    Ok(())
}

fn tree_review_count(node: &anki_proto::decks::DeckTreeNode, deck: i64) -> Option<u32> {
    if node.deck_id == deck {
        return Some(node.review_count);
    }
    node.children
        .iter()
        .find_map(|child| tree_review_count(child, deck))
}

/// The deck list's review count for `deck` next to what the study queue
/// holds, before each answer (real time, 2 s apart), until the queue is empty.
fn probe_check(
    mut col: anki::collection::Collection,
    deck: i64,
    _now: i64,
    rating: i32,
) -> Result<(), Box<dyn std::error::Error>> {
    col.set_current_deck(anki::decks::DeckId(deck))?;
    let request = anki_proto::scheduler::GetQueuedCardsRequest {
        fetch_limit: 1,
        ..Default::default()
    };
    for index in 0..40 {
        let now = anki::timestamp::TimestampSecs::now().0;
        let start = Instant::now();
        let tree = DecksService::deck_tree(&mut col, DeckTreeRequest { now })?;
        let tree_ms = start.elapsed().as_millis();
        let start = Instant::now();
        // the deck list's forecast line (RwkvForecast.kt OFFSETS)
        SchedulerService::rwkv_offline_forecast(
            &mut col,
            RwkvOfflineForecastRequest {
                search: String::new(),
                offsets_secs: vec![0, 3600, 3 * 3600, -1],
            },
        )?;
        let forecast_ms = start.elapsed().as_millis();
        let start = Instant::now();
        SchedulerService::rwkv_offline_forecast(
            &mut col,
            RwkvOfflineForecastRequest {
                search: String::new(),
                offsets_secs: vec![0],
            },
        )?;
        println!(
            "PROBE forecast now only: {} ms",
            start.elapsed().as_millis()
        );
        let listed = tree_review_count(&tree, deck);
        let start = Instant::now();
        let queued = SchedulerService::get_queued_cards(&mut col, request.clone())?;
        let queue_ms = start.elapsed().as_millis();
        let top = queued.cards.first().map(|c| c.card.as_ref().unwrap().id);
        println!(
            "PROBE {index}: deck list {listed:?} ({tree_ms} ms, forecast {forecast_ms} ms) | queue new {} learn {} review {} ({queue_ms} ms) | top {top:?}",
            queued.new_count, queued.learning_count, queued.review_count
        );
        let Some(card) = queued.cards.first() else {
            break;
        };
        let states = card.states.clone().unwrap();
        let new_state = if rating == 1 {
            states.again.clone()
        } else {
            states.good.clone()
        };
        answer(
            &mut col,
            top.unwrap(),
            &states,
            new_state,
            rating - 1,
            anki::timestamp::TimestampMillis::now().0,
        )?;
        std::thread::sleep(std::time::Duration::from_secs(2));
    }
    Ok(())
}

/// RWKV-Curve on `deck`, its preset switched to Curve in this copy: the answer
/// buttons of the first `cards` cards of its queue with FSRS and with Curve
/// (and how long Curve's took), then a Good answer on the first, whose
/// stability must be Curve's S90 for Good, not FSRS's.
fn curve_check(
    mut col: Col,
    deck: i64,
    now: i64,
    cards: u32,
) -> Result<(), Box<dyn std::error::Error>> {
    use anki::services::CardsService;
    col.set_current_deck(anki::decks::DeckId(deck))?;
    let set_curve = |col: &mut Col, on: bool| use_curve(col, deck, on);
    let queued = SchedulerService::get_queued_cards(
        &mut col,
        anki_proto::scheduler::GetQueuedCardsRequest {
            fetch_limit: cards,
            ..Default::default()
        },
    )?;
    let ids: Vec<_> = queued
        .cards
        .iter()
        .map(|c| anki::card::CardId(c.card.as_ref().unwrap().id))
        .collect();
    let labels = |col: &mut Col, cid| -> Result<(Vec<String>, u128), Box<dyn std::error::Error>> {
        let start = Instant::now();
        let states = col.get_scheduling_states(cid)?;
        let ms = start.elapsed().as_millis();
        Ok((col.describe_next_states(&states)?, ms))
    };
    // one switch per pass: a preset change makes the next RWKV call check the
    // history, which isn't Curve's cost
    set_curve(&mut col, false)?;
    let fsrs: Vec<_> = ids
        .iter()
        .map(|&cid| labels(&mut col, cid))
        .collect::<Result<_, _>>()?;
    set_curve(&mut col, true)?;
    labels(&mut col, ids[0])?;
    for (&cid, (fsrs, _)) in ids.iter().zip(fsrs) {
        let (curve, ms) = labels(&mut col, cid)?;
        println!("CURVE {cid}: FSRS {fsrs:?} | Curve {curve:?} ({ms} ms)");
    }
    let Some(&cid) = ids.first() else {
        return Ok(());
    };
    let stability = |col: &mut Col, set: bool| -> Result<f32, Box<dyn std::error::Error>> {
        set_curve(col, set)?;
        let states =
            SchedulerService::get_scheduling_states(col, anki_proto::cards::CardId { cid: cid.0 })?;
        answer(col, cid.0, &states, states.good.clone(), 2, now * 1000)?;
        let card = CardsService::get_card(col, anki_proto::cards::CardId { cid: cid.0 })?;
        let stability = card.memory_state.map_or(0.0, |m| m.stability);
        col.undo()?;
        Ok(stability)
    };
    // Curve first: the undo changes the history under the model state, which
    // is then rebuilt in the background (Curve falls back to FSRS meanwhile)
    let curve = stability(&mut col, true)?;
    let fsrs = stability(&mut col, false)?;
    println!("CURVE Good on {cid}: stability FSRS {fsrs:.3} d, Curve (S90) {curve:.3} d");
    Ok(())
}

/// Switches `deck`'s preset to RWKV-Curve (or back to FSRS) in this copy.
fn use_curve(col: &mut Col, deck: i64, on: bool) -> Result<(), Box<dyn std::error::Error>> {
    use anki::services::DeckConfigService;
    let dcid = col
        .get_deck(anki::decks::DeckId(deck))?
        .unwrap()
        .config_id()
        .unwrap()
        .0;
    let json = DeckConfigService::get_deck_config_legacy(
        col,
        anki_proto::deck_config::DeckConfigId { dcid },
    )?;
    let mut config: serde_json::Value = serde_json::from_slice(&json.json)?;
    config["rwkvReviewEnabled"] = on.into();
    config["rwkvReviewInstantOrderEnabled"] = false.into();
    DeckConfigService::add_or_update_deck_config_legacy(
        col,
        anki_proto::generic::Json {
            json: serde_json::to_vec(&config)?,
        },
    )?;
    Ok(())
}

/// "Reschedule Cards with RWKV-Curve Intervals" on `deck` (switched to
/// Curve in this copy): how many cards changed, and a few of them before and
/// after (interval, due day, stability).
fn curve_reschedule_check(mut col: Col, deck: i64) -> Result<(), Box<dyn std::error::Error>> {
    use anki::services::CardsService;
    use_curve(&mut col, deck, true)?;
    let ids: Vec<i64> = col
        .search_cards(
            &format!("did:{deck} is:review -is:learn -is:suspended"),
            anki::search::SortMode::NoOrder,
        )?
        .into_iter()
        .take(6)
        .map(|cid| cid.0)
        .collect();
    let show = |col: &mut Col, cid: i64| -> Result<String, Box<dyn std::error::Error>> {
        let card = CardsService::get_card(col, anki_proto::cards::CardId { cid })?;
        Ok(format!(
            "ivl {} due {} stability {:.2}",
            card.interval,
            card.due,
            card.memory_state.map_or(0.0, |m| m.stability)
        ))
    };
    let before: Vec<_> = ids
        .iter()
        .map(|&cid| show(&mut col, cid))
        .collect::<Result<_, _>>()?;
    let start = Instant::now();
    let changed = SchedulerService::rwkv_offline_curve_reschedule(
        &mut col,
        anki_proto::decks::DeckId { did: deck },
    )?;
    println!(
        "RESCHEDULE {} cards in {} ms",
        changed.count,
        start.elapsed().as_millis()
    );
    for (cid, before) in ids.iter().zip(before) {
        println!("RESCHEDULE {cid}: {before} -> {}", show(&mut col, *cid)?);
    }
    Ok(())
}

/// KUMA3_RWKV_PRECISION (f32, int8, int4); unset: the app's default (int8).
fn state_precision() -> String {
    std::env::var("KUMA3_RWKV_PRECISION").unwrap_or_default()
}
