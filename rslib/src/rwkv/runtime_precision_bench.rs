// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Offline, fresh-process runtime measurements driven by the Python profiler.

use std::env;
use std::io::Write;
use std::time::Instant;

use serde_json::json;

use super::state_scope_bench::metrics;
use super::state_scope_bench::scope_sizes;
use super::state_scope_bench::Row;
use super::weight_precision_bench::encode;
use super::weight_precision_bench::runtime_with_compute;
use super::*;

fn event(value: serde_json::Value) {
    println!("RWKV_BENCH_EVENT={value}");
    std::io::stdout().flush().unwrap();
    if env::var_os("ANKI_RWKV_RUNTIME_HANDSHAKE").is_some() {
        let mut acknowledgment = String::new();
        assert!(std::io::stdin().read_line(&mut acknowledgment).unwrap() > 0);
    }
}

fn cpu_seconds() -> (f64, f64) {
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
    // SAFETY: getrusage initializes the provided rusage on a successful call.
    assert_eq!(
        unsafe { libc::getrusage(libc::RUSAGE_SELF, usage.as_mut_ptr()) },
        0
    );
    let usage = unsafe { usage.assume_init() };
    let seconds = |value: libc::timeval| value.tv_sec as f64 + value.tv_usec as f64 / 1e6;
    (seconds(usage.ru_utime), seconds(usage.ru_stime))
}

fn state_hash(inference: &RwkvInference) -> String {
    struct DigestWriter(blake3::Hasher);
    impl Write for DigestWriter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.0.update(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    let mut digest = DigestWriter(blake3::Hasher::new());
    for states in [
        &inference.warm_up_states.card,
        &inference.warm_up_states.note,
        &inference.warm_up_states.deck,
        &inference.warm_up_states.preset,
    ] {
        write_snapshot_state_map(&mut digest, states).unwrap();
    }
    write_snapshot_optional_bytes(
        &mut digest,
        inference
            .warm_up_states
            .global
            .as_ref()
            .map(serialize_module_state)
            .as_deref(),
    )
    .unwrap();
    digest.0.finalize().to_hex().to_string()
}

fn phase<T>(name: &str, release_memory: bool, call: impl FnOnce() -> T) -> T {
    event(json!({"kind": "start", "phase": name}));
    let before = cpu_seconds();
    let started = Instant::now();
    let result = call();
    let wall_seconds = started.elapsed().as_secs_f64();
    let after = cpu_seconds();
    let release = Instant::now();
    if release_memory {
        release_free_memory_to_os();
    }
    event(json!({
        "kind": "end", "phase": name, "wall_seconds": wall_seconds,
        "user_cpu_seconds": after.0 - before.0, "system_cpu_seconds": after.1 - before.1,
        "allocator_release_seconds": if release_memory { release.elapsed().as_secs_f64() } else { 0.0 },
    }));
    result
}

fn latest_queries(rows: &[Row], limit: usize) -> Vec<ReviewInput> {
    let day = rows.iter().map(|row| row.8).max().unwrap() + 1;
    let review_id = rows.last().unwrap().0 + 86_400_000;
    let mut seen = HashSet::new();
    rows.iter()
        .rev()
        .filter(|row| seen.insert(row.1))
        .take(limit)
        .map(|row| ReviewInput {
            card_id: row.1,
            note_id: row.2,
            deck_id: row.3,
            preset_id: row.4,
            is_query: true,
            ease: None,
            duration_millis: None,
            card_type: Some(2),
            day_offset: Some(day),
            current_elapsed_days: Some(day - row.8),
            current_elapsed_seconds: Some((review_id - row.0) / 1000),
            target_retentions: [Some(0.9); 4],
            enforce_grade_order: true,
        })
        .collect()
}

#[test]
#[ignore]
fn rwkv_runtime_precision_case() {
    let mode = env::var("ANKI_RWKV_RUNTIME_MODE").unwrap();
    let weights_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin");
    let packed_path = PathBuf::from(env::var("ANKI_RWKV_RUNTIME_PACKED_MODEL").unwrap());
    if mode == "prepare" {
        let packed = encode(&WeightMap::load(&weights_path).unwrap());
        fs::write(&packed_path, &packed).unwrap();
        println!("RWKV_BENCH_PREPARED={}", packed.len());
        return;
    }
    let (precision, compact, resident, shared) = match mode.as_str() {
        "f32" => (StatePrecision::F32, false, false, false),
        "state_int8" => (StatePrecision::Int8, false, false, false),
        "state_int4" => (StatePrecision::Int4, false, false, false),
        "weights_int4_decoded" => (StatePrecision::Int8, true, false, false),
        "weights_int4_resident" => (StatePrecision::Int8, true, true, false),
        "weights_int4_fused" => (StatePrecision::Int8, true, true, false),
        "state_int8_all" => (StatePrecision::Int8, false, false, true),
        _ => panic!("invalid runtime precision mode"),
    };
    let (rows, reviews) = phase("input_preparation", true, || {
        let mut source: serde_json::Value = serde_json::from_slice(
            &fs::read(env::var("ANKI_RWKV_RUNTIME_INPUT").unwrap()).unwrap(),
        )
        .unwrap();
        let rows: Vec<Row> =
            serde_json::from_value(source.as_object_mut().unwrap().remove("rows").unwrap())
                .unwrap();
        let reviews = rows
            .iter()
            .map(|row| ReviewInput {
                card_id: row.1,
                note_id: row.2,
                deck_id: row.3,
                preset_id: row.4,
                is_query: false,
                ease: Some(row.5),
                duration_millis: Some(row.6),
                card_type: Some(row.7),
                day_offset: Some(row.8),
                current_elapsed_days: Some(row.9),
                current_elapsed_seconds: Some(row.10),
                target_retentions: [None; 4],
                enforce_grade_order: true,
            })
            .collect::<Vec<_>>();
        (rows, reviews)
    });
    let load = || {
        let packed = compact.then(|| fs::read(&packed_path).unwrap());
        let mut inference = runtime_with_compute(
            &weights_path,
            packed.as_deref(),
            precision,
            resident,
            mode == "weights_int4_fused",
        );
        if shared {
            for module in &mut Arc::get_mut(&mut inference.model).unwrap().modules {
                module.precision = precision;
            }
        }
        inference
    };
    let mut inference = phase("model_load", true, load);
    let cache = env::var("ANKI_RWKV_RUNTIME_CACHE").ok().map(PathBuf::from);
    let cache_action = env::var("ANKI_RWKV_RUNTIME_CACHE_ACTION").unwrap_or_default();
    if cache_action == "restore" {
        phase("cache_restore", true, || {
            inference
                .restore_warm_up_state_checkpoint(cache.clone().unwrap(), "runtime-bench", 1)
                .unwrap();
        });
    } else {
        phase("state_only_rebuild", true, || {
            for reviews in reviews.chunks(16_384) {
                inference.warm_up_reviews(reviews.to_vec(), false).unwrap();
            }
        });
    }
    if cache_action == "write" {
        let segment = phase("cache_save", true, || {
            let id = inference
                .write_warm_up_state_checkpoint(
                    cache.unwrap(),
                    "runtime-bench",
                    None,
                    rows.last().unwrap().0,
                    rows.len() as i64,
                    "history",
                    "replay",
                    &[],
                    &[],
                    &[],
                    true,
                    true,
                )
                .unwrap();
            inference.finish_warm_up_state_checkpoints().unwrap();
            id
        });
        assert_eq!(segment, 1);
        event(
            json!({"kind": "summary", "mode": mode, "purpose": "cache_save", "scopes": scope_sizes(&inference), "state_blake3": state_hash(&inference)}),
        );
        return;
    }
    let scopes = scope_sizes(&inference);
    let queries = latest_queries(&rows, 4096);
    let features = queries
        .iter()
        .map(|query| inference.features.features_for(query))
        .collect::<Vec<_>>();
    let items = queries
        .iter()
        .zip(&features)
        .map(|(query, features)| ReviewPredictionQueryRef {
            features,
            state: inference.warm_up_states.state_ref(query),
        })
        .collect::<Vec<_>>();
    // Use the actual macOS production GEMM query path explicitly: ordinary
    // cfg(test) inference defaults to scalar queries for reference tests.
    let predictions = inference
        .model
        .review_retrievability_query_refs_batched(&items);
    let mut checksums = Vec::new();
    for index in 0..3 {
        let values = phase(&format!("queue_score_{index}"), true, || {
            inference
                .model
                .review_retrievability_query_refs_batched(&items)
        });
        assert_eq!(values, predictions);
        checksums.push(values.iter().map(|&p| p as f64).sum::<f64>());
    }
    drop(items);
    let (samples, _) = phase("single_answer_updates", true, || {
        let mut answer = queries[0].clone();
        answer.is_query = false;
        answer.ease = Some(3);
        answer.duration_millis = Some(1234);
        let state = inference.warm_up_state(&answer);
        let context = inference.state_for_card(answer.card_id);
        let mut samples = Vec::new();
        for _ in 0..20 {
            let before = cpu_seconds();
            let started = Instant::now();
            inference
                .warm_up_reviews(vec![answer.clone()], false)
                .unwrap();
            let seconds = started.elapsed().as_secs_f64();
            let after = cpu_seconds();
            samples.push(json!({"wall_seconds": seconds, "cpu_seconds": after.0 + after.1 - before.0 - before.1}));
            // Restoration is deliberately outside each answer's timed region.
            inference.restore_state(&context);
            inference
                .restore_warm_up_state(
                    answer.card_id,
                    answer.note_id,
                    answer.deck_id,
                    answer.preset_id,
                    ReviewStateOwned {
                        card: state.card.clone(),
                        note: state.note.clone(),
                        deck: state.deck.clone(),
                        preset: state.preset.clone(),
                        global: state.global.clone(),
                    },
                )
                .unwrap();
        }
        (samples, ())
    });
    event(json!({
        "kind": "summary", "mode": mode, "reviews": reviews.len(), "queries": queries.len(),
        "scopes": scopes, "answer_samples": samples, "queue_checksums": checksums,
        "rayon_threads": rayon::current_num_threads(),
        "state_blake3": state_hash(&inference),
    }));
    drop(inference);
    release_free_memory_to_os();
    if let Ok(path) = env::var("ANKI_RWKV_RUNTIME_PREDICTIONS") {
        let mut inference = load();
        let predictions = inference.warm_up_reviews(reviews, true).unwrap();
        assert_eq!(predictions.len(), rows.len());
        let mut output = std::io::BufWriter::new(fs::File::create(path).unwrap());
        for (expected, &(index, prediction)) in predictions.iter().enumerate() {
            assert_eq!(index, expected);
            assert!(prediction.is_finite() && (0.0..=1.0).contains(&prediction));
            output.write_all(&prediction.to_le_bytes()).unwrap();
        }
        output.flush().unwrap();
        event(
            json!({"kind": "quality", "metrics": metrics(&rows, &predictions, &predictions.iter().map(|&(_, p)| p).collect::<Vec<_>>())}),
        );
        println!("RWKV_BENCH_QUALITY_DONE={}", predictions.len());
    }
}
