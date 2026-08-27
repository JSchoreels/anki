// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::env;
use std::fs;
use std::path::Path;
use std::path::PathBuf;
use std::time::Instant;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;

use anki::rwkv::ReviewInput;
use anki::rwkv::ReviewPredictionRequest;
use anki::rwkv::ReviewStateOwned;
use anki::rwkv::RwkvInference;
use rusqlite::Connection;
use rusqlite::OpenFlags;

#[path = "../rwkv/collection_benchmark.rs"]
mod collection_benchmark;

use collection_benchmark::deck_config_ids_by_deck;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse()?;

    let load_start = Instant::now();
    let mut inference = RwkvInference::load(
        args.weights.clone(),
        args.target_retention,
        args.max_interval_days,
    )?;
    let load_ms = elapsed_ms(load_start);

    let workload_start = Instant::now();
    let workload = match &args.collection {
        Some(path) => CollectionWorkload::load(
            path,
            args.warmup_reviews,
            args.queries,
            args.target_retention,
            args.deck_id,
        )?,
        None => CollectionWorkload {
            source: "synthetic".into(),
            warmup_reviews: synthetic_warmup_reviews(args.warmup_reviews, args.queries),
            query_inputs: synthetic_query_inputs(0, args.queries, args.queries),
        },
    };
    let workload_ms = elapsed_ms(workload_start);

    let CollectionWorkload {
        source,
        warmup_reviews,
        query_inputs,
    } = workload;
    let warmup_count = warmup_reviews.len();
    let query_count = query_inputs.len();
    let original_metric_reviews = args.metrics.then(|| warmup_reviews.clone());
    let (warmup_reviews, warmup_original_indices) =
        mix_warmup_windows(warmup_reviews, args.warmup_mix_window);
    let mut warmup_reviews = Some(warmup_reviews);
    let warmup_start = Instant::now();
    let mut checkpoint_metrics = None;
    let recall_metrics = if let Some(checkpoint_mode) = args.checkpoint_mode {
        if args.metrics {
            return Err("--metrics cannot be combined with --checkpoint-mode".into());
        }
        checkpoint_metrics = Some(warm_up_with_checkpoints(
            &mut inference,
            warmup_reviews.take().unwrap(),
            checkpoint_mode,
            args.checkpoint_policy,
            args.checkpoint_max_age_days,
            args.checkpoint_restore,
            args.checkpoint_recovery_age_days,
        )?);
        None
    } else if args.metrics {
        let metric_reviews = original_metric_reviews.expect("metric reviews");
        let recall_labels = recall_labels(&metric_reviews);
        let recall_bins = recall_bins(&metric_reviews);
        let reviews = warmup_reviews.take().unwrap();
        let predictions = remap_prediction_indices(
            inference.warm_up_reviews(reviews, true)?,
            &warmup_original_indices,
        );
        Some(RecallMetrics::from_predictions(
            &recall_labels,
            &recall_bins,
            &predictions,
        ))
    } else {
        None
    };
    if let Some(reviews) = warmup_reviews.take() {
        inference.warm_up_reviews(reviews, false)?;
    }
    let warmup_ms = elapsed_ms(warmup_start);

    let snapshot_start = Instant::now();
    let snapshot = if args.resident_state {
        StateSnapshot::default()
    } else {
        StateSnapshot::from_inference(&inference)
    };
    let snapshot_ms = elapsed_ms(snapshot_start);

    for (index, batch_size) in args.batch_sizes.iter().copied().enumerate() {
        if index > 0 {
            println!("---");
        }
        let timing = run_prediction_timing(
            &mut inference,
            &query_inputs,
            &snapshot,
            batch_size,
            args.repeat,
            args.retrievability_only,
            args.resident_state,
        )?;

        println!("weights={}", args.weights.display());
        println!("source={source}");
        if let Some(collection) = &args.collection {
            println!("collection={}", collection.display());
        }
        println!("queries={query_count}");
        println!("batch_size={batch_size}");
        println!("warmup_reviews={warmup_count}");
        println!("warmup_mix_window={}", args.warmup_mix_window.unwrap_or(0));
        println!("repeat={}", args.repeat);
        if let Some(deck_id) = args.deck_id {
            println!("deck_id={deck_id}");
        }
        println!(
            "prediction_mode={}",
            if args.resident_state {
                "retrievability_only_resident_state"
            } else if args.retrievability_only {
                "retrievability_only"
            } else {
                "full"
            }
        );
        println!("target_retention={:.6}", args.target_retention);
        println!("max_interval_days={}", args.max_interval_days);
        println!("load_ms={load_ms:.3}");
        println!("workload_ms={workload_ms:.3}");
        println!("warmup_ms={warmup_ms:.3}");
        if let Some(metrics) = &checkpoint_metrics {
            println!("checkpoint_mode={}", metrics.mode.name());
            println!("checkpoint_policy={}", metrics.policy.name());
            println!("checkpoint_count={}", metrics.checkpoint_count);
            println!("checkpoint_write_ms={:.3}", metrics.write_ms);
            println!("checkpoint_bytes={}", metrics.bytes);
            if let Some(state_bytes) = metrics.state_bytes {
                println!("checkpoint_state_bytes={state_bytes}");
            }
            if let Some(runtime_bytes) = metrics.runtime_bytes {
                println!("checkpoint_runtime_bytes={runtime_bytes}");
            }
            if let Some(restore_ms) = metrics.restore_ms {
                println!("checkpoint_restore_ms={restore_ms:.3}");
            }
            if let Some(recovery) = &metrics.recovery {
                println!("checkpoint_recovery_age_days={}", recovery.age_days);
                println!("checkpoint_recovery_base_reviews={}", recovery.base_reviews);
                println!(
                    "checkpoint_recovery_replay_reviews={}",
                    recovery.replay_reviews
                );
                println!("checkpoint_recovery_restore_ms={:.3}", recovery.restore_ms);
                println!("checkpoint_recovery_replay_ms={:.3}", recovery.replay_ms);
                println!(
                    "checkpoint_recovery_total_ms={:.3}",
                    recovery.restore_ms + recovery.replay_ms
                );
            }
        }
        println!("snapshot_ms={snapshot_ms:.3}");
        if let Some(metrics) = &recall_metrics {
            println!("eval_predictions={}", metrics.predictions);
            println!("eval_successes={}", metrics.successes);
            println!("eval_observed_success={:.9}", metrics.observed_success);
            println!("eval_predicted_recall={:.9}", metrics.predicted_recall);
            println!("eval_logloss={:.9}", metrics.logloss);
            println!("eval_brier={:.9}", metrics.brier);
            println!("eval_rmse={:.9}", metrics.rmse);
            println!("eval_bins={}", metrics.bins);
            println!("eval_rmse_bins={:.9}", metrics.rmse_bins);
            println!("eval_mae={:.9}", metrics.mae);
        }
        println!("request_build_ms={:.3}", timing.request_build_ms);
        println!("predict_ms={:.3}", timing.predict_ms);
        println!("total_ms={:.3}", timing.total_ms);
        println!(
            "per_query_predict_ms={:.6}",
            timing.predict_ms / timing.predictions.max(1) as f64
        );
        println!(
            "per_query_total_ms={:.6}",
            timing.total_ms / timing.predictions.max(1) as f64
        );
        println!("predictions={}", timing.predictions);
        println!("checksum={:.9}", timing.checksum);
    }

    Ok(())
}

fn run_prediction_timing(
    inference: &mut RwkvInference,
    query_inputs: &[ReviewInput],
    snapshot: &StateSnapshot,
    batch_size: usize,
    repeat: usize,
    retrievability_only: bool,
    resident_state: bool,
) -> Result<PredictionTiming, Box<dyn std::error::Error>> {
    let query_count = query_inputs.len();
    let mut total_build_ms = 0.0;
    let mut total_predict_ms = 0.0;
    let mut total_predictions = 0_usize;
    let mut checksum = 0.0_f64;
    let total_start = Instant::now();

    for _ in 0..repeat {
        for offset in (0..query_count).step_by(batch_size) {
            let size = batch_size.min(query_count - offset);
            let build_start = Instant::now();
            let batch_inputs = &query_inputs[offset..offset + size];
            let requests = (!resident_state).then(|| query_requests(batch_inputs, snapshot));
            total_build_ms += elapsed_ms(build_start);

            let predict_start = Instant::now();
            let retrievabilities = if resident_state {
                inference.predict_retrievability_many_from_warm_up(batch_inputs.to_vec())?
            } else if retrievability_only {
                inference.predict_retrievability_many(requests.unwrap())?
            } else {
                inference
                    .predict_many(requests.unwrap())?
                    .into_iter()
                    .map(|output| output.retrievability)
                    .collect()
            };
            total_predict_ms += elapsed_ms(predict_start);
            total_predictions += retrievabilities.len();
            checksum += retrievabilities
                .iter()
                .map(|retrievability| *retrievability as f64)
                .sum::<f64>();
        }
    }

    Ok(PredictionTiming {
        request_build_ms: total_build_ms,
        predict_ms: total_predict_ms,
        total_ms: elapsed_ms(total_start),
        predictions: total_predictions,
        checksum,
    })
}

fn elapsed_ms(start: Instant) -> f64 {
    start.elapsed().as_secs_f64() * 1000.0
}

struct PredictionTiming {
    request_build_ms: f64,
    predict_ms: f64,
    total_ms: f64,
    predictions: usize,
    checksum: f64,
}

struct Args {
    weights: PathBuf,
    collection: Option<PathBuf>,
    queries: usize,
    batch_sizes: Vec<usize>,
    warmup_reviews: usize,
    repeat: usize,
    target_retention: f32,
    max_interval_days: u32,
    retrievability_only: bool,
    metrics: bool,
    deck_id: Option<i64>,
    warmup_mix_window: Option<usize>,
    resident_state: bool,
    checkpoint_mode: Option<CheckpointMode>,
    checkpoint_policy: CheckpointPolicy,
    checkpoint_max_age_days: Option<i64>,
    checkpoint_restore: bool,
    checkpoint_recovery_age_days: Option<i64>,
}

#[derive(Clone, Copy)]
enum CheckpointMode {
    Legacy,
    Delta,
}

impl CheckpointMode {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "legacy" => Ok(Self::Legacy),
            "delta" => Ok(Self::Delta),
            _ => Err("--checkpoint-mode must be legacy or delta".into()),
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Legacy => "legacy",
            Self::Delta => "delta",
        }
    }
}

#[derive(Clone, Copy)]
enum CheckpointPolicy {
    Exponential,
    RecoveryBase,
}

impl CheckpointPolicy {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "exponential" => Ok(Self::Exponential),
            "base" => Ok(Self::RecoveryBase),
            _ => Err("--checkpoint-policy must be exponential or base".into()),
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Exponential => "exponential",
            Self::RecoveryBase => "base",
        }
    }
}

impl Args {
    fn parse() -> Result<Self, String> {
        let mut weights = None;
        let mut collection = None;
        let mut queries = 4096_usize;
        let mut batch_sizes = vec![512_usize];
        let mut warmup_reviews = 4096_usize;
        let mut repeat = 3_usize;
        let mut target_retention = 0.9_f32;
        let mut max_interval_days = 36_500_u32;
        let mut retrievability_only = false;
        let mut metrics = false;
        let mut deck_id = None;
        let mut warmup_mix_window = None;
        let mut resident_state = false;
        let mut checkpoint_mode = None;
        let mut checkpoint_policy = CheckpointPolicy::Exponential;
        let mut checkpoint_max_age_days = None;
        let mut checkpoint_restore = false;
        let mut checkpoint_recovery_age_days = None;
        let mut args = env::args().skip(1);

        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--weights" => {
                    weights = Some(PathBuf::from(
                        args.next().ok_or("--weights requires a path")?,
                    ));
                }
                "--collection" => {
                    collection = Some(PathBuf::from(
                        args.next().ok_or("--collection requires a path")?,
                    ));
                }
                "--queries" => {
                    queries = parse_next(&mut args, "--queries")?;
                }
                "--batch-size" => {
                    batch_sizes = vec![parse_next(&mut args, "--batch-size")?];
                }
                "--batch-sizes" => {
                    batch_sizes =
                        parse_batch_sizes(&args.next().ok_or("--batch-sizes requires a value")?)?;
                }
                "--warmup-reviews" => {
                    warmup_reviews = parse_next(&mut args, "--warmup-reviews")?;
                }
                "--repeat" => {
                    repeat = parse_next(&mut args, "--repeat")?;
                }
                "--target-retention" => {
                    target_retention = parse_next(&mut args, "--target-retention")?;
                }
                "--max-interval-days" => {
                    max_interval_days = parse_next(&mut args, "--max-interval-days")?;
                }
                "--retrievability-only" => {
                    retrievability_only = true;
                }
                "--metrics" => {
                    metrics = true;
                }
                "--deck-id" => {
                    deck_id = Some(parse_next(&mut args, "--deck-id")?);
                }
                "--warmup-mix-window" => {
                    warmup_mix_window = Some(parse_next(&mut args, "--warmup-mix-window")?);
                }
                "--resident-state" => {
                    retrievability_only = true;
                    resident_state = true;
                }
                "--checkpoint-mode" => {
                    checkpoint_mode = Some(CheckpointMode::parse(
                        &args.next().ok_or("--checkpoint-mode requires a value")?,
                    )?);
                }
                "--checkpoint-policy" => {
                    checkpoint_policy = CheckpointPolicy::parse(
                        &args.next().ok_or("--checkpoint-policy requires a value")?,
                    )?;
                }
                "--checkpoint-max-age-days" => {
                    checkpoint_max_age_days =
                        Some(parse_next(&mut args, "--checkpoint-max-age-days")?);
                }
                "--checkpoint-restore" => {
                    checkpoint_restore = true;
                }
                "--checkpoint-recovery-age-days" => {
                    checkpoint_recovery_age_days =
                        Some(parse_next(&mut args, "--checkpoint-recovery-age-days")?);
                }
                "--help" | "-h" => return Err(usage()),
                _ => return Err(format!("unknown argument: {arg}\n{}", usage())),
            }
        }

        if queries == 0 {
            return Err("--queries must be greater than zero".into());
        }
        if batch_sizes.is_empty() || batch_sizes.contains(&0) {
            return Err("--batch-size values must be greater than zero".into());
        }
        if repeat == 0 {
            return Err("--repeat must be greater than zero".into());
        }
        if warmup_mix_window == Some(0) {
            return Err("--warmup-mix-window must be greater than zero".into());
        }
        if checkpoint_max_age_days.is_some_and(|days| days <= 0) {
            return Err("--checkpoint-max-age-days must be greater than zero".into());
        }
        if checkpoint_recovery_age_days.is_some_and(|days| days <= 0) {
            return Err("--checkpoint-recovery-age-days must be greater than zero".into());
        }
        if checkpoint_restore && !matches!(checkpoint_mode, Some(CheckpointMode::Delta)) {
            return Err("--checkpoint-restore requires --checkpoint-mode delta".into());
        }
        if checkpoint_recovery_age_days.is_some()
            && !matches!(checkpoint_mode, Some(CheckpointMode::Delta))
        {
            return Err("--checkpoint-recovery-age-days requires --checkpoint-mode delta".into());
        }
        if checkpoint_restore && checkpoint_recovery_age_days.is_some() {
            return Err("--checkpoint-restore cannot be combined with checkpoint recovery".into());
        }

        Ok(Self {
            weights: weights.ok_or("--weights is required")?,
            collection,
            queries,
            batch_sizes,
            warmup_reviews,
            repeat,
            target_retention,
            max_interval_days,
            retrievability_only,
            metrics,
            deck_id,
            warmup_mix_window,
            resident_state,
            checkpoint_mode,
            checkpoint_policy,
            checkpoint_max_age_days,
            checkpoint_restore,
            checkpoint_recovery_age_days,
        })
    }
}

struct CheckpointMetrics {
    mode: CheckpointMode,
    policy: CheckpointPolicy,
    checkpoint_count: usize,
    write_ms: f64,
    bytes: u64,
    state_bytes: Option<u64>,
    runtime_bytes: Option<u64>,
    restore_ms: Option<f64>,
    recovery: Option<CheckpointRecoveryMetrics>,
}

struct CheckpointRecoveryMetrics {
    age_days: i64,
    base_reviews: usize,
    replay_reviews: usize,
    restore_ms: f64,
    replay_ms: f64,
}

fn warm_up_with_checkpoints(
    inference: &mut RwkvInference,
    reviews: Vec<ReviewInput>,
    mode: CheckpointMode,
    policy: CheckpointPolicy,
    checkpoint_max_age_days: Option<i64>,
    restore_checkpoint: bool,
    recovery_age_days: Option<i64>,
) -> Result<CheckpointMetrics, Box<dyn std::error::Error>> {
    let endpoints = state_cache_checkpoint_endpoints(&reviews, policy, checkpoint_max_age_days);
    let temporary_dir = tempfile::tempdir()?;
    let mut processed = 0;
    let mut endpoint_index = 0;
    let mut write_ms = 0.0;
    let mut bytes = 0;
    let mut parent_segment_id = None;
    let mut checkpoint_segments = Vec::new();
    while processed < reviews.len() {
        let mut chunk_end = (processed + 16_384).min(reviews.len());
        if let Some(endpoint) = endpoints.get(endpoint_index) {
            chunk_end = chunk_end.min(*endpoint);
        }
        inference.warm_up_reviews(reviews[processed..chunk_end].to_vec(), false)?;
        processed = chunk_end;
        if endpoints.get(endpoint_index) == Some(&processed) {
            let write_started = Instant::now();
            match mode {
                CheckpointMode::Legacy => {
                    let path = temporary_dir
                        .path()
                        .join(format!("checkpoint-{endpoint_index}.bin"));
                    fs::File::create(&path)?;
                    inference.append_warm_up_snapshot_binary(path.clone())?;
                    bytes += path.metadata()?.len();
                }
                CheckpointMode::Delta => {
                    let segment_id = inference.write_warm_up_state_checkpoint(
                        temporary_dir.path().join("state.sqlite3"),
                        "benchmark",
                        parent_segment_id,
                        processed as i64,
                        processed as i64,
                        "benchmark-history",
                        "benchmark-replay",
                        b"",
                        b"",
                        b"",
                        false,
                        false,
                    )?;
                    parent_segment_id = Some(segment_id);
                    checkpoint_segments.push((processed, segment_id));
                }
            }
            write_ms += elapsed_ms(write_started);
            endpoint_index += 1;
        }
    }
    if matches!(mode, CheckpointMode::Delta) {
        let close_started = Instant::now();
        inference.finish_warm_up_state_checkpoints()?;
        write_ms += elapsed_ms(close_started);
    }
    let (state_bytes, runtime_bytes, restore_ms, recovery) =
        if matches!(mode, CheckpointMode::Delta) {
            let store_path = temporary_dir.path().join("state.sqlite3");
            bytes = store_path.metadata()?.len();
            let connection = Connection::open(store_path)?;
            let state_bytes = connection.query_row(
                "select coalesce(sum(length(state_delta)), 0) from segment_state_chunks",
                [],
                |row| row.get::<_, u64>(0),
            )?;
            let runtime_bytes = connection.query_row(
                "select coalesce(sum(length(runtime_state)), 0) from segments",
                [],
                |row| row.get::<_, u64>(0),
            )?;
            drop(connection);
            let restore_ms = if restore_checkpoint {
                let restore_started = Instant::now();
                inference.restore_warm_up_state_checkpoint(
                    temporary_dir.path().join("state.sqlite3"),
                    "benchmark",
                    parent_segment_id.ok_or("missing final checkpoint segment")?,
                )?;
                Some(elapsed_ms(restore_started))
            } else {
                None
            };
            let recovery = if let Some(age_days) = recovery_age_days {
                Some(benchmark_checkpoint_recovery(
                    inference,
                    &reviews,
                    temporary_dir.path().join("state.sqlite3"),
                    &checkpoint_segments,
                    age_days,
                )?)
            } else {
                None
            };
            (Some(state_bytes), Some(runtime_bytes), restore_ms, recovery)
        } else {
            (None, None, None, None)
        };
    Ok(CheckpointMetrics {
        mode,
        policy,
        checkpoint_count: endpoints.len(),
        write_ms,
        bytes,
        state_bytes,
        runtime_bytes,
        restore_ms,
        recovery,
    })
}

fn benchmark_checkpoint_recovery(
    inference: &mut RwkvInference,
    reviews: &[ReviewInput],
    store_path: PathBuf,
    checkpoint_segments: &[(usize, i64)],
    age_days: i64,
) -> Result<CheckpointRecoveryMetrics, Box<dyn std::error::Error>> {
    let last_day = reviews
        .iter()
        .filter_map(|review| review.day_offset)
        .max()
        .ok_or("checkpoint recovery requires review day offsets")?;
    let change_day = last_day - age_days;
    let unchanged_prefix_reviews = reviews.partition_point(|review| {
        review
            .day_offset
            .is_some_and(|day_offset| day_offset < change_day)
    });
    let (base_reviews, segment_id) = checkpoint_segments
        .iter()
        .copied()
        .filter(|(review_count, _segment_id)| {
            *review_count <= unchanged_prefix_reviews && *review_count < reviews.len()
        })
        .max_by_key(|(review_count, _segment_id)| *review_count)
        .ok_or("no checkpoint precedes the simulated changed review")?;

    let restore_started = Instant::now();
    inference.restore_warm_up_state_checkpoint(store_path, "benchmark", segment_id)?;
    let restore_ms = elapsed_ms(restore_started);
    let replay_started = Instant::now();
    inference.warm_up_reviews(reviews[base_reviews..].to_vec(), false)?;
    let replay_ms = elapsed_ms(replay_started);

    Ok(CheckpointRecoveryMetrics {
        age_days,
        base_reviews,
        replay_reviews: reviews.len() - base_reviews,
        restore_ms,
        replay_ms,
    })
}

fn state_cache_checkpoint_endpoints(
    reviews: &[ReviewInput],
    policy: CheckpointPolicy,
    max_age_days: Option<i64>,
) -> Vec<usize> {
    let Some(first_day) = reviews.iter().filter_map(|review| review.day_offset).min() else {
        return (!reviews.is_empty())
            .then_some(reviews.len())
            .into_iter()
            .collect();
    };
    let Some(last_day) = reviews.iter().filter_map(|review| review.day_offset).max() else {
        return (!reviews.is_empty())
            .then_some(reviews.len())
            .into_iter()
            .collect();
    };
    let mut endpoints = Vec::new();
    let mut add_endpoint = |age_days: i64| {
        if last_day - age_days < first_day {
            return;
        }
        let cutoff = last_day - age_days;
        let endpoint = reviews.partition_point(|review| {
            review
                .day_offset
                .is_some_and(|day_offset| day_offset <= cutoff)
        });
        if endpoint > 0 && endpoint < reviews.len() {
            endpoints.push(endpoint);
        }
    };
    match policy {
        CheckpointPolicy::Exponential => {
            let mut age_days = 1_i64;
            while last_day - age_days >= first_day
                && max_age_days.map_or(true, |max_age_days| age_days <= max_age_days)
            {
                add_endpoint(age_days);
                age_days *= 2;
            }
        }
        CheckpointPolicy::RecoveryBase => {
            add_endpoint(max_age_days.unwrap_or(8));
        }
    }
    endpoints.sort_unstable();
    endpoints.dedup();
    if !reviews.is_empty() {
        endpoints.push(reviews.len());
    }
    endpoints
}

fn parse_batch_sizes(value: &str) -> Result<Vec<usize>, String> {
    let batch_sizes = value
        .split(',')
        .map(|value| {
            value
                .trim()
                .parse()
                .map_err(|_| "--batch-sizes has an invalid value".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    if batch_sizes.is_empty() {
        return Err("--batch-sizes requires at least one value".into());
    }
    Ok(batch_sizes)
}

fn parse_next<T: std::str::FromStr>(
    args: &mut impl Iterator<Item = String>,
    name: &str,
) -> Result<T, String> {
    args.next()
        .ok_or_else(|| format!("{name} requires a value"))?
        .parse()
        .map_err(|_| format!("{name} has an invalid value"))
}

fn usage() -> String {
    "usage: rwkv_predict_bench --weights weights.bin [--collection copy.anki2] \
     [--queries N] [--batch-size N|--batch-sizes N,N] [--warmup-reviews N] [--repeat N] \
     [--target-retention R] [--max-interval-days N] [--retrievability-only] \
     [--metrics] [--deck-id ID] [--warmup-mix-window N] \
     [--resident-state] [--checkpoint-mode legacy|delta] \
     [--checkpoint-policy exponential|base] [--checkpoint-max-age-days N] \
     [--checkpoint-restore|--checkpoint-recovery-age-days N]\n\
     With --collection, --warmup-reviews 0 replays all eligible review history."
        .into()
}

fn mix_warmup_windows(
    mut reviews: Vec<ReviewInput>,
    window: Option<usize>,
) -> (Vec<ReviewInput>, Vec<usize>) {
    let mut original_indices = (0..reviews.len()).collect::<Vec<_>>();
    let Some(window) = window.filter(|window| *window > 1) else {
        return (reviews, original_indices);
    };

    for start in (0..reviews.len()).step_by(window) {
        let end = (start + window).min(reviews.len());
        let mut pairs = reviews[start..end]
            .iter()
            .cloned()
            .zip(original_indices[start..end].iter().copied())
            .collect::<Vec<_>>();
        pairs.sort_by_key(|(review, index)| warmup_mix_key(review, *index));
        for (offset, (review, index)) in pairs.into_iter().enumerate() {
            reviews[start + offset] = review;
            original_indices[start + offset] = index;
        }
    }

    (reviews, original_indices)
}

fn warmup_mix_key(review: &ReviewInput, index: usize) -> u64 {
    let mut value = index as u64;
    value ^= (review.card_id as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15);
    value ^= (review.deck_id.unwrap_or_default() as u64).rotate_left(17);
    value ^= (review.preset_id.unwrap_or_default() as u64).rotate_left(31);
    value ^= u64::from(review.ease.unwrap_or_default()).rotate_left(47);
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}

fn remap_prediction_indices(
    predictions: Vec<(usize, f32)>,
    original_indices: &[usize],
) -> Vec<(usize, f32)> {
    predictions
        .into_iter()
        .filter_map(|(index, prediction)| {
            original_indices
                .get(index)
                .copied()
                .map(|original_index| (original_index, prediction))
        })
        .collect()
}

struct RecallMetrics {
    predictions: usize,
    successes: usize,
    observed_success: f64,
    predicted_recall: f64,
    logloss: f64,
    brier: f64,
    rmse: f64,
    bins: usize,
    rmse_bins: f64,
    mae: f64,
}

impl RecallMetrics {
    fn from_predictions(labels: &[bool], bins: &[RecallBin], predictions: &[(usize, f32)]) -> Self {
        let mut successes = 0_usize;
        let mut predicted_sum = 0.0_f64;
        let mut logloss_sum = 0.0_f64;
        let mut squared_error_sum = 0.0_f64;
        let mut absolute_error_sum = 0.0_f64;
        let mut r_matrix: HashMap<RecallBin, RecallBinValue> = HashMap::new();

        for &(index, retrievability) in predictions {
            let success = labels.get(index).copied().unwrap_or(false);
            let observed = if success { 1.0 } else { 0.0 };
            let predicted = (retrievability as f64).clamp(1e-6, 1.0 - 1e-6);
            let error = predicted - observed;

            successes += usize::from(success);
            predicted_sum += predicted;
            logloss_sum += if success {
                -predicted.ln()
            } else {
                -(1.0 - predicted).ln()
            };
            squared_error_sum += error * error;
            absolute_error_sum += error.abs();

            if let Some(bin) = bins.get(index) {
                let value = r_matrix.entry(*bin).or_default();
                value.predicted += predicted;
                value.actual += observed;
                value.count += 1.0;
                value.weight += 1.0;
            }
        }

        let predictions = predictions.len();
        let divisor = predictions.max(1) as f64;
        let brier = squared_error_sum / divisor;
        let (bin_count, rmse_bins) = rmse_bins(&r_matrix);
        Self {
            predictions,
            successes,
            observed_success: successes as f64 / divisor,
            predicted_recall: predicted_sum / divisor,
            logloss: logloss_sum / divisor,
            brier,
            rmse: brier.sqrt(),
            bins: bin_count,
            rmse_bins,
            mae: absolute_error_sum / divisor,
        }
    }
}

fn recall_labels(reviews: &[ReviewInput]) -> Vec<bool> {
    reviews
        .iter()
        .map(|review| review.ease.is_some_and(|ease| ease > 1))
        .collect()
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct RecallBin {
    delta_t_bin: u32,
    length_bin: u32,
    lapse_bin: u32,
}

#[derive(Default)]
struct RecallBinValue {
    predicted: f64,
    actual: f64,
    count: f64,
    weight: f64,
}

fn recall_bins(reviews: &[ReviewInput]) -> Vec<RecallBin> {
    let mut long_term_review_counts_by_card = HashMap::new();
    let mut prior_lapse_counts_by_card = HashMap::new();
    let mut bins = Vec::with_capacity(reviews.len());

    for review in reviews {
        let elapsed_days = review.current_elapsed_days.unwrap_or(-1);
        let is_long_term_review = elapsed_days >= 1;
        let prior_long_term_reviews = long_term_review_counts_by_card
            .get(&review.card_id)
            .copied()
            .unwrap_or(0);
        let prior_lapses = prior_lapse_counts_by_card
            .get(&review.card_id)
            .copied()
            .unwrap_or(0);
        let long_term_reviews = prior_long_term_reviews + u32::from(is_long_term_review);

        bins.push(RecallBin {
            delta_t_bin: fsrs_delta_t_bin(elapsed_days),
            length_bin: fsrs_count_bin(long_term_reviews as f64 + 1.0, 1.99, 1.89),
            lapse_bin: if prior_lapses == 0 {
                0
            } else {
                fsrs_count_bin(prior_lapses as f64, 1.65, 1.73)
            },
        });

        if is_long_term_review {
            long_term_review_counts_by_card.insert(review.card_id, long_term_reviews);
            if review.ease == Some(1) {
                prior_lapse_counts_by_card.insert(review.card_id, prior_lapses + 1);
            }
        }
    }

    bins
}

fn fsrs_delta_t_bin(delta_t: i64) -> u32 {
    if delta_t <= 0 {
        return 0;
    }
    fsrs_count_bin(delta_t as f64, 248.0, 3.62)
}

fn fsrs_count_bin(value: f64, multiplier: f64, base: f64) -> u32 {
    if value <= 0.0 {
        return 0;
    }
    let binned = multiplier * base.powf(value.log(base).floor());
    if binned.is_finite() && binned >= 0.0 {
        binned.round() as u32
    } else {
        0
    }
}

fn rmse_bins(r_matrix: &HashMap<RecallBin, RecallBinValue>) -> (usize, f64) {
    let weight_sum = r_matrix.values().map(|value| value.weight).sum::<f64>();
    if weight_sum == 0.0 {
        return (0, 0.0);
    }

    let squared_error_sum = r_matrix
        .values()
        .map(|value| {
            let predicted = value.predicted / value.count;
            let actual = value.actual / value.count;
            (predicted - actual).powi(2) * value.weight
        })
        .sum::<f64>();

    (r_matrix.len(), (squared_error_sum / weight_sum).sqrt())
}

#[derive(Default)]
struct StateSnapshot {
    card: HashMap<i64, Vec<u8>>,
    note: HashMap<i64, Vec<u8>>,
    deck: HashMap<i64, Vec<u8>>,
    preset: HashMap<i64, Vec<u8>>,
    global: Option<Vec<u8>>,
}

impl StateSnapshot {
    fn from_inference(inference: &RwkvInference) -> Self {
        let snapshot = inference.warm_up_snapshot();
        Self {
            card: snapshot.card_states.into_iter().collect(),
            note: snapshot.note_states.into_iter().collect(),
            deck: snapshot.deck_states.into_iter().collect(),
            preset: snapshot.preset_states.into_iter().collect(),
            global: snapshot.global_state,
        }
    }
}

struct CollectionWorkload {
    source: String,
    warmup_reviews: Vec<ReviewInput>,
    query_inputs: Vec<ReviewInput>,
}

impl CollectionWorkload {
    fn load(
        path: &Path,
        warmup_limit: usize,
        query_limit: usize,
        target_retention: f32,
        deck_id: Option<i64>,
    ) -> rusqlite::Result<Self> {
        let uri = format!("file:{}?mode=ro&immutable=1", path.to_string_lossy());
        let db = Connection::open_with_flags(
            uri,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
        )?;
        let timing = BenchTiming::today();
        let preset_by_deck = deck_config_ids_by_deck(&db)?;
        let warmup_reviews =
            collection_warmup_reviews(&db, warmup_limit, &timing, &preset_by_deck, deck_id)?;
        let query_inputs = collection_query_inputs(
            &db,
            query_limit,
            target_retention,
            &timing,
            &preset_by_deck,
            deck_id,
        )?;
        Ok(Self {
            source: "collection".into(),
            warmup_reviews,
            query_inputs,
        })
    }
}

#[derive(Clone, Copy)]
struct BenchTiming {
    now_secs: i64,
    days_elapsed: i64,
    next_day_at: i64,
}

impl BenchTiming {
    fn today() -> Self {
        let now_secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs() as i64;
        let days_elapsed = now_secs / 86_400;
        Self {
            now_secs,
            days_elapsed,
            next_day_at: (days_elapsed + 1) * 86_400,
        }
    }
}

fn collection_warmup_reviews(
    db: &Connection,
    limit: usize,
    timing: &BenchTiming,
    preset_by_deck: &HashMap<i64, Option<i64>>,
    deck_id: Option<i64>,
) -> rusqlite::Result<Vec<ReviewInput>> {
    let current_deck_id_sql = current_deck_id_sql(db)?;
    let sql = if limit == 0 {
        historical_review_sql(None, current_deck_id_sql, deck_id)
    } else {
        historical_review_sql(Some(limit), current_deck_id_sql, deck_id)
    };
    let mut stmt = db.prepare(&sql)?;
    let rows = stmt.query_map([], |row| {
        Ok(HistoricalReviewRow {
            review_id: row.get(0)?,
            card_id: row.get(1)?,
            note_id: row.get(2)?,
            deck_id: row.get(3)?,
            ease: row.get(4)?,
            duration_millis: row.get(5)?,
            review_kind: row.get(6)?,
            is_learning_start: row.get(7)?,
        })
    })?;

    let mut previous_review_id_by_card = HashMap::new();
    let mut reviews = Vec::new();
    for row in rows {
        let row = row?;
        let previous_review_id = previous_review_id_by_card.insert(row.card_id, row.review_id);
        let day_offset = historical_day_offset(row.review_id, timing);
        let elapsed_seconds =
            previous_review_id.map_or(-1, |previous| ((row.review_id - previous) / 1000).max(0));
        let elapsed_days = previous_review_id.map_or(-1, |previous| {
            (day_offset - historical_day_offset(previous, timing)).max(0)
        });
        reviews.push(ReviewInput {
            card_id: row.card_id,
            note_id: Some(row.note_id),
            deck_id: Some(row.deck_id),
            preset_id: preset_by_deck.get(&row.deck_id).copied().flatten(),
            is_query: false,
            ease: Some(row.ease as u8),
            duration_millis: Some(row.duration_millis),
            card_type: Some(historical_state(row.review_kind, row.is_learning_start)),
            day_offset: Some(day_offset),
            current_elapsed_days: Some(elapsed_days),
            current_elapsed_seconds: Some(elapsed_seconds),
            target_retentions: [Some(0.9), Some(0.9), Some(0.9), Some(0.9)],
            enforce_grade_order: true,
        });
    }

    Ok(reviews)
}

fn historical_review_sql(
    limit: Option<usize>,
    current_deck_id_sql: &str,
    deck_id: Option<i64>,
) -> String {
    let limit_clause = limit.map_or(String::new(), |limit| format!(" limit {limit}"));
    let deck_clause = deck_id.map_or(String::new(), |deck_id| {
        format!(" and {current_deck_id_sql} = {deck_id}")
    });
    format!(
        "
with eligible as (
  select
    r.id,
    r.cid,
    c.nid,
    {current_deck_id_sql} as deck_id,
    r.ease,
    r.time,
    r.type,
    lag(r.type) over (partition by r.cid order by r.id) as previous_type
  from revlog r
  join cards c on c.id = r.cid
  where r.ease between 1 and 4
    and r.type in (0, 1, 2, 3, 4, 5)
    and not (r.type = 3 and r.factor = 0)
    {deck_clause}
), retained_starts as (
  select cid, max(id) as start_id
  from eligible
  where type = 0 and (previous_type is null or previous_type != 0)
  group by cid
)
select
  e.id,
  e.cid,
  e.nid,
  e.deck_id,
  e.ease,
  e.time,
  e.type,
  e.id = s.start_id
from eligible e
join retained_starts s on s.cid = e.cid
where e.id >= s.start_id
order by e.id, e.cid
{limit_clause}"
    )
}

struct HistoricalReviewRow {
    review_id: i64,
    card_id: i64,
    note_id: i64,
    deck_id: i64,
    ease: i64,
    duration_millis: i64,
    review_kind: i64,
    is_learning_start: bool,
}

fn collection_query_inputs(
    db: &Connection,
    limit: usize,
    target_retention: f32,
    timing: &BenchTiming,
    preset_by_deck: &HashMap<i64, Option<i64>>,
    deck_id: Option<i64>,
) -> rusqlite::Result<Vec<ReviewInput>> {
    let current_deck_id_sql = current_deck_id_sql(db)?;
    let deck_clause = deck_id.map_or(String::new(), |deck_id| {
        format!(" and {current_deck_id_sql} = {deck_id}")
    });
    let mut stmt = db.prepare(&format!(
        "
select
  c.id,
  c.nid,
  {current_deck_id_sql} as current_did,
  max(r.id) as last_review_id
from cards c
left join revlog r on r.cid = c.id
  and r.ease between 1 and 4
  and r.type in (0, 1, 2, 3, 4, 5)
  and not (r.type = 3 and r.factor = 0)
where c.type = 2
  and c.queue = 2
  {deck_clause}
group by c.id
order by c.id
limit {limit}"
    ))?;

    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, Option<i64>>(3)?,
        ))
    })?;

    let mut inputs = Vec::new();
    for row in rows {
        let (card_id, note_id, deck_id, last_review_id) = row?;
        let elapsed_seconds = last_review_id
            .map(|id| (timing.now_secs - id / 1000).max(0))
            .unwrap_or(-1);
        let elapsed_days = if elapsed_seconds >= 0 {
            elapsed_seconds / 86_400
        } else {
            -1
        };
        inputs.push(ReviewInput {
            card_id,
            note_id: Some(note_id),
            deck_id: Some(deck_id),
            preset_id: preset_by_deck.get(&deck_id).copied().flatten(),
            is_query: true,
            ease: None,
            duration_millis: None,
            card_type: Some(2),
            day_offset: Some(timing.days_elapsed),
            current_elapsed_days: Some(elapsed_days),
            current_elapsed_seconds: Some(elapsed_seconds),
            target_retentions: [
                Some(target_retention),
                Some(target_retention),
                Some(target_retention),
                Some(target_retention),
            ],
            enforce_grade_order: true,
        });
    }

    Ok(inputs)
}

fn current_deck_id_sql(db: &Connection) -> rusqlite::Result<&'static str> {
    Ok(if table_has_column(db, "cards", "odid")? {
        "case when c.odid != 0 then c.odid else c.did end"
    } else {
        "c.did"
    })
}

fn table_has_column(db: &Connection, table: &str, column: &str) -> rusqlite::Result<bool> {
    let mut stmt = db.prepare(&format!("pragma table_info({table})"))?;
    let columns = stmt.query_map([], |row| row.get::<_, String>(1))?;
    for name in columns {
        if name? == column {
            return Ok(true);
        }
    }
    Ok(false)
}

fn historical_state(review_kind: i64, is_learning_start: bool) -> i64 {
    if is_learning_start {
        0
    } else {
        review_kind + 1
    }
}

fn historical_day_offset(review_id: i64, timing: &BenchTiming) -> i64 {
    let review_secs = review_id / 1000;
    let days_before_today = (timing.next_day_at - 1 - review_secs).max(0) / 86_400;
    (timing.days_elapsed - days_before_today).max(0)
}

fn synthetic_warmup_reviews(count: usize, card_count: usize) -> Vec<ReviewInput> {
    (0..count)
        .map(|index| {
            let card_id = card_id_for_index(index, card_count);
            synthetic_input(index, card_id, false, Some((index % 4 + 1) as u8))
        })
        .collect()
}

fn synthetic_query_inputs(offset: usize, count: usize, card_count: usize) -> Vec<ReviewInput> {
    (offset..offset + count)
        .map(|index| {
            let card_id = card_id_for_index(index, card_count);
            synthetic_input(index, card_id, true, None)
        })
        .collect()
}

fn query_requests(
    inputs: &[ReviewInput],
    snapshot: &StateSnapshot,
) -> Vec<ReviewPredictionRequest> {
    inputs
        .iter()
        .cloned()
        .map(|input| ReviewPredictionRequest {
            state: ReviewStateOwned {
                card: snapshot.card.get(&input.card_id).cloned(),
                note: input
                    .note_id
                    .and_then(|note_id| snapshot.note.get(&note_id).cloned()),
                deck: input
                    .deck_id
                    .and_then(|deck_id| snapshot.deck.get(&deck_id).cloned()),
                preset: input
                    .preset_id
                    .and_then(|preset_id| snapshot.preset.get(&preset_id).cloned()),
                global: snapshot.global.clone(),
            },
            input,
        })
        .collect()
}

fn card_id_for_index(index: usize, card_count: usize) -> i64 {
    (index % card_count) as i64 + 1
}

fn synthetic_input(index: usize, card_id: i64, is_query: bool, ease: Option<u8>) -> ReviewInput {
    ReviewInput {
        card_id,
        note_id: Some(card_id / 2 + 1),
        deck_id: Some(100 + card_id % 8),
        preset_id: Some(1_000 + card_id % 4),
        is_query,
        ease,
        duration_millis: ease.map(|_| 750 + (index % 9) as i64 * 250),
        card_type: Some(2),
        day_offset: Some(30 + (index / 200) as i64),
        current_elapsed_days: Some(1 + (index % 120) as i64),
        current_elapsed_seconds: None,
        target_retentions: [Some(0.9), Some(0.9), Some(0.9), Some(0.9)],
        enforce_grade_order: true,
    }
}
