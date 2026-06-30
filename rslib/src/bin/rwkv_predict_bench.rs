// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::env;
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
        )?,
        None => CollectionWorkload {
            source: "synthetic".into(),
            warmup_reviews: synthetic_warmup_reviews(args.warmup_reviews, args.queries),
            query_inputs: synthetic_query_inputs(0, args.queries, args.queries),
        },
    };
    let workload_ms = elapsed_ms(workload_start);

    let warmup_count = workload.warmup_reviews.len();
    let query_count = workload.query_inputs.len();
    let warmup_start = Instant::now();
    inference.warm_up_reviews(workload.warmup_reviews, false)?;
    let warmup_ms = elapsed_ms(warmup_start);

    let snapshot_start = Instant::now();
    let snapshot = StateSnapshot::from_inference(&inference);
    let snapshot_ms = elapsed_ms(snapshot_start);

    for (index, batch_size) in args.batch_sizes.iter().copied().enumerate() {
        if index > 0 {
            println!("---");
        }
        let timing = run_prediction_timing(
            &mut inference,
            &workload.query_inputs,
            &snapshot,
            batch_size,
            args.repeat,
            args.retrievability_only,
        )?;

        println!("weights={}", args.weights.display());
        println!("source={}", workload.source);
        if let Some(collection) = &args.collection {
            println!("collection={}", collection.display());
        }
        println!("queries={query_count}");
        println!("batch_size={batch_size}");
        println!("warmup_reviews={warmup_count}");
        println!("repeat={}", args.repeat);
        println!(
            "prediction_mode={}",
            if args.retrievability_only {
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
        println!("snapshot_ms={snapshot_ms:.3}");
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
            let requests = query_requests(&query_inputs[offset..offset + size], snapshot);
            total_build_ms += elapsed_ms(build_start);

            let predict_start = Instant::now();
            let retrievabilities = if retrievability_only {
                inference.predict_retrievability_many(requests)?
            } else {
                inference
                    .predict_many(requests)?
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
        })
    }
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
     [--target-retention R] [--max-interval-days N] [--retrievability-only]\n\
     With --collection, --warmup-reviews 0 replays all eligible review history."
        .into()
}

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
    ) -> rusqlite::Result<Self> {
        let uri = format!("file:{}?mode=ro&immutable=1", path.to_string_lossy());
        let db = Connection::open_with_flags(
            uri,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
        )?;
        let timing = BenchTiming::today();
        let warmup_reviews = collection_warmup_reviews(&db, warmup_limit, &timing)?;
        let query_inputs = collection_query_inputs(&db, query_limit, target_retention, &timing)?;
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
) -> rusqlite::Result<Vec<ReviewInput>> {
    let sql = if limit == 0 {
        historical_review_sql(None)
    } else {
        historical_review_sql(Some(limit))
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
        })
    })?;

    let mut previous_review_id_by_card = HashMap::new();
    let mut reviews = Vec::new();
    for row in rows {
        let row = row?;
        let previous_review_id = previous_review_id_by_card.insert(row.card_id, row.review_id);
        let elapsed_seconds =
            previous_review_id.map_or(-1, |previous| ((row.review_id - previous) / 1000).max(0));
        let elapsed_days = if elapsed_seconds >= 0 {
            elapsed_seconds / 86_400
        } else {
            -1
        };
        reviews.push(ReviewInput {
            card_id: row.card_id,
            note_id: Some(row.note_id),
            deck_id: Some(row.deck_id),
            preset_id: Some(row.deck_id),
            is_query: false,
            ease: Some(row.ease as u8),
            duration_millis: Some(row.duration_millis),
            card_type: Some(historical_card_type(row.review_kind)),
            day_offset: Some(historical_day_offset(row.review_id, timing)),
            current_elapsed_days: Some(elapsed_days),
            current_elapsed_seconds: Some(elapsed_seconds),
            target_retentions: [Some(0.9), Some(0.9), Some(0.9), Some(0.9)],
        });
    }

    Ok(reviews)
}

fn historical_review_sql(limit: Option<usize>) -> String {
    let limit_clause = limit.map_or(String::new(), |limit| format!(" limit {limit}"));
    format!(
        "
select
  r.id,
  r.cid,
  c.nid,
  c.did,
  r.ease,
  r.time,
  r.type
from revlog r
join cards c on c.id = r.cid
where r.ease between 1 and 4
  and (r.type in (0, 1, 2, 3) or r.type = 4)
order by r.id, r.cid
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
}

fn collection_query_inputs(
    db: &Connection,
    limit: usize,
    target_retention: f32,
    timing: &BenchTiming,
) -> rusqlite::Result<Vec<ReviewInput>> {
    let mut stmt = db.prepare(&format!(
        "
select
  c.id,
  c.nid,
  case when c.odid != 0 then c.odid else c.did end as current_did,
  max(r.id) as last_review_id
from cards c
left join revlog r on r.cid = c.id
  and r.ease between 1 and 4
  and (r.type in (0, 1, 2, 3) or r.type = 4)
where c.type = 2
  and c.queue = 2
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
            preset_id: Some(deck_id),
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
        });
    }

    Ok(inputs)
}

fn historical_card_type(review_kind: i64) -> i64 {
    match review_kind {
        0 => 1,
        2 => 3,
        _ => 2,
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
    }
}
