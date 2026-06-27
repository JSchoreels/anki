// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::env;
use std::fs;
use std::io;
use std::path::PathBuf;
use std::time::Instant;

const D_MODEL: usize = 128;
const CARD_FEATURES: usize = 92;
const HEADS: usize = 4;
const HEAD_SIZE: usize = D_MODEL / HEADS;
const HEAD_DIM: usize = 4 * D_MODEL;
const NUM_CURVES: usize = 128;

const MODULE_LAYERS: [usize; 5] = [3, 4, 2, 3, 4];
const CHANNEL_MIXER_DIMS: [usize; 5] = [192, 256, 192, 256, 256];

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse()?;

    let load_started = Instant::now();
    let model = SrsModel::load(&args.weights)?;
    let trace = Trace::load(&args.trace)?;
    let load_elapsed = load_started.elapsed();

    let mut state = RuntimeState::default();
    let warmup_steps = args.warmup_steps.min(trace.steps.len());
    for step in &trace.steps[..warmup_steps] {
        let (_, next_state) = model.review_probability(&step.features, state.get(step));
        if !step.skip {
            state.store(step, next_state);
        }
    }

    let started = Instant::now();
    let mut max_abs_error = 0.0_f32;
    let mut max_abs_error_step = 0_usize;
    let mut sum_abs_error = 0.0_f64;
    let mut sum_squared_error = 0.0_f64;
    let mut errors = Vec::new();
    let mut checkpoint_errors = Vec::new();
    let mut first_probability = None;
    let mut last_probability = 0.0;
    let mut updates = 0_usize;
    let mut queries = 0_usize;

    for _ in 0..args.repeat {
        for step in &trace.steps[warmup_steps..] {
            let (probability, next_state) =
                model.review_probability(&step.features, state.get(step));
            let abs_error = (probability - step.expected_probability).abs();
            if abs_error > max_abs_error {
                max_abs_error = abs_error;
                max_abs_error_step = updates + queries + warmup_steps;
            }
            sum_abs_error += abs_error as f64;
            sum_squared_error += f64::from(probability - step.expected_probability).powi(2);
            errors.push(abs_error);
            let step_number = updates + queries + warmup_steps + 1;
            if matches!(
                step_number,
                1 | 10 | 100 | 1_000 | 10_000 | 50_000 | 100_000 | 150_000 | 200_000
            ) {
                checkpoint_errors.push((
                    step_number,
                    abs_error,
                    probability,
                    step.expected_probability,
                ));
            }
            first_probability.get_or_insert(probability);
            last_probability = probability;

            if step.skip {
                queries += 1;
            } else {
                state.store(step, next_state);
                updates += 1;
            }
        }
    }

    let elapsed = started.elapsed();
    let op_count = updates + queries;
    println!("weights={}", args.weights.display());
    println!("trace={}", args.trace.display());
    println!("load_ms={:.3}", load_elapsed.as_secs_f64() * 1000.0);
    println!("warmup_steps={warmup_steps}");
    println!("ops={op_count}");
    println!("updates={updates}");
    println!("queries={queries}");
    println!("elapsed_ms={:.3}", elapsed.as_secs_f64() * 1000.0);
    println!(
        "per_op_ms={:.6}",
        elapsed.as_secs_f64() * 1000.0 / op_count.max(1) as f64
    );
    println!("first_probability={:.9}", first_probability.unwrap_or(0.0));
    println!("last_probability={last_probability:.9}");
    println!("max_abs_error={max_abs_error:.9}");
    println!("max_abs_error_step={max_abs_error_step}");
    if !errors.is_empty() {
        errors.sort_by(|left, right| left.total_cmp(right));
        println!("mean_abs_error={:.9}", sum_abs_error / errors.len() as f64);
        println!(
            "rmse={:.9}",
            (sum_squared_error / errors.len() as f64).sqrt()
        );
        println!("p50_abs_error={:.9}", percentile(&errors, 0.50));
        println!("p95_abs_error={:.9}", percentile(&errors, 0.95));
        println!("p99_abs_error={:.9}", percentile(&errors, 0.99));
        println!("p999_abs_error={:.9}", percentile(&errors, 0.999));
        for (step, abs_error, probability, expected) in checkpoint_errors {
            println!(
                "checkpoint step={step} abs_error={abs_error:.9} rust={probability:.9} torch={expected:.9}"
            );
        }
    }

    Ok(())
}

fn percentile(sorted: &[f32], p: f32) -> f32 {
    let index = ((sorted.len() - 1) as f32 * p).round() as usize;
    sorted[index]
}

struct Args {
    weights: PathBuf,
    trace: PathBuf,
    repeat: usize,
    warmup_steps: usize,
}

impl Args {
    fn parse() -> Result<Self, String> {
        let mut weights = None;
        let mut trace = None;
        let mut repeat = 1_usize;
        let mut warmup_steps = 0_usize;
        let mut args = env::args().skip(1);

        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--weights" => {
                    weights = Some(PathBuf::from(
                        args.next().ok_or("--weights requires a path")?,
                    ));
                }
                "--trace" => {
                    trace = Some(PathBuf::from(args.next().ok_or("--trace requires a path")?));
                }
                "--repeat" => {
                    repeat = args
                        .next()
                        .ok_or("--repeat requires a value")?
                        .parse()
                        .map_err(|_| "--repeat must be a positive integer")?;
                }
                "--warmup-steps" => {
                    warmup_steps = args
                        .next()
                        .ok_or("--warmup-steps requires a value")?
                        .parse()
                        .map_err(|_| "--warmup-steps must be a positive integer")?;
                }
                "--help" | "-h" => {
                    return Err(
                        "usage: rwkv_bench --weights weights.bin --trace trace.bin [--repeat N] [--warmup-steps N]"
                            .into(),
                    );
                }
                _ => return Err(format!("unknown argument: {arg}")),
            }
        }

        Ok(Self {
            weights: weights.ok_or("--weights is required")?,
            trace: trace.ok_or("--trace is required")?,
            repeat,
            warmup_steps,
        })
    }
}

struct Trace {
    steps: Vec<TraceStep>,
}

struct TraceStep {
    skip: bool,
    card_id: i64,
    note_id: i64,
    deck_id: i64,
    preset_id: i64,
    features: Vec<f32>,
    expected_probability: f32,
}

impl Trace {
    fn load(path: &PathBuf) -> io::Result<Self> {
        let data = fs::read(path)?;
        let mut cursor = Cursor::new(&data);
        cursor.expect_magic(b"ARWKVTRACE2")?;
        let count = cursor.u32()? as usize;
        let mut steps = Vec::with_capacity(count);
        for _ in 0..count {
            let skip = cursor.u8()? != 0;
            let card_id = cursor.i64()?;
            let note_id = cursor.i64()?;
            let deck_id = cursor.i64()?;
            let preset_id = cursor.i64()?;
            let mut features = Vec::with_capacity(CARD_FEATURES);
            for _ in 0..CARD_FEATURES {
                features.push(cursor.f32()?);
            }
            let expected_probability = cursor.f32()?;
            steps.push(TraceStep {
                skip,
                card_id,
                note_id,
                deck_id,
                preset_id,
                features,
                expected_probability,
            });
        }
        cursor.expect_end()?;
        Ok(Self { steps })
    }
}

struct WeightMap {
    tensors: HashMap<String, Tensor>,
}

struct Tensor {
    shape: Vec<usize>,
    values: Vec<f32>,
}

impl WeightMap {
    fn load(path: &PathBuf) -> io::Result<Self> {
        let data = fs::read(path)?;
        let mut cursor = Cursor::new(&data);
        cursor.expect_magic(b"ARWKVWEIGHTS1")?;
        let count = cursor.u32()? as usize;
        let mut tensors = HashMap::with_capacity(count);

        for _ in 0..count {
            let name_len = cursor.u16()? as usize;
            let name = cursor.string(name_len)?;
            let rank = cursor.u8()? as usize;
            let mut shape = Vec::with_capacity(rank);
            let mut len = 1_usize;
            for _ in 0..rank {
                let dim = cursor.u32()? as usize;
                len = len.checked_mul(dim).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "tensor is too large")
                })?;
                shape.push(dim);
            }
            let mut values = Vec::with_capacity(len);
            for _ in 0..len {
                values.push(cursor.f32()?);
            }
            tensors.insert(name, Tensor { shape, values });
        }

        cursor.expect_end()?;
        Ok(Self { tensors })
    }

    fn values(&self, name: &str) -> io::Result<Vec<f32>> {
        self.tensors
            .get(name)
            .map(|tensor| tensor.values.clone())
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, missing_weight(name)))
    }

    fn linear(&self, name: &str, input: usize, output: usize, bias: bool) -> io::Result<Linear> {
        let weight_name = format!("{name}.weight");
        let bias_name = format!("{name}.bias");
        let weight = self.tensor(&weight_name, &[output, input])?.values.clone();
        let bias = if bias {
            Some(self.tensor(&bias_name, &[output])?.values.clone())
        } else {
            None
        };
        Ok(Linear {
            input,
            output,
            weight,
            bias,
        })
    }

    fn layer_norm(&self, name: &str, dim: usize, eps: f32) -> io::Result<Norm> {
        Ok(Norm {
            groups: 1,
            dim,
            eps,
            weight: self
                .tensor(&format!("{name}.weight"), &[dim])?
                .values
                .clone(),
            bias: self.tensor(&format!("{name}.bias"), &[dim])?.values.clone(),
        })
    }

    fn group_norm(&self, name: &str, groups: usize, dim: usize, eps: f32) -> io::Result<Norm> {
        Ok(Norm {
            groups,
            dim,
            eps,
            weight: self
                .tensor(&format!("{name}.weight"), &[dim])?
                .values
                .clone(),
            bias: self.tensor(&format!("{name}.bias"), &[dim])?.values.clone(),
        })
    }

    fn tensor(&self, name: &str, shape: &[usize]) -> io::Result<&Tensor> {
        let tensor = self
            .tensors
            .get(name)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, missing_weight(name)))?;
        if tensor.shape != shape {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "weight {name} has shape {:?}, expected {shape:?}",
                    tensor.shape
                ),
            ));
        }
        Ok(tensor)
    }
}

fn missing_weight(name: &str) -> String {
    format!("missing weight: {name}")
}

struct Cursor<'a> {
    data: &'a [u8],
    offset: usize,
}

impl<'a> Cursor<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, offset: 0 }
    }

    fn expect_magic(&mut self, magic: &[u8]) -> io::Result<()> {
        let found = self.bytes(magic.len())?;
        if found != magic {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "unexpected file magic",
            ));
        }
        Ok(())
    }

    fn expect_end(&self) -> io::Result<()> {
        if self.offset == self.data.len() {
            Ok(())
        } else {
            Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "trailing bytes in file",
            ))
        }
    }

    fn bytes(&mut self, len: usize) -> io::Result<&'a [u8]> {
        let end = self
            .offset
            .checked_add(len)
            .ok_or_else(|| io::Error::new(io::ErrorKind::UnexpectedEof, "offset overflow"))?;
        let bytes = self
            .data
            .get(self.offset..end)
            .ok_or_else(|| io::Error::new(io::ErrorKind::UnexpectedEof, "file ended early"))?;
        self.offset = end;
        Ok(bytes)
    }

    fn string(&mut self, len: usize) -> io::Result<String> {
        let bytes = self.bytes(len)?;
        String::from_utf8(bytes.to_vec())
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid utf-8 string"))
    }

    fn u8(&mut self) -> io::Result<u8> {
        Ok(self.bytes(1)?[0])
    }

    fn u16(&mut self) -> io::Result<u16> {
        let mut bytes = [0; 2];
        bytes.copy_from_slice(self.bytes(2)?);
        Ok(u16::from_le_bytes(bytes))
    }

    fn u32(&mut self) -> io::Result<u32> {
        let mut bytes = [0; 4];
        bytes.copy_from_slice(self.bytes(4)?);
        Ok(u32::from_le_bytes(bytes))
    }

    fn i64(&mut self) -> io::Result<i64> {
        let mut bytes = [0; 8];
        bytes.copy_from_slice(self.bytes(8)?);
        Ok(i64::from_le_bytes(bytes))
    }

    fn f32(&mut self) -> io::Result<f32> {
        let mut bytes = [0; 4];
        bytes.copy_from_slice(self.bytes(4)?);
        Ok(f32::from_le_bytes(bytes))
    }
}

struct SrsModel {
    features_0: Linear,
    features_norm: Norm,
    features_3: Linear,
    modules: Vec<RwkvModule>,
    prehead_norm: Norm,
    head_w_0: Linear,
    head_w_norm: Norm,
    head_w_4: Linear,
    w_linear: Linear,
    head_ahead_0: Linear,
    ahead_linear: Linear,
    head_p_0: Linear,
    p_linear: Linear,
}

impl SrsModel {
    fn load(path: &PathBuf) -> io::Result<Self> {
        let weights = WeightMap::load(path)?;
        let modules = MODULE_LAYERS
            .iter()
            .enumerate()
            .map(|(module_id, layer_count)| RwkvModule::load(&weights, module_id, *layer_count))
            .collect::<io::Result<Vec<_>>>()?;

        Ok(Self {
            features_0: weights.linear("features2card.0", CARD_FEATURES, HEAD_DIM, true)?,
            features_norm: weights.layer_norm("features2card.2", HEAD_DIM, 1e-5)?,
            features_3: weights.linear("features2card.3", HEAD_DIM, D_MODEL, true)?,
            modules,
            prehead_norm: weights.layer_norm("prehead_norm", D_MODEL, 1e-5)?,
            head_w_0: weights.linear("head_w.0", D_MODEL, D_MODEL, true)?,
            head_w_norm: weights.layer_norm("head_w.2", D_MODEL, 1e-5)?,
            head_w_4: weights.linear("head_w.4", D_MODEL, HEAD_DIM, true)?,
            w_linear: weights.linear("w_linear", HEAD_DIM, NUM_CURVES, true)?,
            head_ahead_0: weights.linear("head_ahead_logits.0", D_MODEL, HEAD_DIM, true)?,
            ahead_linear: weights.linear("ahead_linear", HEAD_DIM, NUM_CURVES, true)?,
            head_p_0: weights.linear("head_p.0", D_MODEL, HEAD_DIM, true)?,
            p_linear: weights.linear("p_linear", HEAD_DIM, 4, true)?,
        })
    }

    fn review_probability(&self, features: &[f32], state: SrsStateRef<'_>) -> (f32, SrsState) {
        let mut x = self.features_0.apply(features);
        silu_in_place(&mut x);
        x = self.features_norm.apply(&x);
        x = self.features_3.apply(&x);
        silu_in_place(&mut x);

        let (x, card_state) = self.modules[0].run(&x, state.card);
        let (x, deck_state) = self.modules[1].run(&x, state.deck);
        let (x, note_state) = self.modules[2].run(&x, state.note);
        let (x, preset_state) = self.modules[3].run(&x, state.preset);
        let (x, global_state) = self.modules[4].run(&x, state.global);

        let x = self.prehead_norm.apply(&x);

        let mut head_w = self.head_w_0.apply(&x);
        relu_in_place(&mut head_w);
        head_w = self.head_w_norm.apply(&head_w);
        head_w = self.head_w_4.apply(&head_w);
        let weights = softmax(&self.w_linear.apply(&head_w));

        let mut ahead = self.head_ahead_0.apply(&x);
        relu_in_place(&mut ahead);
        let ahead_logits = self.ahead_linear.apply(&ahead);

        let mut head_p = self.head_p_0.apply(&x);
        relu_in_place(&mut head_p);
        let logits = self.p_linear.apply(&head_p);
        let probabilities = softmax(&logits);

        let next_state = SrsState {
            card: card_state,
            deck: deck_state,
            note: note_state,
            preset: preset_state,
            global: global_state,
        };

        // Keep the retention head alive in the optimizer-facing graph equivalent.
        std::hint::black_box(weights);
        std::hint::black_box(ahead_logits);

        (1.0 - probabilities[0], next_state)
    }
}

#[derive(Default)]
struct RuntimeState {
    cards: HashMap<i64, ModuleState>,
    decks: HashMap<i64, ModuleState>,
    notes: HashMap<i64, ModuleState>,
    presets: HashMap<i64, ModuleState>,
    global: Option<ModuleState>,
}

impl RuntimeState {
    fn get(&self, step: &TraceStep) -> SrsStateRef<'_> {
        SrsStateRef {
            card: self.cards.get(&step.card_id),
            deck: self.decks.get(&step.deck_id),
            note: self.notes.get(&step.note_id),
            preset: self.presets.get(&step.preset_id),
            global: self.global.as_ref(),
        }
    }

    fn store(&mut self, step: &TraceStep, state: SrsState) {
        self.cards.insert(step.card_id, state.card);
        self.decks.insert(step.deck_id, state.deck);
        self.notes.insert(step.note_id, state.note);
        self.presets.insert(step.preset_id, state.preset);
        self.global = Some(state.global);
    }
}

struct SrsStateRef<'a> {
    card: Option<&'a ModuleState>,
    deck: Option<&'a ModuleState>,
    note: Option<&'a ModuleState>,
    preset: Option<&'a ModuleState>,
    global: Option<&'a ModuleState>,
}

struct SrsState {
    card: ModuleState,
    deck: ModuleState,
    note: ModuleState,
    preset: ModuleState,
    global: ModuleState,
}

struct RwkvModule {
    layers: Vec<RwkvLayer>,
}

impl RwkvModule {
    fn load(weights: &WeightMap, module_id: usize, layer_count: usize) -> io::Result<Self> {
        let mut layers = Vec::with_capacity(layer_count);
        for layer_id in 0..layer_count {
            layers.push(RwkvLayer::load(weights, module_id, layer_id)?);
        }
        Ok(Self { layers })
    }

    fn run(&self, input: &[f32], state: Option<&ModuleState>) -> (Vec<f32>, ModuleState) {
        let mut x = input.to_vec();
        let mut v0 = vec![0.0; D_MODEL];
        let mut next_layers = Vec::with_capacity(self.layers.len());

        for (layer_id, layer) in self.layers.iter().enumerate() {
            let layer_state = state.and_then(|state| state.layers.get(layer_id));
            let (next_x, next_v0, next_layer_state) = layer.run(&x, &v0, layer_state);
            x = next_x;
            v0 = next_v0;
            next_layers.push(next_layer_state);
        }

        (
            x,
            ModuleState {
                layers: next_layers,
            },
        )
    }
}

struct ModuleState {
    layers: Vec<LayerState>,
}

struct RwkvLayer {
    time_mixer: TimeMixer,
    channel_mixer: ChannelMixer,
}

impl RwkvLayer {
    fn load(weights: &WeightMap, module_id: usize, layer_id: usize) -> io::Result<Self> {
        Ok(Self {
            time_mixer: TimeMixer::load(weights, module_id, layer_id)?,
            channel_mixer: ChannelMixer::load(weights, module_id, layer_id)?,
        })
    }

    fn run(
        &self,
        input: &[f32],
        v0: &[f32],
        state: Option<&LayerState>,
    ) -> (Vec<f32>, Vec<f32>, LayerState) {
        let (x, v0, time_state) =
            self.time_mixer
                .run(input, v0, state.and_then(|state| state.time.as_ref()));
        let (x, channel_shift) = self
            .channel_mixer
            .run(&x, state.and_then(|state| state.channel_shift.as_ref()));
        (
            x,
            v0,
            LayerState {
                time: Some(time_state),
                channel_shift: Some(channel_shift),
            },
        )
    }
}

struct LayerState {
    time: Option<TimeState>,
    channel_shift: Option<Vec<f32>>,
}

struct TimeMixer {
    layer_id: usize,
    layer_norm: Norm,
    rkvdag_lerp: Vec<f32>,
    bonus: Vec<f32>,
    w_r: Linear,
    w_k: Linear,
    w_v: Linear,
    w_o: Linear,
    k_scale_linear: Linear,
    v_scale_linear: Linear,
    v_lora: LoraSimple,
    a_lora: LoraSimple,
    d_lora: LoraSimple,
    lora_a_g: Linear,
    lora_b_g: Linear,
    out_group_norm: Norm,
}

impl TimeMixer {
    fn load(weights: &WeightMap, module_id: usize, layer_id: usize) -> io::Result<Self> {
        let prefix = format!("rwkv_modules.{module_id}.blocks.{layer_id}.time_mixer");
        Ok(Self {
            layer_id,
            layer_norm: weights.layer_norm(&format!("{prefix}.layer_norm"), D_MODEL, 1e-5)?,
            rkvdag_lerp: weights.values(&format!("{prefix}.rkvdag_lerp"))?,
            bonus: weights.values(&format!("{prefix}.bonus"))?,
            w_r: weights.linear(&format!("{prefix}.W_r"), D_MODEL, D_MODEL, false)?,
            w_k: weights.linear(&format!("{prefix}.W_k"), D_MODEL, D_MODEL, false)?,
            w_v: weights.linear(&format!("{prefix}.W_v"), D_MODEL, D_MODEL, false)?,
            w_o: weights.linear(&format!("{prefix}.W_o"), D_MODEL, D_MODEL, false)?,
            k_scale_linear: weights.linear(
                &format!("{prefix}.k_scale_linear"),
                D_MODEL,
                HEADS,
                true,
            )?,
            v_scale_linear: weights.linear(
                &format!("{prefix}.v_scale_linear"),
                D_MODEL,
                HEADS,
                true,
            )?,
            v_lora: LoraSimple::load(weights, &format!("{prefix}.v_lora_simple"), 8)?,
            a_lora: LoraSimple::load(weights, &format!("{prefix}.a_lora_simple"), 16)?,
            d_lora: LoraSimple::load(weights, &format!("{prefix}.d_lora_mlp"), 16)?,
            lora_a_g: weights.linear(&format!("{prefix}.lora_A_g"), D_MODEL, 16, false)?,
            lora_b_g: weights.linear(&format!("{prefix}.lora_B_g"), 16, D_MODEL, false)?,
            out_group_norm: weights.group_norm(
                &format!("{prefix}.out_group_norm"),
                HEADS,
                D_MODEL,
                64e-5,
            )?,
        })
    }

    fn run(
        &self,
        input: &[f32],
        v0: &[f32],
        state: Option<&TimeState>,
    ) -> (Vec<f32>, Vec<f32>, TimeState) {
        let x = self.layer_norm.apply(input);
        let (x_shift, state_matrix) = match state {
            Some(state) => (state.x_shift.as_slice(), state.matrix.as_slice()),
            None => (x.as_slice(), &[0.0; HEADS * HEAD_SIZE * HEAD_SIZE][..]),
        };

        let mut mixed = vec![vec![0.0; D_MODEL]; 8];
        for (mix_id, mixed_row) in mixed.iter_mut().enumerate() {
            let lerp_offset = mix_id * D_MODEL;
            for channel in 0..D_MODEL {
                mixed_row[channel] = lerp(
                    x[channel],
                    x_shift[channel],
                    self.rkvdag_lerp[lerp_offset + channel],
                );
            }
        }

        let r = self.w_r.apply(&mixed[0]);
        let mut k = self.w_k.apply(&mixed[1]);
        let mut k_scale = self.k_scale_linear.apply(&mixed[6]);
        sigmoid_in_place(&mut k_scale);
        let mut v_scale = self.v_scale_linear.apply(&mixed[7]);
        sigmoid_in_place(&mut v_scale);

        let (v, next_v0) = if self.layer_id == 0 {
            let v = self.w_v.apply(&mixed[2]);
            (v.clone(), v)
        } else {
            let mut v_lerp = self.v_lora.apply_sigmoid(&mixed[2]);
            let w_v = self.w_v.apply(&mixed[2]);
            for channel in 0..D_MODEL {
                v_lerp[channel] = lerp(w_v[channel], v0[channel], v_lerp[channel]);
            }
            (v_lerp, v0.to_vec())
        };

        let a = self.a_lora.apply_sigmoid(&mixed[4]);
        let mut g = self.lora_a_g.apply(&mixed[5]);
        sigmoid_in_place(&mut g);
        g = self.lora_b_g.apply(&g);

        let mut d = self.d_lora.apply_tanh(&mixed[3]);
        for value in &mut d {
            *value = -0.5 - softplus(-*value);
        }
        let w = d
            .iter()
            .map(|value| (-value.exp()).exp())
            .collect::<Vec<_>>();

        normalize_heads_in_place(&mut k);
        for head in 0..HEADS {
            for index in 0..HEAD_SIZE {
                k[head * HEAD_SIZE + index] *= k_scale[head];
            }
        }

        let mut v = v;
        normalize_heads_in_place(&mut v);
        for head in 0..HEADS {
            for index in 0..HEAD_SIZE {
                v[head * HEAD_SIZE + index] *= v_scale[head];
            }
        }

        let k_deformed = k.clone();
        for channel in 0..D_MODEL {
            k[channel] *= a[channel];
        }

        let (mut out, next_matrix) = single_timestep(&r, &k, &v, &w, &a, &k_deformed, state_matrix);
        out = self.out_group_norm.apply(&out);

        let mut bonus = vec![0.0; D_MODEL];
        for head in 0..HEADS {
            let base = head * HEAD_SIZE;
            let mut bonus_scale = 0.0;
            for index in 0..HEAD_SIZE {
                bonus_scale += r[base + index] * self.bonus[base + index] * k[base + index];
            }
            for index in 0..HEAD_SIZE {
                bonus[base + index] = bonus_scale * v[base + index];
            }
        }

        for channel in 0..D_MODEL {
            out[channel] = g[channel] * (out[channel] + bonus[channel]);
        }
        let out = self.w_o.apply(&out);
        let mut next = vec![0.0; D_MODEL];
        for channel in 0..D_MODEL {
            next[channel] = input[channel] + out[channel];
        }

        (
            next,
            next_v0,
            TimeState {
                x_shift: x,
                matrix: next_matrix,
            },
        )
    }
}

struct TimeState {
    x_shift: Vec<f32>,
    matrix: Vec<f32>,
}

struct ChannelMixer {
    layer_norm: Norm,
    lerp_k: Vec<f32>,
    w_k: Linear,
    w_v: Linear,
}

impl ChannelMixer {
    fn load(weights: &WeightMap, module_id: usize, layer_id: usize) -> io::Result<Self> {
        let channel_dim = CHANNEL_MIXER_DIMS[module_id];
        let prefix = format!("rwkv_modules.{module_id}.blocks.{layer_id}.channel_mixer");
        Ok(Self {
            layer_norm: weights.layer_norm(&format!("{prefix}.layer_norm"), D_MODEL, 1e-5)?,
            lerp_k: weights.values(&format!("{prefix}.lerp_k"))?,
            w_k: weights.linear(&format!("{prefix}.W_k"), D_MODEL, channel_dim, false)?,
            w_v: weights.linear(&format!("{prefix}.W_v"), channel_dim, D_MODEL, false)?,
        })
    }

    fn run(&self, input: &[f32], state: Option<&Vec<f32>>) -> (Vec<f32>, Vec<f32>) {
        let x = self.layer_norm.apply(input);
        let x_shift = state.map_or(x.as_slice(), |state| state.as_slice());
        let mut mixed = vec![0.0; D_MODEL];
        for channel in 0..D_MODEL {
            mixed[channel] = lerp(x[channel], x_shift[channel], self.lerp_k[channel]);
        }

        let mut k = self.w_k.apply(&mixed);
        for value in &mut k {
            *value = value.max(0.0).powi(2);
        }
        let out = self.w_v.apply(&k);
        let mut next = vec![0.0; D_MODEL];
        for channel in 0..D_MODEL {
            next[channel] = input[channel] + out[channel];
        }
        (next, x)
    }
}

struct LoraSimple {
    a: Linear,
    b: Linear,
}

impl LoraSimple {
    fn load(weights: &WeightMap, prefix: &str, rank: usize) -> io::Result<Self> {
        Ok(Self {
            a: weights.linear(&format!("{prefix}.A"), D_MODEL, rank, false)?,
            b: weights.linear(&format!("{prefix}.B_and_lamb"), rank, D_MODEL, true)?,
        })
    }

    fn apply_sigmoid(&self, input: &[f32]) -> Vec<f32> {
        let mut out = self.b.apply(&self.a.apply(input));
        sigmoid_in_place(&mut out);
        out
    }

    fn apply_tanh(&self, input: &[f32]) -> Vec<f32> {
        let mut hidden = self.a.apply(input);
        for value in &mut hidden {
            *value = value.tanh();
        }
        self.b.apply(&hidden)
    }
}

struct Linear {
    input: usize,
    output: usize,
    weight: Vec<f32>,
    bias: Option<Vec<f32>>,
}

impl Linear {
    fn apply(&self, input: &[f32]) -> Vec<f32> {
        debug_assert_eq!(input.len(), self.input);
        let mut out = vec![0.0; self.output];
        for (row, output) in out.iter_mut().enumerate() {
            let weight_row = &self.weight[row * self.input..(row + 1) * self.input];
            let mut sum = dot_product(input, weight_row);
            sum += self.bias.as_ref().map_or(0.0, |bias| bias[row]);
            *output = sum;
        }
        out
    }
}

#[inline(always)]
fn dot_product(left: &[f32], right: &[f32]) -> f32 {
    debug_assert_eq!(left.len(), right.len());
    dot_product_arch(left, right)
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
fn dot_product_arch(left: &[f32], right: &[f32]) -> f32 {
    // SAFETY: aarch64 guarantees NEON support, and the helper only uses
    // unaligned loads within the bounds checked by its loop conditions.
    unsafe { dot_product_neon(left, right) }
}

#[cfg(not(target_arch = "aarch64"))]
#[inline(always)]
fn dot_product_arch(left: &[f32], right: &[f32]) -> f32 {
    dot_product_scalar(left, right)
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
unsafe fn dot_product_neon(left: &[f32], right: &[f32]) -> f32 {
    use std::arch::aarch64::*;

    let mut offset = 0;
    let len = left.len();
    let mut acc0 = vdupq_n_f32(0.0);
    let mut acc1 = vdupq_n_f32(0.0);
    let mut acc2 = vdupq_n_f32(0.0);
    let mut acc3 = vdupq_n_f32(0.0);

    while offset + 16 <= len {
        let left_ptr = left.as_ptr().add(offset);
        let right_ptr = right.as_ptr().add(offset);
        acc0 = vfmaq_f32(acc0, vld1q_f32(left_ptr), vld1q_f32(right_ptr));
        acc1 = vfmaq_f32(
            acc1,
            vld1q_f32(left_ptr.add(4)),
            vld1q_f32(right_ptr.add(4)),
        );
        acc2 = vfmaq_f32(
            acc2,
            vld1q_f32(left_ptr.add(8)),
            vld1q_f32(right_ptr.add(8)),
        );
        acc3 = vfmaq_f32(
            acc3,
            vld1q_f32(left_ptr.add(12)),
            vld1q_f32(right_ptr.add(12)),
        );
        offset += 16;
    }

    let mut acc = vaddq_f32(vaddq_f32(acc0, acc1), vaddq_f32(acc2, acc3));
    while offset + 4 <= len {
        acc = vfmaq_f32(
            acc,
            vld1q_f32(left.as_ptr().add(offset)),
            vld1q_f32(right.as_ptr().add(offset)),
        );
        offset += 4;
    }

    let mut sum = vaddvq_f32(acc);
    while offset < len {
        sum += left[offset] * right[offset];
        offset += 1;
    }
    sum
}

#[cfg(not(target_arch = "aarch64"))]
#[inline(always)]
fn dot_product_scalar(left: &[f32], right: &[f32]) -> f32 {
    left.iter()
        .zip(right)
        .map(|(left, right)| left * right)
        .sum()
}

struct Norm {
    groups: usize,
    dim: usize,
    eps: f32,
    weight: Vec<f32>,
    bias: Vec<f32>,
}

impl Norm {
    fn apply(&self, input: &[f32]) -> Vec<f32> {
        debug_assert_eq!(input.len(), self.dim);
        let group_size = self.dim / self.groups;
        let mut out = vec![0.0; self.dim];

        for group in 0..self.groups {
            let start = group * group_size;
            let end = start + group_size;
            let values = &input[start..end];
            let mean = values.iter().sum::<f32>() / group_size as f32;
            let variance = values
                .iter()
                .map(|value| {
                    let diff = value - mean;
                    diff * diff
                })
                .sum::<f32>()
                / group_size as f32;
            let scale = (variance + self.eps).sqrt().recip();
            for index in start..end {
                out[index] = (input[index] - mean) * scale * self.weight[index] + self.bias[index];
            }
        }

        out
    }
}

fn single_timestep(
    r: &[f32],
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    k_deformed: &[f32],
    state: &[f32],
) -> (Vec<f32>, Vec<f32>) {
    let mut next_state = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];
    let mut out = vec![0.0; D_MODEL];

    for head in 0..HEADS {
        let head_base = head * HEAD_SIZE;
        let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
        let mut state_dot_k = [0.0_f32; HEAD_SIZE];
        let key_deformed = &k_deformed[head_base..head_base + HEAD_SIZE];
        let receptance = &r[head_base..head_base + HEAD_SIZE];

        for (row, value) in state_dot_k.iter_mut().enumerate() {
            let row_start = matrix_base + row * HEAD_SIZE;
            let state_row = &state[row_start..row_start + HEAD_SIZE];
            *value = dot_product(state_row, key_deformed);
        }

        for row in 0..HEAD_SIZE {
            for column in 0..HEAD_SIZE {
                let channel = head_base + column;
                let old = state[matrix_base + row * HEAD_SIZE + column];
                next_state[matrix_base + row * HEAD_SIZE + column] = old * w[channel]
                    - state_dot_k[row] * a[channel] * k_deformed[channel]
                    + v[head_base + row] * k[channel];
            }
        }

        for row in 0..HEAD_SIZE {
            let row_start = matrix_base + row * HEAD_SIZE;
            let state_row = &next_state[row_start..row_start + HEAD_SIZE];
            out[head_base + row] = dot_product(state_row, receptance);
        }
    }

    (out, next_state)
}

fn normalize_heads_in_place(values: &mut [f32]) {
    for head in 0..HEADS {
        let start = head * HEAD_SIZE;
        let end = start + HEAD_SIZE;
        let norm = values[start..end]
            .iter()
            .map(|value| value * value)
            .sum::<f32>()
            .sqrt()
            .max(1e-12);
        for value in &mut values[start..end] {
            *value /= norm;
        }
    }
}

fn softmax(input: &[f32]) -> Vec<f32> {
    let max = input
        .iter()
        .copied()
        .fold(f32::NEG_INFINITY, |a, b| a.max(b));
    let mut out = input
        .iter()
        .map(|value| (*value - max).exp())
        .collect::<Vec<_>>();
    let sum = out.iter().sum::<f32>();
    for value in &mut out {
        *value /= sum;
    }
    out
}

fn sigmoid_in_place(values: &mut [f32]) {
    for value in values {
        *value = sigmoid(*value);
    }
}

fn sigmoid(value: f32) -> f32 {
    if value >= 0.0 {
        1.0 / (1.0 + (-value).exp())
    } else {
        let exp = value.exp();
        exp / (1.0 + exp)
    }
}

fn softplus(value: f32) -> f32 {
    if value > 20.0 {
        value
    } else if value < -20.0 {
        value.exp()
    } else {
        value.exp().ln_1p()
    }
}

fn silu_in_place(values: &mut [f32]) {
    for value in values {
        *value *= sigmoid(*value);
    }
}

fn relu_in_place(values: &mut [f32]) {
    for value in values {
        *value = value.max(0.0);
    }
}

fn lerp(start: f32, end: f32, weight: f32) -> f32 {
    start + weight * (end - start)
}
