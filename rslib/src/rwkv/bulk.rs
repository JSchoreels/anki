// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Bulk sequence-mode historical replay.
//!
//! The per-review warm-up path runs the whole model one review at a time, so
//! a full replay is serial even though most of the math has no cross-review
//! dependency. This module processes a warm-up call layer-at-a-time over
//! per-scope streams instead: all pointwise work (norms, lerp mixes,
//! projections, loras, heads) runs in parallel across reviews, and only the
//! small per-stream recurrence sweep stays sequential, parallelized across
//! streams.
//!
//! Pointwise projection stages run through the blocked `Linear` kernel
//! (`Linear::apply_block_into`), which streams each weight column once per
//! block of rows while preserving the per-output-element operation sequence
//! of the per-review path exactly (bias init, ascending-column fused
//! accumulation, identical zero-column skip). Results therefore stay
//! bit-identical to per-review replay: only the loop order changes, never
//! the arithmetic performed for a given review.
//!
//! Stream continuity across calls comes from `ReviewStateMaps`, exactly like
//! the per-review path, so the chunked calls issued by the desktop bridge
//! replay to the same states as one large call.

use std::collections::HashMap;

use rayon::prelude::*;

use super::*;

/// Rows processed per internal chunk. Chunking bounds scratch-buffer memory
/// for very large single calls; the desktop bridge already calls in chunks of
/// at most 1000 reviews.
const BULK_CHUNK_ROWS: usize = 4096;

pub(super) fn warm_up_reviews_bulk(
    inference: &mut RwkvInference,
    reviews: Vec<ReviewInput>,
    record_predictions: bool,
) -> io::Result<Vec<(usize, f32)>> {
    warm_up_reviews_bulk_impl(inference, reviews, record_predictions, BULK_CHUNK_ROWS)
}

/// Test hook: a small chunk size exercises cross-chunk stream continuity
/// without needing thousands of reviews.
#[cfg(test)]
pub(super) fn warm_up_reviews_bulk_chunked(
    inference: &mut RwkvInference,
    reviews: Vec<ReviewInput>,
    record_predictions: bool,
    chunk_rows: usize,
) -> io::Result<Vec<(usize, f32)>> {
    warm_up_reviews_bulk_impl(inference, reviews, record_predictions, chunk_rows.max(1))
}

fn warm_up_reviews_bulk_impl(
    inference: &mut RwkvInference,
    reviews: Vec<ReviewInput>,
    record_predictions: bool,
    chunk_size: usize,
) -> io::Result<Vec<(usize, f32)>> {
    // Feature prepass, in review order. Query features are assigned before
    // answer features for each review so first-seen ID encodings keep the
    // same benchmark-compatible order as the per-review path.
    let mut original_indices = Vec::new();
    let mut inputs = Vec::new();
    let mut answer_features: Vec<f32> = Vec::new();
    let mut query_features: Vec<f32> = Vec::new();
    for (index, input) in reviews.into_iter().enumerate() {
        if input.ease.is_none() {
            continue;
        }
        if record_predictions {
            let mut query_input = input.clone();
            query_input.is_query = true;
            query_input.ease = None;
            query_input.duration_millis = None;
            query_features.extend(inference.features.features_for(&query_input));
        }
        answer_features.extend(inference.features.features_for(&input));
        inference.features.store_review(&input);
        original_indices.push(index);
        inputs.push(input);
    }
    let rows = inputs.len();
    if rows == 0 {
        return Ok(vec![]);
    }

    let model = inference.model.clone();

    // Trunk activations for every row: answer lane, plus a query lane that
    // reads the same pre-review states but never advances them.
    #[cfg(test)]
    let profile_started = rwkv_warmup_profile_start();
    let mut x = feature_mlp_rows(&model, &answer_features, rows);
    let mut x_query = record_predictions.then(|| feature_mlp_rows(&model, &query_features, rows));
    #[cfg(test)]
    rwkv_warmup_profile_record(RwkvWarmupProfileBucket::FeatureMlp, profile_started);

    for (module_id, module) in model.modules.iter().enumerate() {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();
        run_module(
            module,
            module_id,
            &inputs,
            &mut inference.warm_up_states,
            &mut x,
            x_query.as_deref_mut(),
            chunk_size,
        );
        #[cfg(test)]
        rwkv_warmup_profile_record(module_profile_bucket(module_id), profile_started);
    }

    // Recall curves: the per-review path stores one curve per answered
    // review, keyed by card, so only each card's chronologically last row
    // survives.
    #[cfg(test)]
    let profile_started = rwkv_warmup_profile_start();
    let mut last_row_by_card = HashMap::new();
    for (row, input) in inputs.iter().enumerate() {
        last_row_by_card.insert(input.card_id, row);
    }
    let curves: Vec<(i64, ReviewCurve)> = last_row_by_card
        .into_par_iter()
        .map(|(card_id, row_index)| {
            let prehead = model.prehead_norm.apply(row(&x, row_index));
            (card_id, model.curve_head(&prehead))
        })
        .collect();
    for (card_id, curve) in curves {
        inference.curves.insert(card_id, curve);
    }

    let Some(x_query) = x_query else {
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Heads, profile_started);
        return Ok(vec![]);
    };
    let retrievabilities: Vec<f32> = (0..rows)
        .into_par_iter()
        .map(|row_index| {
            let prehead = model.prehead_norm.apply(row(&x_query, row_index));
            model.retrievability_head(&prehead)
        })
        .collect();
    #[cfg(test)]
    rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Heads, profile_started);
    Ok(original_indices.into_iter().zip(retrievabilities).collect())
}

#[cfg(test)]
fn module_profile_bucket(module_id: usize) -> RwkvWarmupProfileBucket {
    match module_id {
        0 => RwkvWarmupProfileBucket::ModuleCard,
        1 => RwkvWarmupProfileBucket::ModuleDeck,
        2 => RwkvWarmupProfileBucket::ModuleNote,
        3 => RwkvWarmupProfileBucket::ModulePreset,
        _ => RwkvWarmupProfileBucket::ModuleGlobal,
    }
}

fn feature_mlp_rows(model: &SrsModel, features: &[f32], rows: usize) -> Vec<f32> {
    let mut out = vec![0.0; rows * D_MODEL];
    out.par_chunks_mut(D_MODEL)
        .enumerate()
        .for_each(|(row_index, out_row)| {
            let features = &features[row_index * CARD_FEATURES..(row_index + 1) * CARD_FEATURES];
            out_row.copy_from_slice(&model.feature_mlp(features));
        });
    out
}

fn row(buffer: &[f32], index: usize) -> &[f32] {
    &buffer[index * D_MODEL..(index + 1) * D_MODEL]
}

/// The recurrent-state scope a module's streams are keyed by. `None` means
/// the review carries no id for this scope: it sees fresh state and nothing
/// is persisted, matching `ReviewStateMaps::{state_ref, store}`.
fn stream_key(module_id: usize, input: &ReviewInput) -> Option<i64> {
    match module_id {
        0 => Some(input.card_id),
        1 => input.deck_id,
        2 => input.note_id,
        3 => input.preset_id,
        _ => Some(0),
    }
}

fn take_stream_state(
    states: &mut ReviewStateMaps,
    module_id: usize,
    key: i64,
) -> Option<ModuleState> {
    match module_id {
        0 => states.card.remove(&key),
        1 => states.deck.remove(&key),
        2 => states.note.remove(&key),
        3 => states.preset.remove(&key),
        _ => states.global.take(),
    }
}

fn put_stream_state(states: &mut ReviewStateMaps, module_id: usize, key: i64, state: ModuleState) {
    match module_id {
        0 => {
            states.card.insert(key, state);
        }
        1 => {
            states.deck.insert(key, state);
        }
        2 => {
            states.note.insert(key, state);
        }
        3 => {
            states.preset.insert(key, state);
        }
        _ => states.global = Some(state),
    }
}

struct StreamPlan {
    key: Option<i64>,
    rows: Vec<u32>,
}

struct ModulePlan {
    streams: Vec<StreamPlan>,
    stream_of_row: Vec<u32>,
    prev_row: Vec<Option<u32>>,
}

impl ModulePlan {
    fn build(module_id: usize, inputs: &[ReviewInput]) -> Self {
        let mut streams: Vec<StreamPlan> = Vec::new();
        let mut stream_of_row = Vec::with_capacity(inputs.len());
        let mut prev_row = Vec::with_capacity(inputs.len());
        let mut stream_by_key: HashMap<i64, u32> = HashMap::new();
        for (row, input) in inputs.iter().enumerate() {
            match stream_key(module_id, input) {
                Some(key) => {
                    let stream_index = *stream_by_key.entry(key).or_insert_with(|| {
                        streams.push(StreamPlan {
                            key: Some(key),
                            rows: Vec::new(),
                        });
                        (streams.len() - 1) as u32
                    });
                    let stream = &mut streams[stream_index as usize];
                    prev_row.push(stream.rows.last().copied());
                    stream.rows.push(row as u32);
                    stream_of_row.push(stream_index);
                }
                None => {
                    streams.push(StreamPlan {
                        key: None,
                        rows: vec![row as u32],
                    });
                    prev_row.push(None);
                    stream_of_row.push((streams.len() - 1) as u32);
                }
            }
        }
        Self {
            streams,
            stream_of_row,
            prev_row,
        }
    }
}

fn layer_states(initial: Option<ModuleState>, layer_count: usize) -> Vec<LayerState> {
    let mut layers = initial.map(|state| state.layers).unwrap_or_default();
    layers.truncate(layer_count);
    while layers.len() < layer_count {
        layers.push(LayerState {
            time: None,
            channel_shift: None,
        });
    }
    layers
}

#[allow(clippy::too_many_arguments)]
fn run_module(
    module: &RwkvModule,
    module_id: usize,
    inputs: &[ReviewInput],
    states: &mut ReviewStateMaps,
    x: &mut [f32],
    mut x_query: Option<&mut [f32]>,
    chunk_size: usize,
) {
    let rows = inputs.len();
    let plan = ModulePlan::build(module_id, inputs);
    let layer_count = module.layers.len();

    let mut stream_layers: Vec<Vec<LayerState>> = plan
        .streams
        .iter()
        .map(|stream| {
            let initial = stream
                .key
                .and_then(|key| take_stream_state(states, module_id, key));
            layer_states(initial, layer_count)
        })
        .collect();

    let mut chunk_start = 0;
    while chunk_start < rows {
        let chunk_end = (chunk_start + chunk_size).min(rows);
        let chunk_rows = chunk_end - chunk_start;

        // Streams with rows in this chunk; row lists are ascending, so the
        // chunk's slice of each stream is contiguous.
        let mut chunk_rows_by_stream: Vec<&[u32]> = vec![&[]; plan.streams.len()];
        let mut chunk_streams = Vec::new();
        for (stream_index, stream) in plan.streams.iter().enumerate() {
            let low = stream
                .rows
                .partition_point(|&row| (row as usize) < chunk_start);
            let high = stream
                .rows
                .partition_point(|&row| (row as usize) < chunk_end);
            if low < high {
                chunk_rows_by_stream[stream_index] = &stream.rows[low..high];
                chunk_streams.push(stream_index);
            }
        }

        let mut v0 = vec![0.0; chunk_rows * D_MODEL];
        let mut v0_query = x_query.as_ref().map(|_| vec![0.0; chunk_rows * D_MODEL]);

        for (layer_id, layer) in module.layers.iter().enumerate() {
            run_layer_chunk(LayerChunk {
                layer,
                layer_id,
                plan: &plan,
                stream_layers: &mut stream_layers,
                chunk_rows_by_stream: &chunk_rows_by_stream,
                chunk_streams: &chunk_streams,
                chunk_start,
                chunk_rows,
                x,
                v0: &mut v0,
                x_query: x_query.as_deref_mut(),
                v0_query: v0_query.as_deref_mut(),
            });
        }

        chunk_start = chunk_end;
    }

    for (stream, layers) in plan.streams.iter().zip(stream_layers) {
        if let Some(key) = stream.key {
            put_stream_state(states, module_id, key, ModuleState { layers });
        }
    }
}

struct LayerChunk<'a> {
    layer: &'a RwkvLayer,
    layer_id: usize,
    plan: &'a ModulePlan,
    stream_layers: &'a mut [Vec<LayerState>],
    chunk_rows_by_stream: &'a [&'a [u32]],
    chunk_streams: &'a [usize],
    chunk_start: usize,
    chunk_rows: usize,
    x: &'a mut [f32],
    v0: &'a mut [f32],
    x_query: Option<&'a mut [f32]>,
    v0_query: Option<&'a mut [f32]>,
}

fn run_layer_chunk(chunk: LayerChunk<'_>) {
    let LayerChunk {
        layer,
        layer_id,
        plan,
        stream_layers,
        chunk_rows_by_stream,
        chunk_streams,
        chunk_start,
        chunk_rows,
        x,
        v0,
        mut x_query,
        mut v0_query,
    } = chunk;
    let mixer = &layer.time_mixer;

    // Time mixer, pointwise pre-recurrence stage.
    let x_norm = normed_rows(&mixer.layer_norm, x, chunk_start, chunk_rows);
    let x_norm_query = x_query
        .as_ref()
        .map(|x_query| normed_rows(&mixer.layer_norm, x_query, chunk_start, chunk_rows));

    let parts = time_parts(
        mixer,
        layer_id,
        plan,
        stream_layers,
        chunk_start,
        chunk_rows,
        &x_norm,
        &x_norm,
        v0,
    );
    if layer_id == 0 {
        fill_v0(v0, &parts);
    }
    let parts_query = x_norm_query.as_ref().map(|x_norm_query| {
        let v0_query = v0_query.take().expect("query v0 buffer");
        let parts_query = time_parts(
            mixer,
            layer_id,
            plan,
            stream_layers,
            chunk_start,
            chunk_rows,
            x_norm_query,
            &x_norm,
            v0_query,
        );
        if layer_id == 0 {
            fill_v0(v0_query, &parts_query);
        }
        parts_query
    });

    // Recurrence sweep: sequential within a stream, parallel across streams.
    // Query rows read the pre-review state; only answer rows advance it.
    let mut recurrence = vec![0.0; chunk_rows * D_MODEL];
    let mut recurrence_query = parts_query
        .as_ref()
        .map(|_| vec![0.0; chunk_rows * D_MODEL]);
    let sweep_outs: Vec<Option<(Vec<f32>, Vec<f32>)>> = stream_layers
        .par_iter_mut()
        .enumerate()
        .map(|(stream_index, layers)| {
            let stream_rows = chunk_rows_by_stream[stream_index];
            if stream_rows.is_empty() {
                return None;
            }
            let layer_state = &mut layers[layer_id];
            let mut matrix = match layer_state.time.take() {
                Some(time) => time.matrix,
                None => vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE],
            };
            let mut outs = vec![0.0; stream_rows.len() * D_MODEL];
            let mut outs_query = if parts_query.is_some() {
                vec![0.0; stream_rows.len() * D_MODEL]
            } else {
                Vec::new()
            };
            for (position, &stream_row) in stream_rows.iter().enumerate() {
                let index = stream_row as usize - chunk_start;
                if let Some(parts_query) = &parts_query {
                    let (out, _) = single_timestep(
                        row(&parts_query.r, index),
                        row(&parts_query.k, index),
                        row(&parts_query.v, index),
                        row(&parts_query.w, index),
                        row(&parts_query.a, index),
                        row(&parts_query.k_deformed, index),
                        &matrix,
                    );
                    outs_query[position * D_MODEL..(position + 1) * D_MODEL].copy_from_slice(&out);
                }
                let (out, next_matrix) = single_timestep(
                    row(&parts.r, index),
                    row(&parts.k, index),
                    row(&parts.v, index),
                    row(&parts.w, index),
                    row(&parts.a, index),
                    row(&parts.k_deformed, index),
                    &matrix,
                );
                matrix = next_matrix;
                outs[position * D_MODEL..(position + 1) * D_MODEL].copy_from_slice(&out);
            }
            if plan.streams[stream_index].key.is_some() {
                let last = *stream_rows.last().unwrap() as usize - chunk_start;
                layer_state.time = Some(TimeState {
                    x_shift: row(&x_norm, last).to_vec(),
                    matrix,
                });
            }
            Some((outs, outs_query))
        })
        .collect();
    for &stream_index in chunk_streams {
        let Some((outs, outs_query)) = &sweep_outs[stream_index] else {
            continue;
        };
        for (position, &stream_row) in chunk_rows_by_stream[stream_index].iter().enumerate() {
            let index = stream_row as usize - chunk_start;
            recurrence[index * D_MODEL..(index + 1) * D_MODEL]
                .copy_from_slice(&outs[position * D_MODEL..(position + 1) * D_MODEL]);
            if let Some(recurrence_query) = &mut recurrence_query {
                recurrence_query[index * D_MODEL..(index + 1) * D_MODEL]
                    .copy_from_slice(&outs_query[position * D_MODEL..(position + 1) * D_MODEL]);
            }
        }
    }

    // Time mixer, pointwise post-recurrence stage.
    let time_out = time_outputs(mixer, &parts, &recurrence, x, chunk_start, chunk_rows);
    let time_out_query = parts_query.as_ref().map(|parts_query| {
        time_outputs(
            mixer,
            parts_query,
            recurrence_query.as_ref().expect("query recurrence"),
            x_query.as_deref().expect("query trunk"),
            chunk_start,
            chunk_rows,
        )
    });

    // Channel mixer. Shift states must stay pre-chunk until both lanes have
    // resolved their shifts.
    let cm = &layer.channel_mixer;
    let cm_norm = normed_rows_from(&cm.layer_norm, &time_out, chunk_rows);
    let cm_norm_query = time_out_query
        .as_ref()
        .map(|time_out_query| normed_rows_from(&cm.layer_norm, time_out_query, chunk_rows));

    channel_outputs(
        cm,
        layer_id,
        plan,
        stream_layers,
        chunk_start,
        chunk_rows,
        &cm_norm,
        &cm_norm,
        &time_out,
        x,
    );
    if let Some(cm_norm_query) = &cm_norm_query {
        channel_outputs(
            cm,
            layer_id,
            plan,
            stream_layers,
            chunk_start,
            chunk_rows,
            cm_norm_query,
            &cm_norm,
            time_out_query.as_ref().expect("query time out"),
            x_query.take().expect("query trunk"),
        );
    }

    for &stream_index in chunk_streams {
        if plan.streams[stream_index].key.is_none() {
            continue;
        }
        let stream_rows = chunk_rows_by_stream[stream_index];
        let last = *stream_rows.last().unwrap() as usize - chunk_start;
        stream_layers[stream_index][layer_id].channel_shift = Some(row(&cm_norm, last).to_vec());
    }
}

fn normed_rows(norm: &Norm, buffer: &[f32], chunk_start: usize, chunk_rows: usize) -> Vec<f32> {
    let mut out = vec![0.0; chunk_rows * D_MODEL];
    out.par_chunks_mut(D_MODEL)
        .enumerate()
        .for_each(|(index, out_row)| {
            norm.apply_into(row(buffer, chunk_start + index), out_row);
        });
    out
}

fn normed_rows_from(norm: &Norm, buffer: &[f32], chunk_rows: usize) -> Vec<f32> {
    normed_rows(norm, buffer, 0, chunk_rows)
}

/// Per-chunk struct-of-arrays buffers for the time mixer's pre-recurrence
/// parts. Each field holds `rows * D_MODEL` values, row-major, so the blocked
/// projection kernels can write rows contiguously.
struct BulkTimeMixParts {
    r: Vec<f32>,
    k: Vec<f32>,
    v: Vec<f32>,
    w: Vec<f32>,
    a: Vec<f32>,
    k_deformed: Vec<f32>,
    g: Vec<f32>,
    next_v0: Vec<f32>,
}

impl BulkTimeMixParts {
    fn zeroed(rows: usize) -> Self {
        Self {
            r: vec![0.0; rows * D_MODEL],
            k: vec![0.0; rows * D_MODEL],
            v: vec![0.0; rows * D_MODEL],
            w: vec![0.0; rows * D_MODEL],
            a: vec![0.0; rows * D_MODEL],
            k_deformed: vec![0.0; rows * D_MODEL],
            g: vec![0.0; rows * D_MODEL],
            next_v0: vec![0.0; rows * D_MODEL],
        }
    }
}

/// One `LINEAR_BLOCK_ROWS`-row block's mutable view over every
/// `BulkTimeMixParts` field.
struct PartsBlockMut<'a> {
    r: &'a mut [f32],
    k: &'a mut [f32],
    v: &'a mut [f32],
    w: &'a mut [f32],
    a: &'a mut [f32],
    k_deformed: &'a mut [f32],
    g: &'a mut [f32],
    next_v0: &'a mut [f32],
}

fn fill_v0(v0: &mut [f32], parts: &BulkTimeMixParts) {
    v0.copy_from_slice(&parts.next_v0);
}

/// Resolves the one-step shift value for a row: the previous same-stream row
/// in this chunk, the persisted stream state, or the row's own normed value
/// when the stream has no state yet.
fn resolve_shift<'a>(
    plan: &ModulePlan,
    row_index: usize,
    chunk_start: usize,
    previous_chunk_values: &'a [f32],
    own: &'a [f32],
    stream_state: impl FnOnce(usize) -> Option<&'a [f32]>,
) -> &'a [f32] {
    match plan.prev_row[row_index] {
        Some(previous) if (previous as usize) >= chunk_start => {
            row(previous_chunk_values, previous as usize - chunk_start)
        }
        _ => {
            let stream_index = plan.stream_of_row[row_index] as usize;
            stream_state(stream_index).unwrap_or(own)
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn time_parts(
    mixer: &TimeMixer,
    layer_id: usize,
    plan: &ModulePlan,
    stream_layers: &[Vec<LayerState>],
    chunk_start: usize,
    chunk_rows: usize,
    x_norm: &[f32],
    x_norm_answer: &[f32],
    v0: &[f32],
) -> BulkTimeMixParts {
    let mut parts = BulkTimeMixParts::zeroed(chunk_rows);
    let block_size = LINEAR_BLOCK_ROWS * D_MODEL;
    parts
        .r
        .par_chunks_mut(block_size)
        .zip(parts.k.par_chunks_mut(block_size))
        .zip(parts.v.par_chunks_mut(block_size))
        .zip(parts.w.par_chunks_mut(block_size))
        .zip(parts.a.par_chunks_mut(block_size))
        .zip(parts.k_deformed.par_chunks_mut(block_size))
        .zip(parts.g.par_chunks_mut(block_size))
        .zip(parts.next_v0.par_chunks_mut(block_size))
        .enumerate()
        .for_each(
            |(block_index, (((((((r, k), v), w), a), k_deformed), g), next_v0))| {
                time_parts_block(
                    mixer,
                    layer_id,
                    plan,
                    stream_layers,
                    chunk_start,
                    block_index * LINEAR_BLOCK_ROWS,
                    x_norm,
                    x_norm_answer,
                    v0,
                    PartsBlockMut {
                        r,
                        k,
                        v,
                        w,
                        a,
                        k_deformed,
                        g,
                        next_v0,
                    },
                );
            },
        );
    parts
}

/// Computes the time-mix parts for one block of rows. Every projection runs
/// through the blocked `Linear` kernel, which performs the identical
/// per-output-element operation sequence as the per-row path, so results stay
/// bit-identical; only the loop order over rows changes.
#[allow(clippy::too_many_arguments)]
fn time_parts_block(
    mixer: &TimeMixer,
    layer_id: usize,
    plan: &ModulePlan,
    stream_layers: &[Vec<LayerState>],
    chunk_start: usize,
    block_start: usize,
    x_norm: &[f32],
    x_norm_answer: &[f32],
    v0: &[f32],
    block: PartsBlockMut<'_>,
) {
    let rows = block.r.len() / D_MODEL;
    debug_assert!(rows <= LINEAR_BLOCK_ROWS);

    // The shift source per row is fixed for the whole layer, so resolve it
    // once and reuse it for every lerp mix.
    let mut shifts: [&[f32]; LINEAR_BLOCK_ROWS] = [&[]; LINEAR_BLOCK_ROWS];
    for (index, shift) in shifts.iter_mut().enumerate().take(rows) {
        let own = row(x_norm, block_start + index);
        *shift = resolve_shift(
            plan,
            chunk_start + block_start + index,
            chunk_start,
            x_norm_answer,
            own,
            |stream_index| {
                stream_layers[stream_index][layer_id]
                    .time
                    .as_ref()
                    .map(|time| time.x_shift.as_slice())
            },
        );
    }
    let fill_mixed = |mix_id: usize, mixed: &mut [f32]| {
        for index in 0..rows {
            let own = row(x_norm, block_start + index);
            fill_time_mixed(
                &mixer.rkvdag_lerp,
                mix_id,
                own,
                shifts[index],
                &mut mixed[index * D_MODEL..(index + 1) * D_MODEL],
            );
        }
    };

    let mut mixed = [0.0f32; LINEAR_BLOCK_ROWS * D_MODEL];
    let mixed = &mut mixed[..rows * D_MODEL];

    fill_mixed(0, mixed);
    mixer.w_r.apply_block_into(mixed, block.r, rows);

    fill_mixed(1, mixed);
    mixer.w_k.apply_block_into(mixed, block.k, rows);

    fill_mixed(6, mixed);
    let mut k_scale = [0.0f32; LINEAR_BLOCK_ROWS * HEADS];
    let k_scale = &mut k_scale[..rows * HEADS];
    mixer.k_scale_linear.apply_block_into(mixed, k_scale, rows);
    sigmoid_in_place(k_scale);

    fill_mixed(7, mixed);
    let mut v_scale = [0.0f32; LINEAR_BLOCK_ROWS * HEADS];
    let v_scale = &mut v_scale[..rows * HEADS];
    mixer.v_scale_linear.apply_block_into(mixed, v_scale, rows);
    sigmoid_in_place(v_scale);

    fill_mixed(2, mixed);
    if mixer.layer_id == 0 {
        mixer.w_v.apply_block_into(mixed, block.v, rows);
        block.next_v0.copy_from_slice(block.v);
    } else {
        mixer.v_lora.apply_sigmoid_block_into(mixed, block.v, rows);
        let mut w_v = [0.0f32; LINEAR_BLOCK_ROWS * D_MODEL];
        let w_v = &mut w_v[..rows * D_MODEL];
        mixer.w_v.apply_block_into(mixed, w_v, rows);
        for index in 0..rows {
            let range = index * D_MODEL..(index + 1) * D_MODEL;
            let v0_row = row(v0, block_start + index);
            for ((value, &w_v_value), &v0_value) in block.v[range.clone()]
                .iter_mut()
                .zip(&w_v[range.clone()])
                .zip(v0_row)
            {
                *value = lerp(w_v_value, v0_value, *value);
            }
            block.next_v0[range].copy_from_slice(v0_row);
        }
    }

    fill_mixed(4, mixed);
    mixer.a_lora.apply_sigmoid_block_into(mixed, block.a, rows);

    fill_mixed(5, mixed);
    let mut g_hidden = [0.0f32; LINEAR_BLOCK_ROWS * 16];
    let g_hidden = &mut g_hidden[..rows * 16];
    mixer.lora_a_g.apply_block_into(mixed, g_hidden, rows);
    sigmoid_in_place(g_hidden);
    mixer.lora_b_g.apply_block_into(g_hidden, block.g, rows);

    fill_mixed(3, mixed);
    mixer.d_lora.apply_tanh_block_into(mixed, block.w, rows);
    for value in block.w.iter_mut() {
        let d = -0.5 - softplus(-*value);
        *value = (-d.exp()).exp();
    }

    for index in 0..rows {
        let range = index * D_MODEL..(index + 1) * D_MODEL;
        let k_row = &mut block.k[range.clone()];
        normalize_heads_in_place(k_row);
        for head in 0..HEADS {
            let scale = k_scale[index * HEADS + head];
            for i in 0..HEAD_SIZE {
                k_row[head * HEAD_SIZE + i] *= scale;
            }
        }

        let v_row = &mut block.v[range.clone()];
        normalize_heads_in_place(v_row);
        for head in 0..HEADS {
            let scale = v_scale[index * HEADS + head];
            for i in 0..HEAD_SIZE {
                v_row[head * HEAD_SIZE + i] *= scale;
            }
        }

        block.k_deformed[range.clone()].copy_from_slice(&block.k[range.clone()]);
        for channel in range {
            block.k[channel] *= block.a[channel];
        }
    }
}

fn time_outputs(
    mixer: &TimeMixer,
    parts: &BulkTimeMixParts,
    recurrence: &[f32],
    trunk: &[f32],
    chunk_start: usize,
    chunk_rows: usize,
) -> Vec<f32> {
    let mut out = vec![0.0; chunk_rows * D_MODEL];
    out.par_chunks_mut(LINEAR_BLOCK_ROWS * D_MODEL)
        .enumerate()
        .for_each(|(block_index, out_block)| {
            let block_start = block_index * LINEAR_BLOCK_ROWS;
            let rows = out_block.len() / D_MODEL;
            let mut gated = [0.0f32; LINEAR_BLOCK_ROWS * D_MODEL];
            let gated = &mut gated[..rows * D_MODEL];
            for index in 0..rows {
                let chunk_index = block_start + index;
                let gated_row = &mut gated[index * D_MODEL..(index + 1) * D_MODEL];
                mixer
                    .out_group_norm
                    .apply_into(row(recurrence, chunk_index), gated_row);

                let r_row = row(&parts.r, chunk_index);
                let k_row = row(&parts.k, chunk_index);
                let v_row = row(&parts.v, chunk_index);
                let g_row = row(&parts.g, chunk_index);
                for head in 0..HEADS {
                    let base = head * HEAD_SIZE;
                    let mut bonus_scale = 0.0;
                    for i in 0..HEAD_SIZE {
                        bonus_scale += r_row[base + i] * mixer.bonus[base + i] * k_row[base + i];
                    }
                    for i in 0..HEAD_SIZE {
                        let channel = base + i;
                        gated_row[channel] =
                            g_row[channel] * (gated_row[channel] + bonus_scale * v_row[channel]);
                    }
                }
            }
            mixer.w_o.apply_block_into(gated, out_block, rows);
            for index in 0..rows {
                let input = row(trunk, chunk_start + block_start + index);
                for channel in 0..D_MODEL {
                    out_block[index * D_MODEL + channel] += input[channel];
                }
            }
        });
    out
}

#[allow(clippy::too_many_arguments)]
fn channel_outputs(
    cm: &ChannelMixer,
    layer_id: usize,
    plan: &ModulePlan,
    stream_layers: &[Vec<LayerState>],
    chunk_start: usize,
    chunk_rows: usize,
    cm_norm: &[f32],
    cm_norm_answer: &[f32],
    time_out: &[f32],
    trunk: &mut [f32],
) {
    trunk[chunk_start * D_MODEL..(chunk_start + chunk_rows) * D_MODEL]
        .par_chunks_mut(LINEAR_BLOCK_ROWS * D_MODEL)
        .enumerate()
        .for_each(|(block_index, trunk_block)| {
            let block_start = block_index * LINEAR_BLOCK_ROWS;
            let rows = trunk_block.len() / D_MODEL;
            let mut mixed = [0.0f32; LINEAR_BLOCK_ROWS * D_MODEL];
            let mixed = &mut mixed[..rows * D_MODEL];
            for index in 0..rows {
                let chunk_index = block_start + index;
                let own = row(cm_norm, chunk_index);
                let x_shift = resolve_shift(
                    plan,
                    chunk_start + chunk_index,
                    chunk_start,
                    cm_norm_answer,
                    own,
                    |stream_index| {
                        stream_layers[stream_index][layer_id]
                            .channel_shift
                            .as_deref()
                    },
                );
                let out = &mut mixed[index * D_MODEL..(index + 1) * D_MODEL];
                for channel in 0..D_MODEL {
                    out[channel] = lerp(own[channel], x_shift[channel], cm.lerp_k[channel]);
                }
            }

            let channel_dim = cm.w_k.output;
            debug_assert!(channel_dim <= 256);
            let mut k = [0.0f32; LINEAR_BLOCK_ROWS * 256];
            let k = &mut k[..rows * channel_dim];
            cm.w_k.apply_block_into(mixed, k, rows);
            for value in k.iter_mut() {
                *value = value.max(0.0).powi(2);
            }
            cm.w_v.apply_block_into(k, trunk_block, rows);
            for index in 0..rows {
                let input = row(time_out, block_start + index);
                for channel in 0..D_MODEL {
                    trunk_block[index * D_MODEL + channel] += input[channel];
                }
            }
        });
}

fn fill_time_mixed(
    lerp_values: &[f32],
    mix_id: usize,
    x: &[f32],
    x_shift: &[f32],
    mixed: &mut [f32],
) {
    debug_assert_eq!(mixed.len(), D_MODEL);
    let lerp_offset = mix_id * D_MODEL;
    for channel in 0..D_MODEL {
        mixed[channel] = lerp(
            x[channel],
            x_shift[channel],
            lerp_values[lerp_offset + channel],
        );
    }
}
